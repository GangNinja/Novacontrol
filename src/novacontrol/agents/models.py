"""Agent domain models."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class AgentRole(StrEnum):
    RESEARCH = "research"
    CODING = "coding"
    DEBUG = "debug"
    PLANNING = "planning"
    DOCUMENTATION = "documentation"
    DESIGN = "design"
    TESTING = "testing"
    REVIEW = "review"
    BROWSER = "browser"
    DESKTOP_AUTOMATION = "desktop_automation"
    PROJECT_MANAGER = "project_manager"
    COORDINATOR = "coordinator"


class AgentTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AgentTask:
    goal: str
    role: AgentRole | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)
    status: AgentTaskStatus = AgentTaskStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.goal.strip():
            raise ValueError("Agent task goal is required.")

    def with_role(self, role: AgentRole) -> "AgentTask":
        return replace(self, role=role, status=AgentTaskStatus.RUNNING)


@dataclass(frozen=True, slots=True)
class AgentResponse:
    task_id: str
    agent_name: str
    role: AgentRole
    status: AgentTaskStatus
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent_name": self.agent_name,
            "role": self.role.value,
            "status": self.status.value,
            "content": self.content,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class AgentMessage:
    from_agent: str
    to_agent: str
    content: str
    task_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
