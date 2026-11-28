"""PI-15 focused verification: isolated DQN training, validation and artifacts."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None  # type: ignore[assignment]

from novacontrol.novamarket.agent_training import TrainingConfig, TrainingManager
from novacontrol.novamarket.agent_training import training as training_module
from novacontrol.novamarket.agent_training.stability import DEFAULT_STABILITY_LIMITS
from novacontrol.novamarket.agent_training.agent import (
    ACTION_NAMES,
    DQNAgent,
    ObservationEncoder,
    TrainedPolicy,
)
from novacontrol.novamarket.agent_training.models import (
    PolicyArtifact,
    TrainingResourceError,
    TrainingRun,
    TrainingStateError,
    TrainingValidationError,
)
from novacontrol.novamarket.backtesting.core import HistoricalBar
from novacontrol.novamarket.rewards import RewardService, default_reward_config
from novacontrol.novamarket.rl import (
    RLEnvironment,
    RLEnvironmentConfig,
    RLInvalidActionError,
)
from test_novamarket_pi14 import environment_payload

START = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)


def price_bars(count: int = 90) -> list[HistoricalBar]:
    bars: list[HistoricalBar] = []
    at = START
    index = 0
    while len(bars) < count:
        if at.weekday() < 5:
            base = 100.0 + index * 0.6
            open_price = Decimal(str(round(base, 2)))
            close = Decimal(str(round(base + 0.4, 2)))
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


def mixed_regime_bars(count: int = 90) -> list[HistoricalBar]:
    """A fixture whose regime changes inside the validation and test windows.

    ``price_bars`` rises every bar, so a run sees a single trend and a regime
    slice could only prove plumbing.  Each split here starts with a two-bar
    drift, then a directional three-bar move followed by the opposite three-bar
    move, so the validation window reads a decline and then a volatile stretch
    and the test window an advance and then a volatile stretch — each from that
    split's own evidence.
    """
    moves = (
        [Decimal("0.010")] * 20  # 0-19: train advance
        + [Decimal("-0.025")] * 10  # 20-29: train decline
        + [Decimal("0.025")] * 10  # 30-39: train recovery
        + [Decimal("-0.015")] * 10  # 40-49: train drift into the split
        + [Decimal("-0.025")] * 3  # 50-52: validation opens with a decline
        + [Decimal("0.030")] * 3  # 53-55: reversal makes the window volatile
        + [Decimal("0.005")] * 14  # 56-69: calm into the test split
        + [Decimal("0.025")] * 3  # 70-72: test opens with an advance
        + [Decimal("-0.030")] * 3  # 73-75: reversal makes the window volatile
        + [Decimal("-0.005")] * 14  # 76-89: calm tail
    )
    bars: list[HistoricalBar] = []
    at = START
    price = Decimal("100.00")
    index = 0
    while len(bars) < count:
        if at.weekday() < 5:
            open_price = price
            price = (open_price * (Decimal("1") + moves[index])).quantize(Decimal("0.01"))
            bars.append(
                HistoricalBar(
                    instrument_id="XNSE:ALPHA",
                    observed_at=at,
                    open=open_price,
                    high=max(open_price, price) + Decimal("1"),
                    low=min(open_price, price) - Decimal("1"),
                    close=price,
                    volume=Decimal("250000"),
                    source="nm02",
                )
            )
            index += 1
        at += timedelta(days=1)
    return bars


def forecast(at: datetime) -> dict[str, Any]:
    return {
        "forecast_id": f"fc_{at.date().isoformat()}",
        "generated_at": at.isoformat(),
        "direction": "up",
        "expected_return": 0.02,
        "predicted_return": 0.02,
        "p_up": 0.62,
        "p_down": 0.25,
        "probability_status": "calibrated",
        "lower_return": 0.0,
        "upper_return": 0.04,
        "uncertainty": "low",
        "uncertainty_width": 0.04,
        "disagreement": 0.05,
        "horizon": "1d",
        "validity": "valid",
        "available": True,
        "model_version": "ensemble-test",
        "feature_set_version": "pi04-test",
        "dataset_version": "ds-test-v1",
    }


def config_payload(bars: Sequence[HistoricalBar], **changes: Any) -> dict[str, Any]:
    environment = environment_payload(bars)
    payload: dict[str, Any] = {
        "name": "PI-15 focused DQN",
        "algorithm": "dqn",
        "environment": environment,
        "reward_config": default_reward_config().to_dict(),
        "splits": {
            "train": {
                "label": "train",
                "start": bars[0].observed_at.isoformat(),
                "end": bars[49].observed_at.isoformat(),
            },
            "validation": {
                "label": "validation",
                "start": bars[50].observed_at.isoformat(),
                "end": bars[69].observed_at.isoformat(),
            },
            "test": {
                "label": "test",
                "start": bars[70].observed_at.isoformat(),
                "end": bars[89].observed_at.isoformat(),
            },
        },
        "max_episodes": 2,
        "max_steps": 8,
        "episode_length": 5,
        "batch_size": 2,
        "learning_rate": 0.001,
        "gamma": 0.99,
        "epsilon_start": 1.0,
        "epsilon_end": 0.05,
        "epsilon_decay": 0.995,
        "hidden_layers": [8],
        "activation": "relu",
        "optimizer": "adam",
        "target_update_interval": 5,
        "replay_capacity": 100,
        "checkpoint_interval": 4,
        "validation_interval": 1,
        "validation_max_transitions": 6,
        "test_max_transitions": 6,
        "max_wall_seconds": 60,
        "max_memory_mb": 1024,
        "seed": 7,
        "description": "Deterministic focused training fixture.",
    }
    payload.update(changes)
    return payload


class ModelContractTests(unittest.TestCase):
    def test_dqn_initialization_is_seeded_and_actions_are_discrete(self) -> None:
        encoder = ObservationEncoder()
        state = [0.1] * encoder.dimension
        first = DQNAgent(input_dim=encoder.dimension, hidden_layers=(8,), seed=11, encoder=encoder)
        second = DQNAgent(input_dim=encoder.dimension, hidden_layers=(8,), seed=11, encoder=encoder)
        self.assertEqual(first.q_values(state), second.q_values(state))
        action = first.act(state)
        self.assertIn(action, range(len(ACTION_NAMES)))
        self.assertEqual(first.action_count, 5)

    def test_encoder_is_bounded_and_deterministic(self) -> None:
        encoder = ObservationEncoder(feature_names=("momentum", "breadth"))
        observation = {
            "market": {"close": 100.0, "return_1": 0.04, "support_distance_pct": 0.01},
            "portfolio": {"cash": 100000.0, "equity": 100000.0, "exposure": 0.0},
            "forecasts": {"expected_return": 0.02, "p_up": 0.6, "p_down": 0.2},
            "features": {"values": {"momentum": 0.3, "breadth": -0.2}},
        }
        values = encoder.encode(observation)
        self.assertEqual(values, encoder.encode(observation))
        self.assertTrue(all(-1.0 <= value <= 1.0 for value in values))
        self.assertEqual(len(values), encoder.dimension)

    def test_configuration_rejects_overlapping_splits(self) -> None:
        bars = price_bars()
        payload = config_payload(bars)
        payload["splits"]["validation"] = {
            "label": "validation",
            "start": bars[45].observed_at.isoformat(),
            "end": bars[69].observed_at.isoformat(),
        }
        with self.assertRaises(TrainingValidationError):
            TrainingConfig.from_mapping(payload)

    def test_configuration_rejects_unknown_algorithm_and_field(self) -> None:
        bars = price_bars()
        with self.assertRaises(TrainingValidationError):
            TrainingConfig.from_mapping(config_payload(bars, algorithm="ppo"))
        payload = config_payload(bars)
        payload["not_a_hyperparameter"] = 1
        with self.assertRaises(TrainingValidationError):
            TrainingConfig.from_mapping(payload)
        payload = config_payload(bars)
        payload["feature_set_version"] = "unverified-feature-set-v9"
        with self.assertRaises(TrainingValidationError):
            TrainingConfig.from_mapping(payload)


class TrainedPolicyActionMaskTests(unittest.TestCase):
    """The adapter must never emit a structurally impossible trade."""

    def decision(
        self,
        q_values: Sequence[float],
        *,
        portfolio: dict[str, Any],
        environment: dict[str, Any] | None = None,
    ) -> Any:
        encoder = ObservationEncoder()
        agent = DQNAgent(
            input_dim=encoder.dimension,
            hidden_layers=(8,),
            seed=11,
            encoder=encoder,
        )
        policy = TrainedPolicy(agent, name="masked-test-policy")
        with mock.patch.object(agent, "q_values", return_value=list(q_values)):
            return policy.select(
                observation={"instrument_id": "XNSE:ALPHA"},
                portfolio=portfolio,
                environment=environment
                or {
                    "max_position_weight": "0.25",
                    "max_gross_exposure": "1.00",
                },
                history=(),
            )

    @staticmethod
    def portfolio(*, quantity: str, market_value: str) -> dict[str, Any]:
        positions = []
        if Decimal(quantity) > 0:
            positions.append(
                {
                    "instrument_id": "XNSE:ALPHA",
                    "quantity": quantity,
                    "market_value": market_value,
                    "last_price": "100.00",
                }
            )
        return {
            "cash": "75000.00",
            "equity": "100000.00",
            "positions": positions,
        }

    def test_buy_is_masked_at_the_position_cap(self) -> None:
        decision = self.decision(
            [10.0, 0.0, 1.0, 0.0, 0.0],
            portfolio=self.portfolio(quantity="250.000000", market_value="25000.00"),
        )
        self.assertEqual(decision.action["action_type"], "hold")
        self.assertIn("buy is unavailable at the position/exposure cap", decision.rationale)

    def test_sell_is_masked_without_a_position(self) -> None:
        decision = self.decision(
            [0.0, 10.0, 1.0, 0.0, 0.0],
            portfolio=self.portfolio(quantity="0", market_value="0.00"),
        )
        self.assertEqual(decision.action["action_type"], "hold")
        self.assertIn("sell is unavailable without a simulated position", decision.rationale)

    def test_buy_remains_legal_below_the_cap(self) -> None:
        decision = self.decision(
            [10.0, 0.0, 1.0, 0.0, 0.0],
            portfolio=self.portfolio(quantity="100.000000", market_value="10000.00"),
        )
        self.assertEqual(decision.action["action_type"], "buy")
        self.assertNotIn("buy is unavailable", decision.rationale)

    def test_sell_remains_legal_with_a_position(self) -> None:
        decision = self.decision(
            [0.0, 10.0, 1.0, 0.0, 0.0],
            portfolio=self.portfolio(quantity="250.000000", market_value="25000.00"),
        )
        self.assertEqual(decision.action["action_type"], "sell")

    def test_the_stricter_gross_exposure_cap_also_masks_buy(self) -> None:
        decision = self.decision(
            [10.0, 0.0, 1.0, 0.0, 0.0],
            portfolio=self.portfolio(quantity="250.000000", market_value="25000.00"),
            environment={
                "max_position_weight": "0.50",
                "max_gross_exposure": "0.25",
            },
        )
        self.assertEqual(decision.action["action_type"], "hold")

    def test_environment_risk_and_execution_checks_remain_authoritative(self) -> None:
        bars = price_bars()
        payload = environment_payload(
            bars,
            max_position_weight="0.25",
            max_order_notional="25000.00",
        )
        config = RLEnvironmentConfig.from_mapping(payload)
        env = RLEnvironment(config, bars)
        env.reset("mask-authority-buy")
        env.step(
            {
                "action_type": "buy",
                "instrument_id": "XNSE:ALPHA",
                "size_fraction": "0.25",
            }
        )
        self.assertTrue(env.portfolio.positions)
        self.assertGreaterEqual(
            env.portfolio.positions[0].market_value,
            env.portfolio.equity * Decimal("0.25"),
        )
        with self.assertRaisesRegex(RLInvalidActionError, "position/exposure"):
            env.step(
                {
                    "action_type": "buy",
                    "instrument_id": "XNSE:ALPHA",
                    "size_fraction": "0.25",
                }
            )

        flat = RLEnvironment(config, bars)
        flat.reset("mask-authority-sell")
        with self.assertRaisesRegex(RLInvalidActionError, "no simulated position"):
            flat.step({"action_type": "sell", "instrument_id": "XNSE:ALPHA"})


class TrainingFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "novamarket_rl.sqlite3"
        self.bars = price_bars()
        self.forecasts = {bar.observed_at: forecast(bar.observed_at) for bar in self.bars}
        self.rewards = RewardService(
            self.path,
            transition_reader=lambda episode_id: [],
            bar_loader=lambda config: self.bars,
            forecast_loader=lambda config, at: self.forecasts.get(at),
        )
        self.manager = TrainingManager(
            self.path,
            bar_loader=lambda config: self.bars,
            forecast_loader=lambda config, at: self.forecasts.get(at),
            reward_service=self.rewards,
        )

    def tearDown(self) -> None:
        self.manager.close()
        self.rewards.close()
        self.temp.cleanup()

    def start(self, payload: dict[str, Any] | None = None) -> tuple[str, str]:
        result = self.manager.start({"config": payload or config_payload(self.bars)})
        return result["run"]["run_id"], result["policy_id"]

    def wait_run(self, run_id: str, timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.manager.run(run_id)
            if run["status"] in {"completed", "failed", "cancelled"}:
                return run
            time.sleep(0.03)
        self.fail(f"training run {run_id} did not finish within {timeout}s")

    def test_complete_lifecycle_validation_test_and_checkpoints(self) -> None:
        run_id, policy_id = self.start()
        run = self.wait_run(run_id)
        self.assertEqual(run["status"], "completed", run)
        self.assertIn("validation", run["metrics"])
        self.assertIn("test", run["metrics"])
        for key in (
            "reward_variance",
            "rolling_reward",
            "action_frequencies",
            "invalid_action_frequency",
            "dominant_action",
            "training_throughput_steps_per_second",
            "environment_step_latency_ms",
            "training_elapsed_seconds",
        ):
            self.assertIn(key, run["metrics"])
        # The legacy per-condition booleans are gone: the stability monitor's
        # record is the only summary of reward collapse, action repetition and
        # invalid-action spikes, so the healthy case reads from it.
        for legacy in (
            "reward_collapse_warning",
            "action_repetition_warning",
            "invalid_action_spike",
        ):
            self.assertNotIn(legacy, run["metrics"])
        stability = run["metrics"]["stability"]
        self.assertEqual(stability["action"], "ok")
        self.assertEqual(stability["codes"], [])
        self.assertEqual(
            stability["limits"], DEFAULT_STABILITY_LIMITS.to_dict()
        )
        self.assertEqual(run["metrics"]["validation"]["conditions_identical"], True)
        self.assertEqual(run["metrics"]["test"]["test_data_used"], True)
        policy = self.manager.policy(policy_id)
        self.assertEqual(policy["status"], "tested")
        self.assertFalse(policy["production_authority"])
        self.assertFalse(policy["activation_available"])
        self.assertTrue(policy["artifact_hash"])
        self.assertTrue(Path(policy["artifact_path"]).is_file())
        self.assertIn("total_return", policy["test_metrics"]["financial"])
        self.assertIn("max_drawdown", policy["test_metrics"]["risk"])
        self.assertIn("total_costs", policy["test_metrics"]["trading"])
        self.assertIn("action_distribution", policy["test_metrics"]["decision"])
        self.assertEqual(policy["creation_metadata"]["framework"], "pure-python-stdlib")
        self.assertEqual(
            policy["dataset_provenance"]["feature_set_version"],
            "pi13-observation-v1",
        )
        checkpoints = self.manager.checkpoints(run_id)["checkpoints"]
        kinds = {row["kind"] for row in checkpoints}
        self.assertIn("final", kinds)
        self.assertIn("periodic", kinds)
        for row in checkpoints:
            self.assertTrue(row["integrity_hash"])

    def test_changed_weights_are_deterministic_for_same_seed(self) -> None:
        first_run, first_policy = self.start()
        first = self.wait_run(first_run)
        second_run, second_policy = self.start()
        second = self.wait_run(second_run)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "completed")
        self.assertAlmostEqual(
            float(first["metrics"]["validation"]["score"]),
            float(second["metrics"]["validation"]["score"]),
            places=8,
        )
        self.assertEqual(
            first["metrics"]["reward_history"],
            second["metrics"]["reward_history"],
        )
        self.assertNotEqual(first_policy, second_policy)
        comparison = self.manager.compare_policies(
            {"policy_ids": [first_policy, second_policy]}
        )["comparison"]
        self.assertTrue(comparison["conditions_identical"])
        self.assertEqual(len(comparison["results"]), 2)
        self.assertEqual(comparison["production_authority"], False)

    def test_resume_from_checkpoint_starts_a_new_run(self) -> None:
        run_id, _policy_id = self.start()
        self.wait_run(run_id)
        checkpoint = next(
            row
            for row in self.manager.checkpoints(run_id)["checkpoints"]
            if row["kind"] == "periodic"
        )
        resumed = self.manager.resume_checkpoint(checkpoint["checkpoint_id"])
        self.assertNotEqual(resumed["run"]["run_id"], run_id)
        self.assertGreaterEqual(resumed["run"]["current_step"], int(checkpoint["step"]))
        final = self.wait_run(resumed["run"]["run_id"])
        self.assertEqual(final["status"], "completed")

    def test_numerical_instability_fails_and_rejects_policy(self) -> None:
        with mock.patch.object(DQNAgent, "train_step", return_value=float("inf")):
            run_id, policy_id = self.start()
            run = self.wait_run(run_id)
        self.assertEqual(run["status"], "failed")
        self.assertIn("non-finite", run["error"])
        self.assertEqual(self.manager.policy(policy_id)["status"], "rejected")
        retried = self.manager.retry(run_id)
        self.assertNotEqual(retried["run"]["run_id"], run_id)
        retried_run = self.wait_run(retried["run"]["run_id"])
        self.assertEqual(retried_run["status"], "completed")
        self.assertEqual(
            self.manager.policy(retried["policy_id"])["status"],
            "tested",
        )

    def test_concurrent_worker_limit_is_enforced(self) -> None:
        long_payload = config_payload(
            self.bars,
            max_episodes=8,
            max_steps=2500,
            episode_length=40,
            validation_interval=20,
            max_wall_seconds=120,
        )
        first_run, _policy_id = self.start(long_payload)
        with self.assertRaises(TrainingResourceError):
            self.start(long_payload)
        self.manager.cancel(first_run)
        self.wait_run(first_run, timeout=20)

    def test_manual_checkpoint_is_written_at_a_safe_boundary(self) -> None:
        payload = config_payload(
            self.bars,
            max_episodes=8,
            max_steps=1200,
            episode_length=40,
            checkpoint_interval=200,
            validation_interval=20,
            max_wall_seconds=120,
        )
        run_id, _policy_id = self.start(payload)
        self.manager.request_checkpoint(run_id)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            kinds = {row["kind"] for row in self.manager.checkpoints(run_id)["checkpoints"]}
            if "manual" in kinds:
                break
            time.sleep(0.03)
        else:
            self.fail("manual checkpoint was not written")
        self.manager.cancel(run_id)
        self.wait_run(run_id, timeout=20)

    def test_checkpoint_integrity_rejects_tampering(self) -> None:
        run_id, _policy_id = self.start()
        self.wait_run(run_id)
        checkpoint = self.manager.checkpoints(run_id)["checkpoints"][0]
        path = Path(checkpoint["path"])
        encoded = json.loads(path.read_text(encoding="utf-8"))
        encoded["agent"]["online"]["weights"][0][0][0] = 999.0
        path.write_text(json.dumps(encoded), encoding="utf-8")
        with self.assertRaises(TrainingValidationError):
            self.manager.resume_checkpoint(checkpoint["checkpoint_id"])

    def test_pause_resume_and_cancel(self) -> None:
        payload = config_payload(
            self.bars,
            max_episodes=8,
            max_steps=2500,
            episode_length=40,
            validation_interval=20,
            checkpoint_interval=100,
            max_wall_seconds=120,
        )
        run_id, _policy_id = self.start(payload)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            run = self.manager.run(run_id)
            if run["status"] == "running" and run["current_step"] >= 2:
                break
            time.sleep(0.02)
        else:
            self.fail("run did not reach a running step before pause")
        self.manager.pause(run_id)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            run = self.manager.run(run_id)
            if run["status"] == "paused":
                break
            time.sleep(0.03)
        else:
            self.fail("run did not pause safely")
        kinds = {row["kind"] for row in self.manager.checkpoints(run_id)["checkpoints"]}
        self.assertIn("paused", kinds)
        self.manager.resume(run_id)
        self.manager.cancel(run_id)
        cancelled = self.wait_run(run_id, timeout=20)
        self.assertEqual(cancelled["status"], "cancelled")

    def test_resource_wall_limit_fails_without_claiming_success(self) -> None:
        payload = config_payload(
            self.bars,
            max_episodes=20,
            max_steps=4000,
            episode_length=40,
            max_wall_seconds=1,
            validation_interval=20,
            checkpoint_interval=200,
        )
        class AdvancingClock:
            def __init__(self) -> None:
                self.value = 0.0

            def monotonic(self) -> float:
                self.value += 2.0
                return self.value

        with mock.patch.object(training_module, "time", AdvancingClock()):
            run_id, policy_id = self.start(payload)
            run = self.wait_run(run_id, timeout=20)
        self.assertEqual(run["status"], "failed")
        self.assertIn("max_wall_seconds", run["error"])
        self.assertEqual(self.manager.policy(policy_id)["status"], "rejected")

    def test_final_validation_failure_is_not_reported_as_completed(self) -> None:
        payload = config_payload(
            self.bars,
            max_episodes=1,
            max_steps=10,
            episode_length=3,
            validation_interval=99,
            checkpoint_interval=100,
        )
        with mock.patch.object(
            self.manager,
            "_evaluate_for_split",
            side_effect=RuntimeError("validation outage"),
        ):
            run_id, policy_id = self.start(payload)
            run = self.wait_run(run_id)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["stop_reason"], "validation_failed")
        self.assertIn("validation outage", run["error"])
        policy = self.manager.policy(policy_id)
        self.assertEqual(policy["status"], "trained")
        self.assertFalse(policy["production_authority"])

    def test_hyperparameter_search_is_bounded_and_never_uses_test(self) -> None:
        result = self.manager.optimize(
            {
                "config": config_payload(self.bars),
                "method": "grid",
                "trials": 1,
                "space": {"hidden_layers": [[8]]},
            }
        )
        search_id = result["search"]["search_id"]
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            search = self.manager.search(search_id)["search"]
            if search["status"] == "completed":
                break
            time.sleep(0.05)
        else:
            self.fail("bounded hyperparameter search did not complete")
        self.assertEqual(search["test_data_used"], False)
        self.assertEqual(len(search["trials"]), 1)
        trials = self.manager.trials(search_id)["trials"]
        self.assertEqual(len(trials), 1)
        self.assertEqual(trials[0]["status"], "completed")

    def test_interrupted_run_is_recovered_as_failed(self) -> None:
        interrupted = TrainingRun(
            run_id="interrupted-run",
            name="interrupted test",
            status="running",
            seed=7,
            policy_id="interrupted-policy",
        )
        self.manager._store.save_run(interrupted)
        self.manager._store.save_policy(
            PolicyArtifact(
                policy_id="interrupted-policy",
                run_id="interrupted-run",
                status="validation_pending",
            )
        )
        self.manager.close()
        recovered = TrainingManager(
            self.path,
            bar_loader=lambda config: self.bars,
            forecast_loader=lambda config, at: self.forecasts.get(at),
            reward_service=self.rewards,
        )
        try:
            row = recovered.run("interrupted-run")
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["stop_reason"], "interrupted")
            self.assertIn("checkpoint recovery", row["error"])
            self.assertEqual(
                recovered.policy("interrupted-policy")["status"], "rejected"
            )
        finally:
            recovered.close()

    def test_financial_tables_are_absent_from_the_training_database(self) -> None:
        run_id, _policy_id = self.start()
        self.wait_run(run_id)
        isolation = self.manager.status()["isolation"]
        for flag in (
            "production_portfolio_mutation",
            "production_position_mutation",
            "production_ledger_write",
            "production_paper_fill",
            "nm10_order_submission",
            "nm08_bypass",
            "autonomy_mode_change",
            "operator_permission_change",
            "automatic_policy_promotion",
            "live_trading",
            "broker_connection",
            "activation_control",
        ):
            self.assertFalse(isolation[flag], flag)
        connection = sqlite3.connect(self.path)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            connection.close()
        for table in tables:
            lowered = table.lower()
            self.assertFalse(
                any(
                    word in lowered
                    for word in (
                        "production",
                        "portfolio",
                        "order",
                        "ledger",
                        "paper",
                        "autonomy",
                        "wallet",
                        "broker",
                    )
                ),
                table,
            )

    def test_archive_policy_does_not_activate_it(self) -> None:
        run_id, policy_id = self.start()
        self.wait_run(run_id)
        proposal = self.manager.advisory_proposal(
            policy_id,
            {
                "observation": {
                    "instrument_id": "XNSE:ALPHA",
                    "portfolio": {"cash": 100000.0, "equity": 100000.0},
                    "forecasts": {"expected_return": 0.02, "p_up": 0.6},
                    "features": {"values": {"momentum": 0.2}},
                }
            },
        )
        self.assertTrue(proposal["advisory"])
        self.assertIn(proposal["action"]["action_type"], ACTION_NAMES)
        self.assertFalse(proposal["authorizes_action"])
        self.assertFalse(proposal["execution_allowed"])
        self.assertFalse(proposal["orders_possible"])
        self.assertTrue(proposal["risk_review_required"])
        archived = self.manager.archive_policy(policy_id)
        self.assertEqual(archived["policy"]["status"], "archived")
        self.assertFalse(archived["policy"]["activation_available"])
        self.assertFalse(archived["policy"]["production_authority"])
        with self.assertRaises(TrainingStateError):
            self.manager.advisory_proposal(
                policy_id, {"observation": {"instrument_id": "XNSE:ALPHA"}}
            )


class TrainingApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from test_web_api import _IsolatedApiTestCase

        if TestClient is None:
            self.skipTest("httpx not installed")
        self._base = _IsolatedApiTestCase(methodName="runTest")
        self._base.setUp()
        self.client = self._base._client
        self.bars = price_bars()

    def tearDown(self) -> None:
        self._base.tearDown()

    def test_api_status_validation_start_and_artifact_surfaces(self) -> None:
        with (
            mock.patch.object(
                type(self._base._nova.novamarket), "_rl_bars", return_value=self.bars
            ),
            mock.patch.object(
                type(self._base._nova.novamarket), "_rl_feature_snapshot", return_value=None
            ),
            mock.patch.object(
                type(self._base._nova.novamarket), "_rl_forecast_snapshot", return_value=None
            ),
        ):
            status = self.client.get("/market/training/status")
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json()["phase"], "PI-15")
            self.assertFalse(status.json()["production_authority"])

            invalid = self.client.post(
                "/market/training/validate",
                json={"config": {**config_payload(self.bars), "algorithm": "ppo"}},
            )
            self.assertEqual(invalid.status_code, 200)
            self.assertFalse(invalid.json()["valid"])

            config = config_payload(self.bars)
            validation = self.client.post("/market/training/validate", json={"config": config})
            self.assertEqual(validation.status_code, 200, validation.text)
            self.assertTrue(validation.json()["valid"])
            created = self.client.post("/market/training/configurations", json={"config": config})
            self.assertEqual(created.status_code, 200, created.text)
            config_id = created.json()["configuration"]["config_id"]
            started = self.client.post(
                "/market/training/runs",
                json={"config_id": config_id},
            )
            self.assertEqual(started.status_code, 200, started.text)
            run_id = started.json()["run"]["run_id"]
            policy_id = started.json()["policy_id"]
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                run = self.client.get(f"/market/training/runs/{run_id}")
                if run.json()["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.05)
            self.assertEqual(run.json()["status"], "completed", run.text)
            policy = self.client.get(f"/market/training/policies/{policy_id}")
            self.assertEqual(policy.status_code, 200, policy.text)
            self.assertEqual(policy.json()["status"], "tested")
            self.assertFalse(policy.json()["activation_available"])
            checkpoints = self.client.get(f"/market/training/runs/{run_id}/checkpoints")
            self.assertGreaterEqual(checkpoints.json()["count"], 1)
            metrics = self.client.get(f"/market/training/runs/{run_id}/metrics")
            self.assertIn("action_distribution", metrics.json()["metrics"])
            archived = self.client.post(f"/market/training/policies/{policy_id}/archive")
            self.assertEqual(archived.status_code, 200, archived.text)
            self.assertEqual(archived.json()["policy"]["status"], "archived")
            comparisons = self.client.get("/market/training/evaluations")
            self.assertEqual(comparisons.status_code, 200)


if __name__ == "__main__":
    unittest.main()
