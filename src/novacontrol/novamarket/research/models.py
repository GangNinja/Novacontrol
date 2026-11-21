"""PI-19 financial-research contracts.

The research layer reads caller-supplied, timestamped evidence and produces
structured advisory reports.  It never owns a portfolio, ledger, risk approval,
paper order, broker session or autonomy mode.  Missing or unverifiable evidence
is represented explicitly; it is never filled with a plausible-looking value.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast
from urllib.parse import urlsplit

RESEARCH_PHASE = "PI-19"
RESEARCH_CONTRACT_VERSION = "pi19-financial-research-1.0"
RESEARCH_SCHEMA_VERSION = 1
RESEARCH_VERSION = "pi19-research-1.0"

MAX_RUNS = 1_000
MAX_EVIDENCE = 20_000
MAX_STATEMENTS = 100
MAX_NEWS = 100
MAX_PEERS = 25
MAX_MACRO = 100
MAX_TEXT = 4_000
MAX_LONG_TEXT = 12_000
MAX_ANALYSIS_TYPES = 12
MAX_CONCURRENT_RUNS = 2
DEFAULT_RUN_TIMEOUT_SECONDS = 60
MAX_RUN_TIMEOUT_SECONDS = 120
DEFAULT_EVIDENCE_MAX_AGE_HOURS = 720.0
MAX_EVIDENCE_MAX_AGE_HOURS = 8_760.0

HORIZONS: dict[str, dict[str, str]] = {
    "5m": {"group": "intraday", "label": "5 minutes"},
    "15m": {"group": "intraday", "label": "15 minutes"},
    "20m": {"group": "intraday", "label": "20 minutes"},
    "60m": {"group": "intraday", "label": "60 minutes"},
    "1d": {"group": "swing", "label": "1 trading day"},
    "3d": {"group": "swing", "label": "3 trading days"},
    "5d": {"group": "swing", "label": "5 trading days"},
    "30d": {"group": "long_term", "label": "30 calendar days"},
    "90d": {"group": "long_term", "label": "90 calendar days"},
}

_INSTRUMENT_ID = re.compile(r"^[A-Z0-9][A-Z0-9._:-]{0,63}$")
_SAFE_URL = re.compile(r"^https?://", re.IGNORECASE)
_INSTRUCTION_RISK = re.compile(
    r"(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|above)\s+"
    r"(?:instructions?|prompts?)|"
    r"(?:system|developer)\s+(?:prompt|message)|"
    r"reveal\s+(?:the\s+)?(?:api\s*key|secret|password|token)|"
    r"execute\s+(?:this|the following)\s+(?:command|tool)|"
    r"you\s+are\s+now\s+(?:an?\s+)?(?:admin|administrator|system)",
    re.IGNORECASE,
)


class ResearchError(ValueError):
    """A PI-19 request is invalid."""


class ResearchValidationError(ResearchError):
    """Research configuration or evidence failed validation."""


class ResearchNotFoundError(LookupError):
    """A requested PI-19 record does not exist."""


class ResearchStateError(RuntimeError):
    """The record state does not permit the requested operation."""


class ResearchResourceError(RuntimeError):
    """A bounded PI-19 resource limit would be exceeded."""


class ResearchStoreError(RuntimeError):
    """The isolated PI-19 store could not complete the request."""


class ResearchStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    PARTIAL_EVIDENCE = "partial_evidence"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    SOURCE_UNAVAILABLE = "source_unavailable"
    STALE_DATA = "stale_data"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(
    {
        ResearchStatus.COMPLETED,
        ResearchStatus.COMPLETED_WITH_WARNINGS,
        ResearchStatus.PARTIAL_EVIDENCE,
        ResearchStatus.INSUFFICIENT_EVIDENCE,
        ResearchStatus.SOURCE_UNAVAILABLE,
        ResearchStatus.STALE_DATA,
        ResearchStatus.FAILED,
        ResearchStatus.CANCELLED,
    }
)


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partially_verified"
    UNVERIFIED = "unverified"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    SOURCE_UNAVAILABLE = "source_unavailable"
    STALE = "stale"


class EvidenceKind(StrEnum):
    COMPANY_PROFILE = "company_profile"
    FINANCIAL_STATEMENT = "financial_statement"
    CORPORATE_EVENT = "corporate_event"
    NEWS = "news"
    SECTOR = "sector"
    PEER = "peer"
    MACRO = "macro"
    MARKET_DATA = "market_data"
    FORECAST = "forecast"
    PORTFOLIO = "portfolio"
    OTHER = "other"


class SourceTier(StrEnum):
    PRIMARY_OFFICIAL = "primary_official"
    OFFICIAL_SECONDARY = "official_secondary"
    INDEPENDENT = "independent"
    COMMENTARY = "commentary"
    OPINION = "opinion"
    RUMOR = "rumor"
    DERIVED_MODEL = "derived_model"
    UNKNOWN = "unknown"


class AnalysisType(StrEnum):
    COMPANY = "company"
    STATEMENTS = "statements"
    RATIOS = "ratios"
    EVENTS = "events"
    NEWS = "news"
    SECTOR = "sector"
    PEERS = "peers"
    VALUATION = "valuation"
    MACRO = "macro"
    THESIS = "thesis"


class ResearchObjective(StrEnum):
    FULL_RESEARCH = "full_research"
    COMPANY_DUE_DILIGENCE = "company_due_diligence"
    EVENT_IMPACT = "event_impact"
    RELATIVE_VALUATION = "relative_valuation"
    FUNDAMENTAL_UPDATE = "fundamental_update"
    PORTFOLIO_CONTEXT = "portfolio_context"


class StatementType(StrEnum):
    INCOME = "income"
    BALANCE_SHEET = "balance_sheet"
    CASH_FLOW = "cash_flow"


class PeriodType(StrEnum):
    QUARTERLY = "quarterly"
    ANNUAL = "annual"


class StatementBasis(StrEnum):
    CONSOLIDATED = "consolidated"
    STANDALONE = "standalone"


class EventKind(StrEnum):
    EARNINGS = "earnings"
    DIVIDEND = "dividend"
    STOCK_SPLIT = "stock_split"
    BONUS = "bonus"
    RIGHTS = "rights"
    MERGER = "merger"
    ACQUISITION = "acquisition"
    BUYBACK = "buyback"
    MANAGEMENT_CHANGE = "management_change"
    REGULATORY = "regulatory"
    CONTRACT = "contract"
    RESTRUCTURING = "restructuring"
    SHARE_ISSUANCE = "share_issuance"
    LITIGATION = "litigation"
    TRADING_SUSPENSION = "trading_suspension"
    OTHER = "other"


class EventOccurrence(StrEnum):
    ANNOUNCED = "announced"
    EFFECTIVE = "effective"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class SentimentLabel(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    MIXED = "mixed"
    UNCERTAIN = "uncertain"


class Stance(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    UNCERTAIN = "uncertain"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ValuationMethod(StrEnum):
    RELATIVE = "relative"
    DCF = "dcf"


class Reliability(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def clean(value: object, *, limit: int = MAX_TEXT) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x00", " ").strip()
    return text[:limit]


def number(
    value: object,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or value is None or value == "":
        raise ResearchValidationError(f"{name} must be a finite number")
    try:
        result = float(str(value))
    except (TypeError, ValueError) as exc:
        raise ResearchValidationError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ResearchValidationError(f"{name} must be finite")
    if minimum is not None and result < minimum:
        raise ResearchValidationError(f"{name} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise ResearchValidationError(f"{name} must be at most {maximum}")
    return result


def optional_number(value: object, name: str) -> float | None:
    if value is None or value == "":
        return None
    return number(value, name)


def optional_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        result = float(str(value))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ResearchValidationError(f"{name} must be timezone-aware")
    return value


def publication_available(publication: datetime, cutoff: datetime) -> bool:
    """Whether statement-style evidence is available at ``cutoff``, by day.

    The free filing source publishes no time of day, so a statement or profile
    fact is dated by the first observation of the data and belongs to that
    observation's calendar day: a fetch at 15:00 is available to a run whose
    cutoff is 09:00 the same day, and still excluded by a cutoff on an earlier
    day.  The row keeps the real observation timestamp; only this comparison is
    day-granular.  Evidence with a real publication timestamp never uses it.
    """
    return publication.astimezone(UTC).date() <= cutoff.astimezone(UTC).date()


def optional_time(value: object, name: str = "timestamp") -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return aware(value, name)
    text = clean(value, limit=80)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchValidationError(f"{name} must be an ISO-8601 timestamp") from exc
    return aware(parsed, name)


def canonical_instrument(value: object) -> str:
    text = clean(value, limit=64).upper()
    if ":" not in text or not _INSTRUMENT_ID.match(text):
        raise ResearchValidationError("instrument_id must be exchange-qualified (MIC:SYMBOL)")
    return text


def sequence(value: object) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(value)
    raise ResearchValidationError("value must be a list")


def stable_id(prefix: str, *parts: object) -> str:
    encoded = "|".join(str(item) for item in parts)
    return prefix + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def digest_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def safe_url(value: object) -> str:
    text = clean(value, limit=2_000)
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ResearchValidationError("source_url must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ResearchValidationError("source_url must not embed credentials")
    return text


def instruction_risk(*texts: object) -> bool:
    combined = " ".join(clean(item, limit=MAX_LONG_TEXT) for item in texts if item)
    return bool(_INSTRUCTION_RISK.search(combined))


def canonical_horizon(value: object) -> str:
    horizon = clean(value, limit=8).lower() or "5d"
    if horizon not in HORIZONS:
        raise ResearchValidationError(
            f"unsupported research horizon {horizon!r}; expected one of {', '.join(HORIZONS)}"
        )
    return horizon


@dataclass(frozen=True, slots=True)
class ResearchRequest:
    research_id: str
    instrument_id: str
    as_of: datetime
    company_name: str = ""
    exchange: str = ""
    horizon: str = "5d"
    objective: str = ResearchObjective.FULL_RESEARCH.value
    analysis_types: tuple[str, ...] = ()
    publication_cutoff: datetime | None = None
    portfolio_id: str = ""
    parent_run_id: str = ""
    max_evidence_age_hours: float = DEFAULT_EVIDENCE_MAX_AGE_HOURS
    requested_by: str = ""
    idempotency_key: str = ""
    version: int = 1
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", canonical_instrument(self.instrument_id))
        object.__setattr__(self, "as_of", aware(self.as_of, "as_of"))
        if self.publication_cutoff is not None:
            cutoff = aware(self.publication_cutoff, "publication_cutoff")
            if cutoff > self.as_of:
                raise ResearchValidationError("publication_cutoff cannot be after as_of")
            object.__setattr__(self, "publication_cutoff", cutoff)
        exchange = clean(self.exchange, limit=16).upper() or self.instrument_id.split(":", 1)[0]
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "company_name", clean(self.company_name, limit=200))
        object.__setattr__(self, "horizon", canonical_horizon(self.horizon))
        objective = clean(self.objective, limit=64).lower()
        if objective not in {item.value for item in ResearchObjective}:
            raise ResearchValidationError(f"unknown research objective {objective!r}")
        object.__setattr__(self, "objective", objective)
        requested = tuple(
            dict.fromkeys(
                clean(item, limit=32).lower()
                for item in sequence(self.analysis_types)
                if clean(item, limit=32)
            )
        )
        if not requested:
            requested = tuple(item.value for item in AnalysisType)
        unknown = [item for item in requested if item not in {kind.value for kind in AnalysisType}]
        if unknown:
            raise ResearchValidationError(f"unknown analysis type(s): {', '.join(unknown)}")
        if len(requested) > MAX_ANALYSIS_TYPES:
            raise ResearchValidationError("too many analysis types requested")
        object.__setattr__(self, "analysis_types", requested)
        object.__setattr__(
            self,
            "max_evidence_age_hours",
            number(
                self.max_evidence_age_hours,
                "max_evidence_age_hours",
                minimum=1.0,
                maximum=MAX_EVIDENCE_MAX_AGE_HOURS,
            ),
        )
        object.__setattr__(self, "version", max(1, int(self.version)))
        object.__setattr__(self, "created_at", aware(self.created_at, "created_at"))

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> ResearchRequest:
        created = now or utcnow()
        instrument = canonical_instrument(payload.get("instrument_id"))
        as_of = optional_time(payload.get("as_of"), "as_of") or created
        cutoff = optional_time(payload.get("publication_cutoff"), "publication_cutoff")
        research_id = clean(payload.get("research_id"), limit=80) or stable_id(
            "prr_", instrument, as_of.isoformat(), payload.get("horizon") or "5d"
        )
        return cls(
            research_id=research_id,
            instrument_id=instrument,
            as_of=as_of,
            company_name=clean(payload.get("company_name"), limit=200),
            exchange=clean(payload.get("exchange"), limit=16).upper(),
            horizon=clean(payload.get("horizon"), limit=8) or "5d",
            objective=clean(payload.get("objective"), limit=64) or "full_research",
            analysis_types=tuple(payload.get("analysis_types") or ()),
            publication_cutoff=cutoff,
            portfolio_id=clean(payload.get("portfolio_id"), limit=80),
            parent_run_id=clean(payload.get("parent_run_id"), limit=80),
            max_evidence_age_hours=payload.get(
                "max_evidence_age_hours", DEFAULT_EVIDENCE_MAX_AGE_HOURS
            ),
            requested_by=clean(payload.get("requested_by"), limit=120),
            idempotency_key=clean(payload.get("idempotency_key"), limit=160),
            version=max(1, int(payload.get("version") or 1)),
            created_at=created,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "research_id": self.research_id,
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "company_name": self.company_name,
            "horizon": self.horizon,
            "horizon_group": HORIZONS[self.horizon]["group"],
            "objective": self.objective,
            "analysis_types": list(self.analysis_types),
            "as_of": self.as_of.isoformat(),
            "publication_cutoff": (
                self.publication_cutoff.isoformat() if self.publication_cutoff else None
            ),
            "portfolio_id": self.portfolio_id,
            "parent_run_id": self.parent_run_id,
            "max_evidence_age_hours": self.max_evidence_age_hours,
            "requested_by": self.requested_by,
            "idempotency_key": self.idempotency_key,
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "config_version": RESEARCH_VERSION,
            "currency": "INR",
            "nova_coin_rate": "1 NC = 1 INR",
        }


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_id: str
    instrument_id: str
    kind: str
    source_id: str
    source_name: str
    claim: str
    published_at: datetime | None = None
    retrieved_at: datetime = field(default_factory=utcnow)
    source_url: str = ""
    source_tier: str = SourceTier.UNKNOWN.value
    effective_at: datetime | None = None
    summary: str = ""
    reporting_period: str = ""
    currency: str = "INR"
    unit: str = ""
    verification: str = VerificationStatus.UNVERIFIED.value
    reliability: str = Reliability.UNKNOWN.value
    value: float | None = None
    content_hash: str = ""
    is_ai_interpretation: bool = False
    instruction_risk: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", canonical_instrument(self.instrument_id))
        kind = clean(self.kind, limit=32).lower()
        if kind not in {item.value for item in EvidenceKind}:
            raise ResearchValidationError(f"unknown evidence kind {kind!r}")
        object.__setattr__(self, "kind", kind)
        tier = clean(self.source_tier, limit=32).lower()
        if tier not in {item.value for item in SourceTier}:
            raise ResearchValidationError(f"unknown source tier {tier!r}")
        object.__setattr__(self, "source_tier", tier)
        verification = clean(self.verification, limit=32).lower()
        if verification not in {item.value for item in VerificationStatus}:
            raise ResearchValidationError(f"unknown verification status {verification!r}")
        object.__setattr__(self, "verification", verification)
        reliability = clean(self.reliability, limit=16).lower() or Reliability.UNKNOWN.value
        if reliability not in {item.value for item in Reliability}:
            raise ResearchValidationError(f"unknown reliability {reliability!r}")
        object.__setattr__(self, "reliability", reliability)
        object.__setattr__(self, "source_name", clean(self.source_name, limit=200))
        object.__setattr__(self, "source_id", clean(self.source_id, limit=160))
        object.__setattr__(self, "claim", clean(self.claim, limit=MAX_TEXT))
        object.__setattr__(self, "summary", clean(self.summary, limit=MAX_LONG_TEXT))
        object.__setattr__(self, "source_url", safe_url(self.source_url))
        object.__setattr__(self, "reporting_period", clean(self.reporting_period, limit=40))
        object.__setattr__(self, "unit", clean(self.unit, limit=32))
        object.__setattr__(self, "currency", clean(self.currency, limit=3).upper() or "INR")
        if self.published_at is not None:
            object.__setattr__(self, "published_at", aware(self.published_at, "published_at"))
        object.__setattr__(self, "retrieved_at", aware(self.retrieved_at, "retrieved_at"))
        if self.effective_at is not None:
            object.__setattr__(self, "effective_at", aware(self.effective_at, "effective_at"))
        if self.value is not None and not math.isfinite(float(self.value)):
            raise ResearchValidationError("evidence value must be finite")
        object.__setattr__(
            self,
            "instruction_risk",
            bool(self.instruction_risk)
            or instruction_risk(self.claim, self.summary, self.source_name),
        )
        object.__setattr__(
            self,
            "content_hash",
            clean(self.content_hash, limit=128)
            or hashlib.sha256(
                f"{self.source_id}|{self.claim}|{self.published_at}".encode()
            ).hexdigest(),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))
        object.__setattr__(
            self, "warnings", tuple(clean(item, limit=300) for item in self.warnings)
        )

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        instrument_id: str,
        default_time: datetime | None = None,
    ) -> EvidenceRecord:
        source_id = clean(payload.get("source_id") or payload.get("source"), limit=160)
        claim = clean(payload.get("claim") or payload.get("statement"), limit=MAX_TEXT)
        published = optional_time(
            payload.get("published_at") or payload.get("publication_time"),
            "published_at",
        )
        evidence_id = clean(payload.get("evidence_id"), limit=80) or stable_id(
            "pre_", instrument_id, source_id, claim, published.isoformat() if published else ""
        )
        return cls(
            evidence_id=evidence_id,
            instrument_id=instrument_id,
            kind=payload.get("kind") or EvidenceKind.OTHER.value,
            source_id=source_id,
            source_name=clean(payload.get("source_name") or payload.get("publisher"), limit=200),
            claim=claim,
            published_at=published,
            retrieved_at=optional_time(payload.get("retrieved_at"), "retrieved_at")
            or default_time
            or utcnow(),
            source_url=payload.get("source_url") or payload.get("url") or "",
            source_tier=payload.get("source_tier") or SourceTier.UNKNOWN.value,
            effective_at=optional_time(payload.get("effective_at"), "effective_at"),
            summary=clean(payload.get("summary"), limit=MAX_LONG_TEXT),
            reporting_period=clean(payload.get("reporting_period"), limit=40),
            currency=clean(payload.get("currency"), limit=3) or "INR",
            unit=clean(payload.get("unit"), limit=32),
            verification=payload.get("verification") or VerificationStatus.UNVERIFIED.value,
            reliability=payload.get("reliability") or Reliability.UNKNOWN.value,
            value=optional_float(payload.get("value")),
            content_hash=clean(payload.get("content_hash"), limit=128),
            is_ai_interpretation=bool(payload.get("is_ai_interpretation")),
            metadata=cast(Mapping[str, Any], payload.get("metadata") or {}),
            warnings=tuple(payload.get("warnings") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "instrument_id": self.instrument_id,
            "kind": self.kind,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "source_tier": self.source_tier,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "retrieved_at": self.retrieved_at.isoformat(),
            "effective_at": self.effective_at.isoformat() if self.effective_at else None,
            "claim": self.claim,
            "summary": self.summary,
            "reporting_period": self.reporting_period,
            "currency": self.currency,
            "unit": self.unit,
            "value": self.value,
            "verification": self.verification,
            "reliability": self.reliability,
            "content_hash": self.content_hash,
            "is_ai_interpretation": self.is_ai_interpretation,
            "instruction_risk": self.instruction_risk,
            "is_untrusted": True,
            "metadata": dict(sorted(self.metadata.items())),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class ProfileField:
    name: str
    value: str | tuple[str, ...] | None
    evidence_ids: tuple[str, ...] = ()
    published_at: datetime | None = None
    verification: str = VerificationStatus.UNVERIFIED.value
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", clean(self.name, limit=80))
        if isinstance(self.value, list):
            object.__setattr__(self, "value", tuple(clean(item, limit=300) for item in self.value))
        elif self.value is not None and not isinstance(self.value, tuple):
            object.__setattr__(self, "value", clean(self.value, limit=MAX_TEXT))
        if self.published_at is not None:
            object.__setattr__(self, "published_at", aware(self.published_at, "published_at"))
        if self.verification not in {item.value for item in VerificationStatus}:
            raise ResearchValidationError("unknown profile verification status")
        object.__setattr__(self, "note", clean(self.note, limit=500))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": list(self.value) if isinstance(self.value, tuple) else self.value,
            "evidence_ids": list(self.evidence_ids),
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "verification": self.verification,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class CompanyProfile:
    instrument_id: str
    symbol: str
    exchange: str
    name: str
    sector: str
    industry: str
    description: str
    as_of: datetime
    fields: Mapping[str, ProfileField] = field(default_factory=dict)
    missing_fields: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", canonical_instrument(self.instrument_id))
        object.__setattr__(self, "as_of", aware(self.as_of, "as_of"))
        object.__setattr__(self, "fields", dict(self.fields))
        object.__setattr__(self, "missing_fields", tuple(self.missing_fields))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    def value(self, name: str) -> Any:
        item = self.fields.get(name)
        return item.value if item else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "name": self.name,
            "sector": self.sector,
            "industry": self.industry,
            "description": self.description,
            "as_of": self.as_of.isoformat(),
            "fields": {key: value.to_dict() for key, value in sorted(self.fields.items())},
            "missing_fields": list(self.missing_fields),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class FinancialStatement:
    statement_id: str
    instrument_id: str
    statement_type: str
    period_type: str
    basis: str
    period_end: datetime
    currency: str
    values: Mapping[str, float | None]
    published_at: datetime | None = None
    retrieved_at: datetime = field(default_factory=utcnow)
    period_start: datetime | None = None
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None
    unit: str = "absolute"
    scale: float = 1.0
    fx_rate: float | None = None
    fx_as_of: datetime | None = None
    fx_source: str = ""
    restatement_version: int = 1
    supersedes_statement_id: str = ""
    shares_outstanding: float | None = None
    evidence_id: str = ""
    verification: str = VerificationStatus.UNVERIFIED.value
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", canonical_instrument(self.instrument_id))
        statement_type = clean(self.statement_type, limit=32).lower()
        if statement_type not in {item.value for item in StatementType}:
            raise ResearchValidationError(f"unknown statement type {statement_type!r}")
        object.__setattr__(self, "statement_type", statement_type)
        period_type = clean(self.period_type, limit=32).lower()
        if period_type not in {item.value for item in PeriodType}:
            raise ResearchValidationError(f"unknown period type {period_type!r}")
        object.__setattr__(self, "period_type", period_type)
        basis = clean(self.basis, limit=32).lower()
        if basis not in {item.value for item in StatementBasis}:
            raise ResearchValidationError(f"unknown statement basis {basis!r}")
        object.__setattr__(self, "basis", basis)
        object.__setattr__(self, "period_end", aware(self.period_end, "period_end"))
        if self.period_start is not None:
            start = aware(self.period_start, "period_start")
            if start > self.period_end:
                raise ResearchValidationError("period_start cannot be after period_end")
            object.__setattr__(self, "period_start", start)
        if self.published_at is not None:
            object.__setattr__(self, "published_at", aware(self.published_at, "published_at"))
        object.__setattr__(self, "retrieved_at", aware(self.retrieved_at, "retrieved_at"))
        object.__setattr__(self, "currency", clean(self.currency, limit=3).upper() or "INR")
        object.__setattr__(self, "unit", clean(self.unit, limit=32).lower() or "absolute")
        object.__setattr__(self, "scale", number(self.scale, "scale", minimum=1e-12))
        object.__setattr__(
            self, "values", {clean(key, limit=80): value for key, value in self.values.items()}
        )
        object.__setattr__(self, "restatement_version", max(1, int(self.restatement_version)))
        if self.fx_rate is not None:
            object.__setattr__(self, "fx_rate", number(self.fx_rate, "fx_rate", minimum=1e-12))
        if self.fx_as_of is not None:
            object.__setattr__(self, "fx_as_of", aware(self.fx_as_of, "fx_as_of"))
        if self.shares_outstanding is not None:
            object.__setattr__(
                self,
                "shares_outstanding",
                number(self.shares_outstanding, "shares_outstanding", minimum=1e-12),
            )
        for key, value in self.values.items():
            if value is not None and not math.isfinite(float(value)):
                raise ResearchValidationError(f"statement value {key!r} must be finite")
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def has_publication_time(self) -> bool:
        return self.published_at is not None

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        instrument_id: str,
        default_time: datetime | None = None,
    ) -> FinancialStatement:
        values_raw = payload.get("values")
        if not isinstance(values_raw, Mapping):
            raise ResearchValidationError("financial statement values must be an object")
        values: dict[str, float | None] = {}
        for key, value in values_raw.items():
            values[clean(key, limit=80)] = optional_float(value)
        # period_type is part of the identity: an annual statement and its Q4
        # sibling share the same period end, so leaving it out collapsed them
        # into one id and the later row silently replaced the earlier one.
        statement_id = clean(payload.get("statement_id"), limit=80) or stable_id(
            "prs_",
            instrument_id,
            payload.get("statement_type"),
            clean(payload.get("period_type"), limit=40) or PeriodType.ANNUAL.value,
            payload.get("period_end"),
            payload.get("basis"),
            payload.get("restatement_version") or 1,
        )
        # The reporting period is an identity fact, never a default: filling a
        # missing period_end from default_time (the research cutoff in the
        # caller-context path) dated an undated statement to the run that read
        # it and let the point-in-time gate's "reporting period ends after the
        # cutoff" check pass against an invented date.  A publication time is
        # likewise never defaulted, so a statement with no published_at keeps
        # None and the gate reports it as missing.
        period_end = optional_time(payload.get("period_end"), "period_end")
        if period_end is None:
            raise ResearchValidationError("financial statement period_end is required")
        return cls(
            statement_id=statement_id,
            instrument_id=instrument_id,
            statement_type=payload.get("statement_type") or StatementType.INCOME.value,
            period_type=payload.get("period_type") or PeriodType.ANNUAL.value,
            basis=payload.get("basis") or StatementBasis.CONSOLIDATED.value,
            period_end=period_end,
            currency=payload.get("currency") or "INR",
            values=values,
            published_at=optional_time(payload.get("published_at"), "published_at"),
            retrieved_at=optional_time(payload.get("retrieved_at"), "retrieved_at")
            or default_time
            or utcnow(),
            period_start=optional_time(payload.get("period_start"), "period_start"),
            fiscal_year=(
                int(payload["fiscal_year"])
                if payload.get("fiscal_year") not in (None, "")
                else None
            ),
            fiscal_quarter=(
                int(payload["fiscal_quarter"])
                if payload.get("fiscal_quarter") not in (None, "")
                else None
            ),
            unit=payload.get("unit") or "absolute",
            scale=payload.get("scale") or 1.0,
            fx_rate=optional_float(payload.get("fx_rate")),
            fx_as_of=optional_time(payload.get("fx_as_of"), "fx_as_of"),
            fx_source=clean(payload.get("fx_source"), limit=200),
            restatement_version=payload.get("restatement_version") or 1,
            supersedes_statement_id=clean(payload.get("supersedes_statement_id"), limit=80),
            shares_outstanding=optional_float(payload.get("shares_outstanding")),
            evidence_id=clean(payload.get("evidence_id"), limit=80),
            verification=payload.get("verification") or VerificationStatus.UNVERIFIED.value,
            warnings=tuple(payload.get("warnings") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement_id": self.statement_id,
            "instrument_id": self.instrument_id,
            "statement_type": self.statement_type,
            "period_type": self.period_type,
            "basis": self.basis,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat(),
            "fiscal_year": self.fiscal_year,
            "fiscal_quarter": self.fiscal_quarter,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "retrieved_at": self.retrieved_at.isoformat(),
            "currency": self.currency,
            "unit": self.unit,
            "scale": self.scale,
            "values": dict(sorted(self.values.items())),
            "fx_rate": self.fx_rate,
            "fx_as_of": self.fx_as_of.isoformat() if self.fx_as_of else None,
            "fx_source": self.fx_source,
            "restatement_version": self.restatement_version,
            "supersedes_statement_id": self.supersedes_statement_id,
            "shares_outstanding": self.shares_outstanding,
            "evidence_id": self.evidence_id,
            "verification": self.verification,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class NormalizedStatement:
    statement_id: str
    statement_type: str
    period_type: str
    basis: str
    period_end: datetime
    published_at: datetime | None
    fiscal_year: int | None
    fiscal_quarter: int | None
    currency: str
    normalized_currency: str
    multiplier: float
    values: Mapping[str, float | None]
    original_values: Mapping[str, float | None]
    shares_outstanding: float | None
    restatement_version: int
    evidence_id: str
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement_id": self.statement_id,
            "statement_type": self.statement_type,
            "period_type": self.period_type,
            "basis": self.basis,
            "period_end": self.period_end.isoformat(),
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "fiscal_year": self.fiscal_year,
            "fiscal_quarter": self.fiscal_quarter,
            "currency": self.currency,
            "normalized_currency": self.normalized_currency,
            "multiplier": self.multiplier,
            "values": dict(sorted(self.values.items())),
            "original_values": dict(sorted(self.original_values.items())),
            "shares_outstanding": self.shares_outstanding,
            "restatement_version": self.restatement_version,
            "evidence_id": self.evidence_id,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class RatioResult:
    values: Mapping[str, float | None]
    statuses: Mapping[str, str]
    formulas: Mapping[str, str]
    inputs: Mapping[str, Any]
    warnings: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    period: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "values": dict(sorted(self.values.items())),
            "statuses": dict(sorted(self.statuses.items())),
            "formulas": dict(sorted(self.formulas.items())),
            "inputs": dict(sorted(self.inputs.items())),
            "warnings": list(self.warnings),
            "assumptions": list(self.assumptions),
            "period": self.period,
        }


@dataclass(frozen=True, slots=True)
class FinancialAnalysis:
    statements: tuple[Mapping[str, Any], ...]
    normalized: tuple[Mapping[str, Any], ...]
    ttm: Mapping[str, Any] | None
    comparisons: Mapping[str, Any]
    ratios: RatioResult
    restatements: tuple[Mapping[str, Any], ...] = ()
    missing: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "statements": [dict(item) for item in self.statements],
            "normalized": [dict(item) for item in self.normalized],
            "ttm": dict(self.ttm) if self.ttm else None,
            "comparisons": dict(self.comparisons),
            "ratios": self.ratios.to_dict(),
            "restatements": [dict(item) for item in self.restatements],
            "missing": list(self.missing),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class CorporateEvent:
    event_id: str
    instrument_id: str
    event_type: str
    announced_at: datetime
    effective_at: datetime | None
    source_name: str
    description: str
    verification: str
    occurrence: str = EventOccurrence.ANNOUNCED.value
    source_url: str = ""
    source_tier: str = SourceTier.UNKNOWN.value
    financial_details: Mapping[str, Any] = field(default_factory=dict)
    interpretation: str = ""
    evidence_ids: tuple[str, ...] = ()
    instruction_risk: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", canonical_instrument(self.instrument_id))
        event_type = clean(self.event_type, limit=40).lower()
        if event_type not in {item.value for item in EventKind}:
            raise ResearchValidationError(f"unknown corporate event type {event_type!r}")
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(self, "announced_at", aware(self.announced_at, "announced_at"))
        if self.effective_at is not None:
            object.__setattr__(self, "effective_at", aware(self.effective_at, "effective_at"))
        if self.verification not in {item.value for item in VerificationStatus}:
            raise ResearchValidationError("unknown event verification status")
        if self.occurrence not in {item.value for item in EventOccurrence}:
            raise ResearchValidationError("unknown event occurrence")
        object.__setattr__(self, "source_url", safe_url(self.source_url))
        object.__setattr__(self, "description", clean(self.description, limit=MAX_LONG_TEXT))
        object.__setattr__(self, "interpretation", clean(self.interpretation, limit=MAX_TEXT))
        object.__setattr__(self, "financial_details", dict(self.financial_details))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(
            self,
            "instruction_risk",
            bool(self.instruction_risk) or instruction_risk(self.description, self.interpretation),
        )

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        instrument_id: str,
        default_time: datetime | None = None,
    ) -> CorporateEvent:
        announced = (
            optional_time(
                payload.get("announced_at") or payload.get("announcement_date"),
                "announced_at",
            )
            or default_time
            or utcnow()
        )
        return cls(
            event_id=clean(payload.get("event_id"), limit=80)
            or stable_id("pev_", instrument_id, payload.get("event_type"), announced.isoformat()),
            instrument_id=instrument_id,
            event_type=payload.get("event_type") or EventKind.OTHER.value,
            announced_at=announced,
            effective_at=optional_time(
                payload.get("effective_at") or payload.get("effective_date"),
                "effective_at",
            ),
            source_name=clean(payload.get("source_name") or payload.get("source"), limit=200),
            source_url=payload.get("source_url") or payload.get("url") or "",
            source_tier=payload.get("source_tier") or SourceTier.UNKNOWN.value,
            description=clean(payload.get("description"), limit=MAX_LONG_TEXT),
            verification=payload.get("verification") or VerificationStatus.UNVERIFIED.value,
            occurrence=payload.get("occurrence") or EventOccurrence.ANNOUNCED.value,
            financial_details=cast(Mapping[str, Any], payload.get("financial_details") or {}),
            interpretation=clean(payload.get("interpretation"), limit=MAX_TEXT),
            evidence_ids=tuple(payload.get("evidence_ids") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "instrument_id": self.instrument_id,
            "event_type": self.event_type,
            "announced_at": self.announced_at.isoformat(),
            "effective_at": self.effective_at.isoformat() if self.effective_at else None,
            "occurrence": self.occurrence,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "source_tier": self.source_tier,
            "description": self.description,
            "financial_details": dict(sorted(self.financial_details.items())),
            "verification": self.verification,
            "interpretation": self.interpretation,
            "evidence_ids": list(self.evidence_ids),
            "instruction_risk": self.instruction_risk,
            "is_confirmed": self.verification == VerificationStatus.VERIFIED.value
            and self.occurrence != EventOccurrence.ANNOUNCED.value,
            "has_occurred": self.occurrence == EventOccurrence.EFFECTIVE.value,
        }


@dataclass(frozen=True, slots=True)
class NewsArticle:
    article_id: str
    headline: str
    source_name: str
    published_at: datetime | None
    retrieved_at: datetime
    summary: str = ""
    url: str = ""
    source_id: str = ""
    evidence_ids: tuple[str, ...] = ()
    source_tier: str = SourceTier.UNKNOWN.value
    instrument_ids: tuple[str, ...] = ()
    category: str = "company"
    relevance: float = 0.0
    sentiment: str = SentimentLabel.UNCERTAIN.value
    sentiment_score: float | None = None
    sentiment_basis: tuple[str, ...] = ()
    verification: str = VerificationStatus.UNVERIFIED.value
    is_ai_interpretation: bool = False
    claims: tuple[str, ...] = ()
    instruction_risk: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "headline", clean(self.headline, limit=500))
        if not self.headline:
            raise ResearchValidationError("news headline is required")
        object.__setattr__(self, "source_name", clean(self.source_name, limit=200))
        object.__setattr__(self, "source_id", clean(self.source_id, limit=160))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        if self.published_at is not None:
            object.__setattr__(self, "published_at", aware(self.published_at, "published_at"))
        object.__setattr__(self, "retrieved_at", aware(self.retrieved_at, "retrieved_at"))
        object.__setattr__(self, "summary", clean(self.summary, limit=MAX_LONG_TEXT))
        object.__setattr__(self, "url", safe_url(self.url))
        tier = clean(self.source_tier, limit=32).lower()
        if tier not in {item.value for item in SourceTier}:
            raise ResearchValidationError(f"unknown news source tier {tier!r}")
        object.__setattr__(self, "source_tier", tier)
        object.__setattr__(
            self,
            "instrument_ids",
            tuple(canonical_instrument(item) for item in self.instrument_ids),
        )
        object.__setattr__(self, "category", clean(self.category, limit=40).lower() or "company")
        object.__setattr__(
            self, "relevance", number(self.relevance, "relevance", minimum=0.0, maximum=1.0)
        )
        sentiment = clean(self.sentiment, limit=20).lower()
        if sentiment not in {item.value for item in SentimentLabel}:
            raise ResearchValidationError(f"unknown sentiment label {sentiment!r}")
        object.__setattr__(self, "sentiment", sentiment)
        if self.sentiment_score is not None:
            object.__setattr__(
                self,
                "sentiment_score",
                number(self.sentiment_score, "sentiment_score", minimum=-1.0, maximum=1.0),
            )
        if self.verification not in {item.value for item in VerificationStatus}:
            raise ResearchValidationError("unknown news verification status")
        object.__setattr__(
            self, "claims", tuple(clean(item, limit=MAX_TEXT) for item in self.claims)
        )
        object.__setattr__(
            self,
            "instruction_risk",
            bool(self.instruction_risk)
            or instruction_risk(self.headline, self.summary, *self.claims),
        )

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        instrument_id: str,
        default_time: datetime | None = None,
    ) -> NewsArticle:
        published = optional_time(
            payload.get("published_at") or payload.get("publication_time"),
            "published_at",
        )
        headline = clean(payload.get("headline") or payload.get("title"), limit=500)
        source_name = clean(payload.get("source_name") or payload.get("source"), limit=200)
        return cls(
            article_id=clean(payload.get("article_id"), limit=80)
            or stable_id(
                "pna_",
                instrument_id,
                source_name,
                headline,
                published.isoformat() if published else "",
            ),
            headline=headline,
            source_name=source_name,
            source_id=clean(payload.get("source_id"), limit=160),
            evidence_ids=tuple(payload.get("evidence_ids") or ()),
            published_at=published,
            retrieved_at=optional_time(payload.get("retrieved_at"), "retrieved_at")
            or default_time
            or utcnow(),
            summary=clean(payload.get("summary"), limit=MAX_LONG_TEXT),
            url=payload.get("url") or payload.get("source_url") or "",
            source_tier=payload.get("source_tier") or SourceTier.UNKNOWN.value,
            instrument_ids=tuple(payload.get("instrument_ids") or (instrument_id,)),
            category=payload.get("category") or "company",
            relevance=payload.get("relevance", 0.0),
            sentiment=payload.get("sentiment") or SentimentLabel.UNCERTAIN.value,
            sentiment_score=optional_float(payload.get("sentiment_score")),
            sentiment_basis=tuple(payload.get("sentiment_basis") or ()),
            verification=payload.get("verification") or VerificationStatus.UNVERIFIED.value,
            is_ai_interpretation=bool(payload.get("is_ai_interpretation")),
            claims=tuple(payload.get("claims") or ()),
            instruction_risk=bool(payload.get("instruction_risk")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "article_id": self.article_id,
            "headline": self.headline,
            "source_name": self.source_name,
            "source_id": self.source_id,
            "evidence_ids": list(self.evidence_ids),
            "url": self.url,
            "source_tier": self.source_tier,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "retrieved_at": self.retrieved_at.isoformat(),
            "instrument_ids": list(self.instrument_ids),
            "category": self.category,
            "relevance": self.relevance,
            "sentiment": self.sentiment,
            "sentiment_score": self.sentiment_score,
            "sentiment_basis": list(self.sentiment_basis),
            "verification": self.verification,
            "summary": self.summary,
            "claims": list(self.claims),
            "is_ai_interpretation": self.is_ai_interpretation,
            "instruction_risk": self.instruction_risk,
            "is_untrusted": True,
            "price_direction_claim": False,
        }


@dataclass(frozen=True, slots=True)
class SectorSnapshot:
    sector: str
    as_of: datetime
    source_name: str
    published_at: datetime | None
    retrieval_at: datetime
    industry: str = ""
    performance: float | None = None
    relative_strength: float | None = None
    volatility: float | None = None
    valuation_metrics: Mapping[str, float | None] = field(default_factory=dict)
    growth_metrics: Mapping[str, float | None] = field(default_factory=dict)
    risks: tuple[str, ...] = ()
    regulatory: str = ""
    economic_sensitivity: str = ""
    correlations: Mapping[str, float | None] = field(default_factory=dict)
    verification: str = VerificationStatus.UNVERIFIED.value
    evidence_id: str = ""
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "sector", clean(self.sector, limit=120).lower())
        if not self.sector:
            raise ResearchValidationError("sector is required")
        object.__setattr__(self, "as_of", aware(self.as_of, "as_of"))
        if self.published_at is not None:
            object.__setattr__(self, "published_at", aware(self.published_at, "published_at"))
        object.__setattr__(self, "retrieval_at", aware(self.retrieval_at, "retrieval_at"))
        if self.verification not in {item.value for item in VerificationStatus}:
            raise ResearchValidationError("unknown sector verification status")
        object.__setattr__(self, "valuation_metrics", dict(self.valuation_metrics))
        object.__setattr__(self, "growth_metrics", dict(self.growth_metrics))
        object.__setattr__(self, "risks", tuple(clean(item, limit=300) for item in self.risks))
        object.__setattr__(self, "correlations", dict(self.correlations))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        default_time: datetime | None = None,
    ) -> SectorSnapshot:
        return cls(
            sector=payload.get("sector") or "",
            as_of=optional_time(payload.get("as_of"), "as_of") or default_time or utcnow(),
            source_name=clean(payload.get("source_name") or payload.get("source"), limit=200),
            published_at=optional_time(payload.get("published_at"), "published_at"),
            retrieval_at=optional_time(payload.get("retrieval_at"), "retrieval_at")
            or default_time
            or utcnow(),
            industry=clean(payload.get("industry"), limit=120).lower(),
            performance=optional_float(payload.get("performance")),
            relative_strength=optional_float(payload.get("relative_strength")),
            volatility=optional_float(payload.get("volatility")),
            valuation_metrics=cast(
                Mapping[str, float | None], payload.get("valuation_metrics") or {}
            ),
            growth_metrics=cast(Mapping[str, float | None], payload.get("growth_metrics") or {}),
            risks=tuple(payload.get("risks") or ()),
            regulatory=clean(payload.get("regulatory"), limit=MAX_TEXT),
            economic_sensitivity=clean(payload.get("economic_sensitivity"), limit=MAX_TEXT),
            correlations=cast(Mapping[str, float | None], payload.get("correlations") or {}),
            verification=payload.get("verification") or VerificationStatus.UNVERIFIED.value,
            evidence_id=clean(payload.get("evidence_id"), limit=80),
            warnings=tuple(payload.get("warnings") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sector": self.sector,
            "industry": self.industry,
            "as_of": self.as_of.isoformat(),
            "source_name": self.source_name,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "retrieval_at": self.retrieval_at.isoformat(),
            "performance": self.performance,
            "relative_strength": self.relative_strength,
            "volatility": self.volatility,
            "valuation_metrics": dict(sorted(self.valuation_metrics.items())),
            "growth_metrics": dict(sorted(self.growth_metrics.items())),
            "risks": list(self.risks),
            "regulatory": self.regulatory,
            "economic_sensitivity": self.economic_sensitivity,
            "correlations": dict(sorted(self.correlations.items())),
            "verification": self.verification,
            "evidence_id": self.evidence_id,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class PeerRecord:
    instrument_id: str
    name: str
    industry: str
    metrics: Mapping[str, float | None]
    source_name: str
    published_at: datetime | None
    period_end: datetime
    currency: str = "INR"
    sector: str = ""
    market_cap: float | None = None
    exchange: str = ""
    evidence_id: str = ""
    verification: str = VerificationStatus.UNVERIFIED.value
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", canonical_instrument(self.instrument_id))
        object.__setattr__(self, "industry", clean(self.industry, limit=120).lower())
        if self.published_at is not None:
            object.__setattr__(self, "published_at", aware(self.published_at, "published_at"))
        object.__setattr__(self, "period_end", aware(self.period_end, "period_end"))
        object.__setattr__(self, "currency", clean(self.currency, limit=3).upper() or "INR")
        object.__setattr__(
            self, "metrics", {clean(key, limit=80): value for key, value in self.metrics.items()}
        )
        for key, value in self.metrics.items():
            if value is not None and not math.isfinite(float(value)):
                raise ResearchValidationError(f"peer metric {key!r} must be finite")
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        default_time: datetime | None = None,
    ) -> PeerRecord:
        return cls(
            instrument_id=payload.get("instrument_id") or "",
            name=clean(payload.get("name"), limit=200),
            industry=clean(payload.get("industry"), limit=120).lower(),
            metrics=cast(Mapping[str, float | None], payload.get("metrics") or {}),
            source_name=clean(payload.get("source_name") or payload.get("source"), limit=200),
            published_at=optional_time(payload.get("published_at"), "published_at"),
            period_end=optional_time(payload.get("period_end"), "period_end")
            or default_time
            or utcnow(),
            currency=payload.get("currency") or "INR",
            sector=clean(payload.get("sector"), limit=120).lower(),
            market_cap=optional_float(payload.get("market_cap")),
            exchange=clean(payload.get("exchange"), limit=16).upper(),
            evidence_id=clean(payload.get("evidence_id"), limit=80),
            verification=payload.get("verification") or VerificationStatus.UNVERIFIED.value,
            warnings=tuple(payload.get("warnings") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "name": self.name,
            "industry": self.industry,
            "sector": self.sector,
            "exchange": self.exchange,
            "currency": self.currency,
            "market_cap": self.market_cap,
            "period_end": self.period_end.isoformat(),
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "source_name": self.source_name,
            "metrics": dict(sorted(self.metrics.items())),
            "evidence_id": self.evidence_id,
            "verification": self.verification,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class MacroObservation:
    indicator: str
    value: float
    unit: str
    period: str
    published_at: datetime | None
    source_name: str
    revision_status: str = "original"
    revised_at: datetime | None = None
    source_url: str = ""
    reliability: str = Reliability.UNKNOWN.value
    evidence_id: str = ""
    verification: str = VerificationStatus.UNVERIFIED.value
    relevant: bool = True
    relevance_note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "indicator", clean(self.indicator, limit=120))
        object.__setattr__(self, "value", number(self.value, "macro value"))
        object.__setattr__(self, "unit", clean(self.unit, limit=60))
        object.__setattr__(self, "period", clean(self.period, limit=60))
        if self.published_at is not None:
            object.__setattr__(self, "published_at", aware(self.published_at, "published_at"))
        if self.revised_at is not None:
            object.__setattr__(self, "revised_at", aware(self.revised_at, "revised_at"))
        object.__setattr__(
            self, "revision_status", clean(self.revision_status, limit=40).lower() or "original"
        )
        object.__setattr__(self, "source_url", safe_url(self.source_url))
        object.__setattr__(self, "relevance_note", clean(self.relevance_note, limit=500))

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        default_time: datetime | None = None,
    ) -> MacroObservation:
        return cls(
            indicator=payload.get("indicator") or "",
            value=number(payload.get("value"), "macro value"),
            unit=payload.get("unit") or "",
            period=payload.get("period") or "",
            published_at=optional_time(payload.get("published_at"), "published_at") or default_time,
            source_name=clean(payload.get("source_name") or payload.get("source"), limit=200),
            revision_status=payload.get("revision_status") or "original",
            revised_at=optional_time(payload.get("revised_at"), "revised_at"),
            source_url=payload.get("source_url") or payload.get("url") or "",
            reliability=payload.get("reliability") or Reliability.UNKNOWN.value,
            evidence_id=clean(payload.get("evidence_id"), limit=80),
            verification=payload.get("verification") or VerificationStatus.UNVERIFIED.value,
            relevant=bool(payload.get("relevant", True)),
            relevance_note=clean(payload.get("relevance_note"), limit=500),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "indicator": self.indicator,
            "value": self.value,
            "unit": self.unit,
            "period": self.period,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "revised_at": self.revised_at.isoformat() if self.revised_at else None,
            "revision_status": self.revision_status,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "reliability": self.reliability,
            "evidence_id": self.evidence_id,
            "verification": self.verification,
            "relevant": self.relevant,
            "relevance_note": self.relevance_note,
        }


@dataclass(frozen=True, slots=True)
class ValuationAssumptions:
    method: str
    currency: str = "INR"
    forecast_years: int = 5
    discount_rate: float = 0.12
    terminal_growth: float = 0.04
    revenue_growth: tuple[float, ...] = ()
    operating_margin: float = 0.15
    tax_rate: float = 0.25
    capex_pct_revenue: float = 0.05
    working_capital_pct_revenue: float = 0.05
    shares_outstanding: float | None = None
    net_debt: float = 0.0
    peer_metric: str = "pe"
    peer_multiple: float | None = None
    base_metric: float | None = None
    revenue: float | None = None
    ebitda: float | None = None
    book_value: float | None = None
    eps: float | None = None
    free_cash_flow: float | None = None
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        method = clean(self.method, limit=32).lower()
        if method not in {item.value for item in ValuationMethod}:
            raise ResearchValidationError(f"unknown valuation method {method!r}")
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "currency", clean(self.currency, limit=3).upper() or "INR")
        object.__setattr__(self, "forecast_years", max(1, min(int(self.forecast_years), 15)))
        object.__setattr__(
            self,
            "discount_rate",
            number(self.discount_rate, "discount_rate", minimum=0.0001, maximum=1.0),
        )
        object.__setattr__(
            self,
            "terminal_growth",
            number(self.terminal_growth, "terminal_growth", minimum=-0.5, maximum=0.5),
        )
        if self.terminal_growth >= self.discount_rate:
            raise ResearchValidationError("terminal_growth must be below discount_rate")
        growth = tuple(
            number(item, "revenue_growth", minimum=-0.99, maximum=5.0)
            for item in self.revenue_growth
        )
        object.__setattr__(self, "revenue_growth", growth)
        object.__setattr__(
            self,
            "operating_margin",
            number(self.operating_margin, "operating_margin", minimum=-1.0, maximum=1.0),
        )
        object.__setattr__(
            self, "tax_rate", number(self.tax_rate, "tax_rate", minimum=0.0, maximum=1.0)
        )
        object.__setattr__(
            self,
            "capex_pct_revenue",
            number(self.capex_pct_revenue, "capex_pct_revenue", minimum=0.0, maximum=2.0),
        )
        object.__setattr__(
            self,
            "working_capital_pct_revenue",
            number(
                self.working_capital_pct_revenue,
                "working_capital_pct_revenue",
                minimum=0.0,
                maximum=2.0,
            ),
        )
        if self.shares_outstanding is not None:
            object.__setattr__(
                self,
                "shares_outstanding",
                number(self.shares_outstanding, "shares_outstanding", minimum=1e-12),
            )
        object.__setattr__(self, "net_debt", number(self.net_debt, "net_debt"))
        if self.peer_multiple is not None:
            object.__setattr__(
                self, "peer_multiple", number(self.peer_multiple, "peer_multiple", minimum=0.0)
            )
        for name in ("base_metric", "revenue", "ebitda", "book_value", "eps", "free_cash_flow"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, number(value, name))
        object.__setattr__(self, "notes", tuple(clean(item, limit=500) for item in self.notes))

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ValuationAssumptions:
        growth = payload.get("revenue_growth") or ()
        return cls(
            method=payload.get("method") or ValuationMethod.RELATIVE.value,
            currency=payload.get("currency") or "INR",
            forecast_years=payload.get("forecast_years") or 5,
            discount_rate=payload.get("discount_rate") or 0.12,
            terminal_growth=payload.get("terminal_growth") or 0.04,
            revenue_growth=tuple(growth),
            operating_margin=payload.get("operating_margin") or 0.15,
            tax_rate=payload.get("tax_rate") or 0.25,
            capex_pct_revenue=payload.get("capex_pct_revenue") or 0.05,
            working_capital_pct_revenue=payload.get("working_capital_pct_revenue") or 0.05,
            shares_outstanding=optional_float(payload.get("shares_outstanding")),
            net_debt=payload.get("net_debt") or 0.0,
            peer_metric=payload.get("peer_metric") or "pe",
            peer_multiple=optional_float(payload.get("peer_multiple")),
            base_metric=optional_float(payload.get("base_metric")),
            revenue=optional_float(payload.get("revenue")),
            ebitda=optional_float(payload.get("ebitda")),
            book_value=optional_float(payload.get("book_value")),
            eps=optional_float(payload.get("eps")),
            free_cash_flow=optional_float(payload.get("free_cash_flow")),
            notes=tuple(payload.get("notes") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "currency": self.currency,
            "forecast_years": self.forecast_years,
            "discount_rate": self.discount_rate,
            "terminal_growth": self.terminal_growth,
            "revenue_growth": list(self.revenue_growth),
            "operating_margin": self.operating_margin,
            "tax_rate": self.tax_rate,
            "capex_pct_revenue": self.capex_pct_revenue,
            "working_capital_pct_revenue": self.working_capital_pct_revenue,
            "shares_outstanding": self.shares_outstanding,
            "net_debt": self.net_debt,
            "peer_metric": self.peer_metric,
            "peer_multiple": self.peer_multiple,
            "base_metric": self.base_metric,
            "revenue": self.revenue,
            "ebitda": self.ebitda,
            "book_value": self.book_value,
            "eps": self.eps,
            "free_cash_flow": self.free_cash_flow,
            "notes": list(self.notes),
            "is_assumption": True,
        }


@dataclass(frozen=True, slots=True)
class ValuationResult:
    method: str
    status: str
    currency: str
    estimate: float | None = None
    low: float | None = None
    high: float | None = None
    assumptions: Mapping[str, Any] = field(default_factory=dict)
    inputs: Mapping[str, Any] = field(default_factory=dict)
    sensitivity: tuple[Mapping[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.method not in {item.value for item in ValuationMethod}:
            raise ResearchValidationError("unknown valuation result method")
        object.__setattr__(self, "assumptions", dict(self.assumptions))
        object.__setattr__(self, "inputs", dict(self.inputs))
        object.__setattr__(self, "sensitivity", tuple(dict(item) for item in self.sensitivity))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "limitations", tuple(self.limitations))

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "status": self.status,
            "currency": self.currency,
            "estimate": self.estimate,
            "low": self.low,
            "high": self.high,
            "assumptions": dict(sorted(self.assumptions.items())),
            "inputs": dict(sorted(self.inputs.items())),
            "sensitivity": [dict(item) for item in self.sensitivity],
            "warnings": list(self.warnings),
            "limitations": list(self.limitations),
            "is_objectively_verified_price": False,
            "estimate_type": "derived_estimate",
        }


@dataclass(frozen=True, slots=True)
class InvestmentThesis:
    thesis_id: str
    instrument_id: str
    horizon: str
    stance: str
    overview: Mapping[str, Any]
    positive_factors: tuple[Mapping[str, Any], ...]
    negative_factors: tuple[Mapping[str, Any], ...]
    forecast_context: Mapping[str, Any]
    portfolio_context: Mapping[str, Any]
    conclusion: str
    supporting_evidence: tuple[str, ...]
    contradictory_evidence: tuple[str, ...]
    assumptions: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    confidence: Mapping[str, Any]
    version: int = 1
    parent_thesis_id: str = ""
    change_reason: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", canonical_instrument(self.instrument_id))
        object.__setattr__(self, "horizon", canonical_horizon(self.horizon))
        if self.stance not in {item.value for item in Stance}:
            raise ResearchValidationError(f"unknown thesis stance {self.stance!r}")
        object.__setattr__(self, "supporting_evidence", tuple(self.supporting_evidence))
        object.__setattr__(self, "contradictory_evidence", tuple(self.contradictory_evidence))
        object.__setattr__(self, "assumptions", tuple(self.assumptions))
        object.__setattr__(self, "unresolved_questions", tuple(self.unresolved_questions))
        object.__setattr__(self, "change_reason", tuple(self.change_reason))

    def to_dict(self) -> dict[str, Any]:
        return {
            "thesis_id": self.thesis_id,
            "instrument_id": self.instrument_id,
            "horizon": self.horizon,
            "stance": self.stance,
            "overview": dict(self.overview),
            "positive_factors": [dict(item) for item in self.positive_factors],
            "negative_factors": [dict(item) for item in self.negative_factors],
            "forecast_context": dict(self.forecast_context),
            "portfolio_context": dict(self.portfolio_context),
            "conclusion": self.conclusion,
            "supporting_evidence": list(self.supporting_evidence),
            "contradictory_evidence": list(self.contradictory_evidence),
            "assumptions": list(self.assumptions),
            "unresolved_questions": list(self.unresolved_questions),
            "confidence": dict(self.confidence),
            "version": self.version,
            "parent_thesis_id": self.parent_thesis_id,
            "change_reason": list(self.change_reason),
            "is_execution_authorization": False,
            "is_buy_sell_recommendation": False,
        }


__all__ = [
    "AnalysisType",
    "CompanyProfile",
    "CorporateEvent",
    "DEFAULT_EVIDENCE_MAX_AGE_HOURS",
    "DEFAULT_RUN_TIMEOUT_SECONDS",
    "EventKind",
    "EventOccurrence",
    "EvidenceKind",
    "EvidenceRecord",
    "FinancialAnalysis",
    "FinancialStatement",
    "HORIZONS",
    "InvestmentThesis",
    "MAX_ANALYSIS_TYPES",
    "MAX_CONCURRENT_RUNS",
    "MAX_EVIDENCE",
    "MAX_EVIDENCE_MAX_AGE_HOURS",
    "MAX_LONG_TEXT",
    "MAX_MACRO",
    "MAX_NEWS",
    "MAX_PEERS",
    "MAX_RUNS",
    "MAX_RUN_TIMEOUT_SECONDS",
    "MAX_STATEMENTS",
    "MAX_TEXT",
    "MacroObservation",
    "NewsArticle",
    "NormalizedStatement",
    "PeerRecord",
    "PeriodType",
    "ProfileField",
    "RatioResult",
    "Reliability",
    "ResearchError",
    "ResearchNotFoundError",
    "ResearchObjective",
    "ResearchRequest",
    "ResearchResourceError",
    "ResearchStateError",
    "ResearchStatus",
    "ResearchStoreError",
    "ResearchValidationError",
    "RESEARCH_CONTRACT_VERSION",
    "RESEARCH_PHASE",
    "RESEARCH_SCHEMA_VERSION",
    "RESEARCH_VERSION",
    "SectorSnapshot",
    "SentimentLabel",
    "SourceTier",
    "Stance",
    "StatementBasis",
    "StatementType",
    "TERMINAL_STATUSES",
    "ValuationAssumptions",
    "ValuationMethod",
    "ValuationResult",
    "VerificationStatus",
    "aware",
    "canonical_horizon",
    "canonical_instrument",
    "clean",
    "digest_payload",
    "instruction_risk",
    "number",
    "optional_float",
    "optional_number",
    "optional_time",
    "safe_url",
    "sequence",
    "stable_id",
    "utcnow",
]


def _sanity() -> None:  # pragma: no cover - import-time constant checks
    assert set(HORIZONS) == {"5m", "15m", "20m", "60m", "1d", "3d", "5d", "30d", "90d"}
