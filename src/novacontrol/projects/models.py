"""Project domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"


class TaskStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class Milestone:
    title: str
    id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "created_at": self.created_at.isoformat()}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Milestone":
        return cls(
            title=str(payload["title"]),
            id=str(payload["id"]),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
        )


@dataclass(frozen=True, slots=True)
class ProjectTask:
    title: str
    status: TaskStatus = TaskStatus.TODO
    milestone_id: str | None = None
    id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status.value,
            "milestone_id": self.milestone_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProjectTask":
        return cls(
            title=str(payload["title"]),
            status=TaskStatus(str(payload.get("status", TaskStatus.TODO.value))),
            milestone_id=payload.get("milestone_id"),
            id=str(payload["id"]),
        )


@dataclass(frozen=True, slots=True)
class Project:
    name: str
    description: str = ""
    status: ProjectStatus = ProjectStatus.ACTIVE
    milestones: tuple[Milestone, ...] = ()
    tasks: tuple[ProjectTask, ...] = ()
    id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def progress(self) -> float:
        if not self.tasks:
            return 0.0
        done = sum(1 for task in self.tasks if task.status is TaskStatus.DONE)
        return done / len(self.tasks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "status": self.status.value,
            "milestones": [milestone.to_dict() for milestone in self.milestones],
            "tasks": [task.to_dict() for task in self.tasks],
            "progress": self.progress(),
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Project":
        return cls(
            name=str(payload["name"]),
            description=str(payload.get("description", "")),
            status=ProjectStatus(str(payload.get("status", ProjectStatus.ACTIVE.value))),
            milestones=tuple(Milestone.from_dict(item) for item in payload.get("milestones", ())),
            tasks=tuple(ProjectTask.from_dict(item) for item in payload.get("tasks", ())),
            id=str(payload["id"]),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
        )
