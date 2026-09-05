"""Workflow graph execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from novacontrol.planning.models import Plan, PlanStatus, PlanStep, PlanStepStatus, WorkflowResult

StepHandler = Callable[[PlanStep], Awaitable[dict[str, object]]]


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    max_step_attempts: int = 1

    def __post_init__(self) -> None:
        if self.max_step_attempts < 1:
            raise ValueError("max_step_attempts must be at least 1.")


class WorkflowExecutor:
    """Executes a plan while respecting step dependencies."""

    def __init__(
        self,
        *,
        step_handler: StepHandler | None = None,
        recovery_policy: RecoveryPolicy | None = None,
    ) -> None:
        self.step_handler = step_handler or _default_step_handler
        self.recovery_policy = recovery_policy or RecoveryPolicy()

    async def execute(self, plan: Plan) -> WorkflowResult:
        if plan.needs_clarification:
            return WorkflowResult(
                plan_id=plan.id,
                status=PlanStatus.NEEDS_CLARIFICATION,
                step_outputs={},
                errors={"plan": "Clarification required before execution."},
            )

        completed: set[str] = set()
        outputs: dict[str, dict[str, object]] = {}
        errors: dict[str, str] = {}

        while len(completed) < len(plan.steps):
            ready = [
                step
                for step in plan.steps
                if step.id not in completed and all(dependency in completed for dependency in step.depends_on)
            ]
            if not ready:
                return WorkflowResult(
                    plan_id=plan.id,
                    status=PlanStatus.FAILED,
                    step_outputs=outputs,
                    errors={"dependencies": "No executable steps remain."},
                )
            for step in ready:
                try:
                    outputs[step.id] = await self._execute_step(step)
                    completed.add(step.id)
                except Exception as exc:
                    errors[step.id] = f"{type(exc).__name__}: {exc}"
                    return WorkflowResult(
                        plan_id=plan.id,
                        status=PlanStatus.FAILED,
                        step_outputs=outputs,
                        errors=errors,
                    )

        return WorkflowResult(
            plan_id=plan.id,
            status=PlanStatus.COMPLETED,
            step_outputs=outputs,
            errors=errors,
        )

    async def _execute_step(self, step: PlanStep) -> dict[str, object]:
        last_error: Exception | None = None
        for _ in range(self.recovery_policy.max_step_attempts):
            try:
                return await self.step_handler(step)
            except Exception as exc:
                last_error = exc
        if last_error is None:
            raise RuntimeError("Step execution failed without an error.")
        raise last_error


async def _default_step_handler(step: PlanStep) -> dict[str, object]:
    return {
        "step_id": step.id,
        "status": PlanStepStatus.COMPLETED.value,
        "summary": step.description,
    }
