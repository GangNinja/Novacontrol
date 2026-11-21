"""Regressions found by exercising the RL Training page in a browser.

Each case pins a defect that only showed up against the running app: a payload
the page can send that used to answer 500, an artifact that could not be traced
back to its run, and a comparison column that was always empty.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from novacontrol.novamarket.agent_training import TrainingManager
from novacontrol.novamarket.rewards import RewardService
from novacontrol.novamarket.rl.models import RLValidationError
from test_novamarket_pi15 import config_payload, forecast, price_bars


class TrainingUiContractTests(unittest.TestCase):
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

    def wait_terminal(self, run_id: str, timeout: float = 60.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.manager.run(run_id)
            if run["terminal"]:
                return run
            time.sleep(0.03)
        self.fail(f"run {run_id} never finished")

    def test_an_empty_environment_is_a_validation_error_not_a_crash(self) -> None:
        # The page can send this before a dataset is chosen; a 500 blames the
        # server for what is a caller mistake.
        with self.assertRaises(RLValidationError):
            self.manager.create_config(
                {
                    "config": {
                        "name": "blank",
                        "algorithm": "dqn",
                        "environment": {"dataset_id": "", "dataset_version": ""},
                    }
                }
            )

    def test_a_saved_policy_names_the_run_that_produced_it(self) -> None:
        run_id, policy_id = self.start()
        run = self.wait_terminal(run_id)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(self.manager.policy(policy_id)["run_id"], run_id)

    def test_comparison_reports_each_policys_training_reward(self) -> None:
        policy_ids: list[str] = []
        for _ in range(2):
            run_id, policy_id = self.start()
            self.assertEqual(self.wait_terminal(run_id)["status"], "completed")
            policy_ids.append(policy_id)
        payload = self.manager.compare_policies({"policy_ids": policy_ids})
        comparison = payload.get("comparison", payload)
        rows = comparison["results"]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIsInstance(row["training_metrics"]["mean_reward"], float)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
