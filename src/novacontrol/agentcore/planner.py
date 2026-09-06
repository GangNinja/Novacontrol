"""Adaptive planner: dynamic ordered plans over the tool registry.

Plans are step objects, not macros — each step names an action family, a
semantic target, and how to VERIFY it. `replan` rebuilds the remaining plan
from the current observation when reality diverges from the plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any
from uuid import uuid4


class StepKind(StrEnum):
    OBSERVE = "observe"
    RESEARCH = "research"
    BROWSER = "browser"
    DESKTOP = "desktop"
    PHONE = "phone"
    REASON = "reason"
    VERIFY = "verify"


@dataclass(frozen=True, slots=True)
class AgentPlanStep:
    """One planned step with its own verification expectation."""

    kind: StepKind
    description: str
    target: str = ""
    expected: str = ""  # what success looks like; feeds the Verifier
    id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "description": self.description,
            "target": self.target,
            "expected": self.expected,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AgentPlanStep":
        return cls(
            kind=StepKind(str(payload.get("kind", StepKind.OBSERVE.value))),
            description=str(payload.get("description", "")),
            target=str(payload.get("target", "")),
            expected=str(payload.get("expected", "")),
            id=str(payload.get("id") or uuid4().hex),
        )


@dataclass(frozen=True, slots=True)
class AgentPlan:
    goal: str
    steps: tuple[AgentPlanStep, ...] = ()
    id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "goal": self.goal, "steps": [step.to_dict() for step in self.steps]}

    def remaining_from(self, step_id: str) -> tuple[AgentPlanStep, ...]:
        """Steps after (and excluding) the given step id."""
        found = False
        remaining: list[AgentPlanStep] = []
        for step in self.steps:
            if found:
                remaining.append(step)
            elif step.id == step_id:
                found = True
        return tuple(remaining) if found else self.steps


_BROWSER_WORDS = r"(chrome|edge|firefox|brave|browser|website|url|github|youtube|gmail|http)"
_RESEARCH_WORDS = r"(research|find (?:out|info|information|documentation|docs)|look up|compare)"
_PHONE_WORDS = r"(whatsapp|sms|text \w+ on my phone|call |dial |screenshot on my phone|on my phone)"


class AdaptivePlanner:
    """Builds ordered observe->act->verify plans and replans on divergence."""

    def __init__(self, known_workflows: dict[str, tuple[str, ...]] | None = None) -> None:
        # application/instance -> previously verified step sequence
        self._known_workflows: dict[str, tuple[str, ...]] = dict(known_workflows or {})

    def remember_workflow(self, key: str, steps: tuple[str, ...]) -> None:
        self._known_workflows[key.lower()] = tuple(steps)

    def workflow_for(self, key: str) -> tuple[str, ...] | None:
        return self._known_workflows.get(key.lower())

    def create_plan(self, goal: str, *, interpretation: dict[str, Any] | None = None) -> AgentPlan:
        text = goal.strip()
        steps: list[AgentPlanStep] = [AgentPlanStep(
            kind=StepKind.OBSERVE,
            description="Observe the current environment before acting",
            expected="A UI state describing what is currently on screen",
        )]

        for hint in (interpretation or {}).get("subtasks", ()):
            text_step = str(hint)
            steps.extend(self._steps_for_fragment(text_step))

        if not interpretation:  # heuristic mode: derive from the goal text itself
            for fragment in re.split(r"\s+and\s+", text, flags=re.IGNORECASE):
                steps.extend(self._steps_for_fragment(fragment.strip()))

        # Guarantee the plan ends with verification of the overall goal.
        if not any(step.kind is StepKind.VERIFY for step in steps):
            steps.append(AgentPlanStep(
                kind=StepKind.VERIFY,
                description=f"Verify the overall goal was achieved: {text[:80]}",
                expected="Observable evidence that the goal state was reached",
            ))
        return AgentPlan(goal=text, steps=tuple(steps))

    def replan(
        self,
        plan: AgentPlan,
        *,
        failed_step: AgentPlanStep,
        observation_summary: str,
        recovery_strategy: str | None = None,
    ) -> AgentPlan:
        """Rebuild the remaining plan after a failure.

        The new plan re-observes, then retries the failed step with the
        recovery strategy applied (or replaces it with a research step when
        the environment no longer matches anything we know).
        """
        remaining = plan.remaining_from(failed_step.id)
        new_steps: list[AgentPlanStep] = [
            AgentPlanStep(
                kind=StepKind.OBSERVE,
                description=f"Re-observe after failure of: {failed_step.description[:60]}",
                expected="A fresh UI state reflecting the current environment",
            ),
        ]
        if recovery_strategy:
            new_steps.append(replace(failed_step, description=recovery_strategy))
        elif failed_step.kind in (StepKind.BROWSER, StepKind.DESKTOP, StepKind.PHONE):
            new_steps.append(AgentPlanStep(
                kind=StepKind.RESEARCH,
                description=f"Research how to accomplish: {failed_step.description[:70]}",
                expected="A documented procedure or alternative path",
            ))
            new_steps.append(replace(failed_step, description=f"Retry with researched procedure: {failed_step.description[:60]}"))
        else:
            new_steps.append(failed_step)
        new_steps.extend(remaining)
        return AgentPlan(goal=plan.goal, steps=tuple(new_steps))

    # -- fragment -> steps ---------------------------------------------------

    def _steps_for_fragment(self, fragment: str) -> list[AgentPlanStep]:
        lowered = fragment.lower().strip()
        if not lowered:
            return []
        if re.search(_PHONE_WORDS, lowered):
            return [AgentPlanStep(
                kind=StepKind.PHONE,
                description=fragment,
                target=self._target_of(fragment),
                expected=f"The phone carried out: {fragment[:60]}",
            )]
        if re.search(_BROWSER_WORDS, lowered) or re.search(r"\bnavigate\b", lowered):
            return [
                AgentPlanStep(
                    kind=StepKind.BROWSER,
                    description=fragment,
                    target=self._target_of(fragment),
                    expected=f"The browser shows: {fragment[:60]}",
                ),
                AgentPlanStep(
                    kind=StepKind.OBSERVE,
                    description=f"Observe the page after: {fragment[:60]}",
                    expected="A UI state reflecting the new page content",
                ),
            ]
        if re.search(r"\bsearch\b", lowered):
            # "search for cats" means a WEB search: navigate the browser to a
            # results URL for the query, then observe the results page.
            query = re.sub(r"^.*?\bsearch\b(?:\s+for\s+)?", "", lowered).strip() or lowered
            return [
                AgentPlanStep(
                    kind=StepKind.BROWSER,
                    description=fragment,
                    target=f"https://www.google.com/search?q={query.replace(' ', '+')}",
                    expected=f"Search results for {query[:40]}",
                ),
                AgentPlanStep(
                    kind=StepKind.OBSERVE,
                    description=f"Observe the search results for: {query[:60]}",
                    expected="A results page listing items for the query",
                ),
            ]
        if re.search(_RESEARCH_WORDS, lowered):
            return [AgentPlanStep(
                kind=StepKind.RESEARCH,
                description=fragment,
                target=self._target_of(fragment),
                expected="Cited findings with confidence per claim",
            )]
        if re.search(r"\b(click|type|press|scroll|screenshot|open |close |window|folder|terminal|run )\b", lowered):
            return [
                AgentPlanStep(
                    kind=StepKind.DESKTOP,
                    description=fragment,
                    target=self._target_of(fragment),
                    expected=f"The desktop state reflects: {fragment[:60]}",
                ),
                AgentPlanStep(
                    kind=StepKind.OBSERVE,
                    description=f"Observe the screen after: {fragment[:60]}",
                    expected="A UI state reflecting the change",
                ),
            ]
        return [AgentPlanStep(
            kind=StepKind.REASON,
            description=fragment,
            expected="A reasoned answer grounded in observations",
        )]

    @staticmethod
    def _target_of(fragment: str) -> str:
        match = re.search(r"(?:open|navigate to|go to|visit|search(?: for)?|click|type|press|launch|start)\s+(.+)", fragment, re.IGNORECASE)
        return match.group(1).strip().strip("\"'") if match else fragment.strip()
