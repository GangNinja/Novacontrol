"""Recovery Engine: diagnose failures and generate alternative strategies.

A failure is never terminal on the first attempt. The engine inspects the
failed action, the current observation, and the knowledge graph to pick one of
five strategies, in escalation order:

    RETRY           transient failure, environment unchanged, nothing delivered
    RECHECK         input may already have landed; re-observe, never resend
    RELOCATE        the target is genuinely gone; a DIFFERENT element matches
    RESEARCH        we do not know how to do this here; ask the research agent
    ABORT           destructive/irreversible, identity change, or repeated failure

The successful strategy is recorded so future runs skip the failure entirely.

LIM-2 (2026-11-26): the engine used to treat any element whose text merely
matched the failed target as a *relocated* target, including the target itself
when it was still present. Combined with the orchestrator's automatic replan,
that could re-deliver a click to the element that had just been clicked. The
engine now classifies delivery (`input_delivery_state`) and target identity,
reports a present target accurately, never relocates onto the same target, and
actions whose input may already have been delivered are only ever re-observed
and re-verified (`RECHECK`) — never replayed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from novacontrol.agentcore.knowledge import ApplicationKnowledgeGraph
from novacontrol.agentcore.state import ActionRecord
from novacontrol.agentcore.ui_state import UiState


class RecoveryStrategy(StrEnum):
    RETRY = "retry"
    RECHECK = "recheck"  # re-observe and re-verify only; never re-sends input
    RELOCATE = "relocate"
    RESEARCH = "research"
    ABORT = "abort"


#: Actions that change application state, deliver input, or move a position that
#: a second attempt would advance again. Repeating one of these automatically can
#: duplicate or compound a real effect, so they are never replayed merely because
#: their expected effect was not observed. This list covers both the labels the
#: agentcore engine emits today and every `DesktopActionType` that delivers input,
#: so a future wiring cannot inherit a retry by accident.
NON_IDEMPOTENT_ACTIONS: frozenset[str] = frozenset(
    {
        "type",
        "type_text",
        "click",
        "vision_click",
        "press",
        "press_key",
        "keyboard_shortcut",
        "key_down",
        "key_up",
        "drag",
        "mouse_move",
        "mouse_click",
        "mouse_button_down",
        "mouse_button_up",
        "mouse_scroll",
        "mouse_drag",
        "scroll",
        "save",
        "paste",
        "copy",
        "select_all",
        "go_back",
        "compose_message",
        "execute_terminal_command",
        "execute_script",
        "delete",
        "write_file",
        "organize_files",
        "close_application",
        "stop_app",
        "download",
        "upload",
        "web_search",
        "search_start_menu",
        "search_in_app",
        "app_navigate",
        "send",
        "submit",
    }
)

#: Actions that are safe to repeat: they read state, or they activate/launch a
#: surface. Repeating one cannot duplicate an effect on the target's content —
#: the worst case is a second window or tab. This is an ALLOWLIST: anything not
#: named here (including an action kind added later) is treated as input, so an
#: unclassified action can never be retried automatically.
IDEMPOTENT_ACTIONS: frozenset[str] = frozenset(
    {
        "observe",
        "reason",
        "wait",
        "noop",
        "screenshot",
        "take_screenshot",
        "window_details",
        "window_geometry",
        "list_processes",
        "list_files",
        "read_file",
        "system_info",
        "focus_window",
        "window_focus",
        "minimize_window",
        "window_minimize",
        "maximize_window",
        "window_maximize",
        "open_application",
        "open_folder",
        "game_launch",
        "navigate",
    }
)

#: Failure details that prove the input never reached the target. Anything else
#: after an input failure is *uncertain*: keys may already have been sent.
_NOT_DELIVERED_MARKERS: tuple[str, ...] = (
    "refusing",
    "could not focus",
    "no matching element",
    "not found",
    "no observed point",
    "no desktop runner",
    "no browser runner",
    "no desktop or browser runner",
    "no window bound",
    "no objective-bound window",
    "names no text",
    "unsupported",
    "not approved",
    "requires approval",
    "denied",
    "permission",
)


def input_delivery_state(record: ActionRecord) -> str:
    """Whether this action's input may already have reached its target.

    ``delivered``     the input API returned success; the effect is unobserved
    ``not_delivered`` the action failed before sending anything
    ``uncertain``     input may or may not have been sent (interruption, a failed
                      post-send verification, a timeout)
    ``not_input``     the action is on the retry-safe allowlist (observe, focus,
                      launch, navigation)

    Classification fails CLOSED: anything that is not explicitly retry-safe is
    treated as input, so an action kind nobody classified is never replayed.
    """
    action = str(record.action or "").strip().lower()
    if action in IDEMPOTENT_ACTIONS:
        return "not_input"
    output = record.output if isinstance(record.output, dict) else {}
    detail = f"{record.detail} {output.get('error', '')}".lower()
    if any(marker in detail for marker in _NOT_DELIVERED_MARKERS):
        return "not_delivered"
    if record.success:
        return "delivered"
    return "uncertain"


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    strategy: RecoveryStrategy
    diagnosis: str
    alternative_target: str = ""       # for RELOCATE: the new semantic label/selector
    alternative_selector: str = ""
    strategy_text: str = ""            # human-readable plan for the orchestrator
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy.value,
            "diagnosis": self.diagnosis,
            "alternative_target": self.alternative_target,
            "alternative_selector": self.alternative_selector,
            "strategy_text": self.strategy_text,
            "confidence": self.confidence,
        }


class RecoveryEngine:
    """Diagnoses action failures and selects an alternative strategy."""

    def __init__(self, *, knowledge: ApplicationKnowledgeGraph | None = None, max_attempts: int = 2) -> None:
        self._knowledge = knowledge or ApplicationKnowledgeGraph()
        self._max_attempts = max_attempts

    def diagnose(
        self,
        failed: ActionRecord,
        *,
        current_state: UiState | None,
        application: str,
        prior_attempts: int = 0,
        expected_identity: tuple[int, int] | None = None,
    ) -> RecoveryOutcome:
        app = application or current_state.application if current_state else application
        output = failed.output or {}
        error_text = str(output.get("error") or failed.detail or "").lower()
        delivery = input_delivery_state(failed)
        grounding, grounding_text = self._ground_target(
            failed, current_state, expected_identity
        )

        # Hard stop: destructive action failures are never auto-retried.
        if failed.action in ("execute_terminal_command", "delete", "download", "upload"):
            return RecoveryOutcome(
                RecoveryStrategy.ABORT,
                "Destructive action failed; automatic retry is not safe.",
                strategy_text="Ask the user before retrying this action.",
                confidence=1.0,
            )

        # Hard stop: missing dependencies cannot be fixed by retrying.
        dependency_markers = (
            "not installed", "no module named", "executable not found",
            "is not available", "not found on path",
        )
        if any(marker in error_text for marker in dependency_markers):
            return RecoveryOutcome(
                RecoveryStrategy.ABORT,
                "A required dependency is missing; retrying cannot install it.",
                strategy_text="Install the missing dependency, then re-run the task.",
                confidence=0.95,
            )

        # Repeated failure: stop hammering.
        if prior_attempts >= self._max_attempts:
            return RecoveryOutcome(
                RecoveryStrategy.ABORT,
                f"Action failed {prior_attempts + 1} times; retry budget exhausted.",
                strategy_text="Surface the failure to the user with the diagnosis.",
                confidence=1.0,
            )

        # A target whose identity changed is not a relocation candidate: another
        # process wearing the same title is a different window (D12), and
        # ambiguity means no window can be chosen safely.
        if grounding == "present_changed":
            return RecoveryOutcome(
                RecoveryStrategy.ABORT,
                f"{grounding_text} Refusing to continue against a replacement window.",
                strategy_text="Stop and ask the user; the intended window is gone.",
                confidence=1.0,
            )
        if grounding == "ambiguous":
            return RecoveryOutcome(
                RecoveryStrategy.ABORT,
                f"{grounding_text} Refusing to guess which one is meant.",
                strategy_text="Stop and ask the user to identify the window.",
                confidence=0.95,
            )

        # The input may already have landed. Nothing may be re-sent: re-observe
        # and re-verify only, and report the presence of the target accurately.
        if delivery in ("delivered", "uncertain"):
            if delivery == "uncertain":
                diagnosis = (
                    f"Delivery of {failed.action} to {failed.target!r} could not be "
                    "confirmed after the failure; repeating it could duplicate input."
                )
                if grounding == "present_same":
                    diagnosis = f"{grounding_text} {diagnosis}"
            elif grounding == "present_same":
                diagnosis = (
                    f"{grounding_text} The {failed.action} was delivered, but its "
                    "expected effect was not observed."
                )
            else:
                diagnosis = (
                    f"The {failed.action} to {failed.target!r} was delivered, but its "
                    "expected effect was not observed."
                )
            return RecoveryOutcome(
                RecoveryStrategy.RECHECK,
                diagnosis,
                strategy_text="Re-observe and re-verify; do not resend input.",
                confidence=0.6,
            )

        # From here the action is non-input, or its input provably never reached
        # the target, so retrying cannot duplicate a delivered effect.
        if grounding == "present_same":
            return RecoveryOutcome(
                RecoveryStrategy.RETRY,
                f"{grounding_text} The previous attempt did not reach it.",
                strategy_text=(
                    f"Re-observe, then retry: {failed.action} {failed.target!r}."
                ),
                confidence=0.6,
            )

        # A genuinely absent target: another element may carry the same purpose.
        # The failed target itself is never a relocation candidate.
        relocated = self._find_relocation(failed.target, current_state)
        if relocated is not None:
            element, confidence = relocated
            known = self._knowledge.best_path(app, element.text) or element.selector
            return RecoveryOutcome(
                RecoveryStrategy.RELOCATE,
                f"Target {failed.target!r} is not present; a different element "
                f"{element.text!r} matches it.",
                alternative_target=element.text,
                alternative_selector=element.selector or str(known or ""),
                strategy_text=f"Use the relocated element {element.text!r} instead.",
                confidence=confidence,
            )

        # Environment changed underneath us (navigation, dialog) -> re-observe
        # is already implied; a known alternative path short-circuits research.
        if "timeout" in error_text or "not found" in error_text or "no matching element" in error_text:
            known = self._knowledge.best_path(app, failed.target)
            if known:
                return RecoveryOutcome(
                    RecoveryStrategy.RELOCATE,
                    "The expected element was not found, but application knowledge has an alternative path.",
                    alternative_target=known,
                    strategy_text=f"Use the known alternative path: {known}",
                    confidence=self._knowledge.confidence_for(app, known),
                )
            return RecoveryOutcome(
                RecoveryStrategy.RESEARCH,
                "The element is missing and there is no stored alternative path.",
                strategy_text=f"Research how to accomplish {failed.target!r} in {app}; then retry with the documented procedure.",
                confidence=0.5,
            )

        # Everything else: one clean retry (transient focus/timing issues).
        return RecoveryOutcome(
            RecoveryStrategy.RETRY,
            "No structural cause identified; treating as a transient failure.",
            strategy_text=f"Re-observe, then retry: {failed.action} {failed.target!r}.",
            confidence=0.55,
        )

    def record_recovery(self, application: str, failed_target: str, outcome: RecoveryOutcome, *, succeeded: bool) -> None:
        """Persist what worked (or did not) into application knowledge."""
        if succeeded and outcome.strategy is RecoveryStrategy.RELOCATE and outcome.alternative_target:
            self._knowledge.record_alternative_path(
                application=application,
                failed_target=failed_target,
                alternative=outcome.alternative_target,
                confidence=max(0.6, outcome.confidence),
            )

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _ground_target(
        failed: ActionRecord,
        state: UiState | None,
        expected_identity: tuple[int, int] | None,
    ) -> tuple[str, str]:
        """Classify the failed target against the fresh observation.

        Status is one of ``present_same``, ``present_changed``, ``absent``,
        ``ambiguous`` or ``unknown`` (no objective-bound target was reported).
        The sentence is written for the operator, not the log: it must not say a
        target is gone when it is still there.
        """
        if state is None:
            return "unknown", "No fresh observation is available."
        data = (state.metadata or {}).get("target")
        if not isinstance(data, dict) or str(data.get("status")) in ("", "none_requested"):
            return "unknown", "No objective-bound target was reported in this observation."
        status = str(data.get("status"))
        raw_ref = data.get("ref")
        ref: dict[str, Any] = raw_ref if isinstance(raw_ref, dict) else {}
        try:
            identity = (int(ref.get("handle") or 0), int(ref.get("pid") or 0))
        except (TypeError, ValueError):
            identity = (0, 0)
        title = str(ref.get("title") or "")
        if status == "ambiguous":
            return (
                "ambiguous",
                f"More than one observed window matches {failed.target!r}.",
            )
        if status in ("identity_changed", "retitled"):
            return (
                "present_changed",
                f"A window titled {title or failed.target!r} exists, but it is not the "
                "window that was acted on.",
            )
        if status != "bound":
            return "absent", f"Target {failed.target!r} is not present in this observation."
        if expected_identity and identity != tuple(expected_identity):
            return (
                "present_changed",
                f"The window named {failed.target!r} changed identity "
                f"({tuple(expected_identity)} -> {identity}).",
            )
        return (
            "present_same",
            f"Target {failed.target!r} is still present with the same identity.",
        )

    @staticmethod
    def _find_relocation(target: str, state: UiState | None) -> tuple[Any, float] | None:
        """A DIFFERENT element that may serve the failed target's purpose.

        An element whose text is the failed target is excluded: a target that is
        gone cannot be the element wearing its text, and relocating onto it is
        how a delivered click could be repeated.
        """
        needle = target.strip().lower()
        if not needle or state is None:
            return None

        def stem(word: str) -> str:
            return word.rstrip("s")  # light plural folding: integrations ~ integration

        def normalized(text: str) -> str:
            return " ".join(text.replace("text=", " ").lower().split())

        needle_words = {stem(word) for word in needle.split()}
        best: tuple[Any, float] | None = None
        for element in state.interactive():
            hay = f"{element.text} {element.selector}".lower()
            if not hay.strip():
                continue
            # Never "relocate" onto the failed target itself.
            if normalized(element.text) == normalized(needle):
                continue
            if element.selector and normalized(element.selector) == f"text={normalized(needle)}":
                continue
            hay_words = {stem(word) for word in hay.replace("text=", "").split()}
            overlap = len(needle_words & hay_words)
            if overlap == 0 and not any(word in hay for word in needle_words if len(word) > 3):
                continue
            confidence = min(0.9, 0.5 + 0.1 * overlap)
            if best is None or confidence > best[1]:
                best = (element, confidence)
        return best
