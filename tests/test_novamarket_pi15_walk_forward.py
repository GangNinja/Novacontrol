"""PI-15 walk-forward verification: bounded sequential folds that never leak.

The leak proof is direct rather than descriptive: every planned fold is prepared
through the REAL ``TrainingDataPreparer`` and the bar timestamps of one fold's
test window are compared with every other fold's training and validation
windows.  Selection is proven separate by construction — ``select_fold`` only
ever reads validation scores.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from novacontrol.novamarket.agent_training import (
    MAX_WALK_FORWARD_FOLDS,
    TrainingConfig,
    TrainingManager,
    TrainingValidationError,
)
from novacontrol.novamarket.agent_training.datasets import TrainingDataPreparer
from novacontrol.novamarket.agent_training.walkforward import (
    WalkForwardFold,
    assert_fold_separation,
    build_walk_forward_folds,
    fold_config,
    select_fold,
)
from novacontrol.novamarket.rewards import RewardService
from test_novamarket_pi15 import config_payload, forecast, price_bars

#: 260 weekday bars hold exactly two base-width folds (a third runs past the end).
BAR_COUNT = 260


def walk_payload(bars: Any, **changes: Any) -> dict[str, Any]:
    """The focused fixture's config with an explicit rolling-window template."""
    payload = config_payload(bars)
    payload["splits"] = {
        "train": {
            "label": "train",
            "start": bars[0].observed_at.isoformat(),
            "end": bars[79].observed_at.isoformat(),
        },
        "validation": {
            "label": "validation",
            "start": bars[80].observed_at.isoformat(),
            "end": bars[99].observed_at.isoformat(),
        },
        "test": {
            "label": "test",
            "start": bars[100].observed_at.isoformat(),
            "end": bars[119].observed_at.isoformat(),
        },
    }
    payload.update(changes)
    return payload


def forecasts_for(bars: Any) -> dict[datetime, dict[str, Any]]:
    return {bar.observed_at: forecast(bar.observed_at) for bar in bars}


class FoldPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bars = price_bars(BAR_COUNT)
        self.config = TrainingConfig.from_mapping(walk_payload(self.bars))

    def test_folds_roll_forward_without_sharing_a_boundary(self) -> None:
        folds = build_walk_forward_folds(self.config, 2)
        self.assertEqual([fold.index for fold in folds], [0, 1])
        for fold in folds:
            self.assertLess(fold.train_end, fold.validation_start)
            self.assertLess(fold.validation_end, fold.test_start)
        first, second = folds
        # The second fold starts strictly after the first fold's test window, so
        # the first fold's test bars cannot be in the second fold's training set.
        self.assertGreater(second.train_start, first.test_end)
        self.assertEqual(second.train_start - first.test_end, timedelta(microseconds=1))
        self.assertEqual(first.train_start.isoformat(), self.config.splits["train"]["start"])

    def test_fold_count_is_bounded(self) -> None:
        for rejected in (0, -1, MAX_WALK_FORWARD_FOLDS + 1):
            with self.assertRaises(TrainingValidationError):
                build_walk_forward_folds(self.config, rejected)
        for rejected in (True, "2", 2.0):
            with self.assertRaises(TrainingValidationError):
                build_walk_forward_folds(self.config, rejected)  # type: ignore[arg-type]
        self.assertEqual(len(build_walk_forward_folds(self.config, 2)), 2)

    def test_folds_past_the_environment_window_are_refused(self) -> None:
        with self.assertRaises(TrainingValidationError) as ctx:
            build_walk_forward_folds(self.config, 3)
        self.assertIn("environment window", str(ctx.exception))

    def test_overlapping_folds_are_refused(self) -> None:
        first = build_walk_forward_folds(self.config, 2)[0]
        inside_first_test = WalkForwardFold(
            index=1,
            train_start=first.test_start,
            train_end=first.test_end,
            validation_start=first.test_end + timedelta(days=1),
            validation_end=first.test_end + timedelta(days=2),
            test_start=first.test_end + timedelta(days=3),
            test_end=first.test_end + timedelta(days=4),
        )
        with self.assertRaises(TrainingValidationError) as ctx:
            assert_fold_separation((first, inside_first_test))
        message = str(ctx.exception)
        self.assertIn("refusing fold overlap", message)
        self.assertIn("test", message)
        self.assertIn("train", message)

    def test_fold_configuration_keeps_the_base_hyperparameters(self) -> None:
        fold = build_walk_forward_folds(self.config, 2)[1]
        folded = fold_config(self.config, fold)
        self.assertEqual(folded.algorithm, self.config.algorithm)
        self.assertEqual(folded.seed, self.config.seed)
        self.assertEqual(folded.max_episodes, self.config.max_episodes)
        self.assertEqual(folded.splits["train"]["start"], fold.train_start.isoformat())
        self.assertIn("walk-forward fold 2", folded.name)
        self.assertNotEqual(folded.config_hash, self.config.config_hash)

    def test_prepared_datasets_share_no_bar_between_folds(self) -> None:
        folds = build_walk_forward_folds(self.config, 2)
        forecasts = forecasts_for(self.bars)
        preparer = TrainingDataPreparer(
            bar_loader=lambda config: self.bars,
            forecast_loader=lambda config, at: forecasts.get(at),
        )
        stamps: list[dict[str, set[datetime]]] = []
        for fold in folds:
            bundle = preparer.prepare(fold_config(self.config, fold))
            per_split = {
                label: {bar.observed_at for bar in dataset.bars}
                for label, dataset in bundle.datasets.items()
            }
            # Within a fold the three windows are already ordered and separate.
            self.assertFalse(per_split["train"] & per_split["validation"])
            self.assertFalse(per_split["validation"] & per_split["test"])
            self.assertFalse(per_split["train"] & per_split["test"])
            stamps.append(per_split)
        # The leak assertion: no other fold's training/validation bar is the bar
        # this fold was tested on, in either direction.
        self.assertFalse(stamps[0]["test"] & stamps[1]["train"])
        self.assertFalse(stamps[0]["test"] & stamps[1]["validation"])
        self.assertFalse(stamps[1]["test"] & stamps[0]["train"])
        self.assertFalse(stamps[1]["test"] & stamps[0]["validation"])


class FoldSelectionTests(unittest.TestCase):
    def test_a_better_test_score_never_wins_selection(self) -> None:
        rows = [
            {"index": 0, "status": "completed", "validation_score": 0.41, "test": {"score": 9.5}},
            {"index": 1, "status": "completed", "validation_score": 0.83, "test": {"score": -3.0}},
        ]
        self.assertEqual(select_fold(rows), 1)

    def test_failed_folds_are_never_selected(self) -> None:
        rows = [
            {"index": 0, "status": "failed", "validation_score": 0.99, "test": {"score": 1.0}},
            {"index": 1, "status": "completed", "validation_score": 0.2, "test": {"score": 0.0}},
        ]
        self.assertEqual(select_fold(rows), 1)

    def test_no_completed_fold_selects_nothing(self) -> None:
        rows = [
            {"index": 0, "status": "failed", "validation_score": None},
            {"index": 1, "status": "cancelled", "validation_score": 0.5},
        ]
        self.assertIsNone(select_fold(rows))


class WalkForwardExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "novamarket_rl.sqlite3"
        self.bars = price_bars(BAR_COUNT)
        self.forecasts = forecasts_for(self.bars)
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

    def wait_sequence(self, walk_forward_id: str, timeout: float = 180.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.manager.walk_forward_record(walk_forward_id)["walk_forward"]
            if state["status"] != "running":
                return state
            time.sleep(0.05)
        self.fail(f"walk-forward sequence {walk_forward_id} did not finish within {timeout}s")

    def test_folds_run_sequentially_and_select_on_validation_only(self) -> None:
        started = self.manager.walk_forward({"config": walk_payload(self.bars), "folds": 2})
        walk_forward_id = started["walk_forward"]["walk_forward_id"]
        state = self.wait_sequence(walk_forward_id)

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["folds_requested"], 2)
        self.assertEqual(state["completed_folds"], 2, state["folds"])
        self.assertEqual([row["status"] for row in state["folds"]], ["completed", "completed"])
        self.assertTrue(state["sequential"])
        self.assertEqual(state["max_concurrent_folds"], 1)
        self.assertTrue(state["fold_overlap_refused"])

        # Sequential execution: fold 2's run only exists once fold 1's run ended.
        runs = [self.manager.run(row["run_id"]) for row in state["folds"]]
        self.assertTrue(all(run["status"] == "completed" for run in runs))
        first_end = datetime.fromisoformat(runs[0]["ended_at"])
        second_start = datetime.fromisoformat(runs[1]["created_at"])
        self.assertLessEqual(first_end, second_start)

        # Each fold is an ordinary PI-15 run and artifact of its own fold config.
        base_config_id = TrainingConfig.from_mapping(walk_payload(self.bars)).config_id
        for row, run in zip(state["folds"], runs, strict=True):
            self.assertTrue(row["policy_id"])
            self.assertNotEqual(run["config_id"], base_config_id)
            self.assertEqual(run["config_id"], "rl_train_cfg_" + row["config_hash"][:20])
            policy = self.manager.policy(row["policy_id"])
            self.assertFalse(policy["production_authority"])
            self.assertFalse(policy["activation_available"])

        # Selection reads validation only, and the reported test result cannot win.
        self.assertEqual(state["selection_basis"], "validation")
        self.assertFalse(state["test_used_for_selection"])
        self.assertFalse(state["test_used_for_training"])
        scores = [row["validation_score"] for row in state["folds"]]
        self.assertTrue(all(isinstance(score, (int, float)) for score in scores), scores)
        winner = max(range(len(scores)), key=lambda position: scores[position])
        self.assertEqual(state["selected_fold"], state["folds"][winner]["index"])

    def test_a_refused_fold_plan_launches_no_run(self) -> None:
        with self.assertRaises(TrainingValidationError):
            self.manager.walk_forward({"config": walk_payload(self.bars), "folds": 3})
        self.assertEqual(self.manager.runs()["runs"], [])
        self.assertEqual(self.manager.policies()["policies"], [])

    def test_identical_requests_reuse_the_sequence(self) -> None:
        first = self.manager.walk_forward({"config": walk_payload(self.bars), "folds": 2})
        second = self.manager.walk_forward({"config": walk_payload(self.bars), "folds": 2})
        self.assertEqual(
            first["walk_forward"]["walk_forward_id"],
            second["walk_forward"]["walk_forward_id"],
        )
        self.assertTrue(second.get("reused"))
        self.wait_sequence(first["walk_forward"]["walk_forward_id"])
        self.assertEqual(len(self.manager.runs()["runs"]), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
