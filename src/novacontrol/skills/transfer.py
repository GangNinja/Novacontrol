"""Transfer a validated skill to a related but different task.

Adaptation is deliberately explicit: every change is reported, the original
version is never mutated, and the adapted version starts ``ADAPTED`` with an
empty reliability record. It cannot be promoted until its OWN executions pass
validation. An unsupported environment produces a refusal with the missing
capability named, never a best-effort plan that pretends portability.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from novacontrol.skills.models import (
    AdaptationResult,
    Compatibility,
    Skill,
    SkillInput,
    SkillLifecycle,
    SkillStep,
    unique_sorted,
)

#: Application/interface substitutions this build can perform structurally.
#: They are target words, not permissions: the executor still has to succeed.
DEFAULT_TARGET_MAP: dict[str, str] = {
    "calculator": "notepad",
    "notepad": "calculator",
    "chrome": "msedge",
    "google chrome": "microsoft edge",
    "msedge": "chrome",
    "microsoft edge": "google chrome",
}


class SkillTransfer:
    def __init__(self, target_map: Mapping[str, str] | None = None) -> None:
        self.target_map = {
            str(key).strip().lower(): str(value)
            for key, value in (target_map or DEFAULT_TARGET_MAP).items()
        }

    def compatibility(
        self,
        skill: Skill,
        *,
        environment: str = "",
        inputs: Mapping[str, Any] | None = None,
        available_tools: Sequence[str] | None = None,
        require_inputs: bool = True,
    ) -> Compatibility:
        # Adaptation is a PLAN: the new task's values are not necessarily a
        # complete binding of the OLD contract, and enforcing the old inputs
        # here made every genuinely different task look incompatible. Execution
        # still checks the adapted contract in full.
        missing_inputs = (
            [
                name
                for name in skill.required_inputs()
                if name not in (inputs or {})
            ]
            if (inputs is not None and require_inputs)
            else []
        )
        missing_tools: list[str] = []
        if available_tools is not None:
            available = {str(tool) for tool in available_tools}
            missing_tools = [
                tool for tool in skill.tools if tool and tool not in available
            ]
        reasons: list[str] = []
        environment_changed = False
        env = str(environment or "").strip()
        declared = {str(item).strip().lower() for item in skill.environments}
        if env and declared and "any" not in declared:
            if env.lower() not in declared:
                environment_changed = True
                # A target-word table is not an environment adapter. Letting an
                # arbitrary target_map entry ("windows" -> "linux") authorize a
                # new OS relabeled the steps while the old execution code was
                # still underneath. Cross-OS transfer therefore stays refused
                # until a real adapter declares support.
                reasons.append(
                    f"environment {env!r} is unsupported by this skill "
                    f"(declared: {', '.join(skill.environments)})"
                )
        if missing_inputs:
            reasons.append("missing required inputs: " + ", ".join(missing_inputs))
        if missing_tools:
            reasons.append("missing tools: " + ", ".join(missing_tools))
        return Compatibility(
            compatible=not reasons,
            reasons=tuple(reasons),
            missing_inputs=tuple(missing_inputs),
            missing_capabilities=tuple(unique_sorted(missing_tools)),
            environment_changed=environment_changed,
        )

    def adapt(
        self,
        skill: Skill,
        *,
        version: int,
        goal: str,
        inputs: Mapping[str, Any] | None = None,
        environment: str = "",
        available_tools: Sequence[str] | None = None,
        target_map: Mapping[str, str] | None = None,
        requested_by: str = "operator",
    ) -> AdaptationResult:
        compatibility = self.compatibility(
            skill,
            environment=environment,
            inputs=inputs,
            available_tools=available_tools,
            require_inputs=False,
        )
        if not compatibility.compatible:
            return AdaptationResult(
                source_skill_id=skill.id,
                source_version=skill.version,
                compatible=False,
                reasons=compatibility.reasons,
                missing_inputs=compatibility.missing_inputs,
                missing_capabilities=compatibility.missing_capabilities,
            )
        mapping = {
            **self.target_map,
            **{
                str(key).strip().lower(): str(value)
                for key, value in (target_map or {}).items()
            },
        }
        changes: list[dict[str, object]] = []
        adapted_steps: list[SkillStep] = []
        pattern = (
            re.compile(
                "|".join(
                    rf"\b{re.escape(source)}\b" for source in mapping
                ),
                re.IGNORECASE,
            )
            if mapping
            else None
        )
        for index, step in enumerate(skill.steps):
            instruction = step.instruction
            if pattern is not None:
                replaced = pattern.findall(instruction)
                if replaced:
                    # ONE pass: replacing sequentially let a target that is also
                    # a source (calculator→notepad, notepad→calculator) undo the
                    # first replacement, leaving the original instruction.
                    instruction = pattern.sub(
                        lambda match: mapping[match.group(0).lower()],
                        instruction,
                    )
                    for found in replaced:
                        changes.append(
                            {
                                "step": index,
                                "kind": "target_replacement",
                                "from": found.lower(),
                                "to": mapping[found.lower()],
                            }
                        )
            tool = step.tool
            if tool and tool.lower() in mapping:
                tool = mapping[tool.lower()]
                changes.append(
                    {
                        "step": index,
                        "kind": "tool_replacement",
                        "from": step.tool,
                        "to": tool,
                    }
                )
            step_environment = step.environment
            if (
                step_environment
                and environment
                and step_environment.lower() != environment.lower()
            ):
                changes.append(
                    {
                        "step": index,
                        "kind": "environment",
                        "from": step_environment,
                        "to": environment,
                    }
                )
                step_environment = environment
            adapted_steps.append(
                SkillStep(
                    instruction=instruction,
                    kind=step.kind,
                    tool=tool,
                    permission=step.permission,
                    environment=step_environment,
                    verify=step.verify,
                    optional=step.optional,
                )
            )
        for name, value in (inputs or {}).items():
            original = next(
                (item.example for item in skill.inputs if item.name == name), ""
            )
            if str(original) != str(value):
                changes.append(
                    {
                        "kind": "input",
                        "name": name,
                        "from": original,
                        "to": value,
                    }
                )
        if environment and environment.lower() not in {
            item.lower() for item in skill.environments
        }:
            changes.append(
                {
                    "kind": "environment",
                    "from": ", ".join(skill.environments),
                    "to": environment,
                }
            )
        placeholders: set[str] = set()
        for step in adapted_steps:
            placeholders.update(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", step.instruction))
        input_values = dict(inputs or {})
        provided = set(input_values)
        kept_inputs = tuple(
            item
            for item in skill.inputs
            if item.name in placeholders or item.name in provided
        )
        known = {item.name for item in kept_inputs}
        added_inputs: list[SkillInput] = []
        for name in unique_sorted(sorted(provided - known)):
            added_inputs.append(
                SkillInput(
                    name,
                    description="introduced by the adapted task",
                    required=True,
                    example=str(input_values[name]),
                )
            )
            changes.append(
                {"kind": "input_added", "name": name, "value": input_values[name]}
            )
        for item in skill.inputs:
            if item.name not in {row.name for row in kept_inputs}:
                changes.append(
                    {
                        "kind": "input_removed",
                        "name": item.name,
                        "reason": "the adapted procedure no longer binds it",
                    }
                )
        adapted_inputs = kept_inputs + tuple(added_inputs)
        adapted_tools = unique_sorted(
            [step.tool for step in adapted_steps if step.tool]
        )
        if not changes:
            changes.append(
                {
                    "kind": "context",
                    "detail": "same procedure, bound to the new task's inputs",
                }
            )
        adapted = Skill(
            id=skill.id,
            name=skill.name,
            description=skill.description,
            objective=goal.strip() or skill.objective,
            category=skill.category,
            lifecycle=SkillLifecycle.ADAPTED,
            version=version,
            steps=tuple(adapted_steps),
            inputs=adapted_inputs,
            outputs=skill.outputs,
            tools=adapted_tools,
            permissions=skill.permissions,
            preconditions=skill.preconditions
            + ("the adapted environment supplies the declared tools",),
            postconditions=skill.postconditions,
            dependencies=skill.dependencies,
            environments=(
                (environment,)
                if environment
                else skill.environments
            ),
            limitations=skill.limitations
            + ("adapted from another context; independently evaluated",),
            validation=(
                "adaptation changes are recorded; this version needs its own "
                "verified executions before promotion",
            ),
            provenance=unique_sorted(
                list(skill.provenance)
                + [
                    f"adaptation_of:{skill.id}:{skill.version}",
                    f"requested_by:{requested_by}",
                ]
            ),
            reliability=skill.reliability.__class__(),
            parent_version=skill.version,
            metadata={
                **dict(skill.metadata),
                "adapted_from": skill.version,
                "adapted_environment": environment,
                "adaptation_changes": [dict(change) for change in changes],
                # A fresh id here would create a second skill; the catalog stores
                # this as a new VERSION of the same identity.
                "adaptation_id": uuid4().hex,
            },
        )
        return AdaptationResult(
            source_skill_id=skill.id,
            source_version=skill.version,
            compatible=True,
            skill=adapted,
            reasons=("compatible context; changes are explicit",),
            changes=tuple(changes),
            missing_inputs=compatibility.missing_inputs,
            missing_capabilities=compatibility.missing_capabilities,
            validation_required=True,
        )
