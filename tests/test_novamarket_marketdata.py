"""NM-02 tests: provider adapters, canonical data, quality, storage, fallback.

The adapters are exercised against RECORDED response shapes through a fake
transport, so the assertions are deterministic and offline. One separately
skipped class performs real provider calls (``NOVAMARKET_LIVE_TESTS=1``); fixture
tests are never presented as live verification.

The pipeline tests drive the real engine, the real append-only SQLite store and
the real quality/freshness code. Nothing here stubs the component under test.
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

try:
    from fastapi.testclient import TestClient  # requires httpx
except Exception:  # pragma: no cover - optional dev dependency
    TestClient = None  # type: ignore[assignment]

from novacontrol.browser import NoopBrowserRunner
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.novamarket.config import NovaMarketConfig
from novacontrol.novamarket.marketdata import quality
from novacontrol.novamarket.marketdata.calendar import SessionState, calendar_for
from novacontrol.novamarket.marketdata.engine import MarketDataEngine
from novacontrol.novamarket.marketdata.freshness import assess_quote as assess_freshness
from novacontrol.novamarket.marketdata.freshness import FreshnessSettings
from novacontrol.novamarket.marketdata.models import (
    AssetClass,
    DataInterval,
    DataQuality,
    Freshness,
    Instrument,
    OhlcvBar,
    PriceObservation,
    ReconciliationStatus,
)
from novacontrol.novamarket.marketdata.providers.alpha_vantage import (
    AlphaVantageAdapter,
)
from novacontrol.novamarket.marketdata.providers.base import (
    CircuitBreaker,
    HttpTransport,
    HttpResponse,
    ProviderBar,
    ProviderInstrument,
    ProviderQuote,
    RateLimiter,
    RetryPolicy,
)
from novacontrol.novamarket.marketdata.providers.coingecko import CoinGeckoAdapter
from novacontrol.novamarket.marketdata.providers.registry import ProviderRegistry
from novacontrol.novamarket.marketdata.providers.yahoo import YahooAdapter
from novacontrol.novamarket.marketdata.settings import CacheSettings, IngestionSettings
from novacontrol.novamarket.marketdata.store import MarketDataStore
from novacontrol.novamarket.contracts import (
    CircuitOpenError,
    ConfigurationError,
    InstrumentNotFoundError,
    ProviderAuthenticationError,
    ProviderInvalidResponseError,
    ProviderRateLimitedError,
    ProviderSymbolNotFoundError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderUnsupportedIntervalError,
)
from novacontrol.novamarket.service import NovaMarketService
from novacontrol.core.events import EventBus

#: A Monday during the NSE session: 10:30 IST. All equity fixtures are built
#: around this instant so market hours never make a test pass or fail by date.
NOW = datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
#: 16:00 IST, after the same session closed.
AFTER_CLOSE = datetime(2026, 11, 2, 10, 30, tzinfo=UTC)

_INSTANT = datetime(2026, 11, 2, 4, 58, tzinfo=UTC)


class FakeTransport(HttpTransport):
    """An in-memory HTTP transport keyed by a URL fragment.

    A mapping value may be an ``HttpResponse``, an exception to raise, or a list
    of either; a list is consumed in order and its last entry repeats, which is
    how the retry and duplicate-ingestion tests drive sequences.
    """

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = dict(routes)
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        self.calls.append((url, dict(params or {})))
        for fragment, value in self.routes.items():
            if fragment not in url:
                continue
            if isinstance(value, list):
                if len(value) > 1:
                    chosen = value.pop(0)
                else:
                    chosen = value[0]
            else:
                chosen = value
            if isinstance(chosen, Exception):
                raise chosen
            if isinstance(chosen, HttpResponse):
                return chosen
            return HttpResponse(200, _json(chosen), {}, url)
        return HttpResponse(404, '{"error": "not found"}', {}, url)


def _json(value: Any) -> str:
    import json

    return json.dumps(value)


def _response(payload: Any, status: int = 200) -> HttpResponse:
    return HttpResponse(status, _json(payload), {}, "https://fake.invalid")


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())


def yahoo_chart(
    *,
    price: object = "2450.50",
    currency: str = "INR",
    observed_at: datetime = _INSTANT,
    day: str = "2026-11-02",
    open_price: str = "2420.00",
    high: str = "2465.00",
    low: str = "2411.00",
    close: str = "2450.50",
    volume: object = 1_250_000,
    include_bars: bool = True,
) -> dict[str, Any]:
    """A Yahoo chart shape, including the fields the adapter relies on."""
    epoch = _epoch(observed_at)
    payload: dict[str, Any] = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "currency": currency,
                        "symbol": "RELIANCE.NS",
                        "exchangeName": "NSI",
                        "fullExchangeName": "NSE",
                        "instrumentType": "EQUITY",
                        "exchangeTimezoneName": "Asia/Kolkata",
                        "regularMarketPrice": price,
                        "regularMarketTime": epoch,
                        "previousClose": 2400.0,
                        "marketState": "REGULAR",
                        "gmtoffset": 19_800,
                    },
                    "timestamp": [epoch],
                    "indicators": {
                        "quote": [
                            {
                                "open": [open_price],
                                "high": [high],
                                "low": [low],
                                "close": [close],
                                "volume": [volume],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }
    if not include_bars:
        payload["chart"]["result"][0]["timestamp"] = []
        payload["chart"]["result"][0]["indicators"] = {"quote": [{}]}
    return payload


def coingecko_quote(price: object = "65000.25", observed_at: datetime = _INSTANT) -> dict[str, Any]:
    return {
        "bitcoin": {
            "usd": price,
            "last_updated_at": _epoch(observed_at),
        }
    }


def coingecko_ohlc(
    *, start: datetime = _INSTANT, count: int = 3
) -> list[list[object]]:
    rows: list[list[object]] = []
    for index in range(count):
        moment = start + timedelta(minutes=30 * index)
        base = Decimal("65000") + index
        rows.append(
            [
                int(moment.timestamp() * 1000),
                str(base),
                str(base + 40),
                str(base - 40),
                str(base + 10),
            ]
        )
    return rows


def equity_instrument() -> Instrument:
    return Instrument.create(
        symbol="RELIANCE",
        name="Reliance Industries",
        exchange="NSE",
        mic="XNSE",
        asset_class=AssetClass.EQUITY,
        currency="INR",
        timezone="Asia/Kolkata",
        provider_symbols={"yahoo": "RELIANCE.NS"},
    )


def crypto_instrument() -> Instrument:
    return Instrument.create(
        symbol="BTC-USD",
        name="Bitcoin",
        exchange="CRYPTO",
        mic="CRYPTO",
        asset_class=AssetClass.CRYPTO,
        currency="USD",
        timezone="UTC",
        provider_symbols={"yahoo": "BTC-USD", "coingecko": "bitcoin"},
    )


def make_yahoo(transport: Any, *, attempts: int = 1) -> YahooAdapter:
    return YahooAdapter(
        transport=transport,
        limiter=RateLimiter(0),
        retry_policy=RetryPolicy(
            max_attempts=attempts,
            base_delay_seconds=0,
            max_delay_seconds=0,
            jitter_ratio=0,
        ),
        clock=lambda: NOW,
    )


def make_coingecko(transport: Any, *, attempts: int = 1) -> CoinGeckoAdapter:
    return CoinGeckoAdapter(
        transport=transport,
        limiter=RateLimiter(0),
        retry_policy=RetryPolicy(
            max_attempts=attempts,
            base_delay_seconds=0,
            max_delay_seconds=0,
            jitter_ratio=0,
        ),
        clock=lambda: NOW,
    )


class ProviderAdapterTests(unittest.IsolatedAsyncioTestCase):
    """Adapters normalize real response shapes and fail honestly."""

    async def test_yahoo_quote_is_normalized_with_source_time_and_currency(self) -> None:
        transport = FakeTransport({"query1.finance.yahoo.com": _response(yahoo_chart())})
        quote = await make_yahoo(transport).fetch_quote(equity_instrument())

        self.assertEqual(quote.price, Decimal("2450.5"))
        self.assertEqual(quote.currency, "INR")
        self.assertEqual(quote.observed_at, _INSTANT)
        self.assertEqual(quote.source_timezone, "Asia/Kolkata")
        self.assertEqual(quote.provider_metadata["timestamp_basis"], "provider regularMarketTime")
        self.assertEqual(quote.provider_metadata["exchange_timezone"], "Asia/Kolkata")

    async def test_yahoo_quote_preserves_provider_day_range_and_volume(self) -> None:
        payload = yahoo_chart()
        payload["chart"]["result"][0]["meta"].update(
            {
                "regularMarketDayHigh": 2466.5,
                "regularMarketDayLow": 2400.25,
                "regularMarketVolume": 987654,
                "regularMarketChangePercent": 2.1,
            }
        )
        transport = FakeTransport({"query1.finance.yahoo.com": _response(payload)})

        quote = await make_yahoo(transport).fetch_quote(equity_instrument())

        self.assertEqual(quote.provider_metadata["day_high"], 2466.5)
        self.assertEqual(quote.provider_metadata["day_low"], 2400.25)
        self.assertEqual(quote.provider_metadata["day_volume"], 987654)
        self.assertEqual(quote.provider_metadata["change_percent"], Decimal("2.1"))

    async def test_yahoo_ohlcv_is_parsed_and_missing_rows_are_skipped(self) -> None:
        payload = yahoo_chart(
            high="2465.00", low="2411.00", open_price="2420.00", close="2450.50"
        )
        payload["chart"]["result"][0]["timestamp"] = [
            _epoch(_INSTANT) - 86_400,
            _epoch(_INSTANT),
        ]
        quote_block = payload["chart"]["result"][0]["indicators"]["quote"][0]
        # The first row is incomplete: it must be skipped, not filled from the
        # second row or from a neighbour.
        for key in ("open", "high", "low", "close", "volume"):
            quote_block[key] = [None, quote_block[key][0]]
        transport = FakeTransport({"query1.finance.yahoo.com": _response(payload)})

        bars = await make_yahoo(transport).fetch_ohlcv(
            equity_instrument(), DataInterval.DAY_1
        )

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].close, Decimal("2450.5"))
        self.assertEqual(bars[0].volume, Decimal("1250000"))
        self.assertEqual(bars[0].currency, "INR")

    async def test_yahoo_unknown_symbol_and_schema_change_are_distinct(self) -> None:
        missing = FakeTransport(
            {
                "query1.finance.yahoo.com": _response(
                    {"chart": {"result": None, "error": {"code": "Not Found"}}}
                )
            }
        )
        with self.assertRaises(ProviderSymbolNotFoundError):
            await make_yahoo(missing).fetch_quote(equity_instrument())

        changed = FakeTransport(
            {"query1.finance.yahoo.com": _response({"chart": {"result": [{}]}})}
        )
        with self.assertRaises(ProviderInvalidResponseError):
            await make_yahoo(changed).fetch_quote(equity_instrument())

    async def test_retries_are_bounded_and_a_retryable_failure_recovers(self) -> None:
        transport = FakeTransport(
            {
                "query1.finance.yahoo.com": [
                    _response({"error": "unavailable"}, status=503),
                    _response({"error": "unavailable"}, status=503),
                    _response(yahoo_chart()),
                ]
            }
        )
        quote = await make_yahoo(transport, attempts=3).fetch_quote(equity_instrument())
        self.assertEqual(quote.price, Decimal("2450.5"))
        self.assertEqual(len(transport.calls), 3)

    async def test_a_timeout_is_retried_then_reported(self) -> None:
        transport = FakeTransport(
            {"query1.finance.yahoo.com": ProviderTimeoutError("no answer", provider="yahoo")}
        )
        with self.assertRaises(ProviderTimeoutError):
            await make_yahoo(transport, attempts=3).fetch_quote(equity_instrument())
        self.assertEqual(len(transport.calls), 3)

    async def test_a_connection_failure_is_retried_then_reported(self) -> None:
        transport = FakeTransport(
            {
                "query1.finance.yahoo.com": ProviderUnavailableError(
                    "the provider could not be reached", provider="yahoo"
                )
            }
        )
        with self.assertRaises(ProviderUnavailableError):
            await make_yahoo(transport, attempts=2).fetch_quote(equity_instrument())
        self.assertEqual(len(transport.calls), 2)

    async def test_http_429_honors_retry_after_and_fails_closed_when_exhausted(self) -> None:
        rate_limited = HttpResponse(
            429,
            '{"error": "rate limited"}',
            {"retry-after": "0"},
            "https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS",
        )
        recovered = FakeTransport(
            {"query1.finance.yahoo.com": [rate_limited, _response(yahoo_chart())]}
        )
        quote = await make_yahoo(recovered, attempts=2).fetch_quote(equity_instrument())
        self.assertEqual(quote.price, Decimal("2450.5"))
        self.assertEqual(len(recovered.calls), 2)

        exhausted = FakeTransport({"query1.finance.yahoo.com": rate_limited})
        with self.assertRaises(ProviderRateLimitedError) as caught:
            await make_yahoo(exhausted, attempts=2).fetch_quote(equity_instrument())
        self.assertEqual(caught.exception.retry_after, 0.0)
        self.assertEqual(len(exhausted.calls), 2)

    async def test_coingecko_quote_and_volume_free_ohlc(self) -> None:
        transport = FakeTransport(
            {
                "api.coingecko.com/api/v3/simple/price": _response(coingecko_quote()),
                "api.coingecko.com/api/v3/coins/bitcoin/ohlc": _response(
                    coingecko_ohlc()
                ),
            }
        )
        adapter = make_coingecko(transport)
        quote = await adapter.fetch_quote(crypto_instrument())
        self.assertEqual(quote.price, Decimal("65000.25"))
        self.assertEqual(quote.currency, "USD")
        self.assertTrue(quote.provider_metadata["aggregated_across_exchanges"])

        bars = await adapter.fetch_ohlcv(crypto_instrument(), DataInterval.MINUTE_30)
        self.assertEqual(len(bars), 3)
        self.assertIsNone(bars[0].volume, "missing volume must be absent, not zero")
        with self.assertRaises(ProviderUnsupportedIntervalError):
            await adapter.fetch_ohlcv(crypto_instrument(), DataInterval.DAY_1)

    async def test_alpha_vantage_without_a_key_is_an_authentication_failure(self) -> None:
        adapter = AlphaVantageAdapter(
            transport=FakeTransport({}),
            limiter=RateLimiter(0),
            retry_policy=RetryPolicy(max_attempts=1),
        )
        with self.assertRaises(ProviderAuthenticationError):
            await adapter.fetch_quote(
                Instrument.create(
                    symbol="AAPL",
                    exchange="NASDAQ",
                    mic="XNAS",
                    currency="USD",
                    timezone="America/New_York",
                    provider_symbols={"alpha-vantage": "AAPL"},
                )
            )

    async def test_adapter_capabilities_are_declared_not_guessed(self) -> None:
        yahoo = make_yahoo(FakeTransport({}))
        self.assertIn("XNSE", yahoo.capabilities.exchanges)
        self.assertTrue(yahoo.capabilities.supports("ohlcv"))
        self.assertEqual(yahoo.capabilities.data_kind, "delayed")

        coingecko = make_coingecko(FakeTransport({}))
        self.assertEqual(coingecko.capabilities.exchanges, ("CRYPTO",))
        self.assertNotIn(DataInterval.DAY_1, coingecko.capabilities.intervals)
        self.assertTrue(coingecko.capabilities.requires_credentials is False)


class StoreIntegrityTests(unittest.TestCase):
    """The append-only store keeps history, handles duplicates and paginates."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "market.sqlite3"
        self.store = MarketDataStore(self.path, clock=lambda: NOW)
        self.instrument = self.store.upsert_instrument(equity_instrument())

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _observation(
        self,
        *,
        price: str = "2450.50",
        observed_at: datetime = _INSTANT,
        provider: str = "yahoo",
        currency: str = "INR",
    ) -> PriceObservation:
        return PriceObservation.create(
            instrument_id=self.instrument.instrument_id,
            provider=provider,
            price=price,
            currency=currency,
            observed_at=observed_at,
            retrieved_at=NOW,
            source_timezone="Asia/Kolkata",
        )

    def test_identical_ingestion_is_idempotent(self) -> None:
        first = self.store.append_observation(self._observation())
        second = self.store.append_observation(self._observation())

        self.assertTrue(first.inserted)
        self.assertFalse(second.inserted)
        self.assertTrue(second.duplicate)
        self.assertEqual(self.store.observations(self.instrument.instrument_id).total, 1)

    def test_a_changed_value_is_a_traceable_correction_not_an_overwrite(self) -> None:
        self.store.append_observation(self._observation(price="2450.50"))
        conflict = self.store.append_observation(self._observation(price="9999.00"))

        self.assertTrue(conflict.conflict)
        self.assertFalse(conflict.inserted)
        page = self.store.observations(self.instrument.instrument_id)
        self.assertEqual(page.total, 1)
        self.assertEqual(page.items[0].price, Decimal("2450.50"))
        self.assertEqual(page.items[0].quality, DataQuality.SUSPECT)
        corrections = self.store.corrections()
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["incoming"]["price"], "9999.00")
        self.assertEqual(corrections[0]["previous"]["price"], "2450.50")

    def test_history_survives_reopening_the_database(self) -> None:
        self.store.append_observation(self._observation())
        self.store.close()
        self.store = MarketDataStore(self.path, clock=lambda: NOW)

        page = self.store.observations(self.instrument.instrument_id)
        self.assertEqual(page.total, 1)
        self.assertEqual(page.items[0].currency, "INR")
        self.assertEqual(self.store.check()["schema_version"], 1)

    def test_pagination_is_bounded_and_reports_has_more(self) -> None:
        for index in range(5):
            self.store.append_observation(
                self._observation(
                    price=str(2400 + index),
                    observed_at=_INSTANT - timedelta(minutes=index),
                )
            )
        page = self.store.observations(
            self.instrument.instrument_id, limit=2, offset=0, ascending=True
        )
        self.assertEqual(len(page.items), 2)
        self.assertEqual(page.total, 5)
        self.assertTrue(page.to_dict()["has_more"])

    def test_provider_health_and_retrieval_log_are_persisted(self) -> None:
        self.store.record_provider_success("yahoo", "quote", latency_ms=42.0)
        self.store.record_retrieval(
            provider="yahoo",
            operation="quote",
            instrument_id=self.instrument.instrument_id,
            started_at=_INSTANT,
            finished_at=NOW,
            outcome="ok",
            records=1,
        )
        self.store.record_provider_failure(
            "yahoo", "quote", error_type="ProviderTimeoutError", error="slow"
        )
        self.store.record_retrieval(
            provider="yahoo",
            operation="quote",
            instrument_id=self.instrument.instrument_id,
            started_at=_INSTANT,
            finished_at=NOW,
            outcome="error",
            error_type="ProviderTimeoutError",
            error_message="slow",
        )
        health = self.store.provider_health("yahoo")
        self.assertIn("quote", health.verified_operations)
        self.assertEqual(health.consecutive_failures, 1)
        self.assertEqual(len(self.store.retrieval_log()), 2)


class QualityAndFreshnessTests(unittest.TestCase):
    """Anomalies are flagged, genuine moves survive, and staleness is not INVALID."""

    def _quote(self, price: str, *, observed_at: datetime = _INSTANT, currency: str = "INR") -> PriceObservation:
        return PriceObservation.create(
            instrument_id="XNSE:RELIANCE",
            provider="yahoo",
            price=price,
            currency=currency,
            observed_at=observed_at,
            retrieved_at=NOW,
            source_timezone="Asia/Kolkata",
        )

    def test_a_genuine_large_move_is_flagged_but_never_deleted(self) -> None:
        previous = self._quote("100", observed_at=_INSTANT - timedelta(minutes=30))
        warning = quality.assess_quote(
            self._quote("125"), previous=previous, freshness=Freshness.FRESH
        )
        self.assertEqual(warning.quality, DataQuality.WARNING)
        self.assertIn("price_jump_warning", warning.flags)
        self.assertTrue(self._quote("125").usable)

        suspect = quality.assess_quote(
            self._quote("160"), previous=previous, freshness=Freshness.FRESH
        )
        self.assertEqual(suspect.quality, DataQuality.SUSPECT)
        self.assertIn("price_jump_suspect", suspect.flags)

    def test_a_previous_close_is_stale_not_invalid(self) -> None:
        instrument = equity_instrument()
        close = self._quote("2450.50", observed_at=_INSTANT - timedelta(days=3))
        assessment = assess_freshness(
            instrument,
            close,
            now=NOW,
            settings=FreshnessSettings(),
            provider_delay_seconds=900,
        )
        self.assertEqual(assessment.freshness, Freshness.STALE)
        self.assertIn("stale", " ".join(assessment.reasons).lower())
        self.assertFalse(assessment.live)

    def test_ohlcv_structural_validation_refuses_impossible_bars(self) -> None:
        common = dict(
            instrument_id="XNSE:RELIANCE",
            provider="yahoo",
            interval=DataInterval.DAY_1,
            currency="INR",
            observed_at=_INSTANT,
            retrieved_at=NOW,
            source_timezone="Asia/Kolkata",
        )
        impossible = OhlcvBar.create(open="10", high="9", low="11", close="10", **common)
        self.assertEqual(impossible.quality, DataQuality.INVALID)
        self.assertIn("high is below low", " ".join(impossible.reasons))

        outside = OhlcvBar.create(open="10", high="12", low="9", close="13", **common)
        self.assertEqual(outside.quality, DataQuality.INVALID)
        self.assertIn("close is outside", " ".join(outside.reasons))

        negative = OhlcvBar.create(
            open="10", high="12", low="9", close="11", volume="-1", **common
        )
        self.assertEqual(negative.quality, DataQuality.INVALID)
        self.assertIn("volume is negative", " ".join(negative.reasons))

    def test_volume_outlier_is_flagged_against_the_recent_median(self) -> None:
        base = dict(
            instrument_id="XNSE:RELIANCE",
            provider="yahoo",
            interval=DataInterval.DAY_1,
            open="10",
            high="12",
            low="9",
            close="11",
            currency="INR",
            retrieved_at=NOW,
            source_timezone="Asia/Kolkata",
        )
        previous = tuple(
            OhlcvBar.create(
                volume="100",
                observed_at=_INSTANT - timedelta(days=index + 1),
                **base,
            )
            for index in range(4)
        )
        assessment = quality.assess_bar(
            OhlcvBar.create(
                volume="2000", observed_at=_INSTANT, **base
            ),
            previous_bars=previous,
            freshness=Freshness.FRESH,
        )
        self.assertEqual(assessment.quality, DataQuality.WARNING)
        self.assertIn("volume_warning", assessment.flags)

    def test_reconciliation_selects_by_priority_and_never_averages(self) -> None:
        instrument = equity_instrument()
        first = self._quote("100")
        second = PriceObservation.create(
            instrument_id="XNSE:RELIANCE",
            provider="coingecko",
            price="100.1",
            currency="INR",
            observed_at=_INSTANT,
            retrieved_at=NOW,
            source_timezone="Asia/Kolkata",
        )
        agreed = quality.reconcile(
            (first, second), instrument=instrument, priority=("yahoo", "coingecko")
        )
        self.assertEqual(agreed.status, ReconciliationStatus.OK)
        self.assertIsNotNone(agreed.selected)
        self.assertEqual(agreed.selected.provider, "yahoo")

        far = PriceObservation.create(
            instrument_id="XNSE:RELIANCE",
            provider="coingecko",
            price="120",
            currency="INR",
            observed_at=_INSTANT,
            retrieved_at=NOW,
            source_timezone="Asia/Kolkata",
        )
        conflict = quality.reconcile(
            (first, far), instrument=instrument, priority=("yahoo",)
        )
        self.assertEqual(conflict.status, ReconciliationStatus.CONFLICT)
        self.assertIsNone(conflict.selected)
        self.assertTrue(
            any("no price was averaged" in reason for reason in conflict.reasons)
        )

    def test_a_cross_currency_pair_is_a_conflict(self) -> None:
        instrument = equity_instrument()
        inr = self._quote("2450")
        usd = self._quote("29.50", currency="USD")
        result = quality.reconcile(
            (inr, usd), instrument=instrument, priority=("yahoo",)
        )
        self.assertEqual(result.status, ReconciliationStatus.CONFLICT)


class EnginePipelineTests(unittest.IsolatedAsyncioTestCase):
    """One real engine: fetch → normalize → validate → persist → reconcile."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = MarketDataStore(
            Path(self._tmp.name) / "market.sqlite3", clock=lambda: NOW
        )
        self.store.upsert_instrument(equity_instrument())

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _engine(
        self,
        adapters: tuple[Any, ...],
        *,
        now: datetime = NOW,
        ingestion: IngestionSettings | None = None,
        cache: CacheSettings | None = None,
        history_page_limit: int = 200,
    ) -> MarketDataEngine:
        return MarketDataEngine(
            store=self.store,
            registry=ProviderRegistry(adapters),
            enabled_providers=tuple(adapter.name for adapter in adapters),
            universe=(),
            ingestion=ingestion
            or IngestionSettings(include_quotes=True, include_bars=False),
            cache=cache,
            clock=lambda: now,
            history_page_limit=history_page_limit,
        )

    async def test_a_quote_is_fetched_normalized_persisted_and_readable(self) -> None:
        transport = FakeTransport({"query1.finance.yahoo.com": _response(yahoo_chart())})
        engine = self._engine((make_yahoo(transport),))

        report = await engine.run_ingestion(["XNSE:RELIANCE"])

        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.quotes_inserted, 1)
        self.assertEqual(report.results[0].provider, "yahoo")
        latest = engine.latest("XNSE:RELIANCE")
        self.assertEqual(latest.status, ReconciliationStatus.OK)
        self.assertIsNotNone(latest.selected)
        assert latest.selected is not None
        self.assertEqual(latest.selected.price, Decimal("2450.5"))
        self.assertEqual(latest.selected.currency, "INR")
        self.assertEqual(latest.selected.freshness, Freshness.FRESH)
        self.assertTrue(latest.selected.live)
        provenance = latest.selected.provenance()
        self.assertEqual(provenance["provider"], "yahoo")
        self.assertEqual(provenance["source_timezone"], "Asia/Kolkata")
        self.assertIn("timestamp_basis", provenance["provider_metadata"])
        self.assertEqual(engine.store.stats()["observations"], 1)

    async def test_an_explicit_force_quote_reaches_the_provider_while_closed(self) -> None:
        transport = FakeTransport({"query1.finance.yahoo.com": _response(yahoo_chart())})
        engine = self._engine(
            (make_yahoo(transport),),
            now=AFTER_CLOSE,
            ingestion=IngestionSettings(
                include_quotes=True, include_bars=False, run_when_closed=False
            ),
        )

        baseline = await engine.run_ingestion(["XNSE:RELIANCE"])
        self.assertEqual(baseline.quotes_inserted, 0)
        self.assertEqual(baseline.instruments_skipped, 1)

        forced = await engine.run_ingestion(
            ["XNSE:RELIANCE"], force_quotes=True
        )
        self.assertEqual(forced.quotes_inserted, 1)
        self.assertEqual(forced.results[0].status, "ok")
        self.assertEqual(engine.store.stats()["observations"], 1)

    async def test_a_bars_only_cycle_spends_one_provider_request(self) -> None:
        transport = FakeTransport(
            {"query1.finance.yahoo.com": _response(yahoo_chart())}
        )
        engine = self._engine(
            (make_yahoo(transport),),
            ingestion=IngestionSettings(
                include_quotes=True,
                include_bars=True,
            ),
        )

        report = await engine.run_ingestion(
            ["XNSE:RELIANCE"],
            intervals=["1m"],
            include_quotes=False,
        )

        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.quotes_inserted, 0)
        self.assertGreater(report.bars_inserted, 0)
        # A watchlist pass chooses bars over quotes, so it pays one request per
        # member rather than the two a quote-plus-bar cycle would spend.
        self.assertEqual(len(transport.calls), 1)

    async def test_duplicate_ingestion_is_idempotent(self) -> None:
        transport = FakeTransport({"query1.finance.yahoo.com": _response(yahoo_chart())})
        engine = self._engine((make_yahoo(transport),))
        first = await engine.run_ingestion(["XNSE:RELIANCE"])
        second = await engine.run_ingestion(["XNSE:RELIANCE"])

        self.assertEqual(first.quotes_inserted, 1)
        self.assertEqual(second.quotes_inserted, 0)
        self.assertTrue(second.quotes_duplicate)
        self.assertEqual(engine.store.stats()["observations"], 1)

    async def test_a_conflicting_repeat_is_retained_as_a_correction(self) -> None:
        first = FakeTransport({"query1.finance.yahoo.com": _response(yahoo_chart())})
        engine = self._engine((make_yahoo(first),))
        await engine.run_ingestion(["XNSE:RELIANCE"])

        # Same provider timestamp, different price: a correction, not a new
        # observation and not a silent overwrite.
        second = FakeTransport(
            {"query1.finance.yahoo.com": _response(yahoo_chart(price="2500.00"))}
        )
        engine.registry = ProviderRegistry((make_yahoo(second),))
        engine.reconfigure(enabled_providers=("yahoo",))
        report = await engine.run_ingestion(["XNSE:RELIANCE"])

        self.assertEqual(report.quotes_inserted, 0)
        self.assertEqual(engine.store.stats()["observations"], 1)
        self.assertEqual(len(engine.store.corrections()), 1)
        retained = engine.store.observations("XNSE:RELIANCE").items[0]
        self.assertEqual(retained.price, Decimal("2450.5"))
        self.assertEqual(retained.quality, DataQuality.SUSPECT)
        # A SUSPECT record is preserved but is not usable, so no price is
        # selected for downstream calculation.
        self.assertFalse(engine.latest("XNSE:RELIANCE").usable)
        # A stored observation that cannot be used IS a provider problem, and
        # that is a different answer from "nothing is stored yet".
        self.assertEqual(
            engine.freshness_of("XNSE:RELIANCE").freshness,
            Freshness.PROVIDER_UNAVAILABLE,
        )

    async def test_a_cross_currency_quote_is_never_used_as_an_inr_price(self) -> None:
        transport = FakeTransport(
            {"query1.finance.yahoo.com": _response(yahoo_chart(currency="USD", price="29.50"))}
        )
        engine = self._engine((make_yahoo(transport),))

        report = await engine.run_ingestion(["XNSE:RELIANCE"])

        self.assertEqual(report.quotes_inserted, 1)
        stored = engine.store.observations("XNSE:RELIANCE").items[0]
        self.assertEqual(stored.currency, "USD")
        self.assertEqual(stored.quality, DataQuality.SUSPECT)
        self.assertIn("trades in INR", " ".join(stored.reasons))
        latest = engine.latest("XNSE:RELIANCE")
        self.assertFalse(latest.usable)

    async def test_a_closed_market_with_nothing_stored_is_missing_not_an_outage(
        self,
    ) -> None:
        # Sunday 10:30 IST: the exchange is shut for the weekend.
        sunday = datetime(2026, 11, 1, 5, 0, tzinfo=UTC)
        engine = self._engine((make_yahoo(FakeTransport({})),), now=sunday)

        assessment = engine.freshness_of("XNSE:RELIANCE")

        # The exchange is shut, so no live quote exists. That is expected, not a
        # provider failure, and the reason has to say which one it is.
        self.assertEqual(assessment.freshness, Freshness.MISSING)
        self.assertEqual(assessment.market_state, SessionState.WEEKEND)
        reasons = " ".join(assessment.reasons)
        self.assertIn("market is weekend", reasons)
        self.assertNotIn("no provider could supply", reasons)

    async def test_a_stale_observation_is_stored_as_stale_not_live(self) -> None:
        old = _INSTANT - timedelta(days=3)
        transport = FakeTransport(
            {"query1.finance.yahoo.com": _response(yahoo_chart(observed_at=old))}
        )
        engine = self._engine((make_yahoo(transport),))
        await engine.run_ingestion(["XNSE:RELIANCE"])

        latest = engine.latest("XNSE:RELIANCE")
        assert latest.selected is not None
        self.assertEqual(latest.selected.freshness, Freshness.STALE)
        self.assertFalse(latest.selected.live)
        self.assertIn(DataQuality.STALE.value, latest.selected.quality.value)

    async def test_ohlcv_bars_are_validated_and_persisted_after_the_close(self) -> None:
        payload = yahoo_chart(observed_at=datetime(2026, 11, 2, 3, 45, tzinfo=UTC))
        transport = FakeTransport({"query1.finance.yahoo.com": _response(payload)})
        engine = self._engine(
            (make_yahoo(transport),),
            now=AFTER_CLOSE,
            ingestion=IngestionSettings(
                include_quotes=False,
                include_bars=True,
                intervals=(DataInterval.DAY_1.value,),
            ),
        )
        report = await engine.run_ingestion(["XNSE:RELIANCE"])

        self.assertEqual(report.bars_inserted, 1)
        page = engine.bars("XNSE:RELIANCE", interval=DataInterval.DAY_1)
        self.assertEqual(page.total, 1)
        bar = page.items[0]
        self.assertEqual(bar.close, Decimal("2450.5"))
        self.assertFalse(bar.partial)
        self.assertTrue(bar.usable)
        self.assertEqual(engine.latest("XNSE:RELIANCE").status, ReconciliationStatus.UNAVAILABLE)

    async def test_a_quote_from_an_uninstalled_provider_stays_readable(self) -> None:
        self.store.append_observation(
            PriceObservation.create(
                instrument_id="XNSE:RELIANCE",
                provider="retired-feed",
                price="2450.50",
                currency="INR",
                observed_at=_INSTANT,
                retrieved_at=NOW,
                source_timezone="Asia/Kolkata",
                freshness=Freshness.FRESH,
            )
        )
        engine = self._engine((make_yahoo(FakeTransport({})),))

        latest = engine.latest("XNSE:RELIANCE")

        self.assertTrue(latest.usable)
        self.assertIsNotNone(latest.selected)
        assert latest.selected is not None
        self.assertIn("not installed", " ".join(latest.selected.reasons))

    async def test_the_history_page_limit_caps_an_unbounded_request(self) -> None:
        transport = FakeTransport({"query1.finance.yahoo.com": _response(yahoo_chart())})
        engine = self._engine((make_yahoo(transport),), history_page_limit=1)
        await engine.run_ingestion(["XNSE:RELIANCE"])
        for minute in range(1, 4):
            engine.store.append_observation(
                PriceObservation.create(
                    instrument_id="XNSE:RELIANCE",
                    provider="yahoo",
                    price=str(2400 + minute),
                    currency="INR",
                    observed_at=_INSTANT - timedelta(minutes=minute),
                    retrieved_at=NOW,
                    source_timezone="Asia/Kolkata",
                )
            )
        page = engine.observations("XNSE:RELIANCE", limit=10_000)
        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.limit, 1)
        self.assertEqual(page.total, 4)


class FallbackAndCircuitTests(unittest.IsolatedAsyncioTestCase):
    """A dead provider cannot block a healthy one, and its circuit opens."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = MarketDataStore(
            Path(self._tmp.name) / "market.sqlite3", clock=lambda: NOW
        )
        self.store.upsert_instrument(crypto_instrument())
        self.failing_yahoo = FakeTransport(
            {"query1.finance.yahoo.com": ProviderUnavailableError("down", provider="yahoo")}
        )
        self.coingecko = FakeTransport(
            {
                "api.coingecko.com/api/v3/simple/price": _response(coingecko_quote()),
                "api.coingecko.com/api/v3/coins/bitcoin/ohlc": _response(
                    coingecko_ohlc()
                ),
            }
        )

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _engine(self, yahoo: Any, coingecko: Any) -> MarketDataEngine:
        return MarketDataEngine(
            store=self.store,
            registry=ProviderRegistry(
                (
                    make_yahoo(yahoo),
                    make_coingecko(coingecko),
                )
            ),
            enabled_providers=("yahoo", "coingecko"),
            universe=(),
            ingestion=IngestionSettings(include_quotes=True, include_bars=False),
            clock=lambda: NOW,
        )

    async def test_secondary_provider_supplies_data_after_the_primary_fails(self) -> None:
        engine = self._engine(self.failing_yahoo, self.coingecko)
        report = await engine.run_ingestion(["CRYPTO:BTC-USD"])

        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.provider_attempts, 2)
        self.assertEqual(report.provider_failures, 1)
        result = report.results[0]
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.provider, "coingecko")
        latest = engine.latest("CRYPTO:BTC-USD")
        assert latest.selected is not None
        self.assertEqual(latest.selected.provider, "coingecko")
        self.assertEqual(latest.selected.currency, "USD")

    async def test_both_providers_failing_reports_unavailable_and_keeps_nothing(self) -> None:
        dead_coingecko = FakeTransport(
            {"api.coingecko.com": ProviderUnavailableError("down", provider="coingecko")}
        )
        engine = self._engine(self.failing_yahoo, dead_coingecko)
        report = await engine.run_ingestion(["CRYPTO:BTC-USD"])

        self.assertFalse(report.ok)
        self.assertEqual(report.instruments_unavailable, 1)
        self.assertIn("down", report.results[0].detail)
        self.assertEqual(engine.store.stats()["observations"], 0)

    async def test_the_circuit_opens_and_stops_spending_provider_requests(self) -> None:
        engine = self._engine(self.failing_yahoo, self.coingecko)
        for _ in range(3):
            await engine.run_ingestion(["CRYPTO:BTC-USD"])
        calls_after_open = len(self.failing_yahoo.calls)
        self.assertEqual(calls_after_open, 3)

        report = await engine.run_ingestion(["CRYPTO:BTC-USD"])
        self.assertEqual(len(self.failing_yahoo.calls), calls_after_open)
        self.assertIn("circuit", report.results[0].detail.lower())
        self.assertEqual(engine.status()["circuits"]["yahoo"], "open")

    async def test_provider_disagreement_is_visible_and_not_averaged(self) -> None:
        yahoo = FakeTransport(
            {
                "query1.finance.yahoo.com": _response(
                    yahoo_chart(price="65000.00", currency="USD")
                )
            }
        )
        coingecko = FakeTransport(
            {
                "api.coingecko.com/api/v3/simple/price": _response(
                    coingecko_quote(price="70000.00")
                )
            }
        )
        engine = self._engine(yahoo, coingecko)
        report = await engine.run_ingestion(["CRYPTO:BTC-USD"])

        latest = engine.latest("CRYPTO:BTC-USD")
        self.assertEqual(latest.status, ReconciliationStatus.CONFLICT)
        self.assertIsNone(latest.selected)
        self.assertEqual(len(engine.store.latest_observations("CRYPTO:BTC-USD")), 2)
        self.assertTrue(
            any("disagree" in error or "disagree" in result.detail for error in report.errors)
            or any("disagree" in item.detail for item in report.results)
        )


class CircuitBreakerUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_half_open_allows_exactly_one_probe(self) -> None:
        clock = [0.0]
        breaker = CircuitBreaker(
            "yahoo", failure_threshold=2, cooldown_seconds=10, clock=lambda: clock[0]
        )
        await breaker.record_failure()
        await breaker.record_failure()
        with self.assertRaises(CircuitOpenError):
            await breaker.before_call()

        clock[0] = 11.0
        await breaker.before_call()
        with self.assertRaises(CircuitOpenError):
            await breaker.before_call()
        await breaker.record_success()
        self.assertEqual(breaker.state.value, "closed")


class ServiceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """NovaControl's service owns the engine, worker and persistent repository."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.bus = EventBus(continue_on_error=True, history=400)
        self.transport = FakeTransport(
            {"query1.finance.yahoo.com": _response(yahoo_chart())}
        )

    async def asyncTearDown(self) -> None:
        await self.service.shutdown(reason="test teardown")
        self._tmp.cleanup()

    def _service(self, **config: Any) -> NovaMarketService:
        self.service = NovaMarketService(
            event_bus=self.bus,
            clock=lambda: NOW,
            data_dir=self._tmp.name,
            transport=self.transport,
            config=NovaMarketConfig.from_mapping(
                {
                    "data_providers": ["yahoo"],
                    "universe": ["XNSE:RELIANCE"],
                    **config,
                }
            ),
        )
        return self.service

    async def test_market_data_is_available_through_the_real_lifecycle(self) -> None:
        service = self._service()
        await service.enable()

        status = service.market_data_status()
        self.assertTrue(status["initialised"])
        self.assertEqual(status["enabled_providers"], ["yahoo"])
        self.assertEqual(status["store"]["schema_version"], 1)

        service.register_market_instrument(
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
        report = await service.ingest_market_data(instrument_ids=["XNSE:RELIANCE"])
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["quotes_inserted"], 1)

        quote = await service.market_quote("XNSE:RELIANCE")
        self.assertIsNotNone(quote["quote"]["selected"])
        self.assertEqual(quote["provenance"]["provider"], "yahoo")
        self.assertTrue(quote["quote"]["selected"]["live"])
        self.assertEqual(service.market_history("XNSE:RELIANCE")["total"], 1)

        # The retrieval proved the operation, so the capability is now advertised.
        record = next(
            item
            for item in service.status()["capabilities"]
            if item["capability_id"] == "market.read"
        )
        self.assertTrue(record["available"])
        self.assertIn("quote", service.market_data_status()["verified_operations"])

        await service.disable()
        with self.assertRaises(Exception):
            service.market_history("XNSE:RELIANCE")

    async def test_history_is_kept_across_a_disable_enable_cycle(self) -> None:
        service = self._service()
        await service.enable()
        service.register_market_instrument(
            {
                "symbol": "RELIANCE",
                "exchange": "NSE",
                "mic": "XNSE",
                "asset_class": "equity",
                "currency": "INR",
                "timezone": "Asia/Kolkata",
                "provider_symbols": {"yahoo": "RELIANCE.NS"},
            }
        )
        await service.ingest_market_data(instrument_ids=["XNSE:RELIANCE"])
        await service.disable()
        await service.enable()

        self.assertEqual(service.market_history("XNSE:RELIANCE")["total"], 1)
        self.assertEqual(
            service.market_instruments()["items"][0]["instrument_id"],
            "XNSE:RELIANCE",
        )

    async def test_background_ingestion_starts_stops_and_never_duplicates(self) -> None:
        service = self._service(
            ingestion={
                "enabled": True,
                "poll_seconds": 3600,
                "include_bars": False,
                "intervals": ["1d"],
            }
        )
        # The instrument must exist before the worker validates its universe,
        # and the engine exists only after initialize().
        await service.initialize()
        service.register_market_instrument(
            {
                "symbol": "RELIANCE",
                "exchange": "NSE",
                "mic": "XNSE",
                "asset_class": "equity",
                "currency": "INR",
                "timezone": "Asia/Kolkata",
                "provider_symbols": {"yahoo": "RELIANCE.NS"},
            }
        )
        await service.enable()

        self.assertEqual(service.market_data_status()["ingestion_error"], "")
        self.assertTrue(service.market_data_status()["ingestion"]["running"])
        worker = service.market_data._worker_task
        self.assertIsNotNone(worker)

        await service.market_data.start_ingestion()
        self.assertIs(service.market_data._worker_task, worker)

        await service.disable()
        self.assertFalse(service.market_data_status()["ingestion"]["running"])

    async def test_ingestion_that_cannot_start_degrades_health_with_the_reason(self) -> None:
        service = self._service(
            universe=["XNSE:UNREGISTERED"],
            ingestion={"enabled": True, "poll_seconds": 3600, "include_bars": False},
        )
        await service.enable()
        status = service.market_data_status()
        self.assertFalse(status["ingestion"]["running"])
        self.assertIn("XNSE:UNREGISTERED", status["ingestion_error"])
        self.assertEqual(service.health().state.value, "degraded")

    async def test_an_unknown_provider_is_reported_not_silently_enabled(self) -> None:
        service = self._service(data_providers=["nse-bhavcopy"])
        await service.enable()
        status = service.market_data_status()
        self.assertEqual(status["unknown_providers"], ["nse-bhavcopy"])
        self.assertEqual(status["enabled_providers"], [])


@unittest.skipIf(TestClient is None, "fastapi.testclient requires httpx")
class MarketDataApiTests(unittest.TestCase):
    """The HTTP surface drives the real service and repository."""

    def setUp(self) -> None:
        from novacontrol.api.app import create_app
        from novacontrol.application import NovaControlApplication

        self._tmp = TemporaryDirectory()
        self._nova: NovaControlApplication | None = None
        self._patchers = [
            mock.patch(
                "novacontrol.api.app.NovaControlApplication",
                side_effect=self._isolated_application,
            ),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch(
                "novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner
            ),
        ]
        for patcher in self._patchers:
            patcher.start()
        self._client = TestClient(create_app())
        self._client.__enter__()

    def tearDown(self) -> None:
        self._client.__exit__(None, None, None)
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmp.cleanup()

    def _isolated_application(self, **kwargs: Any) -> Any:
        from novacontrol.application import NovaControlApplication

        kwargs["data_dir"] = self._tmp.name
        application = NovaControlApplication(**kwargs)
        self._nova = application
        return application

    def _use_fake_provider(self, payload: dict[str, Any] | None = None) -> None:
        assert self._nova is not None
        self._client.post("/market/disable", json={})
        self._nova.novamarket._transport = FakeTransport(
            {"query1.finance.yahoo.com": _response(payload or yahoo_chart())}
        )
        self._client.post("/market/enable", json={})

    def _register_equity(self) -> Any:
        return self._client.post(
            "/market/instruments",
            json={
                "symbol": "RELIANCE",
                "name": "Reliance Industries",
                "exchange": "NSE",
                "mic": "XNSE",
                "asset_class": "equity",
                "currency": "INR",
                "timezone": "Asia/Kolkata",
                "provider_symbols": {"yahoo": "RELIANCE.NS"},
            },
        )

    def test_the_full_http_pipeline_registers_ingests_reads_and_reports(self) -> None:
        self._use_fake_provider()
        assert self._nova is not None
        # The API's app clock is real; point the fixed-clock test at a closed
        # market so the adapter's fixed observation is treated as a previous
        # close rather than inventing a live quote.
        self._nova.novamarket._market_engine._ingestion = IngestionSettings(
            include_quotes=True,
            include_bars=False,
            run_when_closed=True,
        )
        self._nova.novamarket._market_engine._clock = lambda: NOW

        registered = self._register_equity()
        self.assertEqual(registered.status_code, 200, registered.text)
        self.assertEqual(
            registered.json()["instrument"]["instrument_id"], "XNSE:RELIANCE"
        )

        report = self._client.post(
            "/market/data/ingest", json={"instrument_ids": ["XNSE:RELIANCE"]}
        )
        self.assertEqual(report.status_code, 200, report.text)
        self.assertTrue(report.json()["ok"], report.json()["errors"])

        quote = self._client.get("/market/data/quote/XNSE:RELIANCE")
        self.assertEqual(quote.status_code, 200, quote.text)
        body = quote.json()
        # The freshness verdict travels with every quote read, including the
        # case where there is no observation to price.
        self.assertIn("freshness", body)
        self.assertIn("market_state", body["freshness"])
        self.assertEqual(body["quote"]["selected"]["provider"], "yahoo")
        self.assertEqual(body["provenance"]["currency"], "INR")
        self.assertIn("provider_metadata", body["provenance"])

        history = self._client.get("/market/data/history/XNSE:RELIANCE")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["total"], 1)

        status = self._client.get("/market/data/status").json()
        self.assertTrue(status["initialised"])
        self.assertIn("quote", status["verified_operations"])

    def test_an_unavailable_quote_still_explains_why(self) -> None:
        self._use_fake_provider()
        self._register_equity()
        # Registered but never ingested: the read must not invent a price, and
        # it must carry a freshness verdict a consumer can act on.
        response = self._client.get("/market/data/quote/XNSE:RELIANCE")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIsNone(body["quote"]["selected"])
        self.assertEqual(body["freshness"]["freshness"], "missing")
        self.assertTrue(body["freshness"]["reasons"])
        self.assertFalse(body["freshness"]["live"])

    def test_invalid_inputs_have_distinct_statuses(self) -> None:
        self._use_fake_provider()
        self.assertEqual(
            self._client.get("/market/data/quote/XNSE:UNKNOWN").status_code, 404
        )
        self.assertEqual(
            self._client.post(
                "/market/instruments",
                json={"symbol": "X", "exchange": "NOPE", "asset_class": "equity"},
            ).status_code,
            422,
        )
        self.assertEqual(
            self._client.get(
                "/market/data/history/XNSE:RELIANCE", params={"start": "yesterday"}
            ).status_code,
            422,
        )

    def test_disabled_market_data_is_a_conflict_not_an_invented_quote(self) -> None:
        self._use_fake_provider()
        self._client.post("/market/disable", json={})
        response = self._client.get("/market/data/quote/XNSE:RELIANCE")
        self.assertEqual(response.status_code, 409)
        self.assertIn("not initialised", response.text)


class LiveProviderTests(unittest.IsolatedAsyncioTestCase):
    """Real provider calls, only when NOVAMARKET_LIVE_TESTS=1.

    These are separated from the fixture suite on purpose: a passing fixture test
    says the parser works, not that the provider is reachable or unchanged.
    """

    @unittest.skipUnless(
        os.environ.get("NOVAMARKET_LIVE_TESTS", "").strip() == "1",
        "live provider verification requires NOVAMARKET_LIVE_TESTS=1",
    )
    async def test_yahoo_reports_a_real_delayed_quote(self) -> None:
        from novacontrol.novamarket.marketdata.providers.base import UrllibTransport

        adapter = YahooAdapter(transport=UrllibTransport(timeout_seconds=20.0))
        instrument = Instrument.create(
            symbol="RELIANCE",
            exchange="NSE",
            mic="XNSE",
            currency="INR",
            timezone="Asia/Kolkata",
            provider_symbols={"yahoo": "RELIANCE.NS"},
        )
        quote = await adapter.fetch_quote(instrument)
        self.assertGreater(quote.price, 0)
        self.assertEqual(quote.currency, "INR")
        self.assertIsNotNone(quote.observed_at.tzinfo)

    @unittest.skipUnless(
        os.environ.get("NOVAMARKET_LIVE_TESTS", "").strip() == "1",
        "live provider verification requires NOVAMARKET_LIVE_TESTS=1",
    )
    async def test_coingecko_reports_a_real_crypto_quote(self) -> None:
        from novacontrol.novamarket.marketdata.providers.base import UrllibTransport

        adapter = CoinGeckoAdapter(transport=UrllibTransport(timeout_seconds=20.0))
        quote = await adapter.fetch_quote(crypto_instrument())
        self.assertGreater(quote.price, 0)
        self.assertEqual(quote.currency, "USD")


if __name__ == "__main__":
    unittest.main()
