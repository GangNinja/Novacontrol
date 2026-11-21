"""PI-13 focused verification: isolated historical RL environment.

Fixtures are deterministic completed daily bars.  No network, broker, wallet,
portfolio or paper-execution store is used by the RL environment; the API test
uses a temporary application data directory and synthetic bar loader.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Sequence
from unittest import mock

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None  # type: ignore[assignment]

from novacontrol.novamarket.backtesting.core import HistoricalBar
from novacontrol.novamarket.rl import (
    ActionType,
    EpisodeStatus,
    ExecutionStatus,
    ObservationBuilder,
    RLAction,
    RLDataError,
    RLEnvironment,
    RLEnvironmentConfig,
    RLEpisodeError,
    RLInvalidActionError,
    RLService,
    RLStoreError,
    RLValidationError,
)
from novacontrol.novamarket.features.models import FeatureRequest
from novacontrol.novamarket.features.service import FeatureService
from novacontrol.novamarket.marketdata.models import DataInterval, OhlcvBar
from novacontrol.novamarket.rl.store import RLExperienceStore, make_episode


START = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)


def weekday_bars(count: int = 12, *, start: datetime = START) -> list[HistoricalBar]:
    bars: list[HistoricalBar] = []
    at = start
    opens = [100, 101, 102, 101, 103, 104, 103, 105, 106, 105, 107, 108, 109, 110]
    while len(bars) < count:
        if at.weekday() < 5:
            index = len(bars)
            open_price = Decimal(opens[index % len(opens)])
            close = open_price + (Decimal("1") if index % 4 else Decimal("-1"))
            bars.append(
                HistoricalBar(
                    instrument_id="XNSE:ALPHA",
                    observed_at=at,
                    open=open_price,
                    high=max(open_price, close) + Decimal("1"),
                    low=min(open_price, close) - Decimal("1"),
                    close=close,
                    volume=Decimal("100000"),
                    source="nm02",
                )
            )
        at += timedelta(days=1)
    return bars


def config_for(bars: Sequence[HistoricalBar], **changes: Any) -> RLEnvironmentConfig:
    data: dict[str, Any] = {
        "name": "PI-13 fixture",
        "dataset_id": "nm02-point-in-time",
        "dataset_version": "ds-v1",
        "instrument_id": "XNSE:ALPHA",
        "start": bars[0].observed_at,
        "end": bars[-1].observed_at + timedelta(days=1),
        "initial_capital": "100000.00",
        "max_steps": 50,
        "max_order_notional": "25000.00",
        "max_position_weight": "0.50",
    }
    data.update(changes)
    return RLEnvironmentConfig(**data)


def feature_snapshot(at: datetime, *, future: bool = False) -> dict[str, Any]:
    return {
        "feature_set_id": f"fs_{at.date().isoformat()}",
        "version": "pi04-feature-v1",
        "as_of": (at + (timedelta(days=2) if future else timedelta())).isoformat(),
        "values": {"rsi_14": 55.0, "macd": 0.5, "volatility_20": 0.02},
        "statuses": {"rsi_14": "present", "macd": "present", "volatility_20": "present"},
        "imputed": {},
        "quality": "valid",
        "completeness": 1.0,
    }


def forecast_snapshot(at: datetime) -> dict[str, Any]:
    return {
        "forecast_id": f"fc_{at.date().isoformat()}",
        "generated_at": at.isoformat(),
        "predicted_return": 0.02,
        "direction": "up",
        "p_up": 0.61,
        "p_down": 0.25,
        "probability_status": "calibrated",
        "lower_return": 0.0,
        "upper_return": 0.04,
        "uncertainty": "medium",
        "uncertainty_width": 0.04,
        "disagreement": 0.1,
        "horizon": "1d",
        "validity": "valid",
        "model_version": "ens-1",
        "feature_set_version": "pi04-feature-v1",
        "dataset_version": "ds-v1",
    }


class EnvironmentContractTests(unittest.TestCase):
    def test_environment_initialization_and_configuration(self) -> None:
        bars = weekday_bars()
        cfg = config_for(bars)
        env = RLEnvironment(cfg, bars)
        obs = env.reset("ep_contract")
        self.assertEqual(obs.instrument_id, "XNSE:ALPHA")
        self.assertEqual(obs.timestamp, bars[0].observed_at)
        self.assertEqual(env.portfolio.cash, Decimal("100000.00"))
        self.assertEqual(env.status, EpisodeStatus.RUNNING)
        self.assertEqual(obs.provenance["dataset_version"], "ds-v1")
        self.assertTrue(obs.to_dict()["simulated"])

    def test_invalid_configuration_is_refused(self) -> None:
        bars = weekday_bars()
        with self.assertRaises(RLValidationError):
            config_for(bars, initial_capital="0")
        with self.assertRaises(RLValidationError):
            config_for(bars, max_position_weight="2")
        with self.assertRaises(RLValidationError):
            config_for(bars, max_gross_exposure="2")
        with self.assertRaises(RLValidationError):
            RLEnvironmentConfig.from_mapping(
                {**config_for(bars).to_dict(), "unexpected": True}
            )
        with self.assertRaises(RLValidationError):
            RLEnvironment(config_for(bars), bars).reset("")

    def test_observation_is_timestamped_and_feature_versioned(self) -> None:
        bars = weekday_bars()
        cfg = config_for(bars)
        features = {bar.observed_at: feature_snapshot(bar.observed_at) for bar in bars}
        forecasts = {bar.observed_at: forecast_snapshot(bar.observed_at) for bar in bars}
        env = RLEnvironment(cfg, bars, feature_snapshots=features, forecast_snapshots=forecasts)
        obs = env.reset("ep_obs")
        self.assertEqual(obs.quality["status"], "valid")
        self.assertEqual(obs.provenance["feature_set_version"], "pi04-feature-v1")
        self.assertEqual(obs.features["values"]["rsi_14"], 55.0)
        self.assertEqual(obs.forecasts["direction"], "up")
        self.assertEqual(obs.market["close"], 99.0)
        self.assertEqual(obs.timestamp.tzinfo, UTC)

    def test_future_feature_and_forecast_are_withheld(self) -> None:
        bars = weekday_bars()
        cfg = config_for(bars)
        features = {
            bar.observed_at: feature_snapshot(bar.observed_at, future=True)
            for bar in bars
        }
        forecasts = {
            bar.observed_at: {**forecast_snapshot(bar.observed_at), "generated_at": (bar.observed_at + timedelta(days=1)).isoformat()}
            for bar in bars
        }
        env = RLEnvironment(cfg, bars, feature_snapshots=features, forecast_snapshots=forecasts)
        obs = env.reset("ep_leak")
        self.assertEqual(obs.features["values"], {})
        self.assertEqual(obs.forecasts["available"], False)
        self.assertIn("features.as_of", obs.quality["future_leak_blocked"])
        self.assertIn("forecasts.generated_at", obs.quality["future_leak_blocked"])

    def test_observation_rejects_history_unavailable_at_the_decision(self) -> None:
        bars = weekday_bars(4)
        cfg = config_for(bars)
        env = RLEnvironment(cfg, bars)
        env.reset("ep_unavailable")
        unavailable = replace(
            bars[0], available_at=bars[0].observed_at + timedelta(hours=1)
        )
        with self.assertRaises(RLDataError):
            ObservationBuilder(cfg).build(
                environment_id=cfg.environment_id,
                episode_id="ep_unavailable",
                timestamp=bars[0].observed_at,
                bar=bars[0],
                history=[unavailable],
                portfolio=env.portfolio,
                feature_snapshot=None,
                forecast_snapshot=None,
            )

    def test_invalid_forecast_is_invalid_not_stale(self) -> None:
        bars = weekday_bars()
        snapshot = {**forecast_snapshot(bars[0].observed_at), "validity": "invalid"}
        env = RLEnvironment(
            config_for(bars), bars, forecast_snapshots={bars[0].observed_at: snapshot}
        )
        obs = env.reset("ep_forecast_invalid")
        self.assertEqual(obs.quality["status"], "invalid")
        self.assertIn("forecasts.validity", obs.quality["invalid"])
        self.assertFalse(obs.forecasts["available"])

    def test_pi01_forecast_fields_are_normalised_for_replay(self) -> None:
        bars = weekday_bars()
        at = bars[0].observed_at
        snapshot = {
            "forecast_id": "sft_replay",
            "forecast_generation_timestamp": at.isoformat(),
            "forecast_target_timestamp": (at + timedelta(days=1)).isoformat(),
            "forecast_validity": "stale",
            "predicted_return": 0.02,
            "predicted_direction": "up",
            "forecast_uncertainty": "medium",
            "forecast_horizon": "1d",
            "p_up": None,
            "probability_status": "unavailable",
            "ensemble_disagreement": 0.1,
        }
        env = RLEnvironment(
            config_for(bars), bars, forecast_snapshots={at: snapshot}
        )
        obs = env.reset("ep_pi01_forecast")
        # The row was generated from old historical data, but at replay time it
        # is fresh; wall-clock staleness from PI-01 must not hide it.
        self.assertTrue(obs.forecasts["available"])
        self.assertEqual(obs.forecasts["validity"], "valid")
        self.assertEqual(obs.forecasts["status"], "valid")
        self.assertEqual(obs.forecasts["direction"], "up")
        self.assertEqual(obs.forecasts["horizon"], "1d")
        self.assertEqual(obs.forecasts["target_at"], snapshot["forecast_target_timestamp"])
        self.assertEqual(obs.forecasts["age_seconds"], 0.0)
        self.assertEqual(obs.forecasts["disagreement"], 0.1)

    def test_forecast_outside_the_replay_window_is_stale(self) -> None:
        bars = weekday_bars()
        at = bars[0].observed_at
        snapshot = {
            **forecast_snapshot(at),
            "generated_at": (at - timedelta(days=5)).isoformat(),
            "validity": "valid",
        }
        env = RLEnvironment(
            config_for(bars), bars, forecast_snapshots={at: snapshot}
        )
        obs = env.reset("ep_forecast_aged")
        self.assertFalse(obs.forecasts["available"])
        self.assertEqual(obs.forecasts["validity"], "stale")
        self.assertIn("forecasts.age_seconds", obs.quality["stale"])

    def test_stale_feature_makes_observation_partial(self) -> None:
        bars = weekday_bars()
        snapshot = feature_snapshot(bars[0].observed_at)
        snapshot["statuses"] = {
            "rsi_14": "stale",
            "macd": "present",
            "volatility_20": "present",
        }
        env = RLEnvironment(
            config_for(bars), bars, feature_snapshots={bars[0].observed_at: snapshot}
        )
        obs = env.reset("ep_feature_stale")
        self.assertEqual(obs.quality["status"], "partial")
        self.assertIn("features.rsi_14", obs.quality["stale"])

    def test_actual_pi04_feature_service_feeds_the_observation(self) -> None:
        bars = weekday_bars(60)
        instrument = SimpleNamespace(
            instrument_id="XNSE:ALPHA",
            mic="XNSE",
            exchange="NSE",
            currency="INR",
            timezone="Asia/Kolkata",
            asset_class=SimpleNamespace(value="equity"),
            metadata={},
        )
        market = SimpleNamespace(get_instrument=lambda instrument_id: instrument)
        features = FeatureService(market_data=market)
        try:
            ohlcv = [
                OhlcvBar.create(
                    instrument_id="XNSE:ALPHA",
                    provider="nm02",
                    interval=DataInterval.DAY_1,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    currency="INR",
                    observed_at=bar.observed_at,
                    retrieved_at=bar.retrieved_at or bar.observed_at,
                    source_timezone="Asia/Kolkata",
                    volume=bar.volume,
                )
                for bar in bars
            ]
            cutoff = bars[40]
            result = features.generate_sync(
                FeatureRequest(
                    instrument_id="XNSE:ALPHA",
                    as_of=cutoff.observed_at,
                    resolution="1d",
                    profile="swing",
                    exchange="XNSE",
                    include_context=False,
                    include_fundamentals=False,
                    include_news=False,
                    dataset_id="nm02-point-in-time",
                    dataset_version="ds-v1",
                    max_bars=41,
                ),
                bars={"1d": ohlcv[:41]},
            )
        finally:
            features.close()
        snapshot = result.feature_set.to_dict()
        self.assertGreater(len(snapshot["values"]), 20)
        cfg = config_for(bars[40:])
        env = RLEnvironment(
            cfg, bars[40:], feature_snapshots={bars[40].observed_at: snapshot}
        )
        obs = env.reset("ep_pi04")
        self.assertTrue(obs.features["available"])
        self.assertGreater(len(obs.features["values"]), 20)
        self.assertEqual(
            obs.provenance["feature_set_version"], result.feature_set.version
        )
        self.assertTrue(obs.features["statuses"])

    def test_rl_feature_loader_ignores_export_retrieval_time(self) -> None:
        from novacontrol.novamarket.service import NovaMarketService

        bars = weekday_bars(35)
        captured: dict[str, Any] = {}

        class FakeFeatures:
            def generate_sync(self, request: Any, *, bars: Any) -> Any:
                captured["bars"] = bars
                return SimpleNamespace(
                    feature_set=SimpleNamespace(
                        to_dict=lambda: {"values": {"rsi_14": 50.0}}
                    )
                )

        service = NovaMarketService()
        at = bars[-1].observed_at
        with mock.patch.object(service, "feature_service", return_value=FakeFeatures()):
            snapshot = service._rl_feature_snapshot(config_for(bars), at, bars)
        self.assertEqual(snapshot["values"]["rsi_14"], 50.0)
        rows = captured["bars"]["1d"]
        self.assertEqual(len(rows), 35)
        # The dataset's export time is later than the replay cutoff; membership
        # itself is the point-in-time provenance.
        self.assertTrue(all(row.retrieved_at == row.observed_at for row in rows))

    def test_missing_invalid_zero_and_imputed_are_distinct(self) -> None:
        bars = weekday_bars()
        cfg = config_for(bars)
        snap = feature_snapshot(bars[0].observed_at)
        snap["values"] = {"zero": 0.0, "missing": None, "bad": float("nan"), "filled": 3.0}
        snap["statuses"] = {"zero": "present", "missing": "missing", "bad": "invalid", "filled": "imputed"}
        snap["imputed"] = {"filled": 3.0}
        env = RLEnvironment(cfg, bars, feature_snapshots={bars[0].observed_at: snap})
        obs = env.reset("ep_states")
        self.assertEqual(obs.features["values"]["zero"], 0.0)
        self.assertIsNone(obs.features["values"]["missing"])
        self.assertIsNone(obs.features["values"]["bad"])
        self.assertEqual(obs.features["values"]["filled"], 3.0)
        self.assertIn("features.missing", obs.quality["missing"])
        self.assertIn("features.bad", obs.quality["invalid"])
        self.assertIn("features.filled", obs.quality["imputed"])


class ActionAndTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bars = weekday_bars()
        self.cfg = config_for(self.bars)
        self.env = RLEnvironment(self.cfg, self.bars)
        self.env.reset("ep_actions")

    def test_buy_fills_at_next_bar_open_with_costs(self) -> None:
        _, _, terminated, truncated, transition = self.env.step(
            {"action_type": "buy", "instrument_id": "XNSE:ALPHA", "notional": "10000"}
        )
        self.assertFalse(terminated)
        self.assertFalse(truncated)
        self.assertEqual(transition.execution["status"], "filled")
        self.assertEqual(transition.execution["at"], self.bars[1].observed_at.isoformat())
        self.assertGreaterEqual(
            Decimal(transition.execution["execution_price"]), self.bars[1].open
        )
        self.assertGreater(Decimal(transition.execution["fees"]), Decimal("0"))
        self.assertGreater(self.env.portfolio.position.quantity, 0)
        self.assertEqual(transition.next_observation["timestamp"], self.bars[1].observed_at.isoformat())

    def test_sell_updates_realized_pnl_and_never_shorts(self) -> None:
        self.env.step({"action_type": "buy", "instrument_id": "XNSE:ALPHA", "notional": "10000"})
        _, _, _, _, transition = self.env.step(
            {"action_type": "sell", "instrument_id": "XNSE:ALPHA"}
        )
        self.assertEqual(transition.execution["status"], "filled")
        self.assertEqual(self.env.portfolio.position, None)
        self.assertGreater(self.env.portfolio.realized_pnl, Decimal("0"))
        with self.assertRaises(RLInvalidActionError):
            self.env.step({"action_type": "sell", "instrument_id": "XNSE:ALPHA"})

    def test_hold_wait_and_no_action_leave_book_unchanged(self) -> None:
        for action in ("hold", "wait", "no_action"):
            with self.subTest(action=action):
                before = self.env.portfolio.to_dict()
                _, _, _, _, transition = self.env.step({"action_type": action})
                self.assertEqual(transition.execution["status"], ExecutionStatus.NOT_APPLICABLE.value)
                self.assertEqual(self.env.portfolio.cash, Decimal(before["cash"]))
                self.assertEqual(transition.reward["components"]["traded"], False)

    def test_action_space_rejects_short_and_oversized_sizing(self) -> None:
        with self.assertRaises(RLInvalidActionError):
            self.env.step({"action_type": "sell", "instrument_id": "XNSE:ALPHA", "quantity": "1"})
        with self.assertRaises(RLInvalidActionError):
            self.env.step(
                {"action_type": "buy", "instrument_id": "XNSE:ALPHA", "notional": "999999"}
            )
        with self.assertRaises(RLInvalidActionError):
            self.env.step({"action_type": "buy", "instrument_id": "XNSE:OTHER", "notional": "100"})
        with self.assertRaises(RLInvalidActionError):
            self.env.step({"action_type": "buy", "instrument_id": "XNSE:ALPHA", "quantity": "1", "notional": "100"})
        with self.assertRaises(RLValidationError):
            self.env.step({"action_type": "short", "instrument_id": "XNSE:ALPHA"})
        self.assertEqual(self.env.invalid_actions, 5)
        self.assertEqual(self.env.steps, 0)

    def test_gross_exposure_bound_caps_a_buy(self) -> None:
        bars = weekday_bars(8)
        env = RLEnvironment(
            config_for(
                bars,
                initial_capital="100000",
                max_position_weight="0.25",
                max_gross_exposure="0.05",
                max_order_notional="25000",
            ),
            bars,
        )
        env.reset("ep_exposure")
        _, _, _, _, transition = env.step(
            {"action_type": "buy", "instrument_id": "XNSE:ALPHA", "notional": "10000"}
        )
        self.assertEqual(transition.execution["status"], ExecutionStatus.REJECTED.value)
        self.assertEqual(transition.reward["components"]["unnecessary_trading"], 1)
        self.assertEqual(env.portfolio.position.quantity if env.portfolio.position else 0, 0)

    def test_transition_contract_and_reward_interface(self) -> None:
        _, reward, _, _, transition = self.env.step({"action_type": "hold"})
        payload = transition.to_dict()
        for key in (
            "transition_id", "environment_id", "episode_id", "sequence",
            "timestamp", "observation", "action", "execution", "reward",
            "next_observation", "terminated", "truncated", "dataset_version",
            "feature_set_version", "environment_version", "simulation_config_hash",
        ):
            self.assertIn(key, payload)
        self.assertEqual(reward.total, Decimal("0"))
        self.assertTrue(reward.interface_only)
        self.assertFalse(reward.authoritative_formula)
        self.assertIn("realized_pnl_delta", reward.components)
        self.assertIn("transaction_cost", reward.components)
        self.assertIn("forecast_direction", reward.components)
        self.assertIn("forecast_validity", reward.components)

    def test_historical_advancement_and_calendar(self) -> None:
        bars = list(self.bars)
        sunday = START + timedelta(days=5)  # Sunday 2026-01-11, after this window
        bars.append(
            HistoricalBar(
                "XNSE:ALPHA", sunday, Decimal("100"), Decimal("101"),
                Decimal("99"), Decimal("100"), Decimal("1000"),
            )
        )
        cfg = config_for(bars)
        env = RLEnvironment(cfg, bars)
        obs = env.reset("ep_calendar")
        self.assertEqual(obs.timestamp, self.bars[0].observed_at)
        self.assertLess(env.timeline_length, len(bars))
        self.assertTrue(any("closed-session" in item for item in env.warnings))
        _, _, _, _, transition = env.step({"action_type": "hold"})
        self.assertGreater(
            datetime.fromisoformat(transition.next_observation["timestamp"]),
            datetime.fromisoformat(transition.observation["timestamp"]),
        )

    def test_timeout_truncation_is_distinct_from_termination(self) -> None:
        bars = weekday_bars(8)
        env = RLEnvironment(config_for(bars, max_steps=2), bars)
        env.reset("ep_trunc")
        _, _, terminated, truncated, _ = env.step({"action_type": "hold"})
        self.assertFalse(terminated)
        self.assertFalse(truncated)
        _, _, terminated, truncated, _ = env.step({"action_type": "hold"})
        self.assertFalse(terminated)
        self.assertTrue(truncated)
        self.assertEqual(env.status, EpisodeStatus.TRUNCATED)
        self.assertEqual(env.summary()["termination_reason"], "step_limit")

    def test_partial_fill_is_bounded_by_next_bar_liquidity(self) -> None:
        bars = weekday_bars(6)
        # The next bar offers only 10 units at a 10% participation rate.
        bars[1] = HistoricalBar(
            "XNSE:ALPHA",
            bars[1].observed_at,
            bars[1].open,
            bars[1].high,
            bars[1].low,
            bars[1].close,
            Decimal("100"),
        )
        cfg = config_for(
            bars,
            execution_config={"participation_rate": "0.10"},
            max_order_notional="25000",
        )
        env = RLEnvironment(cfg, bars)
        env.reset("ep_partial")
        _, _, _, _, transition = env.step(
            {"action_type": "buy", "instrument_id": "XNSE:ALPHA", "quantity": "100"}
        )
        self.assertEqual(transition.execution["status"], "partial")
        self.assertEqual(transition.execution["quantity"], "10.000000")
        self.assertEqual(env.portfolio.position.quantity, Decimal("10.000000"))

    def test_end_of_history_terminates_and_never_continues(self) -> None:
        bars = weekday_bars(2)
        env = RLEnvironment(config_for(bars), bars)
        env.reset("ep_end")
        _, _, terminated, truncated, transition = env.step({"action_type": "hold"})
        self.assertTrue(terminated)
        self.assertFalse(truncated)
        self.assertEqual(env.status, EpisodeStatus.TERMINATED)
        self.assertTrue(env.summary()["complete"])
        with self.assertRaises(RLEpisodeError):
            env.step({"action_type": "hold"})

    def test_episode_reset_clears_simulated_state_and_history(self) -> None:
        self.env.step({"action_type": "buy", "instrument_id": "XNSE:ALPHA", "notional": "5000"})
        self.assertGreater(self.env.sequence, 0)
        obs = self.env.reset()
        self.assertEqual(obs.timestamp, self.bars[0].observed_at)
        self.assertEqual(self.env.sequence, 0)
        self.assertEqual(self.env.portfolio.cash, Decimal("100000.00"))
        self.assertIsNone(self.env.portfolio.position)

    def test_two_environments_are_isolated_and_reproducible(self) -> None:
        def run(episode_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
            env = RLEnvironment(self.cfg, self.bars)
            env.reset(episode_id)
            _, _, _, _, first = env.step({"action_type": "buy", "instrument_id": "XNSE:ALPHA", "notional": "10000"})
            _, _, _, _, second = env.step({"action_type": "hold"})
            return first.to_dict(), env.portfolio.to_dict()

        a = run("ep_same")
        b = run("ep_same")
        self.assertEqual(a, b)
        isolated = RLEnvironment(self.cfg, self.bars)
        isolated.reset("ep_other")
        isolated.step({"action_type": "buy", "instrument_id": "XNSE:ALPHA", "notional": "5000"})
        c = isolated.step({"action_type": "hold"})[4].to_dict()
        self.assertNotEqual(a[0]["portfolio_after"], c["portfolio_after"])


class ServiceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self._tmp.name) / "novamarket_rl.sqlite3"
        self.bars = weekday_bars(10)
        self.services: list[RLService] = []

    def tearDown(self) -> None:
        for service in self.services:
            service.close()
        self._tmp.cleanup()

    def _service(self, **kwargs: Any) -> RLService:
        service = RLService(self.path, bar_loader=lambda cfg: self.bars, **kwargs)
        self.services.append(service)
        return service

    def _start(self, service: RLService, **config: Any) -> tuple[str, str]:
        saved = service.create_environment(
            {
                "name": "service fixture",
                "dataset_id": "nm02-point-in-time",
                "dataset_version": "ds-v1",
                "instrument_id": "XNSE:ALPHA",
                "start": self.bars[0].observed_at,
                "end": self.bars[-1].observed_at + timedelta(days=1),
                "initial_capital": "100000",
                "max_steps": 50,
                **config,
            }
        )
        started = service.start_episode(saved["environment_id"])
        return saved["environment_id"], started["episode"]["episode_id"]

    def test_experience_recording_grouping_and_provenance(self) -> None:
        service = self._service()
        env_id, episode_id = self._start(service)
        service.step(episode_id, {"action_type": "buy", "instrument_id": "XNSE:ALPHA", "notional": "1000"})
        service.step(episode_id, {"action_type": "hold"})
        body = service.experiences(episode_id)
        self.assertEqual(body["total"], 2)
        self.assertEqual([row["sequence"] for row in body["experiences"]], [1, 2])
        row = body["experiences"][0]
        self.assertEqual(row["dataset_version"], "ds-v1")
        self.assertEqual(row["environment_version"], "pi13-rl-env-1.0")
        self.assertEqual(row["simulation_config_hash"], service.environment(env_id)["config_hash"])
        self.assertFalse(row["observation"]["production_book"])
        self.assertTrue(body["bounded_storage"])

    def test_persistence_and_recovery_after_restart(self) -> None:
        service = self._service()
        _, episode_id = self._start(service)
        service.step(episode_id, {"action_type": "buy", "instrument_id": "XNSE:ALPHA", "notional": "1000"})
        service.close()
        self.services.remove(service)
        restarted = self._service()
        result = restarted.step(episode_id, {"action_type": "sell", "instrument_id": "XNSE:ALPHA"})
        self.assertEqual(result["step_count"], 2)
        self.assertEqual(restarted.experiences(episode_id)["total"], 2)
        self.assertEqual(restarted.episode(episode_id)["status"], "running")

    def test_observation_failure_restores_pre_step_state(self) -> None:
        service = self._service()
        _, episode_id = self._start(service)
        original_build = ObservationBuilder.build
        failures = {"remaining": 1}

        def flaky_build(builder: ObservationBuilder, **kwargs: Any) -> Any:
            if failures["remaining"]:
                failures["remaining"] -= 1
                raise RuntimeError("observation exploded")
            return original_build(builder, **kwargs)

        with mock.patch.object(ObservationBuilder, "build", new=flaky_build):
            with self.assertRaisesRegex(RuntimeError, "observation exploded"):
                service.step(episode_id, {"action_type": "hold"})
        after = service.episode(episode_id)
        self.assertEqual(after["status"], "failed")
        self.assertIn("RuntimeError: observation exploded", after["error"])
        self.assertEqual(after["state"]["step_count"], 0)
        self.assertEqual(after["state"]["transition_count"], 0)
        self.assertEqual(
            after["state"]["current_timestamp"],
            self.bars[0].observed_at.isoformat(),
        )
        self.assertEqual(after["state"]["snapshot"]["index"], 0)
        self.assertEqual(service.experiences(episode_id)["total"], 0)

    def test_corrupted_recovery_is_refused_without_touching_other_episode(self) -> None:
        service = self._service()
        _, first = self._start(service)
        _, second = self._start(service)
        service.step(first, {"action_type": "hold"})
        service.step(second, {"action_type": "hold"})
        service.close()
        self.services.remove(service)
        connection = sqlite3.connect(self.path)
        try:
            row = connection.execute(
                "SELECT payload_json FROM rl_episodes WHERE episode_id=?", (first,)
            ).fetchone()
            payload = json.loads(row[0])
            payload["state"]["snapshot"]["config_hash"] = "corrupted"
            connection.execute(
                "UPDATE rl_episodes SET payload_json=? WHERE episode_id=?",
                (json.dumps(payload), first),
            )
            connection.commit()
        finally:
            connection.close()
        restarted = self._service()
        with self.assertRaises(RLDataError):
            restarted.step(first, {"action_type": "hold"})
        self.assertEqual(restarted.episode(second)["transition_count"], 1)

    def test_cancel_is_terminal_and_leaves_other_episode_untouched(self) -> None:
        service = self._service()
        _, first = self._start(service)
        _, second = self._start(service)
        service.step(second, {"action_type": "hold"})
        cancelled = service.cancel(first)
        self.assertEqual(cancelled["episode"]["status"], "cancelled")
        self.assertEqual(service.episode(second)["transition_count"], 1)
        with self.assertRaises(RLEpisodeError):
            service.step(first, {"action_type": "hold"})

    def test_cancel_recovers_a_persisted_running_episode(self) -> None:
        service = self._service()
        _, episode_id = self._start(service)
        service.step(episode_id, {"action_type": "hold"})
        service.close()
        self.services.remove(service)

        restarted = self._service()
        cancelled = restarted.cancel(episode_id)
        self.assertEqual(cancelled["episode"]["status"], "cancelled")
        self.assertEqual(restarted.health()["active_environments"], 0)

    def test_bounded_storage_and_active_environment_limit(self) -> None:
        service = self._service(max_experiences_per_episode=2, max_active_environments=1)
        _, episode_id = self._start(service)
        for _ in range(4):
            service.step(episode_id, {"action_type": "hold"})
        self.assertEqual(service.experiences(episode_id)["total"], 2)
        with self.assertRaises(RLEpisodeError):
            service.start_episode(service.environment(service.episode(episode_id)["environment_id"])["environment_id"])

    def test_recovery_after_bounded_experience_pruning_continues(self) -> None:
        service = self._service(max_experiences_per_episode=2, max_active_environments=1)
        _, episode_id = self._start(service)
        for _ in range(4):
            service.step(episode_id, {"action_type": "hold"})
        self.assertEqual(service.experiences(episode_id)["total"], 2)
        service.close()
        self.services.remove(service)

        restarted = self._service(
            max_experiences_per_episode=2, max_active_environments=1
        )
        result = restarted.step(episode_id, {"action_type": "hold"})
        self.assertEqual(result["transition_count"], 5)
        self.assertEqual(restarted.experiences(episode_id)["total"], 2)

    def test_reset_cannot_add_an_episode_over_the_active_limit(self) -> None:
        service = self._service(max_active_environments=1)
        _, first = self._start(service)
        service.cancel(first)
        _, second = self._start(service)
        with self.assertRaises(RLEpisodeError):
            service.reset_episode(first)
        self.assertEqual(service.episode(second)["status"], "running")
        self.assertEqual(service.episode(first)["status"], "cancelled")

    def test_store_refuses_duplicate_transition_sequence(self) -> None:
        store = RLExperienceStore(Path(self._tmp.name) / "store.sqlite3")
        try:
            cfg = config_for(self.bars).to_dict()
            episode = make_episode(
                episode_id="ep_dup",
                environment_id="env_dup",
                status="running",
                config=cfg,
                state={},
            )
            store.save_episode(episode)
            env = RLEnvironment(config_for(self.bars), self.bars)
            env.reset("ep_dup")
            transition = env.step({"action_type": "hold"})[4]
            store.record_step(episode, transition)
            with self.assertRaises(RLStoreError):
                store.record_step(episode, transition)
        finally:
            store.close()

    def test_store_caps_environment_and_episode_rows(self) -> None:
        store = RLExperienceStore(Path(self._tmp.name) / "limits.sqlite3")
        try:
            first = config_for(self.bars).with_environment_id("env_limit_1")
            second = config_for(self.bars).with_environment_id("env_limit_2")
            with mock.patch(
                "novacontrol.novamarket.rl.store.MAX_ENVIRONMENTS", 1
            ):
                store.save_environment(first.to_dict())
                with self.assertRaises(RLStoreError):
                    store.save_environment(second.to_dict())
            episode_one = make_episode(
                episode_id="ep_limit_1",
                environment_id="env_limit_1",
                status="running",
                config=first.to_dict(),
                state={},
            )
            episode_two = make_episode(
                episode_id="ep_limit_2",
                environment_id="env_limit_2",
                status="running",
                config=second.to_dict(),
                state={},
            )
            with mock.patch(
                "novacontrol.novamarket.rl.store.MAX_STORED_EPISODES", 1
            ):
                store.save_episode(episode_one)
                with self.assertRaises(RLStoreError):
                    store.save_episode(episode_two)
        finally:
            store.close()

    def test_step_store_failure_leaves_no_phantom_experience(self) -> None:
        service = self._service()
        _, episode_id = self._start(service)

        def fail(*_args: Any, **_kwargs: Any) -> Any:
            raise RLStoreError("simulated store failure")

        service._store.record_step = fail  # type: ignore[method-assign]
        with self.assertRaises(RLStoreError):
            service.step(episode_id, {"action_type": "hold"})
        episode = service.episode(episode_id)
        self.assertEqual(episode["status"], "failed")
        self.assertEqual(episode["transition_count"], 0)
        self.assertEqual(service.experiences(episode_id)["total"], 0)
        self.assertEqual(service.health()["active_environments"], 0)

    def test_refused_action_does_not_mark_the_environment_unhealthy(self) -> None:
        service = self._service()
        _, episode_id = self._start(service)
        with self.assertRaises(RLInvalidActionError):
            service.step(episode_id, {"action_type": "sell", "instrument_id": "XNSE:ALPHA"})
        self.assertTrue(service.health()["healthy"])
        self.assertEqual(service.episode(episode_id)["invalid_actions"], 1)

    def test_rl_database_contains_only_rl_tables(self) -> None:
        service = self._service()
        _, episode_id = self._start(service)
        service.step(episode_id, {"action_type": "hold"})
        service.close()
        self.services.remove(service)
        connection = sqlite3.connect(self.path)
        try:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertTrue(names)
        self.assertTrue(all(name.startswith("rl_") for name in names), names)

    def test_production_identifiers_are_absent_from_action_and_portfolio(self) -> None:
        service = self._service()
        _, episode_id = self._start(service)
        result = service.step(episode_id, {"action_type": "hold"})
        self.assertNotIn("order_id", result["transition"]["action"])
        self.assertNotIn("portfolio_id", result["transition"]["portfolio_after"])
        self.assertNotIn("fill_id", result["transition"]["execution"])
        self.assertFalse(result["transition"]["execution"]["production_fill"])
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (Path(__file__).resolve().parent.parent / "src" / "novacontrol" / "novamarket" / "rl").glob("*.py")
        )
        for forbidden in ("ExecutionEngine", "ExecutionStore", "PortfolioEngine", "PortfolioStore", "RiskEngine", "AutonomyRuntime"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_missing_historical_data_is_refused(self) -> None:
        service = RLService(self.path, bar_loader=lambda cfg: [])
        self.services.append(service)
        saved = service.create_environment(
            {
                "name": "empty",
                "dataset_id": "nm02",
                "dataset_version": "empty",
                "instrument_id": "XNSE:ALPHA",
                "start": START,
                "end": START + timedelta(days=5),
            }
        )
        with self.assertRaises(RLDataError):
            service.start_episode(saved["environment_id"])

    def test_health_reports_bounded_resources_and_latency(self) -> None:
        service = self._service()
        _, episode_id = self._start(service)
        service.step(episode_id, {"action_type": "hold"})
        health = service.health()
        self.assertTrue(health["bounded_resources"])
        self.assertGreaterEqual(health["step_latency_ms"], 0)
        self.assertEqual(health["storage"]["isolated_database"], True)
        self.assertLessEqual(health["active_environments"], health["max_active_environments"])


@unittest.skipIf(TestClient is None, "httpx is required for the API integration test")
class RLApplicationApiTests(unittest.TestCase):
    """Drive the real FastAPI application over a temporary data directory."""

    def setUp(self) -> None:
        from test_web_api import _IsolatedApiTestCase

        self._base = _IsolatedApiTestCase(methodName="runTest")
        self._base.setUp()
        self.client = self._base._client
        self.bars = weekday_bars()

    def tearDown(self) -> None:
        self._base.tearDown()

    def test_api_creates_starts_steps_and_exposes_experiences(self) -> None:
        from novacontrol.novamarket.service import NovaMarketService

        with mock.patch.object(NovaMarketService, "_rl_bars", return_value=self.bars), \
             mock.patch.object(NovaMarketService, "_rl_feature_snapshot", return_value=None), \
             mock.patch.object(NovaMarketService, "_rl_forecast_snapshot", return_value=None):
            status = self.client.get("/market/rl/status")
            self.assertEqual(status.status_code, 200, status.text)
            self.assertEqual(status.json()["phase"], "PI-13")
            contracts = self.client.get("/market/rl/contracts")
            self.assertEqual(contracts.status_code, 200)
            self.assertTrue(contracts.json()["reward"]["interface_only"])

            created = self.client.post(
                "/market/rl/environments",
                json={
                    "name": "api fixture",
                    "dataset_id": "nm02-point-in-time",
                    "dataset_version": "ds-v1",
                    "instrument_id": "XNSE:ALPHA",
                    "start": self.bars[0].observed_at.isoformat(),
                    "end": (self.bars[-1].observed_at + timedelta(days=1)).isoformat(),
                    "initial_capital": "100000",
                    "max_steps": 10,
                },
            )
            self.assertEqual(created.status_code, 200, created.text)
            environment_id = created.json()["environment_id"]
            started = self.client.post(
                f"/market/rl/environments/{environment_id}/episodes", json={}
            )
            self.assertEqual(started.status_code, 200, started.text)
            episode_id = started.json()["episode"]["episode_id"]
            step = self.client.post(
                f"/market/rl/episodes/{episode_id}/step",
                json={"action_type": "buy", "instrument_id": "XNSE:ALPHA", "notional": "1000"},
            )
            self.assertEqual(step.status_code, 200, step.text)
            self.assertEqual(step.json()["transition_count"], 1)
            self.assertFalse(step.json()["transition"]["execution"]["production_fill"])
            history = self.client.get(f"/market/rl/episodes/{episode_id}/experiences")
            self.assertEqual(history.status_code, 200, history.text)
            self.assertEqual(history.json()["total"], 1)
            invalid = self.client.post(
                f"/market/rl/episodes/{episode_id}/step",
                json={"action_type": "sell", "instrument_id": "XNSE:ALPHA", "quantity": "1000"},
            )
            self.assertEqual(invalid.status_code, 422, invalid.text)
            detail = self.client.get(f"/market/rl/episodes/{episode_id}")
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["status"], "running")

    def test_api_unknown_ids_and_surface_have_no_production_mutation_route(self) -> None:
        status = self.client.get("/market/rl/status")
        self.assertEqual(status.status_code, 200)
        missing = self.client.get("/market/rl/episodes/does-not-exist")
        self.assertEqual(missing.status_code, 404)
        paths = {
            route.path
            for route in self.client.app.routes
            if getattr(route, "path", "").startswith("/market/rl")
        }
        self.assertTrue(paths)
        for path in paths:
            self.assertFalse(any(word in path for word in ("execute", "order", "portfolio", "autonomy", "promote")))


class FrontendIntegrationTests(unittest.TestCase):
    def test_research_page_is_registered_and_calls_only_rl_routes(self) -> None:
        root = Path(__file__).resolve().parent.parent / "web" / "src"
        page = (root / "pages" / "RLResearch.tsx").read_text(encoding="utf-8")
        nav = (root / "nav.ts").read_text(encoding="utf-8")
        app = (root / "App.tsx").read_text(encoding="utf-8")
        self.assertIn("RLResearchPage", app)
        self.assertIn('case "rlresearch"', app)
        self.assertIn('id: "rlresearch"', nav)
        self.assertIn("/market/rl/environments", page)
        self.assertIn("/market/rl/episodes", page)
        self.assertIn("simulated", page.lower())
        self.assertIn("not trading performance", page.lower())
        for forbidden in ("/market/execution", "/market/autonomy", "/market/portfolios", "/command/execute"):
            self.assertNotIn(forbidden, page)


if __name__ == "__main__":
    unittest.main()


class _ForecastStoreStub:
    """The PI-09 read surface, returning whatever contracts the test stored."""

    def __init__(self, rows: Sequence[Any]) -> None:
        self.rows = list(rows)

    def list_forecasts(self, **_kwargs: Any) -> list[Any]:
        return list(self.rows)


class _PredictionServiceStub:
    def __init__(self, rows: Sequence[Any]) -> None:
        self.store = _ForecastStoreStub(rows)


class RLForecastSnapshotTests(unittest.TestCase):
    """The PI-13 forecast loader only reads contracts of its own dataset."""

    def setUp(self) -> None:
        from novacontrol.novamarket.service import NovaMarketService

        self.temp = TemporaryDirectory()
        self.service = NovaMarketService(data_dir=Path(self.temp.name))
        self.at = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
        self.config = RLEnvironmentConfig.from_mapping(
            {
                "name": "forecast scope fixture",
                "dataset_id": "nm02-point-in-time",
                "dataset_version": "ds-pi13-v1",
                "instrument_id": "XNSE:ALPHA",
                "start": self.at.isoformat(),
                "end": (self.at + timedelta(days=30)).isoformat(),
                "initial_capital": "100000",
                "max_steps": 10,
                "max_order_notional": "25000",
                "max_position_weight": "0.5",
                "execution_config": {},
            }
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def contract(self, forecast_id: str, *, dataset_version: str, generated_at: datetime) -> Any:
        from novacontrol.novamarket.prediction.models import ForecastOutput

        return ForecastOutput(
            forecast_id=forecast_id,
            lifecycle_id=f"lc_{forecast_id}",
            lifecycle_stage="t_minus_1",
            instrument_id="XNSE:ALPHA",
            exchange="XNSE",
            currency="INR",
            reference_price=Decimal("100"),
            reference_at=generated_at,
            predicted_price=Decimal("101"),
            predicted_return=0.01,
            direction="up",
            lower_return=-0.05,
            upper_return=0.08,
            lower_price=Decimal("95"),
            upper_price=Decimal("108"),
            uncertainty="medium",
            uncertainty_width=0.13,
            p_up=0.6,
            p_down=0.3,
            p_neutral=0.1,
            probability_status="calibrated",
            calibration_status="calibrated",
            confidence=0.6,
            ensemble_id="ens_pi13",
            model_id="ensemble",
            model_version="pi08-v1",
            member_versions=("xgboost@v1",),
            forecast_horizon="5d",
            target_at=generated_at + timedelta(days=5),
            generated_at=generated_at,
            data_observed_at=generated_at,
            data_received_at=generated_at,
            data_resolution="1d",
            data_freshness="fresh",
            data_quality="valid",
            data_snapshot_id="snap_pi13",
            validity="valid",
            validity_conditions=(),
            disagreement=0.1,
            digest="b" * 64,
            metadata={},
            feature_set_version="pi04-features-v1",
            dataset_version=dataset_version,
        )

    def load(self, rows: Sequence[Any], config: Any = None) -> Any:
        from novacontrol.novamarket.service import NovaMarketService

        with mock.patch.object(
            NovaMarketService,
            "prediction_service",
            return_value=_PredictionServiceStub(rows),
        ):
            return self.service._rl_forecast_snapshot(config or self.config, self.at)

    def test_a_forecast_from_another_dataset_is_ignored(self) -> None:
        rows = [
            self.contract(
                "pfc_native", dataset_version="ds-pi13-v1", generated_at=self.at - timedelta(hours=1)
            ),
            # Later and therefore "latest" if the loader reads every dataset.
            self.contract(
                "pfc_foreign", dataset_version="ds-other-v9", generated_at=self.at
            ),
        ]
        snapshot = self.load(rows)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["forecast_id"], "pfc_native")
        self.assertEqual(snapshot["dataset_version"], "ds-pi13-v1")

    def test_only_a_foreign_dataset_forecast_reads_as_missing(self) -> None:
        rows = [
            self.contract(
                "pfc_foreign", dataset_version="ds-other-v9", generated_at=self.at
            )
        ]
        self.assertIsNone(self.load(rows))

    def test_each_dataset_version_gets_its_own_cached_scope(self) -> None:
        rows = [
            self.contract(
                "pfc_native", dataset_version="ds-pi13-v1", generated_at=self.at - timedelta(hours=1)
            )
        ]
        self.assertIsNotNone(self.load(rows))
        other = replace(self.config, dataset_version="ds-other-v9")
        # The same cached instrument rows must not leak into the other version.
        self.assertIsNone(self.load(rows, config=other))


class SeededReplayIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """A seeded PI-02 dataset and a stored PI-01 contract stay usable in replay.

    Both regressions were silent: features came back empty because the export's
    ingestion timestamp post-dated the replay cutoff, and a forecast built on
    another dataset could be selected as "latest".  This drives the real
    dataset builder, the real PI-04 feature service and the real PI-01 store
    through the service's own replay loaders.
    """

    async def test_seeded_bars_yield_features_and_a_usable_forecast(self) -> None:
        from test_novamarket_datasets import (
            FakeAdapter,
            _clock,
            _day,
            _instrument,
            _provider_bar,
            _request,
        )

        from novacontrol.novamarket.datasets import DatasetStore, HistoricalDatasetService
        from novacontrol.novamarket.marketdata.engine import MarketDataEngine
        from novacontrol.novamarket.marketdata.providers.registry import ProviderRegistry
        from novacontrol.novamarket.marketdata.store import MarketDataStore
        from novacontrol.novamarket.prediction.models import ForecastOutput
        from novacontrol.novamarket.service import NovaMarketService

        start = datetime(2026, 1, 5, tzinfo=UTC)
        provider_rows = tuple(_provider_bar(_day(index), close=str(100 + index)) for index in range(60))
        end = max(row.observed_at for row in provider_rows) + timedelta(days=1)

        # The service keeps its SQLite connections open for the whole test.
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "novamarket.sqlite3"
            market_store = MarketDataStore(str(path), clock=_clock)
            engine = MarketDataEngine(
                store=market_store,
                registry=ProviderRegistry([FakeAdapter(provider_rows)]),
                enabled_providers=("fake",),
                clock=_clock,
            )
            engine.register_instrument(_instrument())
            built = await HistoricalDatasetService(
                store=DatasetStore(str(path), clock=_clock),
                market_data=engine,
                clock=_clock,
            ).build(
                _request(instruments=("XNSE:RELIANCE",), start=start, end=end), fetch=True
            )
            self.assertGreaterEqual(built["bar_count"], 30)

            service = NovaMarketService(data_dir=Path(directory))
            config = RLEnvironmentConfig.from_mapping(
                {
                    "name": "seeded replay",
                    "dataset_id": "nse-daily-core",
                    "dataset_version": built["version"],
                    "instrument_id": "XNSE:RELIANCE",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "initial_capital": "100000",
                    "max_steps": 40,
                    "max_order_notional": "25000",
                    "max_position_weight": "0.5",
                    "execution_config": {},
                }
            )

            bars = service._rl_bars(config)
            self.assertGreaterEqual(len(bars), 30)
            at = bars[-1].observed_at

            # PI-04 features over the seeded PI-02 bars must carry values.
            features = service._rl_feature_snapshot(config, at, bars)
            self.assertIsNotNone(features, "real PI-02 bars produced no feature snapshot")
            values = {
                name: value
                for name, value in (features.get("values") or {}).items()
                if value is not None
            }
            self.assertGreaterEqual(
                len(values), 10, f"features are empty: {sorted(features)}"
            )
            as_of = datetime.fromisoformat(str(features["as_of"]).replace("Z", "+00:00"))
            self.assertLessEqual(as_of, at, "a feature row must not post-date the replay bar")

            # A real PI-01 contract, stored through the real store, must be
            # usable at this replay timestamp.
            forecast = ForecastOutput(
                forecast_id="pfc_seeded_replay",
                lifecycle_id="lc_seeded_replay",
                lifecycle_stage="t_minus_1",
                instrument_id="XNSE:RELIANCE",
                exchange="NSE",
                currency="INR",
                reference_price=Decimal("159"),
                reference_at=at,
                predicted_price=Decimal("160.6"),
                predicted_return=0.01,
                direction="up",
                lower_return=-0.03,
                upper_return=0.05,
                lower_price=Decimal("155"),
                upper_price=Decimal("168"),
                uncertainty="medium",
                uncertainty_width=0.08,
                p_up=0.6,
                p_down=0.3,
                p_neutral=0.1,
                probability_status="calibrated",
                calibration_status="calibrated",
                confidence=0.6,
                ensemble_id="ens_seeded_replay",
                model_id="ensemble",
                model_version="pi08-v1",
                member_versions=("xgboost@v1",),
                forecast_horizon="5d",
                target_at=at + timedelta(days=5),
                generated_at=at,
                data_observed_at=at,
                data_received_at=at,
                data_resolution="1d",
                data_freshness="fresh",
                data_quality="valid",
                data_snapshot_id="snap_seeded_replay",
                validity="valid",
                validity_conditions=(),
                disagreement=0.05,
                digest="c" * 64,
                metadata={},
                feature_set_version="pi04-features-v1",
                dataset_version=built["version"],
            )
            self.assertTrue(service.prediction_service().store.save_forecast(forecast))
            snapshot = service._rl_forecast_snapshot(config, at)
            self.assertIsNotNone(snapshot, "a valid stored forecast was not found at replay time")
            self.assertEqual(snapshot["forecast_id"], "pfc_seeded_replay")
            self.assertEqual(snapshot["dataset_version"], built["version"])

            coverage = ObservationBuilder(config).coverage(
                at=at, feature_snapshot=features, forecast_snapshot=snapshot
            )
            self.assertEqual(coverage, {"features": "available", "forecasts": "available"})
