"""General automation models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class AutomationWorkflowStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AutomationStep:
    name: str
    action_type: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "action_type": self.action_type,
            "parameters": dict(self.parameters),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AutomationStep":
        return cls(
            name=str(payload["name"]),
            action_type=str(payload["action_type"]),
            parameters=dict(payload.get("parameters", {})),
            id=str(payload["id"]),
        )


@dataclass(frozen=True, slots=True)
class AutomationWorkflow:
    name: str
    steps: tuple[AutomationStep, ...]
    status: AutomationWorkflowStatus = AutomationWorkflowStatus.DRAFT
    id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status.value,
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AutomationWorkflow":
        return cls(
            name=str(payload["name"]),
            steps=tuple(AutomationStep.from_dict(item) for item in payload.get("steps", ())),
            status=AutomationWorkflowStatus(str(payload.get("status", AutomationWorkflowStatus.DRAFT.value))),
            id=str(payload["id"]),
        )
