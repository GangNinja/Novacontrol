"""PI-15 per-market-regime validation and out-of-sample verification.

Two properties are pinned here:

* a regime label comes from ONE PI-13 observation's own evidence, so a slice
  cannot borrow another split's evidence;
* the regime slices are reported evidence only — they never train the policy,
  tune a hyperparameter or decide checkpoint selection.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from novacontrol.novamarket.agent_training import TrainingManager
from novacontrol.novamarket.agent_training.training import _selection_score
from novacontrol.novamarket.features.models import RegimeLabel
from novacontrol.novamarket.features.regime import (
    DEFAULT_REGIME_DETECTOR,
    classify_regime,
)
from novacontrol.novamarket.rewards import RewardService
from novacontrol.novamarket.rewards.regimes import (
    MIN_REGIME_SAMPLES,
    REGIME_THRESHOLDS,
    observation_regime,
    regime_breakdown,
)
from test_novamarket_pi15 import config_payload, forecast, mixed_regime_bars


def observation(**market: Any) -> dict[str, Any]:
    base = {
        "close": 100.0,
        "open": 99.0,
        "high": 101.0,
        "low": 98.0,
        "volume": 1000.0,
        "return_20": 0.0,
        "volatility_20": 0.01,
    }
    base.update(market)
    return {"market": base, "features": {"values": {}}, "forecasts": {}}


class FakeView:
    """The three fields ``regime_breakdown`` reads from a PI-14 TransitionView."""

    def __init__(
        self,
        *,
        regime_market: dict[str, Any],
        net_return: float | None,
        execution_status: str = "filled",
    ) -> None:
        self.observation = observation(**regime_market)
        self.net_return = net_return
        self.execution_status = execution_status


class RegimeLabelTests(unittest.TestCase):
    def test_labels_come_from_the_observation_market_evidence(self) -> None:
        self.assertEqual(observation_regime(observation(return_20=0.05)), "weak_up")
        self.assertEqual(observation_regime(observation(return_20=-0.05)), "weak_down")
        self.assertEqual(
            observation_regime(observation(return_20=0.001, volatility_20=0.01)),
            "sideways",
        )

    def test_volatility_bands_take_precedence_over_direction(self) -> None:
        self.assertEqual(
            observation_regime(observation(return_20=0.09, volatility_20=0.05)),
            "high_volatility",
        )
        self.assertEqual(
            observation_regime(observation(return_20=0.0, volatility_20=0.001)),
            "low_volatility",
        )

    def test_trend_falls_back_to_price_vs_sma_when_return_20_is_absent(self) -> None:
        payload = observation(price_vs_sma_20=0.08)
        payload["market"].pop("return_20")
        self.assertEqual(observation_regime(payload), "weak_up")

    def test_missing_or_non_finite_evidence_is_unclassified(self) -> None:
        self.assertEqual(observation_regime(None), "unclassified")
        self.assertEqual(observation_regime({}), "unclassified")
        payload = observation(return_20=float("nan"), volatility_20=float("nan"))
        self.assertEqual(observation_regime(payload), "unclassified")
        payload = observation(return_20="not-a-number", volatility_20=None)
        self.assertEqual(observation_regime(payload), "unclassified")

    def test_a_label_reads_nothing_outside_its_own_observation(self) -> None:
        payload = observation(return_20=0.05)
        label = observation_regime(payload)
        # Extra envelope keys — including a neighbouring split's own evidence —
        # cannot change this observation's label.
        noisy = {
            **payload,
            "validation": {"return_20": -0.5, "volatility_20": 0.9},
            "test": {"return_20": -0.9},
            "aggregate": {"regime": "weak_down"},
        }
        self.assertEqual(observation_regime(noisy), label)
        self.assertEqual(label, "weak_up")


class RegimeClassifierOwnershipTests(unittest.TestCase):
    """PI-04 is the only regime classifier; the fallback is for no-feature rows."""

    FEATURE_VALUES = {
        "close": 110.0,
        "sma_20": 100.0,
        "sma_50": 95.0,
        "ema_50": 96.0,
        "rsi_14": 62.0,
        "plus_di_14": 30.0,
        "minus_di_14": 10.0,
        "adx_14": 30.0,
        "return_20": 0.05,
    }

    def feature_observation(self) -> dict[str, Any]:
        # The market evidence alone would read as sideways/low volatility; the
        # feature snapshot is PI-04's evidence and must win.
        payload = observation(return_20=0.0, volatility_20=0.001)
        payload["features"] = {
            "values": dict(self.FEATURE_VALUES),
            "status": "valid",
            "version": "pi04-features-1.0",
            "feature_set_id": "fs_1",
            "as_of": "2025-01-01T00:00:00+00:00",
            "quality": "valid",
        }
        return payload

    def test_a_feature_snapshot_is_labelled_by_the_pi04_classifier(self) -> None:
        payload = self.feature_observation()
        self.assertEqual(observation_regime(payload), classify_regime(self.FEATURE_VALUES))
        self.assertEqual(observation_regime(payload), "strong_up")

    def test_the_slice_records_which_evidence_produced_the_label(self) -> None:
        view = FakeView(regime_market={"return_20": 0.0}, net_return=0.01)
        view.observation = self.feature_observation()
        breakdown = regime_breakdown(transitions=[view])
        row = breakdown["rows"][0]
        self.assertEqual(row["regime"], "strong_up")
        self.assertEqual(row["label_source"], "features")
        self.assertEqual(breakdown["label_sources"], {"features": 1})
        self.assertEqual(breakdown["thresholds"], DEFAULT_REGIME_DETECTOR.parameters)

    def test_the_fallback_speaks_the_single_pi04_vocabulary(self) -> None:
        labels = {
            observation_regime(observation(return_20=0.05)),
            observation_regime(observation(return_20=-0.05)),
            observation_regime(observation(return_20=0.001, volatility_20=0.01)),
            observation_regime(observation(return_20=0.09, volatility_20=0.05)),
            observation_regime(observation(return_20=0.0, volatility_20=0.001)),
        }
        self.assertLessEqual(labels, {item.value for item in RegimeLabel})
        # Market-only evidence cannot confirm PI-04's strong-trend requirement,
        # so the fallback reports PI-04's weak labels instead of a second set.
        self.assertIn(RegimeLabel.WEAK_UP.value, labels)
        self.assertIn(RegimeLabel.WEAK_DOWN.value, labels)

    def test_a_regime_bucket_reports_every_source_that_contributed(self) -> None:
        """One label can mix PI-04 feature rows and market-fallback rows.

        The bucket used to take its ``label_source`` from the first row, so a
        mixed bucket claimed one provenance for transitions it did not produce.
        """

        feature_row = FakeView(
            regime_market={"return_20": 0.0, "volatility_20": 0.001},
            net_return=0.01,
        )
        feature_row.observation = {
            **observation(return_20=0.0, volatility_20=0.001),
            "features": {
                "values": {
                    "close": 102.0,
                    "sma_20": 100.0,
                    "sma_50": 99.0,
                    "rsi_14": 56.0,
                    "adx_14": 12.0,
                    "return_20": 0.025,
                },
                "status": "valid",
            },
        }
        market_row = FakeView(regime_market={"return_20": 0.05}, net_return=-0.01)

        rows = regime_breakdown(transitions=[feature_row, market_row])["rows"]
        self.assertEqual([row["regime"] for row in rows], ["weak_up"])
        row = rows[0]
        self.assertEqual(row["samples"], 2)
        self.assertEqual(row["label_sources"], {"features": 1, "market": 1})
        self.assertEqual(row["label_source"], "mixed")

    def test_a_single_source_bucket_keeps_its_provenance(self) -> None:
        breakdown = regime_breakdown(
            transitions=[FakeView(regime_market={"return_20": 0.05}, net_return=0.01)]
        )
        row = breakdown["rows"][0]
        self.assertEqual(row["label_source"], "market")
        self.assertEqual(row["label_sources"], {"market": 1})

    def test_the_fallback_runs_only_without_a_feature_snapshot(self) -> None:
        breakdown = regime_breakdown(
            transitions=[FakeView(regime_market={"return_20": 0.05}, net_return=0.01)]
        )
        row = breakdown["rows"][0]
        self.assertEqual(row["regime"], "weak_up")
        self.assertEqual(row["label_source"], "market")
        self.assertEqual(breakdown["label_sources"], {"market": 1})
        self.assertEqual(breakdown["fallback"]["thresholds"], REGIME_THRESHOLDS)

    def test_a_missing_feature_snapshot_falls_back_while_an_uncertain_one_does_not(self) -> None:
        no_features = observation(return_20=0.05)
        del no_features["features"]
        self.assertEqual(observation_regime(no_features), "weak_up")
        # Present-but-insufficient evidence is PI-04's answer: uncertain, not a
        # second set of thresholds.
        payload = observation(return_20=0.05)
        payload["features"] = {"values": {"rsi_14": 50.0}, "status": "insufficient"}
        self.assertEqual(observation_regime(payload), "uncertain")


class RegimeBreakdownTests(unittest.TestCase):
    def test_rows_group_transitions_by_their_own_regime(self) -> None:
        views = [
            FakeView(regime_market={"return_20": 0.05}, net_return=0.02),
            FakeView(regime_market={"return_20": 0.06}, net_return=-0.01),
            FakeView(regime_market={"return_20": 0.04}, net_return=0.03),
            FakeView(regime_market={"return_20": -0.05}, net_return=-0.04),
        ]
        rewards = [{"total": 1.0}, {"total": -0.5}, {"total": 0.5}, {"total": -1.0}]
        breakdown = regime_breakdown(transitions=views, rewards=rewards)
        self.assertTrue(breakdown["available"])
        self.assertEqual(breakdown["samples"], 4)
        self.assertIn("PI-13 observation evidence", breakdown["source"])
        self.assertFalse(breakdown["cross_split_inference"])
        rows = {row["regime"]: row for row in breakdown["rows"]}
        self.assertEqual(set(rows), {"weak_up", "weak_down"})
        up = rows["weak_up"]
        self.assertEqual(up["samples"], 3)
        self.assertAlmostEqual(up["mean_return"], (0.02 - 0.01 + 0.03) / 3, places=10)
        self.assertAlmostEqual(up["positive_ratio"], 2 / 3, places=10)
        self.assertAlmostEqual(up["mean_reward"], (1.0 - 0.5 + 0.5) / 3, places=10)
        self.assertAlmostEqual(up["share"], 0.75, places=10)
        self.assertEqual(up["conclusion"], "")
        down = rows["weak_down"]
        self.assertEqual(down["samples"], 1)
        self.assertFalse(down["sufficient"])
        self.assertIn("insufficient samples", down["conclusion"])
        self.assertEqual(breakdown["insufficient"], ["weak_down"])
        self.assertEqual(breakdown["min_samples"], MIN_REGIME_SAMPLES)
        # PI-04 owns the thresholds; the market-only fallback publishes its own
        # limits separately rather than competing with the classifier's.
        self.assertEqual(breakdown["thresholds"], DEFAULT_REGIME_DETECTOR.parameters)
        self.assertEqual(breakdown["fallback"]["thresholds"], REGIME_THRESHOLDS)

    def test_an_empty_split_reports_no_conclusion(self) -> None:
        breakdown = regime_breakdown(transitions=[])
        self.assertFalse(breakdown["available"])
        self.assertEqual(breakdown["rows"], [])
        self.assertEqual(breakdown["samples"], 0)

    def test_unclassified_evidence_is_its_own_row(self) -> None:
        views = [FakeView(regime_market={"return_20": None}, net_return=0.01)]
        views[0].observation = {"market": {"close": 1.0}}
        breakdown = regime_breakdown(transitions=views)
        self.assertEqual([row["regime"] for row in breakdown["rows"]], ["unclassified"])
        self.assertEqual(breakdown["insufficient"], ["unclassified"])

    def test_invalid_actions_are_counted_per_regime(self) -> None:
        views = [
            FakeView(
                regime_market={"return_20": 0.05},
                net_return=None,
                execution_status="invalid",
            )
        ]
        rows = regime_breakdown(transitions=views)["rows"]
        self.assertEqual(rows[0]["invalid_actions"], 1)
        self.assertIsNone(rows[0]["mean_return"])


class SelectionIgnoresRegimesTests(unittest.TestCase):
    def test_the_selection_score_cannot_read_regime_metrics(self) -> None:
        base = {
            "financial": {"total_return": 0.10},
            "risk": {"max_drawdown": -0.20},
            "learning": {"average_reward": 0.30},
        }
        # A regime row claiming any out-of-sample outcome leaves the score equal:
        # selection is computed from financial/risk/learning only.
        with_regimes = {
            **base,
            "regimes": {
                "rows": [{"regime": "weak_up", "mean_return": 999.0, "positive_ratio": 1.0}]
            },
        }
        self.assertEqual(_selection_score(base), _selection_score(with_regimes))


class TrainingRegimeReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "novamarket_rl.sqlite3"
        self.bars = mixed_regime_bars(90)
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

    def run_once(self) -> tuple[str, str, dict[str, Any]]:
        started = self.manager.start({"config": config_payload(self.bars)})
        run_id = started["run"]["run_id"]
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            run = self.manager.run(run_id)
            if run["status"] in {"completed", "failed", "cancelled"}:
                return run_id, started["policy_id"], run
            time.sleep(0.03)
        self.fail("the focused training run did not finish")

    def test_validation_and_out_of_sample_metrics_carry_regime_slices(self) -> None:
        run_id, policy_id, run = self.run_once()
        self.assertEqual(run["status"], "completed", run.get("error"))
        validation = run["metrics"]["validation"]
        out_of_sample = run["metrics"]["test"]
        for summary, split in ((validation, "validation"), (out_of_sample, "test")):
            regimes = summary["regimes"]
            self.assertTrue(regimes["available"], split)
            self.assertIn("PI-04 RegimeDetector", regimes["source"])
            self.assertEqual(regimes["thresholds"], DEFAULT_REGIME_DETECTOR.parameters)
            self.assertFalse(regimes["cross_split_inference"])
            self.assertEqual(regimes["min_samples"], MIN_REGIME_SAMPLES)
            # Every transition of THIS split is accounted for exactly once, so
            # no other split's observations can be folded into the slice.
            self.assertEqual(regimes["samples"], summary["policy"]["transitions"])
            self.assertEqual(regimes["samples"], sum(row["samples"] for row in regimes["rows"]))
            # The fixture spans regimes, so this split must really be split:
            # several labels, partition shares, and per-row counts that add up
            # to exactly this split's transitions.
            labels = [row["regime"] for row in regimes["rows"]]
            self.assertGreaterEqual(len(labels), 2, (split, labels))
            # The fixture's own path moves direction inside every split, so a
            # split that only ever saw a monotone drift (or a single regime)
            # fails here instead of passing on plumbing alone.
            self.assertTrue(
                {label for label in labels if label in {"weak_up", "weak_down"}},
                (split, labels),
            )
            self.assertEqual(len(labels), len(set(labels)))
            self.assertTrue(all(row["samples"] > 0 for row in regimes["rows"]))
            # Rows publish shares rounded to 6 dp, so the partition sums to 1
            # up to that rounding and no further.
            self.assertAlmostEqual(
                sum(row["share"] for row in regimes["rows"]), 1.0, delta=1e-4
            )
            self.assertEqual(
                sum(row["samples"] for row in regimes["rows"]),
                summary["policy"]["transitions"],
            )
            # This split carries no feature snapshot, so every row records the
            # documented market fallback as the evidence behind its label.
            self.assertEqual(regimes["label_sources"], {"market": regimes["samples"]})
            self.assertEqual(
                regimes["insufficient"],
                [row["regime"] for row in regimes["rows"] if row["samples"] < MIN_REGIME_SAMPLES],
            )
            self.assertIn("never train", summary["regime_note"])
        # Only the out-of-sample split is flagged as having used test data.
        self.assertFalse(validation["test_data_used"])
        self.assertTrue(out_of_sample["test_data_used"])
        # The report is reachable through the API surface that serves run metrics.
        served = self.manager.metrics(run_id)["metrics"]
        self.assertEqual(
            [row["regime"] for row in served["validation"]["regimes"]["rows"]],
            [row["regime"] for row in validation["regimes"]["rows"]],
        )
        self.assertEqual(
            [row["regime"] for row in served["test"]["regimes"]["rows"]],
            [row["regime"] for row in out_of_sample["regimes"]["rows"]],
        )
        # ... and through the policy artifact's own validation/test evidence.
        policy = self.manager.policy(policy_id)
        self.assertTrue(policy["validation_metrics"]["regimes"]["available"])
        self.assertTrue(policy["test_metrics"]["regimes"]["available"])
        self.assertEqual(
            policy["test_metrics"]["regimes"]["samples"],
            out_of_sample["regimes"]["samples"],
        )

    def test_validation_slice_never_counts_test_observations(self) -> None:
        _run_id, _policy_id, run = self.run_once()
        validation = run["metrics"]["validation"]["regimes"]
        out_of_sample = run["metrics"]["test"]["regimes"]
        self.assertEqual(
            validation["samples"], run["metrics"]["validation"]["policy"]["transitions"]
        )
        self.assertEqual(
            out_of_sample["samples"], run["metrics"]["test"]["policy"]["transitions"]
        )
        # Independent evidence: the two slices are separate breakdowns, not a
        # running total that would let the test window inflate validation.
        self.assertIsNot(validation["rows"], out_of_sample["rows"])
        self.assertTrue(out_of_sample["samples"] > 0)

    def test_the_contract_publishes_the_regime_slice_rules(self) -> None:
        contract = self.manager.contract()["regimes"]
        self.assertEqual(contract["min_samples"], MIN_REGIME_SAMPLES)
        self.assertFalse(contract["used_for_training"])
        self.assertFalse(contract["used_for_selection"])
        self.assertIn("PI-13 observation evidence", contract["label_source"])


if __name__ == "__main__":  # pragma: no cover
    import unittest as _unittest

    _unittest.main()


class TrainingRegimeFeatureProvenanceTests(unittest.TestCase):
    """A run given a feature loader reports PI-04's labels as feature-labelled.

    ``TrainingRegimeReportTests`` covers the no-feature path end to end, where
    every slice uses the market-only fallback.  Here the same training flow is
    given a feature loader, so every PI-13 observation carries a snapshot and
    the slices must be labelled by PI-04's classifier and say so.
    """

    STRONG_UP: dict[str, float] = {
        "close": 110.0,
        "sma_20": 100.0,
        "sma_50": 95.0,
        "ema_50": 96.0,
        "rsi_14": 62.0,
        "plus_di_14": 30.0,
        "minus_di_14": 10.0,
        "adx_14": 30.0,
        "return_20": 0.05,
    }
    STRONG_DOWN: dict[str, float] = {
        "close": 90.0,
        "sma_20": 100.0,
        "sma_50": 105.0,
        "ema_50": 104.0,
        "rsi_14": 38.0,
        "plus_di_14": 10.0,
        "minus_di_14": 30.0,
        "adx_14": 30.0,
        "return_20": -0.05,
    }

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "novamarket_rl.sqlite3"
        self.bars = mixed_regime_bars(90)
        self.forecasts = {bar.observed_at: forecast(bar.observed_at) for bar in self.bars}
        positions = {bar.observed_at: index for index, bar in enumerate(self.bars)}
        self.feature_calls: list[Any] = []

        def feature_loader(
            config: Any, at: Any, history: Any
        ) -> dict[str, Any]:
            self.feature_calls.append(at)
            values = self.STRONG_UP if positions[at] % 2 == 0 else self.STRONG_DOWN
            return {
                "as_of": at.isoformat(),
                "values": dict(values),
                "status": "valid",
                "version": "pi04-test",
            }

        self.rewards = RewardService(
            self.path,
            transition_reader=lambda episode_id: [],
            bar_loader=lambda config: self.bars,
            feature_loader=feature_loader,
            forecast_loader=lambda config, at: self.forecasts.get(at),
        )
        self.manager = TrainingManager(
            self.path,
            bar_loader=lambda config: self.bars,
            feature_loader=feature_loader,
            forecast_loader=lambda config, at: self.forecasts.get(at),
            reward_service=self.rewards,
        )

    def tearDown(self) -> None:
        self.manager.close()
        self.rewards.close()
        self.temp.cleanup()

    def run_once(self) -> dict[str, Any]:
        started = self.manager.start({"config": config_payload(self.bars)})
        run_id = started["run"]["run_id"]
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            run = self.manager.run(run_id)
            if run["status"] in {"completed", "failed", "cancelled"}:
                return run
            time.sleep(0.03)
        self.fail("the focused training run did not finish")

    def test_slices_of_a_feature_backed_run_report_pi04_labels_and_features(self) -> None:
        run = self.run_once()
        self.assertEqual(run["status"], "completed", run.get("error"))
        self.assertTrue(self.feature_calls, "the feature loader was never consulted")
        for split in ("validation", "test"):
            summary = run["metrics"][split]
            regimes = summary["regimes"]
            self.assertTrue(regimes["available"], split)
            self.assertEqual(regimes["samples"], summary["policy"]["transitions"], split)
            # No row fell back to market evidence: every transition carried its
            # own feature snapshot.
            self.assertEqual(regimes["label_sources"], {"features": regimes["samples"]}, split)
            labels = {row["regime"] for row in regimes["rows"]}
            self.assertEqual(labels, {"strong_up", "strong_down"}, split)
            self.assertLessEqual(labels, {item.value for item in RegimeLabel})
            for row in regimes["rows"]:
                self.assertEqual(row["label_source"], "features", split)
                self.assertEqual(row["label_sources"], {"features": row["samples"]}, split)
                self.assertGreater(row["samples"], 0, split)
