"""Compose validated skills into one larger workflow.

Composition is a planning decision, not an execution shortcut: it checks that
every skill exists, is executable, is compatible with the target context, that
its required inputs are supplied or produced by an earlier skill, that
dependencies are present and acyclic, and that no two skills claim the same
output. Nothing is executed here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from novacontrol.skills.models import (
    CompositionResult,
    Skill,
    unique_sorted,
)
from novacontrol.skills.transfer import SkillTransfer


class SkillComposer:
    def __init__(self, transfer: SkillTransfer | None = None) -> None:
        self.transfer = transfer or SkillTransfer()

    def compose(
        self,
        objective: str,
        skills: Sequence[Skill],
        *,
        inputs_by_skill: Mapping[str, Mapping[str, Any]] | None = None,
        environment: str = "",
        available_tools: Sequence[str] | None = None,
    ) -> CompositionResult:
        conflicts: list[str] = []
        missing_inputs: list[str] = []
        missing_capabilities: list[str] = []
        permissions: set[str] = set()
        if not objective.strip():
            conflicts.append("a composed workflow needs an objective")
        if not skills:
            conflicts.append("no skills were selected")
        by_id: dict[str, Skill] = {}
        for skill in skills:
            if skill.id in by_id:
                conflicts.append(f"skill {skill.name!r} was selected twice")
                continue
            by_id[skill.id] = skill
            if not skill.executable:
                conflicts.append(
                    f"skill {skill.name!r} is {skill.lifecycle.value}, not executable"
                )
            # Only an EXPLICIT binding is checked here. Passing ``{}`` for a
            # skill whose inputs are produced by an earlier skill made every
            # dependency-backed composition fail before the produced outputs
            # below were considered.
            supplied = (inputs_by_skill or {}).get(skill.id)
            compatibility = self.transfer.compatibility(
                skill,
                environment=environment,
                inputs=supplied,
                available_tools=available_tools,
            )
            if not compatibility.compatible:
                conflicts.extend(
                    f"{skill.name}: {reason}" for reason in compatibility.reasons
                )
                missing_inputs.extend(compatibility.missing_inputs)
                missing_capabilities.extend(compatibility.missing_capabilities)
            permissions.update(skill.permissions)
        order = self._order(by_id, conflicts)
        produced: set[str] = set()
        plan: list[dict[str, Any]] = []
        for skill in order:
            supplied = dict((inputs_by_skill or {}).get(skill.id, {}))
            for name in skill.required_inputs():
                if name not in supplied and name not in produced:
                    missing_inputs.append(f"{skill.name}:{name}")
            for output in skill.outputs:
                if output in produced:
                    owner = next(
                        (
                            item.name
                            for item in order
                            if item is not skill and output in item.outputs
                        ),
                        "another skill",
                    )
                    conflicts.append(
                        f"output {output!r} is produced by both {owner!r} and "
                        f"{skill.name!r}"
                    )
                produced.add(output)
            plan.append(
                {
                    "skill_id": skill.id,
                    "name": skill.name,
                    "version": skill.version,
                    "inputs": supplied,
                    "outputs": list(skill.outputs),
                    "steps": [step.to_dict() for step in skill.steps],
                    "permissions": list(skill.permissions),
                }
            )
        return CompositionResult(
            objective=objective.strip(),
            compatible=not conflicts and not missing_inputs and not missing_capabilities,
            ordered_skill_ids=tuple(skill.id for skill in order),
            plan=tuple(plan),
            conflicts=tuple(unique_sorted(conflicts)),
            missing_inputs=tuple(unique_sorted(missing_inputs)),
            missing_capabilities=tuple(unique_sorted(missing_capabilities)),
            permissions=unique_sorted(list(permissions)),
        )

    def _order(
        self, by_id: Mapping[str, Skill], conflicts: list[str]
    ) -> tuple[Skill, ...]:
        by_name = {skill.name.strip().lower(): skill for skill in by_id.values()}
        resolved: dict[str, set[str]] = {skill.id: set() for skill in by_id.values()}
        for skill in by_id.values():
            for dependency in skill.dependencies:
                target = by_id.get(dependency) or by_name.get(dependency.lower())
                if target is None:
                    conflicts.append(
                        f"{skill.name!r} depends on {dependency!r}, which is not "
                        "part of the composition"
                    )
                    continue
                resolved[skill.id].add(target.id)
        ordered: list[Skill] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visited:
                return
            if node in visiting:
                conflicts.append("the selected skills have a dependency cycle")
                return
            visiting.add(node)
            for neighbour in sorted(resolved.get(node, ())):
                visit(neighbour)
            visiting.discard(node)
            visited.add(node)
            ordered.append(by_id[node])

        for skill in sorted(by_id.values(), key=lambda item: (item.name, item.id)):
            visit(skill.id)
        return tuple(ordered)
