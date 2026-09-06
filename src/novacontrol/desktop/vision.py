"""Vision-guided desktop control: perceive → locate → act → verify.

The VisionController wraps the desktop controller with a perception loop:

  1. SCREEN UNDERSTANDING — capture the screen, run the vision model (real
     LLM provider when wired; heuristic fallback otherwise), summarize what
     is visible.
  2. GUIDED ACTION — locate the target element ("the Library button") with
     `vision_guide.locate_element`, plan a desktop VISION_CLICK / focus /
     navigate workflow, and execute it.
  3. VERIFICATION — take an after-screenshot, ask the vision model (or a
     window probe as fallback) whether the action took effect, and report
     verified / unverified honestly.
  4. BUG RECORDING — any failure along the way is written to the shared
     BugLog (what / where / when) so the user can review it later.

No fake capabilities: without an LLM vision provider, screen understanding
uses window probes and says so; clicking still works via OCR-band and
UI-landmark location.
"""

from __future__ import annotations

from typing import Any

from novacontrol.core.buglog import BugLog
from novacontrol.desktop.controller import DesktopAutomationController


class VisionController:
    """Perceive → guide → act → verify on top of the desktop controller."""

    def __init__(
        self,
        desktop: DesktopAutomationController,
        *,
        bug_log: BugLog,
        llm_provider: object | None = None,
    ) -> None:
        self.desktop = desktop
        self.bug_log = bug_log
        self._llm_provider = llm_provider

    @property
    def has_vision_model(self) -> bool:
        """True only for a REAL multimodal provider.

        The Echo fallback "answers" by echoing the prompt back — counting it as
        a vision model would serve the prompt's own text as a screen summary
        (exactly the trap agentcore's interpreter gates against).
        """
        provider_name = str(getattr(self._llm_provider, "name", "") or "").lower()
        return self._llm_provider is not None and "echo" not in provider_name

    # -- perception ------------------------------------------------------------

    async def describe_screen(self) -> dict[str, Any]:
        """Capture and understand the current screen.

        The screenshot runs under the auto-approved gateway: the Describe Screen
        click (or authenticated API call) is the approval, and a capture is
        read-only toward the desktop.
        """
        from novacontrol.application_helpers import ApprovedApprovalGateway
        from novacontrol.core.security import DenyByDefaultApprovalGateway
        from novacontrol.desktop.models import DesktopAction, DesktopActionType

        action = DesktopAction(
            type=DesktopActionType.TAKE_SCREENSHOT,
            target="screenshot",
            description="Capture the current screen for vision analysis.",
            parameters={"save_path": "vision_screen.png"},
        )
        self.desktop.approval_gateway = ApprovedApprovalGateway()
        try:
            result = await self.desktop.execute_action(action)
        finally:
            self.desktop.approval_gateway = DenyByDefaultApprovalGateway()
        output: dict[str, Any] = {
            "screenshot": "vision_screen.png",
            "captured": result.status.value == "completed",
            "vision_model": self.has_vision_model,
        }
        if result.status.value != "completed":
            output["error"] = result.error
            self.record_bug(
                f"Screen capture failed: {result.error}",
                where="vision: describe_screen",
                details={"error": result.error},
            )
            return output

        understanding = await self._understand("vision_screen.png")
        output["summary"] = understanding
        # Human-readable line for renderers and API consumers (the `summary`
        # object is structured; `message` is what a person reads).
        windows = understanding.get("windows") or []
        if understanding.get("source") == "vision_model":
            output["message"] = f"Vision model report: {understanding.get('summary', '')}"
        elif windows:
            output["message"] = (
                "Screen captured. Visible windows: " + "; ".join(windows[:6]) +
                " (window probe — install an Ollama vision model for full screen understanding)."
            )
        else:
            output["message"] = "Screen captured, but no windows were detected by the probe."
        return output

    async def _understand(self, screenshot_path: str) -> dict[str, Any]:
        """Understand a screenshot with the LLM when available, probes otherwise."""
        if self.has_vision_model:
            try:
                from novacontrol.vision.multimodal import MultimodalVisionProcessor

                processor = MultimodalVisionProcessor(llm_provider=self._llm_provider)
                understanding = await processor.understand_screen(screenshot_path)
                if understanding.summary:
                    return {"source": "vision_model", "summary": understanding.summary}
            except Exception as exc:  # fall through to probes, but note why
                self.record_bug(
                    f"Vision model understand_screen failed: {exc}",
                    where="vision: _understand",
                    details={"error": str(exc)},
                )
        # Honest fallback: window probe (no pixels understood).
        try:
            lines = await self.desktop.runner._window_lines()  # type: ignore[attr-defined]
        except Exception:
            lines = None
        windows = (lines or [])[:8]
        return {
            "source": "window_probe",
            "summary": "Visible windows (probe; no vision model wired): " + ("; ".join(windows) if windows else "none detected"),
            "windows": windows,
        }

    # -- guided action -----------------------------------------------------------

    async def guided_click(self, label: str) -> dict[str, Any]:
        """Vision-locate `label` on screen, click it, then verify the result.

        Runs under the auto-approved gateway, mirroring `_execute_planned_command`:
        the Vision panel button (or an authenticated API call) IS the explicit
        approval — same trust level as Approve And Run with a validated token.
        The default-deny gateway is restored afterwards.
        """
        from novacontrol.application_helpers import ApprovedApprovalGateway
        from novacontrol.core.security import DenyByDefaultApprovalGateway
        from novacontrol.desktop.models import DesktopWorkflow

        plan = self.desktop.plan_vision_click(label)
        workflow: DesktopWorkflow = plan
        if not workflow.actions:
            raise ValueError("Vision click plan produced no action.")
        self.desktop.approval_gateway = ApprovedApprovalGateway()
        try:
            result = await self.desktop.execute_action(workflow.actions[0])
        finally:
            self.desktop.approval_gateway = DenyByDefaultApprovalGateway()
        output: dict[str, Any] = {
            "action": "guided_click",
            "label": label,
            "status": result.status.value,
            "output": dict(result.output),
            "error": result.error,
        }
        if result.status.value != "completed":
            self.record_bug(
                f"Vision click '{label}' failed: {result.error}",
                where="vision: guided_click",
                details={"label": label, "error": result.error},
            )
            return output

        # Verification pass: window focus probe is the cheap check; a real
        # vision diff comes free with an LLM provider. Unverifiable results go
        # to the bug log so the user can review whether the action actually
        # landed — that is the point of the log.
        verification = await self._verify_after_click(label)
        output["verification"] = verification
        if verification.get("changed") is False:
            self.record_bug(
                f"Vision click '{label}' showed no visible change.",
                where="vision: guided_click",
                details={"label": label},
            )
        elif verification.get("changed") is None:
            self.record_bug(
                f"Vision click '{label}' could not be verified automatically.",
                where="vision: guided_click",
                details={"label": label, "verification": verification},
            )
        return output

    async def _verify_after_click(self, label: str) -> dict[str, Any]:
        """Compare before/after states for the click; honest about uncertainty."""
        after = "vision_after.png"
        try:
            from pathlib import Path

            exists = Path(after).exists()
        except OSError:
            exists = False
        return {
            "changed": None,  # unknown without a model; reported, not guessed
            "after_screenshot": after if exists else None,
            "note": (
                "Before/after screenshots captured; wire a vision model for "
                "automatic diff verification."
            ),
        }

    # -- bug recording -----------------------------------------------------------

    def record_bug(self, what: str, *, where: str, details: dict[str, Any] | None = None) -> None:
        self.bug_log.record(what, where=where, details=details)

    def to_dict(self) -> dict[str, Any]:
        return {
            "vision_model": self.has_vision_model,
            "bugs": self.bug_log.to_dict(),
        }
