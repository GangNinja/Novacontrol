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

import re
from pathlib import Path
from typing import Any

from novacontrol.core.buglog import BugLog
from novacontrol.desktop.controller import DesktopAutomationController
from novacontrol.integrations.llm import provider_supports_vision


def _cv2_frame_diff(before_file: Path, after_file: Path) -> dict[str, Any]:
    """OpenCV fast path for the before/after verification diff.

    numpy-backed absdiff + threshold counts changed pixels at C speed (the
    pure-Pillow path walks millions of pixels in a Python loop), and
    connectedComponentsWithStats localizes WHERE the screen changed — the top
    change regions let verification say whether the click site itself reacted.
    Raises FileNotFoundError / ValueError for the honest not-comparable cases.
    """
    import cv2

    before = cv2.imread(str(before_file))
    after = cv2.imread(str(after_file))
    if before is None:
        raise FileNotFoundError(str(before_file))
    if after is None:
        raise FileNotFoundError(str(after_file))
    if before.shape != after.shape:
        raise ValueError(
            "Screenshot sizes differ "
            f"({before.shape[1]}x{before.shape[0]} vs {after.shape[1]}x{after.shape[0]}); not comparable."
        )
    diff = cv2.absdiff(before, after)
    # Max channel movement per pixel: grayscale alone would miss color-only flips.
    motion = diff.max(axis=2)
    # Below 8/255 is screen-capture compression noise, not a visible change.
    _threshold, mask = cv2.threshold(motion, 8, 255, cv2.THRESH_BINARY)
    changed_pixels = int(cv2.countNonZero(mask))
    total = int(mask.size)
    changed_pct = round(100 * changed_pixels / total, 2) if total else 0.0

    regions: list[dict[str, int]] = []
    if changed_pixels:
        count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
            mask, connectivity=8
        )
        # stats rows are [x, y, w, h, area]; component 0 is the background.
        components = sorted(
            (tuple(int(v) for v in stats[i]) for i in range(1, count)),
            key=lambda size: size[4],
            reverse=True,
        )[:5]
        regions = [
            {"x": s[0], "y": s[1], "w": s[2], "h": s[3], "area": s[4]}
            for s in components
        ]
    return {"changed_pct": changed_pct, "regions": regions, "engine": "opencv"}


def _pillow_frame_diff(before_file: Path, after_file: Path) -> dict[str, Any]:
    """Pure-Pillow fallback when OpenCV is unavailable (no region data)."""
    from PIL import Image, ImageChops

    with Image.open(before_file) as before, Image.open(after_file) as after:
        if before.size != after.size:
            raise ValueError(
                f"Screenshot sizes differ ({before.size} vs {after.size}); not comparable."
            )
        width, height = before.size
        diff = ImageChops.difference(before.convert("RGB"), after.convert("RGB"))
        # diff.getdata() yields per-pixel tuples; under the untyped ImagingCore
        # stub mypy sees an opaque object, so the count goes through an
        # explicit conversion it accepts.
        pixels = list(diff.getdata())
        # Same noise floor as the OpenCV path: any channel moving by >= 8
        # counts, so both engines agree on what a visible change is.
        changed_pixels = sum(1 for px in pixels if max(px) >= 8)
        total = width * height
    return {
        "changed_pct": round(100 * changed_pixels / total, 2),
        "regions": [],
        "engine": "pillow",
    }


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

    def set_llm_provider(self, provider: object | None) -> None:
        """Hot-swap the vision model (None = OCR/landmarks only, no restart).

        Used by the application when a vision model is configured or cleared:
        the next describe_screen/guided_click uses the new provider."""
        self._llm_provider = provider

    @property
    def has_vision_model(self) -> bool:
        """True only for a REAL multimodal provider.

        The Echo fallback "answers" by echoing the prompt back — counting it as
        a vision model would serve the prompt's own text as a screen summary
        (exactly the trap agentcore's interpreter gates against). Not-Echo is
        necessary but NOT sufficient: the application wires the chat brain's
        provider in by default, and on a local-Ollama machine that is usually a
        TEXT-ONLY model (qwen3, llama3.2, …) that cannot see an image at all,
        so provider_supports_vision asks the provider what it can really do.
        """
        return provider_supports_vision(self._llm_provider)

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

    # A vision click targets ONE visible UI element — a word on screen. The
    # Vision panel's click box kept receiving whole COMMANDS ("open chrome and
    # search nlp in geeksforgeeks"), which can never be located as an element
    # and produced pure noise bug entries. These markers say "this is an
    # instruction", not a label; anything matching is rejected with guidance.
    _COMMAND_SHAPED_LABEL = re.compile(
        r"\b(open|launch|start|run|close|stop|search|type|press|click on|go to|kill)\b"
        r"|\s+and\s+|\bplease\b",
        re.IGNORECASE,
    )

    def _guard_label(self, label: str) -> None:
        """Reject command-shaped labels with a clear, actionable error."""
        if self._COMMAND_SHAPED_LABEL.search(label):
            raise ValueError(
                f"'{label}' is a command, not a screen element. Vision click targets ONE "
                "word or short phrase visible on screen (e.g. 'File', 'Library', 'Play'). "
                "Send the command to the JARVIS panel instead — it plans and verifies "
                "the whole chain."
            )

    async def guided_click(self, label: str) -> dict[str, Any]:
        """Vision-locate `label` on screen, click it, then verify the result.

        Runs under the auto-approved gateway, mirroring `_execute_planned_command`:
        the Vision panel button (or an authenticated API call) IS the explicit
        approval — same trust level as Approve And Run with a validated token.
        The default-deny gateway is restored afterwards.
        """
        self._guard_label(label)
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

        # Verification pass: pixel-diff of the before/after captures the click
        # already took, plus an OCR re-check of the label's presence near the
        # click point. ONLY when the runner really executed the click — a
        # safe/noop runner returns no capture paths, and diffing whatever stale
        # screenshots happen to sit on disk would be a false "verified".
        output_data = result.output
        if "before_path" not in output_data or "after_path" not in output_data:
            verification: dict[str, Any] = {
                "changed": None,
                "after_screenshot": None,
                "note": (
                    "The desktop runner did not execute a real click "
                    "(safe/noop runner) — there is nothing to verify."
                ),
            }
        else:
            verification = await self._verify_after_click(
                label,
                before_path=str(output_data["before_path"]),
                after_path=str(output_data["after_path"]),
            )
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
        else:
            # The pixel-diff proves the click visibly changed the screen: any
            # still-open bug logged against THIS flow + label (an earlier
            # failure or unverifiable attempt) is resolved with this evidence,
            # so the log reflects that the flow now verifiably works.
            resolved = self.bug_log.resolve_matching(
                where="vision: guided_click",
                label=label,
                evidence={
                    "changed_pct": verification.get("changed_pct"),
                    "after_screenshot": verification.get("after_screenshot"),
                },
            )
            if resolved:
                output["resolved_bugs"] = resolved
        return output

    async def _verify_after_click(
        self,
        label: str,
        before_path: str = "vision_locate.png",
        after_path: str = "vision_after.png",
    ) -> dict[str, Any]:
        """Diff the before/after screenshots for real verification evidence.

        The click itself captured both frames; this compares them: pixel-diff
        percentage (OpenCV fast path when available, Pillow otherwise), where
        the change regions are (cv2 connected components — ``change_near_click``
        says whether the click site itself reacted), whether the OCR text around
        the click point changed, and whether the label's own pixels near the
        click point disappeared (the strongest signal a button was actually
        pressed — a pressed button highlights, inverts, or opens a menu over
        its old pixels). Honest by construction: 'changed' is True/False only
        when both frames exist and are comparable; any missing frame yields
        None with the reason.
        """
        before_file = Path(before_path)
        after_file = Path(after_path)
        if not (before_file.exists() and after_file.exists()):
            return {
                "changed": None,
                "after_screenshot": after_path if after_file.exists() else None,
                "note": "Before/after screenshots unavailable for comparison.",
            }
        try:
            # OpenCV fast path (numpy diff + change-region localization);
            # falls back to pure Pillow when cv2 is not installed.
            try:
                diff_result = _cv2_frame_diff(before_file, after_file)
            except ImportError:
                diff_result = _pillow_frame_diff(before_file, after_file)
            changed_pct = float(diff_result["changed_pct"])
            regions: list[dict[str, int]] = diff_result.get("regions", [])
            engine = str(diff_result.get("engine", "pillow"))

            # Text-level check: re-OCR the after frame and see whether the
            # label is still present near the click point (a 400px band).
            # The runner writes _last_click_point during its click (the
            # controller object itself never has it — reading from there
            # silently disabled this OCR re-check).
            click_point = getattr(getattr(self.desktop, "runner", None), "_last_click_point", None)
            x, y = click_point or (0, 0)
            label_gone = None
            if click_point:
                from novacontrol.desktop.vision_guide import _ocr_words

                words = await _ocr_words(after_path) or []
                band_left, band_top = max(0, x - 200), max(0, y - 100)
                band_right, band_bottom = x + 200, y + 100
                near = [
                    w for w in words
                    if band_left <= int(w["x"]) <= band_right
                    and band_top <= int(w["y"]) <= band_bottom
                ]
                from novacontrol.intelligence.normalize import normalize

                target = normalize(label).strip()
                still_there = any(
                    target in normalize(str(w["text"])) for w in near
                )
                label_gone = not still_there
        except Exception as exc:
            return {
                "changed": None,
                "after_screenshot": after_path,
                "note": f"Verification comparison failed: {exc}",
            }
        changed = changed_pct > 0.05  # below 0.05% = compression noise
        # Region evidence: did any detected change land on the click site?
        change_near_click: bool | None = None
        if click_point and regions:
            change_near_click = any(
                r["x"] <= x <= r["x"] + r["w"] and r["y"] <= y <= r["y"] + r["h"]
                for r in regions
            )
        return {
            "changed": changed,
            "changed_pct": changed_pct,
            "label_gone_near_click": label_gone,
            "change_near_click": change_near_click,
            "regions": regions,
            "diff_engine": engine,
            "after_screenshot": after_path,
            "note": (
                f"Screen changed by {changed_pct}% after the click"
                + ("; the label's text left the click area (button likely activated)" if label_gone else "")
                + "."
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
