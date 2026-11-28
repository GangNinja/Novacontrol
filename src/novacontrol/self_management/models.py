"""Value models for Phase 34 trustworthy self-management.

The models are deliberately small, frozen and JSON-shaped.  They are the
contract between the health collector, the incident store, the recovery
executor and the API/UI.  A diagnosis carries its CONFIDENCE separately from
its prose: "probable" must never be rendered as "confirmed" by accident.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping
from uuid import uuid4

from novacontrol.core.diagnostics import HealthState
from novacontrol.core.security import RiskLevel


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class AutonomyMode(StrEnum):
    """How far standing authorisation reaches for operational actions.

    The names mirror the phase's four levels.  ``AUTOMATIC`` is not "do
    anything": it permits only actions registered as low-risk, reversible and
    explicitly automatic.  Every other action still needs an approval.
    """

    OBSERVE = "observe"
    RECOMMEND = "recommend"
    CONTROLLED = "controlled"
    AUTOMATIC = "automatic"

    @property
    def executes(self) -> bool:
        return self in (AutonomyMode.CONTROLLED, AutonomyMode.AUTOMATIC)

    @property
    def automatic(self) -> bool:
        return self is AutonomyMode.AUTOMATIC


class IncidentState(StrEnum):
    DETECTED = "detected"
    INVESTIGATING = "investigating"
    DIAGNOSED = "diagnosed"
    RECOVERY_PENDING = "recovery_pending"
    RECOVERING = "recovering"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    UNRESOLVED = "unresolved"

    @property
    def open(self) -> bool:
        return self not in (IncidentState.RESOLVED, IncidentState.ESCALATED)


class CauseConfidence(StrEnum):
    CONFIRMED = "confirmed"
    PROBABLE = "probable"
    POSSIBLE = "possible"
    UNKNOWN = "unknown"

    @property
    def rank(self) -> int:
        return list(CauseConfidence).index(self)


class IncidentSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return list(IncidentSeverity).index(self)

    @classmethod
    def from_health(cls, state: HealthState) -> IncidentSeverity:
        return {
            HealthState.OK: cls.INFO,
            HealthState.SKIPPED: cls.INFO,
            HealthState.DEGRADED: cls.WARNING,
            HealthState.UNKNOWN: cls.WARNING,
            HealthState.FAILING: cls.ERROR,
        }[state]


class RecoveryStatus(StrEnum):
    RECOMMENDED = "recommended"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    REFUSED = "refused"
    ESCALATED = "escalated"


class MaintenanceKind(StrEnum):
    TEMP_FILES = "temporary_files"
    STALE_CACHE = "stale_cache"
    LOG_ROTATION = "log_rotation"
    DATABASE = "database"
    STALE_PROCESS = "stale_process"
    RESOURCE_LEAK = "resource_leak"
    CONFIGURATION = "configuration"
    STORAGE = "storage"


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    """One component's measured state, never inferred from "the process is up"."""

    component: str
    category: str
    status: HealthState
    message: str
    severity: IncidentSeverity = IncidentSeverity.INFO
    duration_ms: float | None = None
    checked_at: datetime = field(default_factory=utcnow)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "category": self.category,
            "status": self.status.value,
            "message": self.message,
            "severity": self.severity.value,
            "duration_ms": (
                round(float(self.duration_ms), 3)
                if self.duration_ms is not None
                else None
            ),
            "checked_at": _iso(self.checked_at),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """One live health pass, including the metrics it actually read."""

    snapshot_id: str
    overall: HealthState
    components: tuple[ComponentHealth, ...]
    metrics: Mapping[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=utcnow)
    duration_ms: float = 0.0
    event_signals: tuple[Mapping[str, Any], ...] = ()

    @property
    def unhealthy(self) -> tuple[ComponentHealth, ...]:
        return tuple(row for row in self.components if not row.status.healthy)

    @property
    def healthy_count(self) -> int:
        return sum(1 for row in self.components if row.status.healthy)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "overall": self.overall.value,
            "healthy": self.overall.healthy,
            "components": [row.to_dict() for row in self.components],
            "metrics": dict(self.metrics),
            "checked_at": _iso(self.checked_at),
            "duration_ms": round(float(self.duration_ms), 3),
            "event_signals": [dict(row) for row in self.event_signals],
        }


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """An evidence-backed finding, with confidence kept explicit."""

    rule_id: str
    component: str
    title: str
    cause: str
    confidence: CauseConfidence
    severity: IncidentSeverity
    evidence: tuple[str, ...] = ()
    impact: str = ""
    recommendations: tuple[str, ...] = ()
    rationale: str = ""
    fingerprint: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "component": self.component,
            "title": self.title,
            "cause": self.cause,
            "confidence": self.confidence.value,
            "severity": self.severity.value,
            "evidence": list(self.evidence),
            "impact": self.impact,
            "recommendations": list(self.recommendations),
            "rationale": self.rationale,
            "fingerprint": self.fingerprint,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RecoveryAttempt:
    action_id: str
    status: RecoveryStatus
    started_at: datetime
    finished_at: datetime | None = None
    detail: str = ""
    approved_by: str = ""
    attempts: int = 0
    before: str = ""
    after: str = ""
    verification: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "status": self.status.value,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "detail": self.detail,
            "approved_by": self.approved_by,
            "attempts": self.attempts,
            "before": self.before,
            "after": self.after,
            "verification": dict(self.verification),
        }


@dataclass(frozen=True, slots=True)
class Incident:
    """One operationally significant problem, durable across restarts."""

    id: str
    fingerprint: str
    component: str
    title: str
    severity: IncidentSeverity
    state: IncidentState
    detected_at: datetime
    updated_at: datetime
    symptoms: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    diagnosis: Mapping[str, Any] = field(default_factory=dict)
    cause_confidence: CauseConfidence = CauseConfidence.UNKNOWN
    impact: str = ""
    recovery_attempts: tuple[RecoveryAttempt, ...] = ()
    resolution: str = ""
    related: tuple[str, ...] = ()
    recurrence: int = 0
    notified_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def open(self) -> bool:
        return self.state.open

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "fingerprint": self.fingerprint,
            "component": self.component,
            "title": self.title,
            "severity": self.severity.value,
            "state": self.state.value,
            "open": self.open,
            "detected_at": _iso(self.detected_at),
            "updated_at": _iso(self.updated_at),
            "symptoms": list(self.symptoms),
            "evidence": list(self.evidence),
            "diagnosis": dict(self.diagnosis),
            "cause_confidence": self.cause_confidence.value,
            "impact": self.impact,
            "recovery_attempts": [row.to_dict() for row in self.recovery_attempts],
            "resolution": self.resolution,
            "related": list(self.related),
            "recurrence": self.recurrence,
            "notified_at": _iso(self.notified_at),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Incident:
        attempts = tuple(
            RecoveryAttempt(
                action_id=str(row.get("action_id", "")),
                status=RecoveryStatus(str(row.get("status", "failed"))),
                started_at=_dt(row.get("started_at")) or utcnow(),
                finished_at=_dt(row.get("finished_at")),
                detail=str(row.get("detail", "")),
                approved_by=str(row.get("approved_by", "")),
                attempts=int(row.get("attempts", 0) or 0),
                before=str(row.get("before", "")),
                after=str(row.get("after", "")),
                verification=dict(row.get("verification") or {}),
            )
            for row in payload.get("recovery_attempts", ())
            if isinstance(row, Mapping)
        )
        return cls(
            id=str(payload["id"]),
            fingerprint=str(payload["fingerprint"]),
            component=str(payload["component"]),
            title=str(payload.get("title", "")),
            severity=IncidentSeverity(str(payload.get("severity", "warning"))),
            state=IncidentState(str(payload.get("state", "detected"))),
            detected_at=_dt(payload.get("detected_at")) or utcnow(),
            updated_at=_dt(payload.get("updated_at")) or utcnow(),
            symptoms=tuple(str(row) for row in payload.get("symptoms", ())),
            evidence=tuple(str(row) for row in payload.get("evidence", ())),
            diagnosis=dict(payload.get("diagnosis") or {}),
            cause_confidence=CauseConfidence(
                str(payload.get("cause_confidence", "unknown"))
            ),
            impact=str(payload.get("impact", "")),
            recovery_attempts=attempts,
            resolution=str(payload.get("resolution", "")),
            related=tuple(str(row) for row in payload.get("related", ())),
            recurrence=int(payload.get("recurrence", 0) or 0),
            notified_at=_dt(payload.get("notified_at")),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    """A registered, bounded recovery procedure and its safety declaration."""

    id: str
    component: str
    title: str
    description: str
    risk: RiskLevel
    reversible: bool = True
    destructive: bool = False
    automatic: bool = False
    requires_approval: bool = True
    timeout_seconds: float = 10.0
    max_attempts: int = 2
    cooldown_seconds: float = 300.0
    impact: str = ""

    @property
    def safe_automatic(self) -> bool:
        """Automatic eligibility is a property of the ACTION, not the incident."""
        return (
            self.automatic
            and self.reversible
            and not self.destructive
            and self.risk in (RiskLevel.LOW,)
            and not self.requires_approval
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "component": self.component,
            "title": self.title,
            "description": self.description,
            "risk": self.risk.value,
            "reversible": self.reversible,
            "destructive": self.destructive,
            "automatic": self.automatic,
            "requires_approval": self.requires_approval,
            "timeout_seconds": self.timeout_seconds,
            "max_attempts": self.max_attempts,
            "cooldown_seconds": self.cooldown_seconds,
            "impact": self.impact,
        }


@dataclass(frozen=True, slots=True)
class MaintenanceItem:
    id: str
    kind: MaintenanceKind
    target: str
    action: str
    detail: str
    bytes_affected: int = 0
    files_affected: int = 0
    destructive: bool = False
    requires_approval: bool = False
    protected: bool = False
    status: str = "proposed"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "target": self.target,
            "action": self.action,
            "detail": self.detail,
            "bytes_affected": self.bytes_affected,
            "files_affected": self.files_affected,
            "destructive": self.destructive,
            "requires_approval": self.requires_approval,
            "protected": self.protected,
            "status": self.status,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    mode: str
    items: tuple[MaintenanceItem, ...]
    started_at: datetime
    finished_at: datetime | None = None
    approved_by: str = ""
    removed_bytes: int = 0
    changed_files: int = 0
    skipped: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "items": [row.to_dict() for row in self.items],
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "approved_by": self.approved_by,
            "removed_bytes": self.removed_bytes,
            "changed_files": self.changed_files,
            "skipped": list(self.skipped),
            "errors": list(self.errors),
        }


@dataclass(frozen=True, slots=True)
class ConfigVersion:
    version: int
    created_at: datetime
    checksum: str
    settings: Mapping[str, Any]
    note: str = ""
    known_good: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "created_at": _iso(self.created_at),
            "checksum": self.checksum,
            "settings": dict(self.settings),
            "note": self.note,
            "known_good": self.known_good,
        }


@dataclass(frozen=True, slots=True)
class ConfigReport:
    valid: bool
    checksum: str
    issues: tuple[str, ...] = ()
    current: Mapping[str, Any] = field(default_factory=dict)
    versions: tuple[ConfigVersion, ...] = ()
    active_version: int | None = None
    recovered: bool = False
    recovery_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "checksum": self.checksum,
            "issues": list(self.issues),
            "current": dict(self.current),
            "versions": [row.to_dict() for row in self.versions],
            "active_version": self.active_version,
            "recovered": self.recovered,
            "recovery_note": self.recovery_note,
        }


def checksum(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def new_id(prefix: str = "") -> str:
    return (f"{prefix}-" if prefix else "") + uuid4().hex
