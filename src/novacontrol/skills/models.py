"""Skill domain models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class SkillStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SkillSchema:
    name: str
    description: str
    input_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillInvocation:
    skill_name: str
    inputs: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True, slots=True)
class SkillResult:
    invocation_id: str
    skill_name: str
    status: SkillStatus
    output: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "skill_name": self.skill_name,
            "status": self.status.value,
            "output": dict(self.output),
            "error": self.error,
        }
