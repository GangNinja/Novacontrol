"""NM-13 controlled adaptive learning: records, states and evidence semantics.

The learning subsystem does not trade.  It reads *evidence* produced by NM-10
(paper execution), NM-11 (reproducible backtests) and NM-12 (Shadow Mode),
evaluates candidate strategy configurations, and records a governed promotion
history.  Three rules are encoded here rather than left to callers:

* evidence keeps its origin: a hypothetical shadow outcome is never labelled a
  paper fill and a backtest is never labelled live performance;
* every record that supports a promotion is immutable and versioned;
* a candidate has an explicit lifecycle, so approval cannot be inferred from a
  score and a rollback cannot silently become a promotion.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def parse_time(value: object) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif value in (None, ""):
        raise ValueError("a timestamp is required")
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return result.astimezone(timezone.utc)


def optional_time(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    return parse_time(value)


def decimal(value: object, *, name: str = "value") -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{name} is not a decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def learning_id(prefix: str, *parts: object) -> str:
    seed = "|".join(str(part) for part in parts)
    return prefix + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class EvidenceKind(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    SHADOW = "shadow"


class EvidenceStatus(StrEnum):
    ACCEPTED = "accepted"
    LOW_CONFIDENCE = "low_confidence"
    REJECTED = "rejected"


class ExperimentStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CandidateStatus(StrEnum):
    GENERATED = "generated"
    EVALUATING = "evaluating"
    EVALUATION_FAILED = "evaluation_failed"
    REJECTED = "rejected"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SHADOW_VALIDATION = "shadow_validation"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"
    ARCHIVED = "archived"


class AlertKind(StrEnum):
    PERFORMANCE_DETERIORATION = "performance_deterioration"
    VOLATILITY_SPIKE = "volatility_spike"
    SIGNAL_SHIFT = "signal_shift"
    RISK_INCREASE = "risk_increase"
    INSTABILITY = "instability"
    REGIME_CHANGE = "regime_change"
    SOURCE_DIVERGENCE = "source_divergence"
    ROLLBACK_RECOMMENDED = "rollback_recommended"


class AlertSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ShadowValidationStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    PASSED = "passed"
    FAILED = "failed"


class VersionStatus(StrEnum):
    CANDIDATE = "candidate"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"
    ARCHIVED = "archived"


#: The one legal experiment lifecycle.  An unlisted move raises.
EXPERIMENT_TRANSITIONS: dict[ExperimentStatus, frozenset[ExperimentStatus]] = {
    ExperimentStatus.CREATED: frozenset(
        {ExperimentStatus.QUEUED, ExperimentStatus.EVALUATING, ExperimentStatus.CANCELLED}
    ),
    ExperimentStatus.QUEUED: frozenset(
        {ExperimentStatus.EVALUATING, ExperimentStatus.CANCELLED}
    ),
    ExperimentStatus.EVALUATING: frozenset(
        {ExperimentStatus.COMPLETED, ExperimentStatus.FAILED, ExperimentStatus.CANCELLED}
    ),
    ExperimentStatus.COMPLETED: frozenset(),
    ExperimentStatus.FAILED: frozenset(),
    ExperimentStatus.CANCELLED: frozenset(),
}

#: A candidate cannot be promoted from an arbitrary state.
CANDIDATE_TRANSITIONS: dict[CandidateStatus, frozenset[CandidateStatus]] = {
    CandidateStatus.GENERATED: frozenset(
        {CandidateStatus.EVALUATING, CandidateStatus.ARCHIVED}
    ),
    CandidateStatus.EVALUATING: frozenset(
        {
            CandidateStatus.EVALUATION_FAILED,
            CandidateStatus.REJECTED,
            CandidateStatus.PENDING_APPROVAL,
        }
    ),
    CandidateStatus.EVALUATION_FAILED: frozenset({CandidateStatus.ARCHIVED}),
    CandidateStatus.REJECTED: frozenset({CandidateStatus.ARCHIVED}),
    CandidateStatus.PENDING_APPROVAL: frozenset(
        {
            CandidateStatus.APPROVED,
            CandidateStatus.SHADOW_VALIDATION,
            CandidateStatus.REJECTED,
            CandidateStatus.ARCHIVED,
        }
    ),
    CandidateStatus.APPROVED: frozenset(
        {
            CandidateStatus.SHADOW_VALIDATION,
            CandidateStatus.PROMOTED,
            CandidateStatus.ARCHIVED,
        }
    ),
    CandidateStatus.SHADOW_VALIDATION: frozenset(
        {
            CandidateStatus.PENDING_APPROVAL,
            CandidateStatus.APPROVED,
            CandidateStatus.REJECTED,
            CandidateStatus.ARCHIVED,
        }
    ),
    CandidateStatus.PROMOTED: frozenset({CandidateStatus.ROLLED_BACK}),
    CandidateStatus.ROLLED_BACK: frozenset({CandidateStatus.ARCHIVED}),
    CandidateStatus.ARCHIVED: frozenset(),
}


def ensure_experiment_transition(
    source: ExperimentStatus | str, target: ExperimentStatus | str
) -> None:
    current = ExperimentStatus(source)
    wanted = ExperimentStatus(target)
    if wanted not in EXPERIMENT_TRANSITIONS[current]:
        raise ValueError(
            f"illegal learning experiment transition {current.value} -> {wanted.value}"
        )


def ensure_candidate_transition(
    source: CandidateStatus | str, target: CandidateStatus | str
) -> None:
    current = CandidateStatus(source)
    wanted = CandidateStatus(target)
    if wanted not in CANDIDATE_TRANSITIONS[current]:
        raise ValueError(
            f"illegal learning candidate transition {current.value} -> {wanted.value}"
        )


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """One observation set with its origin, quality verdict and provenance."""

    evidence_id: str
    kind: EvidenceKind
    source_ref: str
    strategy_id: str
    strategy_version: str
    catalog_id: str
    config: dict[str, Any]
    window_start: datetime | None
    window_end: datetime | None
    observations: int
    instruments: tuple[str, ...]
    metrics: dict[str, Any]
    in_sample: bool
    out_of_sample: bool
    regimes: tuple[str, ...]
    received_at: datetime
    status: EvidenceStatus
    confidence: str
    reasons: tuple[str, ...]
    provenance: dict[str, Any]
    evidence_digest: str
    duplicate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind.value,
            "source_ref": self.source_ref,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "catalog_id": self.catalog_id,
            "config": dict(self.config),
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "observations": self.observations,
            "instruments": list(self.instruments),
            "instrument_coverage": len(self.instruments),
            "metrics": dict(self.metrics),
            "in_sample": self.in_sample,
            "out_of_sample": self.out_of_sample,
            "regimes": list(self.regimes),
            "regime_coverage": len(self.regimes),
            "received_at": self.received_at.isoformat(),
            "status": self.status.value,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "provenance": dict(self.provenance),
            "evidence_digest": self.evidence_digest,
            "duplicate": self.duplicate,
            "reliability": _RELIABILITY[self.kind],
            "is_live_performance": False,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvidenceRecord":
        return cls(
            evidence_id=str(payload["evidence_id"]),
            kind=EvidenceKind(str(payload.get("kind") or "backtest")),
            source_ref=str(payload.get("source_ref") or ""),
            strategy_id=str(payload.get("strategy_id") or ""),
            strategy_version=str(payload.get("strategy_version") or ""),
            catalog_id=str(payload.get("catalog_id") or ""),
            config=dict(payload.get("config") or {}),
            window_start=optional_time(payload.get("window_start")),
            window_end=optional_time(payload.get("window_end")),
            observations=int(payload.get("observations") or 0),
            instruments=tuple(str(item) for item in payload.get("instruments") or ()),
            metrics=dict(payload.get("metrics") or {}),
            in_sample=bool(payload.get("in_sample")),
            out_of_sample=bool(payload.get("out_of_sample")),
            regimes=tuple(str(item) for item in payload.get("regimes") or ()),
            received_at=parse_time(payload["received_at"]),
            status=EvidenceStatus(str(payload.get("status") or "rejected")),
            confidence=str(payload.get("confidence") or "0"),
            reasons=tuple(str(item) for item in payload.get("reasons") or ()),
            provenance=dict(payload.get("provenance") or {}),
            evidence_digest=str(payload.get("evidence_digest") or ""),
            duplicate=bool(payload.get("duplicate")),
        )


_RELIABILITY = {
    EvidenceKind.BACKTEST: "historical simulation with explicit costs; not live performance",
    EvidenceKind.PAPER: "simulated paper execution; no real money moved",
    EvidenceKind.SHADOW: "hypothetical shadow observation; no order was created or filled",
}


@dataclass(frozen=True, slots=True)
class PerformanceSnapshot:
    snapshot_id: str
    strategy_id: str
    strategy_version: str
    kind: EvidenceKind
    window_start: datetime | None
    window_end: datetime | None
    observations: int
    metrics: dict[str, Any]
    enough_evidence: bool
    deterioration: str
    reason: str
    regimes: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "kind": self.kind.value,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "observations": self.observations,
            "metrics": dict(self.metrics),
            "enough_evidence": self.enough_evidence,
            "deterioration": self.deterioration,
            "reason": self.reason,
            "regimes": list(self.regimes),
            "evidence_refs": list(self.evidence_refs),
            "created_at": self.created_at.isoformat(),
            "hypothetical": self.kind is not EvidenceKind.PAPER,
            "is_live_performance": False,
        }


@dataclass(frozen=True, slots=True)
class LearningAlert:
    alert_id: str
    strategy_id: str
    kind: AlertKind
    severity: AlertSeverity
    message: str
    metrics: dict[str, Any]
    evidence_refs: tuple[str, ...]
    status: str
    created_at: datetime
    acknowledged_at: datetime | None = None
    acknowledged_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "strategy_id": self.strategy_id,
            "kind": self.kind.value,
            "severity": self.severity.value,
            "message": self.message,
            "metrics": dict(self.metrics),
            "evidence_refs": list(self.evidence_refs),
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "acknowledged_at": (
                self.acknowledged_at.isoformat() if self.acknowledged_at else None
            ),
            "acknowledged_by": self.acknowledged_by,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LearningAlert":
        return cls(
            alert_id=str(payload["alert_id"]),
            strategy_id=str(payload.get("strategy_id") or ""),
            kind=AlertKind(str(payload.get("kind") or "performance_deterioration")),
            severity=AlertSeverity(str(payload.get("severity") or "warning")),
            message=str(payload.get("message") or ""),
            metrics=dict(payload.get("metrics") or {}),
            evidence_refs=tuple(str(item) for item in payload.get("evidence_refs") or ()),
            status=str(payload.get("status") or "open"),
            created_at=parse_time(payload["created_at"]),
            acknowledged_at=optional_time(payload.get("acknowledged_at")),
            acknowledged_by=str(payload.get("acknowledged_by") or ""),
        )


@dataclass(frozen=True, slots=True)
class StrategyVersionRecord:
    """One immutable configuration version produced by learning."""

    version_id: str
    strategy_id: str
    catalog_id: str
    parent_version: str
    config: dict[str, Any]
    created_at: datetime
    description: str
    experiment_id: str
    candidate_id: str
    evidence_refs: tuple[str, ...]
    evaluation: dict[str, Any]
    approval: dict[str, Any]
    promotion: dict[str, Any]
    status: VersionStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "strategy_id": self.strategy_id,
            "catalog_id": self.catalog_id,
            "parent_version": self.parent_version,
            "config": dict(self.config),
            "created_at": self.created_at.isoformat(),
            "description": self.description,
            "experiment_id": self.experiment_id,
            "candidate_id": self.candidate_id,
            "evidence_refs": list(self.evidence_refs),
            "evaluation": dict(self.evaluation),
            "approval": dict(self.approval),
            "promotion": dict(self.promotion),
            "status": self.status.value,
            "immutable": True,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StrategyVersionRecord":
        return cls(
            version_id=str(payload["version_id"]),
            strategy_id=str(payload.get("strategy_id") or ""),
            catalog_id=str(payload.get("catalog_id") or ""),
            parent_version=str(payload.get("parent_version") or ""),
            config=dict(payload.get("config") or {}),
            created_at=parse_time(payload["created_at"]),
            description=str(payload.get("description") or ""),
            experiment_id=str(payload.get("experiment_id") or ""),
            candidate_id=str(payload.get("candidate_id") or ""),
            evidence_refs=tuple(str(item) for item in payload.get("evidence_refs") or ()),
            evaluation=dict(payload.get("evaluation") or {}),
            approval=dict(payload.get("approval") or {}),
            promotion=dict(payload.get("promotion") or {}),
            status=VersionStatus(str(payload.get("status") or "promoted")),
        )


@dataclass(frozen=True, slots=True)
class LearningCandidate:
    candidate_id: str
    experiment_id: str
    strategy_id: str
    catalog_id: str
    baseline_version: str
    baseline_config: dict[str, Any]
    config: dict[str, Any]
    config_digest: str
    objective: str
    status: CandidateStatus
    evaluation: dict[str, Any]
    risk: dict[str, Any]
    policy: dict[str, Any]
    shadow_validation: dict[str, Any]
    approval: dict[str, Any]
    promoted_version: str
    reasons: tuple[str, ...]
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "experiment_id": self.experiment_id,
            "strategy_id": self.strategy_id,
            "catalog_id": self.catalog_id,
            "baseline_version": self.baseline_version,
            "baseline_config": dict(self.baseline_config),
            "config": dict(self.config),
            "config_digest": self.config_digest,
            "objective": self.objective,
            "status": self.status.value,
            "evaluation": dict(self.evaluation),
            "risk": dict(self.risk),
            "policy": dict(self.policy),
            "shadow_validation": dict(self.shadow_validation),
            "approval": dict(self.approval),
            "promoted_version": self.promoted_version,
            "reasons": list(self.reasons),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "is_active": False,
            "executes_trades": False,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LearningCandidate":
        return cls(
            candidate_id=str(payload["candidate_id"]),
            experiment_id=str(payload.get("experiment_id") or ""),
            strategy_id=str(payload.get("strategy_id") or ""),
            catalog_id=str(payload.get("catalog_id") or ""),
            baseline_version=str(payload.get("baseline_version") or ""),
            baseline_config=dict(payload.get("baseline_config") or {}),
            config=dict(payload.get("config") or {}),
            config_digest=str(payload.get("config_digest") or ""),
            objective=str(payload.get("objective") or "risk_adjusted_return"),
            status=CandidateStatus(str(payload.get("status") or "generated")),
            evaluation=dict(payload.get("evaluation") or {}),
            risk=dict(payload.get("risk") or {}),
            policy=dict(payload.get("policy") or {}),
            shadow_validation=dict(payload.get("shadow_validation") or {}),
            approval=dict(payload.get("approval") or {}),
            promoted_version=str(payload.get("promoted_version") or ""),
            reasons=tuple(str(item) for item in payload.get("reasons") or ()),
            created_at=parse_time(payload["created_at"]),
            updated_at=parse_time(payload["updated_at"]),
        )


@dataclass(frozen=True, slots=True)
class LearningExperiment:
    experiment_id: str
    strategy_id: str
    catalog_id: str
    baseline_version: str
    baseline_config: dict[str, Any]
    objective: str
    method: str
    seed: int
    requested_candidates: int
    candidate_configs: tuple[dict[str, Any], ...]
    evidence_refs: tuple[str, ...]
    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime
    instruments: tuple[str, ...]
    baseline_snapshot: dict[str, Any]
    approval_policy: dict[str, Any]
    min_trades: int
    min_observations: int
    status: ExperimentStatus
    progress: int
    results: dict[str, Any]
    candidate_ids: tuple[str, ...]
    error: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "strategy_id": self.strategy_id,
            "catalog_id": self.catalog_id,
            "baseline_version": self.baseline_version,
            "baseline_config": dict(self.baseline_config),
            "objective": self.objective,
            "method": self.method,
            "seed": self.seed,
            "requested_candidates": self.requested_candidates,
            "candidate_configs": [dict(row) for row in self.candidate_configs],
            "evidence_refs": list(self.evidence_refs),
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "validation_start": self.validation_start.isoformat(),
            "validation_end": self.validation_end.isoformat(),
            "instruments": list(self.instruments),
            "baseline_snapshot": dict(self.baseline_snapshot),
            "approval_policy": dict(self.approval_policy),
            "min_trades": self.min_trades,
            "min_observations": self.min_observations,
            "status": self.status.value,
            "progress": self.progress,
            "results": dict(self.results),
            "candidate_ids": list(self.candidate_ids),
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
            "idempotency_key": self.idempotency_key,
            "is_live_trading": False,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LearningExperiment":
        return cls(
            experiment_id=str(payload["experiment_id"]),
            strategy_id=str(payload.get("strategy_id") or ""),
            catalog_id=str(payload.get("catalog_id") or ""),
            baseline_version=str(payload.get("baseline_version") or ""),
            baseline_config=dict(payload.get("baseline_config") or {}),
            objective=str(payload.get("objective") or "risk_adjusted_return"),
            method=str(payload.get("method") or "grid"),
            seed=int(payload.get("seed") or 42),
            requested_candidates=int(payload.get("requested_candidates") or 0),
            candidate_configs=tuple(
                dict(row) for row in payload.get("candidate_configs") or ()
            ),
            evidence_refs=tuple(str(item) for item in payload.get("evidence_refs") or ()),
            train_start=parse_time(payload["train_start"]),
            train_end=parse_time(payload["train_end"]),
            validation_start=parse_time(payload["validation_start"]),
            validation_end=parse_time(payload["validation_end"]),
            instruments=tuple(str(item) for item in payload.get("instruments") or ()),
            baseline_snapshot=dict(payload.get("baseline_snapshot") or {}),
            approval_policy=dict(payload.get("approval_policy") or {}),
            min_trades=int(payload.get("min_trades") or 0),
            min_observations=int(payload.get("min_observations") or 0),
            status=ExperimentStatus(str(payload.get("status") or "created")),
            progress=int(payload.get("progress") or 0),
            results=dict(payload.get("results") or {}),
            candidate_ids=tuple(str(item) for item in payload.get("candidate_ids") or ()),
            error=str(payload.get("error") or ""),
            created_at=parse_time(payload["created_at"]),
            started_at=optional_time(payload.get("started_at")),
            completed_at=optional_time(payload.get("completed_at")),
            cancelled_at=optional_time(payload.get("cancelled_at")),
            idempotency_key=str(payload.get("idempotency_key") or ""),
        )


@dataclass(frozen=True, slots=True)
class RollbackRecord:
    rollback_id: str
    strategy_id: str
    from_version: str
    to_version: str
    reason: str
    actor: str
    at: datetime
    applied: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "rollback_id": self.rollback_id,
            "strategy_id": self.strategy_id,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "reason": self.reason,
            "actor": self.actor,
            "at": self.at.isoformat(),
            "applied": self.applied,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RollbackRecord":
        return cls(
            rollback_id=str(payload["rollback_id"]),
            strategy_id=str(payload.get("strategy_id") or ""),
            from_version=str(payload.get("from_version") or ""),
            to_version=str(payload.get("to_version") or ""),
            reason=str(payload.get("reason") or ""),
            actor=str(payload.get("actor") or ""),
            at=parse_time(payload["at"]),
            applied=bool(payload.get("applied")),
        )


__all__ = [
    "AlertKind",
    "AlertSeverity",
    "CANDIDATE_TRANSITIONS",
    "CandidateStatus",
    "EXPERIMENT_TRANSITIONS",
    "EvidenceKind",
    "EvidenceRecord",
    "EvidenceStatus",
    "ExperimentStatus",
    "LearningAlert",
    "LearningCandidate",
    "LearningExperiment",
    "PerformanceSnapshot",
    "RollbackRecord",
    "ShadowValidationStatus",
    "StrategyVersionRecord",
    "VersionStatus",
    "decimal",
    "digest",
    "ensure_candidate_transition",
    "ensure_experiment_transition",
    "learning_id",
    "optional_time",
    "parse_time",
    "text",
    "utcnow",
]
