"""PI-14 focused verification: reward calculation and policy evaluation.

All fixtures are deterministic historical simulation data.  The tests exercise
the real PI-13 environment, the real reward service/store and the real FastAPI
application; they never use a production portfolio, ledger, risk or execution
store.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None  # type: ignore[assignment]

from novacontrol.novamarket.backtesting.core import HistoricalBar
from novacontrol.novamarket.rewards import (
    DEFAULT_COMPONENTS,
    DEFAULT_WEIGHTS,
    EvaluationConfig,
    PolicyEvaluationError,
    RewardConfig,
    RewardEngine,
    RewardService,
    RewardStore,
    RewardValidationError,
    SplitDefinition,
    TransitionView,
    default_reward_config,
    policy_catalogue,
)
from novacontrol.novamarket.rewards.components import COMPONENT_REGISTRY
from novacontrol.novamarket.rewards.evaluation import EvaluationDataset
from novacontrol.novamarket.rl import RLEnvironmentConfig
from novacontrol.novamarket.rewards.metrics import policy_metrics

START = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)


def price_bars(
    count: int = 90, *, start: datetime = START, rising: bool = True
) -> list[HistoricalBar]:
    bars: list[HistoricalBar] = []
    at = start
    index = 0
    while len(bars) < count:
        if at.weekday() < 5:
            base = 100.0 + index * (0.8 if rising else -0.35)
            open_price = Decimal(str(round(base, 2)))
            close = Decimal(str(round(base + (0.7 if rising else 0.2), 2)))
            bars.append(
                HistoricalBar(
                    instrument_id="XNSE:ALPHA",
                    observed_at=at,
                    open=open_price,
                    high=max(open_price, close) + Decimal("1"),
                    low=min(open_price, close) - Decimal("1"),
                    close=close,
                    volume=Decimal("250000"),
                    source="nm02",
                )
            )
            index += 1
        at += timedelta(days=1)
    return bars


def environment_payload(bars: Sequence[HistoricalBar], **changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "PI-14 fixture",
        "dataset_id": "nm02-point-in-time",
        "dataset_version": "ds-pi14-v1",
        "instrument_id": "XNSE:ALPHA",
        "start": bars[0].observed_at.isoformat(),
        "end": (bars[-1].observed_at + timedelta(days=1)).isoformat(),
        "initial_capital": "100000.00",
        "max_steps": 60,
        "max_order_notional": "25000.00",
        "max_position_weight": "0.50",
        "execution_config": {},
    }
    payload.update(changes)
    return payload


def forecast(at: datetime, *, direction: str = "up", expected: float = 0.02) -> dict[str, Any]:
    return {
        "forecast_id": f"fc_{at.date().isoformat()}",
        "generated_at": at.isoformat(),
        "direction": direction,
        "expected_return": expected,
        "predicted_return": expected,
        "p_up": 0.62 if direction == "up" else 0.38,
        "p_down": 0.25 if direction == "up" else 0.55,
        "probability_status": "calibrated",
        "lower_return": 0.0,
        "upper_return": 0.04,
        "uncertainty": "low",
        "uncertainty_width": 0.04,
        "disagreement": 0.05,
        "horizon": "1d",
        "validity": "valid",
        "available": True,
        "model_version": "ensemble-v1",
        "feature_set_version": "pi04-v1",
        "dataset_version": "ds-pi14-v1",
    }


def transition(
    *,
    action: str = "hold",
    before_equity: float = 100000.0,
    after_equity: float = 100000.0,
    before_cash: float = 100000.0,
    after_cash: float = 100000.0,
    before_position: float = 0.0,
    after_position: float = 0.0,
    before_exposure: float = 0.0,
    after_exposure: float = 0.0,
    realized: float = 0.0,
    unrealized: float = 0.0,
    fees: float = 0.0,
    drawdown: float = 0.0,
    execution_status: str = "not_applicable",
    quantity: float = 0.0,
    price: float = 100.0,
    reference: float = 100.0,
    slippage: float = 0.0,
    forecast_payload: Mapping[str, Any] | None = None,
    sequence: int = 1,
    history: str = "",
) -> dict[str, Any]:
    timestamp = START + timedelta(days=sequence - 1)
    obs_forecast = dict(forecast_payload) if forecast_payload is not None else {}
    execution = {
        "status": execution_status,
        "side": action if action in {"buy", "sell"} else "none",
        "quantity": f"{quantity:.6f}",
        "reference_price": f"{reference:.2f}",
        "execution_price": f"{price:.2f}",
        "notional": f"{quantity * price:.2f}",
        "slippage_amount": f"{slippage:.2f}",
        "fees": f"{fees:.2f}",
        "fee_components": {"brokerage": f"{fees:.2f}"},
        "at": timestamp.isoformat(),
        "reason": "",
        "source": "nm02_historical_bar",
        "price_assumption": "next_bar_open_plus_nm10_slippage_model",
        "production_fill": False,
        "nm10_order_id": "",
    }
    observation = {
        "observation_id": f"obs_{sequence:06d}",
        "timestamp": timestamp.isoformat(),
        "instrument_id": "XNSE:ALPHA",
        "market": {"close": "100.00", "volume": "250000"},
        "features": {"values": {"rsi_14": 55.0, "macd": 0.4, "volatility_20": 0.02}},
        "forecasts": obs_forecast,
        "provenance": {
            "feature_set_version": "pi04-v1",
            "dataset_version": "ds-pi14-v1",
            "reward_interface_version": "pi13-reward-interface-v1",
        },
    }
    next_observation = {
        **observation,
        "observation_id": f"obs_{sequence + 1:06d}",
        "timestamp": (timestamp + timedelta(days=1)).isoformat(),
        "market": {"close": "101.00", "volume": "250000"},
    }
    positions = []
    if after_position:
        positions = [
            {
                "instrument_id": "XNSE:ALPHA",
                "quantity": f"{after_position:.6f}",
                "average_entry_price": "100.00",
                "last_price": "101.00",
                "market_value": f"{after_position * 101.0:.2f}",
                "unrealized_pnl": f"{unrealized:.2f}",
            }
        ]
    before_positions = []
    if before_position:
        before_positions = [
            {
                "instrument_id": "XNSE:ALPHA",
                "quantity": f"{before_position:.6f}",
                "average_entry_price": "100.00",
                "last_price": "100.00",
                "market_value": f"{before_position * 100.0:.2f}",
                "unrealized_pnl": "0.00",
            }
        ]
    return {
        "transition_id": f"tx_{history or 'ep'}_{sequence:06d}",
        "environment_id": "rl_env_fixture",
        "episode_id": history or "ep_fixture",
        "sequence": sequence,
        "timestamp": timestamp.isoformat(),
        "observation": observation,
        "action": {"action_type": action, "instrument_id": "XNSE:ALPHA"},
        "execution": execution,
        "reward": {
            "total": "0.00",
            "components": {},
            "interface_only": True,
        },
        "next_observation": next_observation,
        "terminated": False,
        "truncated": False,
        "termination_reason": "",
        "dataset_version": "ds-pi14-v1",
        "feature_set_version": "pi04-v1",
        "environment_version": "pi13-rl-env-1.0",
        "simulation_config_hash": "fixture-config-hash",
        "portfolio_before": {
            "initial_capital": "100000.00",
            "cash": f"{before_cash:.2f}",
            "positions": before_positions,
            "realized_pnl": "0.00",
            "unrealized_pnl": "0.00",
            "fees_paid": "0.00",
            "equity": f"{before_equity:.2f}",
            "exposure": f"{before_exposure:.6f}",
            "drawdown": "0.000000",
            "peak_equity": f"{max(before_equity, after_equity):.2f}",
        },
        "portfolio_after": {
            "initial_capital": "100000.00",
            "cash": f"{after_cash:.2f}",
            "positions": positions,
            "realized_pnl": f"{realized:.2f}",
            "unrealized_pnl": f"{unrealized:.2f}",
            "fees_paid": f"{fees:.2f}",
            "equity": f"{after_equity:.2f}",
            "exposure": f"{after_exposure:.6f}",
            "drawdown": f"{drawdown:.6f}",
            "peak_equity": f"{max(before_equity, after_equity):.2f}",
        },
    }


class RewardEngineTests(unittest.TestCase):
    def test_engine_initialization_and_registry(self) -> None:
        engine = RewardEngine()
        self.assertEqual(set(engine.registry), set(DEFAULT_COMPONENTS))
        self.assertEqual(len(COMPONENT_REGISTRY), 8)
        self.assertFalse(engine.status()["mutates_production_financial_state"])
        self.assertFalse(engine.status()["mutates_environment"])

    def test_configuration_validation_and_versioning(self) -> None:
        cfg = RewardConfig(name="valid")
        self.assertTrue(cfg.experimental)
        self.assertEqual(cfg.config_hash, RewardConfig(name="valid").config_hash)
        changed = RewardConfig(name="valid", components={**DEFAULT_WEIGHTS, "risk": 0.5})
        self.assertNotEqual(cfg.config_id, changed.config_id)
        with self.assertRaises(RewardValidationError):
            RewardConfig(name="bad", components={**DEFAULT_WEIGHTS, "unknown": 0.1})
        with self.assertRaises(RewardValidationError):
            RewardConfig(name="bad", enabled=("unknown",))
        with self.assertRaises(RewardValidationError):
            RewardConfig(name="bad", normalization="magic")
        with self.assertRaises(RewardValidationError):
            RewardConfig(name="bad", bounds=(1, 1))
        with self.assertRaises(RewardValidationError):
            RewardConfig(name="bad", scales={**cfg.scales, "risk": 0})
        with self.assertRaises(RewardValidationError):
            RewardConfig(name="bad", missing_policy="ignore")
        with self.assertRaises(RewardValidationError):
            RewardConfig(name="bad", penalties={"unknown": 1})
        with self.assertRaises(RewardValidationError):
            RewardConfig(name="bad", thresholds={"unknown": 1})
        with self.assertRaises(RewardValidationError):
            RewardConfig(name="bad", thresholds={"risk_drawdown_high": 0})
        self.assertEqual(cfg.thresholds["risk_drawdown_high"], 0.10)
        self.assertEqual(default_reward_config().name, "PI-14 experimental default")

    def test_component_registry_is_complete(self) -> None:
        from novacontrol.novamarket.rewards.components import validate_component_registry

        validate_component_registry()
        self.assertEqual(
            set(COMPONENT_REGISTRY),
            {
                "profitability",
                "risk",
                "transaction_cost",
                "decision_quality",
                "forecast_utilization",
                "discipline",
                "portfolio_management",
                "execution_quality",
            },
        )

    def test_profitability_calculation_avoids_double_counting_costs(self) -> None:
        result = RewardEngine().calculate(
            transition(
                action="buy",
                before_equity=100000,
                after_equity=101000,
                fees=10,
                slippage=5,
                quantity=100,
                execution_status="filled",
            )
        )
        component = result.components["profitability"]
        self.assertAlmostEqual(component["raw_value"], 0.01015, places=8)
        self.assertAlmostEqual(component["metrics"]["gross_return"], 0.01015, places=8)
        self.assertIn("double_counting_guard", component["metrics"])
        self.assertAlmostEqual(component["metrics"]["net_return"], 0.01, places=8)

    def test_transaction_cost_uses_fees_and_slippage_once(self) -> None:
        result = RewardEngine().calculate(
            transition(
                action="buy",
                before_equity=100000,
                after_equity=100000,
                fees=10,
                slippage=5,
                quantity=100,
                execution_status="filled",
            )
        )
        cost = result.components["transaction_cost"]
        self.assertAlmostEqual(cost["metrics"]["total_cost"], 15.0, places=6)
        self.assertAlmostEqual(cost["raw_value"], -0.00015, places=8)
        self.assertEqual(cost["metrics"]["fees"], 10.0)
        self.assertEqual(cost["metrics"]["slippage"], 5.0)

    def test_risk_component_uses_exposure_concentration_and_drawdown(self) -> None:
        result = RewardEngine().calculate(
            transition(
                before_equity=100000,
                after_equity=100000,
                after_exposure=0.70,
                after_position=700,
                drawdown=-0.20,
            )
        )
        risk = result.components["risk"]
        self.assertLess(risk["raw_value"], -0.30)
        self.assertAlmostEqual(risk["metrics"]["exposure_ratio"], 0.70, places=6)
        self.assertGreater(risk["metrics"]["concentration"], 0.60)
        payload = transition(
            before_equity=100000,
            after_equity=100000,
            after_exposure=0.70,
            after_position=700,
            drawdown=-0.20,
        )
        tight = RewardEngine().calculate(
            payload,
            config=RewardConfig(name="tight risk", thresholds={"risk_drawdown_high": 0.01}),
        )
        loose = RewardEngine().calculate(
            payload,
            config=RewardConfig(name="loose risk", thresholds={"risk_drawdown_high": 0.50}),
        )
        self.assertLess(
            tight.components["risk"]["raw_value"],
            loose.components["risk"]["raw_value"],
        )

    def test_decision_quality_uses_only_decision_time_evidence(self) -> None:
        up = forecast(START, direction="up", expected=0.03)
        result = RewardEngine().calculate(
            transition(
                action="buy",
                before_cash=100000,
                before_equity=100000,
                after_equity=100000,
                forecast_payload=up,
                quantity=100,
                execution_status="filled",
            )
        )
        quality = result.components["decision_quality"]
        self.assertGreater(quality["raw_value"], 0.5)
        self.assertEqual(
            quality["metrics"]["information_boundary"],
            "decision-time observation only; next outcomes not used",
        )
        no_forecast = RewardEngine().calculate(
            transition(action="buy", after_equity=100000, quantity=100, execution_status="filled")
        )
        self.assertLess(no_forecast.components["decision_quality"]["raw_value"], 0)

    def test_decision_quality_treats_reasonable_loss_as_not_automatically_bad(self) -> None:
        up = forecast(START, direction="up", expected=0.03)
        result = RewardEngine().calculate(
            transition(
                action="buy",
                before_equity=100000,
                after_equity=98000,
                forecast_payload=up,
                quantity=100,
                execution_status="filled",
            )
        )
        self.assertGreater(result.components["decision_quality"]["raw_value"], 0.5)
        self.assertLess(result.components["profitability"]["raw_value"], 0)

    def test_sell_is_refused_when_no_position_exists(self) -> None:
        result = RewardEngine().calculate(
            transition(action="sell", before_position=0, quantity=1, execution_status="filled")
        )
        self.assertEqual(result.components["decision_quality"]["raw_value"], -1.0)
        self.assertEqual(
            result.components["decision_quality"]["metrics"]["reason"],
            "sell requires an existing long position",
        )

    def test_sell_with_a_long_position_and_deteriorating_forecast_is_reasonable(self) -> None:
        result = RewardEngine().calculate(
            transition(
                action="sell",
                before_position=100,
                after_position=0,
                before_exposure=0.2,
                after_exposure=0.0,
                quantity=100,
                execution_status="filled",
                forecast_payload=forecast(START, direction="down", expected=-0.02),
            )
        )
        self.assertGreater(result.components["decision_quality"]["raw_value"], 0.0)

    def test_sell_without_exit_evidence_is_not_rewarded(self) -> None:
        result = RewardEngine().calculate(
            transition(
                action="sell",
                before_position=100,
                after_position=0,
                quantity=100,
                execution_status="filled",
            )
        )
        self.assertLessEqual(result.components["decision_quality"]["raw_value"], 0.0)
        self.assertEqual(
            result.components["decision_quality"]["metrics"]["exit_evidence"],
            "forecast deterioration or drawdown",
        )

    def test_forecast_utilization_rewards_abstention_without_evidence(self) -> None:
        result = RewardEngine().calculate(transition(action="wait"))
        forecast_component = result.components["forecast_utilization"]
        self.assertTrue(forecast_component["metrics"]["abstention"])
        self.assertGreater(forecast_component["raw_value"], 0)

    def test_execution_quality_and_partial_fill(self) -> None:
        result = RewardEngine().calculate(
            transition(
                action="buy",
                quantity=50,
                execution_status="partial",
                before_equity=100000,
                after_equity=100000,
                price=101.0,
                reference=100.0,
            )
        )
        execution = result.components["execution_quality"]
        self.assertEqual(execution["metrics"]["fill_ratio"], 0.5)
        self.assertEqual(execution["metrics"]["future_price_used_to_judge_order"], False)
        self.assertAlmostEqual(execution["metrics"]["slippage_bps"], 100.0)
        rejected = RewardEngine().calculate(
            transition(action="buy", quantity=0, execution_status="rejected")
        )
        self.assertEqual(rejected.components["execution_quality"]["raw_value"], -1.0)

    def test_portfolio_management_does_not_reward_deployment_for_its_own_sake(self) -> None:
        low = RewardEngine().calculate(transition(after_equity=100000, after_exposure=0.1))
        high = RewardEngine().calculate(
            transition(after_equity=100000, after_exposure=0.8, drawdown=-0.1)
        )
        self.assertEqual(
            low.components["portfolio_management"]["metrics"][
                "deployment_is_not_intrinsically_rewarded"
            ],
            True,
        )
        self.assertLessEqual(
            high.components["portfolio_management"]["raw_value"],
            low.components["portfolio_management"]["raw_value"],
        )

    def test_missing_component_handling_and_renormalization(self) -> None:
        result = RewardEngine().calculate(transition(before_equity=0, after_equity=0))
        self.assertIn("profitability", result.missing_components)
        self.assertIn("risk", result.missing_components)
        self.assertEqual(result.components["profitability"]["contribution"], 0.0)
        self.assertEqual(result.normalization["method"], "bounded")
        self.assertIn("missing_policy", result.normalization)
        cfg = RewardConfig(
            name="zero",
            missing_policy="zero",
            enabled=("profitability", "risk"),
        )
        zero = RewardEngine().calculate(transition(before_equity=0, after_equity=0), config=cfg)
        self.assertEqual(zero.total, 0.0)

    def test_outlier_is_bounded_and_explained(self) -> None:
        result = RewardEngine().calculate(transition(before_equity=100000, after_equity=100000000))
        profitability = result.components["profitability"]
        self.assertEqual(profitability["normalized"], 1.0)
        self.assertTrue(profitability["outlier_clipped"])
        low, high = default_reward_config().bounds
        self.assertGreaterEqual(result.total, low)
        self.assertLessEqual(result.total, high)

    def test_reproducible_result_and_configuration_change_does_not_rewrite_history(self) -> None:
        engine = RewardEngine(clock=lambda: datetime(2026, 1, 10, tzinfo=UTC))
        tx = transition(
            action="buy",
            forecast_payload=forecast(START),
            quantity=100,
            execution_status="filled",
            after_equity=100300,
        )
        first = engine.calculate(tx)
        second = engine.calculate(tx)
        self.assertEqual(first.result_hash, second.result_hash)
        self.assertEqual(first.reward_id, second.reward_id)
        changed = engine.calculate(
            tx,
            config=RewardConfig(
                name="changed", components={**DEFAULT_WEIGHTS, "profitability": 0.1}
            ),
        )
        self.assertNotEqual(first.config_hash, changed.config_hash)
        self.assertNotEqual(first.result_hash, changed.result_hash)

    def test_attribution_is_sorted_and_complete(self) -> None:
        result = RewardEngine().calculate(transition(action="hold"))
        self.assertEqual(len(result.attribution), len(DEFAULT_COMPONENTS))
        self.assertEqual(
            [item["component"] for item in result.attribution],
            sorted(
                (item["component"] for item in result.attribution),
                key=lambda name: (-abs(result.components[name]["contribution"]), name),
            ),
        )
        self.assertTrue(result.source["transition_id"])
        self.assertTrue(result.source["environment_version"])
        self.assertIn("weights_denominator", result.normalization)

    def test_disabled_component_is_excluded_and_explained(self) -> None:
        cfg = RewardConfig(name="disabled", enabled=("profitability",))
        result = RewardEngine().calculate(transition(), config=cfg)
        self.assertIn("risk", result.excluded_components)
        self.assertEqual(result.components["risk"]["enabled"], False)
        self.assertEqual(result.components["risk"]["contribution"], 0.0)

    def test_zero_initial_capital_is_explicit_not_invented(self) -> None:
        result = RewardEngine().calculate(
            transition(before_equity=0, after_equity=0, before_cash=0, after_cash=0)
        )
        self.assertEqual(result.components["profitability"]["available"], False)
        self.assertEqual(
            result.components["profitability"]["reason"],
            "previous portfolio equity was not positive",
        )
        self.assertTrue(math_is_finite(result.total))

    def test_historical_result_can_be_recalculated_from_stored_transition(self) -> None:
        engine = RewardEngine()
        tx = transition(
            action="buy",
            forecast_payload=forecast(START),
            quantity=100,
            execution_status="filled",
            after_equity=100200,
        )
        first = engine.calculate(tx)
        recalculated = engine.recalculate(json.loads(json.dumps(tx)))
        self.assertEqual(first.total, recalculated.total)
        self.assertEqual(first.result_hash, recalculated.result_hash)


class RewardStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "novamarket_rl.sqlite3"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_configuration_and_reward_persistence_is_append_only(self) -> None:
        store = RewardStore(self.path)
        cfg = RewardConfig(name="persisted")
        saved = store.save_config(cfg)
        self.assertEqual(saved["config_hash"], cfg.config_hash)
        self.assertEqual(store.config(cfg.config_id)["name"], "persisted")
        result = RewardEngine().calculate(transition())
        store.save_result(result)
        self.assertEqual(store.results("ep_fixture")["total"], 1)
        self.assertEqual(store.result(result.reward_id)["total"], result.total)
        connection = sqlite3.connect(self.path)
        try:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            connection.close()
        self.assertTrue(
            {
                "rl_reward_configs",
                "rl_reward_results",
                "rl_policy_evaluations",
                "rl_policy_comparisons",
            }
            <= tables
        )
        self.assertNotIn("portfolios", tables)
        self.assertNotIn("ledger_entries", tables)
        self.assertNotIn("paper_orders", tables)
        store.close()

    def test_policy_evaluation_upsert_and_comparison(self) -> None:
        from novacontrol.novamarket.rewards.models import PolicyEvaluationResult

        store = RewardStore(self.path)
        result = PolicyEvaluationResult(
            evaluation_id="pe_test",
            status="completed",
            config_hash="cfg",
            split="test",
            policy_name="no_action",
            metrics={"financial": {"total_return": 0.0}},
            reward_summary={"total_reward": 0.0},
            action_distribution={"no_action": 2},
            transitions=2,
            dataset_version="ds",
            environment_version="env",
            repro_hash="hash",
            created_at=START,
        )
        store.save_evaluation(result)
        store.save_evaluation(result)
        self.assertEqual(store.evaluations()["total"], 1)
        self.assertEqual(store.evaluation("pe_test")["policy_name"], "no_action")
        store.save_comparison(
            {"comparison_id": "cmp_test", "config_hash": "cfg", "created_at": START.isoformat()}
        )
        self.assertEqual(store.comparisons()["comparisons"][0]["comparison_id"], "cmp_test")
        status = store.status()
        self.assertEqual(status["production_financial_tables"], 0)
        store.close()

    def test_unknown_ids_are_reported(self) -> None:
        store = RewardStore(self.path)
        self.assertIsNone(store.config("rwc_missing"))
        self.assertIsNone(store.result("rw_missing"))
        store.close()


class PolicyEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "novamarket_rl.sqlite3"
        self.bars = price_bars()
        self.payload = environment_payload(self.bars)
        self.forecasts = {bar.observed_at: forecast(bar.observed_at) for bar in self.bars}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def service(self) -> RewardService:
        return RewardService(
            self.path,
            transition_reader=lambda episode_id: [],
            bar_loader=lambda config: self.bars,
            feature_loader=lambda config, at, history: {"values": {"rsi_14": 55.0}},
            forecast_loader=lambda config, at: self.forecasts.get(at),
        )

    def test_policy_catalogue_and_baselines(self) -> None:
        names = {row["name"] for row in policy_catalogue()}
        self.assertEqual(names, {"no_action", "rule_based", "pi12_adapter", "experimental_rl"})
        service = self.service()
        self.assertEqual(len(service.baselines()["policies"]), 4)
        self.assertTrue(service.baselines()["identical_conditions_required"])
        service.close()

    def test_identical_condition_comparison(self) -> None:
        service = self.service()
        try:
            result = service.evaluate(
                {
                    "name": "all baselines",
                    "environment": self.payload,
                    "policies": ["no_action", "rule_based", "pi12_adapter", "experimental_rl"],
                    "reward_config": default_reward_config().to_dict(),
                    "split": "all",
                    "max_transitions": 40,
                }
            )
            self.assertTrue(result["conditions_identical"])
            self.assertEqual(len(result["results"]), 4)
            fingerprints = {row["conditions"]["dataset_hash"] for row in result["results"]}
            self.assertEqual(len(fingerprints), 1)
            for row in result["results"]:
                self.assertTrue(row["simulated"])
                self.assertFalse(row["production_authority"])
                self.assertIn("not future performance", result["claim"])
                self.assertTrue(row["action_distribution"])
                self.assertEqual(row["evaluation_config"]["name"], "all baselines")
                self.assertIn("dataset_id", row["dataset"])
                self.assertIn("dataset_version", row["dataset"])
                self.assertIn("dataset_hash", row["conditions"])
                self.assertIn("episode_completion", row["metrics"]["learning"])
                self.assertIn("component_totals", row["reward_summary"])
                self.assertIn("penalty_totals", row["reward_summary"])
            self.assertIn("condition_fingerprint", result)
        finally:
            service.close()

    def test_chronological_split_validation_and_leakage_boundary(self) -> None:
        service = self.service()
        with self.assertRaises(RewardValidationError):
            EvaluationConfig(
                name="overlap",
                environment=self.payload,
                policies=("no_action",),
                split="test",
                splits={
                    "train": SplitDefinition("train", START, START + timedelta(days=10)),
                    "test": SplitDefinition(
                        "test", START + timedelta(days=5), START + timedelta(days=20)
                    ),
                },
            )
        with self.assertRaises(RewardValidationError):
            EvaluationConfig(
                name="missing",
                environment=self.payload,
                policies=("no_action",),
                split="test",
            )
        with self.assertRaises(RewardValidationError):
            EvaluationConfig(
                name="shared boundary",
                environment=self.payload,
                policies=("no_action",),
                split="test",
                splits={
                    "train": SplitDefinition("train", START, self.bars[49].observed_at),
                    "test": SplitDefinition(
                        "test", self.bars[49].observed_at, START + timedelta(days=80)
                    ),
                },
            )
        with self.assertRaises(RewardValidationError):
            EvaluationConfig(
                name="test before train",
                environment=self.payload,
                policies=("no_action",),
                split="test",
                splits={
                    "train": SplitDefinition(
                        "train", self.bars[50].observed_at, self.bars[-1].observed_at
                    ),
                    "test": SplitDefinition("test", START, self.bars[40].observed_at),
                },
            )
        splits = {
            "train": SplitDefinition("train", self.bars[0].observed_at, self.bars[49].observed_at),
            "test": SplitDefinition("test", self.bars[50].observed_at, self.bars[-1].observed_at),
        }
        result = service.evaluate(
            {
                "name": "chronological",
                "environment": self.payload,
                "policies": ["no_action", "rule_based"],
                "split": "test",
                "splits": {key: value.to_dict() for key, value in splits.items()},
                "max_transitions": 30,
            }
        )
        self.assertEqual(result["dataset"]["split"], "test")
        self.assertEqual(result["dataset"]["start"], splits["test"].start.isoformat())
        service.close()

    def test_feature_history_includes_pre_split_bars(self) -> None:
        calls: list[tuple[datetime, int]] = []

        def feature_loader(config: Any, at: datetime, history: Sequence[Any]) -> Mapping[str, Any]:
            calls.append((at, len(history)))
            return {"values": {"rsi_14": 55.0}}

        service = RewardService(
            self.path,
            transition_reader=lambda episode_id: [],
            bar_loader=lambda config: self.bars,
            feature_loader=feature_loader,
        )
        splits = {
            "train": SplitDefinition("train", self.bars[0].observed_at, self.bars[49].observed_at),
            "test": SplitDefinition("test", self.bars[50].observed_at, self.bars[-1].observed_at),
        }
        try:
            service.evaluate(
                {
                    "name": "warmup",
                    "environment": self.payload,
                    "policies": ["no_action"],
                    "split": "test",
                    "splits": {key: value.to_dict() for key, value in splits.items()},
                    "max_transitions": 2,
                }
            )
            first_test_history = next(size for at, size in calls if at == self.bars[50].observed_at)
            self.assertEqual(first_test_history, 51)
        finally:
            service.close()

    def test_split_history_is_bounded_and_keeps_pre_split_warmup(self) -> None:
        bars = price_bars(90)
        future = price_bars(5, start=bars[-1].observed_at + timedelta(days=1))
        all_bars = bars + future
        loaded_ranges: list[tuple[datetime, datetime]] = []
        loaded_batches: list[list[HistoricalBar]] = []
        feature_calls: list[tuple[datetime, list[datetime]]] = []

        def bar_loader(config: Any) -> list[HistoricalBar]:
            loaded_ranges.append((config.start, config.end))
            batch = [
                bar
                for bar in all_bars
                if config.start <= bar.observed_at <= config.end
            ]
            loaded_batches.append(batch)
            return batch

        def feature_loader(
            config: Any, at: datetime, history: Sequence[HistoricalBar]
        ) -> Mapping[str, Any]:
            feature_calls.append((at, [bar.observed_at for bar in history]))
            return {"values": {"rsi_14": 55.0}}

        service = RewardService(
            self.path,
            transition_reader=lambda episode_id: [],
            bar_loader=bar_loader,
            feature_loader=feature_loader,
        )
        splits = {
            "train": SplitDefinition("train", bars[0].observed_at, bars[29].observed_at),
            "validation": SplitDefinition("validation", bars[30].observed_at, bars[59].observed_at),
            "test": SplitDefinition("test", bars[60].observed_at, bars[89].observed_at),
        }
        try:
            service.evaluate(
                {
                    "name": "bounded warmup",
                    "environment": self.payload,
                    "policies": ["no_action"],
                    "split": "test",
                    "splits": {key: value.to_dict() for key, value in splits.items()},
                    "max_transitions": 2,
                }
            )
            # The loader is asked only for history through the split end, so it
            # cannot return future rows that would later be discarded.
            self.assertEqual(loaded_ranges[-1][1], splits["test"].end)
            self.assertTrue(loaded_batches[-1])
            self.assertLessEqual(
                max(bar.observed_at for bar in loaded_batches[-1]),
                splits["test"].end,
            )
            # The first test observation receives every prior bar, not only the
            # split's own rows.
            first_at, first_history = feature_calls[0]
            self.assertEqual(first_at, bars[60].observed_at)
            self.assertEqual(first_history, [bar.observed_at for bar in bars[:61]])
            self.assertLessEqual(first_history[-1], first_at)
        finally:
            service.close()

    def test_walk_forward_and_stability(self) -> None:
        service = self.service()
        walk = service.walk_forward(
            {
                "name": "walk",
                "environment": {**self.payload, "max_steps": 90},
                "policies": ["no_action", "rule_based"],
                "split": "all",
                "walk_forward": True,
                "walk_forward_train": 40,
                "walk_forward_test": 15,
                "walk_forward_step": 15,
                "max_transitions": 20,
            }
        )
        self.assertGreaterEqual(walk["windows"], 1)
        self.assertTrue(walk["no_test_fitting"])
        self.assertTrue(all(run["test_start"] < run["test_end"] for run in walk["results"]))
        splits = {
            "train": SplitDefinition("train", self.bars[0].observed_at, self.bars[44].observed_at),
            "test": SplitDefinition("test", self.bars[45].observed_at, self.bars[-1].observed_at),
        }
        stable = service.stability(
            {
                "name": "stable",
                "environment": self.payload,
                "policies": ["no_action"],
                "split": "test",
                "splits": {key: value.to_dict() for key, value in splits.items()},
                "max_transitions": 10,
            }
        )
        self.assertEqual(len(stable["runs"]), 2)
        self.assertFalse(stable["generalization_claim"])
        service.close()

    def test_truncation_bound_and_no_shared_mutable_book(self) -> None:
        service = self.service()
        result = service.evaluate(
            {
                "name": "bounded",
                "environment": self.payload,
                "policies": ["rule_based", "pi12_adapter"],
                "split": "all",
                "max_transitions": 3,
            }
        )
        for row in result["results"]:
            self.assertEqual(row["status"], "truncated")
            self.assertLessEqual(row["transitions"], 3)
            self.assertEqual(row["metrics"]["validity"]["sufficient"], True)
            self.assertFalse(row["metrics"]["learning"]["episode_completion"])
            self.assertTrue(row["metrics"]["learning"]["episode_truncated"])
        service.close()

    def test_environment_step_limit_is_not_reported_as_completed(self) -> None:
        service = self.service()
        result = service.evaluate(
            {
                "name": "environment truncation",
                "environment": {**self.payload, "max_steps": 5},
                "policies": ["no_action"],
                "split": "all",
                "max_transitions": 100,
            }
        )
        row = result["results"][0]
        self.assertEqual(row["status"], "truncated")
        self.assertFalse(row["metrics"]["learning"]["episode_completion"])
        self.assertTrue(row["metrics"]["learning"]["episode_truncated"])
        service.close()

    def test_metrics_honestly_report_insufficient_observations(self) -> None:
        service = self.service()
        result = service.evaluate(
            {
                "name": "short",
                "environment": self.payload,
                "policies": ["no_action"],
                "split": "all",
                "max_transitions": 2,
            }
        )
        metrics = result["results"][0]["metrics"]
        self.assertIsNone(metrics["risk"]["sharpe"]["value"])
        self.assertTrue(metrics["risk"]["no_annualized_metrics_from_short_sample"])
        self.assertIsNone(metrics["financial"]["sharpe"])
        self.assertIsNone(metrics["financial"]["annualized_return_raw"])
        self.assertIsNone(metrics["financial"]["annualized_return"])
        self.assertEqual(metrics["trading"]["total_trades"], 0)
        service.close()

    def test_trade_execution_time_and_total_costs(self) -> None:
        executed_at = START + timedelta(days=2)
        buy = transition(
            action="buy",
            sequence=1,
            quantity=10,
            execution_status="filled",
            price=101.0,
            reference=100.0,
            fees=10.0,
            slippage=2.0,
            after_equity=100000.0,
        )
        buy["execution"]["at"] = executed_at.isoformat()
        sell = transition(
            action="sell",
            sequence=3,
            quantity=10,
            execution_status="filled",
            before_position=10,
            after_position=0,
            before_equity=100000.0,
            after_equity=100100.0,
            realized=100.0,
        )
        metrics, trades = policy_metrics(
            initial_capital=Decimal("100000.00"),
            transitions=[
                TransitionView.from_transition(buy),
                TransitionView.from_transition(sell),
            ],
            rewards=[],
        )
        self.assertEqual(trades[0].executed_at, executed_at)
        self.assertEqual(Decimal(metrics["trading"]["total_costs"]), Decimal("12"))
        self.assertEqual(Decimal(metrics["trading"]["total_fees"]), Decimal("10"))
        self.assertEqual(Decimal(metrics["trading"]["total_slippage"]), Decimal("2"))

    def test_episode_summary_has_trade_level_attribution(self) -> None:
        buy = transition(
            action="buy",
            sequence=1,
            quantity=10,
            execution_status="filled",
            after_equity=100000.0,
        )
        sell = transition(
            action="sell",
            sequence=2,
            quantity=10,
            execution_status="filled",
            before_position=10,
            after_position=0,
            after_equity=100100.0,
            realized=100.0,
        )
        service = RewardService(
            self.path,
            transition_reader=lambda episode_id: [buy, sell],
            bar_loader=lambda config: self.bars,
        )
        try:
            result = service.calculate_episode("ep_trade_attribution", persist=False)
            self.assertEqual(result["summary"]["trade_count"], 2)
            self.assertEqual(
                [row["action"] for row in result["summary"]["trade_attribution"]],
                ["buy", "sell"],
            )
        finally:
            service.close()

    def test_reward_results_are_persisted_during_evaluation(self) -> None:
        service = self.service()
        service.evaluate(
            {
                "name": "persist",
                "environment": self.payload,
                "policies": ["no_action"],
                "split": "all",
                "max_transitions": 4,
            }
        )
        self.assertGreater(service.store.status()["reward_results"], 0)
        self.assertGreater(service.store.status()["policy_evaluations"], 0)
        service.close()

    def test_evaluation_cancellation_is_bounded_and_persisted(self) -> None:
        service = self.service()
        service.request_cancel("req_cancel")
        result = service.evaluate(
            {
                "name": "cancelled",
                "environment": self.payload,
                "policies": ["no_action", "rule_based"],
                "split": "all",
                "max_transitions": 20,
            },
            request_id="req_cancel",
        )
        self.assertTrue(result["cancelled"])
        self.assertTrue(all(row["status"] == "cancelled" for row in result["results"]))
        self.assertTrue(all(row["transitions"] == 0 for row in result["results"]))
        service.close()

    def test_cancellation_registry_is_bounded(self) -> None:
        service = self.service()
        try:
            for index in range(128):
                service.request_cancel(f"cancel_{index}")
            with self.assertRaises(RewardValidationError):
                service.request_cancel("cancel_overflow")
        finally:
            service.close()

    def test_concurrent_evaluations_are_bounded(self) -> None:
        service = self.service()
        try:
            service._acquire_evaluation()
            service._acquire_evaluation()
            with self.assertRaises(PolicyEvaluationError):
                service._acquire_evaluation()
            service._release_evaluation()
            service._release_evaluation()
        finally:
            service.close()


class ApplicationIsolationTests(unittest.TestCase):
    def test_reward_package_has_no_production_authority_imports(self) -> None:
        root = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "novacontrol"
            / "novamarket"
            / "rewards"
        )
        forbidden = {
            "ExecutionEngine",
            "ExecutionStore",
            "PortfolioEngine",
            "PortfolioStore",
            "RiskEngine",
            "AutonomyRuntime",
        }
        found: set[str] = set()
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in forbidden:
                    found.add(node.id)
                if isinstance(node, ast.Attribute) and node.attr in forbidden:
                    found.add(node.attr)
                if isinstance(node, ast.ImportFrom) and node.module:
                    for name in forbidden:
                        if name in node.module:
                            found.add(name)
        self.assertEqual(found, set())

    def test_reward_service_contract_states_advisory_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = RewardService(
                Path(directory) / "rl.sqlite3",
                transition_reader=lambda episode_id: [],
                bar_loader=lambda config: price_bars(10),
            )
            contract = service.contract()
            self.assertFalse(contract["production_authority"])
            self.assertTrue(contract["advisory_only"])
            self.assertFalse(contract["normalization"]["learned_parameters"])
            status = service.status()
            self.assertFalse(status["isolation"]["automatic_policy_promotion"])
            self.assertFalse(status["isolation"]["nm10_order_submission"])
            self.assertFalse(status["isolation"]["production_portfolio_mutation"])
            service.close()

    def test_reward_episode_calculation_and_store_recovery(self) -> None:
        transitions = [
            transition(action="hold", sequence=1),
            transition(
                action="buy",
                sequence=2,
                before_equity=100000,
                after_equity=100200,
                forecast_payload=forecast(START),
                quantity=100,
                execution_status="filled",
                fees=5,
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rl.sqlite3"
            service = RewardService(
                path,
                transition_reader=lambda episode_id: transitions,
                bar_loader=lambda config: price_bars(10),
            )
            result = service.calculate_episode("ep_fixture")
            self.assertEqual(result["summary"]["reward_count"], 2)
            self.assertIn("result_hash", result["rewards"][0])
            service.close()
            reopened = RewardService(
                path,
                transition_reader=lambda episode_id: transitions,
                bar_loader=lambda config: price_bars(10),
            )
            stored = reopened.episode_rewards("ep_fixture")
            self.assertEqual(stored["total"], 2)
            self.assertTrue(stored["append_only"])
            reopened.close()

    def test_invalid_action_records_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = RewardService(
                Path(directory) / "rl.sqlite3",
                transition_reader=lambda episode_id: [
                    transition(action="buy", execution_status="rejected", quantity=0)
                ],
                bar_loader=lambda config: price_bars(10),
            )
            result = service.calculate_episode("ep_fixture")
            decision = result["rewards"][0]["components"]["decision_quality"]
            self.assertEqual(decision["raw_value"], -1.0)
            service.close()


def math_is_finite(value: float) -> bool:
    import math

    return math.isfinite(value)


@unittest.skipIf(TestClient is None, "httpx is required for the API integration test")
class RewardApplicationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from test_web_api import _IsolatedApiTestCase

        self._base = _IsolatedApiTestCase(methodName="runTest")
        self._base.setUp()
        self.client = self._base._client
        self.bars = price_bars()

    def tearDown(self) -> None:
        self._base.tearDown()

    def test_api_status_config_calculate_evaluate_and_baselines(self) -> None:
        from novacontrol.novamarket.service import NovaMarketService

        with (
            mock.patch.object(NovaMarketService, "_rl_bars", return_value=self.bars),
            mock.patch.object(NovaMarketService, "_rl_feature_snapshot", return_value=None),
            mock.patch.object(NovaMarketService, "_rl_forecast_snapshot", return_value=None),
        ):
            status = self.client.get("/market/rewards/status")
            self.assertEqual(status.status_code, 200, status.text)
            self.assertEqual(status.json()["phase"], "PI-14")
            contracts = self.client.get("/market/rewards/contracts")
            self.assertEqual(contracts.status_code, 200, contracts.text)
            self.assertFalse(contracts.json()["production_authority"])
            baselines = self.client.get("/market/rewards/baselines")
            self.assertEqual(baselines.status_code, 200)
            self.assertEqual(len(baselines.json()["policies"]), 4)
            cancel = self.client.post(
                "/market/rewards/evaluations/cancel", json={"request_id": "api-cancel"}
            )
            self.assertEqual(cancel.status_code, 200, cancel.text)
            cancelled = self.client.post(
                "/market/rewards/evaluations",
                json={
                    "name": "API cancelled",
                    "environment": {
                        **environment_payload(self.bars),
                        "name": "PI-14 API environment",
                        "max_steps": 8,
                    },
                    "policies": ["no_action"],
                    "split": "all",
                    "max_transitions": 8,
                    "request_id": "api-cancel",
                },
            )
            self.assertEqual(cancelled.status_code, 200, cancelled.text)
            self.assertTrue(cancelled.json()["cancelled"])

            created = self.client.post(
                "/market/rewards/configurations",
                json={"name": "API experimental", "version": "api-1"},
            )
            self.assertEqual(created.status_code, 200, created.text)
            config_id = created.json()["configuration"]["config_id"]
            self.assertEqual(
                self.client.get(f"/market/rewards/configurations/{config_id}").status_code, 200
            )

            environment = self.client.post(
                "/market/rl/environments",
                json={
                    **environment_payload(self.bars),
                    "name": "PI-14 API environment",
                    "max_steps": 8,
                },
            )
            self.assertEqual(environment.status_code, 200, environment.text)
            environment_id = environment.json()["environment_id"]
            started = self.client.post(
                f"/market/rl/environments/{environment_id}/episodes", json={}
            )
            self.assertEqual(started.status_code, 200, started.text)
            episode_id = started.json()["episode"]["episode_id"]
            self.client.post(
                f"/market/rl/episodes/{episode_id}/step",
                json={"action_type": "wait"},
            )
            calculated = self.client.post(
                f"/market/rewards/episodes/{episode_id}/calculate",
                json={"config_id": config_id},
            )
            self.assertEqual(calculated.status_code, 200, calculated.text)
            self.assertEqual(calculated.json()["summary"]["reward_count"], 1)
            stored = self.client.get(f"/market/rewards/episodes/{episode_id}")
            self.assertEqual(stored.status_code, 200)
            self.assertEqual(stored.json()["total"], 1)

            evaluated = self.client.post(
                "/market/rewards/evaluations",
                json={
                    "name": "API comparison",
                    "environment": {
                        **environment_payload(self.bars),
                        "name": "PI-14 API environment",
                        "max_steps": 8,
                    },
                    "policies": ["no_action", "rule_based"],
                    "split": "all",
                    "max_transitions": 8,
                },
            )
            self.assertEqual(evaluated.status_code, 200, evaluated.text)
            body = evaluated.json()
            self.assertTrue(body["conditions_identical"])
            self.assertEqual(len(body["evaluation_ids"]), 2)
            self.assertEqual(self.client.get("/market/rewards/evaluations").status_code, 200)
            self.assertEqual(
                self.client.get(
                    f"/market/rewards/evaluations/{body['evaluation_ids'][0]}"
                ).status_code,
                200,
            )
            self.assertEqual(self.client.get("/market/rewards/comparisons").status_code, 200)

    def test_api_unknown_ids_and_no_production_mutation_routes(self) -> None:
        missing = self.client.get("/market/rewards/configurations/rwc_missing")
        self.assertEqual(missing.status_code, 404)
        paths = {
            route.path
            for route in self.client.app.routes
            if getattr(route, "path", "").startswith("/market/rewards")
        }
        self.assertTrue(paths)
        for path in paths:
            self.assertFalse(
                any(
                    word in path
                    for word in ("execute", "order", "portfolio", "autonomy", "promote", "activate")
                )
            )


class FrontendIntegrationTests(unittest.TestCase):
    def test_reward_page_is_registered_and_uses_reward_routes_only(self) -> None:
        root = Path(__file__).resolve().parent.parent / "web" / "src"
        page = (root / "pages" / "RewardEvaluation.tsx").read_text(encoding="utf-8")
        nav = (root / "nav.ts").read_text(encoding="utf-8")
        app = (root / "App.tsx").read_text(encoding="utf-8")
        self.assertIn("RewardEvaluationPage", app)
        self.assertIn('case "rewards"', app)
        self.assertIn('id: "rewards"', nav)
        self.assertIn("/market/rewards/configurations", page)
        self.assertIn("/market/rewards/evaluations", page)
        self.assertIn("/market/rewards/evaluations/cancel", page)
        self.assertIn("request_id", page)
        self.assertIn("Cancel evaluation", page)
        self.assertIn("/market/rewards/episodes", page)
        self.assertIn("historical simulation", page.lower())
        self.assertIn("not guaranteed future performance", page.lower())
        for forbidden in (
            "/market/execution",
            "/market/autonomy",
            "/market/portfolios",
            "/command/execute",
        ):
            self.assertNotIn(forbidden, page)


if __name__ == "__main__":
    unittest.main()


class EvaluationCoverageTests(unittest.TestCase):
    """Coverage counts evidence the environment can read, never key presence."""

    def setUp(self) -> None:
        self.bars = tuple(price_bars(6))
        self.config = RLEnvironmentConfig.from_mapping(environment_payload(self.bars))

    def dataset(
        self,
        features: Mapping[datetime, Mapping[str, Any]] | None = None,
        forecasts: Mapping[datetime, Mapping[str, Any]] | None = None,
    ) -> EvaluationDataset:
        return EvaluationDataset(
            config=self.config,
            split="all",
            bars=self.bars,
            features=dict(features or {}),
            forecasts=dict(forecasts or {}),
            bars_hash="bars_hash",
            start=self.bars[0].observed_at,
            end=self.bars[-1].observed_at,
        )

    @staticmethod
    def feature_snapshot(at: datetime, **values: Any) -> dict[str, Any]:
        return {
            "as_of": at.isoformat(),
            "feature_set_id": "fs_test",
            "values": dict(values),
            "statuses": {name: "present" for name in values},
        }

    def test_empty_feature_snapshots_never_report_full_coverage(self) -> None:
        features = {
            bar.observed_at: self.feature_snapshot(bar.observed_at) for bar in self.bars
        }
        coverage = self.dataset(features=features).to_dict()["coverage"]
        self.assertEqual(coverage["features"], 0)
        self.assertEqual(coverage["feature_ratio"], 0.0)
        self.assertEqual(coverage["feature_breakdown"]["available"], 0)
        self.assertEqual(coverage["feature_breakdown"]["missing"], len(self.bars))

    def test_all_null_feature_values_are_not_coverage(self) -> None:
        features = {
            bar.observed_at: self.feature_snapshot(bar.observed_at, rsi_14=None)
            for bar in self.bars
        }
        coverage = self.dataset(features=features).to_dict()["coverage"]
        self.assertEqual(coverage["features"], 0)
        self.assertEqual(coverage["feature_ratio"], 0.0)
        self.assertEqual(coverage["feature_breakdown"]["available"], 0)
        self.assertEqual(
            coverage["feature_breakdown"]["invalid"], len(self.bars)
        )

    def test_a_point_in_time_snapshot_with_a_value_is_available(self) -> None:
        features = {
            bar.observed_at: self.feature_snapshot(bar.observed_at, rsi_14=55.0)
            for bar in self.bars
        }
        coverage = self.dataset(features=features).to_dict()["coverage"]
        self.assertEqual(coverage["features"], len(self.bars))
        self.assertEqual(coverage["feature_ratio"], 1.0)
        self.assertEqual(coverage["feature_breakdown"]["available"], len(self.bars))

    def test_a_feature_row_that_post_dates_the_bar_is_missing(self) -> None:
        features = {
            bar.observed_at: self.feature_snapshot(
                bar.observed_at + timedelta(days=1), rsi_14=55.0
            )
            for bar in self.bars
        }
        coverage = self.dataset(features=features).to_dict()["coverage"]
        self.assertEqual(coverage["features"], 0)
        self.assertEqual(coverage["feature_breakdown"]["missing"], len(self.bars))

    def test_a_forecast_expired_at_the_observation_is_stale(self) -> None:
        forecasts = {
            bar.observed_at: {
                **forecast(bar.observed_at),
                "validity": "expired",
            }
            for bar in self.bars
        }
        coverage = self.dataset(forecasts=forecasts).to_dict()["coverage"]
        self.assertEqual(coverage["forecasts"], 0)
        self.assertEqual(coverage["forecast_ratio"], 0.0)
        self.assertEqual(coverage["forecast_breakdown"]["stale"], len(self.bars))

    def test_a_forecast_aged_past_the_replay_bound_is_stale(self) -> None:
        forecasts = {
            bar.observed_at: {
                **forecast(bar.observed_at),
                "generated_at": (bar.observed_at - timedelta(days=7)).isoformat(),
                "validity": "stale",
            }
            for bar in self.bars
        }
        coverage = self.dataset(forecasts=forecasts).to_dict()["coverage"]
        self.assertEqual(coverage["forecasts"], 0)
        self.assertEqual(coverage["forecast_breakdown"]["stale"], len(self.bars))

    def test_an_undated_forecast_is_missing(self) -> None:
        forecasts = {
            bar.observed_at: {
                key: value
                for key, value in forecast(bar.observed_at).items()
                if key != "generated_at"
            }
            for bar in self.bars
        }
        coverage = self.dataset(forecasts=forecasts).to_dict()["coverage"]
        self.assertEqual(coverage["forecasts"], 0)
        self.assertEqual(coverage["forecast_breakdown"]["missing"], len(self.bars))

    def test_partial_coverage_is_reported_as_a_fraction(self) -> None:
        forecasts = {
            bar.observed_at: forecast(bar.observed_at) for bar in self.bars[:2]
        }
        coverage = self.dataset(forecasts=forecasts).to_dict()["coverage"]
        self.assertEqual(coverage["forecasts"], 2)
        self.assertEqual(coverage["forecast_ratio"], round(2 / len(self.bars), 6))
        self.assertEqual(
            coverage["forecast_breakdown"]["missing"], len(self.bars) - 2
        )
        self.assertIn("not stale", coverage["forecast_basis"])

    def test_disabled_sources_are_reported_as_disabled(self) -> None:
        config = RLEnvironmentConfig.from_mapping(
            environment_payload(self.bars, include_features=False, include_forecasts=False)
        )
        dataset = EvaluationDataset(
            config=config,
            split="all",
            bars=self.bars,
            features={},
            forecasts={},
            bars_hash="bars_hash",
            start=self.bars[0].observed_at,
            end=self.bars[-1].observed_at,
        )
        coverage = dataset.to_dict()["coverage"]
        self.assertEqual(coverage["feature_breakdown"]["disabled"], len(self.bars))
        self.assertEqual(coverage["forecast_breakdown"]["disabled"], len(self.bars))
        # Disabled is not 0% coverage: the source was not requested at all.
        self.assertIsNone(coverage["feature_ratio"])
        self.assertEqual(coverage["feature_ratio_status"], "not_applicable")
        self.assertIsNone(coverage["forecast_ratio"])
        self.assertEqual(coverage["forecast_ratio_status"], "not_applicable")

    def test_only_the_disabled_source_is_not_applicable(self) -> None:
        config = RLEnvironmentConfig.from_mapping(
            environment_payload(self.bars, include_features=False, include_forecasts=True)
        )
        dataset = EvaluationDataset(
            config=config,
            split="all",
            bars=self.bars,
            features={},
            forecasts={
                bar.observed_at: forecast(bar.observed_at) for bar in self.bars
            },
            bars_hash="bars_hash",
            start=self.bars[0].observed_at,
            end=self.bars[-1].observed_at,
        )
        coverage = dataset.to_dict()["coverage"]
        self.assertEqual(coverage["feature_breakdown"]["disabled"], len(self.bars))
        self.assertIsNone(coverage["feature_ratio"])
        self.assertEqual(coverage["feature_ratio_status"], "not_applicable")
        # The source that was requested still reports a measured ratio.
        self.assertEqual(coverage["forecasts"], len(self.bars))
        self.assertEqual(coverage["forecast_ratio"], 1.0)
        self.assertEqual(coverage["forecast_ratio_status"], "measured")
