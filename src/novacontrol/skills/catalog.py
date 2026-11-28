"""Durable, versioned skill catalog with a small lexical index.

The catalog owns definitions, versions and the active pointer. It does not
execute, validate or adapt anything: those decisions belong to the service and
its evidence. Retrieval is deterministic and indexed, so a large library is not
re-scored on every request.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from novacontrol.skills.models import (
    Compatibility,
    Skill,
    SkillLifecycle,
    SkillMatch,
    skill_tokens,
    similarity,
    unique_sorted,
)


class DuplicateSkillError(ValueError):
    """A different skill already has this name and procedure shape."""


class SkillCatalog:
    def __init__(self, skills: Iterable[Skill] = ()) -> None:
        self._versions: dict[str, dict[int, Skill]] = {}
        self._active: dict[str, int] = {}
        self._index: dict[str, set[str]] = {}
        for skill in skills:
            try:
                self.add(skill, activate=False)
            except DuplicateSkillError:
                continue
        # A snapshot's explicit active map is applied by from_dict; for direct
        # construction the newest executable (else newest) version wins.
        for skill_id in self._versions:
            if skill_id not in self._active:
                self._active[skill_id] = self._best_version(skill_id)

    # -- reading ---------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._versions)

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._versions))

    def versions(self, skill_id: str) -> tuple[Skill, ...]:
        rows = self._versions.get(str(skill_id))
        if not rows:
            return ()
        return tuple(rows[version] for version in sorted(rows))

    def get(self, skill_id: str, version: int | None = None) -> Skill:
        rows = self._versions.get(str(skill_id))
        if not rows:
            raise KeyError(f"Skill not found: {skill_id}")
        if version is None:
            active = self._active.get(str(skill_id))
            if active in rows:
                return rows[active]
            fallback = self._best_version(
                str(skill_id), skip_deprecated=True
            )
            if fallback in rows:
                return rows[fallback]
            raise KeyError(
                f"Skill {skill_id!r} has no active validated version"
            )
        try:
            return rows[int(version)]
        except KeyError as exc:
            raise KeyError(f"Skill version not found: {skill_id} v{version}") from exc

    def current(self, skill_id: str) -> Skill:
        return self.get(skill_id)

    def all(self) -> tuple[Skill, ...]:
        return tuple(
            skill
            for skill_id in self.ids()
            for skill in self.versions(skill_id)
        )

    def stats(self) -> dict[str, Any]:
        counts = {lifecycle.value: 0 for lifecycle in SkillLifecycle}
        for skill in self.all():
            counts[skill.lifecycle.value] += 1
        return {
            "skills": len(self._versions),
            "versions": sum(len(rows) for rows in self._versions.values()),
            "lifecycle": counts,
            "active": {
                skill_id: self._active.get(skill_id, 0)
                for skill_id in self.ids()
            },
        }

    # -- writing ---------------------------------------------------------------

    def add(
        self,
        skill: Skill,
        *,
        activate: bool = True,
        allow_duplicate: bool = False,
    ) -> Skill:
        rows = self._versions.get(skill.id)
        if rows and skill.version in rows:
            raise ValueError(
                f"Skill {skill.name!r} already has a version {skill.version}."
            )
        if not allow_duplicate:
            duplicate = self._duplicate_of(skill)
            if duplicate is not None:
                raise DuplicateSkillError(
                    f"A different skill already performs this procedure: "
                    f"{duplicate.name!r} ({duplicate.id} v{duplicate.version})."
                )
        # Do not create the version bucket until every refusal has passed: an
        # empty bucket made a rejected duplicate count as a stored skill.
        rows = self._versions.setdefault(skill.id, {})
        rows[skill.version] = skill
        if activate or skill.id not in self._active:
            self._active[skill.id] = skill.version
        self._index_skill(skill)
        return skill

    def update(self, skill: Skill) -> Skill:
        """Replace one version in place, used for reliability/statistics only."""
        rows = self._versions.get(skill.id)
        if not rows or skill.version not in rows:
            raise KeyError(f"Skill version not found: {skill.id} v{skill.version}")
        rows[skill.version] = skill
        self._index_skill(skill)
        return skill

    def promote(self, skill_id: str, version: int | None = None) -> Skill:
        skill = self.get(skill_id, version)
        if skill.lifecycle is not SkillLifecycle.VALIDATED:
            raise ValueError(
                f"Skill {skill.name!r} v{skill.version} is {skill.lifecycle.value}; "
                "only a validated version can become the active version. Adapt, "
                "execute and validate it first."
            )
        self._active[skill.id] = skill.version
        return skill

    def deprecate(
        self, skill_id: str, version: int | None = None, *, reason: str = ""
    ) -> Skill:
        skill = self.get(skill_id, version)
        updated = skill.with_(
            lifecycle=SkillLifecycle.DEPRECATED,
            metadata={**dict(skill.metadata), "deprecated_reason": str(reason or "")},
        )
        self.update(updated)
        if self._active.get(skill.id) == skill.version:
            replacement = self._best_version(skill.id, skip_deprecated=True)
            if replacement:
                self._active[skill.id] = replacement
            else:
                self._active.pop(skill.id, None)
        return updated

    def rollback(self, skill_id: str, version: int) -> Skill:
        target = self.get(skill_id, version)
        if target.lifecycle is not SkillLifecycle.VALIDATED:
            raise ValueError(
                f"Cannot roll back to {target.name!r} v{target.version}: it is "
                f"{target.lifecycle.value}; only a validated version is trusted."
            )
        previous_version = self._active.get(skill_id)
        if previous_version is not None and previous_version != version:
            previous = self._versions[skill_id].get(previous_version)
            if previous is not None:
                self.update(
                    previous.with_(
                        lifecycle=SkillLifecycle.DEPRECATED,
                        metadata={
                            **dict(previous.metadata),
                            "deprecated_reason": f"rolled back to v{version}",
                        },
                    )
                )
        self._active[skill_id] = version
        return target

    # -- retrieval -------------------------------------------------------------

    def retrieve(
        self,
        goal: str,
        *,
        environment: str = "",
        inputs: Mapping[str, Any] | None = None,
        available_tools: Sequence[str] | None = None,
        limit: int = 8,
        include_incompatible: bool = False,
    ) -> tuple[SkillMatch, ...]:
        query = skill_tokens(goal)
        candidate_ids: set[str] = set()
        for token in query:
            candidate_ids.update(self._index.get(token, ()))
        if not candidate_ids:
            return ()
        hits: list[SkillMatch] = []
        for skill_id in candidate_ids:
            try:
                skill = self.get(skill_id)
            except KeyError:
                # Deprecated without a validated replacement: there is no
                # active version to retrieve, and that is not an error.
                continue
            if skill.lifecycle in (
                SkillLifecycle.DEPRECATED,
                SkillLifecycle.FAILED,
            ):
                continue
            compatibility = self._compatibility(
                skill,
                environment=environment,
                inputs=inputs,
                available_tools=available_tools,
            )
            if not compatibility.compatible and not include_incompatible:
                continue
            text = " ".join(
                [
                    skill.name,
                    skill.objective,
                    skill.description,
                    skill.category.value,
                    " ".join(skill.input_names),
                ]
            )
            relevance = similarity(goal, text)
            score = (
                0.65 * relevance
                + 0.2 * skill.reliability.success_rate
                + 0.1 * skill.reliability.confidence
                + 0.05 * (1.0 if skill.lifecycle is SkillLifecycle.VALIDATED else 0.0)
            )
            reasons = [f"lexical relevance {relevance:.2f}"]
            if skill.reliability.success_rate:
                reasons.append(
                    f"success {skill.reliability.success_rate:.0%} over "
                    f"{skill.reliability.trials} evidence row(s)"
                )
            if compatibility.reasons:
                reasons.extend(compatibility.reasons)
            hits.append(
                SkillMatch(
                    skill_id=skill.id,
                    name=skill.name,
                    version=skill.version,
                    score=score,
                    lifecycle=skill.lifecycle,
                    reasons=tuple(reasons),
                    compatible=compatibility.compatible,
                    missing_inputs=compatibility.missing_inputs,
                    missing_capabilities=compatibility.missing_capabilities,
                )
            )
        hits.sort(key=lambda item: (-item.score, item.name, item.skill_id))
        return tuple(hits[: max(1, int(limit))])

    # -- persistence -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "skills": [skill.to_dict() for skill in self.all()],
            "active": {skill_id: version for skill_id, version in self._active.items()},
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> SkillCatalog:
        data = payload if isinstance(payload, Mapping) else {}
        rows: list[Skill] = []
        for item in data.get("skills", ()) or ():
            skill = Skill.from_dict(item) if isinstance(item, Mapping) else None
            if skill is not None:
                rows.append(skill)
        catalog = cls(rows)
        active = data.get("active")
        if isinstance(active, Mapping):
            for skill_id, version in active.items():
                try:
                    catalog._active[str(skill_id)] = int(version)
                except (TypeError, ValueError):
                    continue
        return catalog

    # -- internals -------------------------------------------------------------

    def _best_version(
        self, skill_id: str, *, skip_deprecated: bool = False
    ) -> int:
        rows = self._versions.get(skill_id, {})
        if not rows:
            return 0
        validated = [
            version
            for version, skill in rows.items()
            if skill.lifecycle is SkillLifecycle.VALIDATED
        ]
        if validated:
            return max(validated)
        if skip_deprecated:
            # Deprecating the active version must never promote an unvalidated
            # adaptation (or another deprecated version) by accident. With no
            # trusted replacement, the skill has no active version.
            return 0
        candidates = [
            version
            for version, skill in rows.items()
            if skill.lifecycle not in (SkillLifecycle.DEPRECATED, SkillLifecycle.FAILED)
        ]
        return max(candidates) if candidates else 0

    def _duplicate_of(self, skill: Skill) -> Skill | None:
        name = skill.name.strip().lower()
        signature = skill.signature
        for other in self.all():
            if other.id == skill.id:
                continue
            if other.name.strip().lower() == name and other.signature == signature:
                return other
        return None

    def _compatibility(
        self,
        skill: Skill,
        *,
        environment: str,
        inputs: Mapping[str, Any] | None,
        available_tools: Sequence[str] | None,
    ) -> Compatibility:
        missing_inputs: list[str] = []
        if inputs is not None:
            missing_inputs = [
                name for name in skill.required_inputs() if name not in inputs
            ]
        missing_tools: list[str] = []
        if available_tools is not None:
            available = {str(tool) for tool in available_tools}
            missing_tools = [
                tool for tool in skill.tools if tool and tool not in available
            ]
        reasons: list[str] = []
        compatible = True
        if missing_inputs:
            compatible = False
            reasons.append("missing required inputs: " + ", ".join(missing_inputs))
        if missing_tools:
            compatible = False
            reasons.append("missing tools: " + ", ".join(missing_tools))
        env = str(environment or "").strip()
        environments = {str(item).strip().lower() for item in skill.environments}
        environment_changed = False
        if env and environments and "any" not in environments:
            if env.strip().lower() not in environments:
                compatible = False
                environment_changed = True
                reasons.append(f"environment {env!r} is not declared by this skill")
        return Compatibility(
            compatible=compatible,
            reasons=tuple(reasons),
            missing_inputs=tuple(missing_inputs),
            missing_capabilities=tuple(unique_sorted(missing_tools)),
            environment_changed=environment_changed,
        )

    def _index_skill(self, skill: Skill) -> None:
        for token in skill_tokens(
            " ".join(
                [
                    skill.name,
                    skill.objective,
                    skill.description,
                    skill.category.value,
                    " ".join(skill.input_names),
                    " ".join(skill.tools),
                ]
            )
        ):
            self._index.setdefault(token, set()).add(skill.id)
