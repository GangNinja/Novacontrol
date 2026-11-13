"""AT-03 decision and risk types.

AT-03 turns an NM-09 signal into a proposed intent and sends that intent to the
independent NM-08 risk engine.  Nothing in this module is an order, a fill or a
portfolio mutation; an approved proposal is explicitly non-executable here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

DECISION_CONTRACT_VERSION = "at03-decision-1.0"
PAPER_ONLY = True
LIVE_TRADING = False
BROKER_CONNECTED = False
EXECUTION_IMPLEMENTED = False
MAX_INSTRUMENTS_PER_RUN = 20
MAX_STRATEGIES_PER_RUN = 10
RISK_TIMEOUT_SECONDS = 30.0
PROPOSAL_TTL_HOURS = 2


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def decision_id(prefix: str, *parts: object) -> str:
    seed = "|".join(str(part) for part in parts)
    return prefix + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def digest_payload(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


class DecisionRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DecisionProposalStatus(StrEnum):
    CREATED = "created"
    VALIDATION_PENDING = "validation_pending"
    INVALID = "invalid"
    RISK_PENDING = "risk_pending"
    RISK_APPROVED = "risk_approved"
    RISK_REJECTED = "risk_rejected"
    RISK_RESTRICTED = "risk_restricted"
    RISK_REVIEW = "risk_review"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class EvaluationOutcome(StrEnum):
    SIGNAL = "signal"
    NO_ACTION = "no_action"
    INVALID = "invalid"
    FAILED = "failed"
    SKIPPED = "skipped"


class SignalValidation(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    NO_ACTION = "no_action"


class DecisionErrorCode(StrEnum):
    RUNTIME_DISABLED = "RUNTIME_DISABLED"
    RUNTIME_PAUSED = "RUNTIME_PAUSED"
    LEASE_INVALID = "LEASE_INVALID"
    MONITORING_CYCLE_REQUIRED = "MONITORING_CYCLE_REQUIRED"
    MONITORING_CYCLE_NOT_ELIGIBLE = "MONITORING_CYCLE_NOT_ELIGIBLE"
    MONITORING_CYCLE_INCOMPLETE = "MONITORING_CYCLE_INCOMPLETE"
    NO_ELIGIBLE_CANDIDATES = "NO_ELIGIBLE_CANDIDATES"
    NO_ACTIVE_STRATEGY = "NO_ACTIVE_STRATEGY"
    NO_PORTFOLIO_CONFIGURED = "NO_PORTFOLIO_CONFIGURED"
    STRATEGY_UNAVAILABLE = "STRATEGY_UNAVAILABLE"
    STRATEGY_EVALUATION_FAILED = "STRATEGY_EVALUATION_FAILED"
    INVALID_SIGNAL = "INVALID_SIGNAL"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    MISSING_REFERENCE_PRICE = "MISSING_REFERENCE_PRICE"
    PROPOSAL_VALIDATION_FAILED = "PROPOSAL_VALIDATION_FAILED"
    PORTFOLIO_INELIGIBLE = "PORTFOLIO_INELIGIBLE"
    INSTRUMENT_INELIGIBLE = "INSTRUMENT_INELIGIBLE"
    HORIZON_MISMATCH = "HORIZON_MISMATCH"
    RISK_ENGINE_UNAVAILABLE = "RISK_ENGINE_UNAVAILABLE"
    RISK_ASSESSMENT_FAILED = "RISK_ASSESSMENT_FAILED"
    RISK_REJECTED = "RISK_REJECTED"
    PIPELINE_FAILED = "PIPELINE_FAILED"
    DUPLICATE_PROPOSAL = "DUPLICATE_PROPOSAL"
    CYCLE_ALREADY_ACTIVE = "CYCLE_ALREADY_ACTIVE"
    ALREADY_RUNNING = "ALREADY_RUNNING"
    INVALID_REQUEST = "INVALID_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    CYCLE_CANCELLED = "CYCLE_CANCELLED"


class DecisionError(RuntimeError):
    """Base class for AT-03 refusals and failures."""

    def __init__(self, code: str, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class DecisionNotFound(DecisionError):
    def __init__(self, message: str) -> None:
        super().__init__(DecisionErrorCode.NOT_FOUND.value, message, status=404)


class DecisionStateError(DecisionError):
    pass


class DecisionValidationError(DecisionError):
    def __init__(self, message: str) -> None:
        super().__init__(DecisionErrorCode.INVALID_REQUEST.value, message, status=422)


#: Proposal lifecycle.  A terminal risk state never becomes executable here.
PROPOSAL_TRANSITIONS: dict[DecisionProposalStatus, frozenset[DecisionProposalStatus]] = {
    DecisionProposalStatus.CREATED: frozenset(
        {DecisionProposalStatus.VALIDATION_PENDING, DecisionProposalStatus.CANCELLED}
    ),
    DecisionProposalStatus.VALIDATION_PENDING: frozenset(
        {
            DecisionProposalStatus.INVALID,
            DecisionProposalStatus.RISK_PENDING,
            DecisionProposalStatus.CANCELLED,
        }
    ),
    DecisionProposalStatus.RISK_PENDING: frozenset(
        {
            DecisionProposalStatus.RISK_APPROVED,
            DecisionProposalStatus.RISK_REJECTED,
            DecisionProposalStatus.RISK_RESTRICTED,
            DecisionProposalStatus.RISK_REVIEW,
            DecisionProposalStatus.CANCELLED,
        }
    ),
    DecisionProposalStatus.RISK_APPROVED: frozenset(
        {DecisionProposalStatus.EXPIRED, DecisionProposalStatus.CANCELLED}
    ),
    DecisionProposalStatus.RISK_REJECTED: frozenset(
        {DecisionProposalStatus.EXPIRED, DecisionProposalStatus.CANCELLED}
    ),
    DecisionProposalStatus.RISK_RESTRICTED: frozenset(
        {DecisionProposalStatus.EXPIRED, DecisionProposalStatus.CANCELLED}
    ),
    DecisionProposalStatus.RISK_REVIEW: frozenset(
        {DecisionProposalStatus.EXPIRED, DecisionProposalStatus.CANCELLED}
    ),
    DecisionProposalStatus.INVALID: frozenset(),
    DecisionProposalStatus.EXPIRED: frozenset(),
    DecisionProposalStatus.CANCELLED: frozenset(),
}


def ensure_proposal_transition(
    current: DecisionProposalStatus | str, target: DecisionProposalStatus | str
) -> tuple[DecisionProposalStatus, DecisionProposalStatus]:
    source = DecisionProposalStatus(current)
    destination = DecisionProposalStatus(target)
    if source is destination:
        return source, destination
    if destination not in PROPOSAL_TRANSITIONS[source]:
        raise DecisionStateError(
            DecisionErrorCode.INVALID_REQUEST.value,
            f"proposal cannot move from {source.value} to {destination.value}",
        )
    return source, destination


@dataclass(frozen=True, slots=True)
class DecisionRun:
    run_id: str
    selection_key: str
    monitoring_cycle_id: str
    mode: str
    status: DecisionRunStatus
    trigger: str
    portfolio_ids: tuple[str, ...]
    strategy_ids: tuple[str, ...]
    instrument_ids: tuple[str, ...]
    evaluations: int
    signals: int
    proposals: int
    approved: int
    rejected: int
    invalid: int
    errors: int
    started_at: datetime
    finished_at: datetime | None
    error_code: str
    error: str
    actor: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "selection_key": self.selection_key,
            "monitoring_cycle_id": self.monitoring_cycle_id,
            "mode": self.mode,
            "status": self.status.value,
            "trigger": self.trigger,
            "portfolio_ids": list(self.portfolio_ids),
            "strategy_ids": list(self.strategy_ids),
            "instrument_ids": list(self.instrument_ids),
            "evaluations": self.evaluations,
            "signals": self.signals,
            "proposals": self.proposals,
            "approved": self.approved,
            "rejected": self.rejected,
            "invalid": self.invalid,
            "errors": self.errors,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error_code": self.error_code,
            "error": self.error,
            "actor": self.actor,
            "paper_only": PAPER_ONLY,
            "live_trading": LIVE_TRADING,
            "broker_connected": BROKER_CONNECTED,
            "execution_implemented": EXECUTION_IMPLEMENTED,
            "orders_possible": False,
            "is_decision_run": True,
        }


@dataclass(frozen=True, slots=True)
class DecisionEvaluation:
    evaluation_id: str
    evaluation_key: str
    run_id: str
    monitoring_cycle_id: str
    strategy_id: str
    strategy_version: str
    config_digest: str
    portfolio_id: str
    instrument_id: str
    exchange: str
    horizon: str
    market_data_cutoff: str
    data_quality: str
    outcome: EvaluationOutcome
    signal_id: str
    explanation: str
    conditions: tuple[str, ...]
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "run_id": self.run_id,
            "monitoring_cycle_id": self.monitoring_cycle_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "config_digest": self.config_digest,
            "portfolio_id": self.portfolio_id,
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "horizon": self.horizon,
            "market_data_cutoff": self.market_data_cutoff,
            "data_quality": self.data_quality,
            "outcome": self.outcome.value,
            "signal_id": self.signal_id,
            "explanation": self.explanation,
            "conditions": list(self.conditions),
            "created_at": self.created_at.isoformat(),
            "is_evaluation": True,
        }


@dataclass(frozen=True, slots=True)
class DecisionSignal:
    signal_id: str
    evaluation_id: str
    run_id: str
    monitoring_cycle_id: str
    strategy_id: str
    strategy_version: str
    portfolio_id: str
    instrument_id: str
    exchange: str
    horizon: str
    direction: str
    created_at: datetime
    source_market_data_at: datetime
    expires_at: datetime
    rationale: str
    confidence: str | None
    reference_price: str | None
    data_quality: str
    freshness: str
    validation: SignalValidation
    validation_error: str
    conditions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "evaluation_id": self.evaluation_id,
            "run_id": self.run_id,
            "monitoring_cycle_id": self.monitoring_cycle_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "portfolio_id": self.portfolio_id,
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "horizon": self.horizon,
            "direction": self.direction,
            "created_at": self.created_at.isoformat(),
            "source_market_data_at": self.source_market_data_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "rationale": self.rationale,
            "confidence": self.confidence,
            "reference_price": self.reference_price,
            "data_quality": self.data_quality,
            "freshness": self.freshness,
            "validation": self.validation.value,
            "validation_error": self.validation_error,
            "conditions": list(self.conditions),
            "is_signal": True,
            "is_order": False,
        }


@dataclass(frozen=True, slots=True)
class DecisionProposal:
    proposal_id: str
    run_id: str
    signal_id: str
    monitoring_cycle_id: str
    strategy_id: str
    strategy_version: str
    portfolio_id: str
    instrument_id: str
    exchange: str
    horizon: str
    action: str
    side: str
    notional: Decimal
    reference_price: Decimal | None
    price_at: datetime | None
    created_at: datetime
    expires_at: datetime
    reason: str
    status: DecisionProposalStatus
    data_quality: str
    source_market_data_at: datetime | None
    risk_decision_id: str = ""
    risk_outcome: str = ""
    risk_policy_id: str = ""
    risk_policy_version: str = ""
    risk_state_version: str = ""
    risk_restrictions: tuple[str, ...] = ()
    risk_error: str = ""
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "run_id": self.run_id,
            "signal_id": self.signal_id,
            "monitoring_cycle_id": self.monitoring_cycle_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "portfolio_id": self.portfolio_id,
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "horizon": self.horizon,
            "action": self.action,
            "side": self.side,
            "notional": str(self.notional),
            "reference_price": (
                None if self.reference_price is None else str(self.reference_price)
            ),
            "price_at": self.price_at.isoformat() if self.price_at else None,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "reason": self.reason,
            "status": self.status.value,
            "data_quality": self.data_quality,
            "source_market_data_at": (
                self.source_market_data_at.isoformat()
                if self.source_market_data_at
                else None
            ),
            "risk_decision_id": self.risk_decision_id,
            "risk_outcome": self.risk_outcome,
            "risk_policy_id": self.risk_policy_id,
            "risk_policy_version": self.risk_policy_version,
            "risk_state_version": self.risk_state_version,
            "risk_restrictions": list(self.risk_restrictions),
            "risk_error": self.risk_error,
            "updated_at": (self.updated_at or self.created_at).isoformat(),
            "execution_allowed": False,
            "is_executed": False,
            "execution_order_id": "",
            "orders_possible": False,
            "is_proposal": True,
        }


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    proposal_id: str
    risk_decision_id: str
    engine: str
    engine_version: str
    outcome: str
    assessed_at: datetime
    policy_id: str
    policy_version: str
    state_version: str
    data_quality: str
    metrics: dict[str, Any] = field(default_factory=dict)
    violations: tuple[dict[str, Any], ...] = ()
    warnings: tuple[dict[str, Any], ...] = ()
    restrictions: tuple[str, ...] = ()
    explanation: str = ""
    references: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "risk_decision_id": self.risk_decision_id,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "outcome": self.outcome,
            "assessed_at": self.assessed_at.isoformat(),
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "state_version": self.state_version,
            "data_quality": self.data_quality,
            "metrics": dict(self.metrics),
            "violations": [dict(item) for item in self.violations],
            "warnings": [dict(item) for item in self.warnings],
            "restrictions": list(self.restrictions),
            "explanation": self.explanation,
            "references": list(self.references),
            "is_risk_decision": True,
            "is_order": False,
            "raw": dict(self.raw),
        }


@dataclass(frozen=True, slots=True)
class DecisionEvent:
    event_id: str
    at: datetime
    run_id: str
    proposal_id: str
    kind: str
    message: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "at": self.at.isoformat(),
            "run_id": self.run_id,
            "proposal_id": self.proposal_id,
            "kind": self.kind,
            "message": self.message,
            "payload": dict(self.payload),
        }


__all__ = [
    "BROKER_CONNECTED",
    "DECISION_CONTRACT_VERSION",
    "DecisionError",
    "DecisionErrorCode",
    "DecisionEvaluation",
    "DecisionEvent",
    "DecisionNotFound",
    "DecisionProposal",
    "DecisionProposalStatus",
    "DecisionRun",
    "DecisionRunStatus",
    "DecisionSignal",
    "DecisionStateError",
    "DecisionValidationError",
    "EvaluationOutcome",
    "EXECUTION_IMPLEMENTED",
    "LIVE_TRADING",
    "MAX_INSTRUMENTS_PER_RUN",
    "MAX_STRATEGIES_PER_RUN",
    "PAPER_ONLY",
    "PROPOSAL_TTL_HOURS",
    "PROPOSAL_TRANSITIONS",
    "RISK_TIMEOUT_SECONDS",
    "RiskAssessment",
    "SignalValidation",
    "decision_id",
    "digest_payload",
    "ensure_proposal_transition",
    "utcnow",
]
