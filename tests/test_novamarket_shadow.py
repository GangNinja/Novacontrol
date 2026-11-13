"""NM-12 Shadow Mode tests.

These tests exercise the real ShadowService pipeline with deterministic
observations.  The final integration test uses the real NM-02 MarketDataStore,
the real NM-09 catalog evaluator and the real NM-08 policy evaluator.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from novacontrol.novamarket.marketdata.models import (
    AssetClass,
    DataQuality,
    Instrument,
    OhlcvBar,
)
from novacontrol.novamarket.marketdata.store import MarketDataStore
from novacontrol.novamarket.service import NovaMarketService
from novacontrol.novamarket.shadow import (
    ShadowIsolationError,
    ShadowService,
    ShadowStateError,
    ShadowValidationError,
)
from novacontrol.novamarket.shadow.models import ShadowDecisionStatus, ShadowSessionStatus

NOW = datetime(2026, 1, 2, 10, 0, tzinfo=timezone.utc)
CLOSES = (110, 108, 106, 104, 102, 100, 98, 96, 94, 92, 90, 80, 95, 110)
POLICY = {
    "max_volatility_pct": "1000",
    "min_cash_fraction": "0",
    "max_invested_fraction": "1",
    "max_position_weight": "1",
    "max_sector_weight": "1",
    "min_liquidity_value": "0",
    "max_turnover": "2",
    "max_drawdown_pct": "100",
    "max_data_age_hours": 1000,
    "max_state_age_hours": 1000,
    "min_observations": 2,
    "max_aggregate_sector_weight": "1",
}
BASELINE = {
    "portfolio_id": "portfolio_1",
    "positions": [],
    "cash": "100000",
    "invested": "0",
    "total_value": "100000",
    "currency": "INR",
}


def _strategy() -> dict[str, object]:
    return {
        "strategy_id": "trend",
        "catalog_id": "trend_sma",
        "version": "v1",
        "config": {"fast_period": 2, "slow_period": 3},
    }


def _bars(
    closes: tuple[int, ...] = CLOSES,
    *,
    end: datetime = NOW,
    step_days: int = 1,
) -> list[dict[str, str]]:
    start = end - timedelta(days=len(closes) - 1)
    return [
        {
            "observed_at": (start + timedelta(days=index)).isoformat(),
            "close": str(close),
            "high": str(close + 1),
            "low": str(close - 1),
            "volume": "10000",
        }
        for index, close in enumerate(closes)
    ]


def _observation(
    instrument_id: str = "XNSE:ALPHA",
    *,
    price: str = "110",
    at: datetime = NOW,
    closes: tuple[int, ...] = CLOSES,
    freshness: str = "fresh",
    quality: str = "valid",
    bars: list[dict[str, str]] | None = None,
) -> dict[str, dict[str, object]]:
    return {
        instrument_id: {
            "price": price,
            "observed_at": at.isoformat(),
            "quality": quality,
            "freshness": freshness,
            "bars": _bars(closes, end=at) if bars is None else bars,
        }
    }


class _Selected:
    def __init__(self, price: object, at: datetime) -> None:
        self.price = Decimal(str(price))
        self.observed_at = at
        self.quality = "valid"
        self.freshness = "fresh"


class _Result:
    def __init__(self, price: object, at: datetime) -> None:
        self.selected = _Selected(price, at)
        self.usable = True


class _Bar:
    def __init__(self, close: object, at: datetime) -> None:
        self.close = Decimal(str(close))
        self.high = self.close + 1
        self.low = self.close - 1
        self.volume = Decimal("10000")
        self.observed_at = at
        self.partial = False


class _Page:
    def __init__(self, items: list[_Bar]) -> None:
        self.items = items


class _Market:
    def __init__(self, closes: tuple[int, ...] = CLOSES) -> None:
        self._closes = closes

    def _items(self) -> list[_Bar]:
        return [
            _Bar(close, NOW - timedelta(days=len(self._closes) - index))
            for index, close in enumerate(self._closes)
        ]

    def latest(self, instrument_id: str) -> _Result:
        return _Result(self._closes[-1], NOW)

    def bars(self, instrument_id: str, limit: int = 60, ascending: bool = True) -> _Page:
        return _Page(self._items())


class _RecordingMarket(_Market):
    """Records the provider filter each read is asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, object]] = []
        self.bar_calls: list[dict[str, object]] = []

    def latest(self, instrument_id: str, **kwargs: object) -> _Result:
        self.calls.append(dict(kwargs))
        return super().latest(instrument_id)

    def bars(
        self,
        instrument_id: str,
        limit: int = 60,
        ascending: bool = True,
        **kwargs: object,
    ) -> _Page:
        self.bar_calls.append(dict(kwargs))
        return super().bars(instrument_id, limit=limit, ascending=ascending)


class _StoreMarket:
    """A thin NM-02 read adapter; no fabricated prices or depth."""

    def __init__(self, store: MarketDataStore) -> None:
        self._store = store

    def latest(self, instrument_id: str) -> _Result:
        page = self._store.bars(
            instrument_id, interval="1d", limit=1, ascending=False
        )
        bar = page.items[0]
        return _Result(bar.close, bar.observed_at)

    def bars(self, instrument_id: str, limit: int = 60, ascending: bool = True):
        return self._store.bars(
            instrument_id, interval="1d", limit=limit, ascending=ascending
        )


class _ShadowCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def service(self, path: Path | None = None, **kwargs: object) -> ShadowService:
        service = ShadowService(
            path or Path(self._tmp.name) / "shadow.sqlite3",
            market_data=kwargs.pop("market_data", None),
            strategy_loader=kwargs.pop("strategy_loader", None),
            portfolio_loader=kwargs.pop("portfolio_loader", None),
            risk_policy_loader=kwargs.pop("risk_policy_loader", lambda version: POLICY),
            backtest_loader=kwargs.pop("backtest_loader", None),
            clock=lambda: NOW,
            min_sample_size=int(kwargs.pop("min_sample_size", 3)),
        )
        self.addAsyncCleanup(service.close)
        return service

    async def create(self, service: ShadowService, **overrides: object) -> dict:
        payload: dict[str, object] = {
            "name": "test shadow",
            "instruments": ["XNSE:ALPHA"],
            "strategies": [_strategy()],
            "baseline_snapshot": deepcopy(BASELINE),
            "auto_monitor": False,
        }
        payload.update(overrides)
        return (await service.create(payload))["session"]


class ShadowLifecycleTests(_ShadowCase):
    async def test_lifecycle_and_terminal_refusal(self) -> None:
        service = self.service()
        session = await self.create(service)
        session_id = session["session_id"]
        self.assertEqual(session["status"], "created")

        self.assertEqual(
            (await service.start(session_id))["session"]["status"], "running"
        )
        self.assertEqual(
            (await service.pause(session_id))["session"]["status"], "paused"
        )
        self.assertEqual(
            (await service.resume(session_id))["session"]["status"], "running"
        )
        self.assertEqual(
            (await service.stop(session_id))["session"]["status"], "stopped"
        )
        self.assertEqual(
            (await service.complete(session_id))["session"]["status"], "completed"
        )
        with self.assertRaises(ShadowStateError):
            await service.cancel(session_id)
        with self.assertRaises(ShadowStateError):
            await service.start(session_id)

    async def test_invalid_lifecycle_edges_raise(self) -> None:
        service = self.service()
        session = await self.create(service)
        with self.assertRaises(ShadowStateError):
            await service.pause(session["session_id"])
        with self.assertRaises(ShadowStateError):
            await service.resume(session["session_id"])

    async def test_baseline_is_captured_once(self) -> None:
        portfolio = {
            "holdings": [
                {
                    "instrument_id": "XNSE:ALPHA",
                    "quantity": "10",
                    "market_value": "1000",
                    "weight": "0.01",
                }
            ],
            "cash": "99000",
            "valuation": {"total_value": "100000"},
        }
        service = self.service(portfolio_loader=lambda portfolio_id: portfolio)
        session = await self.create(
            service, portfolio_id="portfolio_1", baseline_snapshot=None
        )
        started = await service.start(session["session_id"])
        snapshot = started["session"]["baseline"]
        self.assertEqual(snapshot["cash"], "99000.00")
        self.assertEqual(snapshot["snapshot_id"], session["baseline"] or snapshot["snapshot_id"])
        portfolio["cash"] = "0"
        loaded = service.get(session["session_id"])["session"]["baseline"]
        self.assertEqual(loaded["cash"], "99000.00")

    async def test_restart_recovery_pauses_a_running_session(self) -> None:
        path = Path(self._tmp.name) / "recovery.sqlite3"
        first = self.service(path=path)
        session = await self.create(first)
        await first.start(session["session_id"])
        await first.close()

        recovered = self.service(path=path)
        loaded = recovered.get(session["session_id"])["session"]
        self.assertEqual(loaded["status"], "paused")
        self.assertIn("restart", loaded["error"])

    async def test_configuration_is_validated(self) -> None:
        service = self.service()
        with self.assertRaises(ShadowValidationError):
            await service.create(
                {"instruments": [], "strategies": [_strategy()], "baseline_snapshot": BASELINE}
            )
        with self.assertRaises(ShadowValidationError):
            await service.create(
                {"instruments": ["XNSE:ALPHA"], "strategies": [], "baseline_snapshot": BASELINE}
            )
        with self.assertRaises(ShadowValidationError):
            await service.create(
                {
                    "instruments": ["XNSE:ALPHA"],
                    "strategies": [{"strategy_id": "bad", "catalog_id": "not_a_catalog"}],
                    "baseline_snapshot": BASELINE,
                }
            )


class ShadowPipelineTests(_ShadowCase):
    async def _buy_cycle(self, service: ShadowService, session_id: str):
        return await service.run_cycle(
            session_id, observations=_observation()
        )

    async def test_buy_signal_becomes_a_hypothetical_projection(self) -> None:
        service = self.service()
        session = await self.create(service)
        session_id = session["session_id"]
        await service.start(session_id)
        result = await self._buy_cycle(service, session_id)
        self.assertEqual(result["counts"]["decisions"], 1)
        self.assertEqual(result["counts"]["projections"], 1)

        decisions = service.decisions(session_id)["decisions"]
        self.assertEqual(len(decisions), 1)
        decision = decisions[0]
        self.assertEqual(decision["signal_type"], "buy")
        self.assertEqual(decision["status"], "projected")
        self.assertTrue(decision["risk"]["approved"])
        self.assertEqual(decision["risk"]["outcome"], "approved")
        self.assertEqual(decision["market_data_reference"]["data_source"], "explicit")
        self.assertFalse(decision["execution_permitted"])

        projections = service.projections(session_id)["projections"]
        self.assertEqual(len(projections), 1)
        projection = projections[0]
        self.assertEqual(projection["label"], "hypothetical order projection")
        self.assertTrue(projection["simulated"])
        self.assertIsNone(projection["execution_order_id"])
        self.assertFalse(projection["is_execution"])
        self.assertGreater(Decimal(projection["estimated_total_fees"]), 0)

    async def test_duplicate_observation_is_idempotent(self) -> None:
        service = self.service()
        session = await self.create(service)
        session_id = session["session_id"]
        await service.start(session_id)
        first = await self._buy_cycle(service, session_id)
        second = await self._buy_cycle(service, session_id)
        self.assertEqual(first["counts"]["decisions"], 1)
        self.assertEqual(second["counts"]["duplicates"], 1)
        self.assertEqual(second["counts"]["decisions"], 0)
        self.assertEqual(service.decisions(session_id)["total"], 1)
        self.assertEqual(service.projections(session_id)["total"], 1)

    async def test_stale_data_stops_new_decisions(self) -> None:
        service = self.service()
        session = await self.create(service)
        session_id = session["session_id"]
        await service.start(session_id)
        result = await service.run_cycle(
            session_id, observations=_observation(freshness="stale")
        )
        self.assertEqual(result["counts"]["data_failures"], 1)
        self.assertEqual(result["counts"]["decisions"], 0)
        errors = service.errors(session_id)["errors"]
        self.assertTrue(any(item["kind"] == "data_stale" for item in errors))

    async def test_failed_risk_evaluation_is_never_an_approval(self) -> None:
        def refuse(version: str):
            raise RuntimeError("policy store unavailable")

        service = self.service(risk_policy_loader=refuse)
        session = await self.create(service)
        session_id = session["session_id"]
        await service.start(session_id)
        result = await self._buy_cycle(service, session_id)
        self.assertEqual(result["counts"]["decisions"], 1)
        self.assertEqual(result["counts"]["projections"], 0)
        decision = service.decisions(session_id)["decisions"][0]
        self.assertEqual(decision["status"], "blocked")
        self.assertFalse(decision["risk"]["approved"])
        self.assertEqual(decision["risk"]["outcome"], "not_evaluated")
        self.assertTrue(
            any(item["kind"] == "risk_failed" for item in service.errors(session_id)["errors"])
        )

    async def test_risk_rejection_is_recorded_without_a_projection(self) -> None:
        strict = {**POLICY, "min_cash_fraction": "0.99"}
        service = self.service(risk_policy_loader=lambda version: strict)
        session = await self.create(service)
        session_id = session["session_id"]
        await service.start(session_id)
        await self._buy_cycle(service, session_id)
        decision = service.decisions(session_id)["decisions"][0]
        self.assertEqual(decision["status"], "risk_rejected")
        self.assertFalse(decision["risk"]["approved"])
        self.assertEqual(service.projections(session_id)["total"], 0)
        self.assertTrue(decision["risk"]["breaches"])

    async def test_multiple_instruments_are_evaluated(self) -> None:
        service = self.service()
        session = await self.create(
            service, instruments=["XNSE:ALPHA", "XNSE:BETA"]
        )
        session_id = session["session_id"]
        await service.start(session_id)
        observations = {
            **_observation("XNSE:ALPHA"),
            **_observation("XNSE:BETA", price="55", closes=tuple(value // 2 for value in CLOSES)),
        }
        await service.run_cycle(session_id, observations=observations)
        decisions = service.decisions(session_id)["decisions"]
        self.assertEqual(len(decisions), 2)
        self.assertEqual({item["instrument_id"] for item in decisions}, {"XNSE:ALPHA", "XNSE:BETA"})

    async def test_future_observations_and_bars_are_rejected(self) -> None:
        service = self.service()
        session = await self.create(service)
        session_id = session["session_id"]
        await service.start(session_id)
        future = NOW + timedelta(hours=1)
        result = await service.run_cycle(
            session_id, observations=_observation(at=future)
        )
        self.assertEqual(result["counts"]["data_failures"], 1)
        self.assertEqual(result["counts"]["decisions"], 0)

        second = await self.create(service)
        second_id = second["session_id"]
        await service.start(second_id)
        future_bar = {
            "observed_at": future.isoformat(),
            "close": "200",
            "high": "201",
            "low": "199",
            "volume": "10000",
        }
        await service.run_cycle(
            second_id,
            observations=_observation(bars=_bars() + [future_bar]),
        )
        decision = service.decisions(second_id)["decisions"][0]
        self.assertEqual(
            decision["market_data_reference"]["bar_count"], len(CLOSES)
        )

    async def test_strategy_loader_failure_is_structured(self) -> None:
        def fail(strategy_id: str):
            raise RuntimeError("strategy vanished")

        service = self.service(strategy_loader=fail)
        session = await self.create(service, strategies=["external"])
        session_id = session["session_id"]
        await service.start(session_id)
        result = await service.run_cycle(session_id, observations=_observation())
        self.assertEqual(result["counts"]["strategy_failures"], 1)
        self.assertEqual(result["counts"]["decisions"], 0)
        self.assertTrue(
            any(
                item["kind"] == "strategy_failed"
                for item in service.errors(session_id)["errors"]
            )
        )

    async def test_end_at_completes_the_session(self) -> None:
        service = self.service()
        session = await self.create(
            service, end_at=(NOW + timedelta(hours=1)).isoformat()
        )
        session_id = session["session_id"]
        await service.start(session_id)
        result = await service.run_cycle(
            session_id,
            now=NOW + timedelta(hours=2),
            observations=_observation(at=NOW + timedelta(hours=2)),
        )
        self.assertTrue(result["completed"])
        self.assertEqual(
            service.get(session_id)["session"]["status"], "completed"
        )

    async def test_real_nm02_store_drives_the_pipeline(self) -> None:
        store = MarketDataStore(Path(self._tmp.name) / "market.sqlite3", clock=lambda: NOW)
        self.addCleanup(store.close)
        store.upsert_instrument(
            Instrument.create(
                symbol="ALPHA",
                name="Alpha fixture",
                exchange="NSE",
                mic="XNSE",
                asset_class=AssetClass.EQUITY,
                currency="INR",
                timezone="Asia/Kolkata",
                provider_symbols={"fixture": "XNSE:ALPHA"},
            )
        )
        for index, close in enumerate(CLOSES):
            at = NOW - timedelta(days=len(CLOSES) - 1 - index)
            store.append_bar(
                OhlcvBar(
                    bar_id=f"bar_{index}",
                    instrument_id="XNSE:ALPHA",
                    provider="fixture",
                    interval="1d",
                    open=close,
                    high=close + 1,
                    low=close - 1,
                    close=close,
                    currency="INR",
                    observed_at=at,
                    retrieved_at=at,
                    source_timezone="Asia/Kolkata",
                    volume=Decimal("10000"),
                    quality=DataQuality.VALID,
                )
            )
        service = self.service(market_data=_StoreMarket(store))
        session = await self.create(service)
        session_id = session["session_id"]
        await service.start(session_id)
        result = await service.run_cycle(session_id)
        self.assertEqual(result["counts"]["decisions"], 1)
        decision = service.decisions(session_id)["decisions"][0]
        self.assertEqual(decision["market_data_reference"]["data_source"], "nm02")
        self.assertIn(decision["risk"]["outcome"], {"approved", "rejected", "restricted"})
        self.assertTrue(decision["risk_policy_version"])


class ShadowFreezeTests(_ShadowCase):
    async def test_start_freezes_the_strategy_definition(self) -> None:
        state = {"config": {"fast_period": 2, "slow_period": 3}}

        def loader(strategy_id: str) -> dict[str, object]:
            return {
                "strategy_id": strategy_id,
                "catalog_id": "trend_sma",
                "version": "v1",
                "status": "active",
                "config": dict(state["config"]),
            }

        service = self.service(strategy_loader=loader)
        session = await self.create(service, strategies=["external"])
        session_id = session["session_id"]
        started = await service.start(session_id)
        frozen = started["session"]["metadata"]["frozen_strategies"]["external"]
        self.assertEqual(frozen["config"], {"fast_period": 2, "slow_period": 3})
        self.assertEqual(frozen["version"], "v1")

        # A later strategy edit must not change the running session.
        state["config"] = {"fast_period": 50, "slow_period": 200}
        await service.run_cycle(session_id, observations=_observation())
        decision = service.decisions(session_id)["decisions"][0]
        self.assertEqual(decision["strategy_id"], "external")
        self.assertEqual(decision["strategy_version"], "v1")
        self.assertEqual(decision["signal_type"], "buy")

    async def test_start_freezes_the_risk_policy(self) -> None:
        policies = {"current": dict(POLICY)}
        service = self.service(
            risk_policy_loader=lambda version: policies["current"]
        )
        session = await self.create(service)
        session_id = session["session_id"]
        started = await service.start(session_id)
        self.assertIn("frozen_policy", started["session"]["metadata"])

        policies["current"] = {**POLICY, "min_cash_fraction": "0.99"}
        await service.run_cycle(session_id, observations=_observation())
        decision = service.decisions(session_id)["decisions"][0]
        self.assertEqual(decision["status"], "projected")
        self.assertTrue(decision["risk"]["approved"])


class ShadowMarketSourceTests(_ShadowCase):
    async def test_configured_provider_is_forwarded_and_the_default_is_unfiltered(
        self,
    ) -> None:
        market = _RecordingMarket()
        service = self.service(market_data=market)
        plain = await self.create(service)
        await service.start(plain["session_id"])
        await service.run_cycle(plain["session_id"])
        self.assertEqual(market.calls, [{}])
        self.assertEqual(market.bar_calls, [{}])
        decision = service.decisions(plain["session_id"])["decisions"][0]
        self.assertEqual(decision["market_data_reference"]["data_source"], "nm02")

        filtered = await self.create(service, market_data_source="fixture")
        await service.start(filtered["session_id"])
        await service.run_cycle(filtered["session_id"])
        self.assertEqual(market.calls[-1], {"provider": "fixture"})
        self.assertEqual(market.bar_calls[-1], {"provider": "fixture"})

    async def test_explicit_observations_do_not_use_the_provider(self) -> None:
        market = _RecordingMarket()
        service = self.service(market_data=market)
        session = await self.create(service, market_data_source="fixture")
        await service.start(session["session_id"])
        await service.run_cycle(session["session_id"], observations=_observation())
        self.assertEqual(market.calls, [])


class ShadowPolicyValidationTests(_ShadowCase):
    async def test_invalid_policy_blocks_instead_of_defaulting(self) -> None:
        service = self.service(
            risk_policy_loader=lambda version: {
                **POLICY,
                "max_position_weight": "not-a-number",
            }
        )
        session = await self.create(service)
        session_id = session["session_id"]
        await service.start(session_id)
        result = await service.run_cycle(session_id, observations=_observation())
        self.assertEqual(result["counts"]["decisions"], 1)
        self.assertEqual(result["counts"]["projections"], 0)
        decision = service.decisions(session_id)["decisions"][0]
        self.assertEqual(decision["status"], "blocked")
        self.assertFalse(decision["risk"]["approved"])
        self.assertEqual(decision["risk"]["outcome"], "not_evaluated")
        self.assertTrue(
            any(
                item["kind"] == "risk_failed"
                for item in service.errors(session_id)["errors"]
            )
        )


class ShadowOutcomeTests(_ShadowCase):
    async def _decide(self, service: ShadowService) -> str:
        session = await self.create(service)
        session_id = session["session_id"]
        await service.start(session_id)
        await service.run_cycle(session_id, observations=_observation())
        return session_id

    async def test_outcomes_use_only_later_observations(self) -> None:
        service = self.service()
        session_id = await self._decide(service)
        await service.track_outcomes(
            session_id, now=NOW, observations=_observation()
        )
        early = service.outcomes(session_id)["outcomes"]
        self.assertTrue(early)
        self.assertTrue(all(item["status"] == "unresolved" for item in early))

        later = NOW + timedelta(days=1)
        await service.track_outcomes(
            session_id,
            now=later,
            observations=_observation(
                price="120",
                at=later,
                bars=[
                    {
                        "observed_at": (NOW + timedelta(hours=1)).isoformat(),
                        "close": "115",
                        "high": "116",
                        "low": "108",
                        "volume": "10000",
                    }
                ],
            ),
        )
        by_interval = {item["interval"]: item for item in service.outcomes(session_id)["outcomes"]}
        self.assertEqual(by_interval[1]["status"], "resolved")
        self.assertGreater(Decimal(by_interval[1]["hypothetical_return_pct"]), 0)
        self.assertEqual(by_interval[5]["status"], "unresolved")
        self.assertEqual(by_interval[20]["status"], "unresolved")

    async def test_mfe_mae_and_analytics_small_sample(self) -> None:
        service = self.service()
        session_id = await self._decide(service)
        later = NOW + timedelta(days=1)
        await service.track_outcomes(
            session_id,
            now=later,
            observations=_observation(
                price="120",
                at=later,
                bars=[
                    {
                        "observed_at": (NOW + timedelta(hours=1)).isoformat(),
                        "close": "115",
                        "high": "116",
                        "low": "108",
                        "volume": "10000",
                    }
                ],
            ),
        )
        first = next(
            item for item in service.outcomes(session_id)["outcomes"] if item["interval"] == 1
        )
        self.assertGreater(Decimal(first["max_favorable_pct"]), 0)
        self.assertLess(Decimal(first["max_adverse_pct"]), 0)

        analytics = service.analytics(session_id)
        self.assertTrue(analytics["hypothetical"])
        self.assertFalse(analytics["outcomes"]["sample_sufficient"])
        self.assertIsNone(analytics["outcomes"]["win_rate_pct"])
        self.assertEqual(analytics["outcomes"]["minimum_sample"], 3)
        self.assertEqual(analytics["projections"]["total"], 1)


class ShadowIsolationTests(_ShadowCase):
    async def test_execution_guard_refuses_every_execution_path(self) -> None:
        service = self.service()
        guard = service.execution_guard
        for method in (
            guard.submit_order,
            guard.execute,
            guard.place_order,
            guard.process_order,
            guard.reserve_capital,
            guard.debit,
            guard.credit,
        ):
            with self.subTest(method=method.__name__):
                with self.assertRaises(ShadowIsolationError):
                    method()

        session = await self.create(service)
        session_id = session["session_id"]
        await service.start(session_id)
        await service.run_cycle(session_id, observations=_observation())
        projection = service.projections(session_id)["projections"][0]
        guard.assert_projection(service._store.projection(projection["projection_id"]))
        with self.assertRaises(ShadowIsolationError):
            guard.assert_projection(
                replace(
                    service._store.projection(projection["projection_id"]),
                    execution_order_id="ord_1",
                )
            )

    async def test_no_portfolio_or_execution_state_is_mutated(self) -> None:
        portfolio = deepcopy(BASELINE)
        service = self.service()
        session = await self.create(service, baseline_snapshot=portfolio)
        session_id = session["session_id"]
        await service.start(session_id)
        await service.run_cycle(session_id, observations=_observation())
        self.assertEqual(portfolio, BASELINE)
        self.assertFalse(hasattr(service, "execution"))
        self.assertFalse(hasattr(service, "_execution_engine"))
        for projection in service.projections(session_id)["projections"]:
            self.assertIsNone(projection["execution_order_id"])
            self.assertTrue(projection["simulated"])

    async def test_comparison_categorizes_disagreement(self) -> None:
        service = self.service()
        session = await self.create(service)
        session_id = session["session_id"]
        await service.start(session_id)
        await service.run_cycle(session_id, observations=_observation())
        result = await service.compare(
            session_id,
            {
                "reference": {
                    "signals": {"buy": 0},
                    "risk_outcomes": {"approved": 0},
                    "trades": 3,
                }
            },
        )
        comparison = result["comparison"]
        self.assertTrue(comparison["compatible"])
        self.assertIn("signal_disagreement", comparison["categories"])
        self.assertIn("risk_decision_disagreement", comparison["categories"])
        self.assertIn("execution_assumption_difference", comparison["categories"])
        self.assertTrue(service.comparisons(session_id)["total"])

    async def test_backtest_comparison_uses_a_compatible_reference(self) -> None:
        service = self.service(
            backtest_loader=lambda backtest_id: {
                "metrics": {"total_return": "0.02"}
            }
        )
        session = await self.create(service)
        session_id = session["session_id"]
        await service.start(session_id)
        await service.run_cycle(session_id, observations=_observation())
        later = NOW + timedelta(days=1)
        await service.track_outcomes(
            session_id,
            now=later,
            observations=_observation(
                price="120",
                at=later,
                bars=[
                    {
                        "observed_at": (NOW + timedelta(hours=1)).isoformat(),
                        "close": "115",
                        "high": "116",
                        "low": "108",
                        "volume": "10000",
                    }
                ],
            ),
        )
        result = await service.compare(session_id, {"backtest_id": "bt_1"})
        comparison = result["comparison"]
        self.assertTrue(comparison["compatible"])
        self.assertIn("timing_difference", comparison["categories"])
        self.assertIn("outcome_divergence", comparison["categories"])

    async def test_comparison_detects_data_quality_mismatch(self) -> None:
        service = self.service()
        session = await self.create(service)
        session_id = session["session_id"]
        await service.start(session_id)
        await service.run_cycle(session_id, observations=_observation())
        result = await service.compare(
            session_id, {"reference": {"data_quality": {"failures": 4}}}
        )
        comparison = result["comparison"]
        self.assertIn("data_mismatch", comparison["categories"])
        self.assertEqual(
            comparison["evidence"]["data_quality"]["shadow_failures"], 0
        )
        self.assertEqual(
            comparison["evidence"]["data_quality"]["reference_failures"], 4
        )

    async def test_comparison_without_reference_is_explicit(self) -> None:
        service = self.service()
        session = await self.create(service)
        result = await service.compare(session["session_id"], {})
        self.assertFalse(result["comparison"]["compatible"])
        self.assertTrue(result["comparison"]["note"])


class ShadowApiTests(_ShadowCase):
    """The HTTP surface is exercised in the API test module."""

    async def test_history_and_health_are_available(self) -> None:
        service = self.service()
        session = await self.create(service)
        session_id = session["session_id"]
        await service.start(session_id)
        await service.run_cycle(session_id, observations=_observation())
        history = service.history(session_id)
        self.assertEqual(history["counts"]["decisions"], 1)
        health = service.health(session_id)
        self.assertEqual(health["status"], "running")
        self.assertFalse(health["execution_connected"])
        self.assertTrue(service.status()["execution_connected"] is False)


class ShadowSampleSizeTests(_ShadowCase):
    async def test_small_sample_counts_distinct_decisions_not_intervals(self) -> None:
        service = self.service()
        session = await self.create(service)
        session_id = session["session_id"]
        await service.start(session_id)
        await service.run_cycle(session_id, observations=_observation())

        later = NOW + timedelta(days=21)
        bars = [
            {
                "observed_at": (NOW + timedelta(days=day)).isoformat(),
                "close": str(110 + day),
                "high": str(111 + day),
                "low": str(109 + day),
                "volume": "10000",
            }
            for day in range(1, 22)
        ]
        await service.track_outcomes(
            session_id,
            now=later,
            observations=_observation(price="131", at=later, bars=bars),
        )
        resolved = [
            item
            for item in service.outcomes(session_id)["outcomes"]
            if item["status"] == "resolved"
        ]
        self.assertGreaterEqual(len(resolved), 3)
        analytics = service.analytics(session_id)
        self.assertGreaterEqual(analytics["outcomes"]["resolved"], 3)
        self.assertEqual(analytics["outcomes"]["resolved_decisions"], 1)
        self.assertFalse(analytics["outcomes"]["sample_sufficient"])
        self.assertIsNone(analytics["outcomes"]["win_rate_pct"])


class ShadowImmutabilityTests(_ShadowCase):
    async def test_decisions_projections_and_comparisons_are_append_only(
        self,
    ) -> None:
        path = Path(self._tmp.name) / "immutable.sqlite3"
        service = self.service(path=path)
        session = await self.create(service)
        session_id = session["session_id"]
        await service.start(session_id)
        await service.run_cycle(session_id, observations=_observation())
        decision_id = service.decisions(session_id)["decisions"][0]["decision_id"]
        await service.compare(
            session_id, {"reference": {"signals": {"buy": 1}}}
        )

        with closing(sqlite3.connect(path)) as connection:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "UPDATE shadow_decisions SET payload = '{}' "
                    "WHERE decision_id = ?",
                    (decision_id,),
                )
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM shadow_projections")
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM shadow_comparisons")


class ShadowDatabaseIsolationTests(unittest.TestCase):
    def test_shadow_database_is_its_own_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = NovaMarketService(data_dir=tmp)
            shadow = Path(service._shadow_path())
            self.assertEqual(shadow.name, "novamarket_shadow.sqlite3")
            self.assertEqual(shadow.parent, Path(tmp))
            self.assertNotEqual(shadow, Path(service._radar_path()))

    def test_shadow_database_sits_beside_the_market_data_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            market = Path(tmp) / "market.sqlite3"
            service = NovaMarketService(market_data_path=market)
            shadow = Path(service._shadow_path())
            self.assertEqual(shadow.parent, market.parent)
            self.assertEqual(shadow.name, "novamarket_shadow.sqlite3")
            self.assertNotEqual(shadow, market)

    def test_shadow_database_is_in_memory_without_a_data_directory(self) -> None:
        service = NovaMarketService()
        self.assertEqual(service._shadow_path(), ":memory:")


if __name__ == "__main__":
    unittest.main()
