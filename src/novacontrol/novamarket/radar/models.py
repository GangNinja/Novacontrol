"""NM-04 event-intelligence value types.

These are the only shapes the radar stores, serves and publishes. A feed entry is
parsed by ``radar.sources`` into :class:`SourceEntry`, extraction turns it into an
:class:`ExtractedEvent`, verification and classification fill in their own value
objects, and the store persists a :class:`MarketEvent` version.

The rules enforced here (in construction, not merely in prose):

* **Three times are three fields.** ``occurred_at`` (when the thing happened),
  ``published_at`` (when the source announced it) and ``retrieved_at`` (when this
  build fetched it) are separate, individually optional, and never substituted
  for one another. A missing occurrence time stays ``None`` instead of being
  quietly set to the publication time.
* **Verification state is not confidence.** Both exist; neither is derived from
  the other. A high confidence score cannot make an unverified rumour confirmed.
* **Importance is not a prediction.** There is no direction, price target or
  recommendation field anywhere in this module, so the system cannot express one.
* **Categories are data.** :data:`SUBCATEGORIES` is a registry of rows; adding a
  category means adding rows, not editing the pipeline.
* **Transitions are explicit.** :data:`STATUS_TRANSITIONS` and
  :data:`VERIFICATION_TRANSITIONS` are the only legal moves; both state machines
  refuse anything else.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from novacontrol.novamarket.contracts import EventStateError, EventValidationError

#: Longest text this build will keep from one feed entry. An event description is
#: an excerpt with provenance, never a copy of a whole page.
MAX_TEXT = 4_000
#: Longest title accepted; a 4 KB "title" is a parsing failure, not a headline.
MAX_TITLE = 500


class EventCategory(StrEnum):
    """The four event families NM-04 classifies, plus the honest unknown."""

    COMPANY = "company"
    MACROECONOMIC = "macroeconomic"
    SECTOR = "sector"
    MARKET_STRUCTURE = "market_structure"
    UNCLASSIFIED = "unclassified"


class VerificationState(StrEnum):
    """How well an event's claims are supported by its evidence."""

    UNVERIFIED = "unverified"
    SINGLE_SOURCE = "single_source"
    MULTI_SOURCE_CORROBORATED = "multi_source_corroborated"
    OFFICIALLY_CONFIRMED = "officially_confirmed"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    RETRACTED = "retracted"
    EXPIRED = "expired"


class Importance(StrEnum):
    """How much attention an event deserves — never how far a price will move."""

    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class EventStatus(StrEnum):
    """The lifecycle position of an event version."""

    DISCOVERED = "discovered"
    EXTRACTED = "extracted"
    NORMALIZED = "normalized"
    VERIFICATION = "verification"
    CLASSIFIED = "classified"
    ACTIVE = "active"
    UPDATED = "updated"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    RETRACTED = "retracted"


class SourceType(StrEnum):
    """What kind of publisher a source is; drives the reliability default."""

    EXCHANGE = "exchange"
    COMPANY_IR = "company_ir"
    GOVERNMENT = "government"
    REGULATOR = "regulator"
    OFFICIAL_STATISTICS = "official_statistics"
    FINANCIAL_NEWS = "financial_news"
    SECONDARY = "secondary"


class SourceReliability(StrEnum):
    """How much an official statement from this source can establish.

    The weight is used by classification and by the verification rules; it is
    deliberately coarse, because a fake precision about publisher trust would be
    its own kind of fabrication.
    """

    OFFICIAL = "official"
    PRIMARY = "primary"
    REPUTABLE = "reputable"
    SECONDARY = "secondary"
    UNKNOWN = "unknown"

    @property
    def weight(self) -> float:
        return {
            SourceReliability.OFFICIAL: 1.0,
            SourceReliability.PRIMARY: 0.85,
            SourceReliability.REPUTABLE: 0.7,
            SourceReliability.SECONDARY: 0.5,
            SourceReliability.UNKNOWN: 0.3,
        }[self]


class EvidenceStatus(StrEnum):
    """Whether a stored evidence record was actually retrieved."""

    RETRIEVED = "retrieved"
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"
    REJECTED = "rejected"


class Relationship(StrEnum):
    """How an instrument relates to an event."""

    DIRECT = "direct"
    INDIRECT = "indirect"
    UNCERTAIN = "uncertain"


class FeedFormat(StrEnum):
    """Feed dialects this build can parse."""

    RSS = "rss"
    ATOM = "atom"
    JSON = "json"


#: Subcategories are declared, not hardcoded into the pipeline. Each row carries
#: the keywords extraction matches on, a base importance weight, a review window
#: after which an unresolved event expires, and whether the event is market-wide.
@dataclass(frozen=True, slots=True)
class SubcategoryDefinition:
    id: str
    category: EventCategory
    label: str
    keywords: tuple[str, ...]
    importance_weight: float
    expiry_days: int
    market_wide: bool = False

    def __post_init__(self) -> None:
        if not self.id or not self.id.islower():
            raise EventValidationError(
                f"a subcategory id must be lower_snake_case, got {self.id!r}"
            )
        if not 0 < self.importance_weight <= 1:
            raise EventValidationError(
                f"{self.id}: importance_weight must be in (0, 1]"
            )
        if self.expiry_days < 1:
            raise EventValidationError(f"{self.id}: expiry_days must be positive")


SUBCATEGORIES: tuple[SubcategoryDefinition, ...] = (
    # -- company -------------------------------------------------------------
    SubcategoryDefinition("earnings", EventCategory.COMPANY, "Earnings results", ("quarterly results", "annual results", "q1 results", "q2 results", "q3 results", "q4 results", "earnings", "net profit", "revenue from operations", "ebitda"), 0.8, 30),
    SubcategoryDefinition("guidance", EventCategory.COMPANY, "Earnings guidance", ("guidance", "outlook", "profit warning", "raises forecast", "cuts forecast"), 0.8, 30),
    SubcategoryDefinition("management_change", EventCategory.COMPANY, "Management change", ("appoints", "appointed", "resigns", "resignation", "chief executive", "managing director", "board of directors"), 0.5, 30),
    SubcategoryDefinition("merger_acquisition", EventCategory.COMPANY, "Mergers & acquisitions", ("acquisition", "acquires", "merger", "takeover", "stake purchase", "amalgamation", "demerger"), 0.9, 60),
    SubcategoryDefinition("restructuring", EventCategory.COMPANY, "Corporate restructuring", ("restructuring", "reorganisation", "reorganization", "turnaround", "impairment"), 0.6, 45),
    SubcategoryDefinition("buyback", EventCategory.COMPANY, "Share buyback", ("buyback", "share repurchase", "repurchase programme"), 0.6, 30),
    SubcategoryDefinition("dividend", EventCategory.COMPANY, "Dividend", ("dividend", "record date", "ex-dividend"), 0.5, 30),
    SubcategoryDefinition("stock_split", EventCategory.COMPANY, "Stock split / bonus", ("stock split", "share split", "bonus issue", "sub-division"), 0.4, 30),
    SubcategoryDefinition("fundraising", EventCategory.COMPANY, "Fundraising & debt", ("fundraise", "rights issue", "qualified institutional placement", "qip", "debenture", "bond issue", "ncd"), 0.6, 45),
    SubcategoryDefinition("product_launch", EventCategory.COMPANY, "Product launch", ("product launch", "launches", "unveils", "new product"), 0.4, 30),
    SubcategoryDefinition("regulatory_investigation", EventCategory.COMPANY, "Regulatory investigation", ("investigation", "show cause", "probe", "enforcement action", "search operation", "raid"), 0.7, 60),
    SubcategoryDefinition("litigation", EventCategory.COMPANY, "Litigation", ("lawsuit", "litigation", "court", "tribunal", "arbitration", "judgment"), 0.5, 60),
    SubcategoryDefinition("bankruptcy", EventCategory.COMPANY, "Bankruptcy & insolvency", ("bankruptcy", "insolvency", "insolvent", "resolution professional", "nclt", "winding up"), 0.9, 90),
    SubcategoryDefinition("contract", EventCategory.COMPANY, "Contracts & orders", ("order win", "bags order", "letter of award", "contract", "agreement"), 0.6, 45),
    SubcategoryDefinition("partnership", EventCategory.COMPANY, "Partnerships", ("partnership", "joint venture", "collaboration", "memorandum of understanding", "mou"), 0.5, 45),
    # -- macroeconomic -------------------------------------------------------
    SubcategoryDefinition("inflation", EventCategory.MACROECONOMIC, "Inflation", ("inflation", "consumer price index", "cpi", "wpi", "wholesale price index"), 0.9, 30, True),
    SubcategoryDefinition("interest_rate", EventCategory.MACROECONOMIC, "Interest-rate decision", ("repo rate", "interest rate", "rate cut", "rate hike", "monetary policy"), 0.95, 30, True),
    SubcategoryDefinition("central_bank", EventCategory.MACROECONOMIC, "Central-bank announcement", ("central bank", "reserve bank", "federal reserve", "european central bank", "bank of japan"), 0.7, 30, True),
    SubcategoryDefinition("gdp", EventCategory.MACROECONOMIC, "GDP release", ("gross domestic product", "gdp", "economic growth"), 0.8, 45, True),
    SubcategoryDefinition("employment", EventCategory.MACROECONOMIC, "Employment report", ("unemployment", "nonfarm payrolls", "jobs report", "payroll"), 0.7, 30, True),
    SubcategoryDefinition("fiscal_policy", EventCategory.MACROECONOMIC, "Fiscal policy", ("fiscal deficit", "fiscal policy", "stimulus", "tax rate", "taxation"), 0.7, 45, True),
    SubcategoryDefinition("budget", EventCategory.MACROECONOMIC, "Government budget", ("union budget", "budget", "finance bill", "appropriation"), 0.8, 45, True),
    SubcategoryDefinition("currency", EventCategory.MACROECONOMIC, "Currency developments", ("exchange rate", "rupee", "dollar", "forex reserves", "currency"), 0.6, 30, True),
    SubcategoryDefinition("sovereign_debt", EventCategory.MACROECONOMIC, "Sovereign debt", ("sovereign", "bond yield", "g-sec", "treasury yield", "credit rating"), 0.6, 45, True),
    SubcategoryDefinition("trade_policy", EventCategory.MACROECONOMIC, "Trade policy", ("tariff", "trade policy", "export ban", "import duty", "customs duty"), 0.7, 45, True),
    SubcategoryDefinition("geopolitics", EventCategory.MACROECONOMIC, "Geopolitical development", ("sanctions", "geopolitical", "border tension", "conflict", "war"), 0.7, 60, True),
    # -- sector --------------------------------------------------------------
    SubcategoryDefinition("sector_regulation", EventCategory.SECTOR, "Sector regulation", ("regulator", "regulatory change", "circular", "norms", "sebi", "trai", "irdai"), 0.7, 60),
    SubcategoryDefinition("demand", EventCategory.SECTOR, "Industry demand", ("demand", "order inflow", "consumption", "volume growth"), 0.5, 30),
    SubcategoryDefinition("supply_chain", EventCategory.SECTOR, "Supply chain", ("supply chain", "shortage", "disruption", "logistics", "port congestion"), 0.6, 30),
    SubcategoryDefinition("commodity_prices", EventCategory.SECTOR, "Commodity prices", ("crude oil", "brent", "gold price", "steel price", "copper", "natural gas"), 0.6, 30),
    SubcategoryDefinition("technology", EventCategory.SECTOR, "Technology development", ("technology", "artificial intelligence", "semiconductor", "chip", "5g", "innovation"), 0.5, 45),
    SubcategoryDefinition("earnings_trend", EventCategory.SECTOR, "Sector earnings trend", ("sector earnings", "industry profit", "margin pressure"), 0.5, 30),
    SubcategoryDefinition("sector_investment", EventCategory.SECTOR, "Industry investment", ("capex", "capital expenditure", "capacity expansion", "new plant"), 0.5, 45),
    SubcategoryDefinition("competition", EventCategory.SECTOR, "Competitive development", ("market share", "price war", "new entrant", "competition"), 0.4, 30),
    # -- market structure ----------------------------------------------------
    SubcategoryDefinition("exchange_announcement", EventCategory.MARKET_STRUCTURE, "Exchange announcement", ("exchange", "circular", "notice", "announcement"), 0.5, 15),
    SubcategoryDefinition("trading_halt", EventCategory.MARKET_STRUCTURE, "Trading halt", ("trading halt", "suspended from trading", "suspension", "freeze"), 0.8, 15),
    SubcategoryDefinition("listing", EventCategory.MARKET_STRUCTURE, "Listing", ("listing", "lists on", "initial public offering", "ipo"), 0.6, 30),
    SubcategoryDefinition("delisting", EventCategory.MARKET_STRUCTURE, "Delisting", ("delisting", "delisted", "compulsory delisting"), 0.8, 45),
    SubcategoryDefinition("index_change", EventCategory.MARKET_STRUCTURE, "Index inclusion/exclusion", ("index inclusion", "index exclusion", "rebalance", "nifty", "sensex", "msci", "ftse"), 0.6, 30),
    SubcategoryDefinition("liquidity", EventCategory.MARKET_STRUCTURE, "Liquidity & margins", ("liquidity", "margin requirement", "settlement"), 0.4, 15),
    SubcategoryDefinition("circuit_breaker", EventCategory.MARKET_STRUCTURE, "Circuit breaker", ("circuit breaker", "market-wide limit", "trading curb"), 0.9, 15, True),
    SubcategoryDefinition("infrastructure_incident", EventCategory.MARKET_STRUCTURE, "Market infrastructure incident", ("outage", "technical glitch", "systems down", "cyber attack"), 0.7, 20),
)

SUBCATEGORY_INDEX: Mapping[str, SubcategoryDefinition] = {
    definition.id: definition for definition in SUBCATEGORIES
}

#: Fallback for an entry no rule recognises. It is stored (that is evidence of
#: what the source said) but it is never treated as a market-moving event.
UNCLASSIFIED = SubcategoryDefinition(
    "unclassified",
    EventCategory.UNCLASSIFIED,
    "Unclassified",
    (),
    0.15,
    14,
)

#: The legal lifecycle moves. Terminal states are genuinely terminal.
STATUS_TRANSITIONS: Mapping[EventStatus, frozenset[EventStatus]] = {
    EventStatus.DISCOVERED: frozenset({EventStatus.EXTRACTED}),
    EventStatus.EXTRACTED: frozenset({EventStatus.NORMALIZED}),
    EventStatus.NORMALIZED: frozenset({EventStatus.VERIFICATION}),
    EventStatus.VERIFICATION: frozenset({EventStatus.CLASSIFIED}),
    EventStatus.CLASSIFIED: frozenset({EventStatus.ACTIVE}),
    EventStatus.ACTIVE: frozenset(
        {EventStatus.UPDATED, EventStatus.RESOLVED, EventStatus.EXPIRED, EventStatus.RETRACTED}
    ),
    EventStatus.UPDATED: frozenset(
        {EventStatus.ACTIVE, EventStatus.RESOLVED, EventStatus.EXPIRED, EventStatus.RETRACTED}
    ),
    EventStatus.RESOLVED: frozenset({EventStatus.UPDATED}),
    EventStatus.EXPIRED: frozenset({EventStatus.UPDATED}),
    EventStatus.RETRACTED: frozenset(),
}

#: The legal verification moves. ``EXPIRED`` and ``RETRACTED`` end the line except
#: that new evidence may reopen an expired event (it is a later statement about
#: the same subject), while a retraction is never silently undone.
VERIFICATION_TRANSITIONS: Mapping[VerificationState, frozenset[VerificationState]] = {
    VerificationState.UNVERIFIED: frozenset(
        {
            VerificationState.SINGLE_SOURCE,
            VerificationState.MULTI_SOURCE_CORROBORATED,
            VerificationState.OFFICIALLY_CONFIRMED,
            VerificationState.CONFLICTING_EVIDENCE,
            VerificationState.RETRACTED,
        }
    ),
    VerificationState.SINGLE_SOURCE: frozenset(
        {
            VerificationState.MULTI_SOURCE_CORROBORATED,
            VerificationState.OFFICIALLY_CONFIRMED,
            VerificationState.CONFLICTING_EVIDENCE,
            VerificationState.RETRACTED,
            VerificationState.EXPIRED,
        }
    ),
    VerificationState.MULTI_SOURCE_CORROBORATED: frozenset(
        {
            VerificationState.OFFICIALLY_CONFIRMED,
            VerificationState.CONFLICTING_EVIDENCE,
            VerificationState.RETRACTED,
            VerificationState.EXPIRED,
        }
    ),
    VerificationState.OFFICIALLY_CONFIRMED: frozenset(
        {
            VerificationState.CONFLICTING_EVIDENCE,
            VerificationState.RETRACTED,
            VerificationState.EXPIRED,
        }
    ),
    VerificationState.CONFLICTING_EVIDENCE: frozenset(
        {
            VerificationState.MULTI_SOURCE_CORROBORATED,
            VerificationState.OFFICIALLY_CONFIRMED,
            VerificationState.RETRACTED,
            VerificationState.EXPIRED,
        }
    ),
    VerificationState.EXPIRED: frozenset(
        {
            VerificationState.UNVERIFIED,
            VerificationState.SINGLE_SOURCE,
            VerificationState.MULTI_SOURCE_CORROBORATED,
            VerificationState.OFFICIALLY_CONFIRMED,
        }
    ),
    VerificationState.RETRACTED: frozenset(),
}


def ensure_transition(
    current: EventStatus | str, target: EventStatus | str
) -> tuple[EventStatus, EventStatus]:
    """Return the pair if the lifecycle move is legal, else raise."""
    source = EventStatus(current)
    destination = EventStatus(target)
    if destination not in STATUS_TRANSITIONS[source]:
        raise EventStateError(
            f"event status cannot move from {source.value} to {destination.value}"
        )
    return source, destination


def ensure_verification_transition(
    current: VerificationState | str, target: VerificationState | str
) -> tuple[VerificationState, VerificationState]:
    """Return the pair if the verification move is legal, else raise."""
    source = VerificationState(current)
    destination = VerificationState(target)
    if source is destination:
        return source, destination
    if destination not in VERIFICATION_TRANSITIONS[source]:
        raise EventStateError(
            f"verification cannot move from {source.value} to {destination.value}"
        )
    return source, destination


def _aware(value: object, *, field_name: str) -> datetime:
    """A timezone-aware UTC datetime; naive input is refused, never assumed."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise EventValidationError(
                f"{field_name} must be timezone-aware; a naive timestamp is an "
                "unknown time, not UTC"
            )
        return value.astimezone(UTC)
    raise EventValidationError(f"{field_name} must be a datetime, got {type(value).__name__}")


def optional_moment(value: object, *, field_name: str) -> datetime | None:
    if value is None or value == "":
        return None
    return _aware(value, field_name=field_name)


_WHITESPACE = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9]+")


def normalize_text(value: object) -> str:
    """Lower-case, collapse whitespace, keep the words."""
    return _WHITESPACE.sub(" ", str(value or "").strip()).lower()


def token_set(value: object) -> frozenset[str]:
    """Significant words of a text, for similarity comparisons."""
    stop = {
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "at",
        "by", "with", "from", "as", "is", "are", "was", "were", "be", "been",
        "it", "its", "this", "that", "after", "before", "over", "under",
    }
    return frozenset(
        word for word in _WORD.findall(normalize_text(value)) if word not in stop and len(word) > 1
    )


def similarity(left: object, right: object) -> float:
    """Jaccard overlap of two token sets; 0.0 when either side is empty."""
    a, b = token_set(left), token_set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def content_hash(*parts: object) -> str:
    """A stable digest of the parts that identify one source record."""
    text = "|".join(normalize_text(part) for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clip(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SourceEntry:
    """One item as a feed published it, before any interpretation."""

    title: str
    summary: str
    url: str
    published_at: datetime | None
    external_id: str = ""

    def __post_init__(self) -> None:
        title = clip(self.title, MAX_TITLE)
        if not title:
            raise EventValidationError("a feed entry needs a title")
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "summary", clip(self.summary, MAX_TEXT))
        object.__setattr__(self, "url", str(self.url or "").strip())
        object.__setattr__(self, "published_at", optional_moment(self.published_at, field_name="entry published_at"))
        object.__setattr__(self, "external_id", clip(self.external_id, 200))

    @property
    def digest(self) -> str:
        """Duplicate-detection digest: the source text and its own timestamp."""
        return content_hash(self.title, self.summary, self.published_at.isoformat() if self.published_at else "")


@dataclass(frozen=True, slots=True)
class EventSourceDefinition:
    """A configured feed. ``enabled`` is operator state, not a capability claim."""

    source_id: str
    name: str
    source_type: SourceType
    reliability: SourceReliability
    url: str
    feed_format: FeedFormat
    publisher: str
    market: str = "GLOBAL"
    description: str = ""
    enabled_default: bool = False
    #: ``True`` only once a real fetch of this exact URL has succeeded here.
    verified_live: bool = False

    def __post_init__(self) -> None:
        if not self.source_id or not self.source_id.replace("_", "").isalnum():
            raise EventValidationError(
                f"source id must be alnum/underscore, got {self.source_id!r}"
            )
        if not self.url.startswith("https://"):
            raise EventValidationError(
                f"{self.source_id}: only HTTPS sources are supported, got {self.url!r}"
            )
        object.__setattr__(self, "source_type", SourceType(self.source_type))
        object.__setattr__(self, "reliability", SourceReliability(self.reliability))
        object.__setattr__(self, "feed_format", FeedFormat(self.feed_format))
        object.__setattr__(self, "market", str(self.market or "GLOBAL").upper())

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "name": self.name,
            "source_type": self.source_type.value,
            "reliability": self.reliability.value,
            "url": self.url,
            "feed_format": self.feed_format.value,
            "publisher": self.publisher,
            "market": self.market,
            "description": self.description,
            "enabled_default": self.enabled_default,
            "verified_live": self.verified_live,
        }


@dataclass(frozen=True, slots=True)
class SourceHealth:
    """The observed state of one source, including why it is not ok."""

    source_id: str
    status: str = "never_run"
    enabled: bool = False
    last_attempt: datetime | None = None
    last_success: datetime | None = None
    consecutive_failures: int = 0
    entries_seen: int = 0
    entries_new: int = 0
    last_error: str = ""
    last_error_code: str = ""
    etag: str = ""
    unchanged: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "status": self.status,
            "enabled": self.enabled,
            "last_attempt": self.last_attempt.isoformat() if self.last_attempt else None,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "consecutive_failures": self.consecutive_failures,
            "entries_seen": self.entries_seen,
            "entries_new": self.entries_new,
            "last_error": self.last_error,
            "last_error_code": self.last_error_code,
            "unchanged": self.unchanged,
        }


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """One stored source statement supporting (or contradicting) an event."""

    evidence_id: str
    event_id: str
    source_id: str
    url: str
    publisher: str
    reliability: SourceReliability
    title: str
    excerpt: str
    content_hash: str
    independence_key: str
    retrieved_at: datetime
    published_at: datetime | None = None
    status: EvidenceStatus = EvidenceStatus.RETRIEVED
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "event_id": self.event_id,
            "source_id": self.source_id,
            "url": self.url,
            "publisher": self.publisher,
            "reliability": self.reliability.value,
            "title": self.title,
            "excerpt": self.excerpt,
            "content_hash": self.content_hash,
            "independence_key": self.independence_key,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "retrieved_at": self.retrieved_at.isoformat(),
            "status": self.status.value,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class ExtractedEvent:
    """What extraction could establish from one source entry, with its reasons."""

    title: str
    description: str
    category: EventCategory
    subcategory: str
    matched_terms: tuple[str, ...]
    entity_names: tuple[str, ...] = ()
    occurred_at: datetime | None = None
    market_wide: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "category", EventCategory(self.category))
        if self.subcategory not in SUBCATEGORY_INDEX and self.subcategory != UNCLASSIFIED.id:
            raise EventValidationError(f"unknown subcategory {self.subcategory!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "category": self.category.value,
            "subcategory": self.subcategory,
            "matched_terms": list(self.matched_terms),
            "entity_names": list(self.entity_names),
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "market_wide": self.market_wide,
        }


@dataclass(frozen=True, slots=True)
class Factor:
    """One explainable contribution to an importance or confidence score."""

    name: str
    value: float
    weight: float
    contribution: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": round(self.value, 4),
            "weight": round(self.weight, 4),
            "contribution": round(self.contribution, 4),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class Classification:
    """The importance verdict and the arithmetic behind it."""

    importance: Importance
    score: float
    confidence: float
    factors: tuple[Factor, ...]
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "importance": self.importance.value,
            "score": round(self.score, 2),
            "confidence": round(self.confidence, 3),
            "factors": [factor.to_dict() for factor in self.factors],
            "explanation": self.explanation,
            "is_price_prediction": False,
        }


@dataclass(frozen=True, slots=True)
class VerificationDecision:
    """The verification verdict, every reason, and the evidence it rests on."""

    state: VerificationState
    reasons: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    independent_sources: int
    conflicts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "reasons": list(self.reasons),
            "evidence_ids": list(self.evidence_ids),
            "independent_sources": self.independent_sources,
            "conflicts": list(self.conflicts),
        }


@dataclass(frozen=True, slots=True)
class EventMapping:
    """An evidence-based link between an event and a canonical instrument."""

    instrument_id: str
    relationship: Relationship
    reason: str
    sector: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "relationship": self.relationship.value,
            "reason": self.reason,
            "sector": self.sector,
        }


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """One immutable version of a normalized market event."""

    event_id: str
    event_key: str
    version: int
    title: str
    description: str
    category: EventCategory
    subcategory: str
    status: EventStatus
    verification: VerificationState
    importance: Importance
    confidence: float
    discovered_at: datetime
    retrieved_at: datetime
    updated_at: datetime
    published_at: datetime | None = None
    occurred_at: datetime | None = None
    market: str = "GLOBAL"
    instruments: tuple[str, ...] = ()
    sectors: tuple[str, ...] = ()
    evidence_count: int = 0
    independent_sources: int = 0
    fingerprint: str = ""
    classification: Classification | None = None
    verification_detail: VerificationDecision | None = None
    mappings: tuple[EventMapping, ...] = ()
    supersedes: int | None = None
    expiry_days: int = 30

    def __post_init__(self) -> None:
        if not self.event_id or not self.event_key:
            raise EventValidationError("an event needs an id and a key")
        if self.version < 1:
            raise EventValidationError("event version starts at 1")
        if not clip(self.title, MAX_TITLE):
            raise EventValidationError("an event needs a title")
        if not 0.0 <= self.confidence <= 1.0:
            raise EventValidationError("confidence must be within [0, 1]")
        for name, value in (
            ("discovered_at", self.discovered_at),
            ("retrieved_at", self.retrieved_at),
            ("updated_at", self.updated_at),
        ):
            _aware(value, field_name=name)
        object.__setattr__(self, "title", clip(self.title, MAX_TITLE))
        object.__setattr__(self, "description", clip(self.description, MAX_TEXT))
        object.__setattr__(self, "category", EventCategory(self.category))
        object.__setattr__(self, "status", EventStatus(self.status))
        object.__setattr__(self, "verification", VerificationState(self.verification))
        object.__setattr__(self, "importance", Importance(self.importance))
        object.__setattr__(self, "occurred_at", optional_moment(self.occurred_at, field_name="occurred_at"))
        object.__setattr__(self, "published_at", optional_moment(self.published_at, field_name="published_at"))

    def to_dict(self, *, include_history: bool = False) -> dict[str, Any]:
        # A weak symbol-inside-a-word match is not a stock relationship. Older
        # rows may still carry such mappings, so filter them on the way out as
        # well as refusing to create new ones in the mapper.
        related_mappings = tuple(
            mapping
            for mapping in self.mappings
            if mapping.relationship != Relationship.UNCERTAIN
        )
        instruments = (
            [mapping.instrument_id for mapping in related_mappings]
            if self.mappings
            else list(self.instruments)
        )
        body: dict[str, Any] = {
            "event_id": self.event_id,
            "event_key": self.event_key,
            "version": self.version,
            "title": self.title,
            "description": self.description,
            "category": self.category.value,
            "subcategory": self.subcategory,
            "status": self.status.value,
            "verification": self.verification.value,
            "importance": self.importance.value,
            "confidence": round(self.confidence, 3),
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "discovered_at": self.discovered_at.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "market": self.market,
            "instruments": instruments,
            "sectors": list(self.sectors),
            "evidence_count": self.evidence_count,
            "independent_sources": self.independent_sources,
            "fingerprint": self.fingerprint,
            "expiry_days": self.expiry_days,
            "classification": self.classification.to_dict() if self.classification else None,
            "verification_detail": (
                self.verification_detail.to_dict() if self.verification_detail else None
            ),
            "mappings": [mapping.to_dict() for mapping in related_mappings],
            "supersedes": self.supersedes,
            "is_price_prediction": False,
            "is_trading_recommendation": False,
        }
        if include_history:
            body["history_version"] = self.version
        return body


@dataclass(frozen=True, slots=True)
class EventQuery:
    """A validated event search."""

    category: EventCategory | None = None
    subcategory: str = ""
    verification: VerificationState | None = None
    importance: Importance | None = None
    instrument_id: str = ""
    sector: str = ""
    market: str = ""
    source_id: str = ""
    text: str = ""
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 50
    offset: int = 0
    latest_only: bool = True

    def __post_init__(self) -> None:
        if self.limit < 1 or self.limit > 200:
            raise EventValidationError("limit must be between 1 and 200")
        if self.offset < 0:
            raise EventValidationError("offset must not be negative")
        if self.category is not None:
            object.__setattr__(self, "category", EventCategory(self.category))
        if self.verification is not None:
            object.__setattr__(self, "verification", VerificationState(self.verification))
        if self.importance is not None:
            object.__setattr__(self, "importance", Importance(self.importance))
        if self.subcategory and self.subcategory not in SUBCATEGORY_INDEX and self.subcategory != UNCLASSIFIED.id:
            raise EventValidationError(f"unknown subcategory {self.subcategory!r}")
        if self.since is not None and self.until is not None and self.since > self.until:
            raise EventValidationError("since must not be after until")
        if self.since is not None:
            object.__setattr__(self, "since", _aware(self.since, field_name="since"))
        if self.until is not None:
            object.__setattr__(self, "until", _aware(self.until, field_name="until"))


def event_key(*, subject: str, subcategory: str, published_at: datetime | None) -> str:
    """The identity of an event, independent of which source reported it.

    Subject + subcategory + the day the claim was published. A later day's report
    of the same subject is a *version* rather than a different event only when
    the dedupe rules say so; this key is the candidate bucket, not the verdict.
    """
    day = published_at.date().isoformat() if published_at else "unknown"
    return content_hash(subject, subcategory, day)[:24]


def event_id_for(key: str) -> str:
    return f"evt_{key}"


def expiry_for(subcategory: str) -> int:
    definition = SUBCATEGORY_INDEX.get(subcategory, UNCLASSIFIED)
    return definition.expiry_days


def expiry_deadline(event: MarketEvent) -> datetime:
    """When this event stops being current, from its own category's rules."""
    base = event.published_at or event.discovered_at
    return base + timedelta(days=event.expiry_days)


__all__ = [
    "Classification",
    "EventCategory",
    "EventMapping",
    "EventQuery",
    "EventStatus",
    "EvidenceRecord",
    "EvidenceStatus",
    "ExtractedEvent",
    "Factor",
    "FeedFormat",
    "Importance",
    "MarketEvent",
    "Relationship",
    "SUBCATEGORIES",
    "SUBCATEGORY_INDEX",
    "STATUS_TRANSITIONS",
    "SourceEntry",
    "SourceHealth",
    "SourceReliability",
    "SourceType",
    "SubcategoryDefinition",
    "UNCLASSIFIED",
    "VERIFICATION_TRANSITIONS",
    "VerificationDecision",
    "VerificationState",
    "clip",
    "content_hash",
    "ensure_transition",
    "ensure_verification_transition",
    "event_id_for",
    "event_key",
    "expiry_deadline",
    "expiry_for",
    "normalize_text",
    "optional_moment",
    "similarity",
    "token_set",
    "utcnow",
]
