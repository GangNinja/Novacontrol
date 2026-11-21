"""PI-03 focused tests: gateway policy over the real NM-02 engine and store.

Provider responses stay deterministic and offline.  The fixtures exercise the
real adapter/engine/store path; no test here claims a live exchange feed.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from novacontrol.novamarket.config import NovaMarketConfig
from novacontrol.novamarket.contracts import (
    ConfigurationError,
    ProviderRateLimitedError,
)
from novacontrol.novamarket.marketdata import gateway as gateway_module
from novacontrol.novamarket.marketdata.engine import MarketDataEngine
from novacontrol.novamarket.marketdata.gateway import (
    AcquisitionPriority,
    GatewaySettings,
    MarketDataGateway,
    classify_failure,
)
from novacontrol.novamarket.marketdata.models import (
    AssetClass,
    DataInterval,
    Freshness,
    Instrument,
    ProviderCapabilities,
)
from novacontrol.novamarket.marketdata.providers.base import (
    HttpResponse,
    HttpTransport,
    MarketDataAdapter,
    ProviderQuote,
    RateLimiter,
    RetryPolicy,
    with_retries,
)
from novacontrol.novamarket.marketdata.providers.registry import ProviderRegistry
from novacontrol.novamarket.marketdata.settings import IngestionSettings
from novacontrol.novamarket.marketdata.store import MarketDataStore
from novacontrol.novamarket.service import NovaMarketService

NOW = datetime(2026, 11, 2, 5, 0, tzinfo=UTC)


class _YahooTransport(HttpTransport):
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        return HttpResponse(200, json.dumps(self.payload), {}, url)


def _yahoo_payload(moment: datetime = NOW) -> dict[str, Any]:
    epoch = int(moment.timestamp())
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "currency": "INR",
                        "symbol": "RELIANCE.NS",
                        "exchangeName": "NSI",
                        "fullExchangeName": "NSE",
                        "instrumentType": "EQUITY",
                        "exchangeTimezoneName": "Asia/Kolkata",
                        "regularMarketPrice": 2450.5,
                        "regularMarketTime": epoch,
                        "previousClose": 2400.0,
                        "marketState": "REGULAR",
                        "gmtoffset": 19_800,
                    },
                    "timestamp": [epoch],
                    "indicators": {
                        "quote": [
                            {
                                "open": [2420.0],
                                "high": [2465.0],
                                "low": [2411.0],
                                "close": [2450.5],
                                "volume": [1_250_000],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


class _NoTransport(HttpTransport):
    async def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        return HttpResponse(404, "{}", {}, url)


def _instrument(symbol: str, *, exchange: str = "NSE") -> Instrument:
    return Instrument.create(
        symbol=symbol,
        name=symbol,
        exchange=exchange,
        mic="XNSE" if exchange == "NSE" else exchange,
        asset_class=AssetClass.EQUITY,
        currency="INR",
        timezone="Asia/Kolkata",
        provider_symbols={"fixture": symbol},
    )


def _capabilities(
    *, interval: DataInterval = DataInterval.MINUTE_1, provider: str = "fixture"
) -> ProviderCapabilities:
    return ProviderCapabilities(
        provider=provider,
        asset_classes=(AssetClass.EQUITY,),
        exchanges=("NSE",),
        intervals=(interval,),
        supports_quotes=True,
        supports_ohlcv=False,
        supports_lookup=False,
        data_kind="delayed",
        typical_delay_seconds=60,
        requires_credentials=False,
        rate_limit_per_minute=60,
    )


class _FixtureAdapter(MarketDataAdapter):
    name = "fixture"

    def __init__(
        self,
        *,
        clock: Any,
        fail: Exception | None = None,
        name: str = "fixture",
        price: str = "100",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        super().__init__(
            transport=_NoTransport(),
            capabilities=_capabilities(provider=name),
            limiter=RateLimiter(0),
            retry_policy=RetryPolicy(
                max_attempts=1,
                base_delay_seconds=0,
                max_delay_seconds=0,
                jitter_ratio=0,
            ),
        )
        self._clock = clock
        self.calls = 0
        self.fail = fail
        self.price = price
        self.metadata = dict(metadata or {})
        self.observations: list[datetime] = []

    async def fetch_quote(self, instrument: Instrument) -> ProviderQuote:
        self.calls += 1
        now = self._clock()
        if self.fail is not None:
            raise self.fail
        self.observations.append(now)
        return ProviderQuote(
            price=Decimal(self.price),
            currency="INR",
            observed_at=now,
            source_timezone="Asia/Kolkata",
            provider_metadata={
                "timestamp_basis": "fixture source time",
                **self.metadata,
            },
        )


class _GatewayFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.now = NOW
        self.store = MarketDataStore(
            Path(self._tmp.name) / "market.sqlite3", clock=lambda: self.now
        )
        self.adapter = _FixtureAdapter(clock=lambda: self.now)
        self.instrument = _instrument("RELIANCE")
        self.store.upsert_instrument(self.instrument)
        self.engine = MarketDataEngine(
            store=self.store,
            registry=ProviderRegistry((self.adapter,)),
            enabled_providers=("fixture",),
            universe=(),
            ingestion=IngestionSettings(
                include_quotes=True, include_bars=False, run_when_closed=True
            ),
            clock=lambda: self.now,
        )
        self.gateway = MarketDataGateway(
            self.engine,
            settings=GatewaySettings(
                intervals=(DataInterval.MINUTE_1.value,), run_when_closed=True
            ),
            clock=lambda: self.now,
        )

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    async def asyncTearDown(self) -> None:
        await self.gateway.stop()

    async def test_freshness_is_recomputed_on_read_not_frozen_in_cache(self) -> None:
        await self.engine.run_ingestion([self.instrument.instrument_id])
        first = self.gateway.current(self.instrument.instrument_id)
        self.assertTrue(first.available)
        self.assertEqual(first.freshness, Freshness.FRESH)
        self.assertIsNotNone(first.observed_at)
        self.assertIsNotNone(first.received_at)
        self.assertIsNotNone(first.validated_at)
        self.assertIsNotNone(first.persisted_at)

        self.now = NOW + timedelta(seconds=1_500)
        second = self.gateway.current(self.instrument.instrument_id)
        self.assertFalse(second.fresh)
        self.assertEqual(second.freshness, Freshness.STALE)

    async def test_publication_delay_is_measured_when_a_provider_supplies_it(self) -> None:
        published = self.now - timedelta(seconds=5)
        self.adapter.metadata = {"published_at": published.isoformat()}
        await self.engine.run_ingestion([self.instrument.instrument_id])
        observation = self.gateway.current(self.instrument.instrument_id)
        self.assertEqual(observation.published_at, published)
        self.assertEqual(observation.latency["source_publication_delay_seconds"], 5.0)
        report = self.gateway.status()["latency"]["source_publication_delay"]
        self.assertTrue(report["available"])
        self.assertEqual(report["p50"], 5.0)

    async def test_checkpoint_is_explicit_when_no_fresh_observation_exists(self) -> None:
        await self.engine.run_ingestion([self.instrument.instrument_id])
        self.now = NOW + timedelta(seconds=1_500)
        checkpoint = await self.gateway.checkpoint(
            self.instrument.instrument_id,
            accepted_freshness=(Freshness.FRESH,),
        )
        self.assertFalse(checkpoint["available"])
        self.assertEqual(checkpoint["freshness"], Freshness.STALE.value)
        self.assertIn("not accepted", " ".join(checkpoint["reasons"]))
        observability = self.gateway.status()["observability"]
        self.assertGreaterEqual(observability["counters"]["stale_rejections"], 1)
        self.assertEqual(
            observability["instrument_status"][self.instrument.instrument_id]["freshness"],
            "stale",
        )

    async def test_cache_and_coalescing_do_not_hide_a_forced_refresh(self) -> None:
        await self.gateway.refresh([self.instrument.instrument_id])
        self.assertEqual(self.adapter.calls, 1)
        await self.gateway.refresh([self.instrument.instrument_id])
        self.assertEqual(self.adapter.calls, 1)
        await self.gateway.refresh([self.instrument.instrument_id], force=True)
        self.assertEqual(self.adapter.calls, 2)
        self.gateway.current(self.instrument.instrument_id)
        self.gateway.current(self.instrument.instrument_id)
        status = self.gateway.status()
        self.assertGreaterEqual(status["cache"]["hits"], 1)

    async def test_priorities_cover_every_level_and_fair_rotation_never_duplicates(self) -> None:
        symbols = [f"S{index:02d}" for index in range(25)]
        instruments = [_instrument(symbol) for symbol in symbols]
        for instrument in instruments:
            self.store.upsert_instrument(instrument)
        for index, instrument in enumerate(instruments):
            priority = list(AcquisitionPriority)[index % len(AcquisitionPriority)]
            self.gateway.subscribe(instrument.instrument_id, priority=priority)

        selected = self.gateway.select_batch(
            [item.instrument_id for item in instruments]
        )
        self.assertEqual(len(selected), 20)
        self.assertEqual(len(set(selected)), 20)
        priority_ids = {
            item.instrument_id
            for item in instruments
            if self.gateway._subscriptions[item.instrument_id].priority
            is not AcquisitionPriority.GENERAL
        }
        self.assertTrue(priority_ids.intersection(selected))
        lower_selected = {
            self.gateway._subscriptions[item].priority for item in selected
        }
        self.assertTrue(
            AcquisitionPriority.WATCHLIST in lower_selected
            or AcquisitionPriority.GENERAL in lower_selected
        )

        seen_general = False
        previous = selected
        for _ in range(8):
            batch = self.gateway.select_batch(
                [item.instrument_id for item in instruments]
            )
            self.assertEqual(len(batch), 20)
            self.assertEqual(len(set(batch)), 20)
            seen_general = seen_general or any(
                self.gateway._subscriptions[item].priority
                is AcquisitionPriority.GENERAL
                for item in batch
            )
            previous = batch
        self.assertTrue(seen_general)
        self.assertEqual(len(previous), 20)

    async def test_intraday_cycle_uses_only_intraday_intervals_and_keeps_general_off(self) -> None:
        report = await self.gateway.run_intraday_cycle(
            [self.instrument.instrument_id], trigger="fixture"
        )
        self.assertEqual(report.path, "intraday")
        self.assertEqual(report.selected, (self.instrument.instrument_id,))
        self.assertEqual(self.adapter.calls, 1)
        # The general scheduler was never started by the gateway.
        self.assertIsNone(getattr(self.engine, "_worker_task", None))
        self.assertEqual(report.report["quotes_inserted"], 1)

    async def test_cross_provider_conflict_is_exposed_without_averaging(self) -> None:
        other = _FixtureAdapter(clock=lambda: self.now, name="other", price="120")
        both = Instrument.create(
            symbol=self.instrument.symbol,
            name=self.instrument.name,
            exchange=self.instrument.exchange,
            mic=self.instrument.mic,
            asset_class=self.instrument.asset_class,
            currency=self.instrument.currency,
            timezone=self.instrument.timezone,
            provider_symbols={"fixture": "RELIANCE", "other": "RELIANCE"},
        )
        self.store.upsert_instrument(both)
        engine = MarketDataEngine(
            store=self.store,
            registry=ProviderRegistry((self.adapter, other)),
            enabled_providers=("fixture", "other"),
            universe=(),
            ingestion=IngestionSettings(
                include_quotes=True, include_bars=False, run_when_closed=True
            ),
            clock=lambda: self.now,
        )
        gateway = MarketDataGateway(
            engine, settings=GatewaySettings(run_when_closed=True), clock=lambda: self.now
        )
        report = await engine.run_ingestion([self.instrument.instrument_id])
        self.assertGreaterEqual(report.conflicts, 1)
        envelope = gateway.current(self.instrument.instrument_id)
        self.assertTrue(envelope.provider_conflict)
        self.assertIsNone(envelope.price)
        self.assertEqual(envelope.provenance["observation_count"], 2)
        self.assertEqual(
            {row["provider"] for row in envelope.provenance["conflict_observations"]},
            {"fixture", "other"},
        )
        await gateway.stop()

    async def test_general_cycle_is_bounded_and_reports_an_empty_universe(self) -> None:
        empty = await self.gateway.run_general_cycle()
        self.assertIn("universe is empty", empty.error)
        self.assertEqual(empty.selected, ())
        self.gateway.subscribe(self.instrument.instrument_id)
        cycle = await self.gateway.run_general_cycle()
        self.assertEqual(cycle.path, "general")
        self.assertEqual(cycle.selected, (self.instrument.instrument_id,))
        self.assertEqual(cycle.error, "")
        self.assertIsNotNone(cycle.report)
        self.assertFalse(self.engine.ingestion_status()["running"])

    async def test_gateway_scheduler_is_opt_in_and_start_stop_is_isolated(self) -> None:
        with self.assertRaises(ConfigurationError):
            await self.gateway.start()
        enabled = MarketDataGateway(
            self.engine,
            settings=GatewaySettings(
                enabled=True, run_when_closed=True, max_requests_per_minute=1
            ),
            clock=lambda: self.now,
        )
        enabled.subscribe(self.instrument.instrument_id)
        started = await enabled.start()
        self.assertTrue(started["running"])
        stopped = await enabled.stop()
        self.assertFalse(stopped["running"])
        self.assertIsNone(getattr(self.engine, "_worker_task", None))

    async def test_fallback_is_identified_and_never_labelled_as_the_preferred_source(self) -> None:
        fallback = _FixtureAdapter(clock=lambda: self.now, name="fallback")
        both = Instrument.create(
            symbol=self.instrument.symbol,
            name=self.instrument.name,
            exchange=self.instrument.exchange,
            mic=self.instrument.mic,
            asset_class=self.instrument.asset_class,
            currency=self.instrument.currency,
            timezone=self.instrument.timezone,
            provider_symbols={"fixture": "RELIANCE", "fallback": "RELIANCE"},
        )
        self.store.upsert_instrument(both)
        self.adapter.fail = ProviderRateLimitedError("429", provider="fixture")
        engine = MarketDataEngine(
            store=self.store,
            registry=ProviderRegistry((self.adapter, fallback)),
            enabled_providers=("fixture", "fallback"),
            universe=(),
            ingestion=IngestionSettings(
                include_quotes=True, include_bars=False, run_when_closed=True
            ),
            clock=lambda: self.now,
        )
        gateway = MarketDataGateway(
            engine, settings=GatewaySettings(run_when_closed=True), clock=lambda: self.now
        )
        report = await engine.run_ingestion([self.instrument.instrument_id])
        envelope = gateway.current(self.instrument.instrument_id)
        self.assertEqual(
            envelope.provider,
            "fallback",
            (report.to_dict(), engine.latest(self.instrument.instrument_id).to_dict()),
        )
        self.assertTrue(envelope.is_fallback)
        self.assertIn("preferred provider fixture", envelope.fallback_reason)
        self.assertEqual(envelope.source_class, "third_party_delayed")
        await gateway.stop()

    async def test_validated_observation_events_carry_source_and_reception_metadata(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []

        async def sink(name: str, payload: Any) -> None:
            events.append((name, dict(payload)))

        self.engine.set_event_sink(sink)
        await self.engine.run_ingestion([self.instrument.instrument_id])
        received = [payload for name, payload in events if name == "novamarket.data.received"]
        validated = [payload for name, payload in events if name == "novamarket.data.validated"]
        self.assertTrue(received)
        self.assertTrue(validated)
        self.assertEqual(received[0]["provider"], "fixture")
        self.assertIn("observed_at", received[0])
        self.assertIn("retrieved_at", received[0])
        self.assertIn("freshness", received[0])
        self.assertIn("instrument_id", validated[0])

    async def test_provider_contract_preserves_source_class_and_failure_class(self) -> None:
        await self.engine.run_ingestion([self.instrument.instrument_id])
        contract = self.gateway.provider_contracts()[0]
        self.assertEqual(contract["source_class"], "third_party_delayed")
        self.assertFalse(contract["supports_streaming"])
        self.assertFalse(contract["publication_timestamp_supported"])
        self.assertEqual(contract["schedule"]["rate_limit_per_minute"], 60)

        self.adapter.fail = ProviderRateLimitedError("429 too many", provider="fixture")
        failed = await self.engine.run_ingestion([self.instrument.instrument_id])
        self.assertFalse(failed.ok)
        contract = self.gateway.provider_contracts()[0]
        self.assertEqual(
            contract["failure_class"],
            "rate_limited",
            self.gateway._logs()[:3],
        )
        self.assertGreaterEqual(contract["throttle_events"], 1)

    async def test_retry_observer_is_opt_in_and_does_not_change_retry_policy(self) -> None:
        seen: list[tuple[str, int, float]] = []
        attempts = {"count": 0}

        async def operation() -> str:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise ProviderRateLimitedError("429", provider="fixture")
            return "ok"

        result = await with_retries(
            operation,
            provider="fixture",
            policy=RetryPolicy(
                max_attempts=2,
                base_delay_seconds=0,
                max_delay_seconds=0,
                jitter_ratio=0,
            ),
            sleeper=lambda _: None,
            observer=lambda provider, attempt, delay, error: seen.append(
                (provider, attempt, delay)
            ),
        )
        self.assertEqual(result, "ok")
        self.assertEqual(seen, [("fixture", 1, 0.0)])


class GatewaySettingsTests(unittest.TestCase):
    def test_general_floor_and_batch_cap_cannot_be_weakened(self) -> None:
        with self.assertRaises(ConfigurationError):
            GatewaySettings(max_instruments_per_batch=21)
        with self.assertRaises(ConfigurationError):
            GatewaySettings(general_refresh_floor_seconds=899)
        with self.assertRaises(ConfigurationError):
            GatewaySettings(interval_seconds=900)
        with self.assertRaises(ConfigurationError):
            GatewaySettings(lower_priority_reserved_slots=20)
        with self.assertRaises(ConfigurationError):
            GatewaySettings(intervals=("1d",))

    def test_configuration_round_trip_keeps_gateway_disabled_by_default(self) -> None:
        config = NovaMarketConfig(gateway=GatewaySettings(enabled=True, interval_seconds=45))
        restored = NovaMarketConfig.from_mapping(config.to_dict())
        self.assertTrue(restored.gateway.enabled)
        self.assertEqual(restored.gateway.interval_seconds, 45)
        self.assertFalse(NovaMarketConfig().gateway.enabled)
        self.assertIn(
            "gateway", {row["field"] for row in NovaMarketConfig.schema()}
        )

    def test_failure_classification_is_explicit(self) -> None:
        self.assertEqual(
            classify_failure("ProviderRateLimitedError"), gateway_module.ProviderFailureClass.RATE_LIMITED
        )
        self.assertEqual(
            classify_failure("ProviderTimeoutError"), gateway_module.ProviderFailureClass.TIMEOUT
        )
        self.assertEqual(
            classify_failure("ServiceDown", "HTTP 429"), gateway_module.ProviderFailureClass.RATE_LIMITED
        )


class ServiceGatewayIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_gateway_reads_through_the_real_application_engine(self) -> None:
        with TemporaryDirectory() as directory:
            service = NovaMarketService(
                config=NovaMarketConfig(
                    data_providers=("yahoo",),
                    ingestion=IngestionSettings(
                        include_quotes=True, include_bars=False, run_when_closed=False
                    ),
                ),
                data_dir=directory,
                transport=_YahooTransport(_yahoo_payload()),
                clock=lambda: NOW,
            )
            await service.enable(reason="gateway test")
            registered = service.register_market_instrument(
                {
                    "symbol": "RELIANCE",
                    "name": "Reliance Industries",
                    "exchange": "NSE",
                    "mic": "XNSE",
                    "asset_class": "equity",
                    "currency": "INR",
                    "timezone": "Asia/Kolkata",
                    "provider_symbols": {"yahoo": "RELIANCE.NS"},
                }
            )
            instrument_id = registered["instrument"]["instrument_id"]
            refreshed = await service.gateway_refresh([instrument_id], force=True)
            self.assertEqual(refreshed["error"], "")
            current = service.gateway_current(instrument_id)
            self.assertEqual(current["provider"], "yahoo")
            self.assertTrue(current["fresh"])
            self.assertTrue(current["timestamps"]["persisted_at"])
            self.assertTrue(service.gateway_status()["initialised"])
            await service.shutdown(reason="gateway test")

    async def test_service_exposes_lazy_gateway_status_and_contract_without_network(self) -> None:
        with TemporaryDirectory() as directory:
            service = NovaMarketService(
                config=NovaMarketConfig(
                    data_providers=(),
                    ingestion=IngestionSettings(
                        include_quotes=True, include_bars=False, run_when_closed=False
                    ),
                ),
                data_dir=directory,
                clock=lambda: NOW,
            )
            status = service.gateway_status()
            self.assertTrue(status["implemented"])
            self.assertFalse(status["initialised"])
            contract = service.gateway_contract()
            self.assertEqual(contract["general_refresh_floor_seconds"], 900)
            self.assertIn("gateway", service.status())
            self.assertIn("gateway", service.contracts())
            await service.gateway_stop()
