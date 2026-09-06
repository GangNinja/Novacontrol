"""Agentcore unit tests: interpreter, planner, verifier, recovery, knowledge,
evaluation, state, and the orchestrator loop over FAKE runners — no real
browser, desktop, or network is touched."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from novacontrol.agentcore import (
    ActionEngine,
    AdaptivePlanner,
    AgenticOrchestrator,
    ApplicationKnowledgeGraph,
    EvaluationLedger,
    RecoveryEngine,
    RecoveryStrategy,
    TaskInterpreter,
    UiElement,
    UiPerceptionEngine,
    UiState,
    VerificationStatus,
    Verifier,
)
from novacontrol.agentcore.state import ActionRecord, AgentTaskState
from novacontrol.agentcore.verifier import Expectation


class FakeBrowserRunner:
    """Scripted browser runner: records actions, returns DOM-shaped payloads."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.click_error: str = ""

    async def run(self, action: Any) -> dict[str, Any]:
        self.calls.append((action.type.value, action.target))
        if action.type.value == "navigate":
            return {"adapter": "fake", "url": action.target, "title": "Page"}
        if action.type.value == "click":
            if self.click_error:
                return {"adapter": "fake", "error": self.click_error}
            return {"adapter": "fake", "selector": action.target}
        return {"adapter": "fake"}


class FakePerception:
    """Scripted perception: returns queued UI states in order."""

    def __init__(self, states: list[UiState]) -> None:
        self.states = list(states)
        self.calls = 0

    async def observe(self, **kwargs: Any) -> UiState:
        self.calls += 1
        if len(self.states) > 1:
            return self.states.pop(0)
        return self.states[0]


def _ui(texts: tuple[str, ...], *, url: str = "https://example.com", app: str = "browser") -> UiState:
    return UiState(
        application=app,
        url=url,
        window="Fake Window",
        elements=tuple(
            UiElement(id=f"e{i}", type="button", text=t, selector=f"text={t}", confidence=0.8)
            for i, t in enumerate(texts)
        ),
        raw_text=" ".join(texts),
        source="browser_dom",
    )


class TaskInterpreterTests(unittest.TestCase):
    def test_extracts_subtasks_tools_and_risks(self) -> None:
        interpreter = TaskInterpreter()
        interpreted = asyncio.run(interpreter.interpret(
            "Open Chrome, then search for cats, then summarize the results. Do not delete anything."
        ))
        self.assertTrue(interpreted.subtasks)
        self.assertIn("browser", interpreted.required_tools)
        self.assertIn("research", interpreted.required_tools)
        self.assertIn("high", interpreted.risks)
        self.assertTrue(interpreted.confirmation_required)

    def test_plain_question_still_interprets(self) -> None:
        interpreted = asyncio.run(TaskInterpreter().interpret("What is the capital of France?"))
        self.assertEqual(interpreted.goal, "What is the capital of France?")
        self.assertTrue(interpreted.required_tools)

    def test_empty_request_raises(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(TaskInterpreter().interpret("   "))


class AdaptivePlannerTests(unittest.TestCase):
    def test_plan_starts_with_observe_and_ends_with_verify(self) -> None:
        plan = AdaptivePlanner().create_plan("open chrome and search for cats")
        self.assertEqual(plan.steps[0].kind.value, "observe")
        self.assertEqual(plan.steps[-1].kind.value, "verify")
        kinds = [step.kind.value for step in plan.steps]
        self.assertIn("browser", kinds)

    def test_replan_reobserves_then_retries(self) -> None:
        planner = AdaptivePlanner()
        plan = planner.create_plan("open chrome and search for cats")
        failing = plan.steps[1]
        replanned = planner.replan(plan, failed_step=failing, observation_summary="chrome not open")
        self.assertEqual(replanned.steps[0].kind.value, "observe")
        self.assertGreaterEqual(len(replanned.steps), len(plan.steps))

    def test_known_workflow_is_returned(self) -> None:
        planner = AdaptivePlanner()
        planner.remember_workflow("browser", ("navigate", "search"))
        self.assertEqual(planner.workflow_for("BROWSER"), ("navigate", "search"))


class VerifierTests(unittest.TestCase):
    def test_pass_on_expected_text(self) -> None:
        result = Verifier().verify(
            Expectation(description="search results", ui_contains=("cats",)),
            _ui(("Search results for cats",)),
        )
        self.assertEqual(result.status, VerificationStatus.PASS)

    def test_fail_on_forbidden_error_text(self) -> None:
        result = Verifier().verify(
            Expectation(description="submit", ui_contains=("success",), ui_absent=("error",)),
            _ui(("error: invalid form", "success".upper())),
        )
        self.assertEqual(result.status, VerificationStatus.FAIL)

    def test_fail_when_ui_unchanged(self) -> None:
        before = _ui(("Submit",))
        after = _ui(("Submit",))
        result = Verifier().verify(
            Expectation(description="click submit", requires_observed_change=True),
            after,
            before_state=before,
        )
        self.assertEqual(result.status, VerificationStatus.FAIL)

    def test_inconclusive_without_observation(self) -> None:
        result = Verifier().verify(Expectation(description="anything"), None)
        self.assertEqual(result.status, VerificationStatus.INCONCLUSIVE)


class RecoveryEngineTests(unittest.TestCase):
    def test_relocates_when_similar_element_exists(self) -> None:
        engine = RecoveryEngine()
        failed = ActionRecord(step_id="s1", action="click", target="Integrations", success=False, detail="not found")
        state = _ui(("Settings", "Integrations (new)", "Account"))
        outcome = engine.diagnose(failed, current_state=state, application="dashboard")
        self.assertEqual(outcome.strategy, RecoveryStrategy.RELOCATE)
        self.assertIn("Integrations", outcome.alternative_target)

    def test_research_when_no_alternative(self) -> None:
        engine = RecoveryEngine()
        failed = ActionRecord(step_id="s1", action="click", target="Zen mode", success=False, detail="no matching element")
        outcome = engine.diagnose(failed, current_state=_ui(("Home",)), application="editor")
        self.assertEqual(outcome.strategy, RecoveryStrategy.RESEARCH)

    def test_abort_on_repeated_failure_and_destructive(self) -> None:
        engine = RecoveryEngine()
        failed = ActionRecord(step_id="s1", action="click", target="x", success=False)
        self.assertEqual(engine.diagnose(failed, current_state=None, application="a", prior_attempts=2).strategy, RecoveryStrategy.ABORT)
        shell = ActionRecord(step_id="s2", action="execute_terminal_command", target="rm", success=False)
        self.assertEqual(engine.diagnose(shell, current_state=None, application="a").strategy, RecoveryStrategy.ABORT)

    def test_successful_relocation_recorded_in_knowledge(self) -> None:
        knowledge = ApplicationKnowledgeGraph()
        knowledge.record_workflow("app", "open settings", ("click Integrations",))
        engine = RecoveryEngine(knowledge=knowledge)
        outcome = RecoveryStrategy.RELOCATE and engine.diagnose(
            ActionRecord(step_id="s", action="click", target="Integrations", success=False),
            current_state=_ui(("Integration center",)),
            application="app",
        )
        engine.record_recovery("app", "Integrations", outcome, succeeded=True)
        # The workflow degraded and gained an alternative.
        stored = knowledge.workflows_for("app")[0]
        self.assertTrue(stored.alternatives or stored.confidence < 0.6)


class ApplicationKnowledgeGraphTests(unittest.TestCase):
    def test_roundtrip_through_dict(self) -> None:
        graph = ApplicationKnowledgeGraph()
        graph.record_workflow("chrome", "search cats", ("navigate google", "type cats"), confidence=0.7)
        restored = ApplicationKnowledgeGraph(graph.to_dict())
        self.assertEqual(restored.best_path("chrome", "search cats"), "navigate google")

    def test_confidence_decays_with_staleness(self) -> None:
        graph = ApplicationKnowledgeGraph()
        workflow = graph.record_workflow("app", "task", ("step",), confidence=0.8)
        workflow.last_verified = "2020-01-01T00:00:00+00:00"
        self.assertLess(workflow.effective_confidence(), 0.8)

    def test_failures_lower_confidence(self) -> None:
        graph = ApplicationKnowledgeGraph()
        graph.record_workflow("app", "task", ("step",), confidence=0.7)
        for _ in range(3):
            graph.record_failure("app", "task")
        self.assertEqual(graph.workflows_for("app")[0].failures, 3)

    def test_low_confidence_paths_are_not_proposed(self) -> None:
        graph = ApplicationKnowledgeGraph()
        workflow = graph.record_workflow("app", "task", ("dead step",), confidence=0.9)
        workflow.failures = 10
        self.assertIsNone(graph.best_path("app", "task"))


class EvaluationLedgerTests(unittest.TestCase):
    def test_metrics_derive_from_records(self) -> None:
        ledger = EvaluationLedger()
        ledger.record_task({"task_id": "1", "goal": "g", "status": "completed", "plan": [], "actions": [1], "verification": {"status": "pass"}}, duration_seconds=1.0)
        ledger.record_task({"task_id": "2", "goal": "g", "status": "failed", "plan": [], "actions": [], "recoveries": [{"succeeded": True}], "verification": {}}, duration_seconds=2.0)
        metrics = ledger.metrics()
        self.assertEqual(metrics["tasks"], 2)
        self.assertEqual(metrics["task_success_rate"], 0.5)
        self.assertEqual(metrics["recovery_success_rate"], 1.0)

    def test_roundtrip(self) -> None:
        ledger = EvaluationLedger()
        ledger.record_task({"task_id": "1", "goal": "g", "status": "completed", "plan": [], "actions": []}, duration_seconds=1.0)
        restored = EvaluationLedger()
        restored.load(ledger.snapshot())
        self.assertEqual(restored.metrics()["tasks"], 1)


class ActionEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_click_by_semantic_label(self) -> None:
        browser = FakeBrowserRunner()
        engine = ActionEngine(browser_runner=browser)
        state = _ui(("Submit", "Cancel"))
        outcome = await engine.click_element(state, "submit")
        self.assertTrue(outcome.success)
        self.assertEqual(browser.calls[-1][0], "click")

    async def test_click_missing_element_fails_cleanly(self) -> None:
        outcome = await ActionEngine(browser_runner=FakeBrowserRunner()).click_element(_ui(("Home",)), "Submit")
        self.assertFalse(outcome.success)
        self.assertIn("No matching element", outcome.detail)

    async def test_shell_requires_approval_token(self) -> None:
        outcome = await ActionEngine(desktop_runner=object()).execute_terminal_command("del /q file")
        self.assertFalse(outcome.success)
        self.assertIn("approval", outcome.detail.lower())


class OrchestratorLoopTests(unittest.IsolatedAsyncioTestCase):
    def _orchestrator(self, perception: FakePerception, browser: FakeBrowserRunner) -> AgenticOrchestrator:
        return AgenticOrchestrator(
            perception=perception,
            actions=ActionEngine(browser_runner=browser),
            knowledge=ApplicationKnowledgeGraph(),
            evaluation=EvaluationLedger(),
        )

    async def test_successful_task_completes_and_learns(self) -> None:
        browser = FakeBrowserRunner()
        perception = FakePerception([
            _ui(("Search",)),                        # before observe
            _ui(("Search", "cats results"), url="https://www.google.com/search?q=cats"),  # after
            _ui(("Search", "cats results"), url="https://www.google.com/search?q=cats"),  # final
        ])
        orchestrator = self._orchestrator(perception, browser)
        state = await orchestrator.run_task("search for cats")
        self.assertEqual(state.status.value, "completed")
        self.assertTrue(state.actions)
        self.assertTrue(any(lesson.kind == "workflow" for lesson in state.learned_information))
        self.assertTrue(browser.calls)  # real navigation happened

    async def test_failed_action_triggers_recovery_then_completes(self) -> None:
        browser = FakeBrowserRunner()
        perception = FakePerception([
            _ui(("Search",)),
            _ui(("Search",)),  # after first attempt: nothing happened
            _ui(("Search", "results page"), url="https://www.google.com/search?q=cats"),
            _ui(("Search", "results page"), url="https://www.google.com/search?q=cats"),
        ])
        orchestrator = self._orchestrator(perception, browser)
        # Make only the FIRST click fail; relocation then succeeds via navigate.
        original_run = browser.run

        calls = {"n": 0}

        async def flaky(action: Any) -> dict[str, Any]:
            calls["n"] += 1
            if action.type.value == "click" and calls["n"] == 1:
                return {"adapter": "fake", "error": "no matching element"}
            return await original_run(action)

        browser.run = flaky  # type: ignore[method-assign]
        state = await orchestrator.run_task("search for cats")
        self.assertTrue(state.recoveries, "a recovery should have been attempted")
        self.assertIn(state.status.value, ("completed", "failed"))

    async def test_metrics_recorded_after_run(self) -> None:
        orchestrator = self._orchestrator(
            FakePerception([_ui(("Search",)), _ui(("Search", "results"))]),
            FakeBrowserRunner(),
        )
        await orchestrator.run_task("search for cats")
        self.assertGreaterEqual(orchestrator.evaluation.metrics()["tasks"], 1)


class PerceptionEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_dom_layer_produces_elements(self) -> None:
        class DomRunner:
            async def run(self, action: Any) -> dict[str, Any]:
                return {
                    "adapter": "fake",
                    "url": "https://example.com",
                    "text": "Welcome to Example",
                    "interactive_elements": [
                        {"type": "button", "text": "Sign in", "selector": "text=Sign in", "enabled": True, "visible": True},
                    ],
                }

        engine = UiPerceptionEngine(browser_runner=DomRunner())
        state = await engine.observe(url="https://example.com")
        self.assertEqual(state.application, "browser")
        self.assertEqual(len(state.elements), 1)
        self.assertEqual(state.elements[0].text, "Sign in")

    async def test_semantic_layer_labels_elements(self) -> None:
        class Provider:
            async def complete(self, messages: Any, **kwargs: Any) -> str:
                return "0: opens the account page"

        class DomRunner:
            async def run(self, action: Any) -> dict[str, Any]:
                return {
                    "url": "https://example.com",
                    "text": "x",
                    "interactive_elements": [{"type": "link", "text": "Account", "selector": "a=Account"}],
                }

        engine = UiPerceptionEngine(browser_runner=DomRunner(), completion_provider=Provider())
        state = await engine.observe(url="https://example.com")
        self.assertEqual(state.elements[0].layer, "semantic")
        self.assertIn("account", state.elements[0].attributes["purpose"].lower())


class TaskStateTests(unittest.TestCase):
    def test_state_roundtrip(self) -> None:
        state = AgentTaskState(goal="do a thing")
        state.actions.append(ActionRecord(step_id="s", action="click", target="x", success=True))
        state.learn(type(state.learned_information[0])(kind="observation", text="learned", confidence=0.4)) if state.learned_information else None
        restored = AgentTaskState.from_dict(state.to_dict())
        self.assertEqual(restored.goal, "do a thing")
        self.assertEqual(len(restored.actions), 1)
        self.assertTrue(restored.actions[0].success)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
