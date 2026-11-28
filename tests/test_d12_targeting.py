"""D12 regression tests: objective-bound window targeting.

These tests never touch a real desktop. Every window observation is a fixture and
the only action engine is a recorder, so the original failure — an objective about
one application delivering input to another foreground application — can be
reproduced and asserted without sending a single keystroke to any app.
"""

from __future__ import annotations

import unittest

from novacontrol.agentcore.actions import ActionEngine, ActionOutcome
from novacontrol.agentcore.orchestrator import AgenticOrchestrator
from novacontrol.agentcore.perception import UiPerceptionEngine
from novacontrol.agentcore.planner import AdaptivePlanner, AgentPlan, AgentPlanStep, StepKind
from novacontrol.agentcore.targeting import (
    WindowBinding,
    WindowRef,
    bind_window,
    binding_from_dict,
    requested_target,
    revalidate,
)
from novacontrol.agentcore.verifier import Expectation, VerificationStatus, Verifier

# The exact applications from the failing operator run.
REALTEK = {
    "title": "Realtek Audio Console",
    "handle": 4001,
    "pid": 111,
    "process": "RtkAudUService64.exe",
    "bounds": [0, 0, 1440, 900],
    "foreground": True,
}
PROBE = {
    "title": "NovaGroup1MidAction - Tk",
    "handle": 7123,
    "pid": 4242,
    "process": "python.exe",
    "bounds": [100, 100, 800, 600],
    "foreground": False,
}
GOAL = "type the phrase hello world into the NovaGroup1MidAction window"


def _refs(*windows: dict) -> tuple[WindowRef, ...]:
    return tuple(
        WindowRef(
            title=str(item["title"]),
            handle=int(item["handle"]),
            pid=int(item["pid"]),
            process=str(item.get("process", "")),
            foreground=bool(item.get("foreground")),
        )
        for item in windows
    )


class RequestedTargetTests(unittest.TestCase):
    def test_window_phrase_is_extracted(self) -> None:
        self.assertEqual(requested_target(GOAL), "NovaGroup1MidAction")

    def test_into_phrase_is_extracted(self) -> None:
        self.assertEqual(requested_target("type hello into Notepad"), "Notepad")

    def test_quoted_title_wins(self) -> None:
        self.assertEqual(
            requested_target('type hi into "Build Notes.txt - Notepad"'),
            "Build Notes.txt - Notepad",
        )

    def test_generic_roles_are_not_targets(self) -> None:
        self.assertEqual(requested_target("search the web in the browser window"), "")
        self.assertEqual(requested_target("type hello"), "")


class BindingTests(unittest.TestCase):
    def test_unique_target_binds_with_identity(self) -> None:
        binding = bind_window("NovaGroup1MidAction", _refs(REALTEK, PROBE))
        self.assertTrue(binding.ok)
        self.assertEqual(binding.ref, _refs(PROBE)[0])  # type: ignore[union-attr]
        self.assertEqual(binding.ref.identity, (7123, 4242))  # type: ignore[union-attr]

    def test_first_titled_window_is_not_a_fallback(self) -> None:
        binding = bind_window("NovaGroup1MidAction", _refs(REALTEK))
        self.assertFalse(binding.ok)
        self.assertEqual(binding.status, "absent")
        self.assertIsNone(binding.ref)

    def test_similarly_named_windows_are_ambiguous(self) -> None:
        helper = dict(PROBE, title="NovaGroup1MidAction Helper - Tk", handle=8001, pid=4343)
        binding = bind_window("NovaGroup1MidAction", _refs(PROBE, helper))
        self.assertFalse(binding.ok)
        self.assertEqual(binding.status, "ambiguous")
        self.assertEqual(len(binding.candidates), 2)

    def test_duplicate_exact_titles_are_ambiguous(self) -> None:
        twin = dict(PROBE, handle=9999, pid=4949)
        binding = bind_window("NovaGroup1MidAction - Tk", _refs(PROBE, twin))
        self.assertEqual(binding.status, "ambiguous")

    def test_revalidate_detects_identity_change_under_same_title(self) -> None:
        bound = bind_window("NovaGroup1MidAction", _refs(PROBE))
        impostor = dict(PROBE, handle=9999, pid=4949)
        self.assertEqual(revalidate(bound, _refs(impostor)).status, "identity_changed")

    def test_revalidate_detects_disappearance(self) -> None:
        bound = bind_window("NovaGroup1MidAction", _refs(PROBE))
        self.assertEqual(revalidate(bound, _refs(REALTEK)).status, "absent")

    def test_revalidate_detects_retitle(self) -> None:
        bound = bind_window("NovaGroup1MidAction", _refs(PROBE))
        renamed = dict(PROBE, title="Something Else")
        self.assertEqual(revalidate(bound, _refs(renamed)).status, "retitled")

    def test_binding_roundtrips_through_metadata(self) -> None:
        bound = bind_window("NovaGroup1MidAction", _refs(PROBE))
        self.assertEqual(binding_from_dict(bound.to_dict()), bound)


class _Frames:
    """A window probe that replays fixture frames, repeating the last one."""

    def __init__(self, *frames: tuple[dict, ...]) -> None:
        self._frames = list(frames)
        self.calls = 0

    async def __call__(self) -> list[dict]:
        index = min(self.calls, len(self._frames) - 1)
        self.calls += 1
        return [dict(item) for item in self._frames[index]]


class _RecordingActions(ActionEngine):
    def __init__(self) -> None:
        super().__init__()
        self.typed: list[dict[str, object]] = []

    async def type_into_active(
        self, text: str, *, window_title: str = "", window_handle: int = 0, window_pid: int = 0
    ) -> ActionOutcome:
        self.typed.append(
            {"text": text, "window": window_title, "handle": window_handle, "pid": window_pid}
        )
        return ActionOutcome(
            True, "type", window_title, detail="recorded", output={"handle": window_handle}
        )


class _FixedPlanner(AdaptivePlanner):
    def __init__(self, steps: tuple[AgentPlanStep, ...]) -> None:
        super().__init__()
        self._steps = steps

    def create_plan(self, request: str, *, interpretation: dict | None = None) -> AgentPlan:
        return AgentPlan(goal=request, steps=self._steps)


def _type_step(text: str = "the phrase hello world") -> AgentPlanStep:
    return AgentPlanStep(
        kind=StepKind.DESKTOP,
        description=f"type {text}",
        target=text,
        expected="",
    )


def _orchestrator(*frames: tuple[dict, ...], steps: tuple[AgentPlanStep, ...] | None = None):
    probe = _Frames(*frames)
    perception = UiPerceptionEngine(window_probe=None, window_details_probe=probe)
    actions = _RecordingActions()
    orchestrator = AgenticOrchestrator(
        perception=perception,
        actions=actions,
        planner=_FixedPlanner(steps or (_type_step(),)),
        max_plan_iterations=4,
    )
    return orchestrator, actions


class OperatorLoopTargetTests(unittest.IsolatedAsyncioTestCase):
    async def test_realtek_scenario_types_only_into_the_named_window(self) -> None:
        """The D12 regression: the unrelated foreground app is never the target."""
        orchestrator, actions = _orchestrator((REALTEK, PROBE))
        state = await orchestrator.run_task(GOAL)

        self.assertEqual(len(actions.typed), 1)
        call = actions.typed[0]
        self.assertEqual(call["window"], PROBE["title"])
        self.assertEqual(call["handle"], PROBE["handle"])
        self.assertEqual(call["pid"], PROBE["pid"])
        self.assertNotIn(REALTEK["title"], [entry["window"] for entry in actions.typed])
        self.assertNotIn(REALTEK["handle"], [entry["handle"] for entry in actions.typed])
        self.assertEqual(state.interpretation["target_window"], "NovaGroup1MidAction")

    async def test_absent_target_sends_no_input(self) -> None:
        orchestrator, actions = _orchestrator((REALTEK,))
        state = await orchestrator.run_task(GOAL)

        self.assertEqual(actions.typed, [])
        self.assertEqual(state.status.value, "failed")
        self.assertEqual(state.actions[-1].action, "refused")
        self.assertEqual(state.actions[-1].success, False)
        self.assertIn("no observed window matches", state.actions[-1].detail)
        self.assertTrue(any("Refusing to send input" in error for error in state.errors))

    async def test_only_unrelated_windows_sends_no_input(self) -> None:
        chrome = dict(REALTEK, title="New Tab - Google Chrome", handle=5001, pid=222)
        orchestrator, actions = _orchestrator((chrome, REALTEK))
        state = await orchestrator.run_task(GOAL)

        self.assertEqual(actions.typed, [])
        self.assertEqual(state.actions[-1].action, "refused")

    async def test_ambiguous_target_sends_no_input(self) -> None:
        sibling = dict(PROBE, title="NovaGroup1MidAction Helper - Tk", handle=8001, pid=4343)
        orchestrator, actions = _orchestrator((PROBE, sibling))
        state = await orchestrator.run_task(GOAL)

        self.assertEqual(actions.typed, [])
        self.assertEqual(state.status.value, "failed")
        self.assertIn("ambiguous", state.actions[-1].output["target"]["status"])

    async def test_no_named_target_refuses_input(self) -> None:
        orchestrator, actions = _orchestrator((PROBE,))
        state = await orchestrator.run_task("type the phrase hello world")

        self.assertEqual(actions.typed, [])
        self.assertEqual(state.actions[-1].action, "refused")
        self.assertIn("names no window", state.actions[-1].detail)

    async def test_identity_change_between_steps_sends_no_second_input(self) -> None:
        impostor = dict(PROBE, handle=9999, pid=4949)
        steps = (_type_step("hello"), _type_step("world"))
        orchestrator, actions = _orchestrator(
            (REALTEK, PROBE),  # before step 1
            (REALTEK, PROBE),  # after step 1: verification sees the same target
            (REALTEK, impostor),  # before step 2: same title, different process
            steps=steps,
        )
        state = await orchestrator.run_task(GOAL)

        self.assertEqual(len(actions.typed), 1)
        self.assertEqual(actions.typed[0]["handle"], PROBE["handle"])
        self.assertEqual(state.status.value, "failed")
        self.assertIn("changed identity", state.actions[-1].detail)
        self.assertEqual(state.interpretation["target_identity"], [PROBE["handle"], PROBE["pid"]])

    async def test_target_closing_before_input_sends_no_input(self) -> None:
        orchestrator, actions = _orchestrator((PROBE,), (REALTEK,))
        # First observation binds; the plan's single step observes again only
        # once, so use two steps to force a fresh before-observation.
        orchestrator.planner = _FixedPlanner((_type_step("hello"), _type_step("world")))
        state = await orchestrator.run_task(GOAL)

        self.assertEqual(len(actions.typed), 1)
        self.assertIn("no observed window matches", state.actions[-1].detail)


class ControllerIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def _controller(self, observed: dict):
        from novacontrol.desktop.controller import LocalDesktopRunner

        class _Stub(LocalDesktopRunner):
            def __init__(self) -> None:
                self.staged = ""
                self.observed = observed

            def _uses_windows_type_paste(self) -> bool:
                return True

            async def _stage_and_paste_windows(self, text: str, *, bound=None) -> None:
                self.staged = text

            async def _verify_type_windows(self, text: str):
                return {
                    "clipboard_match": True,
                    "focused_window": True,
                    "window_title": self.observed["title"],
                    "foreground_handle": self.observed["handle"],
                    "foreground_pid": self.observed["pid"],
                }

        return _Stub()

    async def test_paste_into_a_different_foreground_window_is_not_success(self) -> None:
        from novacontrol.desktop.controller import VerificationError

        controller = await self._controller(
            {"title": REALTEK["title"], "handle": REALTEK["handle"], "pid": REALTEK["pid"]}
        )
        with self.assertRaises(VerificationError) as raised:
            await controller._type_text(
                "hello", expect="NovaGroup1MidAction", expect_handle=7123, expect_pid=4242
            )
        self.assertIn("not the intended target", str(raised.exception))

    async def test_paste_into_the_bound_identity_is_verified(self) -> None:
        controller = await self._controller(
            {"title": PROBE["title"], "handle": PROBE["handle"], "pid": PROBE["pid"]}
        )
        output = await controller._type_text(
            "hello", expect="NovaGroup1MidAction", expect_handle=7123, expect_pid=4242
        )
        self.assertTrue(output["verified"])


class VerifierDestinationTests(unittest.TestCase):
    def test_unexpected_destination_fails_even_when_the_action_succeeded(self) -> None:
        from novacontrol.agentcore.ui_state import UiState

        expectation = Expectation(
            description="type hello",
            target_identity=(7123, 4242),
            target_title=PROBE["title"],
        )
        elsewhere = UiState(
            application="Realtek",
            window="Realtek Audio Console",
            raw_text="Realtek Audio Console hello",
            metadata={
                "target": bind_window(
                    "NovaGroup1MidAction", _refs(dict(PROBE, handle=9999, pid=4949))
                ).to_dict()
            },
        )
        result = Verifier().verify(expectation, elsewhere)
        self.assertEqual(result.status, VerificationStatus.FAIL)
        self.assertIn("cannot be attributed", result.reason)

    def test_matching_destination_passes(self) -> None:
        from novacontrol.agentcore.ui_state import UiState

        expectation = Expectation(
            description="type hello",
            target_identity=(7123, 4242),
            target_title=PROBE["title"],
        )
        same = UiState(
            application="python",
            window=PROBE["title"],
            raw_text=f"{PROBE['title']} hello",
            metadata={"target": bind_window("NovaGroup1MidAction", _refs(PROBE)).to_dict()},
        )
        self.assertEqual(Verifier().verify(expectation, same).status, VerificationStatus.PASS)


class GuardrailTests(unittest.IsolatedAsyncioTestCase):
    async def test_typing_without_a_bound_identity_is_refused(self) -> None:
        outcome = await ActionEngine(desktop_runner=object()).type_into_active("hello")
        self.assertFalse(outcome.success)
        self.assertIn("Refusing to type", outcome.detail)

    async def test_typing_without_handle_identity_is_refused(self) -> None:
        outcome = await ActionEngine(desktop_runner=object()).type_into_active(
            "hello", window_title="Notepad"
        )
        self.assertFalse(outcome.success)
        self.assertIn("no stable window handle/pid", outcome.detail)

    async def test_desktop_click_requires_a_bound_target(self) -> None:
        from novacontrol.agentcore.ui_state import UiElement, UiState

        state = UiState(
            application="desktop",
            window="Realtek Audio Console",
            elements=(UiElement(id="1", type="button", text="OK", location=(10, 10)),),
            metadata={"target": {"status": "absent"}},
        )
        outcome = await ActionEngine(desktop_runner=object()).click_element(
            state, "OK", require_bound_target=True
        )
        self.assertFalse(outcome.success)
        self.assertIn("Refusing to click", outcome.detail)

    async def test_destructive_actions_still_require_approval(self) -> None:
        outcome = await ActionEngine(desktop_runner=object()).execute_terminal_command("echo hi")
        self.assertFalse(outcome.success)
        self.assertIn("approval", outcome.detail.lower())

    def test_window_binding_never_returns_a_candidate(self) -> None:
        binding = bind_window("NovaGroup1MidAction", _refs(REALTEK))
        self.assertIsInstance(binding, WindowBinding)
        self.assertIsNone(binding.ref)


if __name__ == "__main__":
    unittest.main()
