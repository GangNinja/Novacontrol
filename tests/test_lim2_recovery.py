"""LIM-2 regression tests: safe recovery after an unverified input action.

No real desktop input is sent: observations are fixtures and every action engine
is a recorder. The suite pins two properties:

  * a target that is still present is never reported as gone and never relocated;
  * input that may already have been delivered is re-observed, never replayed.
"""

from __future__ import annotations

import unittest

from novacontrol.agentcore.actions import ActionEngine, ActionOutcome
from novacontrol.agentcore.orchestrator import AgenticOrchestrator
from novacontrol.agentcore.perception import UiPerceptionEngine
from novacontrol.agentcore.planner import AdaptivePlanner, AgentPlan, AgentPlanStep, StepKind
from novacontrol.agentcore.recovery import (
    IDEMPOTENT_ACTIONS,
    NON_IDEMPOTENT_ACTIONS,
    RecoveryEngine,
    RecoveryStrategy,
    input_delivery_state,
)
from novacontrol.agentcore.state import ActionRecord
from novacontrol.agentcore.ui_state import UiElement, UiState

TITLE = "NovaGroup1MidAction"
HANDLE = 7123
PID = 4242


def _window(
    handle: int = HANDLE, pid: int = PID, title: str = TITLE, foreground: bool = True
) -> dict:
    return {
        "title": title,
        "handle": handle,
        "pid": pid,
        "process": "python.exe",
        "bounds": [100, 100, 800, 600],
        "foreground": foreground,
    }


def _binding(status: str = "bound", *, handle: int = HANDLE, pid: int = PID) -> dict:
    ref = {
        "title": TITLE,
        "handle": handle,
        "pid": pid,
        "process": "python.exe",
        "foreground": True,
    }
    return {
        "target": {
            "status": status,
            "requested": TITLE,
            "ref": ref if status == "bound" else None,
            "candidates": [TITLE] if status == "ambiguous" else [],
            "reason": "fixture",
        }
    }


def _state(*texts: str, metadata: dict | None = None, window: str = TITLE) -> UiState:
    elements = tuple(
        UiElement(id=f"win:{HANDLE}", type="window", text=text, location=(200, 200))
        for text in texts
    )
    return UiState(
        application="desktop",
        window=window,
        elements=elements,
        raw_text="\n".join(texts),
        metadata=metadata or {},
    )


def _record(action: str, *, success: bool, detail: str = "", target: str = TITLE) -> ActionRecord:
    return ActionRecord(step_id="s1", action=action, target=target, success=success, detail=detail)


class DeliveryClassificationTests(unittest.TestCase):
    def test_successful_input_is_delivered(self) -> None:
        self.assertEqual(input_delivery_state(_record("type", success=True)), "delivered")

    def test_refusal_is_not_delivered(self) -> None:
        self.assertEqual(
            input_delivery_state(
                _record("type", success=False, detail="Refusing to type: no window")
            ),
            "not_delivered",
        )

    def test_failed_post_send_verification_is_uncertain(self) -> None:
        self.assertEqual(
            input_delivery_state(
                _record(
                    "type",
                    success=False,
                    detail="VerificationError: Typed text was not verified: no window is focused",
                )
            ),
            "uncertain",
        )

    def test_non_input_action_is_not_input(self) -> None:
        self.assertEqual(input_delivery_state(_record("focus_window", success=False)), "not_input")

    def test_type_and_click_are_non_idempotent(self) -> None:
        self.assertIn("type", NON_IDEMPOTENT_ACTIONS)
        self.assertIn("click", NON_IDEMPOTENT_ACTIONS)

    def test_unclassified_action_fails_closed(self) -> None:
        # A kind nobody classified must be treated as input, not as retry-safe.
        self.assertEqual(
            input_delivery_state(_record("some_future_action", success=True)), "delivered"
        )
        self.assertEqual(
            input_delivery_state(_record("some_future_action", success=False)), "uncertain"
        )

    def test_unclassified_action_is_rechecked_not_retried(self) -> None:
        outcome = RecoveryEngine().diagnose(
            _record("some_future_action", success=False),
            current_state=_state(TITLE, metadata=_binding()),
            application="desktop",
            expected_identity=(HANDLE, PID),
        )
        self.assertEqual(outcome.strategy, RecoveryStrategy.RECHECK)

    def test_go_back_is_not_treated_as_retry_safe(self) -> None:
        self.assertIn("go_back", NON_IDEMPOTENT_ACTIONS)
        outcome = RecoveryEngine().diagnose(
            _record("go_back", success=False),
            current_state=_state(TITLE, metadata=_binding()),
            application="desktop",
            expected_identity=(HANDLE, PID),
        )
        self.assertEqual(outcome.strategy, RecoveryStrategy.RECHECK)

    def test_every_engine_action_label_is_deliberately_classified(self) -> None:
        # Every label actions.py and the orchestrator guards can put on a record.
        labels = (
            "click",
            "type",
            "save",
            "navigate",
            "go_back",
            "scroll",
            "drag",
            "press_key",
            "select_all",
            "copy",
            "paste",
            "open_application",
            "focus_window",
            "minimize_window",
            "maximize_window",
            "close_application",
            "screenshot",
            "execute_terminal_command",
            "observe",
            "reason",
        )
        for label in labels:
            self.assertTrue(
                label in NON_IDEMPOTENT_ACTIONS or label in IDEMPOTENT_ACTIONS,
                f"{label} is not classified",
            )


class DiagnosisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = RecoveryEngine()
        self.state = _state(TITLE, metadata=_binding())

    def test_present_same_identity_is_not_relocated(self) -> None:
        outcome = self.engine.diagnose(
            _record("click", success=True, detail="clicked"),
            current_state=self.state,
            application="desktop",
            expected_identity=(HANDLE, PID),
        )
        self.assertEqual(outcome.strategy, RecoveryStrategy.RECHECK)
        self.assertIn("still present with the same identity", outcome.diagnosis)
        self.assertEqual(outcome.alternative_target, "")

    def test_delivered_but_unobserved_effect_rechecks(self) -> None:
        outcome = self.engine.diagnose(
            _record("type", success=True, detail="typed"),
            current_state=self.state,
            application="desktop",
            expected_identity=(HANDLE, PID),
        )
        self.assertEqual(outcome.strategy, RecoveryStrategy.RECHECK)
        self.assertIn("was delivered", outcome.diagnosis)

    def test_uncertain_delivery_rechecks(self) -> None:
        outcome = self.engine.diagnose(
            _record("type", success=False, detail="VerificationError: Typed text was not verified"),
            current_state=self.state,
            application="desktop",
            expected_identity=(HANDLE, PID),
        )
        self.assertEqual(outcome.strategy, RecoveryStrategy.RECHECK)
        self.assertIn("could not be confirmed", outcome.diagnosis)

    def test_genuinely_absent_target_relocates_to_a_different_element(self) -> None:
        state = _state("Integration center", metadata=_binding("absent"))
        outcome = self.engine.diagnose(
            _record("click", success=False, detail="no matching element", target="Integrations"),
            current_state=state,
            application="desktop",
            expected_identity=(HANDLE, PID),
        )
        self.assertEqual(outcome.strategy, RecoveryStrategy.RELOCATE)
        self.assertNotEqual(outcome.alternative_target, TITLE)

    def test_relocation_never_returns_the_failed_target(self) -> None:
        state = _state(TITLE, metadata=_binding("absent"))
        outcome = self.engine.diagnose(
            _record("click", success=False, detail="no matching element"),
            current_state=state,
            application="desktop",
        )
        self.assertNotEqual(outcome.strategy, RecoveryStrategy.RELOCATE)

    def test_identity_changed_is_aborted(self) -> None:
        outcome = self.engine.diagnose(
            _record("click", success=True),
            current_state=_state(TITLE, metadata=_binding("identity_changed")),
            application="desktop",
            expected_identity=(HANDLE, PID),
        )
        self.assertEqual(outcome.strategy, RecoveryStrategy.ABORT)
        self.assertIn("not the window that was acted on", outcome.diagnosis)

    def test_same_title_different_handle_is_identity_change(self) -> None:
        outcome = self.engine.diagnose(
            _record("type", success=True),
            current_state=_state(TITLE, metadata=_binding(handle=9999, pid=PID)),
            application="desktop",
            expected_identity=(HANDLE, PID),
        )
        self.assertEqual(outcome.strategy, RecoveryStrategy.ABORT)
        self.assertIn("changed identity", outcome.diagnosis)

    def test_similar_title_different_process_is_identity_change(self) -> None:
        outcome = self.engine.diagnose(
            _record("type", success=True),
            current_state=_state(TITLE, metadata=_binding(handle=HANDLE, pid=5151)),
            application="desktop",
            expected_identity=(HANDLE, PID),
        )
        self.assertEqual(outcome.strategy, RecoveryStrategy.ABORT)

    def test_ambiguous_target_is_aborted(self) -> None:
        outcome = self.engine.diagnose(
            _record("click", success=True),
            current_state=_state(TITLE, TITLE + " 2", metadata=_binding("ambiguous")),
            application="desktop",
            expected_identity=(HANDLE, PID),
        )
        self.assertEqual(outcome.strategy, RecoveryStrategy.ABORT)
        self.assertIn("More than one", outcome.diagnosis)

    def test_not_delivered_retry_is_allowed(self) -> None:
        outcome = self.engine.diagnose(
            _record("type", success=False, detail="Refusing to type: no bound window"),
            current_state=self.state,
            application="desktop",
            expected_identity=(HANDLE, PID),
        )
        self.assertEqual(outcome.strategy, RecoveryStrategy.RETRY)
        self.assertIn("did not reach it", outcome.diagnosis)

    def test_idempotent_action_may_retry(self) -> None:
        outcome = self.engine.diagnose(
            _record("focus_window", success=False, detail="could not focus"),
            current_state=self.state,
            application="desktop",
            expected_identity=(HANDLE, PID),
        )
        self.assertEqual(outcome.strategy, RecoveryStrategy.RETRY)


class _Frames:
    """One observation per call; each frame is one window description."""

    def __init__(self, *frames: dict) -> None:
        self._frames = [[dict(frame)] for frame in frames]
        self.calls = 0

    async def __call__(self) -> list[dict]:
        index = min(self.calls, len(self._frames) - 1)
        self.calls += 1
        return [dict(item) for item in self._frames[index]]


class _RecordingActions(ActionEngine):
    def __init__(self, *, succeed: bool = True, detail: str = "") -> None:
        super().__init__()
        self.typed: list[dict[str, object]] = []
        self.clicked: list[dict[str, object]] = []
        self._succeed = succeed
        self._detail = detail

    async def type_into_active(
        self, text: str, *, window_title: str = "", window_handle: int = 0, window_pid: int = 0
    ) -> ActionOutcome:
        self.typed.append(
            {"text": text, "window": window_title, "handle": window_handle, "pid": window_pid}
        )
        return ActionOutcome(
            self._succeed, "type", window_title, detail=self._detail or "recorded"
        )

    async def click_element(
        self, ui_state: UiState, label: str, *, button: str = "left", count: int = 1,
        require_bound_target: bool = False,
    ) -> ActionOutcome:
        self.clicked.append({"label": label, "bound": require_bound_target})
        return ActionOutcome(self._succeed, "click", label, detail=self._detail or "recorded")


class _FixedPlanner(AdaptivePlanner):
    def __init__(self, steps: tuple[AgentPlanStep, ...]) -> None:
        super().__init__()
        self._steps = steps

    def create_plan(self, request: str, *, interpretation: dict | None = None) -> AgentPlan:
        return AgentPlan(goal=request, steps=self._steps)


def _type_step() -> AgentPlanStep:
    return AgentPlanStep(
        kind=StepKind.DESKTOP,
        description=f"type the phrase hello world into the {TITLE} window",
        target="the phrase hello world",
        expected="a phrase that never appears in these fixtures",
    )


def _click_step() -> AgentPlanStep:
    return AgentPlanStep(
        kind=StepKind.DESKTOP,
        description="click Save",
        target="Save",
        expected="a marker that never appears in these fixtures",
    )


def _orchestrator(*frames: dict, actions: _RecordingActions, steps: tuple[AgentPlanStep, ...]):
    perception = UiPerceptionEngine(window_probe=None, window_details_probe=_Frames(*frames))
    return AgenticOrchestrator(
        perception=perception,
        actions=actions,
        planner=_FixedPlanner(steps),
        max_plan_iterations=5,
    )


class OrchestratorRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivered_typing_is_rechecked_not_typed_twice(self) -> None:
        actions = _RecordingActions()
        orchestrator = _orchestrator(
            _window(), _window(), _window(), actions=actions, steps=(_type_step(),)
        )
        state = await orchestrator.run_task(f"type the phrase hello world into the {TITLE} window")

        self.assertEqual(len(actions.typed), 1)
        self.assertEqual(len(state.actions), 1)
        self.assertEqual(state.status.value, "failed")
        self.assertEqual(state.recoveries[0]["strategy"], "recheck")
        self.assertEqual(state.recoveries[0]["delivery"], "delivered")
        self.assertTrue(any("not repeated" in error for error in state.errors))
        self.assertIn("recheck", " ".join(state.verification))
        self.assertEqual(actions.typed[0]["handle"], HANDLE)

    async def test_delivered_click_is_not_clicked_twice(self) -> None:
        actions = _RecordingActions()
        orchestrator = _orchestrator(
            _window(), _window(), _window(), actions=actions, steps=(_click_step(),)
        )
        state = await orchestrator.run_task(f"click Save in the {TITLE} window")

        self.assertEqual(len(actions.clicked), 1)
        self.assertEqual(len(state.actions), 1)
        self.assertEqual(state.recoveries[0]["strategy"], "recheck")

    async def test_not_delivered_failure_retries_through_the_target_gate(self) -> None:
        actions = _RecordingActions(succeed=False, detail="Refusing to type: no bound window")
        orchestrator = _orchestrator(
            _window(), _window(), _window(), _window(), _window(),
            actions=actions, steps=(_type_step(),),
        )
        state = await orchestrator.run_task(f"type hello into the {TITLE} window")

        self.assertEqual(state.recoveries[0]["strategy"], "retry")
        self.assertGreaterEqual(len(actions.typed), 2)
        for call in actions.typed:
            self.assertEqual(call["window"], TITLE)
            self.assertEqual(call["handle"], HANDLE)
            self.assertEqual(call["pid"], PID)
        # The durable record distinguishes the original from the retry attempt.
        type_records = [row for row in state.actions if row.action == "type"]
        self.assertGreaterEqual(len(type_records), 2)
        self.assertNotEqual(type_records[0].step_id, type_records[1].step_id)
        self.assertIn(":retry:", type_records[1].step_id)
        self.assertNotIn(":retry:", type_records[0].step_id)

    async def test_absent_target_sends_nothing(self) -> None:
        actions = _RecordingActions()
        unrelated = dict(_window(), title="Realtek Audio Console", handle=4001, pid=111)
        orchestrator = _orchestrator(unrelated, actions=actions, steps=(_type_step(),))
        state = await orchestrator.run_task(f"type hello into the {TITLE} window")

        self.assertEqual(actions.typed, [])
        self.assertEqual(state.actions[-1].action, "refused")
        self.assertEqual(state.status.value, "failed")

    async def test_durable_history_keeps_one_record_for_the_original_action(self) -> None:
        actions = _RecordingActions()
        orchestrator = _orchestrator(
            _window(), _window(), _window(), actions=actions, steps=(_type_step(),)
        )
        state = await orchestrator.run_task(f"type hello into the {TITLE} window")
        type_records = [row for row in state.actions if row.action == "type"]
        self.assertEqual(len(type_records), 1)
        self.assertEqual(type_records[0].step_id, state.actions[0].step_id)


if __name__ == "__main__":
    unittest.main()
