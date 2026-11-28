"""AT-02 focused tests: verified sessions, monitoring, rotation and safety.

The tests compose the AT-01 runtime and a fake discovery engine.  No test
constructs an execution, portfolio, wallet, risk or strategy engine, and the
suite asserts that AT-02 contains no path to one.
"""

from __future__ import annotations

import ast
import asyncio
import os
import sqlite3
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import mock

try:
    from fastapi.testclient import TestClient  # requires httpx
except Exception:  # pragma: no cover - optional dev dependency
    TestClient = None  # type: ignore[assignment]

from novacontrol.api.models import ApiSurface
from novacontrol.api.route_consumers import ROUTE_CONSUMERS
from novacontrol.novamarket.autonomy import AutonomyRuntime
from novacontrol.novamarket.autonomy.models import (
    AutonomyValidationError,
    MonitoringConfig,
)
from novacontrol.novamarket.autonomy.monitoring import (
    AutonomyMonitoring,
    MonitoringRefused,
    closed_monitoring_status,
)
from novacontrol.novamarket.autonomy.session import evaluate_session
from novacontrol.novamarket.autonomy.store import AutonomyStore
from novacontrol.novamarket.discovery import (
    AssetDiscoveryEngine,
    DiscoveryPolicy,
    DiscoveryStore,
    UniverseFilter,
)
from novacontrol.novamarket.marketdata.models import (
    AssetClass,
    DataInterval,
    Instrument,
    OhlcvBar,
    PriceObservation,
    TradingStatus,
)
from novacontrol.novamarket.marketdata.store import MarketDataStore

_PACKAGE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "novacontrol"
    / "novamarket"
    / "autonomy"
)
_OPEN = datetime(2026, 11, 11, 4, 0, tzinfo=UTC)  # 09:30 IST, verified session


class _Clock:
    def __init__(self, now: datetime = _OPEN) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class _Discovery:
    """A deterministic stand-in for NM-05; it never touches the network."""

    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        *,
        report: dict[str, Any] | None = None,
        fail_times: int = 0,
        raises: BaseException | None = None,
    ) -> None:
        self.rows = list(rows if rows is not None else _rows(25))
        self.report = dict(
            report if report is not None else {"ok": True, "status": "completed"}
        )
        self.fail_times = fail_times
        self.raises = raises
        self.calls = 0
        self.last_kwargs: dict[str, Any] = {}

    async def run_cycle(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        self.last_kwargs = dict(kwargs)
        if self.raises is not None:
            raise self.raises
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("provider unavailable")
        return dict(self.report)

    def candidates(self, *, limit: int = 500) -> dict[str, Any]:
        return {"candidates": [dict(row) for row in self.rows[:limit]], "total": len(self.rows)}


def _rows(count: int, *, prefix: str = "NSE:") -> list[dict[str, Any]]:
    return [
        {
            "instrument_id": f"{prefix}{index:03d}",
            "symbol": f"SYM{index:03d}",
            "exchange": "NSE",
            "data_quality": "good",
        }
        for index in range(count)
    ]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class SessionEvaluatorTests(unittest.TestCase):
    def test_verified_session_is_open(self) -> None:
        decision = evaluate_session("XNSE", _OPEN)
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.verdict.value, "trading_session")
        self.assertEqual(decision.reason_code, "TRADING_SESSION")
        self.assertEqual(decision.trading_date, "2026-11-11")
        self.assertEqual(decision.timezone, "Asia/Kolkata")
        self.assertTrue(decision.calendar_verified)

    def test_local_time_uses_exchange_timezone_not_host(self) -> None:
        decision = evaluate_session("XNSE", _OPEN)
        self.assertIn("+05:30", decision.local_time)
        self.assertTrue(decision.local_time.startswith("2026-11-11T09:30"))

    def test_before_open_is_not_eligible(self) -> None:
        decision = evaluate_session("XNSE", datetime(2026, 11, 11, 3, 0, tzinfo=UTC))
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.verdict.value, "market_not_open")
        self.assertEqual(decision.reason_code, "MARKET_NOT_OPEN")

    def test_after_close_is_not_eligible(self) -> None:
        decision = evaluate_session("XNSE", datetime(2026, 11, 11, 10, 30, tzinfo=UTC))
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.verdict.value, "market_closed")
        self.assertEqual(decision.reason_code, "MARKET_CLOSED")

    def test_weekend_is_not_a_trading_day(self) -> None:
        decision = evaluate_session("XNSE", datetime(2026, 11, 14, 5, 0, tzinfo=UTC))
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.verdict.value, "weekend")
        self.assertEqual(decision.reason_code, "WEEKEND")

    def test_verified_holiday_fails_closed(self) -> None:
        decision = evaluate_session("XNSE", datetime(2026, 11, 10, 5, 0, tzinfo=UTC))
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.verdict.value, "exchange_holiday")
        self.assertEqual(decision.reason_code, "EXCHANGE_HOLIDAY")

    def test_unverified_year_fails_closed(self) -> None:
        decision = evaluate_session("XNSE", datetime(2027, 1, 4, 5, 0, tzinfo=UTC))
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.verdict.value, "calendar_unverified")
        self.assertEqual(decision.reason_code, "CALENDAR_UNVERIFIED")
        self.assertFalse(decision.calendar_verified)

    def test_missing_calendar_fails_closed(self) -> None:
        decision = evaluate_session("XNOT", _OPEN)
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.verdict.value, "calendar_unavailable")
        self.assertEqual(decision.reason_code, "CALENDAR_UNAVAILABLE")

    def test_boundary_times_are_deterministic(self) -> None:
        opens = evaluate_session("XNSE", datetime(2026, 11, 11, 3, 45, tzinfo=UTC))
        closes = evaluate_session("XNSE", datetime(2026, 11, 11, 9, 59, 59, tzinfo=UTC))
        self.assertTrue(opens.eligible)
        self.assertTrue(closes.eligible)
        after = evaluate_session("XNSE", datetime(2026, 11, 11, 10, 0, tzinfo=UTC))
        self.assertFalse(after.eligible)

    def test_naive_datetime_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_session("XNSE", datetime(2026, 11, 11, 9, 30))

    def test_decision_carries_verification_metadata(self) -> None:
        body = evaluate_session("XNSE", _OPEN).to_dict()
        self.assertTrue(body["timezone_aware"])
        self.assertTrue(body["calendar_source"])
        self.assertEqual(body["coverage_year"], 2026)
        self.assertTrue(body["session_open"])
        self.assertTrue(body["session_close"])


class MonitoringConfigTests(unittest.TestCase):
    def test_defaults_are_bounded(self) -> None:
        config = MonitoringConfig()
        self.assertTrue(config.enabled)
        self.assertEqual(config.exchange, "XNSE")
        self.assertEqual(config.interval_seconds, 900)
        self.assertEqual(config.max_instruments_per_cycle, 20)

    def test_refresh_floor_is_enforced(self) -> None:
        with self.assertRaises(AutonomyValidationError):
            MonitoringConfig(interval_seconds=60)
        with self.assertRaises(AutonomyValidationError):
            MonitoringConfig(interval_seconds=0)

    def test_batch_cap_is_enforced(self) -> None:
        with self.assertRaises(AutonomyValidationError):
            MonitoringConfig(max_instruments_per_cycle=21)
        with self.assertRaises(AutonomyValidationError):
            MonitoringConfig(max_instruments_per_cycle=0)

    def test_retry_and_timeout_bounds(self) -> None:
        with self.assertRaises(AutonomyValidationError):
            MonitoringConfig(retry_max=6)
        with self.assertRaises(AutonomyValidationError):
            MonitoringConfig(cycle_timeout_seconds=4)
        with self.assertRaises(AutonomyValidationError):
            MonitoringConfig(cycle_timeout_seconds=601)

    def test_blank_exchange_is_refused_and_lowercase_normalised(self) -> None:
        with self.assertRaises(AutonomyValidationError):
            MonitoringConfig(exchange="  ")
        self.assertEqual(MonitoringConfig(exchange="xnse").exchange, "XNSE")
        self.assertEqual(MonitoringConfig(exchange="NSE").exchange, "XNSE")
        self.assertEqual(MonitoringConfig(exchange="BSE").exchange, "XBOM")


class MonitoringCycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self._tmp.name) / "novamarket.sqlite3"
        self.clock = _Clock()
        self.runtime = AutonomyRuntime(
            self.path,
            clock=self.clock,
            owner_id="at02-owner",
            lease_seconds=3600,
        )
        self.discovery = _Discovery()
        self.monitoring = AutonomyMonitoring(
            self.runtime,
            discovery=lambda: self.discovery,
            clock=self.clock,
        )

    def tearDown(self) -> None:
        try:
            self.runtime.close()
        finally:
            self._tmp.cleanup()

    def _start(self) -> None:
        self.runtime.start("monitoring")

    def _cycles(self) -> list[dict[str, Any]]:
        return self.monitoring.cycles(limit=50)["cycles"]

    def test_completed_cycle_persists_counts_candidates_and_rotation(self) -> None:
        self._start()
        result = _run(self.monitoring.run_cycle())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["eligible"], 25)
        self.assertEqual(result["processed"], 20)
        self.assertEqual(result["deferred"], 5)
        self.assertEqual(result["rotation_cursor"], 20)
        self.assertTrue(result["trading_implemented"] is False)

        row = self.monitoring.cycles()["cycles"][0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["processed_count"], 20)
        self.assertEqual(row["deferred_count"], 5)
        self.assertTrue(row["calendar_verified"])
        self.assertEqual(row["trading_date"], "2026-11-11")
        self.assertIsNone(self.runtime.status()["active_cycle"])

        detail = self.monitoring.cycle(row["cycle_id"])
        dispositions = [item["disposition"] for item in detail["candidates"]]
        self.assertEqual(dispositions.count("processed"), 20)
        self.assertEqual(dispositions.count("deferred"), 5)

    def test_outside_session_is_deferred_without_claiming_a_cycle(self) -> None:
        self.clock.now = datetime(2026, 11, 11, 3, 0, tzinfo=UTC)
        self._start()
        result = _run(self.monitoring.run_cycle())
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason_code"], "OUTSIDE_TRADING_SESSION")
        self.assertIsNone(self.runtime.status()["active_cycle"])
        count = self.runtime.store._conn.execute(
            "SELECT COUNT(*) FROM autonomy_cycles"
        ).fetchone()[0]
        self.assertEqual(count, 0)
        row = self._cycles()[0]
        self.assertEqual(row["status"], "deferred")
        self.assertEqual(row["processed_count"], 0)

    def test_holiday_is_deferred_not_completed(self) -> None:
        self.clock.now = datetime(2026, 11, 10, 5, 0, tzinfo=UTC)
        self._start()
        result = _run(self.monitoring.run_cycle())
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason_code"], "EXCHANGE_HOLIDAY")
        self.assertEqual(self._cycles()[0]["status"], "deferred")

    def test_unverified_year_fails_closed(self) -> None:
        self.clock.now = datetime(2027, 1, 4, 5, 0, tzinfo=UTC)
        self._start()
        result = _run(self.monitoring.run_cycle())
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason_code"], "CALENDAR_UNVERIFIED")
        self.assertFalse(result["session"]["calendar_verified"])
        self.assertIsNone(self.runtime.status()["active_cycle"])

    def test_disabled_runtime_refuses_without_recording_a_cycle(self) -> None:
        result = _run(self.monitoring.run_cycle())
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reason_code"], "RUNTIME_DISABLED")
        self.assertEqual(self._cycles(), [])

    def test_paused_runtime_refuses_and_preserves_the_exit_policy(self) -> None:
        self._start()
        self.runtime.pause()
        result = _run(self.monitoring.run_cycle())
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reason_code"], "RUNTIME_PAUSED")
        self.assertEqual(self._cycles(), [])
        contract = self.monitoring.contract()
        self.assertTrue(contract["paper_only"])
        self.assertFalse(contract["trading_implemented"])

    def test_expired_lease_refuses(self) -> None:
        self.runtime.close()
        self.runtime = AutonomyRuntime(
            self.path,
            clock=self.clock,
            owner_id="at02-owner",
            lease_seconds=30,
        )
        self.monitoring = AutonomyMonitoring(
            self.runtime,
            discovery=lambda: self.discovery,
            clock=self.clock,
        )
        self._start()
        self.clock.advance(31)
        result = _run(self.monitoring.run_cycle())
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reason_code"], "LEASE_INVALID")
        self.assertEqual(self._cycles(), [])

    def test_autonomous_mode_runs_monitoring_but_never_trades(self) -> None:
        self.runtime.start("autonomous", acknowledge_paper_only=True)
        result = _run(self.monitoring.run_cycle())
        self.assertEqual(result["status"], "completed")
        status = self.monitoring.status()
        self.assertEqual(status["mode"], "autonomous")
        self.assertFalse(status["trading_implemented"])
        self.assertFalse(status["orders_possible"])
        self.assertFalse(status["live_trading"])

    def test_discovery_failure_is_persisted_and_counted(self) -> None:
        self._start()
        self.discovery.raises = RuntimeError("provider down")
        result = _run(self.monitoring.run_cycle())
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "DISCOVERY_PROVIDER_FAILURE")
        row = self._cycles()[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(self.monitoring.status()["health"]["consecutive_failures"], 1)
        self.assertIsNone(self.runtime.status()["active_cycle"])

    def test_transient_failure_is_retried(self) -> None:
        self._start()
        self.discovery.fail_times = 1
        result = _run(self.monitoring.run_cycle())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.discovery.calls, 2)

    def test_provider_failure_with_no_candidates_is_a_failure(self) -> None:
        self._start()
        self.discovery.rows = []
        self.discovery.report = {"ok": False, "failed_sources": 2, "errors": "timeout"}
        result = _run(self.monitoring.run_cycle())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "DISCOVERY_PROVIDER_FAILURE")

    def test_empty_candidate_set_completes_with_a_warning(self) -> None:
        self._start()
        self.discovery.rows = []
        result = _run(self.monitoring.run_cycle())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["error_code"], "NO_CANDIDATES")
        self.assertEqual(result["processed"], 0)
        self.assertEqual(self.monitoring.status()["rotation"]["cursor"], 0)

    def test_timeout_is_reported_after_retries(self) -> None:
        self._start()
        self.monitoring.save_config(MonitoringConfig(retry_max=0, cycle_timeout_seconds=5))
        self.discovery.raises = asyncio.TimeoutError()
        result = _run(self.monitoring.run_cycle())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "CYCLE_TIMEOUT")

    def test_duplicate_and_blank_instruments_are_rejected(self) -> None:
        self._start()
        self.discovery.rows = [
            {"instrument_id": "NSE:001", "symbol": "A", "exchange": "NSE"},
            {"instrument_id": "NSE:001", "symbol": "A", "exchange": "NSE"},
            {"instrument_id": "", "symbol": "B", "exchange": "NSE"},
        ]
        result = _run(self.monitoring.run_cycle())
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["rejected"], 2)
        detail = self.monitoring.cycle(result["cycle_id"])
        reasons = [row["reason"] for row in detail["candidates"] if row["disposition"] == "rejected"]
        self.assertTrue(any("duplicate" in reason for reason in reasons))
        self.assertTrue(any("blank" in reason for reason in reasons))

    def test_instrument_lookup_rejects_unknown_identifiers(self) -> None:
        self._start()

        def lookup(instrument_id: str) -> dict[str, Any]:
            if instrument_id == "NSE:001":
                raise KeyError("not registered")
            return {"symbol": "OK", "exchange": "NSE"}

        self.monitoring = AutonomyMonitoring(
            self.runtime,
            discovery=lambda: self.discovery,
            clock=self.clock,
            instrument_lookup=lookup,
        )
        result = _run(self.monitoring.run_cycle())
        self.assertEqual(result["eligible"], 24)
        self.assertEqual(result["rejected"], 1)
        detail = self.monitoring.cycle(result["cycle_id"])
        rejected = [row for row in detail["candidates"] if row["disposition"] == "rejected"]
        self.assertIn("INVALID_INSTRUMENT", rejected[0]["reason"])

    def test_instrument_lookup_with_another_identity_fails_closed(self) -> None:
        self._start()
        discovery = _Discovery(
            [
                {
                    "instrument_id": "XNSE:ALPHA",
                    "symbol": "ALPHA",
                    "exchange": "XNSE",
                    "data_quality": "good",
                }
            ]
        )
        monitor = AutonomyMonitoring(
            self.runtime,
            discovery=lambda: discovery,
            clock=self.clock,
            instrument_lookup=lambda _instrument_id: {
                "instrument_id": "XNSE:OTHER",
                "symbol": "OTHER",
                "exchange": "XNSE",
            },
        )
        result = _run(monitor.run_cycle())
        self.assertEqual(result["eligible"], 0)
        self.assertEqual(result["rejected"], 1)
        detail = monitor.cycle(result["cycle_id"])
        rejected = [row for row in detail["candidates"] if row["disposition"] == "rejected"]
        self.assertIn("INVALID_INSTRUMENT", rejected[0]["reason"])

    def test_concurrent_cycle_is_refused_not_queued(self) -> None:
        self._start()

        async def attempt() -> dict[str, Any]:
            async with self.monitoring._lock:
                return await self.monitoring.run_cycle()

        result = _run(attempt())
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reason_code"], "ALREADY_RUNNING")
        self.assertEqual(self.discovery.calls, 0)

    def test_maybe_run_respects_the_interval(self) -> None:
        self._start()
        first = _run(self.monitoring.maybe_run())
        self.assertEqual(first["status"], "completed")
        immediate = _run(self.monitoring.maybe_run())
        self.assertEqual(immediate["status"], "not_due")
        self.clock.advance(901)
        self.assertTrue(self.runtime.heartbeat())
        due = _run(self.monitoring.maybe_run())
        self.assertEqual(due["status"], "completed")
        self.assertEqual(self.discovery.calls, 2)

    def test_status_reports_session_scheduler_and_rotation(self) -> None:
        self._start()
        _run(self.monitoring.run_cycle())
        status = self.monitoring.status()
        self.assertTrue(status["session"]["eligible"])
        self.assertEqual(status["session"]["exchange"], "XNSE")
        self.assertTrue(status["scheduler"]["running"])
        self.assertEqual(status["rotation"]["cursor"], 20)
        self.assertEqual(status["rotation"]["batch_size"], 20)
        self.assertEqual(status["health"]["state"], "ok")
        self.assertEqual(status["data_freshness"]["state"], "fresh")
        self.assertEqual(status["data_freshness"]["counts"].get("good"), 25)
        self.assertFalse(status["trading_implemented"])

    def test_cycle_detail_unknown_id_raises(self) -> None:
        with self.assertRaises(MonitoringRefused) as caught:
            self.monitoring.cycle("mon_missing")
        self.assertEqual(caught.exception.code, "CYCLE_NOT_FOUND")

    def test_closed_monitoring_reports_disabled_without_trading(self) -> None:
        body = closed_monitoring_status()
        self.assertFalse(body["initialised"])
        self.assertEqual(body["mode"], "disabled")
        self.assertFalse(body["trading_implemented"])
        self.assertFalse(body["orders_possible"])
        self.assertIn("session", body)

    def test_contract_states_cap_and_no_financial_mutation(self) -> None:
        contract = self.monitoring.contract()
        self.assertEqual(contract["batch_cap"], 20)
        self.assertEqual(contract["refresh_floor_seconds"], 900)
        self.assertTrue(contract["one_cycle_at_a_time"])
        self.assertEqual(contract["financial_mutation"], "none")
        self.assertFalse(contract["trading_implemented"])
        self.assertEqual(contract["calendar_years"], [2026])


class RotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self._tmp.name) / "novamarket.sqlite3"
        self.clock = _Clock()
        self.runtime = AutonomyRuntime(
            self.path,
            clock=self.clock,
            owner_id="rotation-owner",
            lease_seconds=600,
        )
        self.runtime.start("monitoring")

    def tearDown(self) -> None:
        try:
            self.runtime.close()
        finally:
            self._tmp.cleanup()

    def _monitor(self, rows: list[dict[str, Any]]) -> AutonomyMonitoring:
        return AutonomyMonitoring(
            self.runtime,
            discovery=lambda: _Discovery(rows),
            clock=self.clock,
        )

    def test_one_instrument(self) -> None:
        result = _run(self._monitor(_rows(1)).run_cycle())
        self.assertEqual((result["processed"], result["deferred"]), (1, 0))
        self.assertEqual(result["rotation_cursor"], 0)

    def test_exactly_twenty_instruments(self) -> None:
        result = _run(self._monitor(_rows(20)).run_cycle())
        self.assertEqual((result["processed"], result["deferred"]), (20, 0))

    def test_more_than_twenty_rotates(self) -> None:
        monitor = self._monitor(_rows(25))
        first = _run(monitor.run_cycle())
        second = _run(monitor.run_cycle())
        self.assertEqual((first["processed"], first["deferred"]), (20, 5))
        self.assertEqual((second["processed"], second["deferred"]), (5, 20))
        self.assertEqual(second["rotation_cursor"], 0)

    def test_forty_five_instruments_rotate_fairly_across_cycles(self) -> None:
        monitor = self._monitor(_rows(45))
        processed = [_run(monitor.run_cycle())["processed"] for _ in range(3)]
        self.assertEqual(processed, [20, 20, 5])
        seen: set[str] = set()
        for row in monitor.cycles(limit=10)["cycles"]:
            detail = monitor.cycle(row["cycle_id"])
            seen.update(
                item["instrument_id"]
                for item in detail["candidates"]
                if item["disposition"] == "processed"
            )
        self.assertEqual(len(seen), 45)

    def test_newly_discovered_instrument_enters_a_later_batch(self) -> None:
        rows = _rows(25)
        discovery = _Discovery(rows)
        monitor = AutonomyMonitoring(
            self.runtime, discovery=lambda: discovery, clock=self.clock
        )
        _run(monitor.run_cycle())
        discovery.rows = rows + [
            {"instrument_id": "NSE:999", "symbol": "NEW", "exchange": "NSE"}
        ]
        second = _run(monitor.run_cycle())
        detail = monitor.cycle(second["cycle_id"])
        processed = {
            item["instrument_id"]
            for item in detail["candidates"]
            if item["disposition"] == "processed"
        }
        self.assertIn("NSE:999", processed)

    def test_removed_instruments_reset_the_cursor_safely(self) -> None:
        discovery = _Discovery(_rows(25))
        monitor = AutonomyMonitoring(
            self.runtime, discovery=lambda: discovery, clock=self.clock
        )
        _run(monitor.run_cycle())
        discovery.rows = _rows(3)
        result = _run(monitor.run_cycle())
        self.assertEqual(result["processed"], 3)
        self.assertEqual(result["rotation_cursor"], 0)

    def test_failed_cycle_does_not_advance_rotation(self) -> None:
        discovery = _Discovery(_rows(25), raises=RuntimeError("boom"))
        monitor = AutonomyMonitoring(
            self.runtime, discovery=lambda: discovery, clock=self.clock
        )
        _run(monitor.run_cycle())
        self.assertEqual(monitor.status()["rotation"]["cursor"], 0)

    def test_discovery_is_scoped_to_the_configured_exchange_and_universe(self) -> None:
        discovery = _Discovery(_rows(1))
        monitor = AutonomyMonitoring(
            self.runtime,
            discovery=lambda: discovery,
            clock=self.clock,
            universe=lambda: ("XNSE:001",),
        )
        _run(monitor.run_cycle())
        scope = discovery.last_kwargs["universe"]
        self.assertEqual(scope.exchanges, ("XNSE",))
        self.assertEqual(scope.max_instruments, 20)
        self.assertEqual(discovery.last_kwargs["instrument_ids"], ("XNSE:001",))

    def test_a_candidate_outside_the_configured_universe_is_rejected(self) -> None:
        discovery = _Discovery(
            [
                {
                    "instrument_id": "XNSE:ALPHA",
                    "symbol": "ALPHA",
                    "exchange": "XNSE",
                    "data_quality": "good",
                },
                {
                    "instrument_id": "XNSE:BETA",
                    "symbol": "BETA",
                    "exchange": "XNSE",
                    "data_quality": "good",
                },
            ]
        )
        monitor = AutonomyMonitoring(
            self.runtime,
            discovery=lambda: discovery,
            clock=self.clock,
            universe=lambda: ("XNSE:ALPHA",),
        )
        result = _run(monitor.run_cycle())
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["rejected"], 1)
        detail = monitor.cycle(result["cycle_id"])
        rejected = [row for row in detail["candidates"] if row["disposition"] == "rejected"]
        self.assertEqual(rejected[0]["instrument_id"], "XNSE:BETA")
        self.assertIn("OUTSIDE_CONFIGURED_UNIVERSE", rejected[0]["reason"])

    def test_legacy_venue_names_are_stored_as_mics_and_real_mismatches_rejected(self) -> None:
        discovery = _Discovery(
            [
                {
                    "instrument_id": "XNSE:ALPHA",
                    "symbol": "ALPHA",
                    "exchange": "NSE",
                    "data_quality": "good",
                },
                {
                    "instrument_id": "XNSE:CROSS",
                    "symbol": "CROSS",
                    "exchange": "BSE",
                    "data_quality": "good",
                },
            ]
        )

        def lookup(instrument_id: str) -> dict[str, Any]:
            return {
                "symbol": instrument_id.split(":", 1)[1],
                "exchange": "NSE" if instrument_id.endswith("ALPHA") else "BSE",
                "mic": "XNSE" if instrument_id.endswith("ALPHA") else "XBOM",
            }

        monitor = AutonomyMonitoring(
            self.runtime,
            discovery=lambda: discovery,
            clock=self.clock,
            instrument_lookup=lookup,
        )
        result = _run(monitor.run_cycle())
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["rejected"], 1)
        detail = monitor.cycle(result["cycle_id"])
        processed = [row for row in detail["candidates"] if row["disposition"] == "processed"]
        rejected = [row for row in detail["candidates"] if row["disposition"] == "rejected"]
        self.assertEqual(processed[0]["exchange"], "XNSE")
        self.assertIn("EXCHANGE_MISMATCH", rejected[0]["reason"])


class PersistenceAndRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self._tmp.name) / "novamarket.sqlite3"
        self.clock = _Clock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_schema_is_v3_and_keeps_at01_tables(self) -> None:
        store = AutonomyStore(self.path)
        try:
            version = store._conn.execute(
                "SELECT value FROM autonomy_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            # Stage C R-02 adds the append-only control audit (v5).
            self.assertEqual(version, "5")
            tables = {
                row[0]
                for row in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(
                {
                    "autonomy_config",
                    "autonomy_runtime",
                    "autonomy_cycles",
                    "autonomy_monitoring_config",
                    "autonomy_monitoring_state",
                    "autonomy_monitoring_cycles",
                    "autonomy_monitoring_candidates",
                }.issubset(tables)
            )
            config = store.monitoring_config()
            self.assertEqual(config.max_instruments_per_cycle, 20)
            self.assertEqual(config.interval_seconds, 900)
        finally:
            store.close()

    def test_v1_database_migrates_additively_and_preserves_data(self) -> None:
        runtime = AutonomyRuntime(
            self.path, clock=self.clock, owner_id="migration", lease_seconds=600
        )
        runtime.start("monitoring")
        runtime.close()

        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "INSERT INTO autonomy_meta(key, value) VALUES('at01_marker', 'kept')"
            )
            for table in (
                "autonomy_monitoring_candidates",
                "autonomy_monitoring_cycles",
                "autonomy_monitoring_state",
                "autonomy_monitoring_config",
            ):
                connection.execute(f"DROP TABLE {table}")
            connection.execute(
                "UPDATE autonomy_meta SET value = '1' WHERE key = 'schema_version'"
            )
            connection.commit()
        finally:
            connection.close()

        store = AutonomyStore(self.path)
        try:
            version = store._conn.execute(
                "SELECT value FROM autonomy_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            # Stage C R-02 adds the append-only control audit (v5).
            self.assertEqual(version, "5")
            marker = store._conn.execute(
                "SELECT value FROM autonomy_meta WHERE key = 'at01_marker'"
            ).fetchone()[0]
            self.assertEqual(marker, "kept")
            tables = {
                row[0]
                for row in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertIn("autonomy_monitoring_cycles", tables)
        finally:
            store.close()

    def test_interrupted_monitoring_cycle_is_marked_failed_not_completed(self) -> None:
        runtime = AutonomyRuntime(
            self.path, clock=self.clock, owner_id="recovery", lease_seconds=600
        )
        runtime.start("monitoring")
        runtime.store.create_monitoring_cycle(
            {
                "cycle_id": "mon_interrupted",
                "cycle_key": "monitoring:XNSE:2026-11-11:interrupted",
                "owner_id": "recovery",
                "mode": "monitoring",
                "exchange": "XNSE",
                "trading_date": "2026-11-11",
                "calendar_verified": True,
                "calendar_source": "marketdata/calendar.py",
                "session_state": "trading_session",
                "session_eligible": True,
                "phase": "discovery",
                "status": "running",
                "trigger": "manual",
                "started_at": self.clock.now.isoformat(),
                "rotation_start": 0,
            },
            now=self.clock.now,
        )
        self.clock.advance(601)  # the dead owner's lease must expire first
        runtime.recover()
        row = runtime.store.monitoring_cycle("mon_interrupted")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], "CYCLE_CANCELLED")
        self.assertTrue(row["finished_at"])
        self.assertEqual(runtime.status()["mode"], "disabled")
        runtime.close()

    def test_rotation_state_persists_across_instances(self) -> None:
        first = AutonomyRuntime(
            self.path, clock=self.clock, owner_id="persist", lease_seconds=600
        )
        first.start("monitoring")
        monitor = AutonomyMonitoring(
            first, discovery=lambda: _Discovery(_rows(25)), clock=self.clock
        )
        _run(monitor.run_cycle())
        first.close()

        second = AutonomyRuntime(
            self.path, clock=self.clock, owner_id="persist-2", lease_seconds=600
        )
        self.assertEqual(second.store.monitoring_state()["rotation_cursor"], 20)
        second.close()

    def test_monitoring_config_persists_and_reloads(self) -> None:
        runtime = AutonomyRuntime(
            self.path, clock=self.clock, owner_id="config", lease_seconds=600
        )
        runtime.start("monitoring")
        monitor = AutonomyMonitoring(
            runtime, discovery=lambda: _Discovery([]), clock=self.clock
        )
        saved = monitor.save_config(MonitoringConfig(interval_seconds=1800, exchange="XBOM"))
        self.assertEqual(saved.interval_seconds, 1800)
        self.assertEqual(saved.exchange, "XBOM")
        runtime.close()

        reopened = AutonomyRuntime(
            self.path, clock=self.clock, owner_id="config-2", lease_seconds=600
        )
        self.assertEqual(reopened.store.monitoring_config().interval_seconds, 1800)
        reopened.close()


@unittest.skipIf(TestClient is None, "fastapi.testclient requires httpx")
class MonitoringApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self._env = mock.patch.dict(
            os.environ,
            {
                "NOVACONTROL_API_TOKEN": "api-secret",
                "NOVACONTROL_AUTONOMY_OPERATOR_TOKEN": "operator-secret",
            },
            clear=False,
        )
        self._env.start()
        from novacontrol.api.app import create_app

        self.app = create_app(data_dir=self._tmp.name)
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer api-secret"}
        self.operator = {
            "Authorization": "Bearer api-secret",
            "X-Autonomy-Operator-Token": "operator-secret",
        }

    def tearDown(self) -> None:
        self.client.close()
        self._env.stop()
        self._tmp.cleanup()

    def test_session_endpoint_is_authenticated_and_verified(self) -> None:
        self.assertEqual(self.client.get("/market/autonomy/session").status_code, 401)
        response = self.client.get("/market/autonomy/session", headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["exchange"], "XNSE")
        self.assertIn("eligible", body)
        self.assertIn("reason_code", body)
        self.assertIn("calendar_verified", body)

    def test_session_endpoint_reports_unknown_exchange_fail_closed(self) -> None:
        response = self.client.get(
            "/market/autonomy/session", params={"exchange": "XNOT"}, headers=self.auth
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["eligible"])
        self.assertEqual(response.json()["verdict"], "calendar_unavailable")

    def test_monitoring_reads_are_authenticated(self) -> None:
        for path in (
            "/market/autonomy/monitoring",
            "/market/autonomy/monitoring/contracts",
            "/market/autonomy/monitoring/cycles",
        ):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        status = self.client.get("/market/autonomy/monitoring", headers=self.auth)
        self.assertEqual(status.status_code, 200, status.text)
        body = status.json()
        self.assertTrue(body["paper_only"])
        self.assertFalse(body["trading_implemented"])
        self.assertFalse(body["orders_possible"])
        self.assertIn("session", body)
        self.assertIn("scheduler", body)
        self.assertIn("rotation", body)

    def test_monitoring_run_requires_the_operator_permission(self) -> None:
        refused = self.client.post("/market/autonomy/monitoring/run", headers=self.auth, json={})
        self.assertEqual(refused.status_code, 403, refused.text)

    def test_manual_run_after_start_is_gated_by_the_verified_session(self) -> None:
        started = self.client.post(
            "/market/autonomy/start",
            headers=self.operator,
            json={"mode": "monitoring"},
        )
        self.assertEqual(started.status_code, 200, started.text)
        # Pin the closed-session verdict: the real session clock would otherwise
        # make this gate test pass or fail depending on the hour the suite runs.
        closed = evaluate_session("XNSE", datetime(2026, 11, 10, 4, 0, tzinfo=UTC))
        with mock.patch(
            "novacontrol.novamarket.autonomy.monitoring.evaluate_session",
            return_value=closed,
        ):
            result = self.client.post(
                "/market/autonomy/monitoring/run", headers=self.operator, json={}
            )
        self.assertEqual(result.status_code, 200, result.text)
        body = result.json()
        self.assertEqual(body["status"], "deferred")
        self.assertFalse(body["trading_implemented"])
        self.assertFalse(body["orders_possible"])
        self.assertIn(body["reason_code"], {"EXCHANGE_HOLIDAY", "MARKET_NOT_OPEN", "MARKET_CLOSED"})
        self.assertIsNone(
            self.client.get("/market/autonomy/status", headers=self.operator).json()["active_cycle"]
        )

    def test_cycle_list_and_unknown_detail(self) -> None:
        listing = self.client.get("/market/autonomy/monitoring/cycles", headers=self.auth)
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertIn("cycles", listing.json())
        missing = self.client.get(
            "/market/autonomy/monitoring/cycles/mon_missing", headers=self.auth
        )
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_cycle_limit_is_validated(self) -> None:
        for limit in ("0", "201", "abc"):
            response = self.client.get(
                "/market/autonomy/monitoring/cycles",
                params={"limit": limit},
                headers=self.auth,
            )
            self.assertEqual(response.status_code, 422, (limit, response.text))

    def test_monitoring_config_validates_and_persists(self) -> None:
        rejected = self.client.post(
            "/market/autonomy/monitoring/config",
            headers=self.operator,
            json={"interval_seconds": 60, "max_instruments_per_cycle": 21},
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)
        accepted = self.client.post(
            "/market/autonomy/monitoring/config",
            headers=self.operator,
            json={"interval_seconds": 1800, "max_instruments_per_cycle": 10},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["interval_seconds"], 1800)
        self.assertEqual(accepted.json()["max_instruments_per_cycle"], 10)
        status = self.client.get("/market/autonomy/monitoring", headers=self.auth)
        self.assertEqual(status.json()["rotation"]["batch_size"], 10)

    def test_monitoring_config_requires_the_operator_permission(self) -> None:
        refused = self.client.post(
            "/market/autonomy/monitoring/config", headers=self.auth, json={"enabled": False}
        )
        self.assertEqual(refused.status_code, 403, refused.text)

    def test_every_at02_route_has_a_contract_and_consumer(self) -> None:
        expected = {
            ("GET", "/market/autonomy/session"),
            ("GET", "/market/autonomy/monitoring/contracts"),
            ("GET", "/market/autonomy/monitoring"),
            ("GET", "/market/autonomy/monitoring/cycles"),
            ("GET", "/market/autonomy/monitoring/cycles/{cycle_id}"),
            ("POST", "/market/autonomy/monitoring/run"),
            ("POST", "/market/autonomy/monitoring/config"),
        }
        self.assertTrue(expected.issubset(ROUTE_CONSUMERS))
        surface = {(route.method, route.path) for route in ApiSurface.default().routes}
        self.assertTrue(expected.issubset(surface))


class _RealSeam:
    """The market-data reads NM-05 needs, over its real store."""

    def __init__(self, store: MarketDataStore) -> None:
        self.store = store

    def get_instrument(self, instrument_id: str) -> Instrument:
        return self.store.require_instrument(instrument_id)

    def list_instruments(self, *, asset_class=None, exchange=None, limit=100, offset=0):
        return self.store.list_instruments(
            asset_class=asset_class, exchange=exchange, limit=limit, offset=offset
        )

    def bars(self, instrument_id: str, *, limit: int = 100, ascending: bool = False):
        return self.store.bars(instrument_id, limit=limit, ascending=ascending)

    def bar_inventory(self, *, interval: str = "1d", min_bars: int = 1, limit: int = 500):
        return self.store.bar_inventory(interval=interval, min_bars=min_bars, limit=limit)

    def latest(self, instrument_id: str) -> Any:
        rows = self.store.latest_observations(instrument_id)
        return SimpleNamespace(selected=rows[0] if rows else None, observations=rows)


class RealDiscoveryEngineIntegrationTests(unittest.TestCase):
    """AT-02 drives NM-05's actual engine, store and candidate ranking."""

    def test_real_discovery_engine_feeds_a_monitoring_cycle(self) -> None:
        now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
        market = MarketDataStore(":memory:", clock=lambda: now)
        market.upsert_instrument(
            Instrument(
                instrument_id="XNSE:ALPHA",
                symbol="ALPHA",
                name="Alpha Energy Ltd",
                asset_class=AssetClass.EQUITY,
                exchange="XNSE",
                mic="XNSE",
                currency="INR",
                timezone="Asia/Kolkata",
                provider_symbols={"fixture": "ALPHA"},
                trading_status=TradingStatus.OPEN,
                metadata={"sector": "Energy"},
            )
        )
        for index, close in enumerate((100.0, 105.0, 110.0)):
            observed = now - timedelta(days=3 - index, hours=1)
            market.append_bar(
                OhlcvBar(
                    bar_id=f"bar_{index}",
                    instrument_id="XNSE:ALPHA",
                    provider="fixture",
                    interval=DataInterval.DAY_1,
                    open=Decimal(str(close - 1)),
                    high=Decimal(str(close + 1)),
                    low=Decimal(str(close - 2)),
                    close=Decimal(str(close)),
                    currency="INR",
                    observed_at=observed,
                    retrieved_at=observed + timedelta(minutes=1),
                    source_timezone="UTC",
                    volume=Decimal("1000000"),
                )
            )
        observed = now - timedelta(hours=1)
        market.append_observation(
            PriceObservation(
                observation_id="obs_alpha",
                instrument_id="XNSE:ALPHA",
                provider="fixture",
                price=Decimal("110"),
                currency="INR",
                observed_at=observed,
                retrieved_at=observed + timedelta(minutes=1),
                source_timezone="UTC",
            )
        )
        engine = AssetDiscoveryEngine(
            DiscoveryStore(":memory:", clock=lambda: now),
            market_data=_RealSeam(market),
            universe=UniverseFilter(asset_classes=("equity",)),
            policy=DiscoveryPolicy(add_threshold=40.0),
            clock=lambda: now,
        )
        clock = _Clock()
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            runtime = AutonomyRuntime(
                Path(tmp) / "nova.sqlite3",
                clock=clock,
                owner_id="real-engine",
                lease_seconds=3600,
            )
            runtime.start("monitoring")
            monitoring = AutonomyMonitoring(
                runtime,
                discovery=lambda: engine,
                clock=clock,
                instrument_lookup=lambda iid: market.require_instrument(iid).to_dict(),
            )
            result = _run(monitoring.run_cycle())
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(result["eligible"], 1)
            self.assertEqual(result["processed"], 1)
            detail = monitoring.cycle(result["cycle_id"])
            processed = [
                row for row in detail["candidates"] if row["disposition"] == "processed"
            ]
            self.assertEqual(processed[0]["instrument_id"], "XNSE:ALPHA")
            self.assertTrue(processed[0]["symbol"])
            self.assertIn(
                processed[0]["data_quality"],
                {"good", "partial", "stale", "insufficient"},
            )
            runtime.close()


class FinancialSafetyTests(unittest.TestCase):
    def test_at02_modules_reference_no_financial_engine(self) -> None:
        forbidden_roots = {
            "execution",
            "portfolio",
            "strategy",
            "strategies",
            "risk",
            "wallet",
            "learning",
            "broker",
        }
        forbidden_names = {
            "PortfolioEngine",
            "ExecutionEngine",
            "WalletEngine",
            "StrategyEngine",
            "RiskEngine",
        }
        for path in _PACKAGE.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    parts = set(node.module.split("."))
                    self.assertFalse(
                        parts & forbidden_roots,
                        f"{path.name} imports financial module {node.module}",
                    )
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        parts = set(alias.name.split("."))
                        self.assertFalse(
                            parts & forbidden_roots,
                            f"{path.name} imports financial module {alias.name}",
                        )
                if isinstance(node, ast.Name):
                    self.assertNotIn(
                        node.id, forbidden_names, f"{path.name} references {node.id}"
                    )
                if isinstance(node, ast.Attribute) and node.attr == "apply":
                    self.fail(f"{path.name} references an apply() financial mutation")

    def test_monitoring_contract_and_status_never_claim_trading(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            runtime = AutonomyRuntime(
                Path(tmp) / "nova.sqlite3",
                clock=_Clock(),
                owner_id="safety",
                lease_seconds=600,
            )
            runtime.start("autonomous", acknowledge_paper_only=True)
            monitoring = AutonomyMonitoring(
                runtime, discovery=lambda: _Discovery([]), clock=_Clock()
            )
            for body in (monitoring.status(), monitoring.contract(), closed_monitoring_status()):
                self.assertTrue(body["paper_only"])
                self.assertFalse(body["live_trading"])
                self.assertFalse(body["trading_implemented"])
                self.assertFalse(body["orders_possible"])
                self.assertFalse(body["broker_connected"])
            runtime.close()

    def test_autonomy_surface_has_no_order_or_execution_route(self) -> None:
        # AT-03 intentionally exposes non-executable proposal reads. The safety
        # invariant is narrower and stronger: no autonomy route may name or
        # provide an order, fill, execution or trade operation.
        forbidden = ("order", "fill", "execute", "trade")
        for route in ApiSurface.default().routes:
            if route.path.startswith("/market/autonomy"):
                for token in forbidden:
                    self.assertNotIn(token, route.path.lower(), route.path)


if __name__ == "__main__":
    unittest.main()
