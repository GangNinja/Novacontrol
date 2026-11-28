"""Phase 36 product-development records.

The product pipeline deliberately stores its lifecycle as one durable project
record rather than inventing a second database or a second orchestration stack.
Documents (PRD, architecture, plan), evidence (tests, builds), decisions
(approvals, ADRs) and execution history all travel with the project so they can
be persisted through NovaControl's existing JSON state store and audited as one
traceable story.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping
from uuid import uuid4


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


class OpportunityValidationState(StrEnum):
    VALIDATED = "VALIDATED"
    CONDITIONALLY_VALIDATED = "CONDITIONALLY_VALIDATED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NOT_VIABLE = "NOT_VIABLE"
    REQUIRES_FURTHER_RESEARCH = "REQUIRES_FURTHER_RESEARCH"
    UNKNOWN = "UNKNOWN"

    @property
    def eligible(self) -> bool:
        return self in {
            OpportunityValidationState.VALIDATED,
            OpportunityValidationState.CONDITIONALLY_VALIDATED,
        }


class ProductState(StrEnum):
    DEVELOPMENT = "DEVELOPMENT"
    INTERNAL_TESTING = "INTERNAL_TESTING"
    QA_IN_PROGRESS = "QA_IN_PROGRESS"
    READY_FOR_RELEASE_REVIEW = "READY_FOR_RELEASE_REVIEW"
    APPROVED_FOR_LAUNCH = "APPROVED_FOR_LAUNCH"
    BLOCKED = "BLOCKED"
    ARCHIVED = "ARCHIVED"


class RequirementKind(StrEnum):
    FUNCTIONAL = "functional"
    NON_FUNCTIONAL = "non_functional"
    CORE_FEATURE = "core_feature"
    OPTIONAL_FEATURE = "optional_feature"
    SECURITY = "security"
    PERFORMANCE = "performance"
    ACCESSIBILITY = "accessibility"
    CONSTRAINT = "constraint"


class RequirementStatus(StrEnum):
    # Requirement records use lower-case status values throughout the PRD,
    # launch-readiness and corrective-task paths. Keep the enum aligned with
    # those stored values; an upper-case value silently made every verified
    # requirement look unverified.
    DRAFT = "draft"
    APPROVED = "approved"
    IMPLEMENTED = "implemented"
    VERIFIED = "verified"
    UNMET = "unmet"
    REJECTED = "rejected"


class TaskState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class TestStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    NOT_RUN = "not_run"
    SKIPPED = "skipped"


class GateStatus(StrEnum):
    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"


class DefectSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DefectStatus(StrEnum):
    OPEN = "open"
    FIXED = "fixed"
    VERIFIED = "verified"
    REOPENED = "reopened"


class ApprovalKind(StrEnum):
    OPPORTUNITY_CONDITIONS = "opportunity_conditions"
    REQUIREMENTS = "requirements"
    ARCHITECTURE = "architecture"
    PLAN = "plan"
    RELEASE = "release"
    LAUNCH = "launch"


RELEASE_TARGETS = ("local", "desktop", "web", "api", "container")


@dataclass
class ProductProject:
    """The persistent record for one isolated product project."""

    id: str
    name: str
    opportunity_id: str
    opportunity_title: str
    validation_state: str
    validation_report: dict[str, Any]
    opportunity_snapshot: dict[str, Any]
    conditions: list[str] = field(default_factory=list)
    condition_approval: dict[str, Any] = field(default_factory=dict)
    state: str = ProductState.DEVELOPMENT.value
    workspace_path: str = ""
    repository: str = ""
    release_target: str = "local"
    environment: dict[str, Any] = field(default_factory=dict)
    generated_files: dict[str, str] = field(default_factory=dict)
    prd: dict[str, Any] | None = None
    prd_history: list[dict[str, Any]] = field(default_factory=list)
    architecture: dict[str, Any] | None = None
    architecture_history: list[dict[str, Any]] = field(default_factory=list)
    plan: dict[str, Any] | None = None
    tests: list[dict[str, Any]] = field(default_factory=list)
    quality_gates: list[dict[str, Any]] = field(default_factory=list)
    build: dict[str, Any] = field(default_factory=dict)
    defects: list[dict[str, Any]] = field(default_factory=list)
    releases: list[dict[str, Any]] = field(default_factory=list)
    readiness: dict[str, Any] | None = None
    launch_approval: dict[str, Any] = field(default_factory=dict)
    condition_resolutions: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    iterations: int = 0
    development_started_at: str = ""
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    archived_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancellation_reason: str = ""
    last_error: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("A product project needs an id.")
        if not self.name.strip():
            raise ValueError("A product project needs a name.")
        if not self.opportunity_id.strip():
            raise ValueError("A product project must retain its originating opportunity.")

    @property
    def cancelled(self) -> bool:
        return self.cancelled_at is not None

    def tasks(self) -> list[dict[str, Any]]:
        return list((self.plan or {}).get("tasks") or [])

    def requirements(self) -> list[dict[str, Any]]:
        return list((self.prd or {}).get("requirements") or [])

    def requirement(self, requirement_id: str) -> dict[str, Any] | None:
        return next(
            (row for row in self.requirements() if row.get("id") == requirement_id),
            None,
        )

    def task(self, task_id: str) -> dict[str, Any] | None:
        return next((row for row in self.tasks() if row.get("id") == task_id), None)

    def gate(self, gate_id: str) -> dict[str, Any] | None:
        return next(
            (row for row in self.quality_gates if row.get("id") == gate_id), None
        )

    def defect(self, defect_id: str) -> dict[str, Any] | None:
        return next((row for row in self.defects if row.get("id") == defect_id), None)

    def release(self, release_id: str = "") -> dict[str, Any] | None:
        if release_id:
            return next((row for row in self.releases if row.get("id") == release_id), None)
        return self.releases[-1] if self.releases else None

    def summary(self) -> dict[str, Any]:
        tasks = self.tasks()
        completed = sum(1 for row in tasks if row.get("status") == TaskState.COMPLETED.value)
        open_defects = [
            row for row in self.defects if row.get("status") in {DefectStatus.OPEN.value, DefectStatus.REOPENED.value}
        ]
        return {
            "id": self.id,
            "name": self.name,
            "opportunity_id": self.opportunity_id,
            "opportunity_title": self.opportunity_title,
            "validation_state": self.validation_state,
            "state": self.state,
            "conditions_count": len(self.conditions),
            "workspace_path": self.workspace_path,
            "release_target": self.release_target,
            "prd_approved": bool((self.prd or {}).get("status") == "approved"),
            "prd_version": (self.prd or {}).get("version"),
            "architecture_approved": bool(
                (self.architecture or {}).get("status") == "approved"
            ),
            "tasks": len(tasks),
            "tasks_completed": completed,
            "open_defects": len(open_defects),
            "tests": len(self.tests),
            "required_gates_passed": self.required_gates_passed(),
            "releases": len(self.releases),
            "readiness": (self.readiness or {}).get("status"),
            "cancelled": self.cancelled,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }

    def required_gates_passed(self) -> bool:
        required = [row for row in self.quality_gates if row.get("required", True)]
        return bool(required) and all(
            row.get("status") == GateStatus.PASSED.value for row in required
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "opportunity_id": self.opportunity_id,
            "opportunity_title": self.opportunity_title,
            "validation_state": self.validation_state,
            "validation_report": dict(self.validation_report),
            "opportunity_snapshot": dict(self.opportunity_snapshot),
            "conditions": list(self.conditions),
            "condition_approval": dict(self.condition_approval),
            "state": self.state,
            "workspace_path": self.workspace_path,
            "repository": self.repository,
            "release_target": self.release_target,
            "environment": dict(self.environment),
            "generated_files": dict(self.generated_files),
            "prd": self.prd,
            "prd_history": list(self.prd_history),
            "architecture": self.architecture,
            "architecture_history": list(self.architecture_history),
            "plan": self.plan,
            "tests": list(self.tests),
            "quality_gates": list(self.quality_gates),
            "build": dict(self.build),
            "defects": list(self.defects),
            "releases": list(self.releases),
            "readiness": self.readiness,
            "launch_approval": dict(self.launch_approval),
            "condition_resolutions": list(self.condition_resolutions),
            "approvals": list(self.approvals),
            "events": list(self.events),
            "agents": list(self.agents),
            "iterations": self.iterations,
            "development_started_at": self.development_started_at,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "archived_at": _iso(self.archived_at),
            "cancelled_at": _iso(self.cancelled_at),
            "cancellation_reason": self.cancellation_reason,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ProductProject:
        return cls(
            id=str(payload.get("id", "")),
            name=str(payload.get("name", "")),
            opportunity_id=str(payload.get("opportunity_id", "")),
            opportunity_title=str(payload.get("opportunity_title", "")),
            validation_state=str(payload.get("validation_state", "")),
            validation_report=dict(payload.get("validation_report") or {}),
            opportunity_snapshot=dict(payload.get("opportunity_snapshot") or {}),
            conditions=[str(item) for item in payload.get("conditions") or []],
            condition_approval=dict(payload.get("condition_approval") or {}),
            state=str(payload.get("state", ProductState.DEVELOPMENT.value)),
            workspace_path=str(payload.get("workspace_path", "")),
            repository=str(payload.get("repository", "")),
            release_target=str(payload.get("release_target", "local")),
            environment=dict(payload.get("environment") or {}),
            generated_files={
                str(key): str(value)
                for key, value in (payload.get("generated_files") or {}).items()
            },
            prd=payload.get("prd") if isinstance(payload.get("prd"), Mapping) else None,
            prd_history=[
                dict(row)
                for row in payload.get("prd_history") or []
                if isinstance(row, Mapping)
            ],
            architecture=payload.get("architecture")
            if isinstance(payload.get("architecture"), Mapping)
            else None,
            architecture_history=[
                dict(row)
                for row in payload.get("architecture_history") or []
                if isinstance(row, Mapping)
            ],
            plan=payload.get("plan") if isinstance(payload.get("plan"), Mapping) else None,
            tests=[
                dict(row) for row in payload.get("tests") or [] if isinstance(row, Mapping)
            ],
            quality_gates=[
                dict(row)
                for row in payload.get("quality_gates") or []
                if isinstance(row, Mapping)
            ],
            build=dict(payload.get("build") or {}),
            defects=[
                dict(row)
                for row in payload.get("defects") or []
                if isinstance(row, Mapping)
            ],
            releases=[
                dict(row)
                for row in payload.get("releases") or []
                if isinstance(row, Mapping)
            ],
            readiness=payload.get("readiness")
            if isinstance(payload.get("readiness"), Mapping)
            else None,
            launch_approval=dict(payload.get("launch_approval") or {}),
            condition_resolutions=[
                dict(row)
                for row in payload.get("condition_resolutions") or []
                if isinstance(row, Mapping)
            ],
            approvals=[
                dict(row)
                for row in payload.get("approvals") or []
                if isinstance(row, Mapping)
            ],
            events=[
                dict(row)
                for row in payload.get("events") or []
                if isinstance(row, Mapping)
            ],
            agents=[str(item) for item in payload.get("agents") or []],
            iterations=int(payload.get("iterations") or 0),
            development_started_at=str(payload.get("development_started_at", "")),
            created_at=_dt(payload.get("created_at")) or utcnow(),
            updated_at=_dt(payload.get("updated_at")) or utcnow(),
            archived_at=_dt(payload.get("archived_at")),
            cancelled_at=_dt(payload.get("cancelled_at")),
            cancellation_reason=str(payload.get("cancellation_reason", "")),
            last_error=str(payload.get("last_error", "")),
        )


@dataclass(frozen=True, slots=True)
class ProductTask:
    """The execution-facing view of one plan task."""

    id: str
    product_id: str
    title: str
    description: str
    role: str
    requirement_ids: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    priority: str = "medium"

    def to_agent_metadata(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "product_task_id": self.id,
            "requirement_ids": list(self.requirement_ids),
            "dependencies": list(self.dependencies),
            "authority": "advisory execution; the product service records verified evidence",
        }


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """What an executor reports. Only ``verified`` may complete a task."""

    verified: bool
    summary: str
    evidence: tuple[str, ...] = ()
    files: Mapping[str, str] = field(default_factory=dict)
    agent: str = ""

    def __post_init__(self) -> None:
        if self.verified and not self.evidence:
            raise ValueError("A verified task needs at least one evidence item.")
        if self.verified and not self.summary.strip():
            raise ValueError("A verified task needs a summary.")


__all__ = [
    "ApprovalKind",
    "DefectSeverity",
    "DefectStatus",
    "GateStatus",
    "OpportunityValidationState",
    "ProductProject",
    "ProductState",
    "ProductTask",
    "RELEASE_TARGETS",
    "RequirementKind",
    "RequirementStatus",
    "TaskOutcome",
    "TaskState",
    "TestStatus",
    "new_id",
    "utcnow",
]
