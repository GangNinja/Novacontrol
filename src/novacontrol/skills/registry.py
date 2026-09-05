"""Skill registry and callable adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from inspect import isawaitable
from typing import Any

from novacontrol.skills.models import SkillInvocation, SkillResult, SkillSchema, SkillStatus

SkillCallable = Callable[[Mapping[str, Any]], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]


class FunctionSkill:
    def __init__(self, schema: SkillSchema, function: SkillCallable) -> None:
        self.schema = schema
        self._function = function

    async def invoke(self, invocation: SkillInvocation) -> SkillResult:
        missing = [key for key in self.schema.input_keys if key not in invocation.inputs]
        if missing:
            return SkillResult(
                invocation.id,
                invocation.skill_name,
                SkillStatus.FAILED,
                error=f"Missing input keys: {', '.join(missing)}",
            )
        try:
            result = self._function(invocation.inputs)
            if isawaitable(result):
                result = await result
            return SkillResult(invocation.id, invocation.skill_name, SkillStatus.COMPLETED, dict(result))
        except Exception as exc:
            return SkillResult(
                invocation.id,
                invocation.skill_name,
                SkillStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, FunctionSkill] = {}

    def register(self, skill: FunctionSkill) -> None:
        name = skill.schema.name
        if name in self._skills:
            raise ValueError(f"Skill already registered: {name}")
        self._skills[name] = skill

    def get(self, name: str) -> FunctionSkill:
        try:
            return self._skills[name]
        except KeyError as exc:
            raise KeyError(f"Skill not registered: {name}") from exc

    def list(self) -> tuple[FunctionSkill, ...]:
        return tuple(self._skills[name] for name in sorted(self._skills))

    async def invoke(self, invocation: SkillInvocation) -> SkillResult:
        return await self.get(invocation.skill_name).invoke(invocation)
