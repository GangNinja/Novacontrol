"""Goal decomposition and clarification detection."""

from __future__ import annotations

import re

from novacontrol.planning.models import Plan, PlanStep


class ClarificationPolicy:
    """Detects goals that are too ambiguous to execute safely."""

    vague_terms = ("something", "anything", "whatever", "stuff", "things")

    def requires_clarification(self, goal: str) -> bool:
        text = goal.strip().lower()
        if len(text) < 8:
            return True
        return any(term in text for term in self.vague_terms)


class PlanningEngine:
    """Creates deterministic workflow plans from natural language goals."""

    def __init__(self, clarification_policy: ClarificationPolicy | None = None) -> None:
        self.clarification_policy = clarification_policy or ClarificationPolicy()

    def create_plan(self, goal: str) -> Plan:
        if self.clarification_policy.requires_clarification(goal):
            return Plan(goal=goal, steps=(), needs_clarification=True)

        tasks = _split_goal(goal)
        steps: tuple[PlanStep, ...]
        if len(tasks) == 1:
            steps = (
                PlanStep(title="Understand request", description=f"Clarify constraints for: {goal}"),
                PlanStep(title="Execute task", description=goal, depends_on=("understand-request",)),
                PlanStep(
                    title="Verify result",
                    description="Run checks and summarize the outcome.",
                    depends_on=("execute-task",),
                ),
            )
        else:
            steps = tuple(
                PlanStep(
                    title=_title(task),
                    description=task,
                    depends_on=(steps_id(index - 1),) if index > 0 else (),
                    id=steps_id(index),
                )
                for index, task in enumerate(tasks)
            )
        return Plan(goal=goal, steps=steps)


def _split_goal(goal: str) -> list[str]:
    parts = [
        part.strip(" .")
        for part in re.split(r"\bthen\b|;|\n|\band\b", goal, flags=re.IGNORECASE)
        if part.strip(" .")
    ]
    return parts or [goal.strip()]


def _title(text: str) -> str:
    words = text.split()
    return " ".join(words[:6]).capitalize()


def steps_id(index: int) -> str:
    return f"step-{index + 1}"
