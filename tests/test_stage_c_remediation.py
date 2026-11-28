"""Stage C remediation regressions: PI-12 context (R-01) and audit (R-02).

R-01: ``_forecast_bars`` must read one interval, newest-first, and never present
a stale or mixed series as the current technical context.
R-02: every control attempt is appended to an immutable, durable audit trail;
successful transitions commit their event with the mode row.
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from novacontrol.novamarket.autonomy import (
    AutonomyLeaseError,
    AutonomyRuntime,
    AutonomyStateError,
)
from novacontrol.novamarket.autonomy.store import AutonomyStore
from novacontrol.novamarket.contracts import MarketDataStoreError
from novacontrol.novamarket.decision.pipeline import AutonomyDecisionPipeline
from novacontrol.novamarket.marketdata.bse_bhavcopy import import_bse_bhavcopy
from novacontrol.novamarket.marketdata.dedupe import one_close_per_session
from novacontrol.novamarket.marketdata.models import (
    DataInterval,
    DataQuality,
    Freshness,
    Instrument,
    OhlcvBar,
)
from novacontrol.novamarket.marketdata.providers.base import ProviderBar
from novacontrol.novamarket.marketdata.providers.yahoo import _aggregate_20m
from novacontrol.novamarket.marketdata.store import MarketDataStore


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 27, 5, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def _series(
    count: int,
    *,
    start: datetime,
    step: timedelta,
    partial_newest: bool = False,
) -> list[dict[str, Any]]:
    """A newest-first page of complete bars, optionally partial at the head."""

    oldest_first = [
        {
            "observed_at": start + step * index,
            "close": 100.0 + index,
            "high": 101.0 + index,
            "low": 99.0 + index,
            "volume": 1_000 + index,
            "partial": False,
        }
        for index in range(count)
    ]
    newest_first = list(reversed(oldest_first))
    if partial_newest:
        newest_first.insert(
            0,
            {
                "observed_at": newest_first[0]["observed_at"] + step,
                "close": 9_999.0,
                "high": 9_999.0,
                "low": 9_999.0,
                "volume": 1,
                "partial": True,
            },
        )
    return newest_first


class _FakeMarketData:
    def __init__(self, pages: dict[DataInterval, list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def bars(
        self,
        instrument_id: str,
        *,
        interval: DataInterval | str | None = None,
        limit: int = 100,
        ascending: bool = False,
        **_: Any,
    ) -> SimpleNamespace:
        self.calls.append(
            {"interval": interval, "limit": limit, "ascending": ascending}
        )
        return SimpleNamespace(items=tuple(self.pages[DataInterval(interval)]))


class Pi12ForecastBarsTests(unittest.TestCase):
    def _pipeline(
        self, pages: dict[DataInterval, list[dict[str, Any]]]
    ) -> tuple[AutonomyDecisionPipeline, _FakeMarketData]:
        pipeline = AutonomyDecisionPipeline.__new__(AutonomyDecisionPipeline)
        fake = _FakeMarketData(pages)
        pipeline._market_data = fake  # type: ignore[attr-defined]
        return pipeline, fake

    def test_medium_term_uses_one_daily_series_newest_first_then_chronological(
        self,
    ) -> None:
        daily = _series(
            30,
            start=datetime(2026, 10, 1, 3, 45, tzinfo=UTC),
            step=timedelta(days=1),
        )
        pipeline, fake = self._pipeline({DataInterval.DAY_1: daily})
        rows = pipeline._forecast_bars("inst", portfolio_horizon="medium_term")
        self.assertEqual(fake.calls[0]["interval"], DataInterval.DAY_1)
        self.assertFalse(fake.calls[0]["ascending"])
        self.assertEqual(fake.calls[0]["limit"], 60)
        self.assertEqual(len(rows), 30)
        # The returned series is chronological and its last row is the newest
        # close, so `_technical` can never read a February price as "last".
        self.assertEqual(rows[-1]["close"], 129.0)
        self.assertEqual(
            [row["observed_at"] for row in rows],
            sorted(row["observed_at"] for row in rows),
        )

    def test_short_term_uses_the_five_minute_series(self) -> None:
        intraday = _series(
            40,
            start=datetime(2026, 11, 27, 4, 0, tzinfo=UTC),
            step=timedelta(minutes=5),
        )
        pipeline, fake = self._pipeline({DataInterval.MINUTE_5: intraday})
        rows = pipeline._forecast_bars("inst", portfolio_horizon="short_term")
        self.assertEqual(fake.calls[0]["interval"], DataInterval.MINUTE_5)
        self.assertEqual(len(rows), 40)
        self.assertEqual(rows[-1]["observed_at"], intraday[0]["observed_at"])

    def test_unknown_horizon_defaults_to_daily_and_never_mixes_intervals(
        self,
    ) -> None:
        daily = _series(
            25,
            start=datetime(2026, 10, 1, 3, 45, tzinfo=UTC),
            step=timedelta(days=1),
        )
        pipeline, fake = self._pipeline({DataInterval.DAY_1: daily})
        pipeline._forecast_bars("inst", portfolio_horizon="")
        self.assertEqual(fake.calls[0]["interval"], DataInterval.DAY_1)

    def test_duplicate_observation_instants_are_collapsed(self) -> None:
        daily = _series(
            24,
            start=datetime(2026, 10, 1, 3, 45, tzinfo=UTC),
            step=timedelta(days=1),
        )
        # A second provider stored the same session at a different timestamp
        # convention; the newest instant is the duplicate of an existing one.
        duplicate = dict(daily[3])
        duplicate["close"] = 555.0
        daily = daily[:3] + [duplicate] + daily[3:]
        pipeline, _ = self._pipeline({DataInterval.DAY_1: daily})
        rows = pipeline._forecast_bars("inst", portfolio_horizon="medium_term")
        self.assertEqual(len(rows), 24)
        self.assertEqual(rows[-1]["close"], 123.0)

    def test_partial_newest_bar_is_not_the_last_close(self) -> None:
        daily = _series(
            21,
            start=datetime(2026, 10, 1, 3, 45, tzinfo=UTC),
            step=timedelta(days=1),
            partial_newest=True,
        )
        pipeline, _ = self._pipeline({DataInterval.DAY_1: daily})
        rows = pipeline._forecast_bars("inst", portfolio_horizon="medium_term")
        self.assertEqual(len(rows), 21)
        self.assertEqual(rows[-1]["close"], 120.0)
        self.assertFalse(rows[-1]["partial"])

    def test_insufficient_history_fails_closed_with_no_context(self) -> None:
        daily = _series(
            20,
            start=datetime(2026, 10, 1, 3, 45, tzinfo=UTC),
            step=timedelta(days=1),
        )
        pipeline, _ = self._pipeline({DataInterval.DAY_1: daily})
        self.assertEqual(
            pipeline._forecast_bars("inst", portfolio_horizon="medium_term"), []
        )

    def test_a_reader_that_cannot_scope_the_interval_fails_closed(self) -> None:
        class _Unscoped:
            def bars(self, instrument_id: str, **kwargs: Any) -> Any:
                raise TypeError("interval is not supported")

        pipeline = AutonomyDecisionPipeline.__new__(AutonomyDecisionPipeline)
        pipeline._market_data = _Unscoped()  # type: ignore[attr-defined]
        self.assertEqual(
            pipeline._forecast_bars("inst", portfolio_horizon="medium_term"), []
        )


class ControlAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "novamarket.sqlite3"
        self.clock = _Clock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _runtime(self, owner: str = "owner-a") -> AutonomyRuntime:
        return AutonomyRuntime(
            self.path, clock=self.clock, owner_id=owner, lease_seconds=60
        )

    def _events(self, limit: int = 50) -> list[dict[str, Any]]:
        store = AutonomyStore(self.path)
        try:
            return store.control_events(limit=limit)
        finally:
            store.close()

    def test_applied_transitions_are_recorded_in_order(self) -> None:
        runtime = self._runtime()
        try:
            runtime.start("monitoring", actor="operator:alice")
            runtime.pause(reason="lunch", actor="operator:alice")
            runtime.resume(actor="operator:alice")
            runtime.stop(actor="operator:alice")
        finally:
            runtime.close()
        newest_first = self._events()
        operations = [row["operation"] for row in newest_first]
        self.assertEqual(operations, ["stop", "resume", "pause", "start"])
        for row in newest_first:
            self.assertEqual(row["actor"], "operator:alice")
            self.assertEqual(row["outcome"], "applied")
            self.assertEqual(row["lease_owner"], "owner-a")
            self.assertEqual(int(row["lease_valid"]), 1)
        start = newest_first[-1]
        self.assertEqual(start["from_mode"], "disabled")
        self.assertEqual(start["resulting_mode"], "monitoring")
        pause = newest_first[2]
        self.assertEqual(pause["from_mode"], "monitoring")
        self.assertEqual(pause["desired_mode"], "monitoring")
        self.assertEqual(pause["reason"], "lunch")

    def test_refused_pause_is_recorded_and_changes_nothing(self) -> None:
        runtime = self._runtime()
        try:
            with self.assertRaises(AutonomyStateError):
                runtime.pause(reason="too early", actor="operator:bob")
            self.assertEqual(runtime.status()["mode"], "disabled")
        finally:
            runtime.close()
        refused = self._events()
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["operation"], "pause")
        self.assertEqual(refused[0]["outcome"], "refused")
        self.assertEqual(refused[0]["actor"], "operator:bob")
        self.assertEqual(refused[0]["resulting_mode"], "disabled")
        self.assertIn("disabled runtime cannot be paused", refused[0]["error"])

    def test_refused_resume_and_noop_pause_are_recorded(self) -> None:
        runtime = self._runtime()
        try:
            with self.assertRaises(AutonomyStateError):
                runtime.resume(actor="operator:bob")
            runtime.start("monitoring")
            runtime.pause()
            runtime.pause(reason="already paused", actor="operator:bob")
        finally:
            runtime.close()
        rows = self._events()
        self.assertEqual(
            [row["outcome"] for row in rows],
            ["applied", "noop", "applied", "applied", "refused"],
        )
        self.assertEqual(rows[0]["operation"], "shutdown")
        self.assertEqual(rows[1]["operation"], "pause")
        self.assertEqual(rows[1]["reason"], "already paused")

    def test_restart_preserves_every_event_and_the_sequence(self) -> None:
        first = self._runtime()
        first.start("monitoring")
        first.pause()
        first.close()
        second = self._runtime("owner-b")
        try:
            second.stop(reason="close out")
        finally:
            second.close()
        rows = self._events()
        # The graceful close of the first runtime is its own audited transition.
        self.assertEqual(len(rows), 4)
        self.assertEqual(
            [row["operation"] for row in rows],
            ["stop", "shutdown", "pause", "start"],
        )
        sequences = [int(row["seq"]) for row in rows]
        self.assertEqual(sequences, sorted(sequences, reverse=True))
        event_ids = {row["event_id"] for row in rows}
        self.assertEqual(len(event_ids), 4)

    def test_a_rejected_state_change_writes_no_event(self) -> None:
        store = AutonomyStore(self.path)
        try:
            before = store.control_events()
            with self.assertRaises(AutonomyLeaseError):
                store.save_control(
                    "not-the-owner",
                    "not-the-token",
                    mode="monitoring",
                    state="running",
                    reason="stolen",
                    now=self.clock(),
                    control={
                        "operation": "start",
                        "actor": "operator:mallory",
                        "reason": "stolen",
                        "occurred_at": self.clock(),
                        "from_mode": "disabled",
                        "requested_mode": "monitoring",
                        "resulting_mode": "monitoring",
                        "desired_mode": "monitoring",
                        "lease_owner": "not-the-owner",
                        "lease_valid": False,
                        "outcome": "applied",
                    },
                )
            self.assertEqual(store.control_events(), before)
        finally:
            store.close()

    def test_v4_store_gains_the_audit_table_without_losing_state(self) -> None:
        store = AutonomyStore(self.path)
        store.save_auto_start(
            enabled=False, mode="monitoring", now=self.clock()
        )
        store.close()
        raw = sqlite3.connect(self.path)
        try:
            raw.execute("DROP TABLE autonomy_control_events")
            raw.execute(
                "UPDATE autonomy_meta SET value = '4' WHERE key = 'schema_version'"
            )
            raw.commit()
        finally:
            raw.close()
        migrated = AutonomyStore(self.path)
        try:
            self.assertEqual(migrated.control_events(), [])
            version = migrated.connection.execute(
                "SELECT value FROM autonomy_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            self.assertEqual(version, "5")
            self.assertFalse(migrated.config().auto_start)
            self.assertEqual(migrated.config().auto_start_mode, "monitoring")
        finally:
            migrated.close()


_HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,"
    "XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,"
    "LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
    "ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,"
    "Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n"
)


def _bhavcopy_csv(day: str, *, close: str = "1140.65") -> str:
    return _HEADER + (
        f"{day},{day},CM,BSE,STK,500003,INE208C01025,AEGISLOG,A,,,,,"
        f"AEGIS LOGISTICS LTD.,1163.00,1174.65,1127.00,{close},1140.00,"
        "1187.95,,1141.08,,,79921,91363294.00,3428,F1,1,,,,,\n"
    )


def _daily_bar(
    instrument_id: str,
    close: str,
    *,
    provider: str = "yahoo",
    day: int = 6,
) -> OhlcvBar:
    return OhlcvBar.create(
        instrument_id=instrument_id,
        provider=provider,
        interval=DataInterval.DAY_1,
        open=Decimal("1163.00"),
        high=Decimal("1174.65"),
        low=Decimal("1127.00"),
        close=Decimal(close),
        volume=Decimal("79921"),
        currency="INR",
        observed_at=datetime(2026, 11, day, 3, 45, tzinfo=UTC),
        retrieved_at=datetime(2026, 11, 27, 5, 0, tzinfo=UTC),
        source_timezone="Asia/Kolkata",
        quality=DataQuality.VALID,
        freshness=Freshness.CLOSED_MARKET,
        partial=False,
    )


class DuplicateSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = MarketDataStore(Path(self._tmp.name) / "market.sqlite3")
        self.instrument = Instrument.create(
            symbol="AEGISLOG",
            name="Aegis Logistics",
            exchange="BSE",
            mic="XBOM",
            currency="INR",
            timezone="Asia/Kolkata",
            provider_symbols={"yahoo": "AEGISLOG.BO"},
        )
        self.store.upsert_instrument(self.instrument)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _import(self, **kwargs: Any) -> dict[str, Any]:
        return import_bse_bhavcopy(
            self.store,
            start=date(2026, 11, 6),
            end=date(2026, 11, 6),
            fetch=lambda day: _bhavcopy_csv(day.isoformat(), **kwargs),
            clock=lambda: datetime(2026, 11, 27, 5, 0, tzinfo=UTC),
        )

    def _bars(self) -> list[OhlcvBar]:
        return list(
            self.store.bars(
                self.instrument.instrument_id,
                interval=DataInterval.DAY_1,
                limit=10,
                ascending=False,
            ).items
        )

    def test_matching_yahoo_session_is_not_imported_twice(self) -> None:
        self.store.append_bar(
            _daily_bar(self.instrument.instrument_id, "1140.65")
        )
        result = self._import()
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["duplicate"], 1)
        self.assertEqual(result["session_conflict_count"], 0)
        self.assertEqual(len(self._bars()), 1)
        self.assertEqual(self._bars()[0].provider, "yahoo")

    def test_conflicting_provider_values_are_reported_not_merged(self) -> None:
        self.store.append_bar(_daily_bar(self.instrument.instrument_id, "1100.00"))
        result = self._import()
        # The official row is appended beside the quote-provider row: the
        # original is never overwritten and the conflict is reported.
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["conflict"], 1)
        self.assertEqual(result["session_conflict_count"], 1)
        conflict = result["session_conflicts"][0]
        self.assertEqual(conflict["date"], "2026-11-06")
        self.assertEqual(conflict["existing_provider"], "yahoo")
        self.assertEqual(conflict["existing_close"], "1100.00")
        self.assertEqual(conflict["incoming_close"], "1140.65")
        self.assertEqual(conflict["action"], "official_session_appended")
        bars = self._bars()
        self.assertEqual(len(bars), 2)
        self.assertEqual(
            {bar.provider for bar in bars}, {"yahoo", "bse_bhavcopy"}
        )
        self.assertEqual(
            {bar.close for bar in bars}, {Decimal("1100.00"), Decimal("1140.65")}
        )
        # A consumer sees exactly one close for the session: the official one.
        chosen = one_close_per_session(bars)
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0].provider, "bse_bhavcopy")

    def test_new_session_is_imported_and_a_retry_does_not_duplicate(self) -> None:
        first = self._import()
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(len(self._bars()), 1)
        retry = self._import()
        self.assertEqual(retry["inserted"], 0)
        self.assertEqual(retry["duplicate"], 1)
        self.assertEqual(len(self._bars()), 1)

    def test_reader_policy_prefers_the_official_bhavcopy_row(self) -> None:
        yahoo = _daily_bar(self.instrument.instrument_id, "1140.65")
        official = _daily_bar(
            self.instrument.instrument_id, "1140.65", provider="bse_bhavcopy"
        )
        chosen = one_close_per_session([yahoo, official])
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0].provider, "bse_bhavcopy")


class BatchedBarPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = MarketDataStore(Path(self._tmp.name) / "market.sqlite3")
        self.instrument = Instrument.create(
            symbol="RELIANCE",
            name="Reliance Industries",
            exchange="NSE",
            mic="XNSE",
            currency="INR",
            timezone="Asia/Kolkata",
            provider_symbols={"yahoo": "RELIANCE.NS"},
        )
        self.store.upsert_instrument(self.instrument)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _bar(self, index: int, close: str = "100") -> OhlcvBar:
        return OhlcvBar.create(
            instrument_id=self.instrument.instrument_id,
            provider="yahoo",
            interval=DataInterval.DAY_1,
            open=Decimal(close),
            high=Decimal(close),
            low=Decimal(close),
            close=Decimal(close),
            volume=Decimal("1000"),
            currency="INR",
            observed_at=datetime(2026, 10, 1, 3, 45, tzinfo=UTC)
            + timedelta(days=index),
            retrieved_at=datetime(2026, 11, 27, 5, 0, tzinfo=UTC),
            source_timezone="Asia/Kolkata",
            quality=DataQuality.VALID,
            freshness=Freshness.CLOSED_MARKET,
            partial=False,
        )

    def test_a_page_inserts_once_and_repeats_as_duplicates(self) -> None:
        bars = [self._bar(index) for index in range(5)]
        first = self.store.append_bars(bars)
        self.assertEqual(len(first), 5)
        self.assertTrue(all(result.inserted for result in first))
        second = self.store.append_bars(bars)
        self.assertTrue(all(result.duplicate for result in second))
        page = self.store.bars(
            self.instrument.instrument_id, interval=DataInterval.DAY_1, limit=50
        )
        self.assertEqual(page.total, 5)

    def test_a_changed_value_in_a_batch_is_recorded_as_a_conflict(self) -> None:
        self.store.append_bars([self._bar(0, "100")])
        result = self.store.append_bars([self._bar(0, "101")])[0]
        self.assertTrue(result.conflict)
        stored = self.store.bars(
            self.instrument.instrument_id, interval=DataInterval.DAY_1, limit=10
        ).items[0]
        self.assertEqual(stored.close, Decimal("100"))

    def test_an_unknown_instrument_rolls_the_whole_page_back(self) -> None:
        good = self._bar(0)
        bad = OhlcvBar.create(
            instrument_id="XNSE:GHOST",
            provider="yahoo",
            interval=DataInterval.DAY_1,
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
            volume=Decimal("1"),
            currency="INR",
            observed_at=datetime(2026, 10, 1, 3, 45, tzinfo=UTC),
            retrieved_at=datetime(2026, 11, 27, 5, 0, tzinfo=UTC),
            source_timezone="Asia/Kolkata",
            quality=DataQuality.VALID,
            freshness=Freshness.CLOSED_MARKET,
            partial=False,
        )
        with self.assertRaises(MarketDataStoreError):
            self.store.append_bars([good, bad])
        self.assertEqual(
            self.store.bars(
                self.instrument.instrument_id,
                interval=DataInterval.DAY_1,
                limit=10,
            ).total,
            0,
        )

    def test_an_empty_page_is_a_no_op(self) -> None:
        self.assertEqual(self.store.append_bars([]), ())


class TwentyMinuteAnchorTests(unittest.TestCase):
    @staticmethod
    def _bar(minute: int, *, day: int = 27) -> ProviderBar:
        return ProviderBar(
            interval=DataInterval.MINUTE_5,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal(str(100 + minute % 100)),
            volume=Decimal("10"),
            currency="INR",
            observed_at=datetime(2026, 11, day, 3, 45, tzinfo=UTC)
            + timedelta(minutes=minute - 555),
            source_timezone="Asia/Kolkata",
            provider_metadata={},
            raw={},
        )

    def test_a_page_starting_at_the_session_open_keeps_the_grid(self) -> None:
        bars = [self._bar(minute) for minute in (555, 560, 565, 570)]
        out = _aggregate_20m(bars, "Asia/Kolkata", session_open_minute=555)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].observed_at, datetime(2026, 11, 27, 3, 45, tzinfo=UTC))
        self.assertEqual(out[0].volume, Decimal("40"))
        self.assertEqual(
            out[0].provider_metadata["session_anchor"], "session_open"
        )

    def test_a_page_starting_at_0920_is_not_shifted_five_minutes(self) -> None:
        bars = [self._bar(minute) for minute in (560, 565, 570, 575, 580, 585, 590)]
        shifted = _aggregate_20m(bars, "Asia/Kolkata")
        # Without a session anchor the page minimum shifts the whole grid: the
        # first emitted "20m" bar starts at 09:20 instead of the session grid.
        self.assertEqual(
            shifted[0].observed_at, datetime(2026, 11, 27, 3, 50, tzinfo=UTC)
        )
        aligned = _aggregate_20m(bars, "Asia/Kolkata", session_open_minute=555)
        # With the exchange anchor the 09:15 bucket is incomplete and dropped;
        # the first complete bucket is the real 09:35-09:55 one.
        self.assertEqual(len(aligned), 1)
        self.assertEqual(
            aligned[0].observed_at, datetime(2026, 11, 27, 4, 5, tzinfo=UTC)
        )
        self.assertEqual(
            aligned[0].provider_metadata["session_anchor"], "session_open"
        )

    def test_a_missing_middle_component_drops_its_group(self) -> None:
        bars = [self._bar(minute) for minute in (555, 560, 575, 580)]
        self.assertEqual(
            _aggregate_20m(bars, "Asia/Kolkata", session_open_minute=555), []
        )

    def test_an_incomplete_final_bucket_is_not_emitted(self) -> None:
        bars = [self._bar(minute) for minute in (555, 560, 565, 570, 585)]
        out = _aggregate_20m(bars, "Asia/Kolkata", session_open_minute=555)
        self.assertEqual(len(out), 1)

    def test_each_session_is_aggregated_on_its_own_grid(self) -> None:
        bars = [
            self._bar(555, day=26),
            self._bar(560, day=26),
            self._bar(565, day=26),
            self._bar(570, day=26),
            self._bar(555, day=27),
            self._bar(560, day=27),
            self._bar(565, day=27),
            self._bar(570, day=27),
        ]
        out = _aggregate_20m(bars, "Asia/Kolkata", session_open_minute=555)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].observed_at.date(), date(2026, 11, 26))
        self.assertEqual(out[1].observed_at.date(), date(2026, 11, 27))

    def test_a_holiday_page_produces_no_fabricated_bars(self) -> None:
        self.assertEqual(
            _aggregate_20m([], "Asia/Kolkata", session_open_minute=555), []
        )


class HorizonCompatibilityTests(unittest.TestCase):
    """SC-R08-01: NM-06 labels and PI-12 interval labels resolve canonically."""

    def test_canonical_horizon_translates_both_forms(self) -> None:
        from novacontrol.novamarket.decision.forecast import canonical_horizon

        self.assertEqual(canonical_horizon("5d"), "one_week")
        self.assertEqual(canonical_horizon("one_week"), "one_week")
        self.assertEqual(canonical_horizon("3d"), "few_sessions")
        self.assertEqual(canonical_horizon("30d"), "one_month")
        self.assertEqual(canonical_horizon("7d"), "7d")

    def test_risk_bridge_does_not_turn_five_days_into_few_sessions(self) -> None:
        from novacontrol.novamarket.decision.forecast import (
            risk_evidence_from_forecasts,
        )

        row = {
            "forecast_id": "f-1",
            "instrument_id": "XNSE:RELIANCE",
            "horizon": "5d",
            "lifecycle_stage": "t_minus_1",
            "validity": "valid",
            "data_quality": "valid",
            "uncertainty": "low",
            "generated_at": "2026-11-27T04:00:00+00:00",
            "model_id": "nm06-5d",
        }
        body = risk_evidence_from_forecasts("XNSE:RELIANCE", [row])
        self.assertEqual(body["forecasts"][0]["horizon"], "one_week")
        self.assertEqual(body["forecasts"][0]["source_horizon"], "5d")

    def test_model_lookup_finds_a_label_registration_from_an_interval_request(
        self,
    ) -> None:
        from novacontrol.novamarket.decision.forecast import ForecastRiskAdapter

        class _Primary:
            def models(self, instrument_id: str | None = None) -> dict[str, Any]:
                return {
                    "models": [
                        {
                            "model_id": "nm06-5d",
                            "instrument_id": "XNSE:RELIANCE",
                            "horizon": "one_week",
                        }
                    ]
                }

        adapter = ForecastRiskAdapter(_Primary(), lambda _instrument: {"models": []})
        found = adapter.models(instrument_id="XNSE:RELIANCE", horizon="5d")
        self.assertEqual([row["model_id"] for row in found["models"]], ["nm06-5d"])
        self.assertEqual(
            adapter.models(instrument_id="XNSE:RELIANCE", horizon="one_week")[
                "models"
            ][0]["model_id"],
            "nm06-5d",
        )
        self.assertEqual(
            adapter.models(instrument_id="XNSE:RELIANCE", horizon="7d")["models"], []
        )


class Independent20mVerificationTests(unittest.TestCase):
    """R-05: independent 5m rows are looked up by normalised timestamp."""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util

        path = Path(__file__).resolve().parents[1] / "scripts" / "stage_c_20m_verify.py"
        spec = importlib.util.spec_from_file_location("stage_c_20m_verify", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.verifier = module

    def _bars(self) -> list[dict[str, Any]]:
        return [
            {
                "observed_at": f"2026-11-05T05:{25 + 5 * index:02d}:00+00:00",
                "open": str(100 + index),
                "high": str(101 + index),
                "low": str(99 + index),
                "close": str(100.5 + index),
                "volume": "1000",
            }
            for index in range(4)
        ]

    def test_an_epoch_component_stamp_normalises_to_the_stored_iso_form(
        self,
    ) -> None:
        moment = self.verifier._stamp(1793856300)
        self.assertEqual(moment.isoformat(), "2026-11-05T05:25:00+00:00")

    def test_four_independent_bars_recompute_the_group_exactly(self) -> None:
        row = {
            "open": "100",
            "high": "104",
            "low": "99",
            "close": "103.5",
            "volume": "4000",
        }
        self.assertTrue(
            self.verifier._independent_recompute_matches(row, self._bars())
        )

    def test_a_missing_component_or_a_conflicting_close_is_rejected(self) -> None:
        row = {
            "open": "100",
            "high": "104",
            "low": "99",
            "close": "103.5",
            "volume": "4000",
        }
        self.assertFalse(
            self.verifier._independent_recompute_matches(row, self._bars()[:3])
        )
        bars = self._bars()
        bars[-1]["close"] = "999"
        self.assertFalse(self.verifier._independent_recompute_matches(row, bars))


class LabelFormForecastBridgeTests(unittest.TestCase):
    """SC-R08-01: an NM-06 ``one_week`` forecast reaches the risk bridge."""

    def test_a_label_form_forecast_is_not_dropped(self) -> None:
        from novacontrol.novamarket.decision.forecast import (
            risk_evidence_from_forecasts,
        )

        row = {
            "forecast_id": "f-2",
            "instrument_id": "XNSE:RELIANCE",
            "horizon": "one_week",
            "lifecycle_stage": "t_minus_1",
            "validity": "valid",
            "data_quality": "valid",
            "uncertainty": "low",
            "generated_at": "2026-11-27T04:00:00+00:00",
            "model_id": "bootstrap",
        }
        body = risk_evidence_from_forecasts("XNSE:RELIANCE", [row])
        self.assertEqual([item["horizon"] for item in body["forecasts"]], ["one_week"])

    def test_an_intraday_forecast_is_not_promoted_to_a_swing_label(self) -> None:
        from novacontrol.novamarket.decision.forecast import (
            risk_evidence_from_forecasts,
        )

        row = {
            "forecast_id": "f-3",
            "instrument_id": "XNSE:RELIANCE",
            "horizon": "1d",
            "lifecycle_stage": "t_minus_1",
            "validity": "valid",
            "data_quality": "valid",
            "uncertainty": "low",
            "generated_at": "2026-11-27T04:00:00+00:00",
        }
        self.assertEqual(
            risk_evidence_from_forecasts("XNSE:RELIANCE", [row])["forecasts"], []
        )
