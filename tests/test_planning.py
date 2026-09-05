from __future__ import annotations

import unittest

from novacontrol.core.events import Event, EventBus
from novacontrol.planning import (
    Plan,
    PlanStatus,
    PlanStep,
    PlanningEngine,
    PlanningModule,
    RecoveryPolicy,
    WorkflowExecutor,
)


class PlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_engine_decomposes_goal(self) -> None:
        engine = PlanningEngine()

        plan = engine.create_plan("Research options then implement the best one")

        self.assertFalse(plan.needs_clarification)
        self.assertEqual(len(plan.steps), 2)
        self.assertEqual(plan.steps[1].depends_on, ("step-1",))

    async def test_engine_flags_ambiguous_goal(self) -> None:
        plan = PlanningEngine().create_plan("do stuff")

        self.assertTrue(plan.needs_clarification)

    async def test_executor_runs_dependency_order(self) -> None:
        seen: list[str] = []

        async def handler(step: PlanStep) -> dict[str, object]:
            seen.append(step.id or "")
            return {"ok": True}

        plan = Plan(
            goal="demo",
            steps=(
                PlanStep("First", "Do first", id="first"),
                PlanStep("Second", "Do second", depends_on=("first",), id="second"),
            ),
        )
        result = await WorkflowExecutor(step_handler=handler).execute(plan)

        self.assertEqual(result.status, PlanStatus.COMPLETED)
        self.assertEqual(seen, ["first", "second"])

    async def test_executor_retries_failed_step(self) -> None:
        attempts = 0

        async def handler(step: PlanStep) -> dict[str, object]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary")
            return {"ok": True}

        plan = Plan(goal="demo", steps=(PlanStep("Only", "Do it", id="only"),))
        result = await WorkflowExecutor(
            step_handler=handler,
            recovery_policy=RecoveryPolicy(max_step_attempts=2),
        ).execute(plan)

        self.assertEqual(result.status, PlanStatus.COMPLETED)
        self.assertEqual(attempts, 2)

    async def test_planning_module_emits_plan_and_workflow_events(self) -> None:
        bus = EventBus()
        module = PlanningModule()
        plans: list[Event] = []
        workflows: list[Event] = []

        async def capture_plan(event: Event) -> None:
            plans.append(event)

        async def capture_workflow(event: Event) -> None:
            workflows.append(event)

        await bus.subscribe("planning.plan_created", capture_plan)
        await bus.subscribe("planning.workflow_completed", capture_workflow)
        await module.start(bus)
        await bus.publish(
            Event(
                type="planning.plan_requested",
                payload={"goal": "Build the API", "execute": True},
            )
        )

        self.assertEqual(plans[0].type, "planning.plan_created")
        self.assertEqual(workflows[0].payload["status"], PlanStatus.COMPLETED.value)


if __name__ == "__main__":
    unittest.main()
