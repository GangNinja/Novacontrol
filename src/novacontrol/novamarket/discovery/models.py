"""NM-05 asset-discovery value types.

These are the only shapes NM-05 stores, serves or publishes. A candidate
evaluation is an immutable record of one decision: a score, the factors that
produced it, the methodology version, a digest of its inputs, and the evidence it
used. Nothing here expresses a trade, a target price or an expected return —
discovery priority is not attractiveness.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

METHODOLOGY_VERSION = "nm05-score-1.0"


class WatchlistKind(StrEnum):
    """Watchlists that behave differently, not synonyms of one list."""

    DISCOVERY = "discovery"
    ACTIVITY = "activity"
    EVENT_DRIVEN = "event_driven"
    LIQUIDITY = "liquidity"
    SECTOR = "sector"
    CUSTOM = "custom"


class EntryStatus(StrEnum):
    """The lifecycle of one membership record."""

    ACTIVE = "active"
    UNDER_REVIEW = "under_review"
    DOWNGRADED = "downgraded"
    STALE = "stale"
    SUSPENDED = "suspended"
    REMOVED = "removed"


class DataQuality(StrEnum):
    """How much the available data can support a score."""

    GOOD = "good"
    PARTIAL = "partial"
    STALE = "stale"
    INSUFFICIENT = "insufficient"


class DiscoveryReason(StrEnum):
    """Why an instrument entered the candidate set."""

    CATALOGUE = "catalogue"
    ACTIVITY = "market_activity"
    LIQUIDITY = "liquidity"
    EVENT = "event_intelligence"
    SECTOR = "sector"
    CONFIGURED = "configured"


class TriggerKind(StrEnum):
    """How a discovery cycle was started."""

    MANUAL = "manual"
    SCHEDULED = "scheduled"
    EVENT = "event"
    MAINTENANCE = "maintenance"


#: Active membership may move between states; removal is terminal, so a
#: re-added instrument is a new membership record with its own history.
ENTRY_TRANSITIONS: dict[EntryStatus, frozenset[EntryStatus]] = {
    EntryStatus.ACTIVE: frozenset(
        {
            EntryStatus.UNDER_REVIEW,
            EntryStatus.DOWNGRADED,
            EntryStatus.STALE,
            EntryStatus.SUSPENDED,
            EntryStatus.REMOVED,
        }
    ),
    EntryStatus.UNDER_REVIEW: frozenset(
        {
            EntryStatus.ACTIVE,
            EntryStatus.DOWNGRADED,
            EntryStatus.STALE,
            EntryStatus.SUSPENDED,
            EntryStatus.REMOVED,
        }
    ),
    EntryStatus.DOWNGRADED: frozenset(
        {
            EntryStatus.ACTIVE,
            EntryStatus.UNDER_REVIEW,
            EntryStatus.STALE,
            EntryStatus.SUSPENDED,
            EntryStatus.REMOVED,
        }
    ),
    EntryStatus.STALE: frozenset(
        {
            EntryStatus.ACTIVE,
            EntryStatus.UNDER_REVIEW,
            EntryStatus.DOWNGRADED,
            EntryStatus.SUSPENDED,
            EntryStatus.REMOVED,
        }
    ),
    EntryStatus.SUSPENDED: frozenset(
        {EntryStatus.ACTIVE, EntryStatus.UNDER_REVIEW, EntryStatus.REMOVED}
    ),
    EntryStatus.REMOVED: frozenset(),
}


def ensure_entry_transition(
    current: EntryStatus | str, target: EntryStatus | str
) -> tuple[EntryStatus, EntryStatus]:
    """Return the pair when the membership move is legal, else raise."""
    source = EntryStatus(current)
    destination = EntryStatus(target)
    if source is destination:
        return source, destination
    if destination not in ENTRY_TRANSITIONS[source]:
        raise ValueError(
            f"watchlist entry cannot move from {source.value} to {destination.value}"
        )
    return source, destination


@dataclass(frozen=True, slots=True)
class UniverseFilter:
    """The markets and asset classes this discovery run may scan."""

    asset_classes: tuple[str, ...] = ("equity", "etf", "index")
    exchanges: tuple[str, ...] = ()
    max_instruments: int = 200
    exclude_unknown_trading_status: bool = False

    def __post_init__(self) -> None:
        classes = tuple(
            str(item).strip().lower() for item in self.asset_classes if str(item).strip()
        )
        if not classes:
            raise ValueError("a discovery universe needs at least one asset class")
        if self.max_instruments < 1 or self.max_instruments > 2_000:
            raise ValueError("max_instruments must be between 1 and 2000")
        object.__setattr__(self, "asset_classes", classes)
        object.__setattr__(
            self,
            "exchanges",
            tuple(str(item).strip().upper() for item in self.exchanges if str(item).strip()),
        )

    def allows(self, asset_class: str, exchange: str, mic: str = "") -> bool:
        if str(asset_class).strip().lower() not in self.asset_classes:
            return False
        if self.exchanges:
            # Instruments carry both a venue name (NSE) and a MIC (XNSE).  A
            # filter declared in MICs must match either, or every real
            # instrument would be excluded from a MIC-scoped cycle.
            candidates = {
                str(exchange or "").strip().upper(),
                str(mic or "").strip().upper(),
            }
            if not candidates.intersection(self.exchanges):
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_classes": list(self.asset_classes),
            "exchanges": list(self.exchanges),
            "max_instruments": self.max_instruments,
            "exclude_unknown_trading_status": self.exclude_unknown_trading_status,
        }


@dataclass(frozen=True, slots=True)
class DiscoveryPolicy:
    """Thresholds and limits. Every one is a decision, not a magic number."""

    #: A quote older than this cannot support a high score.
    stale_after_hours: float = 72.0
    #: Fewer bars than this and activity/volatility stay unknown, not zero.
    min_history_bars: int = 3
    #: Score at or above which a candidate may enter a watchlist.
    add_threshold: float = 55.0
    #: Score below which a membership starts counting toward removal.
    remove_threshold: float = 25.0
    #: Consecutive weak evaluations before removal, so one bad day is not removal.
    remove_after_cycles: int = 3
    #: A drop this large marks a member DOWNGRADED.
    downgrade_delta: float = 12.0
    #: An improvement this large brings a member back to ACTIVE.
    upgrade_delta: float = 8.0
    max_entries_per_watchlist: int = 100
    #: Two cycles closer than this are refused unless forced.
    min_interval_seconds: int = 30
    #: Only the best candidates are auto-ranked per cycle.
    top_n: int = 25
    #: Event scores older than this contribute nothing.
    event_window_days: int = 14

    def __post_init__(self) -> None:
        if self.stale_after_hours <= 0:
            raise ValueError("stale_after_hours must be positive")
        if self.min_history_bars < 2:
            raise ValueError("min_history_bars must be at least 2")
        if not 0 <= self.remove_threshold < self.add_threshold <= 100:
            raise ValueError("thresholds must satisfy 0 <= remove < add <= 100")
        if self.remove_after_cycles < 1:
            raise ValueError("remove_after_cycles must be at least 1")
        if self.max_entries_per_watchlist < 1:
            raise ValueError("max_entries_per_watchlist must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "stale_after_hours": self.stale_after_hours,
            "min_history_bars": self.min_history_bars,
            "add_threshold": self.add_threshold,
            "remove_threshold": self.remove_threshold,
            "remove_after_cycles": self.remove_after_cycles,
            "downgrade_delta": self.downgrade_delta,
            "upgrade_delta": self.upgrade_delta,
            "max_entries_per_watchlist": self.max_entries_per_watchlist,
            "min_interval_seconds": self.min_interval_seconds,
            "top_n": self.top_n,
            "event_window_days": self.event_window_days,
        }


@dataclass(frozen=True, slots=True)
class ScoreFactor:
    """One normalized input, its weight, and what it contributed."""

    name: str
    value: float | None
    weight: float
    contribution: float
    reason: str
    missing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": None if self.value is None else round(self.value, 4),
            "weight": self.weight,
            "contribution": round(self.contribution, 3),
            "reason": self.reason,
            "missing": self.missing,
        }


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    """An immutable record of one ranking decision."""

    instrument_id: str
    score: float
    tier: str
    data_quality: DataQuality
    confidence: float
    factors: tuple[ScoreFactor, ...]
    missing_fields: tuple[str, ...]
    event_ids: tuple[str, ...]
    discovery_reason: DiscoveryReason
    methodology_version: str
    evaluated_at: datetime
    digest: str
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 100.0:
            raise ValueError("score must be within [0, 100]")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within [0, 1]")
        if self.data_quality is DataQuality.INSUFFICIENT and self.score > 15.0:
            raise ValueError("insufficient data may not carry a high score")
        if self.data_quality is DataQuality.STALE and self.score > 35.0:
            raise ValueError("stale data may not carry a high score")

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "score": round(self.score, 2),
            "tier": self.tier,
            "data_quality": self.data_quality.value,
            "confidence": round(self.confidence, 3),
            "factors": [factor.to_dict() for factor in self.factors],
            "missing_fields": list(self.missing_fields),
            "event_ids": list(self.event_ids),
            "discovery_reason": self.discovery_reason.value,
            "methodology_version": self.methodology_version,
            "evaluated_at": self.evaluated_at.isoformat(),
            "digest": self.digest,
            "notes": list(self.notes),
            "is_trading_recommendation": False,
            "is_price_prediction": False,
        }


@dataclass(frozen=True, slots=True)
class WatchlistRecord:
    watchlist_id: str
    name: str
    kind: WatchlistKind
    created_at: datetime
    updated_at: datetime
    description: str = ""
    custom: bool = False
    entries: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "watchlist_id": self.watchlist_id,
            "name": self.name,
            "kind": self.kind.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "description": self.description,
            "custom": self.custom,
            "entries": self.entries,
        }


@dataclass(frozen=True, slots=True)
class WatchlistEntry:
    entry_id: str
    watchlist_id: str
    instrument_id: str
    status: EntryStatus
    added_at: datetime
    updated_at: datetime
    reason: str
    score: float
    last_evaluated_at: datetime | None = None
    status_reason: str = ""
    event_ids: tuple[str, ...] = ()
    below_threshold_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "watchlist_id": self.watchlist_id,
            "instrument_id": self.instrument_id,
            "status": self.status.value,
            "added_at": self.added_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "reason": self.reason,
            "score": round(self.score, 2),
            "last_evaluated_at": (
                self.last_evaluated_at.isoformat() if self.last_evaluated_at else None
            ),
            "status_reason": self.status_reason,
            "event_ids": list(self.event_ids),
            "below_threshold_count": self.below_threshold_count,
        }


@dataclass(frozen=True, slots=True)
class CycleReport:
    cycle_id: str
    trigger: TriggerKind
    status: str
    started_at: datetime
    finished_at: datetime
    scanned: int = 0
    eligible: int = 0
    evaluated: int = 0
    created: int = 0
    pruned: int = 0
    added: int = 0
    upgraded: int = 0
    downgraded: int = 0
    removed: int = 0
    skipped: int = 0
    failed_sources: int = 0
    duration_ms: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "trigger": self.trigger.value,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "scanned": self.scanned,
            "eligible": self.eligible,
            "evaluated": self.evaluated,
            "created": self.created,
            "pruned": self.pruned,
            "added": self.added,
            "upgraded": self.upgraded,
            "downgraded": self.downgraded,
            "removed": self.removed,
            "skipped": self.skipped,
            "failed_sources": self.failed_sources,
            "duration_ms": self.duration_ms,
            "errors": list(self.errors),
        }


def entry_id_for(watchlist_id: str, instrument_id: str) -> str:
    return "wle_" + hashlib.sha256(
        f"{watchlist_id}|{instrument_id}".encode("utf-8")
    ).hexdigest()[:20]


def watchlist_id_for(kind: WatchlistKind, name: str) -> str:
    return "wl_" + hashlib.sha256(
        f"{kind.value}|{name.strip().lower()}".encode("utf-8")
    ).hexdigest()[:16]


def digest_inputs(payload: dict[str, Any]) -> str:
    """A stable digest of the inputs behind one score."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def utcnow() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "ENTRY_TRANSITIONS",
    "METHODOLOGY_VERSION",
    "CandidateEvaluation",
    "CycleReport",
    "DataQuality",
    "DiscoveryPolicy",
    "DiscoveryReason",
    "EntryStatus",
    "ScoreFactor",
    "TriggerKind",
    "UniverseFilter",
    "WatchlistEntry",
    "WatchlistKind",
    "WatchlistRecord",
    "digest_inputs",
    "ensure_entry_transition",
    "entry_id_for",
    "utcnow",
    "watchlist_id_for",
]
