"""Agentic task state: the structured execution record the orchestrator owns.

This is the single source of truth for a running task. Every phase
(plan, observations, actions, verification, recovery, learning) appends to
this state so the whole loop is observable and resumable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class TaskPhase(StrEnum):
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Observation:
    """One perception snapshot taken during a task."""

    source: str  # "browser_dom" | "vision" | "window_probe" | "runner_output" | ...
    summary: str
    ui_state: dict[str, Any] | None = None
    data: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "summary": self.summary,
            "ui_state": self.ui_state,
            "data": self.data,
            "at": self.at,
        }


@dataclass(frozen=True, slots=True)
class ActionRecord:
    """One executed action and its structured outcome."""

    step_id: str
    action: str
    target: str
    success: bool
    observed_change: bool | None = None
    detail: str = ""
    output: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "action": self.action,
            "target": self.target,
            "success": self.success,
            "observed_change": self.observed_change,
            "detail": self.detail,
            "output": self.output,
            "at": self.at,
        }


@dataclass(frozen=True, slots=True)
class Lesson:
    """A reusable fact or strategy learned during a task."""

    kind: str  # "workflow" | "failure_analysis" | "recovery_strategy" | "observation"
    text: str
    confidence: float = 0.5
    source: str = "orchestrator"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "confidence": self.confidence, "source": self.source}


@dataclass(slots=True)
class AgentTaskState:
    """Mutable execution state for one agentic task."""

    goal: str
    id: str = field(default_factory=lambda: uuid4().hex)
    status: TaskPhase = TaskPhase.PLANNING
    interpretation: dict[str, Any] = field(default_factory=dict)
    plan: list[dict[str, Any]] = field(default_factory=list)
    current_step: str = ""
    observations: list[Observation] = field(default_factory=list)
    actions: list[ActionRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    recoveries: list[dict[str, Any]] = field(default_factory=list)
    learned_information: list[Lesson] = field(default_factory=list)
    summary: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC).isoformat()

    def transition(self, phase: TaskPhase) -> None:
        self.status = phase
        self.touch()

    def add_error(self, message: str) -> None:
        self.errors.append(f"{datetime.now(UTC).isoformat()} {message}")
        self.touch()

    def learn(self, lesson: Lesson) -> None:
        self.learned_information.append(lesson)
        self.touch()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.id,
            "goal": self.goal,
            "status": self.status.value,
            "interpretation": self.interpretation,
            "plan": self.plan,
            "current_step": self.current_step,
            "observations": [o.to_dict() for o in self.observations],
            "actions": [a.to_dict() for a in self.actions],
            "errors": self.errors,
            "verification": self.verification,
            "recoveries": self.recoveries,
            "learned_information": [l.to_dict() for l in self.learned_information],
            "summary": self.summary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AgentTaskState":
        state = cls(
            goal=str(payload["goal"]),
            id=str(payload.get("task_id") or payload.get("id") or uuid4().hex),
            status=TaskPhase(str(payload.get("status", TaskPhase.PLANNING.value))),
            interpretation=dict(payload.get("interpretation", {})),
            plan=list(payload.get("plan", [])),
            current_step=str(payload.get("current_step", "")),
            errors=[str(e) for e in payload.get("errors", [])],
            verification=dict(payload.get("verification", {})),
            recoveries=list(payload.get("recoveries", [])),
            summary=str(payload.get("summary", "")),
            created_at=str(payload.get("created_at", datetime.now(UTC).isoformat())),
            updated_at=str(payload.get("updated_at", datetime.now(UTC).isoformat())),
        )
        for item in payload.get("observations", []):
            state.observations.append(
                Observation(
                    source=str(item.get("source", "unknown")),
                    summary=str(item.get("summary", "")),
                    ui_state=item.get("ui_state"),
                    data=dict(item.get("data", {})),
                    at=str(item.get("at", datetime.now(UTC).isoformat())),
                )
            )
        for item in payload.get("actions", []):
            state.actions.append(
                ActionRecord(
                    step_id=str(item.get("step_id", "")),
                    action=str(item.get("action", "")),
                    target=str(item.get("target", "")),
                    success=bool(item.get("success", False)),
                    observed_change=item.get("observed_change"),
                    detail=str(item.get("detail", "")),
                    output=dict(item.get("output", {})),
                    at=str(item.get("at", datetime.now(UTC).isoformat())),
                )
            )
        for item in payload.get("learned_information", []):
            state.learned_information.append(
                Lesson(
                    kind=str(item.get("kind", "observation")),
                    text=str(item.get("text", "")),
                    confidence=float(item.get("confidence", 0.5)),
                    source=str(item.get("source", "orchestrator")),
                )
            )
        return state
