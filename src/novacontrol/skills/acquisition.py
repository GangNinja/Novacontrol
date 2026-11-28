"""Acquire a reusable skill from genuine execution evidence.

A single success is deliberately not enough. Acquisition requires at least two
independent verified executions with the same procedure shape, or one execution
explicitly supplied as a user demonstration; either way the resulting skill
starts ``ACQUIRED`` and cannot execute until it has been validated. That is the
line between "a task that worked once" and a reusable procedure.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from uuid import uuid4

from novacontrol.skills.models import (
    AcquisitionResult,
    Skill,
    SkillCategory,
    SkillLifecycle,
    SkillEvidence,
    SkillInput,
    SkillObservation,
    SkillReliability,
    SkillStep,
    similarity,
    unique_sorted,
)


class SkillAcquisition:
    def __init__(
        self,
        *,
        min_support: int = 2,
        min_goal_similarity: float = 0.2,
    ) -> None:
        self.min_support = max(2, int(min_support))
        self.min_goal_similarity = max(0.0, min(1.0, float(min_goal_similarity)))

    def acquire(
        self,
        goal: str,
        observations: Sequence[SkillObservation],
        *,
        name: str = "",
        description: str = "",
        category: SkillCategory | None = None,
        demonstrated: bool = False,
        requested_by: str = "operator",
    ) -> AcquisitionResult:
        verified = [item for item in observations if item.verified]
        support = len({item.id for item in verified})
        required = 1 if demonstrated else self.min_support
        if support < required:
            reason = (
                "one verified success is not evidence of a reusable skill; "
                f"{self.min_support} independent successes are required"
                if not demonstrated
                else "a demonstration still needs one verified execution"
            )
            return AcquisitionResult(
                created=False,
                support=support,
                rejected=True,
                reasons=(reason,),
            )
        if not goal.strip():
            return AcquisitionResult(
                created=False,
                support=support,
                rejected=True,
                reasons=("the skill needs the objective it serves",),
            )
        inputs = self._extract_inputs(verified)
        shapes = {
            tuple(
                (step.kind.value, step.tool, step.normalized_instruction)
                for step in self._templatize(item.steps, inputs)
            )
            for item in verified
        }
        if len(shapes) != 1:
            return AcquisitionResult(
                created=False,
                support=support,
                rejected=True,
                reasons=(
                    "the observed executions followed different procedures, so "
                    "there is no single reusable procedure to extract",
                ),
            )
        observed_goal = " ".join(item.goal for item in verified)
        goal_similarity = max(
            similarity(goal, item.goal) for item in verified
        )
        if goal_similarity < self.min_goal_similarity and similarity(
            goal, observed_goal
        ) < self.min_goal_similarity:
            return AcquisitionResult(
                created=False,
                support=support,
                rejected=True,
                reasons=(
                    "the recorded executions pursued a different objective "
                    f"(similarity {goal_similarity:.2f})",
                ),
            )

        latest = verified[-1]
        steps = self._templatize(latest.steps, inputs)
        if not steps:
            return AcquisitionResult(
                created=False,
                support=support,
                rejected=True,
                reasons=("the observed execution carried no procedure steps",),
            )
        environments = unique_sorted(
            [item.environment for item in verified if item.environment]
        ) or ("any",)
        tools = unique_sorted(
            [tool for item in verified for tool in item.tools]
            + [step.tool for step in steps if step.tool]
        )
        permissions = unique_sorted(
            [step.permission for step in steps if step.permission]
        )
        provenance = unique_sorted(
            [
                f"observation:{item.id}"
                for item in verified
            ]
            + [f"source:{item.source}" for item in verified]
            + [f"agent:{item.agent}" for item in verified if item.agent]
            + [f"requested_by:{requested_by}"]
            + (["demonstration"] if demonstrated else [])
        )
        resolved_name = name.strip() or _title_from_goal(goal)
        skill = Skill(
            id=uuid4().hex,
            name=resolved_name,
            description=description.strip()
            or f"Extracted from {support} verified execution(s) of: {goal}",
            objective=goal.strip(),
            category=category or infer_category(goal, steps),
            lifecycle=SkillLifecycle.ACQUIRED,
            version=1,
            steps=steps,
            inputs=inputs,
            tools=tools,
            permissions=permissions,
            preconditions=("the declared inputs are supplied",),
            postconditions=tuple(
                f"verified by the executor: {step.instruction}"
                for step in steps
                if step.verify
            ),
            environments=environments,
            limitations=(
                f"acquired from {support} execution(s); not yet independently "
                "validated",
            ),
            validation=(
                f"{support} verified execution(s) shared one procedure shape",
            ),
            provenance=provenance,
            reliability=SkillReliability(
                trials=support,
                successes=support,
                total_duration_ms=sum(
                    max(0.0, item.duration_ms) for item in verified
                ),
            ),
        )
        return AcquisitionResult(
            created=True,
            skill=skill,
            support=support,
            reasons=(
                f"{support} verified execution(s) matched procedure shape",
                f"goal similarity {goal_similarity:.2f}",
            ),
        )

    def evidence_for(
        self, skill: Skill, observations: Sequence[SkillObservation]
    ) -> tuple[SkillEvidence, ...]:
        rows: list[SkillEvidence] = []
        for observation in observations:
            if not observation.verified:
                continue
            rows.append(
                SkillEvidence(
                    skill_id=skill.id,
                    version=skill.version,
                    goal=observation.goal,
                    verified=True,
                    environment=observation.environment,
                    inputs=dict(observation.inputs),
                    steps=tuple(step.to_dict() for step in observation.steps),
                    duration_ms=observation.duration_ms,
                    source=observation.source,
                )
            )
        return tuple(rows)

    # -- internals -------------------------------------------------------------

    def _extract_inputs(
        self, observations: Sequence[SkillObservation]
    ) -> tuple[SkillInput, ...]:
        keys = sorted({key for item in observations for key in item.inputs})
        rows: list[SkillInput] = []
        for key in keys:
            values = [
                item.inputs[key] for item in observations if key in item.inputs
            ]
            required = len(values) == len(observations)
            rows.append(
                SkillInput(
                    name=key,
                    description=f"Observed {key} used by this procedure.",
                    required=required,
                    example=str(values[0]) if values else "",
                )
            )
        return tuple(rows)

    def _templatize(
        self,
        steps: Sequence[SkillStep],
        inputs: Sequence[SkillInput],
    ) -> tuple[SkillStep, ...]:
        values = {
            item.name: item.example
            for item in inputs
            if item.example and len(item.example) >= 2
        }
        rows: list[SkillStep] = []
        for step in steps:
            instruction = step.instruction
            for name, value in values.items():
                if value in instruction and "{" + name + "}" not in instruction:
                    instruction = instruction.replace(value, "{" + name + "}")
            rows.append(replace(step, instruction=instruction))
        return tuple(rows)


def infer_category(goal: str, steps: Sequence[SkillStep]) -> SkillCategory:
    text = goal.lower()
    tools = {step.tool.lower() for step in steps}
    kinds = {step.kind.value for step in steps}
    if kinds & {"desktop"} or any(
        word in text for word in ("open ", "window", "application", "calculator", "notepad")
    ):
        return SkillCategory.DESKTOP
    if kinds & {"browser"} or any(
        word in text for word in ("browser", "navigate", "web page", "website")
    ):
        return SkillCategory.BROWSER
    if any(word in text for word in ("research", "search the web", "sources")):
        return SkillCategory.RESEARCH
    if any(word in text for word in ("csv", "json", "dataset", "analy", "chart")):
        return SkillCategory.DATA
    if any(word in text for word in ("document", "pdf", "docx", "report")):
        return SkillCategory.DOCUMENT
    if any(word in text for word in ("file", "folder", "copy", "move", "directory")) or any(
        "filesystem" in tool for tool in tools
    ):
        return SkillCategory.FILE
    return SkillCategory.GENERAL


def _title_from_goal(goal: str) -> str:
    words = [word for word in goal.strip().split() if word]
    title = " ".join(words[:8]).strip(" .,:;")
    return (title[:1].upper() + title[1:]) if title else "Learned skill"
