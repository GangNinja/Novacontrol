"""Planning domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class PlanStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_CLARIFICATION = "needs_clarification"


class PlanStepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class PlanStep:
    title: str
    description: str
    depends_on: tuple[str, ...] = ()
    assigned_role: str | None = None
    id: str = field(default_factory=lambda: "")

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("Plan step title is required.")
        if not self.description.strip():
            raise ValueError("Plan step description is required.")
        if not self.id:
            object.__setattr__(self, "id", _slug(self.title))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "depends_on": self.depends_on,
            "assigned_role": self.assigned_role,
        }


@dataclass(frozen=True, slots=True)
class Plan:
    goal: str
    steps: tuple[PlanStep, ...]
    needs_clarification: bool = False
    id: str = field(default_factory=lambda: uuid4().hex)
    status: PlanStatus = PlanStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "status": self.status.value,
            "needs_clarification": self.needs_clarification,
            "steps": [step.to_dict() for step in self.steps],
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class WorkflowResult:
    plan_id: str
    status: PlanStatus
    step_outputs: dict[str, dict[str, object]]
    errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "status": self.status.value,
            "step_outputs": self.step_outputs,
            "errors": self.errors,
        }


def _slug(value: str) -> str:
    slug = "-".join(part for part in value.strip().lower().split() if part)
    return slug or uuid4().hex
