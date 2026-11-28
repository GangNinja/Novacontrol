"""The market-data engine: fetch, normalize, validate, reconcile, persist, poll.

This is the orchestration layer. It owns no provider-specific parsing (that is
the adapter's job) and no schema decisions (that is the store's job). It owns the
ORDER of operations that makes the data trustworthy:

1. Ask every compatible enabled provider (fallback order is configuration).
2. Normalize each response into the canonical models.
3. Assess freshness against the instrument's exchange calendar and interval.
4. Assess quality against prior observations and the configured anomaly rules.
5. Persist every observation, including invalid ones, through the append-only
   store; a rejected observation is retained with its reason.
6. Reconcile the latest providers; disagreement is a CONFLICT, never an average.
7. Record provider health, latency and retrieval outcomes.
8. Publish typed events and keep the last measured report.

Background ingestion is an explicit, cancellable task. It never overlaps itself,
never creates one worker per instrument, and skips a provider whose circuit is
open. A failed provider therefore cannot block a healthy one.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from novacontrol.novamarket.contracts import (
    CircuitOpenError,
    ConfigurationError,
    InstrumentNotFoundError,
    MarketDataError,
    MarketDataStoreError,
    ProviderAuthenticationError,
    ProviderError,
    ProviderInvalidResponseError,
    UnsupportedIntervalError,
)
from novacontrol.novamarket.marketdata.calendar import (
    SessionState,
    calendar_contract,
    calendar_for,
)
from novacontrol.novamarket.marketdata.freshness import (
    FreshnessAssessment,
    FreshnessSettings,
    assess_bar as assess_bar_freshness,
    assess_quote as assess_quote_freshness,
    missing_assessment,
)
from novacontrol.novamarket.marketdata.models import (
    AssetClass,
    DataInterval,
    DataQuality,
    Freshness,
    HistoryIngestReport,
    IngestionReport,
    Instrument,
    InstrumentIngestResult,
    OhlcvBar,
    PriceObservation,
    ProviderHealth,
    ProviderStatus,
    ReconciliationResult,
    ReconciliationStatus,
    worst_quality,
)
from novacontrol.novamarket.marketdata.providers.base import (
    CircuitBreaker,
    MarketDataAdapter,
    ProviderBar,
    ProviderQuote,
)
from novacontrol.novamarket.marketdata.providers.registry import ProviderRegistry
from novacontrol.novamarket.marketdata.quality import (
    QualitySettings,
    assess_bar as assess_bar_quality,
    assess_quote as assess_quote_quality,
    reconcile,
)
from novacontrol.novamarket.marketdata.settings import CacheSettings, IngestionSettings
from novacontrol.novamarket.marketdata.store import (
    BarPage,
    InstrumentPage,
    MarketDataStore,
    ObservationPage,
)

_LOGGER = logging.getLogger("novacontrol.novamarket.marketdata")

#: One provider name -> event payload. The service adapts this to the one bus.
EventSink = Callable[[str, Mapping[str, Any]], Awaitable[None]]

#: A provider's success after an open/half-open circuit is a recovery fact.
_CIRCUIT_CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class ProviderProbe:
    provider: str
    ok: bool | None
    detail: str


class MarketDataEngine:
    """The one market-data pipeline used by the subsystem and the API."""

    def __init__(
        self,
        *,
        store: MarketDataStore,
        registry: ProviderRegistry,
        enabled_providers: Sequence[str] = (),
        universe: Sequence[str] = (),
        ingestion: IngestionSettings | None = None,
        freshness: FreshnessSettings | None = None,
        quality: QualitySettings | None = None,
        cache: CacheSettings | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        event_sink: EventSink | None = None,
        history_page_limit: int = 200,
    ) -> None:
        self.store = store
        self.registry = registry
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self._event_sink = event_sink
        self._ingestion = ingestion or IngestionSettings()
        self._freshness = freshness or FreshnessSettings()
        self._quality = quality or QualitySettings()
        self._cache_settings = cache or CacheSettings()
        self._universe = tuple(universe)
        self._history_page_limit = history_page_limit
        self._enabled_names: tuple[str, ...] = ()
        self._adapters: tuple[MarketDataAdapter, ...] = ()
        self._breakers: dict[str, CircuitBreaker] = {}
        self._configure_providers(enabled_providers)

        self._quote_cache: dict[
            tuple[str, str], tuple[float, tuple[PriceObservation, ...]]
        ] = {}
        self._instrument_cache: dict[str, tuple[float, Instrument]] = {}
        self._cache_hits = 0
        self._cache_misses = 0
        self._last_freshness: dict[str, str] = {}
        self._last_report: IngestionReport | None = None
        self._last_error = ""
        self._last_event_error = ""
        self._last_store_error = ""
        self._worker_task: asyncio.Task[Any] | None = None
        self._stop_event: asyncio.Event | None = None
        self._ingest_lock = asyncio.Lock()
        self._started_at: datetime | None = None
        self._cycles = 0
        # Per-cycle provider accounting; reset at the start of each cycle so a
        # report describes THIS run, not the process lifetime.
        self._provider_attempts = 0
        self._provider_failures = 0
        self._provider_retries = 0
        self._retries_by_provider: dict[str, int] = {}

    # -- configuration --------------------------------------------------------

    def _configure_providers(self, names: Sequence[str]) -> None:
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in names:
            name = str(value or "").strip().lower()
            if name in seen:
                raise MarketDataError(f"duplicate provider: {name}")
            seen.add(name)
            cleaned.append(name)
        adapters = self.registry.enabled(cleaned)
        self._enabled_names = tuple(cleaned)
        self._adapters = tuple(adapters)
        for adapter in adapters:
            self._breakers.setdefault(
                adapter.name,
                CircuitBreaker(
                    adapter.name,
                    failure_threshold=3,
                    cooldown_seconds=60.0,
                    clock=self._monotonic,
                ),
            )
            setter = getattr(adapter, "set_retry_observer", None)
            if callable(setter):
                setter(self._record_retry)

    def _record_retry(
        self,
        provider: str,
        attempt: int,
        delay: float,
        error: ProviderError,
    ) -> None:
        """Count adapter retries without changing the adapter's policy."""
        key = str(provider or "").strip().lower()
        self._provider_retries += 1
        self._retries_by_provider[key] = self._retries_by_provider.get(key, 0) + 1

    def provider_retry_counts(self) -> dict[str, int]:
        """Process-lifetime retry evidence, by provider."""
        return dict(sorted(self._retries_by_provider.items()))

    def reconfigure(
        self,
        *,
        enabled_providers: Sequence[str] | None = None,
        universe: Sequence[str] | None = None,
        ingestion: IngestionSettings | None = None,
        freshness: FreshnessSettings | None = None,
        quality: QualitySettings | None = None,
        cache: CacheSettings | None = None,
        history_page_limit: int | None = None,
    ) -> None:
        """Apply validated settings without losing the store or the worker."""
        if enabled_providers is not None:
            self._configure_providers(enabled_providers)
            self._quote_cache.clear()
            self._instrument_cache.clear()
        if universe is not None:
            self._universe = tuple(universe)
        if ingestion is not None:
            self._ingestion = ingestion
        if freshness is not None:
            self._freshness = freshness
        if quality is not None:
            self._quality = quality
        if cache is not None:
            self._cache_settings = cache
            self._quote_cache.clear()
            self._instrument_cache.clear()
        if history_page_limit is not None:
            if not 1 <= history_page_limit <= 1_000:
                raise ConfigurationError("history_page_limit must be 1..1000")
            self._history_page_limit = history_page_limit

    def set_event_sink(self, sink: EventSink | None) -> None:
        self._event_sink = sink

    @property
    def enabled_providers(self) -> tuple[str, ...]:
        return self._enabled_names

    @property
    def universe(self) -> tuple[str, ...]:
        return self._universe

    # -- instrument catalogue -------------------------------------------------

    def register_instrument(self, instrument: Instrument) -> Instrument:
        try:
            stored = self.store.upsert_instrument(instrument)
        except MarketDataStoreError as exc:
            self._last_store_error = str(exc)
            raise
        self._instrument_cache[instrument.instrument_id] = (
            self._monotonic(),
            stored,
        )
        return stored

    def get_instrument(self, instrument_id: str) -> Instrument:
        key = str(instrument_id or "").strip()
        cached = self._instrument_cache.get(key)
        if cached is not None:
            inserted, instrument = cached
            if (
                self._monotonic() - inserted
                <= self._cache_settings.instrument_ttl_seconds
            ):
                self._cache_hits += 1
                return instrument
            self._instrument_cache.pop(key, None)
        try:
            stored_instrument = self.store.get_instrument(key)
        except MarketDataStoreError as exc:
            self._last_store_error = str(exc)
            raise
        if stored_instrument is None:
            raise InstrumentNotFoundError(
                f"instrument {key!r} is not registered; register it with "
                "POST /market/instruments first"
            )
        self._cache_misses += 1
        self._instrument_cache[key] = (self._monotonic(), stored_instrument)
        return stored_instrument

    def list_instruments(
        self,
        *,
        asset_class: str | None = None,
        exchange: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> InstrumentPage:
        return self.store.list_instruments(
            asset_class=asset_class, exchange=exchange, limit=limit, offset=offset
        )

    async def lookup_instrument(
        self,
        provider: str,
        provider_symbol: str,
        *,
        symbol: str | None = None,
        name: str | None = None,
        currency: str | None = None,
        timezone: str | None = None,
        exchange: str | None = None,
        mic: str | None = None,
    ) -> Instrument:
        """Resolve metadata through a provider and register the instrument.

        The provider's own symbol is preserved in ``provider_symbols``; the
        canonical symbol/exchange/currency can be overridden because providers
        (notably crypto aggregators) do not always name an instrument the way
        the exchange does.
        """
        adapter = self._adapter(provider)
        result = await self._provider_call(
            adapter, "lookup", None, lambda: adapter.lookup(provider_symbol)
        )
        canonical_symbol = str(symbol or result.symbol or "").strip().upper()
        resolved_currency = str(currency or result.currency or "").strip().upper()
        if result.asset_class is AssetClass.CRYPTO and "-" not in canonical_symbol:
            canonical_symbol = f"{canonical_symbol}-{resolved_currency}"
        instrument = Instrument.create(
            symbol=canonical_symbol,
            name=name or result.name,
            exchange=exchange or result.exchange,
            mic=mic or result.mic,
            asset_class=result.asset_class,
            currency=resolved_currency,
            timezone=timezone or result.timezone,
            provider_symbols={adapter.name: result.provider_symbol},
            metadata={"lookup_provider": adapter.name},
        )
        return self.register_instrument(instrument)

    # -- reads ----------------------------------------------------------------

    def latest(
        self,
        instrument_id: str,
        *,
        provider: str | None = None,
    ) -> ReconciliationResult:
        """The current cross-provider price, with freshness recomputed now.

        A cached observation is never presented as fresher than it is: the cache
        short-circuits the STORE access, and freshness/quality are evaluated
        again on every read.
        """
        instrument = self.get_instrument(instrument_id)
        key = (instrument.instrument_id, str(provider or "").strip().lower())
        observations = self._cached_observations(key)
        if observations is None:
            try:
                observations = self.store.latest_observations(
                    instrument.instrument_id,
                    providers=(key[1],) if key[1] else None,
                )
            except MarketDataStoreError as exc:
                self._last_store_error = str(exc)
                raise
            self._quote_cache[key] = (self._monotonic(), observations)
        now = self._clock()
        assessed = tuple(
            self._reassess_quote(instrument, item, now) for item in observations
        )
        return reconcile(
            assessed,
            instrument=instrument,
            priority=self._enabled_names,
            settings=self._quality,
        )

    def freshness_of(
        self,
        instrument_id: str,
        *,
        provider: str | None = None,
    ) -> FreshnessAssessment:
        result = self.latest(instrument_id, provider=provider)
        if result.selected is not None:
            adapter = self.registry.get(result.selected.provider)
            return assess_quote_freshness(
                self.get_instrument(instrument_id),
                result.selected,
                now=self._clock(),
                settings=self._freshness,
                provider_delay_seconds=adapter.capabilities.typical_delay_seconds,
            )
        # "Nothing was ever stored" is not a provider outage: a closed market
        # legitimately has no live quote, and calling that PROVIDER_UNAVAILABLE
        # would send a consumer looking for a failure that did not happen. Only
        # observations that exist but could not be used indicate a provider
        # problem.
        return missing_assessment(
            self.get_instrument(instrument_id),
            now=self._clock(),
            settings=self._freshness,
            provider_unavailable=bool(result.observations),
        )

    def observations(
        self,
        instrument_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        provider: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        ascending: bool = False,
    ) -> ObservationPage:
        self.get_instrument(instrument_id)
        cap = self._history_page_limit
        page_limit = cap if limit is None else min(int(limit), cap)
        return self.store.observations(
            instrument_id,
            start=start,
            end=end,
            provider=provider,
            limit=page_limit,
            offset=offset,
            ascending=ascending,
        )

    def bar_inventory(
        self,
        *,
        interval: DataInterval | str = DataInterval.DAY_1,
        min_bars: int = 1,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Read-only inventory of stored series for the analysis workbench."""
        return self.store.bar_inventory(
            interval=_bar_interval(interval, parameter="inventory interval"),
            min_bars=min_bars,
            limit=limit,
            offset=offset,
        )

    def board_rows(self, *, exchange: str | None = None) -> list[dict[str, Any]]:
        """Read the Market Overview rows from the one append-only store."""
        return self.store.board_rows(exchange=exchange)

    def bars(
        self,
        instrument_id: str,
        *,
        interval: DataInterval | str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        provider: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        ascending: bool = False,
    ) -> BarPage:
        self.get_instrument(instrument_id)
        cap = self._history_page_limit
        page_limit = cap if limit is None else min(int(limit), cap)
        return self.store.bars(
            instrument_id,
            interval=(None if interval is None else _bar_interval(interval)),
            start=start,
            end=end,
            provider=provider,
            limit=page_limit,
            offset=offset,
            ascending=ascending,
        )

    def _reassess_quote(
        self, instrument: Instrument, observation: PriceObservation, now: datetime
    ) -> PriceObservation:
        reasons = list(observation.reasons)
        delay = 0
        try:
            adapter = self.registry.get(observation.provider)
            delay = adapter.capabilities.typical_delay_seconds
        except MarketDataError:
            # A stored observation outlives the adapter that produced it. A
            # provider removed from this build must not make every instrument
            # holding its history unreadable; assess it by timestamp alone.
            reasons.append(
                f"provider {observation.provider!r} is not installed; "
                "freshness uses age only"
            )
        freshness = assess_quote_freshness(
            instrument,
            observation,
            now=now,
            settings=self._freshness,
            provider_delay_seconds=delay,
        )
        quality = observation.quality
        if freshness.freshness is Freshness.STALE:
            quality = worst_quality(quality, DataQuality.STALE)
            reasons.append("stale on read; not a current quote")
        return replace(
            observation,
            freshness=freshness.freshness,
            quality=quality,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    def _cached_observations(
        self, key: tuple[str, str]
    ) -> tuple[PriceObservation, ...] | None:
        ttl = self._cache_settings.quote_ttl_seconds
        if ttl <= 0:
            return None
        entry = self._quote_cache.get(key)
        if entry is None:
            self._cache_misses += 1
            return None
        inserted, observations = entry
        if self._monotonic() - inserted > ttl:
            self._quote_cache.pop(key, None)
            self._cache_misses += 1
            return None
        self._cache_hits += 1
        return observations

    def invalidate(self, instrument_id: str) -> None:
        instrument = str(instrument_id)
        for key in tuple(self._quote_cache):
            if key[0] == instrument:
                self._quote_cache.pop(key, None)
        self._instrument_cache.pop(instrument, None)

    # -- provider catalogue and health ---------------------------------------

    def provider_status(self) -> tuple[dict[str, Any], ...]:
        health = {
            item.provider: item for item in self.store.all_provider_health()
        }
        rows: list[dict[str, Any]] = []
        priorities = {name: index for index, name in enumerate(self._enabled_names)}
        for row in self.registry.capability_matrix(health):
            name = row["provider"]
            breaker = self._breakers.get(name)
            row["enabled"] = name in priorities
            row["priority"] = priorities.get(name)
            row["circuit_state"] = (
                breaker.state.value if breaker is not None else "closed"
            )
            rows.append(row)
        return tuple(rows)

    def provider_health(self, provider: str) -> ProviderHealth:
        return self.store.provider_health(provider)

    async def probe_providers(self) -> tuple[ProviderProbe, ...]:
        """Probe only enabled providers; an unsupported probe is UNKNOWN, not OK."""
        probes: list[ProviderProbe] = []
        for adapter in self._adapters:
            if not getattr(adapter, "supports_probe", False):
                probes.append(
                    ProviderProbe(
                        adapter.name,
                        None,
                        "no standalone probe; health comes from real reads",
                    )
                )
                continue
            try:
                detail = await self._provider_call(
                    adapter, "probe", None, lambda: adapter.probe()
                )
                probes.append(ProviderProbe(adapter.name, True, str(detail)))
            except ProviderError as exc:
                probes.append(ProviderProbe(adapter.name, False, str(exc)))
        return tuple(probes)

    # -- ingestion ------------------------------------------------------------

    async def start_ingestion(self) -> dict[str, Any]:
        if not self._ingestion.enabled:
            raise ConfigurationError(
                "background ingestion is disabled; set ingestion.enabled=true"
            )
        if not self._universe:
            raise ConfigurationError(
                "ingestion.universe is empty; register instruments and list them "
                "in the universe before starting the worker"
            )
        missing = [
            instrument_id
            for instrument_id in self._universe
            if self.store.get_instrument(instrument_id) is None
        ]
        if missing:
            raise InstrumentNotFoundError(
                "ingestion universe contains unregistered instrument(s): "
                + ", ".join(missing)
            )
        if not self._enabled_names:
            raise ConfigurationError(
                "no market-data provider is enabled; configure data_providers first"
            )
        if self._worker_task is not None and not self._worker_task.done():
            return self.ingestion_status()
        self._stop_event = asyncio.Event()
        self._worker_task = asyncio.create_task(
            self._worker_loop(self._stop_event), name="novamarket-market-data"
        )
        self._started_at = self._clock()
        return self.ingestion_status()

    async def stop_ingestion(self) -> dict[str, Any]:
        task = self._worker_task
        stop_event = self._stop_event
        if stop_event is not None:
            stop_event.set()
        self._worker_task = None
        self._stop_event = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, TimeoutError):
                pass
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                self._last_error = f"worker stop: {type(exc).__name__}: {exc}"
        self._started_at = None
        return self.ingestion_status()

    def ingestion_status(self) -> dict[str, Any]:
        running = self._worker_task is not None and not self._worker_task.done()
        return {
            "running": running,
            "enabled": self._ingestion.enabled,
            "poll_seconds": self._ingestion.poll_seconds,
            "intervals": list(self._ingestion.intervals),
            "include_quotes": self._ingestion.include_quotes,
            "include_bars": self._ingestion.include_bars,
            "run_when_closed": self._ingestion.run_when_closed,
            "allow_partial_bars": self._ingestion.allow_partial_bars,
            "max_concurrency": self._ingestion.max_concurrency,
            "universe_size": len(self._universe),
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "cycles": self._cycles,
            "last_report": self._last_report.to_dict() if self._last_report else None,
            "last_error": self._last_error,
        }

    async def _worker_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.run_ingestion(trigger="scheduled")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a cycle failure must not kill the worker
                self._last_error = f"{type(exc).__name__}: {exc}"
                _LOGGER.warning("NovaMarket ingestion cycle failed: %s", exc)
                await self._publish(
                    "novamarket.data.ingestion_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            delay = float(self._ingestion.poll_seconds)
            if self._ingestion.jitter_seconds:
                delay += random.uniform(0, self._ingestion.jitter_seconds)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except TimeoutError:
                continue

    async def run_ingestion(
        self,
        instrument_ids: Sequence[str] | None = None,
        *,
        trigger: str = "manual",
        intervals: Sequence[DataInterval | str] | None = None,
        force_quotes: bool = False,
        include_quotes: bool | None = None,
    ) -> IngestionReport:
        """Run exactly one bounded cycle; overlapping cycles are refused.

        ``force_quotes`` is for an explicit manual read of the latest stored
        provider close (for example, populating an empty Market Overview while
        the exchange is shut). Background cycles leave it off, so a closed
        market does not turn into a polling loop over the whole universe.

        ``include_quotes`` overrides the configured quote collection for this
        one cycle. A watchlist pass sets it false to spend its provider budget
        on one-minute bars only; it does not silently change the global setting.
        """
        if self._ingest_lock.locked():
            raise MarketDataError("an ingestion cycle is already running")
        async with self._ingest_lock:
            return await self._run_cycle(
                instrument_ids=instrument_ids,
                trigger=trigger,
                intervals=intervals,
                force_quotes=force_quotes,
                include_quotes=include_quotes,
            )

    async def _run_cycle(
        self,
        *,
        instrument_ids: Sequence[str] | None,
        trigger: str,
        intervals: Sequence[DataInterval | str] | None,
        force_quotes: bool = False,
        include_quotes: bool | None = None,
    ) -> IngestionReport:
        started = self._clock()
        ids = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in (
                    instrument_ids if instrument_ids is not None else self._universe
                )
                if str(item).strip()
            )
        )
        if not ids:
            raise ConfigurationError(
                "no instruments were requested and ingestion.universe is empty"
            )
        self._provider_attempts = 0
        self._provider_failures = 0
        # Validate the requested interval before provider availability: an
        # unknown interval is a bad request whether or not a provider is on.
        scheduled_intervals = tuple(
            _bar_interval(item, parameter="ingestion interval")
            for item in (
                intervals if intervals is not None else self._ingestion.intervals
            )
        )
        if not self._enabled_names:
            report = IngestionReport(
                started_at=started,
                finished_at=self._clock(),
                trigger=trigger,
                instruments_requested=len(ids),
                instruments_unavailable=len(ids),
                errors=("no market-data provider is enabled",),
            )
            self._last_report = report
            return report

        semaphore = asyncio.Semaphore(self._ingestion.max_concurrency)

        async def one(instrument_id: str) -> InstrumentIngestResult:
            async with semaphore:
                try:
                    return await self._ingest_one(
                        instrument_id,
                        intervals=scheduled_intervals,
                        trigger=trigger,
                        force_quotes=force_quotes,
                        include_quotes=include_quotes,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - isolate one instrument
                    _LOGGER.warning(
                        "NovaMarket ingestion failed for %s: %s",
                        instrument_id,
                        exc,
                    )
                    return InstrumentIngestResult(
                        instrument_id=instrument_id,
                        status="unavailable",
                        detail=f"{type(exc).__name__}: {exc}",
                    )

        results = await asyncio.gather(*(one(item) for item in ids))
        report = self._build_report(started, trigger, results)
        self._last_report = report
        self._cycles += 1
        self._last_error = ""
        if report.instruments_succeeded:
            await self._publish(
                "novamarket.data.available",
                assets=tuple(
                    result.instrument_id
                    for result in results
                    if result.status == "ok"
                ),
                sources=tuple(
                    sorted(
                        {
                            result.provider
                            for result in results
                            if result.provider
                        }
                    )
                ),
            )
        elif report.instruments_unavailable:
            await self._publish(
                "novamarket.data.unavailable",
                reason="no provider supplied validated data for any requested instrument",
            )
        return report

    async def _ingest_one(
        self,
        instrument_id: str,
        *,
        intervals: Sequence[DataInterval],
        trigger: str,
        force_quotes: bool = False,
        include_quotes: bool | None = None,
    ) -> InstrumentIngestResult:
        quotes_enabled = (
            self._ingestion.include_quotes
            if include_quotes is None
            else include_quotes
        )
        try:
            instrument = self.get_instrument(instrument_id)
        except (InstrumentNotFoundError, MarketDataStoreError) as exc:
            return InstrumentIngestResult(
                instrument_id=instrument_id, status="unavailable", detail=str(exc)
            )
        calendar = calendar_for(instrument)
        now = self._clock()
        state = calendar.state(now) if calendar else SessionState.UNKNOWN
        if (
            calendar is not None
            and not self._ingestion.run_when_closed
            and state
            not in (SessionState.OPEN, SessionState.CONTINUOUS)
        ):
            # A closed market suppresses LIVE quotes, not the previous
            # session's completed bars: end-of-day ingestion happens after the
            # close by definition. Skip the instrument only when no configured
            # bar interval is actually due.
            due_bars = self._ingestion.include_bars and any(
                self._needs_bars(instrument, interval, calendar, now)
                for interval in intervals
            )
            if not due_bars and not (force_quotes and quotes_enabled):
                return InstrumentIngestResult(
                    instrument_id=instrument_id,
                    status="skipped",
                    detail=f"market is {state.value}; run_when_closed is false",
                )

        quote_result: InstrumentIngestResult | None = None
        try:
            needs_quote = quotes_enabled and (
                force_quotes or self._needs_quote(instrument, calendar, now)
            )
        except (MarketDataStoreError, ValueError) as exc:
            return InstrumentIngestResult(
                instrument_id=instrument_id,
                status="unavailable",
                detail=f"could not determine whether a quote was needed: {exc}",
            )
        if needs_quote:
            try:
                quote_result = await self._collect_quote(instrument, now)
            except MarketDataStoreError as exc:
                self._last_store_error = str(exc)
                return InstrumentIngestResult(
                    instrument_id=instrument_id,
                    status="unavailable",
                    detail=f"market-data store refused the quote: {exc}",
                )

        bars_inserted = 0
        bars_duplicate = 0
        bars_rejected = 0
        bar_provider = ""
        bar_errors: list[str] = []
        if self._ingestion.include_bars:
            for interval in intervals:
                try:
                    if not self._needs_bars(instrument, interval, calendar, now):
                        continue
                except (MarketDataStoreError, ValueError) as exc:
                    bar_errors.append(
                        f"could not determine whether {interval.value} bars were "
                        f"needed: {exc}"
                    )
                    continue
                (
                    inserted,
                    duplicate,
                    rejected,
                    provider_name,
                    errors,
                ) = await self._collect_bars(instrument, interval, now)
                bars_inserted += inserted
                bars_duplicate += duplicate
                bars_rejected += rejected
                if provider_name:
                    bar_provider = provider_name
                bar_errors.extend(errors)

        if quote_result is not None:
            status = quote_result.status
            provider = quote_result.provider
            detail = quote_result.detail
            quote_id = quote_result.quote_id
            quote_inserted = quote_result.quote_inserted
            quote_duplicate = quote_result.quote_duplicate
            quote_rejected = quote_result.quote_rejected
        else:
            status = "skipped"
            provider = ""
            detail = "no quote was needed this cycle"
            quote_id = ""
            quote_inserted = quote_duplicate = quote_rejected = False
        if bars_inserted or bars_duplicate:
            status = "ok"
            provider = provider or bar_provider
            detail = detail or "bars collected"
        elif bar_errors and status != "ok":
            status = "unavailable"
            detail = "; ".join(dict.fromkeys(bar_errors))
        return InstrumentIngestResult(
            instrument_id=instrument_id,
            status=status,
            provider=provider,
            detail=detail,
            quote_id=quote_id,
            quote_inserted=quote_inserted,
            quote_duplicate=quote_duplicate,
            quote_rejected=quote_rejected,
            bars_inserted=bars_inserted,
            bars_duplicate=bars_duplicate,
            bars_rejected=bars_rejected,
        )

    def _needs_quote(
        self,
        instrument: Instrument,
        calendar: Any | None,
        now: datetime,
    ) -> bool:
        if calendar is None:
            return True
        state = calendar.state(now)
        if state in (SessionState.OPEN, SessionState.CONTINUOUS):
            return True
        if not self._ingestion.run_when_closed:
            return False
        last_close = calendar.last_session_close(now)
        if last_close is None:
            return True
        try:
            latest = self.store.latest_observations(instrument.instrument_id)
        except MarketDataStoreError as exc:
            self._last_store_error = str(exc)
            return True
        return not any(item.observed_at >= last_close for item in latest)

    def _needs_bars(
        self,
        instrument: Instrument,
        interval: DataInterval,
        calendar: Any | None,
        now: datetime,
    ) -> bool:
        if calendar is None:
            return interval.is_intraday
        if interval is DataInterval.DAY_1:
            # During an open session, today's daily bar is partial and is dropped
            # by ``_collect_bars``.  That is not a reason to skip the whole
            # request: a US instrument with no daily history still needs the
            # completed sessions in the provider's window.  Ask for daily bars
            # whenever the most recent completed session is absent.
            last_close = calendar.last_session_close(now)
            if last_close is None:
                return True
            existing = self.store.bars(
                instrument.instrument_id,
                interval=interval,
                start=last_close - timedelta(days=4),
                limit=10,
            )
            last_session = calendar.local(last_close).date()
            return not any(
                calendar.local(item.observed_at).date() == last_session
                and item.usable
                for item in existing.items
            )
        # Intraday: there is always a newest bar to ask for; the provider returns
        # a bounded page and duplicates are idempotent.
        return True

    async def _collect_quote(
        self, instrument: Instrument, now: datetime
    ) -> InstrumentIngestResult:
        successes = 0
        inserted = duplicate = rejected = 0
        errors: list[str] = []
        last_observation: PriceObservation | None = None
        for adapter in self._compatible(instrument):
            if successes and adapter.capabilities.daily_request_limit is not None:
                # A keyed free-tier provider is kept for FALLBACK, not spent on
                # a reconciliation duplicate when another source succeeded.
                continue
            try:
                quote = await self._provider_call(
                    adapter,
                    "quote",
                    instrument.instrument_id,
                    lambda: adapter.fetch_quote(instrument),
                )
            except CircuitOpenError as exc:
                errors.append(f"{adapter.name}: {exc}")
                continue
            except ProviderError as exc:
                errors.append(f"{adapter.name}: {exc}")
                continue
            try:
                observation = self._normalize_quote(instrument, adapter, quote, now)
                observation = self._assess_quote(instrument, adapter, observation, now)
            except (ProviderInvalidResponseError, ValueError) as exc:
                errors.append(f"{adapter.name}: normalization failed: {exc}")
                continue
            try:
                append = self.store.append_observation(observation)
            except MarketDataStoreError as exc:
                self._last_store_error = str(exc)
                errors.append(f"{adapter.name}: store refused the observation: {exc}")
                continue
            self.invalidate(instrument.instrument_id)
            if observation.quality is DataQuality.INVALID:
                rejected += 1
                errors.append(
                    f"{adapter.name}: rejected: {'; '.join(observation.reasons)}"
                )
                await self._publish(
                    "novamarket.data.rejected",
                    instrument_id=instrument.instrument_id,
                    provider=adapter.name,
                    reason="; ".join(observation.reasons)
                    or "structural validation failed",
                )
                continue
            successes += 1
            stored_observation = observation
            if append.conflict:
                # The incoming value was not written; the retained original is
                # the record this instrument now has. The conflict is visible,
                # and the original was marked SUSPECT by the store.
                duplicate += 1
                stored = self.store.get_observation(append.record_id)
                if stored is not None:
                    stored_observation = stored
                await self._publish(
                    "novamarket.data.quality_changed",
                    instrument_id=instrument.instrument_id,
                    previous="valid",
                    quality=DataQuality.SUSPECT.value,
                    detail=append.detail,
                )
            elif append.inserted:
                inserted += 1
            else:
                duplicate += 1
            last_observation = stored_observation
            await self._publish(
                "novamarket.data.received",
                instrument_id=instrument.instrument_id,
                provider=adapter.name,
                quality=observation.quality.value,
                freshness=observation.freshness.value,
                observed_at=observation.observed_at.isoformat(),
                retrieved_at=observation.retrieved_at.isoformat(),
                source_timezone=observation.source_timezone,
                age_seconds=round(
                    max((now - observation.observed_at).total_seconds(), 0.0), 3
                ),
                duplicate=append.duplicate,
            )
            await self._publish(
                "novamarket.data.validated",
                instrument_id=instrument.instrument_id,
                provider=adapter.name,
                quality=observation.quality.value,
                freshness=observation.freshness.value,
                observed_at=observation.observed_at.isoformat(),
                retrieved_at=observation.retrieved_at.isoformat(),
                source_timezone=observation.source_timezone,
            )
            if observation.freshness is Freshness.STALE:
                await self._publish(
                    "novamarket.data.stale",
                    asset=instrument.instrument_id,
                    provider=adapter.name,
                    age_seconds=round(
                        max((now - observation.observed_at).total_seconds(), 0.0), 3
                    ),
                )
            await self._announce_freshness(instrument, observation)
        if last_observation is None:
            return InstrumentIngestResult(
                instrument_id=instrument.instrument_id,
                status="unavailable",
                detail="; ".join(dict.fromkeys(errors)) or "no provider supported it",
                quote_rejected=rejected > 0,
            )
        try:
            reconciliation = reconcile(
                self.store.latest_observations(instrument.instrument_id),
                instrument=instrument,
                priority=self._enabled_names,
                settings=self._quality,
            )
        except MarketDataStoreError as exc:
            self._last_store_error = str(exc)
            reconciliation = None
        detail = "; ".join(dict.fromkeys(errors))
        if (
            reconciliation is not None
            and reconciliation.status is ReconciliationStatus.CONFLICT
        ):
            detail = (
                (detail + "; " if detail else "")
                + "providers disagree; latest is SUSPECT and no price was selected"
            )
            await self._publish(
                "novamarket.data.quality_changed",
                instrument_id=instrument.instrument_id,
                previous=last_observation.quality.value,
                quality=DataQuality.SUSPECT.value,
                detail="provider disagreement",
            )
        return InstrumentIngestResult(
            instrument_id=instrument.instrument_id,
            status="ok",
            provider=last_observation.provider,
            detail=detail,
            quote_id=last_observation.observation_id,
            quote_inserted=inserted > 0,
            quote_duplicate=duplicate > 0,
            quote_rejected=rejected > 0,
        )

    async def _collect_bars(
        self,
        instrument: Instrument,
        interval: DataInterval,
        now: datetime,
    ) -> tuple[int, int, int, str, list[str]]:
        inserted = duplicate = rejected = 0
        provider_name = ""
        errors: list[str] = []
        for adapter in self._compatible(instrument):
            if not adapter.capabilities.supports("ohlcv"):
                continue
            if interval not in adapter.capabilities.intervals:
                errors.append(
                    f"{adapter.name}: interval {interval.value} is unsupported"
                )
                continue
            try:
                rows = await self._provider_call(
                    adapter,
                    "ohlcv",
                    instrument.instrument_id,
                    lambda: adapter.fetch_ohlcv(
                        instrument,
                        interval,
                        limit=self._ingestion.history_limit,
                    ),
                )
            except CircuitOpenError as exc:
                errors.append(f"{adapter.name}: {exc}")
                continue
            except ProviderError as exc:
                errors.append(f"{adapter.name}: {exc}")
                continue
            if not rows:
                errors.append(f"{adapter.name}: no bars returned")
                continue
            provider_name = adapter.name
            accepted = 0
            prepared: list[Any] = []
            for row in rows:
                try:
                    bar = self._normalize_bar(instrument, adapter, row, now)
                    if bar is None:
                        continue
                    if bar.partial and not self._ingestion.allow_partial_bars:
                        continue
                    bar = self._assess_bar(instrument, adapter, bar, now)
                except (ProviderInvalidResponseError, ValueError, TypeError) as exc:
                    errors.append(f"{adapter.name}: malformed bar skipped: {exc}")
                    continue
                prepared.append(bar)
            try:
                # One transaction for the whole provider page.  The previous
                # per-bar commit performed a synchronous SQLite commit on the
                # event loop once per row.
                appends = self.store.append_bars(prepared)
            except MarketDataStoreError as exc:
                self._last_store_error = str(exc)
                errors.append(f"{adapter.name}: store refused the page: {exc}")
                break
            for bar, append in zip(prepared, appends, strict=True):
                if bar.quality is DataQuality.INVALID:
                    rejected += 1
                    await self._publish(
                        "novamarket.data.rejected",
                        instrument_id=instrument.instrument_id,
                        provider=adapter.name,
                        reason="; ".join(bar.reasons)
                        or "structural OHLCV validation failed",
                    )
                    continue
                accepted += 1
                if append.inserted:
                    inserted += 1
                else:
                    duplicate += 1
            if accepted:
                self.invalidate(instrument.instrument_id)
                break
            errors.append(f"{adapter.name}: every bar failed validation")
        return inserted, duplicate, rejected, provider_name, errors

    async def ingest_history(
        self,
        instrument_id: str,
        interval: DataInterval | str,
        *,
        start: datetime,
        end: datetime,
        provider: str | None = None,
        allow_partial: bool | None = None,
        limit: int = 5_000,
    ) -> HistoryIngestReport:
        """Acquire one bounded historical date range through the existing pipeline.

        This is PI-02's acquisition door.  It does not add a collector: the
        adapter is called through ``_provider_call`` (retry, rate limit, circuit
        breaker, health and retrieval log), and every row passes the SAME
        ``_normalize_bar`` / ``_assess_bar`` / ``append_bar`` path as background
        ingestion.  Repeated calls are idempotent because the store's unique key
        keeps the first observation and records conflicts.
        """
        canonical = _bar_interval(interval, parameter="historical interval")
        if start.tzinfo is None or end.tzinfo is None:
            raise MarketDataError("historical acquisition bounds must be timezone-aware")
        if end < start:
            raise MarketDataError("historical acquisition end precedes start")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise MarketDataError("historical acquisition limit must be a positive integer")
        started_at = self._clock()
        instrument = self.get_instrument(instrument_id)
        inserted = duplicate = rejected = outside = received = 0
        provider_name = ""
        errors: list[str] = []
        allow = self._ingestion.allow_partial_bars if allow_partial is None else allow_partial
        adapters: tuple[MarketDataAdapter, ...]
        if provider:
            try:
                selected = self._adapter(provider)
            except MarketDataError as exc:
                errors.append(str(exc))
                adapters = ()
            else:
                adapters = (selected,)
        else:
            adapters = self._compatible(instrument)
            if not adapters:
                errors.append(
                    f"no compatible OHLCV provider is enabled for "
                    f"{instrument.instrument_id}"
                )
        for adapter in adapters:
            if not adapter.capabilities.supports("ohlcv"):
                errors.append(f"{adapter.name}: provider does not supply OHLCV")
                continue
            if canonical not in adapter.capabilities.intervals:
                errors.append(
                    f"{adapter.name}: interval {canonical.value} is unsupported"
                )
                continue
            try:
                rows = await self._provider_call(
                    adapter,
                    "ohlcv",
                    instrument.instrument_id,
                    lambda: adapter.fetch_ohlcv(
                        instrument, canonical, start=start, end=end, limit=limit
                    ),
                )
            except CircuitOpenError as exc:
                errors.append(f"{adapter.name}: {exc}")
                continue
            except ProviderError as exc:
                errors.append(f"{adapter.name}: {exc}")
                continue
            if not rows:
                errors.append(f"{adapter.name}: no bars returned")
                continue
            received += len(rows)
            accepted = 0
            for row in rows:
                try:
                    bar = self._normalize_bar(instrument, adapter, row, started_at)
                    if bar is None:
                        continue
                    if bar.partial and not allow:
                        rejected += 1
                        continue
                    bar = self._assess_bar(instrument, adapter, bar, started_at)
                except (ProviderInvalidResponseError, ValueError, TypeError) as exc:
                    errors.append(f"{adapter.name}: malformed bar skipped: {exc}")
                    rejected += 1
                    continue
                if bar.observed_at < start or bar.observed_at > end:
                    outside += 1
                    continue
                try:
                    append = self.store.append_bar(bar)
                except MarketDataStoreError as exc:
                    self._last_store_error = str(exc)
                    errors.append(f"{adapter.name}: store refused a bar: {exc}")
                    break
                if bar.quality is DataQuality.INVALID:
                    rejected += 1
                    await self._publish(
                        "novamarket.data.rejected",
                        instrument_id=instrument.instrument_id,
                        provider=adapter.name,
                        reason="; ".join(bar.reasons)
                        or "structural OHLCV validation failed",
                    )
                    continue
                accepted += 1
                if append.inserted:
                    inserted += 1
                else:
                    duplicate += 1
            if accepted:
                provider_name = adapter.name
                self.invalidate(instrument.instrument_id)
                break
            errors.append(f"{adapter.name}: every historical bar failed validation")
        finished_at = self._clock()
        return HistoryIngestReport(
            instrument_id=instrument.instrument_id,
            interval=canonical,
            requested_start=start,
            requested_end=end,
            started_at=started_at,
            finished_at=finished_at,
            provider=provider_name,
            bars_received=received,
            bars_inserted=inserted,
            bars_duplicate=duplicate,
            bars_rejected=rejected,
            bars_outside_range=outside,
            errors=tuple(errors),
        )

    # -- normalization --------------------------------------------------------

    def _normalize_quote(
        self,
        instrument: Instrument,
        adapter: MarketDataAdapter,
        quote: ProviderQuote,
        now: datetime,
    ) -> PriceObservation:
        return PriceObservation.create(
            instrument_id=instrument.instrument_id,
            provider=adapter.name,
            price=quote.price,
            currency=quote.currency,
            observed_at=quote.observed_at,
            retrieved_at=now,
            source_timezone=quote.source_timezone,
            provider_metadata={
                **dict(quote.provider_metadata),
                "adapter": adapter.name,
            },
            raw=quote.raw,
        )

    def _assess_quote(
        self,
        instrument: Instrument,
        adapter: MarketDataAdapter,
        observation: PriceObservation,
        now: datetime,
    ) -> PriceObservation:
        freshness = assess_quote_freshness(
            instrument,
            observation,
            now=now,
            settings=self._freshness,
            provider_delay_seconds=adapter.capabilities.typical_delay_seconds,
        )
        previous_candidates = self.store.latest_observations(
            instrument.instrument_id, providers=(adapter.name,)
        )
        previous = next(
            (
                item
                for item in previous_candidates
                if item.observed_at < observation.observed_at
            ),
            None,
        )
        quality = assess_quote_quality(
            observation,
            previous=previous,
            freshness=freshness.freshness,
            settings=self._quality,
        )
        final_quality = quality.quality
        reasons = list(quality.reasons)
        mismatch = self._currency_mismatch(instrument, observation.currency)
        if mismatch:
            # A USD price must never be treated as the INR price of an NSE
            # instrument. It is retained (the provider did say it) and marked
            # SUSPECT so no calculation consumes it.
            final_quality = worst_quality(final_quality, DataQuality.SUSPECT)
            reasons.append(mismatch)
        return replace(
            observation,
            freshness=freshness.freshness,
            quality=final_quality,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    def _normalize_bar(
        self,
        instrument: Instrument,
        adapter: MarketDataAdapter,
        row: ProviderBar,
        now: datetime,
    ) -> OhlcvBar | None:
        partial = self._is_partial(instrument, row.interval, row.observed_at, now)
        if row.partial:
            partial = True
        try:
            return OhlcvBar.create(
                instrument_id=instrument.instrument_id,
                provider=adapter.name,
                interval=row.interval,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                currency=row.currency,
                observed_at=row.observed_at,
                retrieved_at=now,
                source_timezone=row.source_timezone,
                partial=partial,
                provider_metadata={
                    **dict(row.provider_metadata),
                    "adapter": adapter.name,
                },
                raw=row.raw,
            )
        except (ValueError, TypeError) as exc:
            raise ProviderInvalidResponseError(
                f"OHLCV normalization failed: {exc}", provider=adapter.name
            ) from exc

    def _assess_bar(
        self,
        instrument: Instrument,
        adapter: MarketDataAdapter,
        bar: OhlcvBar,
        now: datetime,
    ) -> OhlcvBar:
        freshness = assess_bar_freshness(
            instrument,
            bar,
            now=now,
            settings=self._freshness,
            provider_delay_seconds=adapter.capabilities.typical_delay_seconds,
        )
        recent = self.store.recent_bars(
            instrument.instrument_id,
            provider=adapter.name,
            interval=bar.interval,
            before=bar.observed_at,
            limit=self._quality.volume_median_window,
        )
        quality = assess_bar_quality(
            bar,
            previous_bars=recent,
            freshness=freshness.freshness,
            settings=self._quality,
        )
        final_quality = quality.quality
        reasons = list(quality.reasons)
        mismatch = self._currency_mismatch(instrument, bar.currency)
        if mismatch:
            final_quality = worst_quality(final_quality, DataQuality.SUSPECT)
            reasons.append(mismatch)
        return replace(
            bar,
            freshness=freshness.freshness,
            quality=final_quality,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    @staticmethod
    def _currency_mismatch(instrument: Instrument, currency: str) -> str:
        """A provider's currency must match the instrument's, or say why not."""
        if str(currency).strip().upper() == str(instrument.currency).strip().upper():
            return ""
        return (
            f"provider quoted {currency} but {instrument.instrument_id} trades in "
            f"{instrument.currency}; the value is retained but never used as an "
            f"{instrument.currency} price"
        )

    def _is_partial(
        self,
        instrument: Instrument,
        interval: DataInterval,
        observed_at: datetime,
        now: datetime,
    ) -> bool:
        calendar = calendar_for(instrument)
        if interval is DataInterval.DAY_1:
            if calendar is not None:
                local_day = calendar.local(observed_at).date()
                bounds = calendar.session_bounds(local_day)
                if bounds is not None and now < bounds[1]:
                    return True
                if bounds is None:
                    return now < observed_at + timedelta(days=1)
                return False
            return now < observed_at + timedelta(days=1)
        if interval is DataInterval.WEEK_1:
            return now < observed_at + timedelta(days=7)
        if interval is DataInterval.MONTH_1:
            next_month = _next_month(observed_at.astimezone(UTC).date())
            return now.astimezone(UTC).date() < next_month
        seconds = _interval_seconds(interval)
        return seconds > 0 and now < observed_at + timedelta(seconds=seconds)

    async def _announce_freshness(
        self, instrument: Instrument, observation: PriceObservation
    ) -> None:
        previous = self._last_freshness.get(instrument.instrument_id)
        current = observation.freshness.value
        if previous == current:
            return
        self._last_freshness[instrument.instrument_id] = current
        await self._publish(
            "novamarket.data.freshness_changed",
            asset=instrument.instrument_id,
            fresh=observation.freshness is Freshness.FRESH,
            previous=previous or "unknown",
            freshness=current,
        )

    # -- provider invocation and health --------------------------------------

    def _adapter(self, provider: str) -> MarketDataAdapter:
        adapter = self.registry.get(provider)
        if adapter.name not in self._enabled_names:
            raise MarketDataError(
                f"provider {adapter.name!r} is installed but not enabled; "
                f"enabled: {', '.join(self._enabled_names) or 'none'}"
            )
        return cast(MarketDataAdapter, adapter)

    def _compatible(self, instrument: Instrument) -> tuple[MarketDataAdapter, ...]:
        compatible: list[MarketDataAdapter] = []
        for adapter in self._adapters:
            caps = adapter.capabilities
            if instrument.asset_class not in caps.asset_classes:
                continue
            if (
                not caps.covers_other_exchanges
                and instrument.mic not in caps.exchanges
                and instrument.exchange.upper() not in caps.exchanges
            ):
                continue
            if adapter.name not in instrument.provider_symbols:
                continue
            compatible.append(adapter)
        return tuple(compatible)

    async def _provider_call(
        self,
        adapter: MarketDataAdapter,
        operation: str,
        instrument_id: str | None,
        operation_factory: Callable[[], Awaitable[Any]],
    ) -> Any:
        breaker = self._breakers.get(adapter.name)
        if breaker is None:
            breaker = CircuitBreaker(adapter.name, clock=self._monotonic)
            self._breakers[adapter.name] = breaker
        before = breaker.state.value
        await breaker.before_call()
        started_at = self._clock()
        started = self._monotonic()
        self._provider_attempts += 1
        try:
            result = await operation_factory()
        except ProviderError as exc:
            self._provider_failures += 1
            finished_at = self._clock()
            elapsed = max((self._monotonic() - started) * 1000.0, 0.0)
            # A bad symbol or an unsupported interval is an instrument/config
            # problem, not a provider outage: it is recorded but must not open
            # the provider-wide circuit. Retryable failures and authentication
            # failures are provider-wide and do count.
            if exc.retryable or isinstance(exc, ProviderAuthenticationError):
                await breaker.record_failure()
            try:
                self.store.record_provider_failure(
                    adapter.name,
                    operation,
                    error_type=type(exc).__name__,
                    error=str(exc),
                    latency_ms=elapsed,
                    at=finished_at,
                )
                self.store.record_retrieval(
                    provider=adapter.name,
                    operation=operation,
                    instrument_id=instrument_id,
                    started_at=started_at,
                    finished_at=finished_at,
                    outcome="error",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
            except MarketDataStoreError as store_exc:
                self._last_store_error = str(store_exc)
            await self._publish(
                "novamarket.data.provider_unavailable",
                provider=adapter.name,
                reason=str(exc),
            )
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one adapter bug must not kill a cycle
            finished_at = self._clock()
            elapsed = max((self._monotonic() - started) * 1000.0, 0.0)
            await breaker.record_failure()
            wrapped = ProviderInvalidResponseError(
                f"adapter failed: {type(exc).__name__}: {exc}",
                provider=adapter.name,
            )
            try:
                self.store.record_provider_failure(
                    adapter.name,
                    operation,
                    error_type=type(exc).__name__,
                    error=str(exc),
                    latency_ms=elapsed,
                    at=finished_at,
                )
                self.store.record_retrieval(
                    provider=adapter.name,
                    operation=operation,
                    instrument_id=instrument_id,
                    started_at=started_at,
                    finished_at=finished_at,
                    outcome="error",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
            except MarketDataStoreError as store_exc:
                self._last_store_error = str(store_exc)
            raise wrapped from exc
        finished_at = self._clock()
        elapsed = max((self._monotonic() - started) * 1000.0, 0.0)
        await breaker.record_success()
        try:
            self.store.record_provider_success(
                adapter.name,
                operation,
                latency_ms=elapsed,
                at=finished_at,
            )
            self.store.record_retrieval(
                provider=adapter.name,
                operation=operation,
                instrument_id=instrument_id,
                started_at=started_at,
                finished_at=finished_at,
                outcome="ok",
                records=(
                    len(result)
                    if isinstance(result, (list, tuple))
                    else 1
                ),
            )
        except MarketDataStoreError as exc:
            self._last_store_error = str(exc)
        if before != _CIRCUIT_CLOSED:
            await self._publish(
                "novamarket.data.provider_recovered",
                provider=adapter.name,
                circuit_state=breaker.state.value,
            )
        return result

    # -- reporting and events -------------------------------------------------

    def _build_report(
        self,
        started: datetime,
        trigger: str,
        results: Sequence[InstrumentIngestResult],
    ) -> IngestionReport:
        quotes_inserted = sum(1 for item in results if item.quote_inserted)
        quotes_duplicate = sum(1 for item in results if item.quote_duplicate)
        quotes_rejected = sum(1 for item in results if item.quote_rejected)
        errors = tuple(
            item.detail
            for item in results
            if item.status == "unavailable" and item.detail
        )
        return IngestionReport(
            started_at=started,
            finished_at=self._clock(),
            trigger=trigger,
            instruments_requested=len(results),
            instruments_succeeded=sum(
                1 for item in results if item.status == "ok"
            ),
            instruments_unavailable=sum(
                1 for item in results if item.status == "unavailable"
            ),
            instruments_skipped=sum(
                1 for item in results if item.status == "skipped"
            ),
            quotes_inserted=quotes_inserted,
            quotes_duplicate=quotes_duplicate,
            quotes_rejected=quotes_rejected,
            bars_inserted=sum(item.bars_inserted for item in results),
            bars_duplicate=sum(item.bars_duplicate for item in results),
            bars_rejected=sum(item.bars_rejected for item in results),
            conflicts=sum(
                1
                for item in results
                if "disagree" in item.detail or "conflict" in item.detail
            ),
            provider_attempts=self._provider_attempts,
            provider_failures=self._provider_failures,
            errors=errors,
            results=tuple(results),
        )

    async def _publish(self, name: str, **payload: Any) -> None:
        sink = self._event_sink
        if sink is None:
            return
        try:
            await sink(name, payload)
        except Exception as exc:  # noqa: BLE001 - an event must not fail the work
            self._last_event_error = f"{type(exc).__name__}: {exc}"
            _LOGGER.warning("NovaMarket event %s was not published: %s", name, exc)

    # -- introspection --------------------------------------------------------

    def health_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self._last_store_error:
            reasons.append(f"market-data store: {self._last_store_error}")
        for health in self.store.all_provider_health():
            if (
                health.provider in self._enabled_names
                and health.status is ProviderStatus.UNAVAILABLE
            ):
                reasons.append(
                    f"provider {health.provider} is unavailable: "
                    f"{health.last_error or 'no detail'}"
                )
        if (
            self._ingestion.enabled
            and self._worker_task is None
            and self._last_error
        ):
            reasons.append(f"ingestion: {self._last_error}")
        return tuple(reasons)

    def status(self) -> dict[str, Any]:
        try:
            store_stats = self.store.stats()
            store_ok = True
        except MarketDataStoreError as exc:
            self._last_store_error = str(exc)
            store_stats = {}
            store_ok = False
        total_cache = self._cache_hits + self._cache_misses
        return {
            "store": {
                **store_stats,
                "ok": store_ok,
                "last_error": self._last_store_error,
                "append_only": True,
            },
            "enabled_providers": list(self._enabled_names),
            "providers": list(self.provider_status()),
            "universe": list(self._universe),
            "ingestion": self.ingestion_status(),
            "cache": {
                "quote_entries": len(self._quote_cache),
                "instrument_entries": len(self._instrument_cache),
                "hits": self._cache_hits,
                "misses": self._cache_misses,
                "hit_rate": (
                    round(self._cache_hits / total_cache, 4) if total_cache else 0.0
                ),
                "quote_ttl_seconds": self._cache_settings.quote_ttl_seconds,
                "instrument_ttl_seconds": (
                    self._cache_settings.instrument_ttl_seconds
                ),
            },
            "circuits": {
                name: breaker.state.value
                for name, breaker in sorted(self._breakers.items())
            },
            "last_error": self._last_error,
            "last_event_error": self._last_event_error,
            "last_report": self._last_report.to_dict() if self._last_report else None,
            "cycles": self._cycles,
            "provider_retries": dict(sorted(self._retries_by_provider.items())),
        }

    def contract(self) -> dict[str, Any]:
        return {
            "providers": list(self.provider_status()),
            "enabled_providers": list(self._enabled_names),
            "store": {
                "engine": "sqlite3 (stdlib)",
                "schema_version": self.store.schema_version,
                "path": self.store.path,
                "append_only": [
                    "market_observations",
                    "market_bars",
                    "market_corrections",
                    "market_quality_events",
                    "market_retrieval_log",
                ],
                "duplicate_identity": {
                    "observation": "instrument_id + provider + observed_at",
                    "bar": "instrument_id + provider + interval + observed_at",
                },
                "duplicate_behavior": (
                    "identical identity + identical values is a duplicate; a "
                    "different value is recorded in market_corrections and the "
                    "stored record is marked SUSPECT, never overwritten"
                ),
                "retention": "operational logs are capped; price history is not "
                "silently pruned",
            },
            "intervals": [item.value for item in DataInterval],
            "freshness": FreshnessSettings.schema(),
            "quality": QualitySettings.schema(),
            "ingestion": IngestionSettings.schema(),
            "cache": CacheSettings.schema(),
            "calendar": calendar_contract(),
            "models": {
                "instrument": [
                    "instrument_id",
                    "symbol",
                    "name",
                    "asset_class",
                    "exchange",
                    "mic",
                    "currency",
                    "timezone",
                    "provider_symbols",
                    "trading_status",
                ],
                "price_observation": [
                    "observation_id",
                    "instrument_id",
                    "provider",
                    "price",
                    "currency",
                    "observed_at",
                    "retrieved_at",
                    "source_timezone",
                    "quality",
                    "freshness",
                    "reasons",
                    "provider_metadata",
                ],
                "ohlcv_bar": [
                    "bar_id",
                    "instrument_id",
                    "provider",
                    "interval",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "currency",
                    "observed_at",
                    "retrieved_at",
                    "quality",
                    "freshness",
                    "partial",
                ],
            },
            "limitations": [
                "Yahoo's chart endpoint is unofficial and delayed; it is not a "
                "licensed real-time feed and is not for redistribution.",
                "CoinGecko's free OHLC is 30-minute, one-day, and has no volume.",
                "Alpha Vantage is daily-only and needs an API key; without one it "
                "reports an authentication failure.",
                "Holiday coverage is verified for 2026; other years use session "
                "times and weekdays with calendar_coverage=false.",
                "No real-money trading, portfolio, forecast or backtest is "
                "implemented in NM-02.",
            ],
        }


def _bar_interval(
    value: DataInterval | str, *, parameter: str = "interval"
) -> DataInterval:
    """Parse a public bar interval as a typed 4xx, never a bare ValueError."""
    try:
        interval = DataInterval(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(
            item.value for item in DataInterval if item is not DataInterval.SPOT
        )
        raise UnsupportedIntervalError(
            f"{parameter} {value!r} is not supported; use one of: {allowed}"
        ) from exc
    if interval is DataInterval.SPOT:
        raise UnsupportedIntervalError(
            f"{parameter} must be a bar interval, not spot"
        )
    return interval


def _interval_seconds(interval: DataInterval) -> int:
    return {
        DataInterval.MINUTE_1: 60,
        DataInterval.MINUTE_5: 300,
        DataInterval.MINUTE_15: 900,
        DataInterval.MINUTE_20: 1_200,
        DataInterval.MINUTE_30: 1_800,
        DataInterval.HOUR_1: 3_600,
        DataInterval.DAY_1: 86_400,
    }.get(interval, 0)


def _next_month(day: date) -> date:
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return date(day.year, day.month + 1, 1)


__all__ = ["EventSink", "MarketDataEngine", "ProviderProbe"]
