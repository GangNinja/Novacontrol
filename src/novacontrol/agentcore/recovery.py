"""Recovery Engine: diagnose failures and generate alternative strategies.

A failure is never terminal on the first attempt. The engine inspects the
failed action, the current observation, and the knowledge graph to pick one of
four strategies, in escalation order:

    RETRY           transient failure, environment unchanged
    RELOCATE        the semantic target moved; search the fresh UI for it
    RESEARCH        we do not know how to do this here; ask the research agent
    ABORT           destructive/irreversible or repeated failure

The successful strategy is recorded so future runs skip the failure entirely.
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
    RELOCATE = "relocate"
    RESEARCH = "research"
    ABORT = "abort"


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
    ) -> RecoveryOutcome:
        app = application or current_state.application if current_state else application
        output = failed.output or {}
        error_text = str(output.get("error") or failed.detail or "").lower()

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

        # Target disappeared but something similar exists now -> RELOCATE.
        if current_state is not None:
            relocated = self._find_relocation(failed.target, current_state)
            if relocated is not None:
                element, confidence = relocated
                known = self._knowledge.best_path(app, element.text) or element.selector
                return RecoveryOutcome(
                    RecoveryStrategy.RELOCATE,
                    f"Target {failed.target!r} is gone but a similar element {element.text!r} exists.",
                    alternative_target=element.text,
                    alternative_selector=element.selector or str(known or ""),
                    strategy_text=f"Click the relocated element {element.text!r} instead.",
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
    def _find_relocation(target: str, state: UiState) -> tuple[Any, float] | None:
        needle = target.strip().lower()
        if not needle:
            return None
        def stem(word: str) -> str:
            return word.rstrip("s")  # light plural folding: integrations ~ integration

        needle_words = {stem(word) for word in needle.split()}
        best: tuple[Any, float] | None = None
        for element in state.interactive():
            hay = f"{element.text} {element.selector}".lower()
            if not hay.strip():
                continue
            hay_words = {stem(word) for word in hay.replace("text=", "").split()}
            overlap = len(needle_words & hay_words)
            if overlap == 0 and not any(word in hay for word in needle_words if len(word) > 3):
                continue
            confidence = min(0.9, 0.5 + 0.1 * overlap)
            if best is None or confidence > best[1]:
                best = (element, confidence)
        return best
