"""General automation models.

Two families live here, and the difference is deliberate:

* :class:`AutomationWorkflow` is the long-standing shape — a named sequence of
  steps a caller stores and replays. It describes WORK.
* :class:`AutomationTask` is what Phase 13 adds: one request plus a schedule,
  with the state the phase asks to be tracked (status, next and previous
  execution, failure count, enabled, permissions, created timestamp). It
  describes WHEN work happens, and it carries the request text rather than a
  copy of the steps, because the work itself is re-derived by the pipeline that
  runs it (intent → decision → plan → permission → execution) instead of being
  frozen at scheduling time.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from novacontrol.automation.schedule import Schedule


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
    def from_dict(cls, payload: dict[str, Any]) -> AutomationStep:
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
    def from_dict(cls, payload: dict[str, Any]) -> AutomationWorkflow:
        return cls(
            name=str(payload["name"]),
            steps=tuple(AutomationStep.from_dict(item) for item in payload.get("steps", ())),
            status=AutomationWorkflowStatus(
                str(payload.get("status", AutomationWorkflowStatus.DRAFT.value))
            ),
            id=str(payload["id"]),
        )


# --------------------------------------------------------------------------- #
# Phase 13: scheduled automations
# --------------------------------------------------------------------------- #


class AutomationKind(StrEnum):
    """What makes an automation run."""

    ONCE = "once"
    RECURRING = "recurring"
    CONDITIONAL = "conditional"


class AutomationStatus(StrEnum):
    """Where an automation is in its life.

    ``PENDING_APPROVAL`` is a state rather than a flag on purpose: a task that
    was created but never authorized is a thing an operator can go and look at,
    and it is neither scheduled (it will not fire) nor cancelled (it was never
    refused).
    """

    PENDING_APPROVAL = "pending_approval"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DISABLED = "disabled"


class AutomationRunStatus(StrEnum):
    """How one execution ended.

    ``DENIED`` and ``SKIPPED`` are separate from ``FAILED`` because neither is a
    failure of the work: one is the permission layer refusing to start it, the
    other is its condition not being met. Counting either as a failure would
    disable a healthy automation for reasons it did not cause.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    SKIPPED = "skipped"


class ConditionKind(StrEnum):
    """The conditions this build can actually evaluate.

    A condition is a NAMED check with an argument, never a snippet of code: an
    automation that stored executable text would be a second, unreviewed
    execution path beside the one the permission layer gates. ``ALWAYS`` is the
    default so a task without a condition is unconditional rather than unset.
    """

    ALWAYS = "always"
    PATH_EXISTS = "path_exists"
    TESTS_PASS = "tests_pass"


@dataclass(frozen=True, slots=True)
class AutomationCondition:
    """A named check that decides whether a due run may proceed."""

    kind: ConditionKind = ConditionKind.ALWAYS
    argument: str = ""

    def describe(self) -> str:
        if self.kind is ConditionKind.ALWAYS:
            return "always"
        if self.kind is ConditionKind.PATH_EXISTS:
            return f"{self.argument} exists"
        return f"the tests in {self.argument or 'the project'} pass"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "argument": self.argument, "description": self.describe()}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AutomationCondition:
        return cls(
            kind=ConditionKind(str(payload.get("kind", ConditionKind.ALWAYS.value))),
            argument=str(payload.get("argument", "")),
        )


@dataclass(frozen=True, slots=True)
class AutomationRun:
    """One execution of an automation, and what it actually did."""

    status: AutomationRunStatus
    started_at: datetime
    finished_at: datetime
    detail: str = ""
    verified: bool = False
    #: The request's own correlation id, so a run's events can be followed
    #: through the same thread as the original request that created it.
    correlation_id: str = ""
    failure_kind: str = ""
    recovered: bool = False
    #: The permission verdict this run was started (or refused) under, so the
    #: audit trail can show WHY a run was denied rather than only that it was.
    permission: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)

    @property
    def duration_ms(self) -> float:
        return max(0.0, (self.finished_at - self.started_at).total_seconds() * 1000.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_ms": round(self.duration_ms, 3),
            "detail": self.detail,
            "verified": self.verified,
            "correlation_id": self.correlation_id,
            "failure_kind": self.failure_kind,
            "recovered": self.recovered,
            "permission": dict(self.permission),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AutomationRun:
        return cls(
            status=AutomationRunStatus(str(payload["status"])),
            started_at=datetime.fromisoformat(str(payload["started_at"])),
            finished_at=datetime.fromisoformat(str(payload["finished_at"])),
            detail=str(payload.get("detail", "")),
            verified=bool(payload.get("verified", False)),
            correlation_id=str(payload.get("correlation_id", "")),
            failure_kind=str(payload.get("failure_kind", "")),
            recovered=bool(payload.get("recovered", False)),
            permission=dict(payload.get("permission", {})),
            id=str(payload["id"]),
        )


@dataclass(frozen=True, slots=True)
class AutomationTask:
    """One scheduled request and the state of scheduling it.

    Every field the phase asks to be tracked is here and is readable from
    :meth:`to_dict`: the id, the task definition (``request``), the schedule,
    the status, the next and previous execution, the failure count, whether it
    is enabled, the permissions it was approved with, and when it was created.
    """

    request: str
    schedule: Schedule
    name: str = ""
    kind: AutomationKind = AutomationKind.RECURRING
    status: AutomationStatus = AutomationStatus.SCHEDULED
    enabled: bool = False
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    failure_count: int = 0
    run_count: int = 0
    permissions: tuple[str, ...] = ()
    requires_approval: bool = True
    approved: bool = False
    approved_by: str = ""
    condition: AutomationCondition = field(default_factory=AutomationCondition)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_outcome: str = ""
    runs: tuple[AutomationRun, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if not self.request.strip():
            raise ValueError("A scheduled automation needs the request it will run.")
        if self.failure_count < 0 or self.run_count < 0:
            raise ValueError("Automation counters cannot be negative.")

    @property
    def automation_id(self) -> str:
        """The specification's name for ``id``."""
        return self.id

    @property
    def label(self) -> str:
        return self.name or self.request

    def with_(self, **changes: Any) -> AutomationTask:
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "automation_id": self.id,
            "id": self.id,
            "name": self.name,
            "label": self.label,
            "request": self.request,
            "kind": self.kind.value,
            "schedule": self.schedule.to_dict(),
            "schedule_description": self.schedule.describe(),
            "status": self.status.value,
            "enabled": self.enabled,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "failure_count": self.failure_count,
            "run_count": self.run_count,
            "permissions": list(self.permissions),
            "requires_approval": self.requires_approval,
            "approved": self.approved,
            "approved_by": self.approved_by,
            "condition": self.condition.to_dict(),
            "created_at": self.created_at.isoformat(),
            "last_outcome": self.last_outcome,
            "runs": [run.to_dict() for run in self.runs],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AutomationTask:
        raw_next = payload.get("next_run_at")
        raw_last = payload.get("last_run_at")
        raw_created = payload.get("created_at")
        raw_condition = payload.get("condition")
        return cls(
            request=str(payload["request"]),
            schedule=Schedule.from_dict(dict(payload["schedule"])),
            name=str(payload.get("name", "")),
            kind=AutomationKind(str(payload.get("kind", AutomationKind.RECURRING.value))),
            status=AutomationStatus(str(payload.get("status", AutomationStatus.SCHEDULED.value))),
            enabled=bool(payload.get("enabled", False)),
            next_run_at=datetime.fromisoformat(str(raw_next)) if raw_next else None,
            last_run_at=datetime.fromisoformat(str(raw_last)) if raw_last else None,
            failure_count=int(payload.get("failure_count", 0)),
            run_count=int(payload.get("run_count", 0)),
            permissions=tuple(str(item) for item in payload.get("permissions", ())),
            requires_approval=bool(payload.get("requires_approval", True)),
            approved=bool(payload.get("approved", False)),
            approved_by=str(payload.get("approved_by", "")),
            condition=(
                AutomationCondition.from_dict(raw_condition)
                if isinstance(raw_condition, Mapping)
                else AutomationCondition()
            ),
            created_at=(
                datetime.fromisoformat(str(raw_created))
                if raw_created
                else datetime.now(UTC)
            ),
            last_outcome=str(payload.get("last_outcome", "")),
            runs=tuple(AutomationRun.from_dict(item) for item in payload.get("runs", ())),
            metadata=dict(payload.get("metadata", {})),
            id=str(payload["id"]),
        )
