"""PI-03 low-latency market-data gateway.

This module is the controlled acquisition and distribution boundary for
market observations.  It deliberately composes the existing NM-02 authority:

* provider adapters, transport, retries, rate limits, daily quotas and circuit
  breakers stay in ``marketdata.providers``;
* normalization, validation, freshness, quality and persistence stay in
  ``MarketDataEngine`` and ``MarketDataStore``;
* historical reads stay on the engine/PI-02 store, never a second database.

What the gateway adds is the *policy* around that pipeline: a separate
intraday cycle, priority subscriptions with fair rotation, request coalescing,
a short read cache, checkpoint semantics for forecast confirmation, provider
contracts and measured latency/rate-limit evidence.
"""

from __future__ import annotations

import asyncio
import math
import statistics
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, cast

from novacontrol.novamarket.contracts import (
    ConfigurationError,
    MarketDataError,
)
from novacontrol.novamarket.marketdata.calendar import SessionState, calendar_for
from novacontrol.novamarket.marketdata.engine import MarketDataEngine
from novacontrol.novamarket.marketdata.models import (
    DataInterval,
    DataQuality,
    Freshness,
    PriceObservation,
)

#: The existing general refresh cadence.  The gateway never mutates it; this
#: constant is reported as evidence that the intraday path is separate.
GENERAL_REFRESH_FLOOR_SECONDS = 900
#: The existing discovery/batch ceiling.  A later phase may not raise it by
#: configuration.
DISCOVERY_BATCH_CAP = 20
MAX_SUBSCRIPTIONS = 10_000


class AcquisitionPriority(StrEnum):
    """Legitimate application state, highest urgency first."""

    ACTIVE_POSITION = "active_position"
    PENDING_PAPER_ORDER = "pending_paper_order"
    FORECAST_CONFIRMATION = "forecast_confirmation"
    AUTONOMOUS_CANDIDATE = "autonomous_candidate"
    WATCHLIST = "watchlist"
    GENERAL = "general"

    @property
    def rank(self) -> int:
        return _PRIORITY_RANK[self]


_PRIORITY_ORDER = (
    AcquisitionPriority.ACTIVE_POSITION,
    AcquisitionPriority.PENDING_PAPER_ORDER,
    AcquisitionPriority.FORECAST_CONFIRMATION,
    AcquisitionPriority.AUTONOMOUS_CANDIDATE,
    AcquisitionPriority.WATCHLIST,
    AcquisitionPriority.GENERAL,
)
_PRIORITY_RANK = {item: index for index, item in enumerate(_PRIORITY_ORDER)}


class ProviderFailureClass(StrEnum):
    NONE = "none"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    AUTHENTICATION = "authentication"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"
    SYMBOL_NOT_FOUND = "symbol_not_found"
    UNSUPPORTED = "unsupported"
    CIRCUIT_OPEN = "circuit_open"
    UNKNOWN = "unknown"


_FAILURE_BY_TYPE: dict[str, ProviderFailureClass] = {
    "ProviderRateLimitedError": ProviderFailureClass.RATE_LIMITED,
    "ProviderTimeoutError": ProviderFailureClass.TIMEOUT,
    "ProviderAuthenticationError": ProviderFailureClass.AUTHENTICATION,
    "ProviderUnavailableError": ProviderFailureClass.UNAVAILABLE,
    "ProviderInvalidResponseError": ProviderFailureClass.INVALID_RESPONSE,
    "ProviderSymbolNotFoundError": ProviderFailureClass.SYMBOL_NOT_FOUND,
    "ProviderUnsupportedIntervalError": ProviderFailureClass.UNSUPPORTED,
    "CircuitOpenError": ProviderFailureClass.CIRCUIT_OPEN,
}


def classify_failure(error_type: str | None, message: str = "") -> ProviderFailureClass:
    """Map the existing typed provider failures to gateway categories."""

    if error_type in _FAILURE_BY_TYPE:
        return _FAILURE_BY_TYPE[error_type]
    lowered = str(message or "").lower()
    if "429" in lowered or "rate limit" in lowered or "too many" in lowered:
        return ProviderFailureClass.RATE_LIMITED
    if "timed out" in lowered or "timeout" in lowered:
        return ProviderFailureClass.TIMEOUT
    if "circuit" in lowered:
        return ProviderFailureClass.CIRCUIT_OPEN
    if error_type:
        return ProviderFailureClass.UNKNOWN
    return ProviderFailureClass.NONE


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    """Explicit, bounded policy for the separate intraday path.

    The general worker's 900-second floor is not represented as a knob this
    object can lower.  ``general_refresh_floor_seconds`` exists for the
    contract/status and must remain at least the existing floor.
    """

    enabled: bool = False
    interval_seconds: int = 60
    intervals: tuple[str, ...] = (DataInterval.MINUTE_1.value,)
    cache_ttl_seconds: int = 15
    max_instruments_per_batch: int = DISCOVERY_BATCH_CAP
    lower_priority_reserved_slots: int = 2
    max_requests_per_minute: int = 20
    run_when_closed: bool = False
    general_refresh_floor_seconds: int = GENERAL_REFRESH_FLOOR_SECONDS

    LIMITS: Mapping[str, tuple[int, int]] = field(
        default_factory=lambda: {
            "interval_seconds": (30, 900),
            "cache_ttl_seconds": (0, 300),
            "max_instruments_per_batch": (1, DISCOVERY_BATCH_CAP),
            "lower_priority_reserved_slots": (0, 10),
            "max_requests_per_minute": (1, 100),
            "general_refresh_floor_seconds": (GENERAL_REFRESH_FLOOR_SECONDS, 86_400),
        },
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool) or not isinstance(self.run_when_closed, bool):
            raise ConfigurationError("gateway enabled/run_when_closed must be booleans")
        for name, (low, high) in self.LIMITS.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigurationError(f"gateway.{name} must be an integer")
            if not low <= value <= high:
                raise ConfigurationError(
                    f"gateway.{name} must be between {low} and {high}, got {value}"
                )
        if self.lower_priority_reserved_slots >= self.max_instruments_per_batch:
            raise ConfigurationError(
                "gateway.lower_priority_reserved_slots must be smaller than the batch cap"
            )
        if self.interval_seconds >= self.general_refresh_floor_seconds:
            raise ConfigurationError(
                "gateway.interval_seconds must be shorter than the preserved "
                "general refresh floor"
            )
        intervals = self.intervals
        if isinstance(intervals, str) or not isinstance(intervals, (tuple, list)):
            raise ConfigurationError("gateway.intervals must be a list")
        if not intervals:
            raise ConfigurationError("gateway.intervals must not be empty")
        cleaned: list[str] = []
        for value in intervals:
            try:
                interval = DataInterval(value)
            except ValueError as exc:
                raise ConfigurationError(f"unknown gateway interval: {value!r}") from exc
            if not (interval.is_intraday or interval is DataInterval.SPOT):
                raise ConfigurationError(
                    "the intraday gateway accepts only intraday resolutions or spot"
                )
            if interval.value in cleaned:
                raise ConfigurationError(f"duplicate gateway interval: {interval.value}")
            cleaned.append(interval.value)
        if len(cleaned) > 4:
            raise ConfigurationError("at most four intraday resolutions may be configured")
        object.__setattr__(self, "intervals", tuple(cleaned))

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> GatewaySettings:
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ConfigurationError("gateway must be an object")
        defaults = cls()
        allowed = set(defaults.to_dict()) | {"intervals"}
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ConfigurationError(
                f"unknown gateway field(s): {', '.join(unknown)}; "
                f"allowed: {', '.join(sorted(allowed))}"
            )
        return cls(
            enabled=data.get("enabled", defaults.enabled),
            interval_seconds=data.get("interval_seconds", defaults.interval_seconds),
            intervals=tuple(data.get("intervals", defaults.intervals)),
            cache_ttl_seconds=data.get("cache_ttl_seconds", defaults.cache_ttl_seconds),
            max_instruments_per_batch=data.get(
                "max_instruments_per_batch", defaults.max_instruments_per_batch
            ),
            lower_priority_reserved_slots=data.get(
                "lower_priority_reserved_slots", defaults.lower_priority_reserved_slots
            ),
            max_requests_per_minute=data.get(
                "max_requests_per_minute", defaults.max_requests_per_minute
            ),
            run_when_closed=data.get("run_when_closed", defaults.run_when_closed),
            general_refresh_floor_seconds=data.get(
                "general_refresh_floor_seconds", defaults.general_refresh_floor_seconds
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "interval_seconds": self.interval_seconds,
            "intervals": list(self.intervals),
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "max_instruments_per_batch": self.max_instruments_per_batch,
            "lower_priority_reserved_slots": self.lower_priority_reserved_slots,
            "max_requests_per_minute": self.max_requests_per_minute,
            "run_when_closed": self.run_when_closed,
            "general_refresh_floor_seconds": self.general_refresh_floor_seconds,
        }

    @staticmethod
    def schema() -> dict[str, Any]:
        return {
            "fields": {
                "enabled": {"type": "boolean", "default": False},
                "interval_seconds": {"type": "integer", "min": 30, "max": 900},
                "intervals": {
                    "type": "list[string]",
                    "allowed": [
                        DataInterval.MINUTE_1.value,
                        DataInterval.MINUTE_5.value,
                        DataInterval.MINUTE_15.value,
                        DataInterval.MINUTE_20.value,
                        DataInterval.MINUTE_30.value,
                        DataInterval.HOUR_1.value,
                        DataInterval.SPOT.value,
                    ],
                },
                "cache_ttl_seconds": {"type": "integer", "min": 0, "max": 300},
                "max_instruments_per_batch": {"type": "integer", "max": 20},
                "lower_priority_reserved_slots": {"type": "integer", "max": 10},
                "max_requests_per_minute": {"type": "integer", "max": 100},
                "run_when_closed": {"type": "boolean", "default": False},
                "general_refresh_floor_seconds": {"type": "integer", "min": 900},
            },
            "rules": (
                "The intraday path is opt-in and separate from the 900-second "
                "general worker. It never raises a provider rate limit, never "
                "increases the 20-instrument cap and never presents cached data "
                "as fresh."
            ),
        }


@dataclass(frozen=True, slots=True)
class Subscription:
    instrument_id: str
    priority: AcquisitionPriority = AcquisitionPriority.GENERAL
    reason: str = ""
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        key = str(self.instrument_id or "").strip()
        if not key:
            raise ConfigurationError("a gateway subscription needs an instrument id")
        object.__setattr__(self, "instrument_id", key)
        object.__setattr__(self, "priority", AcquisitionPriority(self.priority))
        object.__setattr__(self, "reason", str(self.reason or "").strip())
        if self.created_at is not None:
            object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "priority": self.priority.value,
            "priority_rank": self.priority.rank,
            "reason": self.reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


@dataclass(frozen=True, slots=True)
class GatewayObservation:
    """One gateway-delivered observation with provenance and measured timings."""

    instrument_id: str
    path: str
    provider: str = ""
    price: Decimal | None = None
    currency: str = ""
    resolution: str = DataInterval.SPOT.value
    quality: DataQuality = DataQuality.UNAVAILABLE
    freshness: Freshness = Freshness.MISSING
    available: bool = False
    usable: bool = False
    fresh: bool = False
    stale: bool = False
    provider_conflict: bool = False
    is_fallback: bool = False
    fallback_reason: str = ""
    source_class: str = "unknown"
    observation_id: str = ""
    observed_at: datetime | None = None
    published_at: datetime | None = None
    provider_response_at: datetime | None = None
    received_at: datetime | None = None
    validated_at: datetime | None = None
    persisted_at: datetime | None = None
    available_at: datetime | None = None
    reasons: tuple[str, ...] = ()
    latency: Mapping[str, float | None] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "path": self.path,
            "provider": self.provider,
            "price": format(self.price, "f") if self.price is not None else None,
            "currency": self.currency,
            "resolution": self.resolution,
            "quality": self.quality.value,
            "freshness": self.freshness.value,
            "available": self.available,
            "usable": self.usable,
            "fresh": self.fresh,
            "stale": self.stale,
            "provider_conflict": self.provider_conflict,
            "is_fallback": self.is_fallback,
            "fallback_reason": self.fallback_reason,
            "source_class": self.source_class,
            "observation_id": self.observation_id,
            "timestamps": {
                "source_observed_at": _iso(self.observed_at),
                "provider_published_at": _iso(self.published_at),
                "provider_response_at": _iso(self.provider_response_at),
                "received_at": _iso(self.received_at),
                "validated_at": _iso(self.validated_at),
                "persisted_at": _iso(self.persisted_at),
                "available_at": _iso(self.available_at),
            },
            "latency": dict(self.latency),
            "reasons": list(self.reasons),
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class GatewayCycle:
    cycle_id: str
    path: str
    started_at: datetime
    finished_at: datetime
    requested: tuple[str, ...]
    selected: tuple[str, ...]
    skipped: tuple[str, ...]
    trigger: str
    report: Mapping[str, Any] | None = None
    error: str = ""

    @property
    def duration_ms(self) -> float:
        return max((self.finished_at - self.started_at).total_seconds() * 1000.0, 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "path": self.path,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_ms": round(self.duration_ms, 3),
            "requested": list(self.requested),
            "selected": list(self.selected),
            "skipped": list(self.skipped),
            "trigger": self.trigger,
            "report": dict(self.report) if self.report else None,
            "error": self.error,
        }


@dataclass(slots=True)
class _CacheEntry:
    inserted: float
    result: Any
    freshness: Any


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ConfigurationError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ConfigurationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(item) for item in values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)


def _summary(values: Sequence[float]) -> dict[str, Any]:
    clean = [float(item) for item in values if item is not None]
    return {
        "count": len(clean),
        "p50": _percentile(clean, 0.50),
        "p95": _percentile(clean, 0.95),
        "p99": _percentile(clean, 0.99),
        "mean": round(statistics.fmean(clean), 3) if clean else None,
    }


class MarketDataGateway:
    """The single controlled market-data access path for new consumers."""

    def __init__(
        self,
        engine: MarketDataEngine,
        *,
        settings: GatewaySettings | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if engine is None:
            raise ConfigurationError("the gateway requires the market-data engine")
        self.engine = engine
        self.settings = settings or GatewaySettings()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self._subscriptions: dict[str, Subscription] = {}
        self._last_acquired: dict[str, datetime] = {}
        self._rotation_cursors: dict[AcquisitionPriority, int] = {
            priority: 0 for priority in _PRIORITY_ORDER
        }
        self._cache: dict[tuple[str, str], _CacheEntry] = {}
        self._inflight: dict[tuple[str, str], asyncio.Task[Any]] = {}
        self._cache_hits = 0
        self._cache_misses = 0
        # Gateway-level instrument-request budget. Provider adapters still own
        # their exact HTTP rate limits; this prevents the scheduler/on-demand
        # callers from spending more than the configured acquisition budget in
        # one minute, and never turns a 429 into an unbounded retry loop.
        self._request_times: deque[float] = deque()
        self._coalesced = 0
        self._cycles: list[GatewayCycle] = []
        self._cycle_durations_ms: deque[float] = deque(maxlen=500)
        self._source_ages_seconds: deque[float] = deque(maxlen=500)
        self._source_to_reception_seconds: deque[float] = deque(maxlen=500)
        self._source_publication_delays_seconds: deque[float] = deque(maxlen=500)
        self._persistence_ms: deque[float] = deque(maxlen=500)
        self._distribution_ms: deque[float] = deque(maxlen=500)
        self._last_cycle: GatewayCycle | None = None
        self._last_error = ""
        self._counters: dict[str, int] = {
            "reads": 0,
            "fresh_reads": 0,
            "stale_reads": 0,
            "missing_observations": 0,
            "fallback_reads": 0,
            "conflict_reads": 0,
            "checkpoint_available": 0,
            "checkpoint_unavailable": 0,
            "stale_rejections": 0,
        }
        self._instrument_status: dict[str, dict[str, Any]] = {}
        self._worker_task: asyncio.Task[Any] | None = None
        self._stop_event: asyncio.Event | None = None
        self._started_at: datetime | None = None
        self._refresh_lock = asyncio.Lock()
        self._cycle_lock = asyncio.Lock()

    def reconfigure(self, settings: GatewaySettings) -> None:
        """Apply validated gateway policy without replacing the engine/queue."""
        self.settings = settings
        self._cache.clear()

    def _budget_remaining(self) -> int:
        now = self._monotonic()
        while self._request_times and now - self._request_times[0] >= 60.0:
            self._request_times.popleft()
        return max(self.settings.max_requests_per_minute - len(self._request_times), 0)

    def _spend_budget(self, count: int) -> None:
        if count <= 0:
            return
        if count > self._budget_remaining():
            raise MarketDataError(
                "the gateway acquisition budget for this minute is exhausted"
            )
        now = self._monotonic()
        self._request_times.extend([now] * count)

    # -- subscriptions and priority ------------------------------------------

    def subscribe(
        self,
        instrument_id: str,
        *,
        priority: AcquisitionPriority | str = AcquisitionPriority.GENERAL,
        reason: str = "",
    ) -> dict[str, Any]:
        subscription = Subscription(
            instrument_id=instrument_id,
            priority=(
                priority
                if isinstance(priority, AcquisitionPriority)
                else AcquisitionPriority(str(priority).strip().lower())
            ),
            reason=reason,
            created_at=self._clock(),
        )
        self.engine.get_instrument(subscription.instrument_id)
        if len(self._subscriptions) >= MAX_SUBSCRIPTIONS and (
            subscription.instrument_id not in self._subscriptions
        ):
            raise ConfigurationError(
                f"at most {MAX_SUBSCRIPTIONS} gateway subscriptions are supported"
            )
        self._subscriptions[subscription.instrument_id] = subscription
        return subscription.to_dict()

    def unsubscribe(self, instrument_id: str) -> bool:
        removed = self._subscriptions.pop(str(instrument_id).strip(), None)
        if removed is not None:
            self.invalidate(instrument_id)
        return removed is not None

    def prioritize(
        self,
        instrument_id: str,
        priority: AcquisitionPriority | str,
        *,
        reason: str = "",
    ) -> dict[str, Any]:
        return self.subscribe(instrument_id, priority=priority, reason=reason)

    def subscriptions(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            item.to_dict()
            for item in sorted(
                self._subscriptions.values(),
                key=lambda row: (row.priority.rank, row.instrument_id),
            )
        )

    def _candidates(self, instrument_ids: Sequence[str] | None) -> tuple[str, ...]:
        if instrument_ids is not None:
            requested = tuple(
                dict.fromkeys(
                    str(item).strip() for item in instrument_ids if str(item).strip()
                )
            )
            return requested
        if self._subscriptions:
            return tuple(
                item.instrument_id
                for item in sorted(
                    self._subscriptions.values(),
                    key=lambda row: (row.priority.rank, row.instrument_id),
                )
            )
        return tuple(self.engine.universe)

    def _rotate_take(
        self, priority: AcquisitionPriority, group: Sequence[str], count: int
    ) -> list[str]:
        if count <= 0 or not group:
            return []
        count = min(count, len(group))
        start = self._rotation_cursors.get(priority, 0) % len(group)
        doubled = list(group) + list(group)
        window = doubled[start : start + count]
        self._rotation_cursors[priority] = (start + count) % len(group)
        return window

    def select_batch(
        self,
        instrument_ids: Sequence[str] | None = None,
        *,
        limit: int | None = None,
    ) -> tuple[str, ...]:
        """Select a deterministic, fair, priority-aware batch.

        High-priority state receives the bulk of the batch.  A small reserved
        slice always goes to the lower-priority queue, and every queue rotates,
        so a large active-position book cannot starve watchlist/general names.
        """
        requested_limit = (
            self.settings.max_instruments_per_batch
            if limit is None
            else int(limit)
        )
        capacity = min(
            max(requested_limit, 0),
            self.settings.max_instruments_per_batch,
            DISCOVERY_BATCH_CAP,
        )
        candidates = self._candidates(instrument_ids)
        if not candidates or capacity <= 0:
            return ()
        groups: dict[AcquisitionPriority, list[str]] = {}
        for key in candidates:
            subscription = self._subscriptions.get(key)
            priority = subscription.priority if subscription else AcquisitionPriority.GENERAL
            groups.setdefault(priority, []).append(key)
        levels = [priority for priority in _PRIORITY_ORDER if groups.get(priority)]
        if not levels:
            return ()
        selected: list[str] = []
        if len(levels) == 1:
            selected.extend(self._rotate_take(levels[0], groups[levels[0]], capacity))
            return tuple(selected)

        reserved = min(self.settings.lower_priority_reserved_slots, capacity - 1)
        primary_quota = capacity - reserved
        primary = levels[0]
        selected.extend(self._rotate_take(primary, groups[primary], primary_quota))

        # Fill unused primary capacity from the next levels before spending the
        # reserved slice, so a quiet book does not leave the batch half empty.
        for level in levels[1:]:
            if len(selected) >= primary_quota:
                break
            selected.extend(
                self._rotate_take(level, groups[level], primary_quota - len(selected))
            )

        # The reserved slice rotates across ALL lower levels as one queue so a
        # large watchlist cannot permanently occupy it.
        lower = [
            item
            for level in levels[1:]
            for item in groups[level]
            if item not in selected
        ]
        lower.sort(
            key=lambda item: (
                self._subscriptions.get(item, Subscription(item)).priority.rank,
                self._last_acquired.get(item, datetime.min.replace(tzinfo=UTC)),
                item,
            )
        )
        if lower and reserved:
            start = sum(self._rotation_cursors.values()) % len(lower)
            window = (lower + lower)[start : start + min(reserved, len(lower))]
            selected.extend(item for item in window if item not in selected)
            self._rotation_cursors[levels[-1]] = (
                self._rotation_cursors.get(levels[-1], 0) + len(window)
            ) % max(len(groups[levels[-1]]), 1)
        if len(selected) < capacity:
            for item in candidates:
                if item not in selected:
                    selected.append(item)
                if len(selected) >= capacity:
                    break
        return tuple(selected[:capacity])

    # -- reads ---------------------------------------------------------------

    def _cache_key(self, instrument_id: str, provider: str | None) -> tuple[str, str]:
        return (str(instrument_id).strip(), str(provider or "").strip().lower())

    def _cache_get(
        self, key: tuple[str, str]
    ) -> tuple[Any, Any] | None:
        ttl = self.settings.cache_ttl_seconds
        entry = self._cache.get(key)
        if entry is None:
            self._cache_misses += 1
            return None
        if ttl <= 0 or self._monotonic() - entry.inserted > ttl:
            self._cache.pop(key, None)
            self._cache_misses += 1
            return None
        self._cache_hits += 1
        return entry.result, entry.freshness

    def _read_raw(
        self, instrument_id: str, provider: str | None
    ) -> tuple[Any, Any]:
        key = self._cache_key(instrument_id, provider)
        cached = self._cache_get(key)
        if cached is not None:
            # The stored observation/result may be cached, but its age is a
            # function of the clock: recompute freshness on every read so a
            # cached entry can never keep presenting an old quote as fresh.
            freshness = self.engine.freshness_of(instrument_id, provider=provider)
            return cached[0], freshness
        result = self.engine.latest(instrument_id, provider=provider)
        freshness = self.engine.freshness_of(instrument_id, provider=provider)
        self._cache[key] = _CacheEntry(self._monotonic(), result, freshness)
        return result, freshness

    def invalidate(self, instrument_id: str) -> None:
        key = str(instrument_id).strip()
        for cache_key in tuple(self._cache):
            if cache_key[0] == key:
                self._cache.pop(cache_key, None)

    def _stored_at(self, observation_id: str) -> datetime | None:
        getter = getattr(self.engine.store, "observation_stored_at", None)
        if getter is None:
            return None
        try:
            value = getter(observation_id)
            return cast("datetime | None", value)
        except Exception:  # noqa: BLE001 - timing metadata must not break a read
            return None

    def _source_class(self, provider: str) -> str:
        try:
            adapter = self.engine.registry.get(provider)
            caps = adapter.capabilities
            return str(getattr(caps, "source_class", "unknown"))
        except Exception:  # noqa: BLE001 - an old provider still has history
            return "unknown"

    def _provider_supports(
        self, provider: str, instrument_id: str
    ) -> bool:
        """Whether an enabled provider declares this instrument's market."""
        try:
            instrument = self.engine.get_instrument(instrument_id)
            caps = self.engine.registry.get(provider).capabilities
            if instrument.asset_class not in caps.asset_classes:
                return False
            if caps.covers_other_exchanges:
                return True
            return (
                instrument.exchange in caps.exchanges
                or instrument.mic in caps.exchanges
            )
        except Exception:  # noqa: BLE001 - unknown metadata is not a selection claim
            return False

    def _publication_time(self, observation: PriceObservation) -> datetime | None:
        for key in ("published_at", "publication_time", "publication_timestamp"):
            parsed = _parse_time(observation.provider_metadata.get(key))
            if parsed is not None:
                return parsed
        return None

    def _envelope(
        self,
        instrument_id: str,
        result: Any,
        freshness: Any,
        *,
        path: str,
    ) -> GatewayObservation:
        now = self._clock()
        selected = result.selected if getattr(result, "status", "") == "ok" else None
        observations = tuple(getattr(result, "observations", ()) or ())
        reasons = list(getattr(result, "reasons", ()) or ())
        conflict = bool(
            getattr(getattr(result, "status", ""), "value", result.status) == "conflict"
        )
        self._counters["reads"] += 1
        if selected is None:
            if conflict:
                self._counters["conflict_reads"] += 1
            else:
                self._counters["missing_observations"] += 1
            self._instrument_status[instrument_id] = {
                "freshness": getattr(freshness, "freshness", Freshness.MISSING).value,
                "quality": DataQuality.UNAVAILABLE.value,
                "provider": "",
                "available": False,
                "fresh": False,
                "stale": False,
                "is_fallback": False,
                "observed_at": None,
                "read_at": now.isoformat(),
                "reason": "provider conflict" if conflict else "no observation",
            }
            providers = sorted({item.provider for item in observations})
            conflict_detail = [
                {
                    "provider": item.provider,
                    "price": format(item.price, "f"),
                    "currency": item.currency,
                    "observed_at": _iso(item.observed_at),
                    "quality": item.quality.value,
                }
                for item in observations
            ]
            return GatewayObservation(
                instrument_id=instrument_id,
                path=path,
                provider=providers[0] if len(providers) == 1 else "",
                quality=DataQuality.UNAVAILABLE,
                freshness=getattr(freshness, "freshness", Freshness.MISSING),
                provider_conflict=conflict,
                available_at=now,
                reasons=tuple(dict.fromkeys(reasons)),
                provenance={
                    "path": path,
                    "observation_count": len(observations),
                    "providers": providers,
                    "conflict_observations": conflict_detail,
                },
            )
        stored_at = self._stored_at(selected.observation_id)
        health = None
        try:
            health = self.engine.provider_health(selected.provider)
        except Exception:  # noqa: BLE001
            health = None
        priority = tuple(self.engine.enabled_providers)
        # Provider selection is configuration-ordered.  A selected provider
        # other than the first enabled provider that declares this instrument's
        # market is a fallback, even when the preferred provider produced no row
        # at all (its failure is the reason).  A provider that does not support
        # the market must not make a legitimate source look like a fallback.
        preferred = next(
            (
                name
                for name in priority
                if self._provider_supports(name, instrument_id)
            ),
            selected.provider,
        )
        is_fallback = bool(preferred and preferred != selected.provider)
        fallback_reason = ""
        if is_fallback:
            preferred_health = None
            try:
                preferred_health = self.engine.provider_health(preferred)
            except Exception:  # noqa: BLE001
                preferred_health = None
            last_error = (
                str(getattr(preferred_health, "last_error", "") or "")
                if preferred_health
                else ""
            )
            fallback_reason = (
                f"preferred provider {preferred} did not supply the selected "
                f"observation" + (f": {last_error}" if last_error else "")
            )
        quality = selected.quality
        fresh = getattr(freshness, "freshness", Freshness.UNKNOWN) is Freshness.FRESH
        stale = getattr(freshness, "freshness", Freshness.UNKNOWN) is Freshness.STALE
        usable = bool(selected.usable) and not conflict
        if fresh:
            self._counters["fresh_reads"] += 1
        if stale:
            self._counters["stale_reads"] += 1
        if is_fallback:
            self._counters["fallback_reads"] += 1
        if conflict:
            self._counters["conflict_reads"] += 1
        self._instrument_status[instrument_id] = {
            "freshness": getattr(freshness, "freshness", Freshness.UNKNOWN).value,
            "quality": quality.value,
            "provider": selected.provider,
            "available": usable,
            "fresh": fresh,
            "stale": stale,
            "is_fallback": is_fallback,
            "observed_at": _iso(selected.observed_at),
            "read_at": now.isoformat(),
        }
        source_age = max((now - selected.observed_at).total_seconds(), 0.0)
        self._source_ages_seconds.append(source_age)
        source_to_reception = max(
            (selected.retrieved_at - selected.observed_at).total_seconds(), 0.0
        )
        self._source_to_reception_seconds.append(source_to_reception)
        persistence_ms = (
            max((stored_at - selected.retrieved_at).total_seconds() * 1000.0, 0.0)
            if stored_at is not None
            else None
        )
        if persistence_ms is not None:
            self._persistence_ms.append(persistence_ms)
        distribution_ms = max((now - (stored_at or selected.retrieved_at)).total_seconds() * 1000.0, 0.0)
        self._distribution_ms.append(distribution_ms)
        published_at = self._publication_time(selected)
        publication_delay = (
            max((selected.observed_at - published_at).total_seconds(), 0.0)
            if published_at is not None
            else None
        )
        if publication_delay is not None:
            self._source_publication_delays_seconds.append(publication_delay)
        latency = {
            "source_age_seconds": round(source_age, 3),
            "source_to_reception_seconds": round(source_to_reception, 3),
            "provider_request_ms": (
                round(float(health.latency_ms), 3)
                if health is not None and health.latency_ms is not None
                else None
            ),
            "persistence_ms": (
                round(persistence_ms, 3) if persistence_ms is not None else None
            ),
            "distribution_ms": round(distribution_ms, 3),
            "total_internal_ms": round(
                max((now - selected.retrieved_at).total_seconds() * 1000.0, 0.0), 3
            ),
            "source_publication_delay_seconds": (
                round(publication_delay, 3)
                if publication_delay is not None
                else None
            ),
        }
        reason_list = list(selected.reasons) + list(getattr(freshness, "reasons", ()) or ()) + reasons
        if is_fallback:
            reason_list.append(fallback_reason)
        reason_list.append(
            "source publication delay is not measurable: the provider does not "
            "publish a separate publication timestamp"
            if published_at is None
            else "source publication timestamp supplied by provider metadata"
        )
        provenance = selected.provenance()
        provenance.update(
            {
                "path": path,
                "source_class": self._source_class(selected.provider),
                "is_fallback": is_fallback,
                "fallback_reason": fallback_reason,
                "reconciliation_status": getattr(
                    getattr(result, "status", ""), "value", str(result.status)
                ),
                "observation_count": len(observations),
                "provider_health": (
                    health.to_dict() if health is not None else None
                ),
            }
        )
        return GatewayObservation(
            instrument_id=instrument_id,
            path=path,
            provider=selected.provider,
            price=selected.price,
            currency=selected.currency,
            quality=quality,
            freshness=getattr(freshness, "freshness", Freshness.UNKNOWN),
            available=usable,
            usable=usable,
            fresh=fresh,
            stale=stale,
            provider_conflict=conflict,
            is_fallback=is_fallback,
            fallback_reason=fallback_reason,
            source_class=self._source_class(selected.provider),
            observation_id=selected.observation_id,
            observed_at=selected.observed_at,
            published_at=published_at,
            provider_response_at=selected.retrieved_at,
            received_at=selected.retrieved_at,
            validated_at=selected.retrieved_at,
            persisted_at=stored_at,
            available_at=now,
            reasons=tuple(dict.fromkeys(reason_list)),
            latency=latency,
            provenance=provenance,
        )

    def current(
        self,
        instrument_id: str,
        *,
        provider: str | None = None,
        path: str = "intraday",
    ) -> GatewayObservation:
        """Read the latest validated observation without making a request."""
        result, freshness = self._read_raw(instrument_id, provider)
        return self._envelope(instrument_id, result, freshness, path=path)

    def observation(
        self, instrument_id: str, *, provider: str | None = None
    ) -> GatewayObservation:
        return self.current(instrument_id, provider=provider)

    def latest(
        self, instrument_id: str, *, provider: str | None = None
    ) -> GatewayObservation:
        return self.current(instrument_id, provider=provider)

    async def checkpoint(
        self,
        instrument_id: str,
        *,
        previous_observed_at: datetime | None = None,
        refresh: bool = False,
        provider: str | None = None,
        accepted_freshness: Sequence[Freshness | str] = (Freshness.FRESH,),
    ) -> dict[str, Any]:
        """Return the explicit answer a forecast confirmation needs at T-5/T-1.

        A missing, stale or delayed observation is not substituted.  The caller
        receives ``available=False`` and the last observation only as context;
        ``new_observation_available`` is never true without a newer timestamp.
        """
        refresh_error = ""
        if refresh:
            try:
                await self.refresh([instrument_id], provider=provider, force=True)
            except Exception as exc:  # noqa: BLE001 - a failed refresh is an explicit checkpoint answer
                refresh_error = f"checkpoint refresh failed: {type(exc).__name__}: {exc}"
        envelope = self.current(instrument_id, provider=provider)
        try:
            accepted = tuple(Freshness(item) for item in accepted_freshness)
        except ValueError as exc:
            raise ConfigurationError(
                "accepted_freshness contains an unknown freshness value"
            ) from exc
        fresh_enough = envelope.freshness in accepted
        available = bool(envelope.available and fresh_enough)
        self._counters["checkpoint_available" if available else "checkpoint_unavailable"] += 1
        if not available and envelope.observed_at is not None and not fresh_enough:
            self._counters["stale_rejections"] += 1
        previous = previous_observed_at
        if previous is not None:
            previous = _utc(previous, "previous_observed_at")
        newer: bool | None = None
        if envelope.observed_at is not None and previous is not None:
            newer = envelope.observed_at > previous
        elif envelope.observed_at is not None:
            newer = True
        reasons = list(envelope.reasons)
        if refresh_error:
            reasons.append(refresh_error)
        if not available:
            if envelope.observed_at is None:
                reasons.append("no observation is available at this checkpoint")
            elif not fresh_enough:
                reasons.append(
                    f"freshness {envelope.freshness.value} is not accepted at this "
                    f"checkpoint (accepted: {', '.join(item.value for item in accepted)})"
                )
            else:
                reasons.append("the latest observation is not usable")
        if previous is not None and newer is False:
            reasons.append("the latest observation is not newer than the previous checkpoint")
        return {
            "instrument_id": instrument_id,
            "path": "intraday_checkpoint",
            "available": available,
            "fresh": envelope.fresh,
            "stale": envelope.stale,
            "freshness": envelope.freshness.value,
            "quality": envelope.quality.value,
            "provider": envelope.provider,
            "resolution": envelope.resolution,
            "observed_at": _iso(envelope.observed_at),
            "received_at": _iso(envelope.received_at),
            "persisted_at": _iso(envelope.persisted_at),
            "available_at": _iso(envelope.available_at),
            "previous_observed_at": _iso(previous),
            "newer_than_previous": newer,
            "new_observation_available": bool(newer is True and envelope.observed_at is not None),
            "validation": {
                "quality": envelope.quality.value,
                "usable": envelope.usable,
                "provider_conflict": envelope.provider_conflict,
                "reasons": list(envelope.reasons),
            },
            "observation": envelope.to_dict() if envelope.observed_at is not None else None,
            "reasons": tuple(dict.fromkeys(reasons)),
        }

    # -- acquisition ---------------------------------------------------------

    def _open_for_intraday(self, instrument_id: str) -> bool:
        if self.settings.run_when_closed:
            return True
        try:
            instrument = self.engine.get_instrument(instrument_id)
        except Exception:
            return False
        calendar = calendar_for(instrument)
        if calendar is None:
            return True
        return calendar.state(self._clock()) in (SessionState.OPEN, SessionState.CONTINUOUS)

    def _record_cycle(self, cycle: GatewayCycle) -> None:
        self._last_cycle = cycle
        self._cycles.append(cycle)
        if len(self._cycles) > 200:
            self._cycles.pop(0)
        self._cycle_durations_ms.append(cycle.duration_ms)

    async def run_intraday_cycle(
        self,
        instrument_ids: Sequence[str] | None = None,
        *,
        trigger: str = "manual",
        limit: int | None = None,
    ) -> GatewayCycle:
        """Run the separate low-latency path for one bounded, fair batch."""
        if self._cycle_lock.locked():
            raise MarketDataError("an intraday gateway cycle is already running")
        async with self._cycle_lock:
            started = self._clock()
            requested = self._candidates(instrument_ids)
            budget = self._budget_remaining()
            # Closed instruments are removed before fair selection.  Otherwise
            # a batch of twenty high-priority but closed positions could consume
            # every slot and starve open lower-priority instruments; they spend
            # no request budget, but they must not consume a batch slot.
            open_requested = tuple(
                item for item in requested if self._open_for_intraday(item)
            )
            selected = self.select_batch(
                open_requested,
                limit=min(
                    int(limit)
                    if limit is not None
                    else self.settings.max_instruments_per_batch,
                    budget,
                ),
            )
            skipped = tuple(item for item in requested if item not in open_requested)
            active = selected
            report: Mapping[str, Any] | None = None
            error = ""
            if open_requested and budget <= 0:
                error = "gateway acquisition budget for this minute is exhausted"
            try:
                if active:
                    self._spend_budget(len(active))
                    cycle_report = await self.engine.run_ingestion(
                        list(active),
                        trigger=f"gateway_intraday:{trigger}",
                        intervals=[
                            item
                            for item in self.settings.intervals
                            if item != DataInterval.SPOT.value
                        ],
                        force_quotes=True,
                        include_quotes=True,
                    )
                    report = cycle_report.to_dict()
                    for item in active:
                        self._last_acquired[item] = self._clock()
                        self.invalidate(item)
                # An all-closed batch is a normal no-request outcome, not a
                # provider failure; the cycle still reports the skipped ids.
            except Exception as exc:  # noqa: BLE001 - one cycle must not crash the gateway
                error = f"{type(exc).__name__}: {exc}"
                self._last_error = error
            cycle = GatewayCycle(
                cycle_id=f"gw_{started.timestamp():.6f}",
                path="intraday",
                started_at=started,
                finished_at=self._clock(),
                requested=requested,
                selected=selected,
                skipped=skipped,
                trigger=trigger,
                report=report,
                error=error,
            )
            self._record_cycle(cycle)
            if not error:
                self._last_error = ""
            return cycle

    async def run_general_cycle(
        self,
        instrument_ids: Sequence[str] | None = None,
        *,
        trigger: str = "manual",
    ) -> GatewayCycle:
        """Explicit general-path pass; it uses the engine's existing cadence."""
        started = self._clock()
        ids = tuple(self._candidates(instrument_ids))
        if not ids:
            ids = tuple(self.engine.universe)
        budget = self._budget_remaining()
        selected = self.select_batch(
            ids, limit=min(self.settings.max_instruments_per_batch, budget)
        )
        skipped = tuple(item for item in ids if item not in selected)
        status = self.engine.ingestion_status()
        intervals = tuple(status.get("intervals") or (DataInterval.DAY_1.value,))
        report: Mapping[str, Any] | None = None
        error = ""
        if not ids:
            error = "no instruments were requested and ingestion.universe is empty"
            self._last_error = error
        elif budget <= 0:
            error = "gateway acquisition budget for this minute is exhausted"
            self._last_error = error
        else:
            try:
                if selected:
                    self._spend_budget(len(selected))
                    cycle_report = await self.engine.run_ingestion(
                        list(selected),
                        trigger=f"gateway_general:{trigger}",
                        intervals=list(intervals),
                        include_quotes=bool(status.get("include_quotes", True)),
                    )
                    report = cycle_report.to_dict()
                    for item in selected:
                        self._last_acquired[item] = self._clock()
                        self.invalidate(item)
                self._last_error = ""
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                self._last_error = error
        cycle = GatewayCycle(
            cycle_id=f"gg_{started.timestamp():.6f}",
            path="general",
            started_at=started,
            finished_at=self._clock(),
            requested=ids,
            selected=selected,
            skipped=skipped,
            trigger=trigger,
            report=report,
            error=error,
        )
        self._record_cycle(cycle)
        return cycle

    async def refresh(
        self,
        instrument_ids: Sequence[str],
        *,
        provider: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Coalesced on-demand quote refresh through the engine.

        The engine remains the provider-selection authority; ``provider``
        narrows the freshness pre-check/read and is not a second selection
        system.  Only the engine's configured enabled providers are called.
        """
        ids = tuple(
            dict.fromkeys(
                str(item).strip() for item in instrument_ids if str(item).strip()
            )
        )
        if not ids:
            return {"requested": [], "coalesced": 0, "error": "no instruments requested"}
        if len(ids) > self.settings.max_instruments_per_batch:
            raise MarketDataError(
                f"a gateway refresh is capped at {self.settings.max_instruments_per_batch} instruments"
            )
        key = (
            ids[0] if len(ids) == 1 else "|".join(ids),
            str(provider or "").strip().lower(),
        )
        existing = self._inflight.get(key)
        if existing is not None and not force:
            self._coalesced += 1
            await asyncio.shield(existing)
            return {
                "requested": list(ids),
                "coalesced": 1,
                "error": "",
            }
        task = asyncio.create_task(
            self._do_refresh(ids, provider=provider, force=force),
            name=f"novamarket-gateway-refresh:{ids[0]}",
        )
        self._inflight[key] = task
        try:
            report = await task
            return {
                "requested": list(ids),
                "coalesced": 0,
                "report": report.to_dict(),
                "error": "",
            }
        finally:
            self._inflight.pop(key, None)

    async def _do_refresh(
        self,
        ids: Sequence[str],
        *,
        provider: str | None,
        force: bool,
    ) -> Any:
        async with self._refresh_lock:
            if not force:
                current = [
                    self.current(item, provider=provider) for item in ids
                ]
                if current and all(item.fresh for item in current):
                    return _CachedRefreshReport(len(ids))
            self._spend_budget(len(ids))
            report = await self.engine.run_ingestion(
                list(ids),
                trigger="gateway_refresh",
                intervals=[],
                force_quotes=True,
                include_quotes=True,
            )
            for item in ids:
                self.invalidate(item)
                self._last_acquired[item] = self._clock()
            return report

    async def batch_quotes(
        self,
        instrument_ids: Sequence[str],
        *,
        provider: str | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        ids = tuple(dict.fromkeys(str(item).strip() for item in instrument_ids if str(item).strip()))
        if refresh and ids:
            await self.refresh(ids, provider=provider)
        return {
            "items": [
                self.current(item, provider=provider).to_dict() for item in ids
            ],
            "total": len(ids),
            "path": "intraday" if refresh else "stored",
        }

    # -- scheduler lifecycle ------------------------------------------------

    async def start(self) -> dict[str, Any]:
        if not self.settings.enabled:
            raise ConfigurationError(
                "intraday gateway is disabled; set gateway.enabled=true"
            )
        if self._worker_task is not None and not self._worker_task.done():
            return self.status()
        if not self._subscriptions:
            for instrument_id in self.engine.universe:
                self.subscribe(instrument_id)
        if not self._subscriptions:
            raise ConfigurationError(
                "the intraday gateway has no subscribed instruments"
            )
        self._stop_event = asyncio.Event()
        self._started_at = self._clock()
        self._worker_task = asyncio.create_task(
            self._worker_loop(self._stop_event), name="novamarket-intraday-gateway"
        )
        return self.status()

    async def stop(self) -> dict[str, Any]:
        task = self._worker_task
        event = self._stop_event
        self._worker_task = None
        self._stop_event = None
        if event is not None:
            event.set()
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, TimeoutError):
                pass
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"gateway stop: {type(exc).__name__}: {exc}"
        self._started_at = None
        return self.status()

    async def _worker_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.run_intraday_cycle(trigger="scheduled")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a cycle must not kill the worker
                self._last_error = f"{type(exc).__name__}: {exc}"
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.settings.interval_seconds
                )
            except TimeoutError:
                continue

    # -- provider contracts and telemetry -----------------------------------

    def _logs(self) -> tuple[dict[str, Any], ...]:
        try:
            # MarketDataStore caps one page at 1,000; asking for more makes the
            # whole telemetry read fail, which would look like a healthy empty log.
            return tuple(self.engine.store.retrieval_log(limit=1_000))
        except Exception:  # noqa: BLE001 - telemetry is never allowed to break status
            return ()

    def provider_contracts(self) -> tuple[dict[str, Any], ...]:
        logs = self._logs()
        retry_counts = self.engine.provider_retry_counts()
        now = self._clock()
        rows: list[dict[str, Any]] = []
        for raw in self.engine.provider_status():
            provider = str(raw.get("provider") or "")
            health = raw.get("health") or {}
            latest = next(
                (
                    row
                    for row in logs
                    if row.get("provider") == provider
                    and str(row.get("outcome") or "") != "ok"
                ),
                None,
            )
            request_count = 0
            throttle_count = 0
            cooldown_until: datetime | None = None
            for row in logs:
                if row.get("provider") != provider:
                    continue
                finished = _parse_time(row.get("finished_at"))
                if finished is not None and (now - finished).total_seconds() <= 60:
                    request_count += 1
                if classify_failure(str(row.get("error_type") or ""), str(row.get("error_message") or "")) is ProviderFailureClass.RATE_LIMITED:
                    throttle_count += 1
                    finished_at = _parse_time(row.get("finished_at"))
                    if finished_at is not None:
                        candidate = finished_at + timedelta(seconds=60)
                        if cooldown_until is None or candidate > cooldown_until:
                            cooldown_until = candidate
            failure = classify_failure(
                str((latest or {}).get("error_type") or ""),
                str((latest or {}).get("error_message") or ""),
            )
            rate = raw.get("rate_limit_per_minute")
            schedule: dict[str, Any] = {
                "min_interval_seconds": round(60.0 / float(rate), 3) if rate else None,
                "rate_limit_per_minute": rate,
                "daily_request_limit": raw.get("daily_request_limit"),
                "intraday_intervals": [
                    value
                    for value in raw.get("intervals", [])
                    if DataInterval(value).is_intraday
                ],
                "supports_intraday": any(
                    DataInterval(value).is_intraday for value in raw.get("intervals", [])
                ),
            }
            rows.append(
                {
                    "provider": provider,
                    "source_class": raw.get("source_class", "unknown"),
                    "data_kind": raw.get("data_kind"),
                    "asset_classes": raw.get("asset_classes", []),
                    "response_time_ms": health.get("latency_ms"),
                    "typical_delay_seconds": raw.get("typical_delay_seconds"),
                    "supports": raw.get("supports"),
                    "supported_exchanges": raw.get("exchanges", []),
                    "supported_intervals": raw.get("intervals", []),
                    "supports_streaming": raw.get("supports_streaming", False),
                    "publication_timestamp_supported": raw.get(
                        "publication_timestamp_supported", False
                    ),
                    "historical_days": raw.get("historical_days"),
                    "requires_credentials": raw.get("requires_credentials", False),
                    "enabled": raw.get("enabled", False),
                    "priority": raw.get("priority"),
                    "health": health,
                    "failure_class": failure.value,
                    "last_failure": latest,
                    "request_count_60s": request_count,
                    "throttle_events": throttle_count,
                    "retries": retry_counts.get(provider, 0),
                    "cooldown_until": (
                        cooldown_until.isoformat() if cooldown_until else None
                    ),
                    "schedule": schedule,
                    "limitations": raw.get("limitations", []),
                }
            )
        return tuple(rows)

    def _latency_report(self) -> dict[str, Any]:
        logs = self._logs()
        request_ms = [float(row["latency_ms"]) for row in logs if row.get("latency_ms") is not None]
        source_ages = list(self._source_ages_seconds)
        unavailable_breakdown = {
            "count": 0,
            "p50": None,
            "p95": None,
            "p99": None,
            "mean": None,
            "available": False,
            "reason": "the engine does not expose a separate parsing/validation duration; "
            "these costs are included in provider_request_ms and internal_cycle_ms",
        }
        return {
            "provider_request_ms": _summary(request_ms),
            "internal_cycle_ms": _summary(list(self._cycle_durations_ms)),
            "source_age_seconds": _summary(source_ages),
            "source_to_reception_seconds": _summary(
                list(self._source_to_reception_seconds)
            ),
            "parsing_ms": dict(unavailable_breakdown),
            "validation_ms": dict(unavailable_breakdown),
            "persistence_ms": {
                **_summary(list(self._persistence_ms)),
                "available": bool(self._persistence_ms),
            },
            "distribution_ms": {
                **_summary(list(self._distribution_ms)),
                "available": bool(self._distribution_ms),
            },
            "source_publication_delay": {
                **_summary(list(self._source_publication_delays_seconds)),
                "available": bool(self._source_publication_delays_seconds),
                "reason": (
                    ""
                    if self._source_publication_delays_seconds
                    else "no selected observation supplied a separate publication timestamp"
                ),
            },
            "sample_note": (
                "provider_request_ms comes from the append-only retrieval log; "
                "no fixture value is reported as live latency"
            ),
        }

    def _cache_report(self) -> dict[str, Any]:
        total = self._cache_hits + self._cache_misses
        return {
            "entries": len(self._cache),
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "coalesced_requests": self._coalesced,
            "hit_rate": round(self._cache_hits / total, 4) if total else 0.0,
            "ttl_seconds": self.settings.cache_ttl_seconds,
            **{"hit_ratio": round(self._cache_hits / total, 4) if total else 0.0},
        }

    def health(self) -> dict[str, Any]:
        providers = self.provider_contracts()
        unavailable = [
            row["provider"]
            for row in providers
            if str((row.get("health") or {}).get("status")) == "unavailable"
        ]
        reasons: list[str] = []
        if not providers:
            reasons.append("no provider adapter is enabled")
        if unavailable:
            reasons.append("unavailable provider(s): " + ", ".join(sorted(unavailable)))
        if self._last_cycle is not None and self._last_cycle.error:
            reasons.append(self._last_cycle.error)
        return {
            "state": "degraded" if reasons else "ok",
            "reasons": reasons,
            "running": self._worker_task is not None and not self._worker_task.done(),
            "last_error": self._last_error,
        }

    def status(self) -> dict[str, Any]:
        priority_counts = {priority.value: 0 for priority in _PRIORITY_ORDER}
        for subscription in self._subscriptions.values():
            priority_counts[subscription.priority.value] += 1
        providers = self.provider_contracts()
        latency = self._latency_report()
        return {
            "phase": "PI-03",
            "implemented": True,
            "initialised": True,
            "enabled": self.settings.enabled,
            "running": self._worker_task is not None and not self._worker_task.done(),
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "settings": self.settings.to_dict(),
            "paths": {
                "general": {
                    "floor_seconds": GENERAL_REFRESH_FLOOR_SECONDS,
                    "engine_poll_seconds": self.engine.ingestion_status().get("poll_seconds"),
                    "preserved": True,
                },
                "intraday": {
                    "interval_seconds": self.settings.interval_seconds,
                    "intervals": list(self.settings.intervals),
                    "opt_in": self.settings.enabled,
                    "batch_cap": self.settings.max_instruments_per_batch,
                    "max_requests_per_minute": self.settings.max_requests_per_minute,
                },
                "historical": {"authority": "MarketDataEngine / PI-02 store"},
            },
            "providers": list(providers),
            "cache": self._cache_report(),
            "latency": latency,
            "rate_limits": {
                "budget_per_minute": self.settings.max_requests_per_minute,
                "used_in_window": len(self._request_times),
                "remaining_in_window": self._budget_remaining(),
                "events": sum(
                    int(row.get("throttle_events") or 0) for row in providers
                ),
                "retries": sum(int(row.get("retries") or 0) for row in providers),
                "circuits": {
                    row["provider"]: (row.get("health") or {}).get("circuit_state")
                    for row in providers
                },
            },
            "observability": {
                "counters": dict(self._counters),
                "instrument_status": dict(self._instrument_status),
                "fresh_observation_availability": (
                    round(
                        self._counters["fresh_reads"]
                        / max(self._counters["reads"], 1),
                        4,
                    )
                ),
            },
            "subscriptions": list(self.subscriptions()),
            "priority_counts": priority_counts,
            "rotation": {
                "cursor": dict(self._rotation_cursors),
                "batch_size": self.settings.max_instruments_per_batch,
                "cap": DISCOVERY_BATCH_CAP,
                "reserved_lower_slots": self.settings.lower_priority_reserved_slots,
            },
            "last_cycle": self._last_cycle.to_dict() if self._last_cycle else None,
            "cycles": len(self._cycles),
            "health": self.health(),
            "last_error": self._last_error,
            "measured": {
                "provider_request_samples": latency["provider_request_ms"]["count"],
                "source_age_samples": latency["source_age_seconds"]["count"],
                "persistence_samples": latency["persistence_ms"]["count"],
                "distribution_samples": latency["distribution_ms"]["count"],
            },
        }

    def contract(self) -> dict[str, Any]:
        return {
            "phase": "PI-03",
            "name": "low_latency_market_data_gateway",
            "settings": GatewaySettings.schema(),
            "general_refresh_floor_seconds": GENERAL_REFRESH_FLOOR_SECONDS,
            "discovery_batch_cap": DISCOVERY_BATCH_CAP,
            "paths": {
                "general": "existing scheduled ingestion, 900-second floor",
                "intraday": "separate opt-in quote/bar cycle with explicit cadence",
                "historical": "engine/PI-02 reads and bounded history acquisition",
            },
            "priorities": [
                {"priority": item.value, "rank": item.rank} for item in _PRIORITY_ORDER
            ],
            "source_classes": [
                "exchange",
                "authorized_redistribution",
                "third_party_delayed",
                "delayed",
                "cached",
                "historical",
                "scraped",
                "unknown",
            ],
            "freshness": (
                "engine freshness is recomputed on every gateway read; a cache "
                "hit never upgrades a stale observation to fresh"
            ),
            "checkpoint_rule": (
                "available only when a validated observation is present and its "
                "freshness is accepted; missing/stale/delayed returns available=false"
            ),
            "latency_fields": [
                "source_observed_at",
                "provider_published_at",
                "provider_response_at",
                "received_at",
                "validated_at",
                "persisted_at",
                "available_at",
            ],
            "limitations": [
                "Yahoo is unofficial and delayed; no provider here is exchange "
                "real-time or streaming.",
                "CoinGecko free OHLC is 30-minute/one-day and has no volume.",
                "Alpha Vantage is daily-only, keyed and subject to a 25/day free tier.",
                "Source publication delay is not measurable without a separate "
                "provider publication timestamp.",
                "The gateway is an acquisition/distribution boundary; it does not "
                "place orders or run the PI-09 forecast scheduler.",
            ],
        }

    # -- historical reads ----------------------------------------------------

    def bars(self, *args: Any, **kwargs: Any) -> Any:
        return self.engine.bars(*args, **kwargs)

    def observations(self, *args: Any, **kwargs: Any) -> Any:
        return self.engine.observations(*args, **kwargs)

    def history(self, *args: Any, **kwargs: Any) -> Any:
        return self.engine.ingest_history(*args, **kwargs)


class _CachedRefreshReport:
    """Small report used when a coalesced/read-cache refresh made no request."""

    def __init__(self, count: int) -> None:
        self._count = count

    def to_dict(self) -> dict[str, Any]:
        return {
            "cache_hit": True,
            "instruments_requested": self._count,
            "provider_attempts": 0,
        }


__all__ = [
    "AcquisitionPriority",
    "DISCOVERY_BATCH_CAP",
    "GENERAL_REFRESH_FLOOR_SECONDS",
    "GatewayCycle",
    "GatewayObservation",
    "GatewaySettings",
    "MarketDataGateway",
    "ProviderFailureClass",
    "Subscription",
    "classify_failure",
]