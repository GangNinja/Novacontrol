"""NM-05: autonomous asset discovery, explainable scoring and watchlists.

The tests drive the real engine, the real SQLite store and NM-02's real
instrument/observation/bar storage. Only the market-data *facade* and, where an
event is needed, the feed body are supplied by the test; scoring, dedupe of
memberships, lifecycle decisions, persistence and event publication are the
implementation.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from novacontrol.novamarket.contracts import (
    DiscoveryNotFoundError,
    DiscoveryValidationError,
)
from novacontrol.novamarket.discovery import (
    AssetDiscoveryEngine,
    DataQuality,
    DiscoveryPolicy,
    DiscoveryReason,
    DiscoveryStore,
    EntryStatus,
    EventSignal,
    SessionState,
    TriggerKind,
    UniverseFilter,
    session_state,
    sessions_for,
    ensure_entry_transition,
    score_candidate,
)
from novacontrol.novamarket.discovery import scoring as scoring_module
from novacontrol.novamarket.marketdata.models import (
    AssetClass,
    DataInterval,
    Instrument,
    OhlcvBar,
    PriceObservation,
    TradingStatus,
)
from novacontrol.novamarket.marketdata.store import MarketDataStore
from novacontrol.novamarket.radar.models import EventSourceDefinition
from novacontrol.novamarket.radar import (
    EventQuery,
    EventStore as RadarEventStore,
    FeedFormat,
    InstrumentView as RadarInstrumentView,
    MarketEventRadar,
    SourceCatalog,
    SourceReliability,
    SourceType,
    parse_feed,
)

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
ALPHA = "XNSE:ALPHA"


def _instrument(
    store: MarketDataStore,
    instrument_id: str = ALPHA,
    *,
    asset_class: str = "equity",
    exchange: str = "XNSE",
    sector: str = "Energy",
    status: str = "open",
    name: str = "Alpha Energy Ltd",
    currency: str = "INR",
) -> Instrument:
    mic, symbol = instrument_id.split(":", 1)
    instrument = Instrument(
        instrument_id=instrument_id,
        symbol=symbol,
        name=name,
        asset_class=AssetClass(asset_class),
        exchange=exchange,
        mic=mic,
        currency=currency,
        timezone="Asia/Kolkata",
        provider_symbols={"fixture": symbol},
        trading_status=TradingStatus(status),
        metadata={"sector": sector},
    )
    store.upsert_instrument(instrument)
    return instrument


def _bars(
    store: MarketDataStore,
    instrument_id: str = ALPHA,
    closes: tuple[float, ...] = (100.0, 105.0, 110.0),
    *,
    volume: float | None = 1_000_000.0,
) -> None:
    for index, close in enumerate(closes):
        observed = NOW - timedelta(days=len(closes) - index, hours=1)
        store.append_bar(
            OhlcvBar(
                bar_id=f"bar_{instrument_id}_{index}",
                instrument_id=instrument_id,
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
                volume=Decimal(str(volume)) if volume is not None else None,
            )
        )


def _quote(
    store: MarketDataStore, instrument_id: str = ALPHA, *, price: float = 110.0, age_hours: float = 1.0
) -> None:
    observed = NOW - timedelta(hours=age_hours)
    store.append_observation(
        PriceObservation(
            observation_id=f"obs_{instrument_id}_{price}_{age_hours}",
            instrument_id=instrument_id,
            provider="fixture",
            price=Decimal(str(price)),
            currency="INR",
            observed_at=observed,
            retrieved_at=observed + timedelta(minutes=1),
            source_timezone="UTC",
        )
    )


class _Seam:
    """The four market-data reads discovery needs, over the real store."""

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
        return self.store.bar_inventory(
            interval=interval, min_bars=min_bars, limit=limit
        )

    def latest(self, instrument_id: str) -> Any:
        rows = self.store.latest_observations(instrument_id)
        return SimpleNamespace(selected=rows[0] if rows else None, observations=rows)


class _Publisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, payload: dict[str, Any]) -> None:
        self.events.append((name, payload))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]


def _engine(
    store: MarketDataStore | None = None,
    *,
    radar: Any | None = None,
    publisher: Any | None = None,
    universe: UniverseFilter | None = None,
    policy: DiscoveryPolicy | None = None,
) -> tuple[AssetDiscoveryEngine, DiscoveryStore, _Seam | None]:
    market = store or MarketDataStore(":memory:", clock=lambda: NOW)
    seam = _Seam(market)
    discovery_store = DiscoveryStore(":memory:", clock=lambda: NOW)
    engine = AssetDiscoveryEngine(
        discovery_store,
        market_data=seam,
        radar=radar,
        universe=universe or UniverseFilter(asset_classes=("equity", "etf", "index")),
        policy=policy or DiscoveryPolicy(add_threshold=40.0),
        publisher=publisher,
        clock=lambda: NOW,
    )
    return engine, discovery_store, seam


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


class ScoringModelTests(unittest.TestCase):
    def _score(self, **overrides: Any):
        values: dict[str, Any] = dict(
            instrument_id=ALPHA,
            asset_class="equity",
            trading_status="open",
            bars=[scoring_module.BarPoint(c, 1_000_000.0, NOW - timedelta(hours=2)) for c in (100.0, 105.0, 110.0)],
            quote_price=110.0,
            quote_observed_at=NOW - timedelta(hours=1),
            events=(),
            sector_events=(),
            discovery_reason=DiscoveryReason.CATALOGUE,
            now=NOW,
            policy=DiscoveryPolicy(),
        )
        values.update(overrides)
        return score_candidate(**values)

    def test_weights_are_published_and_sum_to_one(self) -> None:
        self.assertAlmostEqual(sum(scoring_module.WEIGHTS.values()), 1.0, places=6)
        for name, factor in zip(
            scoring_module.WEIGHTS,
            self._score().factors,
        ):
            self.assertEqual(name, factor.name)
            self.assertAlmostEqual(factor.weight, scoring_module.WEIGHTS[name])

    def test_every_factor_is_explained(self) -> None:
        evaluation = self._score()
        self.assertEqual(len(evaluation.factors), 7)
        for factor in evaluation.factors:
            self.assertTrue(factor.reason)
            if not factor.missing:
                self.assertAlmostEqual(
                    factor.contribution, factor.value * factor.weight * 100, places=3
                )
        payload = evaluation.to_dict()
        self.assertFalse(payload["is_trading_recommendation"])
        self.assertFalse(payload["is_price_prediction"])
        self.assertTrue(payload["factors"])

    def test_activity_and_score_rise_with_the_move(self) -> None:
        rising = self._score()
        flat = self._score(
            bars=[scoring_module.BarPoint(100.0, 1_000_000.0, NOW) for _ in range(3)]
        )
        self.assertGreater(rising.score, flat.score)
        self.assertEqual(
            [factor.value for factor in flat.factors if factor.name == "market_activity"],
            [0.0],
        )

    def test_missing_volume_is_unknown_not_zero(self) -> None:
        evaluation = self._score(
            bars=[scoring_module.BarPoint(c, None, NOW) for c in (100.0, 105.0)]
        )
        liquidity = next(f for f in evaluation.factors if f.name == "liquidity")
        self.assertTrue(liquidity.missing)
        self.assertIsNone(liquidity.value)
        self.assertIn("liquidity", evaluation.missing_fields)
        self.assertEqual(evaluation.data_quality, DataQuality.PARTIAL)
        self.assertLessEqual(evaluation.score, scoring_module.QUALITY_CAPS[DataQuality.PARTIAL])

    def test_stale_data_caps_the_score(self) -> None:
        evaluation = self._score(quote_observed_at=NOW - timedelta(days=5))
        self.assertEqual(evaluation.data_quality, DataQuality.STALE)
        self.assertLessEqual(evaluation.score, 35.0)

    def test_no_data_is_insufficient_and_never_high(self) -> None:
        evaluation = self._score(bars=[], quote_price=None, quote_observed_at=None)
        self.assertEqual(evaluation.data_quality, DataQuality.INSUFFICIENT)
        self.assertLessEqual(evaluation.score, 15.0)
        self.assertTrue(evaluation.notes)

    def test_scoring_is_reproducible(self) -> None:
        first = self._score()
        second = self._score()
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.score, second.score)
        changed = self._score(quote_price=111.0)
        self.assertNotEqual(first.digest, changed.digest)

    def test_verified_events_outweigh_unverified_ones(self) -> None:
        confirmed = self._score(
            events=(
                EventSignal("evt_1", "critical", "officially_confirmed", "direct", NOW),
            )
        )
        rumour = self._score(
            events=(EventSignal("evt_2", "critical", "unverified", "direct", NOW),)
        )
        self.assertGreater(confirmed.score, rumour.score)
        self.assertEqual(rumour.event_ids, ("evt_2",))

    def test_a_retracted_event_contributes_nothing(self) -> None:
        evaluation = self._score(
            events=(EventSignal("evt_1", "critical", "retracted", "direct", NOW),)
        )
        event_factor = next(f for f in evaluation.factors if f.name == "event_relevance")
        self.assertEqual(event_factor.value, 0.0)

    def test_an_event_outside_the_window_is_ignored(self) -> None:
        old = self._score(
            events=(
                EventSignal(
                    "evt_old", "critical", "officially_confirmed", "direct", NOW - timedelta(days=60)
                ),
            )
        )
        factor = next(f for f in old.factors if f.name == "event_relevance")
        self.assertEqual(factor.value, 0.0)


# --------------------------------------------------------------------------
# Discovery cycles
# --------------------------------------------------------------------------


class DiscoveryCycleTests(unittest.IsolatedAsyncioTestCase):
    def _seeded(self) -> tuple[AssetDiscoveryEngine, DiscoveryStore, _Publisher]:
        market = MarketDataStore(":memory:", clock=lambda: NOW)
        _instrument(market)
        _bars(market)
        _quote(market)
        publisher = _Publisher()
        engine, store, _ = _engine(market, publisher=publisher)
        return engine, store, publisher

    async def test_a_cycle_discovers_evaluates_and_watchlists_an_instrument(self) -> None:
        engine, store, publisher = self._seeded()
        report = await engine.run_cycle()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["scanned"], 1)
        self.assertEqual(report["evaluated"], 1)
        self.assertGreaterEqual(report["added"], 1)
        candidate = engine.candidates()["candidates"][0]
        self.assertEqual(candidate["instrument_id"], ALPHA)
        self.assertGreater(candidate["last_score"], 0)
        self.assertTrue(candidate["on_watchlist"])
        self.assertTrue(candidate["watchlist_ids"])
        detail = engine.candidate(ALPHA)
        self.assertTrue(detail["on_watchlist"])
        self.assertEqual(detail["watchlist_ids"], candidate["watchlist_ids"])
        self.assertIn(
            "novamarket.asset.discovered", publisher.names()
        )
        self.assertIn(
            "novamarket.asset.candidate.evaluated", publisher.names()
        )
        self.assertGreaterEqual(store.metrics()["evaluations"], 1)
        self.assertGreaterEqual(store.metrics()["cycles"], 1)

    async def test_data_less_catalogue_rows_are_not_candidates(self) -> None:
        market = MarketDataStore(":memory:", clock=lambda: NOW)
        _instrument(market)
        _bars(market)
        _quote(market)
        _instrument(market, "XNSE:BETA", name="Beta Ltd")
        engine, store, _ = _engine(market)
        store.upsert_candidate(
            "XNSE:BETA",
            at=NOW,
            reason="catalogue",
            score=0.0,
            quality="insufficient",
        )
        report = await engine.run_cycle()
        self.assertEqual(report["scanned"], 1)
        self.assertEqual(report["pruned"], 1)
        self.assertEqual(
            [row["instrument_id"] for row in engine.candidates()["candidates"]],
            [ALPHA],
        )

    async def test_the_discovery_watchlist_ranks_its_members(self) -> None:
        engine, _, _ = self._seeded()
        await engine.run_cycle()
        lists = {row["kind"]: row for row in engine.watchlists()["watchlists"]}
        discovery = engine.watchlist(lists["discovery"]["watchlist_id"])
        self.assertEqual(discovery["total"], 1)
        member = discovery["members"][0]
        self.assertEqual(member["instrument_id"], ALPHA)
        self.assertEqual(member["status"], "active")
        self.assertTrue(member["reason"])

    async def test_unsupported_asset_classes_are_not_scanned(self) -> None:
        market = MarketDataStore(":memory:", clock=lambda: NOW)
        _instrument(market, "CRYPTO:BTC-USD", asset_class="crypto", exchange="CRYPTO", sector="")
        _bars(market, "CRYPTO:BTC-USD")
        _quote(market, "CRYPTO:BTC-USD")
        engine, _, _ = _engine(market)
        report = await engine.run_cycle()
        self.assertEqual(report["scanned"], 0)
        self.assertEqual(engine.candidates()["total"], 0)

    async def test_a_mic_scope_matches_an_instrument_venue_name(self) -> None:
        market = MarketDataStore(":memory:", clock=lambda: NOW)
        _instrument(market, "XNSE:ALPHA", exchange="NSE")
        _bars(market)
        _quote(market)
        engine, _, _ = _engine(
            market,
            universe=UniverseFilter(asset_classes=("equity",), exchanges=("XNSE",)),
        )
        report = await engine.run_cycle()
        self.assertEqual(report["scanned"], 1)
        self.assertEqual(engine.candidates()["candidates"][0]["instrument_id"], ALPHA)

    async def test_a_configured_instrument_list_limits_one_cycle(self) -> None:
        market = MarketDataStore(":memory:", clock=lambda: NOW)
        _instrument(market, "XNSE:ALPHA")
        _bars(market)
        _quote(market)
        _instrument(market, "XNSE:BETA", name="Beta Ltd")
        _bars(market, "XNSE:BETA")
        _quote(market, "XNSE:BETA")
        engine, _, _ = _engine(market)
        report = await engine.run_cycle(
            force=True,
            universe=UniverseFilter(asset_classes=("equity",), exchanges=("XNSE",)),
            instrument_ids=("XNSE:ALPHA",),
        )
        self.assertEqual(report["scanned"], 1)
        self.assertEqual(
            [row["instrument_id"] for row in engine.candidates()["candidates"]],
            [ALPHA],
        )

    async def test_the_exchange_boundary_is_respected(self) -> None:
        market = MarketDataStore(":memory:", clock=lambda: NOW)
        _instrument(market, "XNSE:ALPHA")
        _instrument(market, "XBOM:BETA", exchange="XBOM")
        engine, _, _ = _engine(
            market, universe=UniverseFilter(asset_classes=("equity",), exchanges=("XNSE",))
        )
        report = await engine.run_cycle()
        self.assertEqual(report["scanned"], 1)
        self.assertEqual(
            engine.candidates()["candidates"][0]["instrument_id"], "XNSE:ALPHA"
        )

    async def test_a_second_cycle_does_not_duplicate_a_membership(self) -> None:
        engine, store, _ = self._seeded()
        await engine.run_cycle()
        first = store.metrics()["entries"]
        await engine.run_cycle(force=True)
        self.assertEqual(store.metrics()["entries"], first)

    async def test_two_immediate_cycles_are_throttled(self) -> None:
        engine, _, _ = self._seeded()
        await engine.run_cycle()
        second = await engine.run_cycle()
        self.assertEqual(second["status"], "throttled")
        self.assertGreater(second["retry_after_seconds"], 0)

    async def test_force_bypasses_the_throttle(self) -> None:
        engine, _, _ = self._seeded()
        await engine.run_cycle()
        forced = await engine.run_cycle(force=True)
        self.assertEqual(forced["status"], "ok")

    async def test_a_missing_data_source_is_reported_not_hidden(self) -> None:
        discovery_store = DiscoveryStore(":memory:", clock=lambda: NOW)
        engine = AssetDiscoveryEngine(
            discovery_store, market_data=None, clock=lambda: NOW
        )
        report = await engine.run_cycle()
        self.assertEqual(report["status"], "degraded")
        self.assertEqual(report["failed_sources"], 1)
        self.assertEqual(report["scanned"], 0)
        health = {row["source_id"]: row for row in discovery_store.sources()}
        self.assertEqual(health["market_data"]["status"], "error")

    async def test_an_unknown_instrument_is_a_typed_error(self) -> None:
        engine, _, _ = _engine()
        with self.assertRaises(DiscoveryNotFoundError):
            engine.evaluate("XNSE:UNKNOWN")

    async def test_an_evaluation_is_persisted_with_its_factor_breakdown(self) -> None:
        engine, store, _ = self._seeded()
        body = engine.evaluate(ALPHA)
        self.assertTrue(body["ok"])
        self.assertTrue(body["created"])
        stored = store.latest_evaluation(ALPHA)
        assert stored is not None
        self.assertEqual(stored["instrument_id"], ALPHA)
        self.assertTrue(stored["factors"])
        self.assertEqual(stored["methodology_version"], "nm05-score-1.0")


# --------------------------------------------------------------------------
# Watchlists and lifecycle
# --------------------------------------------------------------------------


class WatchlistTests(unittest.IsolatedAsyncioTestCase):
    async def test_creation_is_idempotent_and_custom(self) -> None:
        engine, store, _ = _engine()
        first = await engine.create_watchlist("Momentum", description="mine")
        second = await engine.create_watchlist("Momentum")
        self.assertEqual(first["watchlist_id"], second["watchlist_id"])
        self.assertTrue(first["custom"])
        self.assertEqual(len(engine.watchlists()["watchlists"]), 5)

    async def test_a_blank_name_is_refused(self) -> None:
        engine, _, _ = _engine()
        with self.assertRaises(DiscoveryValidationError):
            await engine.create_watchlist("   ")

    async def test_an_instrument_can_be_added_once(self) -> None:
        market = MarketDataStore(":memory:", clock=lambda: NOW)
        _instrument(market)
        _bars(market)
        _quote(market)
        engine, _, _ = _engine(market)
        watchlist = (await engine.create_watchlist("Focus"))["watchlist_id"]
        added = await engine.add_instrument(watchlist, ALPHA, reason="manual")
        self.assertEqual(added["status"], "active")
        with self.assertRaises(DiscoveryValidationError):
            await engine.add_instrument(watchlist, ALPHA)

    async def test_removal_keeps_history_and_a_readd_reinstates(self) -> None:
        market = MarketDataStore(":memory:", clock=lambda: NOW)
        _instrument(market)
        _bars(market)
        _quote(market)
        engine, store, _ = _engine(market)
        watchlist = (await engine.create_watchlist("Focus"))["watchlist_id"]
        await engine.add_instrument(watchlist, ALPHA)
        removed = await engine.remove_instrument(watchlist, ALPHA, reason="no longer relevant")
        self.assertEqual(removed["status"], "removed")
        history = engine.history(watchlist_id=watchlist)["history"]
        self.assertTrue(any(row["to_status"] == "removed" for row in history))
        reinstated = await engine.add_instrument(watchlist, ALPHA, reason="relevant again")
        self.assertEqual(reinstated["status"], "active")
        self.assertEqual(
            store.metrics()["entries"]["active"], 1, "one active membership only"
        )
        statuses = [row["to_status"] for row in engine.history(watchlist_id=watchlist)["history"]]
        self.assertIn("removed", statuses)
        self.assertIn("active", statuses)

    async def test_unknown_watchlists_and_members_are_typed_errors(self) -> None:
        engine, _, _ = _engine()
        with self.assertRaises(DiscoveryNotFoundError):
            engine.watchlist("wl_missing")
        with self.assertRaises(DiscoveryNotFoundError):
            await engine.remove_instrument("wl_missing", ALPHA)

    async def test_the_member_limit_is_enforced(self) -> None:
        market = MarketDataStore(":memory:", clock=lambda: NOW)
        _instrument(market, "XNSE:ALPHA")
        _instrument(market, "XNSE:BETA", name="Beta Ltd")
        _bars(market, "XNSE:ALPHA")
        _bars(market, "XNSE:BETA")
        _quote(market, "XNSE:ALPHA")
        _quote(market, "XNSE:BETA", price=90.0)
        policy = DiscoveryPolicy(max_entries_per_watchlist=1)
        engine, _, _ = _engine(market, policy=policy)
        watchlist = (await engine.create_watchlist("Tiny"))["watchlist_id"]
        await engine.add_instrument(watchlist, "XNSE:ALPHA")
        with self.assertRaises(DiscoveryValidationError):
            await engine.add_instrument(watchlist, "XNSE:BETA")

    async def test_members_are_ranked_by_score(self) -> None:
        market = MarketDataStore(":memory:", clock=lambda: NOW)
        _instrument(market, "XNSE:ALPHA")
        _instrument(market, "XNSE:BETA", name="Beta Ltd")
        _bars(market, "XNSE:ALPHA", (100.0, 110.0, 120.0))
        _bars(market, "XNSE:BETA", (100.0, 100.2, 100.3))
        _quote(market, "XNSE:ALPHA", price=120.0)
        _quote(market, "XNSE:BETA", price=100.3)
        engine, _, _ = _engine(market)
        watchlist = (await engine.create_watchlist("Mixed"))["watchlist_id"]
        await engine.add_instrument(watchlist, "XNSE:ALPHA")
        await engine.add_instrument(watchlist, "XNSE:BETA")
        members = engine.watchlist(watchlist)["members"]
        self.assertEqual(members[0]["instrument_id"], "XNSE:ALPHA")

    def test_removal_is_terminal_in_the_state_machine(self) -> None:
        with self.assertRaises(ValueError):
            ensure_entry_transition("removed", "active")
        self.assertEqual(
            ensure_entry_transition("active", "downgraded"),
            (EntryStatus.ACTIVE, EntryStatus.DOWNGRADED),
        )


# --------------------------------------------------------------------------
# Maintenance decisions
# --------------------------------------------------------------------------


class MaintenanceDecisionTests(unittest.IsolatedAsyncioTestCase):
    def _evaluation(self, score: float, quality: DataQuality = DataQuality.GOOD):
        """A real evaluation's digest/timestamp, with the score under test."""
        base = scoring_module.score_candidate(
            instrument_id=ALPHA,
            asset_class="equity",
            trading_status="open",
            bars=[scoring_module.BarPoint(c, 1_000_000.0, NOW) for c in (100.0, 105.0, 110.0)],
            quote_price=110.0,
            quote_observed_at=NOW,
            events=(),
            sector_events=(),
            discovery_reason=DiscoveryReason.CATALOGUE,
            now=NOW,
            policy=DiscoveryPolicy(),
        )
        return replace(base, score=score, data_quality=quality)

    def _entry(self, status: EntryStatus = EntryStatus.ACTIVE, *, score: float = 60.0, weak: int = 0):
        from novacontrol.novamarket.discovery import WatchlistEntry

        return WatchlistEntry(
            entry_id="wle_1",
            watchlist_id="wl_1",
            instrument_id=ALPHA,
            status=status,
            added_at=NOW,
            updated_at=NOW,
            reason="test",
            score=score,
            last_evaluated_at=NOW,
            below_threshold_count=weak,
        )

    def test_a_real_score_decline_downgrades(self) -> None:
        engine, _, _ = _engine()
        target, reason, _ = engine._next_status(
            self._entry(score=90.0), self._evaluation(30.0), {"trading_status": "open"}
        )
        self.assertEqual(target, EntryStatus.DOWNGRADED)
        self.assertIn("fell", reason)

    def test_a_recovered_score_returns_to_active(self) -> None:
        engine, _, _ = _engine()
        target, _, _ = engine._next_status(
            self._entry(EntryStatus.DOWNGRADED, score=30.0),
            self._evaluation(80.0),
            {"trading_status": "open"},
        )
        self.assertEqual(target, EntryStatus.ACTIVE)

    def test_a_weak_score_needs_repeated_evidence_before_removal(self) -> None:
        engine, _, _ = _engine(policy=DiscoveryPolicy(remove_after_cycles=2))
        weak = self._evaluation(5.0)
        first, _, weak_count = engine._next_status(
            self._entry(score=70.0), weak, {"trading_status": "open"}
        )
        self.assertEqual(first, EntryStatus.UNDER_REVIEW)
        self.assertEqual(weak_count, 1)
        second, reason, _ = engine._next_status(
            self._entry(EntryStatus.UNDER_REVIEW, score=5.0, weak=1),
            weak,
            {"trading_status": "open"},
        )
        self.assertEqual(second, EntryStatus.REMOVED)
        self.assertIn("below", reason)

    def test_stale_data_marks_stale_not_removed(self) -> None:
        engine, _, _ = _engine()
        stale = self._evaluation(30.0, DataQuality.STALE)
        target, reason, _ = engine._next_status(
            self._entry(), stale, {"trading_status": "open"}
        )
        self.assertEqual(target, EntryStatus.STALE)
        self.assertIn("stale", reason)

    def test_a_delisted_instrument_is_suspended(self) -> None:
        engine, _, _ = _engine()
        target, reason, _ = engine._next_status(
            self._entry(), self._evaluation(80.0), {"trading_status": "delisted"}
        )
        self.assertEqual(target, EntryStatus.SUSPENDED)
        self.assertIn("delisted", reason)

    async def test_maintenance_keeps_a_member_when_its_data_source_fails(self) -> None:
        market = MarketDataStore(":memory:", clock=lambda: NOW)
        _instrument(market)
        _bars(market)
        _quote(market)
        engine, store, seam = _engine(market)
        await engine.run_cycle()
        assert seam is not None
        seam.get_instrument = lambda instrument_id: (_ for _ in ()).throw(RuntimeError("down"))
        before = store.metrics()["entries"]["active"]
        self.assertGreaterEqual(before, 1)
        report = await engine.reevaluate()
        self.assertGreaterEqual(report["skipped"], 1)
        self.assertEqual(report["removed"], 0)
        self.assertEqual(store.metrics()["entries"]["active"], before)


# --------------------------------------------------------------------------
# NM-04 event integration
# --------------------------------------------------------------------------


async def _radar(body: str, *, reliability: SourceReliability, source_type: SourceType):
    definition = EventSourceDefinition(
        source_id="fixture_feed",
        name="Fixture feed",
        source_type=source_type,
        reliability=reliability,
        url="https://example.test/feed",
        feed_format=FeedFormat.RSS,
        publisher="Fixture Publisher",
        market="IN",
        description="fixture",
        enabled_default=False,
        verified_live=False,
    )
    store = RadarEventStore(clock=lambda: NOW)
    radar = MarketEventRadar(
        store,
        catalog=SourceCatalog((definition,)),
        instruments=lambda: (
            RadarInstrumentView(
                instrument_id=ALPHA,
                symbol="ALPHA",
                name="Alpha Energy Ltd",
                asset_class="equity",
                exchange="XNSE",
                market="IN",
                sectors=("Energy",),
            ),
        ),
        clock=lambda: NOW,
    )
    await radar.ingest(definition, parse_feed(body, definition), fetched_at=NOW)
    event = radar.search(EventQuery())['events'][0]
    return radar, event


def _rss(title: str, summary: str) -> str:
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title><item>'
        f"<title>{title}</title><description>{summary}</description>"
        "<link>https://example.test/1</link><guid>one</guid>"
        "<pubDate>Mon, 02 Nov 2026 05:30:00 GMT</pubDate>"
        "</item></channel></rss>"
    )


class EventIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _market(self) -> MarketDataStore:
        market = MarketDataStore(":memory:", clock=lambda: NOW)
        _instrument(market)
        _bars(market)
        _quote(market)
        return market

    async def test_an_official_company_event_drives_an_event_watchlist(self) -> None:
        radar, event = await _radar(
            _rss(
                "Alpha Energy Ltd reports quarterly earnings",
                "Alpha Energy Ltd reported a rise in quarterly profit and revenue.",
            ),
            reliability=SourceReliability.OFFICIAL,
            source_type=SourceType.COMPANY_IR,
        )
        self.assertEqual(event["verification"], "officially_confirmed")
        engine, store, _ = _engine(self._market(), radar=radar)
        result = await engine.consume_event(event["event_id"])
        if event["importance"] not in {"high", "critical"}:
            self.assertEqual(result["status"], "ignored")
            return
        self.assertEqual(result["status"], "evaluated")
        lists = {row["kind"]: row for row in engine.watchlists()["watchlists"]}
        members = engine.watchlist(lists["event_driven"]["watchlist_id"])["members"]
        self.assertEqual(members[0]["instrument_id"], ALPHA)
        self.assertEqual(members[0]["event_ids"], [event["event_id"]])

    async def test_an_unverified_event_never_adds_a_member(self) -> None:
        radar, event = await _radar(
            _rss(
                "Alpha Energy Ltd reports quarterly earnings",
                "Alpha Energy Ltd reported a rise in quarterly profit and revenue.",
            ),
            reliability=SourceReliability.SECONDARY,
            source_type=SourceType.SECONDARY,
        )
        engine, _, _ = _engine(self._market(), radar=radar)
        result = await engine.consume_event(event["event_id"])
        self.assertEqual(result["status"], "ignored")
        self.assertIn("single_source", result["reason"])
        lists = {row["kind"]: row for row in engine.watchlists()["watchlists"]}
        self.assertEqual(
            engine.watchlist(lists["event_driven"]["watchlist_id"])["total"], 0
        )

    async def test_a_sector_event_scores_the_sector_factor_only(self) -> None:
        """NM-04 supplies sector context; scoring must use it without treating it as direct."""
        engine, _, _ = _engine(self._market())
        body = engine.evaluate(ALPHA)
        without = next(f for f in body["factors"] if f["name"] == "sector_relevance")
        self.assertEqual(without["value"], 0.0)
        scored = scoring_module.score_candidate(
            instrument_id=ALPHA,
            asset_class="equity",
            trading_status="open",
            bars=[scoring_module.BarPoint(c, 1_000_000.0, NOW) for c in (100.0, 105.0, 110.0)],
            quote_price=110.0,
            quote_observed_at=NOW,
            events=(),
            sector_events=(
                EventSignal("evt_sector", "high", "officially_confirmed", "indirect", NOW),
            ),
            discovery_reason=DiscoveryReason.CATALOGUE,
            now=NOW,
            policy=DiscoveryPolicy(),
        )
        with_sector = next(f for f in scored.factors if f.name == "sector_relevance")
        self.assertGreater(with_sector.value, 0.0)
        direct = next(f for f in scored.factors if f.name == "event_relevance")
        self.assertEqual(direct.value, 0.0, "a sector event is not a direct instrument event")

    async def test_the_radar_is_required_for_event_ingestion(self) -> None:
        engine, _, _ = _engine(self._market())
        with self.assertRaises(DiscoveryNotFoundError):
            await engine.consume_event("evt_missing")


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidates_watchlists_and_history_survive_a_reopen(self) -> None:
        # A plain temp directory (not TemporaryDirectory): Windows can still hold the
        # sqlite handle for a moment after close(), and cleanup must not fail the test.
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        if True:
            path = Path(folder) / "discovery.sqlite3"
            market = MarketDataStore(":memory:", clock=lambda: NOW)
            _instrument(market)
            _bars(market)
            _quote(market)
            store = DiscoveryStore(path, clock=lambda: NOW)
            engine = AssetDiscoveryEngine(
                store,
                market_data=_Seam(market),
                policy=DiscoveryPolicy(add_threshold=40.0),
                clock=lambda: NOW,
            )
            await engine.run_cycle()
            member = next(
                row["watchlist_id"]
                for row in engine.watchlists()["watchlists"]
                if row["kind"] == "discovery"
            )
            await engine.remove_instrument(member, ALPHA, reason="cleanup")
            store.close()

            reopened = DiscoveryStore(path, clock=lambda: NOW)
            self.assertEqual(reopened.schema_version, 1)
            self.assertGreaterEqual(reopened.metrics()["evaluations"], 1)
            self.assertIsNotNone(reopened.candidate(ALPHA))
            history = reopened.history()
            self.assertTrue(any(row["to_status"] == "removed" for row in history))
            reopened.close()
            market.close()

    async def test_reevaluation_appends_a_new_record(self) -> None:
        market = MarketDataStore(":memory:", clock=lambda: NOW)
        _instrument(market)
        _bars(market)
        _quote(market)
        engine, store, _ = _engine(market)
        engine.evaluate(ALPHA)
        _quote(market, price=111.0, age_hours=2.0)
        engine.evaluate(ALPHA, reason=DiscoveryReason.ACTIVITY)
        records = store.evaluations(ALPHA)
        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0]["digest"], records[1]["digest"])

    async def test_the_evaluation_table_is_append_only(self) -> None:
        market = MarketDataStore(":memory:", clock=lambda: NOW)
        _instrument(market)
        _bars(market)
        _quote(market)
        engine, store, _ = _engine(market)
        engine.evaluate(ALPHA)
        with self.assertRaises(Exception):
            store._connection.execute(
                "UPDATE discovery_evaluations SET score = 0"
            )
        self.assertEqual(len(store.evaluations(ALPHA)), 1)


class _MutableClock:
    def __init__(self, moment: datetime = NOW) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


class DiscoveryAuditTests(unittest.IsolatedAsyncioTestCase):
    """Defects found while auditing NM-05 against its mandate."""

    def _market(self) -> MarketDataStore:
        market = MarketDataStore(":memory:", clock=lambda: NOW)
        _instrument(market)
        _bars(market)
        _quote(market)
        return market

    def _radar_fixture(self) -> tuple[Any, Any]:
        definition = EventSourceDefinition(
            source_id="audit_feed",
            name="Audit feed",
            source_type=SourceType.COMPANY_IR,
            reliability=SourceReliability.OFFICIAL,
            url="https://example.test/audit",
            feed_format=FeedFormat.RSS,
            publisher="Audit Publisher",
            market="IN",
            description="fixture",
            enabled_default=False,
            verified_live=False,
        )
        store = RadarEventStore(clock=lambda: NOW)
        radar = MarketEventRadar(
            store,
            catalog=SourceCatalog((definition,)),
            instruments=lambda: (
                RadarInstrumentView(
                    instrument_id=ALPHA,
                    symbol="ALPHA",
                    name="Alpha Energy Ltd",
                    asset_class="equity",
                    exchange="XNSE",
                    market="IN",
                    sectors=("Energy",),
                ),
            ),
            clock=lambda: NOW,
        )
        return radar, definition

    async def test_a_catalogue_failure_is_a_degraded_cycle(self) -> None:
        engine, store, seam = _engine(self._market())
        assert seam is not None

        def boom(**_kwargs: Any) -> Any:
            raise RuntimeError("catalogue down")

        seam.list_instruments = boom
        report = await engine.run_cycle(force=True)
        self.assertEqual(report["scanned"], 0)
        self.assertEqual(report["status"], "degraded")
        self.assertEqual(report["failed_sources"], 1)
        self.assertTrue(any("catalogue down" in line for line in report["errors"]))
        health = {row["source_id"]: row for row in store.sources()}
        self.assertEqual(health["instrument_catalogue"]["status"], "error")
        self.assertIn("catalogue down", health["instrument_catalogue"]["last_error"])

    async def test_reevaluation_with_unchanged_inputs_is_a_new_record(self) -> None:
        clock = _MutableClock()
        store = DiscoveryStore(":memory:", clock=clock)
        engine = AssetDiscoveryEngine(
            store, market_data=_Seam(self._market()), clock=clock
        )
        engine.evaluate(ALPHA)
        clock.moment = NOW + timedelta(minutes=5)
        engine.evaluate(ALPHA)
        records = store.evaluations(ALPHA)
        self.assertEqual(len(records), 2, "an unchanged reevaluation is still a record")
        self.assertEqual(records[0]["digest"], records[1]["digest"])

    async def test_maintenance_evaluates_each_instrument_once(self) -> None:
        engine, store, _ = _engine(self._market())
        await engine.run_cycle()
        members = len(store.active_entries())
        self.assertGreaterEqual(members, 2, "the fixture joins several lists")
        before = store.metrics()["evaluations"]
        report = await engine.reevaluate()
        self.assertEqual(store.metrics()["evaluations"] - before, 1)
        self.assertEqual(report["evaluated"], members)

    async def test_top_n_caps_automatic_membership(self) -> None:
        market = MarketDataStore(":memory:", clock=lambda: NOW)
        _instrument(market, "XNSE:ALPHA")
        _instrument(market, "XNSE:BETA", name="Beta Ltd")
        _bars(market, "XNSE:ALPHA", (100.0, 110.0, 120.0))
        _bars(market, "XNSE:BETA", (100.0, 104.0, 108.0))
        _quote(market, "XNSE:ALPHA", price=120.0)
        _quote(market, "XNSE:BETA", price=108.0)
        engine, _, _ = _engine(
            market, policy=DiscoveryPolicy(add_threshold=40.0, top_n=1)
        )
        await engine.run_cycle()
        lists = {row["kind"]: row for row in engine.watchlists()["watchlists"]}
        discovery = engine.watchlist(lists["discovery"]["watchlist_id"])
        self.assertEqual(discovery["total"], 1)
        self.assertEqual(discovery["members"][0]["instrument_id"], "XNSE:ALPHA")
        self.assertEqual(engine.candidates()["total"], 2, "both were evaluated")

    async def test_a_retracted_event_clears_membership_evidence(self) -> None:
        radar, definition = self._radar_fixture()
        await radar.ingest(
            definition,
            parse_feed(
                _rss(
                    "Alpha Energy Ltd reports quarterly earnings",
                    "Alpha Energy Ltd reported a rise in quarterly profit and revenue.",
                ),
                definition,
            ),
            fetched_at=NOW,
        )
        event = radar.search(EventQuery())["events"][0]
        self.assertEqual(event["verification"], "officially_confirmed")
        engine, _, _ = _engine(self._market(), radar=radar)
        await engine.consume_event(event["event_id"])
        lists = {row["kind"]: row for row in engine.watchlists()["watchlists"]}
        watchlist_id = lists["event_driven"]["watchlist_id"]
        self.assertEqual(engine.watchlist(watchlist_id)["total"], 1)

        await radar.ingest(
            definition,
            parse_feed(
                _rss(
                    "Alpha Energy Ltd withdraws its statement on quarterly earnings",
                    "Alpha Energy Ltd withdrew its earlier statement about quarterly earnings.",
                ),
                definition,
            ),
            fetched_at=NOW,
        )
        latest = radar.search(EventQuery(latest_only=True))["events"][0]
        self.assertEqual(latest["verification"], "retracted")
        result = await engine.consume_event(latest["event_id"])
        self.assertEqual(result["status"], "retracted")
        member = engine.watchlist(watchlist_id)["members"][0]
        self.assertEqual(member["event_ids"], [])
        self.assertTrue(member["status_reason"])

    async def test_a_market_event_can_be_forwarded_to_discovery(self) -> None:
        radar, definition = self._radar_fixture()
        await radar.ingest(
            definition,
            parse_feed(
                _rss(
                    "Alpha Energy Ltd reports quarterly earnings",
                    "Alpha Energy Ltd reported a rise in quarterly profit and revenue.",
                ),
                definition,
            ),
            fetched_at=NOW,
        )
        event_id = radar.search(EventQuery())["events"][0]["event_id"]
        engine, _, _ = _engine(self._market(), radar=radar)
        forwarded = await engine.forward_market_event(
            "novamarket.market_event.discovered", {"event_id": event_id}
        )
        self.assertTrue(forwarded["forwarded"])
        self.assertEqual(forwarded["status"], "evaluated")
        other = await engine.forward_market_event(
            "novamarket.market_data.ingested", {"event_id": event_id}
        )
        self.assertFalse(other["forwarded"])

    async def test_a_scheduled_cycle_waits_while_every_market_is_closed(self) -> None:
        engine, _, _ = _engine(
            self._market(),
            universe=UniverseFilter(asset_classes=("equity",), exchanges=("XNSE",)),
        )
        report = await engine.run_cycle(trigger=TriggerKind.SCHEDULED, force=True)
        self.assertEqual(report["status"], "market_closed")
        self.assertEqual(report["sessions"], {"XNSE": "closed"})
        self.assertEqual(engine.candidates()["total"], 0)

    async def test_status_exposes_recent_cycles_and_sessions(self) -> None:
        engine, _, _ = _engine(self._market())
        await engine.run_cycle()
        status = engine.status()
        self.assertEqual(len(status["recent_cycles"]), 1)
        self.assertEqual(status["recent_cycles"][0]["status"], "ok")
        self.assertIn("XNSE", status["sessions"])

    def test_a_declared_session_is_not_guessed(self) -> None:
        self.assertEqual(
            session_state("XNSE", datetime(2026, 11, 2, 4, 0, tzinfo=UTC)),
            SessionState.OPEN,
        )
        self.assertEqual(
            session_state("XNSE", datetime(2026, 11, 7, 4, 0, tzinfo=UTC)),
            SessionState.CLOSED,
        )
        self.assertEqual(session_state("XXXX", NOW), SessionState.UNKNOWN)
        self.assertEqual(sessions_for(["xnse", ""], NOW), {"XNSE": "closed"})


if __name__ == "__main__":
    unittest.main()
