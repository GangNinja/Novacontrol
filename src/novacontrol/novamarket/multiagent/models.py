"""PI-17 multi-agent financial-intelligence contracts.

The package is a research coordinator.  It reads authoritative evidence and
produces advisory recommendations; it has no portfolio, ledger, risk-approval,
paper-order or autonomy-mode handle.  NM-08 and NM-10 remain authoritative.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

MULTI_AGENT_PHASE = "PI-17"
MULTI_AGENT_CONTRACT_VERSION = "pi17-multi-agent-1.0"
MULTI_AGENT_SCHEMA_VERSION = 1
MAX_CYCLES = 2_000
MAX_MESSAGES = 50_000
MAX_EVIDENCE = 50_000
MAX_AGENT_RUNS = 50_000
MAX_TEXT = 2_000
MAX_CONCURRENCY = 6
MAX_AGENTS_PER_CYCLE = 9
MAX_AGENT_TIMEOUT_SECONDS = 120
DEFAULT_AGENT_TIMEOUT_SECONDS = 20
DEFAULT_CYCLE_TIMEOUT_SECONDS = 90
MAX_EVIDENCE_PER_RESPONSE = 100
MAX_CYCLE_AGE_DAYS = 365


class MultiAgentError(ValueError):
    """A PI-17 request is invalid."""


class MultiAgentValidationError(MultiAgentError):
    """A configuration, message or evidence payload failed validation."""


class MultiAgentNotFoundError(LookupError):
    """A requested PI-17 record does not exist."""


class MultiAgentStateError(RuntimeError):
    """The record state does not permit the requested operation."""


class MultiAgentResourceError(RuntimeError):
    """A bounded PI-17 limit would be exceeded."""


class MultiAgentStoreError(RuntimeError):
    """The isolated PI-17 store cannot complete a request."""


class FinancialAgentRole(StrEnum):
    MARKET_RESEARCH = "market_research"
    TECHNICAL_ANALYSIS = "technical_analysis"
    FUNDAMENTAL_ANALYSIS = "fundamental_analysis"
    PREDICTION_INTELLIGENCE = "prediction_intelligence"
    MARKET_REGIME = "market_regime"
    RISK_ANALYSIS = "risk_analysis"
    PORTFOLIO_INTELLIGENCE = "portfolio_intelligence"
    RL_POLICY_INTELLIGENCE = "rl_policy_intelligence"
    TRADING_DECISION = "trading_decision"


class AgentHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


class AgentRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    FAILED = "failed"
    TIMEOUT = "timeout"
    INVALID = "invalid"
    SKIPPED = "skipped"


class CycleStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    DEGRADED = "degraded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RecommendationAction(StrEnum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    WAIT = "wait"
    NO_ACTION = "no_action"
    ABSTAIN = "abstain"


class EvidenceKind(StrEnum):
    VERIFIED = "verified"
    DERIVED = "derived"
    AGENT_INTERPRETATION = "agent_interpretation"
    HYPOTHESIS = "hypothesis"
    UNVERIFIED = "unverified"
    MISSING = "missing"


class Freshness(StrEnum):
    FRESH = "fresh"
    DELAYED = "delayed"
    STALE = "stale"
    UNKNOWN = "unknown"


class ValidationStatus(StrEnum):
    VALID = "valid"
    PARTIAL = "partial"
    INVALID = "invalid"
    UNVERIFIED = "unverified"


class MessageType(StrEnum):
    TASK_ASSIGNMENT = "task_assignment"
    AGENT_RESPONSE = "agent_response"
    EVIDENCE_SHARE = "evidence_share"
    CLARIFICATION_REQUEST = "clarification_request"
    CONFLICT_NOTIFICATION = "conflict_notification"
    TIMEOUT_NOTIFICATION = "timeout_notification"
    FAILURE_NOTIFICATION = "failure_notification"


class ConflictSeverity(StrEnum):
    INFO = "info"
    MATERIAL = "material"
    AUTHORITATIVE = "authoritative"


class ConflictKind(StrEnum):
    DIRECTION = "direction"
    STALE_DATA = "stale_data"
    FORECAST_DISAGREEMENT = "forecast_disagreement"
    HIGH_UNCERTAINTY = "high_uncertainty"
    RISK_RESTRICTION = "risk_restriction"
    PORTFOLIO_CONSTRAINT = "portfolio_constraint"
    RL_CONFLICT = "rl_conflict"
    MISSING_EVIDENCE = "missing_evidence"
    AGENT_FAILURE = "agent_failure"


#: Explicit read capabilities.  No write, execution or authority capability is
#: representable here, so configuration cannot expand an agent into one.
READ_CAPABILITIES = frozenset(
    {
        "market.read",
        "features.read",
        "fundamentals.read",
        "news.read",
        "forecasts.read",
        "regime.read",
        "risk.read",
        "portfolio.read",
        "rl.read",
        "adaptive.read",
        "evaluation.read",
        "autonomy.read",
    }
)
FORBIDDEN_CAPABILITY_MARKERS = (
    "write",
    "mutate",
    "execute",
    "order",
    "trade",
    "ledger",
    "broker",
    "activate",
    "promote",
    "override",
)

HORIZON_ALIASES = {"1h": "60m", "one_day": "1d", "one_week": "5d"}
HORIZON_GROUPS: dict[str, str] = {
    "5m": "intraday",
    "15m": "intraday",
    "20m": "intraday",
    "60m": "intraday",
    "1d": "swing",
    "3d": "swing",
    "5d": "swing",
    "30d": "long_term",
    "90d": "long_term",
}
SUPPORTED_HORIZONS = tuple(HORIZON_GROUPS)


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_time(value: object, name: str = "timestamp") -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif value in (None, ""):
        raise MultiAgentValidationError(f"{name} is required")
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise MultiAgentValidationError(f"{name} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MultiAgentValidationError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def optional_time(value: object, name: str = "timestamp") -> datetime | None:
    return None if value in (None, "") else parse_time(value, name)


def text(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip() or default


def canonical_horizon(value: object) -> str:
    raw = text(value).lower()
    raw = HORIZON_ALIASES.get(raw, raw)
    if raw not in HORIZON_GROUPS:
        raise MultiAgentValidationError(
            "horizon must be one of: " + ", ".join(SUPPORTED_HORIZONS)
        )
    return raw


def finite(
    value: object,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool):
        raise MultiAgentValidationError(f"{name} must be a finite number")
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise MultiAgentValidationError(f"{name} must be a finite number") from exc
    if result != result or result in (float("inf"), float("-inf")):
        raise MultiAgentValidationError(f"{name} must be a finite number")
    if minimum is not None and result < minimum:
        raise MultiAgentValidationError(f"{name} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise MultiAgentValidationError(f"{name} must be at most {maximum}")
    return result


def whole(
    value: object,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = 1_000_000,
) -> int:
    if isinstance(value, bool):
        raise MultiAgentValidationError(f"{name} must be an integer")
    try:
        if isinstance(value, (str, bytes, bytearray, int, float)):
            result = int(value)
        else:
            raise TypeError
    except (TypeError, ValueError) as exc:
        raise MultiAgentValidationError(f"{name} must be an integer") from exc
    if str(value).strip() not in {str(result), f"{result}.0"} and not isinstance(
        value, int
    ):
        raise MultiAgentValidationError(f"{name} must be an integer")
    if result < minimum or result > maximum:
        raise MultiAgentValidationError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return result


def sequence(value: object) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    raise MultiAgentValidationError("expected a list")


def stable_id(prefix: str, *parts: object) -> str:
    seed = "|".join(str(part) for part in parts)
    return prefix + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def validate_capability(capability: str) -> str:
    name = text(capability).lower()
    if not name:
        raise MultiAgentValidationError("capability name is required")
    if any(marker in name for marker in FORBIDDEN_CAPABILITY_MARKERS):
        raise MultiAgentValidationError(
            f"capability {name!r} would grant a financial mutation or authority"
        )
    if name not in READ_CAPABILITIES:
        raise MultiAgentValidationError(f"unknown read capability {name!r}")
    return name


def freshness_for(
    available_at: datetime | None,
    *,
    now: datetime,
    fresh_seconds: int = 3_600,
    delayed_seconds: int = 86_400,
) -> Freshness:
    if available_at is None:
        return Freshness.UNKNOWN
    age = max(0.0, (now - available_at).total_seconds())
    if age <= fresh_seconds:
        return Freshness.FRESH
    if age <= delayed_seconds:
        return Freshness.DELAYED
    return Freshness.STALE


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    agent_id: str
    name: str
    role: FinancialAgentRole
    responsibilities: tuple[str, ...]
    input_contract: str
    output_contract: str
    allowed_capabilities: tuple[str, ...]
    timeout_seconds: int
    required: bool = True
    provider: str = "deterministic-local"
    model_version: str = "pi17-rules-1.0"
    config_version: str = "1"

    def __post_init__(self) -> None:
        if not self.agent_id.strip() or not self.name.strip():
            raise MultiAgentValidationError("agent identity and name are required")
        if not self.responsibilities:
            raise MultiAgentValidationError("an agent needs declared responsibilities")
        for capability in self.allowed_capabilities:
            validate_capability(capability)
        if not 0 < self.timeout_seconds <= MAX_AGENT_TIMEOUT_SECONDS:
            raise MultiAgentValidationError(
                f"timeout_seconds must be within [1, {MAX_AGENT_TIMEOUT_SECONDS}]"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "role": self.role.value,
            "responsibilities": list(self.responsibilities),
            "input_contract": self.input_contract,
            "output_contract": self.output_contract,
            "allowed_tools": list(self.allowed_capabilities),
            "allowed_capabilities": list(self.allowed_capabilities),
            "permissions": {
                "financial_mutation": False,
                "risk_approval": False,
                "order_submission": False,
                "autonomy_control": False,
                "policy_activation": False,
            },
            "timeout_seconds": self.timeout_seconds,
            "required": self.required,
            "provider": self.provider,
            "model_version": self.model_version,
            "config_version": self.config_version,
        }


@dataclass(frozen=True, slots=True)
class AgentConfiguration:
    agent_id: str
    enabled: bool = True
    timeout_seconds: int = DEFAULT_AGENT_TIMEOUT_SECONDS
    provider: str = "deterministic-local"
    model_version: str = "pi17-rules-1.0"
    allowed_capabilities: tuple[str, ...] = ()
    version: int = 1
    updated_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not self.agent_id.strip():
            raise MultiAgentValidationError("agent_id is required")
        if not 1 <= int(self.timeout_seconds) <= MAX_AGENT_TIMEOUT_SECONDS:
            raise MultiAgentValidationError(
                f"timeout_seconds must be within [1, {MAX_AGENT_TIMEOUT_SECONDS}]"
            )
        for capability in self.allowed_capabilities:
            validate_capability(capability)
        if self.version < 1:
            raise MultiAgentValidationError("configuration version must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "enabled": self.enabled,
            "timeout_seconds": self.timeout_seconds,
            "provider": self.provider,
            "model_version": self.model_version,
            "allowed_capabilities": list(self.allowed_capabilities),
            "version": self.version,
            "updated_at": self.updated_at.isoformat(),
            "financial_mutation": False,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AgentConfiguration:
        return cls(
            agent_id=text(payload.get("agent_id")),
            enabled=bool(payload.get("enabled", True)),
            timeout_seconds=whole(
                payload.get("timeout_seconds", DEFAULT_AGENT_TIMEOUT_SECONDS),
                "timeout_seconds",
                minimum=1,
                maximum=MAX_AGENT_TIMEOUT_SECONDS,
            ),
            provider=text(payload.get("provider"), "deterministic-local"),
            model_version=text(payload.get("model_version"), "pi17-rules-1.0"),
            allowed_capabilities=tuple(
                validate_capability(str(item))
                for item in sequence(payload.get("allowed_capabilities"))
            ),
            version=whole(payload.get("version", 1), "version", minimum=1),
            updated_at=optional_time(payload.get("updated_at"))
            or datetime.now(UTC),
        )


@dataclass(frozen=True, slots=True)
class AgentMessage:
    message_id: str
    cycle_id: str
    sender_agent_id: str
    recipient_agent_id: str
    task_id: str
    message_type: MessageType
    instrument_id: str
    exchange: str
    payload: Mapping[str, Any]
    created_at: datetime
    expires_at: datetime
    correlation_id: str
    config_version: str = "1"
    source_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for value, name in (
            (self.message_id, "message_id"),
            (self.cycle_id, "cycle_id"),
            (self.sender_agent_id, "sender_agent_id"),
            (self.recipient_agent_id, "recipient_agent_id"),
            (self.task_id, "task_id"),
        ):
            if not text(value):
                raise MultiAgentValidationError(f"{name} is required")
        if self.created_at > self.expires_at:
            raise MultiAgentValidationError("a message cannot expire before it is created")
        if not isinstance(self.payload, Mapping):
            raise MultiAgentValidationError("message payload must be an object")

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "cycle_id": self.cycle_id,
            "sender_agent_id": self.sender_agent_id,
            "recipient_agent_id": self.recipient_agent_id,
            "task_id": self.task_id,
            "message_type": self.message_type.value,
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "payload": dict(self.payload),
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "correlation_id": self.correlation_id,
            "config_version": self.config_version,
            "source_references": list(self.source_references),
        }


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_id: str
    cycle_id: str
    agent_id: str
    kind: EvidenceKind
    source_identity: str
    source_reference: str
    instrument_id: str
    exchange: str
    summary: str
    value: Mapping[str, Any]
    original_at: datetime | None
    retrieved_at: datetime
    published_at: datetime | None = None
    available_at: datetime | None = None
    data_version: str = ""
    forecast_version: str = ""
    model_version: str = ""
    feature_set_version: str = ""
    freshness: Freshness = Freshness.UNKNOWN
    validation: ValidationStatus = ValidationStatus.UNVERIFIED
    independent: bool = True
    correlation_group: str = ""

    def __post_init__(self) -> None:
        if not text(self.evidence_id) or not text(self.cycle_id):
            raise MultiAgentValidationError("evidence and cycle identities are required")
        if self.available_at and self.available_at > self.retrieved_at:
            raise MultiAgentValidationError(
                "evidence cannot be available after it was retrieved"
            )
        if self.published_at and self.available_at and self.published_at > self.available_at:
            raise MultiAgentValidationError(
                "evidence publication cannot follow its availability"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "cycle_id": self.cycle_id,
            "agent_id": self.agent_id,
            "evidence_type": self.kind.value,
            "source_identity": self.source_identity,
            "source_reference": self.source_reference,
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "summary": self.summary,
            "value": dict(self.value),
            "original_at": self.original_at.isoformat() if self.original_at else None,
            "retrieved_at": self.retrieved_at.isoformat(),
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "available_at": self.available_at.isoformat() if self.available_at else None,
            "data_version": self.data_version,
            "forecast_version": self.forecast_version,
            "model_version": self.model_version,
            "feature_set_version": self.feature_set_version,
            "freshness": self.freshness.value,
            "validation": self.validation.value,
            "independent": self.independent,
            "correlation_group": self.correlation_group,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceRecord:
        return cls(
            evidence_id=text(payload.get("evidence_id")),
            cycle_id=text(payload.get("cycle_id")),
            agent_id=text(payload.get("agent_id")),
            kind=EvidenceKind(text(payload.get("evidence_type"), "unverified")),
            source_identity=text(payload.get("source_identity")),
            source_reference=text(payload.get("source_reference")),
            instrument_id=text(payload.get("instrument_id")),
            exchange=text(payload.get("exchange")),
            summary=text(payload.get("summary")),
            value=dict(payload.get("value") or {}),
            original_at=optional_time(payload.get("original_at")),
            retrieved_at=optional_time(payload.get("retrieved_at")) or datetime.now(UTC),
            published_at=optional_time(payload.get("published_at")),
            available_at=optional_time(payload.get("available_at")),
            data_version=text(payload.get("data_version")),
            forecast_version=text(payload.get("forecast_version")),
            model_version=text(payload.get("model_version")),
            feature_set_version=text(payload.get("feature_set_version")),
            freshness=Freshness(text(payload.get("freshness"), "unknown")),
            validation=ValidationStatus(text(payload.get("validation"), "unverified")),
            independent=bool(payload.get("independent", True)),
            correlation_group=text(payload.get("correlation_group")),
        )


@dataclass(frozen=True, slots=True)
class AgentRequest:
    message: AgentMessage
    horizon: str
    as_of: datetime
    context: Mapping[str, Any]
    evidence: tuple[EvidenceRecord, ...] = ()

    def __post_init__(self) -> None:
        canonical_horizon(self.horizon)
        if not isinstance(self.context, Mapping):
            raise MultiAgentValidationError("agent context must be an object")

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message.to_dict(),
            "horizon": self.horizon,
            "as_of": self.as_of.isoformat(),
            "context": dict(self.context),
            "evidence_ids": [item.evidence_id for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class AgentResponse:
    message_id: str
    task_id: str
    cycle_id: str
    agent_id: str
    role: FinancialAgentRole
    status: AgentRunStatus
    action: RecommendationAction = RecommendationAction.ABSTAIN
    confidence: float | None = None
    confidence_kind: str = "unavailable"
    findings: Mapping[str, Any] = field(default_factory=dict)
    supporting: tuple[str, ...] = ()
    contradicting: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    evidence: tuple[EvidenceRecord, ...] = ()
    source_references: tuple[str, ...] = ()
    error: str = ""
    latency_ms: float = 0.0
    config_version: str = "1"
    model_version: str = "pi17-rules-1.0"
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.confidence is not None:
            if not self.confidence_kind.startswith("calibrated"):
                raise MultiAgentValidationError(
                    "a numeric agent confidence requires calibrated provenance"
                )
            finite(self.confidence, "confidence", minimum=0.0, maximum=1.0)
        if len(self.evidence) > MAX_EVIDENCE_PER_RESPONSE:
            raise MultiAgentResourceError(
                f"an agent may return at most {MAX_EVIDENCE_PER_RESPONSE} evidence rows"
            )

    @property
    def usable(self) -> bool:
        return self.status in {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.INSUFFICIENT_EVIDENCE,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "task_id": self.task_id,
            "cycle_id": self.cycle_id,
            "agent_id": self.agent_id,
            "role": self.role.value,
            "status": self.status.value,
            "action": self.action.value,
            "confidence": self.confidence,
            "confidence_kind": self.confidence_kind,
            "findings": dict(self.findings),
            "supporting": list(self.supporting),
            "contradicting": list(self.contradicting),
            "uncertainties": list(self.uncertainties),
            "missing": list(self.missing),
            "evidence": [item.to_dict() for item in self.evidence],
            "source_references": list(self.source_references),
            "error": self.error,
            "latency_ms": round(self.latency_ms, 3),
            "config_version": self.config_version,
            "model_version": self.model_version,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "advisory_only": True,
            "production_authority": False,
        }


@dataclass(frozen=True, slots=True)
class ConflictRecord:
    conflict_id: str
    cycle_id: str
    kind: ConflictKind
    severity: ConflictSeverity
    agents: tuple[str, ...]
    description: str
    evidence_ids: tuple[str, ...]
    rule: str
    resolved: bool
    resolution: str
    preserved: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "cycle_id": self.cycle_id,
            "kind": self.kind.value,
            "severity": self.severity.value,
            "agents": list(self.agents),
            "description": self.description,
            "evidence_ids": list(self.evidence_ids),
            "resolution_rule": self.rule,
            "resolved": self.resolved,
            "resolution": self.resolution,
            "preserved": self.preserved,
        }


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    decision_id: str
    cycle_id: str
    instrument_id: str
    exchange: str
    horizon: str
    action: RecommendationAction
    supporting_agents: tuple[str, ...]
    contradicting_agents: tuple[str, ...]
    supporting_evidence: tuple[str, ...]
    contradictory_evidence: tuple[str, ...]
    evidence_strength: str
    confidence: float | None
    confidence_kind: str
    confidence_provenance: Mapping[str, Any]
    uncertainty: Mapping[str, Any]
    portfolio_context: Mapping[str, Any]
    risk_restrictions: tuple[str, ...]
    unresolved_conflicts: tuple[str, ...]
    explanation: str
    decided_at: datetime
    degraded: bool = False
    risk_review_required: bool = True
    handoff_required: bool = True
    order_submitted: bool = False
    production_authority: bool = False

    def __post_init__(self) -> None:
        canonical_horizon(self.horizon)
        if self.confidence is not None and not self.confidence_kind.startswith(
            "calibrated"
        ):
            raise MultiAgentValidationError(
                "numeric decision confidence requires calibrated provenance"
            )
        if self.order_submitted or self.production_authority:
            raise MultiAgentValidationError(
                "a multi-agent decision cannot submit or authorize an order"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "cycle_id": self.cycle_id,
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "horizon": self.horizon,
            "horizon_group": HORIZON_GROUPS[self.horizon],
            "action": self.action.value,
            "supporting_agents": list(self.supporting_agents),
            "contradicting_agents": list(self.contradicting_agents),
            "supporting_evidence": list(self.supporting_evidence),
            "contradictory_evidence": list(self.contradictory_evidence),
            "evidence_strength": self.evidence_strength,
            "confidence": self.confidence,
            "confidence_kind": self.confidence_kind,
            "confidence_provenance": dict(self.confidence_provenance),
            "uncertainty": dict(self.uncertainty),
            "portfolio_context": dict(self.portfolio_context),
            "risk_restrictions": list(self.risk_restrictions),
            "unresolved_conflicts": list(self.unresolved_conflicts),
            "explanation": self.explanation,
            "decided_at": self.decided_at.isoformat(),
            "degraded": self.degraded,
            "risk_review_required": True,
            "handoff_required": True,
            "order_submitted": False,
            "production_authority": False,
            "risk_authority": "NM-08",
            "execution_authority": "NM-10",
            "authorizes_action": False,
            "activation_available": False,
        }


@dataclass(frozen=True, slots=True)
class DecisionCycle:
    cycle_id: str
    instrument_id: str
    exchange: str
    horizon: str
    status: CycleStatus
    as_of: datetime
    requested_agents: tuple[str, ...]
    assigned_agents: tuple[str, ...]
    required_agents: tuple[str, ...]
    correlation_id: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    current_agent: str = ""
    decision_id: str = ""
    responses: tuple[AgentResponse, ...] = ()
    conflicts: tuple[ConflictRecord, ...] = ()
    error: str = ""
    degraded: bool = False
    cancelled: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        canonical_horizon(self.horizon)
        if len(self.assigned_agents) > MAX_AGENTS_PER_CYCLE:
            raise MultiAgentResourceError(
                f"at most {MAX_AGENTS_PER_CYCLE} agents may run in one cycle"
            )

    def to_dict(self, *, include_responses: bool = True) -> dict[str, Any]:
        body = {
            "cycle_id": self.cycle_id,
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "horizon": self.horizon,
            "horizon_group": HORIZON_GROUPS[self.horizon],
            "status": self.status.value,
            "as_of": self.as_of.isoformat(),
            "requested_agents": list(self.requested_agents),
            "assigned_agents": list(self.assigned_agents),
            "required_agents": list(self.required_agents),
            "correlation_id": self.correlation_id,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "current_agent": self.current_agent,
            "decision_id": self.decision_id,
            "agent_count": len(self.responses),
            "completed_agents": sum(
                1 for item in self.responses if item.status is AgentRunStatus.COMPLETED
            ),
            "failed_agents": sum(
                1
                for item in self.responses
                if item.status
                in {
                    AgentRunStatus.FAILED,
                    AgentRunStatus.TIMEOUT,
                    AgentRunStatus.INVALID,
                }
            ),
            "conflict_count": len(self.conflicts),
            "conflicts": [item.to_dict() for item in self.conflicts],
            "error": self.error,
            "degraded": self.degraded,
            "cancelled": self.cancelled,
            "metadata": dict(self.metadata),
            "advisory_only": True,
            "production_authority": False,
            "order_submitted": False,
        }
        if include_responses:
            body["responses"] = [item.to_dict() for item in self.responses]
        return body


@dataclass(frozen=True, slots=True)
class AgentPerformance:
    agent_id: str
    executions: int
    completed: int
    insufficient: int
    failed: int
    timeouts: int
    invalid: int
    average_latency_ms: float | None
    timeout_rate: float
    error_rate: float
    validation_failure_rate: float
    missing_evidence_rate: float
    recommendation_consistency: float | None
    historical_quality: float | None
    last_execution: str
    provider: str
    model_version: str
    health: AgentHealth

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "executions": self.executions,
            "completed": self.completed,
            "insufficient_evidence": self.insufficient,
            "failed": self.failed,
            "timeouts": self.timeouts,
            "invalid": self.invalid,
            "average_latency_ms": self.average_latency_ms,
            "timeout_rate": round(self.timeout_rate, 6),
            "error_rate": round(self.error_rate, 6),
            "validation_failure_rate": round(self.validation_failure_rate, 6),
            "missing_evidence_rate": round(self.missing_evidence_rate, 6),
            "recommendation_consistency": self.recommendation_consistency,
            "historical_quality": self.historical_quality,
            "last_execution": self.last_execution,
            "provider": self.provider,
            "model_version": self.model_version,
            "health": self.health.value,
            "reliability_basis": (
                "historical validation when available; execution success is not "
                "treated as predictive quality"
            ),
        }


@dataclass(frozen=True, slots=True)
class CycleRequest:
    instrument_id: str
    exchange: str
    horizon: str
    as_of: datetime
    portfolio_id: str = ""
    agents: tuple[str, ...] = ()
    correlation_id: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.instrument_id.strip():
            raise MultiAgentValidationError("instrument_id is required")
        if not self.exchange.strip():
            raise MultiAgentValidationError("exchange is required")
        canonical_horizon(self.horizon)
        if len(self.agents) > MAX_AGENTS_PER_CYCLE:
            raise MultiAgentValidationError(
                f"at most {MAX_AGENTS_PER_CYCLE} agents can be requested"
            )
        if self.instrument_id and len(self.instrument_id) > 128:
            raise MultiAgentValidationError("instrument_id is too long")

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "horizon": self.horizon,
            "as_of": self.as_of.isoformat(),
            "portfolio_id": self.portfolio_id,
            "agents": list(self.agents),
            "correlation_id": self.correlation_id,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, now: datetime) -> CycleRequest:
        raw_agents = payload.get("agents") or payload.get("requested_agents") or []
        if isinstance(raw_agents, str):
            raw_agents = [raw_agents]
        horizon = canonical_horizon(payload.get("horizon") or "5d")
        as_of = optional_time(payload.get("as_of")) or now
        if as_of > now + timedelta(seconds=5):
            raise MultiAgentValidationError("a decision cycle cannot start in the future")
        return cls(
            instrument_id=text(payload.get("instrument_id")),
            exchange=text(payload.get("exchange"), "XNSE").upper(),
            horizon=horizon,
            as_of=as_of,
            portfolio_id=text(payload.get("portfolio_id")),
            agents=tuple(text(item) for item in raw_agents if text(item)),
            correlation_id=text(payload.get("correlation_id")),
            payload=dict(payload),
        )


def response_with(
    response: AgentResponse,
    *,
    status: AgentRunStatus | None = None,
    error: str | None = None,
    finished_at: datetime | None = None,
) -> AgentResponse:
    finish = finished_at or datetime.now(UTC)
    updates: dict[str, Any] = {
        "finished_at": finish,
        "latency_ms": max(0.0, (finish - response.started_at).total_seconds() * 1000.0),
    }
    if status is not None:
        updates["status"] = status
    if error is not None:
        updates["error"] = error
    return replace(response, **updates)


def blank_response(
    request: AgentRequest,
    *,
    role: FinancialAgentRole,
    status: AgentRunStatus,
    missing: Sequence[str] = (),
    error: str = "",
) -> AgentResponse:
    now = datetime.now(UTC)
    return AgentResponse(
        message_id=request.message.message_id,
        task_id=request.message.task_id,
        cycle_id=request.message.cycle_id,
        agent_id=request.message.recipient_agent_id,
        role=role,
        status=status,
        missing=tuple(missing),
        error=error,
        started_at=now,
        finished_at=now,
        config_version=request.message.config_version,
    )
