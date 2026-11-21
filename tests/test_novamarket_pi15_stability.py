"""PI-15 stability monitoring: reward collapse, action repetition, refused actions.

The thresholds and severities are pinned as pure functions, and the worker's
response is pinned behaviourally: a refused-action spike fails the run and
rejects its artifact, while a collapse or a collapsed policy parks the run in
``paused`` with a checkpoint it can be resumed (or cancelled) from.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from collections import Counter, deque
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from novacontrol.novamarket.agent_training import TrainingManager, training as training_module
from novacontrol.novamarket.agent_training.agent import DQNAgent
from novacontrol.novamarket.agent_training.models import TrainingRun
from novacontrol.novamarket.agent_training.stability import (
    DEFAULT_STABILITY_LIMITS,
    PAUSE,
    StabilityLimits,
    action_repetition_signal,
    assess_stability,
    invalid_action_signal,
    reward_collapse_signal,
    stability_action,
    stability_record,
)
from novacontrol.novamarket.rewards import RewardService
from test_novamarket_pi15 import config_payload, forecast, price_bars


def metrics_with(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "step": 10,
        "reward_history": [],
        "action_distribution": {},
        "invalid_actions": 0,
    }
    base.update(changes)
    return base


class RewardCollapseTests(unittest.TestCase):
    def test_a_halving_reward_window_is_a_collapse(self) -> None:
        signal = reward_collapse_signal([2.0, 2.0, 2.0, 2.0, 0.5, 0.5], DEFAULT_STABILITY_LIMITS)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.code, "reward_collapse")
        self.assertEqual(signal.severity, PAUSE)
        self.assertAlmostEqual(signal.evidence["ratio"], 0.25, places=10)

    def test_a_stable_reward_history_is_silent(self) -> None:
        self.assertIsNone(
            reward_collapse_signal([2.0, 2.0, 2.0, 2.0, 2.0, 2.0], DEFAULT_STABILITY_LIMITS)
        )
        self.assertIsNone(
            reward_collapse_signal([2.0, 2.0, 2.0, 2.0, 1.2, 1.2], DEFAULT_STABILITY_LIMITS)
        )

    def test_too_few_episodes_and_non_positive_baselines_are_silent(self) -> None:
        self.assertIsNone(reward_collapse_signal([2.0, 0.1], DEFAULT_STABILITY_LIMITS))
        # A sign change has no meaningful ratio; the monitor does not invent one.
        self.assertIsNone(
            reward_collapse_signal([-1.0, -1.0, -1.0, -1.0, -5.0, -5.0], DEFAULT_STABILITY_LIMITS)
        )


class ActionRepetitionTests(unittest.TestCase):
    def test_a_single_action_dominating_the_window_pauses(self) -> None:
        counts = {"hold": 60, "buy": 1}
        signal = action_repetition_signal(61, counts, DEFAULT_STABILITY_LIMITS)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.code, "action_repetition")
        self.assertEqual(signal.severity, PAUSE)
        self.assertEqual(signal.evidence["action"], "hold")

    def test_small_samples_or_mixed_actions_are_silent(self) -> None:
        # Below the minimum action count a repetition claim is not made.
        self.assertIsNone(action_repetition_signal(20, {"hold": 20}, DEFAULT_STABILITY_LIMITS))
        # Dominant, but not beyond the declared frequency.
        self.assertIsNone(
            action_repetition_signal(60, {"hold": 54, "buy": 6}, DEFAULT_STABILITY_LIMITS)
        )


class InvalidActionSpikeTests(unittest.TestCase):
    def test_a_refused_action_spike_fails(self) -> None:
        signal = invalid_action_signal(25, 6, DEFAULT_STABILITY_LIMITS)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.code, "invalid_action_spike")
        self.assertEqual(signal.severity, "fail")
        self.assertAlmostEqual(signal.evidence["frequency"], 6 / 25, places=10)

    def test_small_samples_or_isolated_refusals_are_silent(self) -> None:
        self.assertIsNone(invalid_action_signal(10, 10, DEFAULT_STABILITY_LIMITS))
        self.assertIsNone(invalid_action_signal(25, 3, DEFAULT_STABILITY_LIMITS))
        self.assertIsNone(invalid_action_signal(25, 0, DEFAULT_STABILITY_LIMITS))


class StabilityCompositionTests(unittest.TestCase):
    def test_all_three_monitors_compose_with_fail_precedence(self) -> None:
        metrics = metrics_with(
            reward_history=[3.0, 3.0, 3.0, 3.0, 0.2, 0.2],
            action_distribution={"hold": 70, "buy": 1},
            invalid_actions=20,
        )
        signals = assess_stability(metrics)
        self.assertEqual(
            [signal.code for signal in signals],
            ["reward_collapse", "action_repetition", "invalid_action_spike"],
        )
        self.assertEqual(stability_action(signals), "fail")
        self.assertEqual(stability_action([signals[0]]), "pause")
        self.assertEqual(stability_action([]), "ok")

    def test_a_healthy_run_produces_no_signal(self) -> None:
        metrics = metrics_with(
            reward_history=[1.0, 1.1, 1.2, 1.3, 1.25, 1.35],
            action_distribution={"hold": 20, "buy": 15, "sell": 15, "wait": 10},
            invalid_actions=0,
        )
        self.assertEqual(assess_stability(metrics), ())
        self.assertEqual(stability_record(metrics)["action"], "ok")

    def test_the_record_publishes_the_limits_and_the_response(self) -> None:
        record = stability_record(
            metrics_with(action_distribution={"hold": 60}, invalid_actions=60),
            checked_at="2026-11-19T00:00:00+00:00",
        )
        self.assertEqual(record["action"], "fail")
        self.assertEqual(record["codes"], ["action_repetition", "invalid_action_spike"])
        self.assertEqual(record["limits"], DEFAULT_STABILITY_LIMITS.to_dict())
        self.assertIn("reject the artifact", record["policy"]["fail"])
        self.assertIn("paused", record["policy"]["pause"])
        self.assertEqual(record["checked_at"], "2026-11-19T00:00:00+00:00")


class TrainingStabilityBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "novamarket_rl.sqlite3"
        self.bars = price_bars(90)
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

    def start(self) -> tuple[str, str]:
        started = self.manager.start({"config": config_payload(self.bars)})
        return started["run"]["run_id"], started["policy_id"]

    def wait_status(self, run_id: str, wanted: set[str], timeout: float = 60.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.manager.run(run_id)
            if run["status"] in wanted:
                return run
            time.sleep(0.03)
        self.fail(f"run {run_id} never reached {sorted(wanted)} (last {run['status']})")

    def test_a_refused_action_spike_fails_the_run_and_rejects_the_artifact(self) -> None:
        original_step = TrainingManager._step

        def refusing_step(self: TrainingManager, *args: Any, **kwargs: Any) -> Any:
            return replace(original_step(self, *args, **kwargs), invalid_action=True)

        limits = StabilityLimits(invalid_action_min_actions=1, invalid_action_frequency=0.05)
        with mock.patch.object(TrainingManager, "_step", refusing_step), mock.patch.object(
            training_module, "DEFAULT_STABILITY_LIMITS", limits
        ):
            run_id, policy_id = self.start()
            run = self.wait_status(run_id, {"failed", "completed", "cancelled"})
        self.assertEqual(run["status"], "failed")
        self.assertIn("refused by the environment", run["error"])
        self.assertIn("invalid_action_spike", run["metrics"]["stability"]["codes"])
        self.assertEqual(run["metrics"]["stability"]["action"], "fail")
        self.assertEqual(self.manager.policy(policy_id)["status"], "rejected")

    def test_action_repetition_pauses_with_a_checkpoint_and_can_be_cancelled(self) -> None:
        limits = StabilityLimits(action_repetition_min_actions=3, action_repetition_frequency=0.9)
        with mock.patch.object(DQNAgent, "act", lambda self, state: 0), mock.patch.object(
            training_module, "DEFAULT_STABILITY_LIMITS", limits
        ):
            run_id, policy_id = self.start()
            run = self.wait_status(run_id, {"paused", "failed", "completed"})
            self.assertEqual(run["status"], "paused", run.get("error"))
            self.assertTrue(run["stop_reason"].startswith("stability pause:"))
            stability = run["metrics"]["stability"]
            self.assertEqual(stability["action"], "pause")
            self.assertIn("action_repetition", stability["codes"])
            kinds = {
                row["kind"] for row in self.manager.checkpoints(run_id)["checkpoints"]
            }
            self.assertIn("paused", kinds)
            # A parked run keeps its artifact for inspection; nothing is rejected.
            self.assertNotEqual(self.manager.policy(policy_id)["status"], "rejected")
            self.manager.cancel(run_id)
            final = self.wait_status(run_id, {"cancelled", "failed", "completed"})
        self.assertEqual(final["status"], "cancelled")
        # A direct operator cancel keeps the generic wording.
        self.assertEqual(final["stop_reason"], "cancelled by request")

    def test_a_reward_collapse_parks_the_run_with_the_collapse_reason(self) -> None:
        real_record = training_module.stability_record

        def collapsing(metrics: dict[str, Any], limits: Any, *, checked_at: str = "") -> Any:
            injected = dict(metrics)
            injected["reward_history"] = [4.0, 4.0, 4.0, 4.0, 0.1, 0.1]
            return real_record(injected, limits, checked_at=checked_at)

        with mock.patch.object(training_module, "stability_record", collapsing):
            run_id, _policy_id = self.start()
            run = self.wait_status(run_id, {"paused", "failed", "completed"})
            self.assertEqual(run["status"], "paused", run.get("error"))
            self.assertIn("reward_collapse", run["metrics"]["stability"]["codes"])
            self.assertIn("reward_collapse", run["stop_reason"])
            self.manager.cancel(run_id)
            self.wait_status(run_id, {"cancelled", "failed", "completed"})

    def test_one_paused_checkpoint_request_writes_exactly_one_checkpoint(self) -> None:
        limits = StabilityLimits(
            action_repetition_min_actions=3, action_repetition_frequency=0.9
        )
        with mock.patch.object(DQNAgent, "act", lambda self, state: 0), mock.patch.object(
            training_module, "DEFAULT_STABILITY_LIMITS", limits
        ):
            run_id, _policy_id = self.start()
            run = self.wait_status(run_id, {"paused", "failed", "completed"})
            self.assertEqual(run["status"], "paused", run.get("error"))
            # Force the window in which a second writer would also see the
            # request: the worker is parked and polling while the API answers.
            real_save = TrainingManager._save_checkpoint

            def slow_save(*args: Any, **kwargs: Any) -> Any:
                time.sleep(0.15)
                return real_save(*args, **kwargs)

            with mock.patch.object(TrainingManager, "_save_checkpoint", slow_save):
                self.manager.request_checkpoint(run_id)
                time.sleep(0.5)
            manual = [
                row
                for row in self.manager.checkpoints(run_id)["checkpoints"]
                if row["kind"] == "manual"
            ]
            self.assertEqual(len(manual), 1, manual)
            self.manager.cancel(run_id)
            self.wait_status(run_id, {"cancelled", "failed", "completed"})

    def test_the_contract_publishes_the_monitors_and_responses(self) -> None:
        block = self.manager.contract()["stability"]
        self.assertEqual(
            block["monitors"],
            [
                "non_finite_or_exploding_loss",
                "reward_collapse",
                "action_repetition",
                "invalid_action_spike",
            ],
        )
        self.assertEqual(block["limits"], DEFAULT_STABILITY_LIMITS.to_dict())
        self.assertIn("reject the artifact", block["response"]["fail"])
        self.assertIn("paused", block["response"]["pause"])


class WalkForwardPauseTests(unittest.TestCase):
    """A paused fold holds the only training slot; a sequence must not deadlock."""

    def test_a_paused_fold_ends_the_fold_instead_of_hanging_the_sequence(self) -> None:
        from test_novamarket_pi15_walk_forward import walk_payload

        bars = price_bars(260)
        forecasts = {bar.observed_at: forecast(bar.observed_at) for bar in bars}
        limits = StabilityLimits(
            action_repetition_min_actions=3, action_repetition_frequency=0.9
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "novamarket_rl.sqlite3"
            rewards = RewardService(
                path,
                transition_reader=lambda episode_id: [],
                bar_loader=lambda config: bars,
                forecast_loader=lambda config, at: forecasts.get(at),
            )
            manager = TrainingManager(
                path,
                bar_loader=lambda config: bars,
                forecast_loader=lambda config, at: forecasts.get(at),
                reward_service=rewards,
            )
            try:
                with mock.patch.object(DQNAgent, "act", lambda self, state: 0), mock.patch.object(
                    training_module, "DEFAULT_STABILITY_LIMITS", limits
                ):
                    started = manager.walk_forward(
                        {"config": walk_payload(bars), "folds": 2}
                    )
                    walk_forward_id = started["walk_forward"]["walk_forward_id"]
                    deadline = time.monotonic() + 120.0
                    state = started["walk_forward"]
                    while time.monotonic() < deadline:
                        state = manager.walk_forward_record(walk_forward_id)["walk_forward"]
                        if state["status"] != "running":
                            break
                        time.sleep(0.05)
                self.assertEqual(state["status"], "completed")
                self.assertEqual([row["status"] for row in state["folds"]], ["failed", "failed"])
                self.assertIsNone(state["selected_fold"])
                for row in state["folds"]:
                    self.assertTrue(row["run_id"])
                    run = manager.run(row["run_id"])
                    self.assertEqual(run["status"], "cancelled")
                    # The sequence row must say what actually happened: the fold
                    # was cancelled because a stability pause owned the slot,
                    # not because an operator asked for a cancellation.
                    self.assertIn("stability pause", row["error"])
                    self.assertIn("action_repetition", row["error"])
                    self.assertIn("release the single training slot", row["error"])
                    self.assertIn("stability pause", run["stop_reason"])
                    self.assertNotEqual(run["stop_reason"], "cancelled by request")
            finally:
                manager.close()
                rewards.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class MetricsUpdateStabilityRecordTests(unittest.TestCase):
    """The metrics update owns which stability record stays visible.

    A prior pause/fail record must survive a healthy (``ok``) update, so a run
    resumed by an operator does not look as if it had never paused.  A fresh
    healthy run writes its own ``ok`` record with no codes, and a paused or
    failed run keeps exposing its record after the update runs.
    """

    PAUSE_RECORD: dict[str, Any] = {
        "action": "pause",
        "codes": ["action_repetition"],
        "signals": [],
        "limits": DEFAULT_STABILITY_LIMITS.to_dict(),
        "policy": {},
        "checked_at": "2026-11-19T00:00:00+00:00",
        "simulated": True,
        "advisory_only": True,
        "production_authority": False,
    }
    FAIL_RECORD: dict[str, Any] = {
        **PAUSE_RECORD,
        "action": "fail",
        "codes": ["invalid_action_spike"],
        "checked_at": "2026-11-19T00:01:00+00:00",
    }

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "novamarket_rl.sqlite3"
        self.bars = price_bars(90)
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
        self.agent = DQNAgent(input_dim=8, hidden_layers=(4,), replay_capacity=8, seed=7)

    def tearDown(self) -> None:
        self.manager.close()
        self.rewards.close()
        self.temp.cleanup()

    def make_run(
        self, *, status: str = "running", prior: dict[str, Any] | None = None
    ) -> TrainingRun:
        metrics: dict[str, Any] = {} if prior is None else {"stability": prior}
        return TrainingRun(
            run_id="run_stability_record",
            status=status,
            current_step=10,
            total_steps=100,
            metrics=metrics,
        )

    def update(self, run: TrainingRun) -> TrainingRun:
        """One healthy metrics update: balanced actions, stable rewards, no refusals."""

        return self.manager._update_training_metrics(
            run,
            0,
            1.0,
            10,
            [1.0],
            [10],
            deque([1.0], maxlen=5),
            [0.5],
            Counter({"hold": 5, "buy": 5}),
            0,
            self.agent,
            0.0,
            "",
        )

    def test_a_fresh_healthy_update_writes_an_ok_record_with_no_codes(self) -> None:
        updated = self.update(self.make_run())
        record = dict(updated.metrics)["stability"]
        self.assertEqual(record["action"], "ok")
        self.assertEqual(record["codes"], [])

    def test_a_prior_pause_record_survives_a_healthy_update(self) -> None:
        updated = self.update(self.make_run(prior=dict(self.PAUSE_RECORD)))
        record = dict(updated.metrics)["stability"]
        self.assertEqual(record["action"], "pause")
        self.assertEqual(record["codes"], ["action_repetition"])
        self.assertEqual(record, self.PAUSE_RECORD)
        self.assertEqual(record["checked_at"], self.PAUSE_RECORD["checked_at"])

    def test_a_fail_record_also_survives_a_healthy_update(self) -> None:
        updated = self.update(self.make_run(prior=dict(self.FAIL_RECORD)))
        record = dict(updated.metrics)["stability"]
        self.assertEqual(record["action"], "fail")
        self.assertEqual(record["codes"], ["invalid_action_spike"])

    def test_a_paused_or_failed_run_keeps_exposing_its_record(self) -> None:
        for status, prior in (
            ("paused", self.PAUSE_RECORD),
            ("failed", self.FAIL_RECORD),
        ):
            with self.subTest(status=status):
                updated = self.update(self.make_run(status=status, prior=dict(prior)))
                record = dict(updated.metrics)["stability"]
                self.assertEqual(record["action"], prior["action"])
                self.assertEqual(record["codes"], prior["codes"])
