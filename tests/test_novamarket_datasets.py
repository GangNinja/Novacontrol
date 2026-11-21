"""PI-02 tests: acquisition, validation, versioning, retrieval and leakage safety.

The provider is a deterministic fake adapter.  No live provider is contacted and
no rate limit is exercised; the real Yahoo/CoinGecko paths remain covered by the
existing NM-02 suite and its opt-in live tests.
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from novacontrol.novamarket.architecture import (
    PREDICTION_STORAGE_BOUNDARIES,
    validate_architecture,
)
from novacontrol.novamarket.datasets import (
    AdjustmentBasis,
    DatasetBuildRequest,
    DatasetNotFoundError,
    DatasetQuality,
    DatasetStateError,
    DatasetStore,
    DatasetValidationError,
    MarketAnnotation,
    HistoricalDatasetService,
    TrainingSample,
)
from novacontrol.novamarket.datasets.corporate import adjust_bars
from novacontrol.novamarket.datasets.models import CorporateAction
from novacontrol.novamarket.datasets.training import (
    _samples_for_instrument,
    build_training_dataset,
)
from novacontrol.novamarket.datasets.validation import assess_segment
from novacontrol.novamarket.events import IMPLEMENTED_EVENTS
from novacontrol.novamarket.marketdata.calendar import calendar_by_mic
from novacontrol.novamarket.marketdata.engine import MarketDataEngine
from novacontrol.novamarket.marketdata.models import (
    AssetClass,
    DataInterval,
    DataQuality,
    Instrument,
    OhlcvBar,
    ProviderCapabilities,
)
from novacontrol.novamarket.marketdata.providers.base import (
    HttpTransport,
    MarketDataAdapter,
    ProviderBar,
)
from novacontrol.novamarket.marketdata.providers.registry import ProviderRegistry
from novacontrol.novamarket.marketdata.store import MarketDataStore
from novacontrol.novamarket.prediction.models import ForecastHorizon
from novacontrol.novamarket.service import CAPABILITIES, NovaMarketService

_NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
_TZ = "Asia/Kolkata"


def _clock() -> datetime:
    return _NOW


class _NullTransport(HttpTransport):
    async def get(self, url, *, params=None, headers=None):  # noqa: ANN001
        raise AssertionError("the dataset tests must not perform network requests")


class FakeAdapter(MarketDataAdapter):
    name = "fake"

    def __init__(self, rows: tuple[ProviderBar, ...] = ()) -> None:
        super().__init__(
            transport=_NullTransport(),
            capabilities=ProviderCapabilities(
                provider=self.name,
                asset_classes=(AssetClass.EQUITY,),
                exchanges=("XNSE", "XBOM"),
                intervals=tuple(DataInterval),
                supports_quotes=False,
                supports_ohlcv=True,
                supports_lookup=False,
                data_kind="end_of_day",
                typical_delay_seconds=0,
                requires_credentials=False,
                historical_days=3_650,
            ),
        )
        self.rows = rows
        self.calls: list[tuple[datetime | None, datetime | None]] = []
        self.fail = False

    async def fetch_ohlcv(self, instrument, interval, *, start=None, end=None, limit=250):  # noqa: ANN001
        self.calls.append((start, end))
        if self.fail:
            from novacontrol.novamarket.contracts import ProviderUnavailableError

            raise ProviderUnavailableError("simulated provider outage", provider=self.name)
        return tuple(
            row
            for row in self.rows
            if row.interval is DataInterval(interval)
            and (start is None or row.observed_at >= start)
            and (end is None or row.observed_at <= end)
        )


def _instrument(symbol: str = "RELIANCE", mic: str = "XNSE") -> Instrument:
    return Instrument.create(
        symbol=symbol,
        name=symbol,
        exchange="NSE" if mic == "XNSE" else "BSE",
        mic=mic,
        currency="INR",
        timezone=_TZ,
        provider_symbols={"fake": f"{symbol}.NS"},
    )


def _day(index: int, *, start: date = date(2026, 1, 5)) -> date:
    candidate = start + timedelta(days=index)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _ist(day: date, hour: int = 9, minute: int = 15) -> datetime:
    zone = __import__("zoneinfo").ZoneInfo(_TZ)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone).astimezone(UTC)


def _provider_bar(
    day: date,
    *,
    close: str = "100",
    interval: DataInterval = DataInterval.DAY_1,
    partial: bool = False,
) -> ProviderBar:
    base = Decimal(close)
    return ProviderBar(
        interval=interval,
        open=base - Decimal(1),
        high=base + Decimal(2),
        low=base - Decimal(2),
        close=base,
        volume=Decimal(1000),
        currency="INR",
        observed_at=_ist(day),
        source_timezone=_TZ,
        partial=partial,
    )


def _bar(
    instrument: Instrument,
    day: date,
    *,
    close: str = "100",
    provider: str = "fake",
    interval: DataInterval = DataInterval.DAY_1,
    partial: bool = False,
    quality: DataQuality = DataQuality.VALID,
    hour: int = 9,
    minute: int = 15,
    retrieved_at: datetime | None = None,
    volume: Decimal | None = Decimal(1000),
) -> OhlcvBar:
    base = Decimal(close)
    return OhlcvBar.create(
        instrument_id=instrument.instrument_id,
        provider=provider,
        interval=interval,
        open=base - Decimal(1),
        high=base + Decimal(2),
        low=base - Decimal(2),
        close=base,
        volume=volume,
        currency="INR",
        observed_at=_ist(day, hour, minute),
        retrieved_at=retrieved_at or _NOW,
        source_timezone=_TZ,
        partial=partial,
        quality=quality,
    )


def _engine(
    *adapters: MarketDataAdapter,
) -> tuple[MarketDataEngine, MarketDataStore]:
    store = MarketDataStore(":memory:", clock=_clock)
    registry = ProviderRegistry(list(adapters) or [FakeAdapter()])
    engine = MarketDataEngine(
        store=store,
        registry=registry,
        enabled_providers=tuple(adapter.name for adapter in (adapters or (FakeAdapter(),))),
        clock=_clock,
    )
    engine.register_instrument(_instrument())
    return engine, store


def _request(
    *,
    dataset_id: str = "nse-daily-core",
    instruments: tuple[str, ...] = ("XNSE:RELIANCE",),
    resolution: DataInterval = DataInterval.DAY_1,
    start: datetime = datetime(2026, 1, 5, tzinfo=UTC),
    end: datetime = datetime(2026, 1, 9, 23, 59, tzinfo=UTC),
    providers: tuple[str, ...] = (),
    version: str = "",
    incremental: bool = False,
    allow_partial: bool = False,
) -> DatasetBuildRequest:
    return DatasetBuildRequest(
        dataset_id=dataset_id,
        instruments=instruments,
        resolution=resolution,
        start_at=start,
        end_at=end,
        providers=providers,
        version=version,
        incremental=incremental,
        allow_partial=allow_partial,
    )


class DatasetBuildTests(unittest.IsolatedAsyncioTestCase):
    def _service(
        self, rows: tuple[ProviderBar, ...]
    ) -> tuple[HistoricalDatasetService, MarketDataEngine, MarketDataStore, DatasetStore]:
        adapter = FakeAdapter(rows)
        engine, market_store = _engine(adapter)
        dataset_store = DatasetStore(":memory:", clock=_clock)
        service = HistoricalDatasetService(
            store=dataset_store, market_data=engine, clock=_clock
        )
        return service, engine, market_store, dataset_store

    async def test_daily_build_normalizes_identity_time_and_provenance(self) -> None:
        rows = tuple(_provider_bar(_day(index)) for index in range(5))
        service, engine, store, dataset_store = self._service(rows)
        try:
            built = await service.build(_request(), fetch=True)
            self.assertEqual(built["dataset_id"], "nse-daily-core")
            self.assertEqual(built["version"], "v1")
            self.assertEqual(built["resolution"], "1d")
            self.assertEqual(built["bar_count"], 5)
            self.assertEqual(built["providers"], ["fake"])
            segment = built["segments"][0]
            self.assertEqual(segment["instrument_id"], "XNSE:RELIANCE")
            self.assertEqual(segment["exchange"], "NSE")
            self.assertEqual(segment["mic"], "XNSE")
            self.assertEqual(segment["coverage"]["expected_bars"], 5)
            self.assertEqual(segment["coverage"]["coverage_ratio"], 1.0)
            self.assertTrue(segment["coverage"]["calendar_verified"])
            self.assertTrue(built["usable_for_training"])
            self.assertEqual(len(built["historical_datasets"]), 1)
            page = service.bars("nse-daily-core", "v1", ascending=True)
            self.assertEqual(page["total"], 5)
            first = page["items"][0]
            self.assertEqual(first["instrument_id"], "XNSE:RELIANCE")
            self.assertEqual(first["exchange"], "NSE")
            self.assertEqual(first["provider"], "fake")
            self.assertTrue(first["observed_at"].endswith("+00:00"))
            self.assertEqual(
                datetime.fromisoformat(first["observed_at"]).astimezone(UTC).hour, 3
            )
            self.assertEqual(engine.get_instrument("XNSE:RELIANCE").mic, "XNSE")
            self.assertEqual(dataset_store.stats()["counts"]["members"], 5)
            self.assertEqual(store.bars("XNSE:RELIANCE", interval="1d").total, 5)
        finally:
            service.close()
            store.close()

    async def test_repeated_build_is_idempotent_and_new_content_versions(self) -> None:
        rows = tuple(_provider_bar(_day(index)) for index in range(5))
        service, engine, store, dataset_store = self._service(rows)
        try:
            first = await service.build(_request(), fetch=True)
            second = await service.build(_request(incremental=True), fetch=True)
            self.assertTrue(second["duplicate"])
            self.assertEqual(first["version"], second["version"])
            self.assertEqual(first["content_digest"], second["content_digest"])
            self.assertEqual(dataset_store.versions("nse-daily-core")["total"], 1)
            adapter = engine.registry.get("fake")
            # The default refresh is incremental: only the latest stored candle
            # is re-read, and the store deduplicates it.
            self.assertEqual(second["acquisition"]["bars_received"], 1)
            self.assertEqual(second["acquisition"]["bars_duplicate"], 1)
            self.assertEqual(adapter.calls[-1][0], _provider_bar(_day(4)).observed_at)
            full = await service.build(_request(), fetch=True)
            self.assertTrue(full["duplicate"])
            self.assertEqual(full["acquisition"]["bars_received"], 5)
            self.assertEqual(full["acquisition"]["bars_inserted"], 0)
            adapter.rows = tuple(
                _provider_bar(_day(index)) for index in range(6)
            )
            third = await service.build(
                _request(
                    end=datetime(2026, 1, 12, 23, 59, tzinfo=UTC),
                    incremental=True,
                ),
                fetch=True,
            )
            self.assertFalse(third["duplicate"])
            self.assertEqual(third["version"], "v2")
            self.assertEqual(third["bar_count"], 6)
            original = service.metadata("nse-daily-core", "v1")
            self.assertEqual(original["bar_count"], 5)
            self.assertNotEqual(original["content_digest"], third["content_digest"])
            with self.assertRaises(DatasetStateError):
                await service.build(
                    _request(
                        end=datetime(2026, 1, 12, 23, 59, tzinfo=UTC), version="v1"
                    ),
                    fetch=True,
                )
        finally:
            service.close()
            store.close()

    async def test_provider_conflict_resolution_is_deterministic(self) -> None:
        rows = tuple(_provider_bar(_day(index)) for index in range(3))
        fake = FakeAdapter(rows)
        alt = FakeAdapter(rows)
        alt.name = "alt"
        alt.capabilities = ProviderCapabilities(
            provider="alt",
            asset_classes=(AssetClass.EQUITY,),
            exchanges=("XNSE",),
            intervals=(DataInterval.DAY_1,),
            supports_quotes=False,
            supports_ohlcv=True,
            supports_lookup=False,
            data_kind="end_of_day",
            typical_delay_seconds=0,
            requires_credentials=False,
        )
        engine, store = _engine(fake, alt)
        instrument = _instrument()
        for index in range(3):
            day = _day(index)
            fake_bar = _bar(instrument, day, close="100", provider="fake")
            alt_bar = _bar(instrument, day, close="101", provider="alt")
            store.append_bar(fake_bar)
            store.append_bar(alt_bar)
        dataset_store = DatasetStore(":memory:", clock=_clock)
        service = HistoricalDatasetService(
            store=dataset_store, market_data=engine, clock=_clock
        )
        try:
            conflict_request = dict(
                dataset_id="fake-first",
                providers=("fake",),
                end=datetime(2026, 1, 7, 23, 59, tzinfo=UTC),
            )
            fake_first = await service.build(
                _request(**conflict_request), fetch=False
            )
            self.assertEqual(fake_first["quality"], "provider_conflicted")
            segment = fake_first["segments"][0]
            self.assertEqual(segment["validation"]["provider_conflicts"], 3)
            self.assertEqual(segment["providers"], ["fake"])
            selections = service.selections("fake-first", "v1")
            self.assertEqual(selections["total"], 3)
            self.assertIn("alt:", selections["items"][0]["alternatives"][0])
            conflict_request["dataset_id"] = "alt-first"
            conflict_request["providers"] = ("alt",)
            alt_first = await service.build(
                _request(**conflict_request), fetch=False
            )
            self.assertEqual(alt_first["segments"][0]["providers"], ["alt"])
            fake_bar = service.bars("fake-first", "v1", limit=1)["items"][0]
            alt_bar = service.bars("alt-first", "v1", limit=1)["items"][0]
            self.assertEqual(fake_bar["provider"], "fake")
            self.assertEqual(alt_bar["provider"], "alt")
            self.assertEqual(Decimal(fake_bar["close"]), Decimal("100"))
            self.assertEqual(Decimal(alt_bar["close"]), Decimal("101"))
        finally:
            service.close()
            store.close()

    async def test_invalid_and_partial_observations_are_retained_but_excluded(self) -> None:
        rows = tuple(_provider_bar(_day(index)) for index in range(5))
        service, engine, store, dataset_store = self._service(rows)
        instrument = _instrument()
        try:
            building = _bar(instrument, _day(5), close="99", quality=DataQuality.INVALID)
            partial = _bar(
                instrument, _day(5), close="100", provider="alt", partial=True
            )
            await service.build(_request(), fetch=True)
            store.append_bar(building)
            store.append_bar(partial)
            built = await service.build(
                _request(end=datetime(2026, 1, 12, 23, 59, tzinfo=UTC)), fetch=False
            )
            self.assertEqual(built["bar_count"], 5)
            validation = built["validation"]
            self.assertEqual(validation["bars_excluded_invalid"], 1)
            self.assertEqual(validation["bars_excluded_partial"], 1)
            allowed = await service.build(
                _request(
                    dataset_id="partial-allowed",
                    end=datetime(2026, 1, 12, 23, 59, tzinfo=UTC),
                    allow_partial=True,
                ),
                fetch=False,
            )
            self.assertEqual(allowed["bar_count"], 6)
            self.assertEqual(allowed["validation"]["bars_selected_partial"], 1)
            self.assertFalse(allowed["complete"])
            # The rejected observations are still in the authoritative store.
            self.assertEqual(store.bars("XNSE:RELIANCE", interval="1d").total, 7)
            gap = service.missing("nse-daily-core", built["version"])["summary"]
            self.assertEqual(gap["by_kind"]["missing_provider_observation"], 1)
            page = service.bars("nse-daily-core", "v1", quality=DataQuality.INVALID.value)
            self.assertEqual(page["total"], 0)
        finally:
            service.close()
            store.close()

    async def test_gap_classification_distinguishes_holiday_weekend_and_absence(self) -> None:
        rows = tuple(_provider_bar(_day(index)) for index in range(5))
        service, engine, store, dataset_store = self._service(rows)
        try:
            missing_middle = await service.build(
                _request(
                    dataset_id="missing-middle",
                    instruments=("XNSE:RELIANCE",),
                ),
                fetch=False,
            )
            self.assertEqual(missing_middle["bar_count"], 0)
            for index in (0, 1, 3, 4):
                store.append_bar(_bar(_instrument(), _day(index), close="100"))
            # A holiday range: 25 Jan 2026 is Sunday, 26 Jan is Republic Day.
            holiday_service, holiday_engine, holiday_store, _ = self._service(())
            insert_bar = _bar(_instrument(), date(2026, 1, 27), close="120")
            holiday_store.append_bar(insert_bar)
            holiday = await holiday_service.build(
                DatasetBuildRequest(
                    dataset_id="holiday-window",
                    instruments=("XNSE:RELIANCE",),
                    resolution=DataInterval.DAY_1,
                    start_at=datetime(2026, 1, 25, tzinfo=UTC),
                    end_at=datetime(2026, 1, 27, 23, 59, tzinfo=UTC),
                ),
                fetch=False,
            )
            self.assertEqual(holiday["bar_count"], 1)
            self.assertEqual(holiday["missing"]["by_kind"], {})
            self.assertIn("1 exchange holiday", holiday["segments"][0]["coverage"]["detail"])
            holiday_service.close()
            holiday_store.close()

            day_missing = _day(2)
            skip = await service.build(
                _request(
                    dataset_id="with-hole",
                    instruments=("XNSE:RELIANCE",),
                ),
                fetch=False,
            )
            self.assertEqual(skip["bar_count"], 4)
            holes = service.missing("with-hole", "v1")["items"]
            self.assertEqual(len(holes), 1)
            self.assertEqual(holes[0]["kind"], "missing_provider_observation")
            self.assertEqual(holes[0]["count"], 1)
        finally:
            service.close()
            store.close()

    def test_validation_detects_out_of_order_and_clips_expected_slots(self) -> None:
        instrument = _instrument()
        bars = [_bar(instrument, _day(index)) for index in range(5)]
        assessment = assess_segment(
            instrument,
            DataInterval.DAY_1,
            datetime(2026, 1, 5, tzinfo=UTC),
            datetime(2026, 1, 9, 23, 59, tzinfo=UTC),
            list(reversed(bars)),
        )
        self.assertEqual(assessment.validation.out_of_order, 4)
        self.assertEqual(assessment.validation.bars_selected, 5)

        # An unfinished daily session is not an expected observation yet.
        partial_day = assess_segment(
            instrument,
            DataInterval.DAY_1,
            datetime(2026, 1, 5, tzinfo=UTC),
            datetime(2026, 1, 5, 4, 0, tzinfo=UTC),
            [],
        )
        self.assertEqual(partial_day.coverage.expected_bars, 0)
        self.assertEqual(partial_day.missing.total_missing, 0)

        # Five-minute slots outside the requested window are not expected.
        clipped = assess_segment(
            instrument,
            DataInterval.MINUTE_5,
            datetime(2026, 1, 5, 9, 0, tzinfo=UTC),
            datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
            [],
        )
        self.assertEqual(clipped.coverage.expected_bars, 12)
        self.assertEqual(clipped.missing.total_missing, 12)

        weekly_full = assess_segment(
            instrument,
            DataInterval.WEEK_1,
            datetime(2026, 1, 5, tzinfo=UTC),
            datetime(2026, 1, 9, 23, 59, tzinfo=UTC),
            [],
        )
        self.assertEqual(weekly_full.coverage.expected_bars, 1)
        weekly_partial = assess_segment(
            instrument,
            DataInterval.WEEK_1,
            datetime(2026, 1, 7, tzinfo=UTC),
            datetime(2026, 1, 9, 23, 59, tzinfo=UTC),
            [],
        )
        self.assertEqual(weekly_partial.coverage.expected_bars, 0)
        self.assertEqual(weekly_partial.missing.total_missing, 0)

        no_volume = assess_segment(
            instrument,
            DataInterval.DAY_1,
            datetime(2026, 1, 5, tzinfo=UTC),
            datetime(2026, 1, 9, 23, 59, tzinfo=UTC),
            [_bar(instrument, _day(index), volume=None) for index in range(5)],
        )
        self.assertEqual(no_volume.validation.bars_missing_volume, 5)
        self.assertEqual(no_volume.quality, DatasetQuality.VALID_WITH_WARNINGS)
        misaligned = assess_segment(
            instrument,
            DataInterval.DAY_1,
            datetime(2026, 1, 5, tzinfo=UTC),
            datetime(2026, 1, 9, 23, 59, tzinfo=UTC),
            [_bar(instrument, _day(index), hour=12) for index in range(5)],
        )
        self.assertEqual(misaligned.validation.boundary_misaligned, 5)

    async def test_one_expected_missing_observation_is_a_warning(self) -> None:
        service, engine, store, dataset_store = self._service(())
        instrument = _instrument()
        try:
            for index in range(5):
                if index == 2:
                    continue
                store.append_bar(_bar(instrument, _day(index)))
            built = await service.build(
                DatasetBuildRequest(
                    dataset_id="nse-daily-core",
                    instruments=("XNSE:RELIANCE",),
                    resolution=DataInterval.DAY_1,
                    start_at=datetime(2026, 1, 5, tzinfo=UTC),
                    end_at=datetime(2026, 1, 9, 23, 59, tzinfo=UTC),
                    # 4/5 coverage is below the default 0.9 threshold.
                    min_coverage_ratio=0.7,
                ),
                fetch=False,
            )
            self.assertEqual(built["bar_count"], 4)
            self.assertEqual(built["quality"], "valid_with_warnings")
            self.assertFalse(built["complete"])
            self.assertEqual(built["missing"]["total_missing"], 1)
        finally:
            service.close()
            store.close()

    async def test_absence_before_first_observation_is_incomplete_coverage(self) -> None:
        service, engine, store, dataset_store = self._service(())
        instrument = _instrument()
        try:
            for index in (2, 3, 4):
                store.append_bar(_bar(instrument, _day(index)))
            built = await service.build(_request(), fetch=False)
            gaps = service.missing("nse-daily-core", built["version"])["items"]
            self.assertEqual(built["bar_count"], 3)
            self.assertEqual(len(gaps), 1)
            self.assertEqual(gaps[0]["kind"], "incomplete_coverage")
            self.assertEqual(gaps[0]["count"], 2)
        finally:
            service.close()
            store.close()

    async def test_intraday_gaps_and_outside_calendar_are_explicit(self) -> None:
        adapter = FakeAdapter(())
        engine, store = _engine(adapter)
        instrument = _instrument()
        day = date(2026, 1, 5)
        for hour, minute in ((9, 15), (10, 15), (15, 15)):
            store.append_bar(
                _bar(
                    instrument,
                    day,
                    close="100",
                    interval=DataInterval.HOUR_1,
                    hour=hour,
                    minute=minute,
                )
            )
        dataset_store = DatasetStore(":memory:", clock=_clock)
        service = HistoricalDatasetService(
            store=dataset_store, market_data=engine, clock=_clock
        )
        try:
            built = await service.build(
                DatasetBuildRequest(
                    dataset_id="intraday-hourly",
                    instruments=("XNSE:RELIANCE",),
                    resolution=DataInterval.HOUR_1,
                    start_at=datetime(2026, 1, 5, tzinfo=UTC),
                    end_at=datetime(2026, 1, 5, 23, 59, tzinfo=UTC),
                ),
                fetch=False,
            )
            self.assertEqual(built["bar_count"], 3)
            gaps = service.missing("intraday-hourly", "v1")["items"]
            hourly_gap = [item for item in gaps if item["kind"] == "missing_provider_observation"]
            self.assertEqual(len(hourly_gap), 1)
            self.assertEqual(hourly_gap[0]["count"], 4)
            self.assertEqual(
                datetime.fromisoformat(hourly_gap[0]["end_at"]),
                datetime(2026, 1, 5, 15, 15, tzinfo=ZoneInfo(_TZ)),
            )
            self.assertEqual(built["quality"], "incomplete")
            unverified = await service.build(
                DatasetBuildRequest(
                    dataset_id="outside-coverage",
                    instruments=("XNSE:RELIANCE",),
                    resolution=DataInterval.DAY_1,
                    start_at=datetime(2025, 1, 6, tzinfo=UTC),
                    end_at=datetime(2025, 1, 10, 23, 59, tzinfo=UTC),
                ),
                fetch=False,
            )
            self.assertEqual(unverified["bar_count"], 0)
            outside = service.missing("outside-coverage", "v1")["items"]
            self.assertTrue(outside)
            self.assertEqual(outside[0]["kind"], "outside_calendar_coverage")
            self.assertFalse(outside[0]["verified"])
            self.assertFalse(unverified["usable_for_training"])
        finally:
            service.close()
            store.close()

    async def test_provider_failure_preserves_previous_data_and_stays_incomplete(self) -> None:
        rows = tuple(_provider_bar(_day(index)) for index in range(5))
        service, engine, store, dataset_store = self._service(rows)
        try:
            await service.build(_request(), fetch=True)
            adapter = engine.registry.get("fake")
            adapter.fail = True
            failed = await service.build(
                _request(end=datetime(2026, 1, 12, 23, 59, tzinfo=UTC)), fetch=True
            )
            self.assertEqual(failed["bar_count"], 5)
            self.assertFalse(failed["complete"])
            self.assertIn(failed["quality"], ("incomplete", "unverified"))
            self.assertEqual(failed["acquisition"]["failed"], 1)
            self.assertTrue(
                any("provider failure" in item for item in failed["limitations"])
            )
            self.assertEqual(store.bars("XNSE:RELIANCE", interval="1d").total, 5)
            self.assertEqual(dataset_store.versions("nse-daily-core")["total"], 2)
        finally:
            service.close()
            store.close()

    async def test_build_request_rejects_unknown_instrument_and_invalid_contract(self) -> None:
        service, engine, store, dataset_store = self._service(())
        try:
            with self.assertRaises(DatasetValidationError):
                await service.build(
                    _request(instruments=("XNSE:UNKNOWN",)), fetch=False
                )
            with self.assertRaises(DatasetValidationError):
                DatasetBuildRequest(
                    dataset_id="x",
                    instruments=(),
                    resolution="bad",
                    start_at=_NOW,
                    end_at=_NOW,
                )
            with self.assertRaises(DatasetNotFoundError):
                service.metadata("does-not-exist")
        finally:
            service.close()
            store.close()

    async def test_multi_instrument_batch_and_pagination(self) -> None:
        fake = FakeAdapter(())
        engine, store = _engine(fake)
        second = Instrument.create(
            symbol="TCS",
            exchange="NSE",
            currency="INR",
            timezone=_TZ,
            provider_symbols={"fake": "TCS.NS"},
        )
        engine.register_instrument(second)
        for index in range(3):
            day = _day(index)
            store.append_bar(_bar(_instrument(), day, close="100"))
            store.append_bar(_bar(second, day, close="200"))
        dataset_store = DatasetStore(":memory:", clock=_clock)
        service = HistoricalDatasetService(
            store=dataset_store, market_data=engine, clock=_clock
        )
        try:
            built = await service.build(
                _request(instruments=("XNSE:RELIANCE", "XNSE:TCS")), fetch=False
            )
            self.assertEqual(built["bar_count"], 6)
            batch = service.batch(
                "nse-daily-core", "v1", limit_per_instrument=10
            )
            self.assertEqual(sorted(batch["instruments"]), ["XNSE:RELIANCE", "XNSE:TCS"])
            page = service.bars("nse-daily-core", "v1", limit=2, offset=1, ascending=True)
            self.assertEqual(len(page["items"]), 2)
            self.assertTrue(page["has_more"])
            self.assertEqual(page["total"], 6)
            with self.assertRaises(Exception):
                service.bars("nse-daily-core", "v1", limit=0)
        finally:
            service.close()
            store.close()


class TrainingDatasetTests(unittest.IsolatedAsyncioTestCase):
    _NSE_HOLIDAYS = {
        date(2026, 1, 15),
        date(2026, 1, 26),
        date(2026, 3, 3),
        date(2026, 3, 26),
        date(2026, 3, 31),
        date(2026, 4, 3),
        date(2026, 4, 14),
        date(2026, 5, 1),
        date(2026, 5, 28),
        date(2026, 6, 26),
        date(2026, 9, 14),
        date(2026, 10, 2),
        date(2026, 10, 20),
        date(2026, 11, 10),
        date(2026, 11, 24),
        date(2026, 12, 25),
    }

    def _trading_days(self) -> list[date]:
        result: list[date] = []
        current = date(2026, 1, 1)
        while current <= date(2026, 12, 31):
            if current.weekday() < 5 and current not in self._NSE_HOLIDAYS:
                result.append(current)
            current += timedelta(days=1)
        return result

    async def _service(self) -> tuple[HistoricalDatasetService, MarketDataStore, MarketDataEngine]:
        adapter = FakeAdapter(())
        engine, store = _engine(adapter)
        for index, day in enumerate(self._trading_days()):
            store.append_bar(
                _bar(
                    _instrument(),
                    day,
                    close=str(100 + index),
                    retrieved_at=datetime(
                        day.year, day.month, day.day, 12, tzinfo=UTC
                    )
                    + timedelta(days=1),
                )
            )
        dataset_store = DatasetStore(":memory:", clock=_clock)
        service = HistoricalDatasetService(
            store=dataset_store, market_data=engine, clock=_clock
        )
        await service.build(
            DatasetBuildRequest(
                dataset_id="nse-year",
                instruments=("XNSE:RELIANCE",),
                resolution=DataInterval.DAY_1,
                start_at=datetime(2026, 1, 1, tzinfo=UTC),
                end_at=datetime(2026, 12, 31, 23, 59, tzinfo=UTC),
            ),
            fetch=False,
        )
        return service, store, engine

    async def test_chronological_splits_targets_and_leakage_guards(self) -> None:
        service, store, engine = await self._service()
        try:
            training = service.build_training(
                "nse-year", "v1", horizon="1d", window_bars=20
            )
            self.assertTrue(training["chronological"])
            self.assertFalse(training["test_used_for_training"])
            self.assertGreater(training["sample_count"], 100)
            self.assertGreater(training["splits"]["train"]["sample_count"], 0)
            self.assertGreater(training["splits"]["validation"]["sample_count"], 0)
            self.assertGreater(training["splits"]["test"]["sample_count"], 0)
            self.assertIn("chronological train < validation < test", training["leakage_checks"])
            page = service.training_samples(
                training["training_id"], split="train", limit=10
            )
            self.assertEqual(len(page["items"]), 10)
            for sample in page["items"]:
                self.assertLess(
                    datetime.fromisoformat(sample["decision_at"]),
                    datetime.fromisoformat(sample["target_at"]),
                )
                self.assertLessEqual(
                    datetime.fromisoformat(sample["window_end_at"]),
                    datetime.fromisoformat(sample["decision_at"]),
                )
                self.assertTrue(sample["features_at_or_before_decision"])
                self.assertTrue(sample["target_after_decision"])
            # Purged targets are visible, not silently counted as samples.
            self.assertGreaterEqual(
                training["splits"]["train"]["purged_target_overlaps"], 0
            )
            verification = service.verify_training(training["training_id"])
            self.assertTrue(verification["ok"], verification["problems"])
            self.assertTrue(verification["membership_verified"])
            again = service.build_training(
                "nse-year", "v1", horizon="1d", window_bars=20
            )
            self.assertTrue(again["duplicate"])
            self.assertEqual(again["training_id"], training["training_id"])
        finally:
            service.close()
            store.close()

    async def test_intraday_horizon_on_daily_data_is_refused(self) -> None:
        service, store, engine = await self._service()
        try:
            with self.assertRaises(DatasetValidationError):
                service.build_training("nse-year", "v1", horizon="5m", window_bars=20)
        finally:
            service.close()
            store.close()

    async def test_training_builder_refuses_fabricated_targets(self) -> None:
        instrument = _instrument()
        bars = tuple(
            _bar(instrument, date(2026, 1, 5) + timedelta(days=index), close="100")
            for index in range(5)
        )
        with self.assertRaises(DatasetValidationError):
            build_training_dataset(
                dataset_id="short",
                dataset_version="v1",
                resolution=DataInterval.DAY_1,
                horizon="5d",
                created_at=_NOW,
                bars_by_instrument={"XNSE:RELIANCE": bars},
                window_bars=3,
            )


class CorporateActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_and_adjusted_series_are_distinct_and_sourced(self) -> None:
        adapter = FakeAdapter(())
        engine, store = _engine(adapter)
        instrument = _instrument()
        for index in range(5):
            store.append_bar(_bar(instrument, _day(index), close="100"))
        dataset_store = DatasetStore(":memory:", clock=_clock)
        service = HistoricalDatasetService(
            store=dataset_store, market_data=engine, clock=_clock
        )
        try:
            built = await service.build(_request(), fetch=False)
            self.assertEqual(built["price_basis"], "raw")
            action = CorporateAction(
                instrument_id=instrument.instrument_id,
                kind="split",
                effective_at=datetime(2026, 1, 8, tzinfo=UTC),
                announced_at=datetime(2025, 12, 20, tzinfo=UTC),
                source="exchange-filing",
                verified=True,
                price_factor=Decimal("0.5"),
            )
            first = service.register_corporate_action(action)
            self.assertEqual(first["status"], "inserted")
            second = service.register_corporate_action(action)
            self.assertEqual(second["status"], "duplicate")
            adjusted_page = service.bars(
                "nse-daily-core", "v1", limit=10, ascending=True, adjusted=True
            )
            adjusted = adjusted_page["items"]
            self.assertEqual(Decimal(adjusted[0]["close"]), Decimal("50"))
            self.assertEqual(Decimal(adjusted[0]["raw"]["close"]), Decimal("100"))
            self.assertTrue(adjusted[0]["adjusted"])
            self.assertEqual(Decimal(adjusted[4]["close"]), Decimal("100"))
            raw_page = service.bars("nse-daily-core", "v1", limit=10, ascending=True)
            self.assertEqual(Decimal(raw_page["items"][0]["close"]), Decimal("100"))
            self.assertNotIn("raw", raw_page["items"][0])
            # The stored observation was never rewritten.
            self.assertEqual(
                Decimal(store.bars("XNSE:RELIANCE", interval="1d", ascending=True).items[0].close),
                Decimal("100"),
            )
            unverified = CorporateAction(
                instrument_id=instrument.instrument_id,
                kind="dividend",
                effective_at=datetime(2026, 1, 9, tzinfo=UTC),
                announced_at=None,
                source="unverified-note",
                verified=False,
                amount=Decimal("5"),
            )
            service.register_corporate_action(unverified)
            total = service.adjusted_bars(
                "XNSE:RELIANCE",
                start=datetime(2026, 1, 5, tzinfo=UTC),
                end=datetime(2026, 1, 9, 23, 59, tzinfo=UTC),
                basis="total_return_adjusted",
            )
            self.assertEqual(total["basis"], "total_return_adjusted")
            self.assertTrue(total["unapplied_action_ids"])
            self.assertTrue(
                any("unverified" in item for item in total["limitations"])
            )
        finally:
            service.close()
            store.close()

    async def test_annotation_classifies_a_halt_instead_of_a_data_defect(self) -> None:
        adapter = FakeAdapter(())
        engine, store = _engine(adapter)
        instrument = _instrument()
        for index in (0, 1, 3, 4):
            store.append_bar(_bar(instrument, _day(index), close="100"))
        dataset_store = DatasetStore(":memory:", clock=_clock)
        service = HistoricalDatasetService(
            store=dataset_store, market_data=engine, clock=_clock
        )
        try:
            service.register_annotation(
                MarketAnnotation(
                    instrument_id=instrument.instrument_id,
                    kind="trading_halt",
                    start_at=_ist(_day(2)),
                    end_at=_ist(_day(2), 15, 30),
                    source="exchange-notice",
                    verified=True,
                    detail="simulated halt",
                )
            )
            built = await service.build(_request(), fetch=False)
            gaps = service.missing("nse-daily-core", "v1")["items"]
            halt = [item for item in gaps if item["kind"] == "trading_halt"]
            self.assertEqual(len(halt), 1)
            self.assertEqual(halt[0]["count"], 1)
            self.assertTrue(halt[0]["verified"])
            self.assertEqual(built["bar_count"], 4)
        finally:
            service.close()
            store.close()


class StoreAndContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_versions_and_samples_are_immutable(self) -> None:
        adapter = FakeAdapter(tuple(_provider_bar(_day(index)) for index in range(3)))
        engine, market_store = _engine(adapter)
        dataset_store = DatasetStore(":memory:", clock=_clock)
        service = HistoricalDatasetService(
            store=dataset_store, market_data=engine, clock=_clock
        )
        try:
            built = await service.build(
                DatasetBuildRequest(
                    dataset_id="immutable",
                    instruments=("XNSE:RELIANCE",),
                    resolution=DataInterval.DAY_1,
                    start_at=datetime(2026, 1, 5, tzinfo=UTC),
                    end_at=datetime(2026, 1, 20, 23, 59, tzinfo=UTC),
                ),
                fetch=True,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                dataset_store._connection.execute(  # noqa: SLF001 - invariant test
                    "UPDATE dataset_versions SET quality='valid' WHERE dataset_id='immutable'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                dataset_store._connection.execute(  # noqa: SLF001
                    "DELETE FROM dataset_members WHERE dataset_id='immutable'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                dataset_store._connection.execute(  # noqa: SLF001
                    "DELETE FROM dataset_versions WHERE dataset_id='immutable'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                dataset_store._connection.execute(  # noqa: SLF001
                    "DELETE FROM dataset_gaps WHERE dataset_id='immutable'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                dataset_store._connection.execute(  # noqa: SLF001
                    "DELETE FROM dataset_segments WHERE dataset_id='immutable'"
                )
            self.assertEqual(built["version"], "v1")
        finally:
            service.close()
            market_store.close()

    def test_contracts_and_storage_boundaries_cover_pi02(self) -> None:
        self.assertEqual(validate_architecture(), ())
        declared = {item.data for item in PREDICTION_STORAGE_BOUNDARIES}
        for table in (
            "historical_dataset_versions",
            "historical_dataset_members",
            "dataset_gap_records",
            "corporate_action_records",
            "dataset_annotations",
            "training_dataset_versions",
            "training_dataset_samples",
        ):
            self.assertIn(table, declared)
        persisted = {item.data for item in PREDICTION_STORAGE_BOUNDARIES if item.phase == "PI-02"}
        self.assertGreaterEqual(len(persisted), 7)

    async def test_existing_service_initializes_and_consumes_datasets(self) -> None:
        with TemporaryDirectory() as directory:
            service = NovaMarketService(data_dir=directory)
            try:
                await service.initialize()
                status = service.status()
                contracts = service.contracts()
                instrument = _instrument()
                assert service._market_store is not None  # noqa: SLF001
                service._market_store.upsert_instrument(instrument)  # noqa: SLF001
                for index in range(5):
                    service._market_store.append_bar(  # noqa: SLF001
                        _bar(instrument, _day(index), close="100")
                    )
                built = await service.build_historical_dataset(
                    _request(dataset_id="service-dataset"), fetch=False
                )
                retrieved = service.dataset_bars(
                    "service-dataset", built["version"], instrument_id="XNSE:RELIANCE"
                )
                quality = service.dataset_quality("service-dataset", built["version"])
                training = service.build_training_dataset(
                    "service-dataset", built["version"], horizon="1d", window_bars=3
                )
            finally:
                await service.shutdown()
        self.assertEqual(status["datasets"]["phase"], "PI-02")
        self.assertTrue(status["datasets"]["initialised"])
        self.assertEqual(contracts["datasets"]["phase"], "PI-02")
        self.assertIn(
            "market.dataset.build",
            {item["capability_id"] for item in service.capability_records()},
        )
        self.assertGreaterEqual(len(CAPABILITIES), 54)
        self.assertGreaterEqual(len(IMPLEMENTED_EVENTS), 181)
        self.assertEqual(built["bar_count"], 5)
        self.assertEqual(retrieved["total"], 5)
        self.assertIn(quality["quality"], ("valid", "valid_with_warnings"))
        self.assertGreater(training["sample_count"], 0)


class SmallUnitTests(unittest.TestCase):
    def test_swing_training_labels_use_verified_sessions_across_data_gaps(self) -> None:
        instrument = _instrument()
        calendar = calendar_by_mic("XNSE")
        assert calendar is not None
        days = (
            date(2026, 1, 5),
            date(2026, 1, 6),
            date(2026, 1, 7),
            # January 8 is a verified session with no row in this dataset.
            date(2026, 1, 9),
            date(2026, 1, 12),
        )
        bars = tuple(_bar(instrument, day, close=str(100 + index)) for index, day in enumerate(days))
        samples, missing = _samples_for_instrument(
            instrument.instrument_id,
            DataInterval.DAY_1,
            ForecastHorizon.DAYS_3,
            bars,
            window_bars=2,
            calendar=calendar,
        )
        by_decision = {sample.decision_at: sample for _, _, sample in samples}
        decision = next(
            sample
            for day, sample in by_decision.items()
            if calendar.local(day).date() == date(2026, 1, 6)
        )
        # Three verified sessions after Jan 6 are Jan 7, Jan 8 and Jan 9.
        # The missing Jan 8 row must not silently turn the label into Jan 12.
        assert calendar.local(decision.target_at).date() == date(2026, 1, 9)
        assert missing >= 1

    def test_long_training_labels_require_the_first_verified_target_session(self) -> None:
        instrument = _instrument()
        calendar = calendar_by_mic("XNSE")
        assert calendar is not None
        bars = (
            _bar(instrument, date(2026, 1, 5), close="100"),
            # The first verified session 30 calendar days later is February 4;
            # February 5 is the next observed row and must not become the target.
            _bar(instrument, date(2026, 2, 5), close="101"),
        )
        samples, missing = _samples_for_instrument(
            instrument.instrument_id,
            DataInterval.DAY_1,
            ForecastHorizon.DAYS_30,
            bars,
            window_bars=1,
            calendar=calendar,
        )
        assert samples == []
        assert missing >= 1

    def test_all_nine_horizons_construct_with_compatible_resolutions(self) -> None:
        instrument = _instrument()
        now = datetime(2026, 11, 13, 12, tzinfo=UTC)
        daily = []
        current = date(2026, 1, 1)
        close = Decimal(100)
        while len(daily) < 200:
            if current.weekday() < 5:
                daily.append(
                    _bar(instrument, current, close=str(close), interval=DataInterval.DAY_1)
                )
                close += 1
            current += timedelta(days=1)
        for horizon in ("1d", "3d", "5d", "30d", "90d"):
            training, samples = build_training_dataset(
                dataset_id="all-horizons",
                dataset_version="v1",
                resolution=DataInterval.DAY_1,
                horizon=horizon,
                created_at=now,
                bars_by_instrument={instrument.instrument_id: daily},
                window_bars=10,
            )
            self.assertGreater(len(samples), 0, horizon)
            self.assertEqual(len(training.leakage_checks), 11)
        base = datetime(2026, 1, 5, 3, 45, tzinfo=UTC)
        cases = (
            ("5m", DataInterval.MINUTE_1, timedelta(minutes=1), 375),
            ("15m", DataInterval.MINUTE_5, timedelta(minutes=5), 75),
            ("20m", DataInterval.MINUTE_5, timedelta(minutes=5), 75),
            ("1h", DataInterval.MINUTE_15, timedelta(minutes=15), 25),
        )
        for horizon, interval, step, count in cases:
            bars = []
            for index in range(count):
                local = (base + step * index).astimezone(ZoneInfo(_TZ))
                bars.append(
                    _bar(
                        instrument,
                        local.date(),
                        close=str(Decimal(100) + index),
                        interval=interval,
                        hour=local.hour,
                        minute=local.minute,
                    )
                )
            training, samples = build_training_dataset(
                dataset_id="all-horizons",
                dataset_version="v1",
                resolution=interval,
                horizon=horizon,
                created_at=now,
                bars_by_instrument={instrument.instrument_id: bars},
                window_bars=10,
            )
            self.assertGreater(len(samples), 0, horizon)
            self.assertEqual(len(training.leakage_checks), 11)

    def test_adjustment_requires_verified_explicit_factors(self) -> None:
        with self.assertRaises(DatasetValidationError):
            adjust_bars(
                _instrument().instrument_id,
                [],
                [],
                basis=AdjustmentBasis.PROVIDER_ADJUSTED,
            )
        instrument = _instrument()
        bars = tuple(_bar(instrument, _day(index), close="100") for index in range(2))
        action = CorporateAction(
            instrument_id=instrument.instrument_id,
            kind="split",
            effective_at=datetime(2026, 1, 9, tzinfo=UTC),
            announced_at=None,
            source="filing",
            verified=False,
            price_factor=Decimal("0.5"),
        )
        series = adjust_bars(instrument.instrument_id, bars, (action,), basis="split_adjusted")
        self.assertEqual(Decimal(series.bars[0].close), Decimal("100"))
        self.assertEqual(series.unapplied_action_ids, (action.action_id,))

    def test_training_sample_refuses_a_future_feature(self) -> None:
        with self.assertRaises(DatasetValidationError):
            TrainingSample(
                instrument_id="XNSE:RELIANCE",
                split="train",
                horizon="1d",
                decision_at=datetime(2026, 1, 5, tzinfo=UTC),
                target_at=datetime(2026, 1, 6, tzinfo=UTC),
                decision_bar_id="a",
                target_bar_id="b",
                window_start_at=datetime(2026, 1, 1, tzinfo=UTC),
                window_end_at=datetime(2026, 1, 7, tzinfo=UTC),
                decision_close=Decimal("100"),
                target_close=Decimal("101"),
                window_bar_ids=("a",),
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
