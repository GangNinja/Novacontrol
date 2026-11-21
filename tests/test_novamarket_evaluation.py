"""PI-11 deterministic forecast-evaluation tests.

These fixtures are synthetic and named as such.  They test formulas, explicit
policies and persistence; they are not historical market evidence and no
profitability claim is made.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from novacontrol.novamarket.backtesting import BacktestConfig
from novacontrol.novamarket.evaluation import (
    EvaluationConfig,
    EvaluationError,
    PredictionEvaluationService,
    record_from_mapping,
)
from novacontrol.novamarket.evaluation.metrics import (
    direction_metrics,
    evaluate_metrics,
    interval_metrics,
    leakage_audit,
    lifecycle_metrics,
    model_comparison,
    price_metrics,
    probability_metrics,
    stability_metrics,
    walk_forward_metrics,
)
from novacontrol.novamarket.evaluation.models import EvaluationRecord
from novacontrol.novamarket.prediction.models import ForecastOutput
from novacontrol.novamarket.prediction.store import PredictionStore
from novacontrol.novamarket.scheduling.models import ForecastOutcome
from novacontrol.novamarket.service import NovaMarketService
from novacontrol.novamarket.strategies.catalog import validate_config

BASE = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)


def record(
    index: int = 0,
    *,
    predicted_price: float = 110.0,
    actual_price: float = 100.0,
    predicted_return: float = 0.10,
    actual_return: float = 0.0,
    predicted_direction: str = "up",
    p_up: float | None = 0.70,
    p_down: float | None = 0.20,
    p_neutral: float | None = 0.10,
    probability_status: str = "calibrated",
    interval_lower: float | None = -0.05,
    interval_upper: float | None = 0.15,
    stage: str = "",
    cycle_id: str = "",
    model_id: str = "fixture_model",
    model_version: str = "v1",
    regime: str = "sideways",
    source: str = "records",
    origin: str = "synthetic_fixture",
    horizon: str = "5d",
    dataset_version: str = "fixture-ds-1",
    feature_set_version: str = "fixture-fs-1",
) -> EvaluationRecord:
    generated = BASE + timedelta(days=index)
    return record_from_mapping(
        {
            "forecast_id": f"fx_{index}",
            "source": source,
            "instrument_id": "XNSE:TEST",
            "horizon": horizon,
            "generated_at": generated,
            "target_at": generated + timedelta(days=5),
            "reference_at": generated,
            "actual_observed_at": generated + timedelta(days=5),
            "reference_price": 100.0,
            "predicted_price": predicted_price,
            "actual_price": actual_price,
            "predicted_return": predicted_return,
            "actual_return": actual_return,
            "predicted_direction": predicted_direction,
            "p_up": p_up,
            "p_down": p_down,
            "p_neutral": p_neutral,
            "probability_status": probability_status,
            "interval_lower": interval_lower,
            "interval_upper": interval_upper,
            "stage": stage,
            "cycle_id": cycle_id,
            "model_id": model_id,
            "model_version": model_version,
            "regime": regime,
            "origin": origin,
            "dataset_version": dataset_version,
            "feature_set_version": feature_set_version,
        }
    )


class MetricsTests(unittest.TestCase):
    def test_price_and_return_metrics_are_exact(self) -> None:
        config = EvaluationConfig(
            sources=("records",),
            record_origin="synthetic_fixture",
            min_samples=2,
            as_of=BASE + timedelta(days=30),
        )
        rows = [
            record(0, predicted_price=110, actual_price=100),
            record(1, predicted_price=90, actual_price=100),
        ]
        price = price_metrics(rows, config)
        self.assertEqual(price["samples"], 2)
        self.assertEqual(price["mae"], 10.0)
        self.assertEqual(price["rmse"], 10.0)
        self.assertEqual(price["mape"], 0.1)
        self.assertEqual(price["smape"], 0.10025063)
        self.assertEqual(price["bias"], 0.0)

    def test_direction_policy_is_explicit_and_uncertain_is_not_guessed(self) -> None:
        config = EvaluationConfig(
            sources=("records",),
            record_origin="synthetic_fixture",
            neutral_threshold=0.01,
            direction_policy="exclude_uncertain",
            as_of=BASE + timedelta(days=30),
        )
        rows = [
            record(0, actual_return=0.10, predicted_direction="up"),
            record(1, actual_return=-0.10, predicted_direction="uncertain"),
            record(2, actual_return=0.001, predicted_direction="neutral"),
        ]
        result = direction_metrics(rows, config)
        self.assertEqual(result["resolved"], 2)
        self.assertEqual(result["uncertain"], 1)
        self.assertEqual(result["abstained"], 0)
        self.assertEqual(result["non_directional"], 1)
        self.assertEqual(result["accuracy"], 1.0)
        self.assertIn("excluded", result["policy"]["uncertain_treatment"])
        assert result["confusion"]["down"]["uncertain"] == 1
        abstained_result = direction_metrics(
            [record(3, actual_return=-0.10, predicted_direction="abstained")], config
        )
        self.assertEqual(abstained_result["abstained"], 1)
        self.assertEqual(abstained_result["resolved"], 0)
        self.assertEqual(abstained_result["abstention_rate"], 1.0)
        self.assertIsNone(abstained_result["accuracy"])
        counted = EvaluationConfig(
            sources=("records",),
            record_origin="synthetic_fixture",
            neutral_threshold=0.01,
            direction_policy="count_uncertain_incorrect",
            as_of=BASE + timedelta(days=30),
        )
        counted_result = direction_metrics(rows, counted)
        self.assertAlmostEqual(counted_result["accuracy"], 2 / 3)
        counted_excluding_neutral = EvaluationConfig(
            sources=("records",),
            record_origin="synthetic_fixture",
            neutral_threshold=0.01,
            direction_policy="count_uncertain_incorrect",
            neutral_policy="exclude",
            as_of=BASE + timedelta(days=30),
        )
        excluded_result = direction_metrics(rows, counted_excluding_neutral)
        # The neutral actual is removed from both numerator and denominator,
        # while the uncertain prediction still counts as incorrect.
        self.assertEqual(excluded_result["resolved"], 1)
        # One correct directional call over two non-neutral actuals; the
        # neutral actual no longer inflates the denominator to three.
        self.assertEqual(excluded_result["accuracy"], 0.5)

    def test_probability_metrics_refuse_uncalibrated_scores(self) -> None:
        config = EvaluationConfig(sources=("records",), record_origin="synthetic_fixture", as_of=BASE)
        rows = [record(0, probability_status="uncalibrated")]
        result = probability_metrics(rows, config)
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("brier", result)

    def test_brier_and_log_loss_match_known_values(self) -> None:
        config = EvaluationConfig(sources=("records",), record_origin="synthetic_fixture", as_of=BASE + timedelta(days=30))
        rows = [record(0, actual_return=0.10, p_up=0.7, p_down=0.2, p_neutral=0.1)]
        result = probability_metrics(rows, config)
        self.assertAlmostEqual(result["brier"], 0.14, places=8)
        self.assertGreater(result["log_loss"], 0)
        self.assertGreaterEqual(result["ece"], 0)

    def test_interval_coverage_is_compared_with_nominal_not_assumed(self) -> None:
        config = EvaluationConfig(
            sources=("records",),
            record_origin="synthetic_fixture",
            interval_confidence=0.80,
            as_of=BASE + timedelta(days=30),
        )
        rows = [
            record(0, actual_return=0.0, interval_lower=-0.1, interval_upper=0.1),
            record(1, actual_return=0.5, interval_lower=-0.1, interval_upper=0.1),
        ]
        result = interval_metrics(rows, config)
        self.assertEqual(result["actual_coverage"], 0.5)
        self.assertEqual(result["coverage_gap"], -0.3)
        self.assertEqual(result["misses_above"], 1)

    def test_lifecycle_paired_comparison_marks_improvement_and_worsening(self) -> None:
        config = EvaluationConfig(sources=("records",), record_origin="synthetic_fixture", as_of=BASE + timedelta(days=30))
        rows = [
            record(0, stage="t_minus_20", cycle_id="c1", predicted_price=110),
            record(1, stage="t_minus_1", cycle_id="c1", predicted_price=101),
            record(2, stage="t_minus_20", cycle_id="c2", predicted_price=90),
            record(3, stage="t_minus_1", cycle_id="c2", predicted_price=130),
        ]
        result = lifecycle_metrics(rows, config)
        paired = result["paired_comparisons"]["initial_vs_final_confirmation"]
        self.assertEqual(paired["samples"], 2)
        self.assertEqual(paired["right_better"], 1)
        self.assertEqual(paired["right_worse"], 1)
        self.assertEqual(result["cycles_observed"], 2)

    def test_model_ranking_is_within_horizon_and_uses_error_not_complexity(self) -> None:
        config = EvaluationConfig(sources=("records",), record_origin="synthetic_fixture", min_samples=2, as_of=BASE + timedelta(days=30))
        rows = [
            record(0, predicted_price=105, model_id="naive"),
            record(1, predicted_price=95, model_id="naive"),
            record(2, predicted_price=130, model_id="lstm", model_version="v9"),
            record(3, predicted_price=70, model_id="lstm", model_version="v9"),
        ]
        result = model_comparison(rows, config)
        ranking = result["ranking"]
        self.assertEqual(ranking[0]["model_id"], "naive")
        self.assertEqual(ranking[0]["rank"], 1)
        self.assertFalse(result["automatic_promotion"])
        self.assertEqual(result["baselines"]["5d"]["model_id"], "naive")

    def test_stability_and_walk_forward_are_chronological(self) -> None:
        config = EvaluationConfig(
            sources=("records",),
            record_origin="synthetic_fixture",
            min_samples=3,
            walk_forward_folds=2,
            as_of=BASE + timedelta(days=60),
        )
        rows = [record(index, predicted_price=101, actual_price=100) for index in range(12)]
        stability = stability_metrics(rows, config)
        self.assertGreaterEqual(len(stability["windows"]), 2)
        self.assertIn("mae_degradation", stability)
        folds = walk_forward_metrics(rows, config)
        self.assertEqual(len(folds), 2)
        self.assertEqual(folds[0]["selection"], "fixed configuration; no fold is used to tune it")
        self.assertIn("purged_overlapping_targets", folds[0])
        self.assertIn("target observation ended", folds[0]["purge_rule"])
        delayed = list(rows)
        delayed[2] = replace(delayed[2], actual_observed_at=BASE + timedelta(days=30))
        purged = walk_forward_metrics(delayed, config)
        self.assertEqual(purged[1]["purged_overlapping_targets"], 1)
        self.assertEqual(purged[1]["preceding_records"], 5)

    def test_regime_slices_report_insufficient_samples(self) -> None:
        config = EvaluationConfig(sources=("records",), record_origin="synthetic_fixture", min_samples=20, as_of=BASE + timedelta(days=30))
        rows = [record(0, regime="high_volatility")]
        result = evaluate_metrics(rows, config)["regimes"][0]
        self.assertFalse(result["sufficient"])
        self.assertIn("no regime conclusion", result["caveat"])

    def test_leakage_audit_records_rejections_and_missing_versions(self) -> None:
        config = EvaluationConfig(sources=("records",), record_origin="synthetic_fixture", as_of=BASE + timedelta(days=30))
        rows = [record(0, dataset_version="", feature_set_version="")]
        audit = leakage_audit(rows, [{"forecast_id": "bad", "reason": "future"}])
        self.assertEqual(audit["rejected_records"], 1)
        self.assertEqual(audit["missing_version_count"], 1)

    def test_reconstruction_requires_model_and_information_cutoffs(self) -> None:
        with self.assertRaises(EvaluationError):
            record_from_mapping(
                {
                    "forecast_id": "r1",
                    "source": "records",
                    "origin": "research_reconstruction",
                    "instrument_id": "XNSE:TEST",
                    "horizon": "5d",
                    "generated_at": BASE,
                    "target_at": BASE + timedelta(days=5),
                    "reference_at": BASE,
                    "actual_observed_at": BASE + timedelta(days=5),
                    "reference_price": 100,
                    "predicted_price": 101,
                    "actual_price": 102,
                    "predicted_return": 0.01,
                    "actual_return": 0.02,
                    "predicted_direction": "up",
                }
            )

    def test_future_actual_observation_is_refused(self) -> None:
        with self.assertRaises(EvaluationError):
            record_from_mapping(
                {
                    "forecast_id": "r1",
                    "source": "records",
                    "instrument_id": "XNSE:TEST",
                    "horizon": "5d",
                    "generated_at": BASE,
                    "target_at": BASE + timedelta(days=5),
                    "reference_at": BASE,
                    "actual_observed_at": BASE + timedelta(days=4),
                    "reference_price": 100,
                    "predicted_price": 101,
                    "actual_price": 102,
                    "predicted_return": 0.01,
                    "actual_return": 0.02,
                    "predicted_direction": "up",
                }
            )


class _FakePredictionStore:
    def __init__(self, forecasts, outcomes, cycles=(), ensemble_rows=()):
        self._forecasts = {item.forecast_id: item for item in forecasts}
        self._outcomes = list(outcomes)
        self._cycles = {item.cycle_id: item for item in cycles}
        self._ensemble_rows = list(ensemble_rows)

    def forecast_outcomes(self, **kwargs):
        rows = self._outcomes
        if kwargs.get("instrument_id"):
            rows = [
                row
                for row in rows
                if self._forecasts[row.forecast_id].instrument_id == kwargs["instrument_id"]
            ]
        return rows

    def get_forecast(self, forecast_id):
        return self._forecasts.get(forecast_id)

    def forecast_cycles(self, **kwargs):
        cycle = self._cycles.get(kwargs.get("cycle_id"))
        return [cycle] if cycle else []

    def ensemble_evaluations(self, **kwargs):
        return self._ensemble_rows[: kwargs.get("limit", 20)]


class _FakeForecastStore:
    def __init__(self, rows, performance_rows=()):
        self._rows = rows
        self._performance_rows = list(performance_rows)

    def forecasts(self, **kwargs):
        rows = self._rows
        if kwargs.get("instrument_id"):
            rows = [row for row in rows if row["instrument_id"] == kwargs["instrument_id"]]
        return rows[: kwargs.get("limit", 50)]

    def performance(self, **kwargs):
        return self._performance_rows[: kwargs.get("limit", 50)]


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "evaluation.sqlite3"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_explicit_records_persist_immutably_and_verify(self) -> None:
        service = PredictionEvaluationService(self.path)
        try:
            payload = {
                "name": "fixture evaluation",
                "sources": ["records"],
                "record_origin": "synthetic_fixture",
                "as_of": (BASE + timedelta(days=30)).isoformat(),
                "min_samples": 2,
                "records": [
                    record(0).to_dict(),
                    record(1, predicted_price=99, actual_price=100).to_dict(),
                ],
            }
            body = service.evaluate(payload)
            self.assertEqual(body["samples"], 2)
            self.assertEqual(body["provenance"]["synthetic_fixtures"], 2)
            self.assertIn(body["evaluation_id"], [row["evaluation_id"] for row in service.list()["evaluations"]])
            verified = service.verify(body["evaluation_id"])
            self.assertTrue(verified["ok"])
            with self.assertRaises(sqlite3.DatabaseError):
                with service.store._connection:  # deliberate immutability probe
                    service.store._connection.execute(
                        "UPDATE pi11_evaluations SET status='failed'"
                    )
            before_replay = service.status()["evaluations"]["total"]
            replay = service.reproduce(body["evaluation_id"])
            self.assertTrue(replay["same_data_digest"])
            self.assertTrue(replay["same_report"])
            self.assertEqual(
                service.status()["evaluations"]["total"],
                before_replay,
                "reproduction is a read-only verification, not another report",
            )
        finally:
            service.close()

    def test_explicit_records_without_an_origin_are_rejected(self) -> None:
        service = PredictionEvaluationService(self.path)
        try:
            unprovenanced = record(0).to_dict()
            unprovenanced.pop("origin")
            body = service.evaluate(
                {
                    "sources": ["records"],
                    "as_of": (BASE + timedelta(days=30)).isoformat(),
                    "records": [unprovenanced],
                }
            )
            self.assertEqual(body["samples"], 0)
            self.assertEqual(body["status"], "insufficient_data")
            self.assertEqual(body["provenance"]["operational_forecasts"], 0)
            self.assertIn(
                "require an origin",
                body["leakage_audit"]["rejected"][0]["reason"],
            )
            self.assertEqual(len(body["input_records"]), 1)
            self.assertNotIn("origin", body["input_records"][0])
            replay = service.reproduce(body["evaluation_id"])
            self.assertTrue(replay["same_data_digest"])
            self.assertTrue(replay["same_report"])
        finally:
            service.close()

    def test_observation_after_the_evaluation_cutoff_is_rejected(self) -> None:
        service = PredictionEvaluationService(self.path)
        try:
            body = service.evaluate(
                {
                    "sources": ["records"],
                    "record_origin": "synthetic_fixture",
                    "as_of": (BASE + timedelta(days=3)).isoformat(),
                    "records": [record(0).to_dict()],
                }
            )
            self.assertEqual(body["samples"], 0)
            self.assertEqual(body["status"], "insufficient_data")
            self.assertEqual(body["leakage_audit"]["rejected_records"], 1)
            self.assertIn(
                "target observed after as_of",
                body["leakage_audit"]["rejected"][0]["reason"],
            )
        finally:
            service.close()

    def test_model_scorecards_honor_the_as_of_cutoff(self) -> None:
        cutoff = BASE + timedelta(days=5)
        performance_rows = [
            {
                "model_id": "old_model",
                "instrument_id": "XNSE:TEST",
                "horizon": "5d",
                "created_at": BASE.isoformat(),
                "samples": 40,
                "metrics": {
                    "version": "v1",
                    "mae": 2.0,
                    "rmse": 2.5,
                    "directional_accuracy": 0.6,
                },
            },
            {
                "model_id": "future_model",
                "instrument_id": "XNSE:TEST",
                "horizon": "5d",
                "created_at": (BASE + timedelta(days=10)).isoformat(),
                "samples": 40,
                "metrics": {
                    "version": "v2",
                    "mae": 0.1,
                    "rmse": 0.2,
                    "directional_accuracy": 0.9,
                },
            },
        ]
        ensemble_rows = [
            {
                "ensemble_id": "ens_old",
                "config_id": "cfg_old",
                "instrument_id": "XNSE:TEST",
                "horizon": "5d",
                "created_at": BASE.isoformat(),
                "test": {"samples": 20, "price_mae": 1.0, "price_rmse": 1.2},
            },
            {
                "ensemble_id": "ens_future",
                "config_id": "cfg_future",
                "instrument_id": "XNSE:TEST",
                "horizon": "5d",
                "created_at": (BASE + timedelta(days=10)).isoformat(),
                "test": {"samples": 20, "price_mae": 0.05, "price_rmse": 0.1},
            },
        ]
        service = PredictionEvaluationService(
            self.path,
            forecast_store=_FakeForecastStore([], performance_rows=performance_rows),
            prediction_store=_FakePredictionStore(
                [], [], ensemble_rows=ensemble_rows
            ),
        )
        try:
            body = service.evaluate(
                {
                    "sources": ["records"],
                    "records": [],
                    "as_of": cutoff.isoformat(),
                }
            )
            evidence = body["model_evidence"]
            self.assertEqual(len(evidence["scorecards"]), 2)
            self.assertEqual(
                {(row["model_id"], row["version"]) for row in evidence["scorecards"]},
                {("old_model", "v1"), ("ensemble", "cfg_old")},
            )
            self.assertEqual(
                [row["model_id"] for row in evidence["ranking"]],
                ["ensemble", "old_model"],
            )
        finally:
            service.close()

    def test_empty_evidence_is_degraded_not_zero_error(self) -> None:
        service = PredictionEvaluationService(self.path)
        try:
            body = service.evaluate(
                {
                    "sources": ["records"],
                    "record_origin": "synthetic_fixture",
                    "records": [],
                    "as_of": (BASE + timedelta(days=30)).isoformat(),
                }
            )
            self.assertEqual(body["status"], "insufficient_data")
            self.assertEqual(body["samples"], 0)
            self.assertIn("not evidence of zero error", " ".join(body["limitations"]))
        finally:
            service.close()

    def test_lifecycle_store_is_reused_without_regeneration(self) -> None:
        forecast = type(
            "Forecast",
            (),
            {
                "forecast_id": "pfx_1",
                "instrument_id": "XNSE:TEST",
                "forecast_horizon": type("H", (), {"value": "5d"})(),
                "generated_at": BASE,
                "reference_at": BASE,
                "reference_price": 100,
                "model_id": "ensemble",
                "model_version": "v1",
                "ensemble_id": "ens_1",
                "dataset_version": "ds_1",
                "feature_set_version": "fs_1",
                "lifecycle_id": "lc_1",
                "stage": type("S", (), {"value": "t_minus_1"})(),
                "p_up": 0.6,
                "p_down": 0.3,
                "p_neutral": 0.1,
                "probability_status": type("P", (), {"value": "calibrated"})(),
                "uncertainty": type("U", (), {"value": "medium"})(),
                "data_quality": type("Q", (), {"value": "verified"})(),
                "data_freshness": type("F", (), {"value": "fresh"})(),
                "validity": type("V", (), {"value": "valid"})(),
                "metadata": {},
            },
        )()
        outcome = type(
            "Outcome",
            (),
            {
                "forecast_id": "pfx_1",
                "cycle_id": "c1",
                "target_at": BASE + timedelta(days=5),
                "actual_observed_at": BASE + timedelta(days=5),
                "predicted_price": 101,
                "actual_price": 102,
                "predicted_return": 0.01,
                "actual_return": 0.02,
                "predicted_direction": "up",
                "interval_lower": -0.05,
                "interval_upper": 0.08,
                "interval_width": 0.13,
                "inside_interval": True,
                "observation_delay_seconds": 0,
                "metadata": {},
                "stage": type("S", (), {"value": "t_minus_1"})(),
            },
        )()
        service = PredictionEvaluationService(
            self.path,
            prediction_store=_FakePredictionStore([forecast], [outcome]),
        )
        try:
            body = service.evaluate(
                {
                    "sources": ["lifecycle"],
                    "instrument_id": "XNSE:TEST",
                    "horizon": "5d",
                    "as_of": (BASE + timedelta(days=10)).isoformat(),
                }
            )
            self.assertEqual(body["samples"], 1)
            self.assertEqual(body["source_summary"]["counts"]["lifecycle"], 1)
            self.assertEqual(body["provenance"]["operational_forecasts"], 1)
            self.assertEqual(body["metrics"]["direction"]["accuracy"], 1.0)
            self.assertIn("lifecycle", [item["source"] for item in body["metrics"]["sources"]])
        finally:
            service.close()

    def test_real_prediction_store_lifecycle_outcome_is_read_verbatim(self) -> None:
        """The actual PI-01/PI-09 tables are the evidence source, not a fixture."""
        store = PredictionStore(Path(self.tmp.name) / "prediction.sqlite3")
        try:
            forecast = ForecastOutput(
                forecast_id="pfc_pi11_real",
                lifecycle_id="lc_pi11_real",
                lifecycle_stage="t_minus_1",
                instrument_id="XNSE:TEST",
                exchange="XNSE",
                currency="INR",
                reference_price=Decimal("100"),
                reference_at=BASE,
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
                ensemble_id="ens_pi11",
                model_id="ensemble",
                model_version="pi08-v1",
                member_versions=("xgboost@v1", "lstm@v1"),
                forecast_horizon="5d",
                target_at=BASE + timedelta(days=5),
                generated_at=BASE,
                data_observed_at=BASE,
                data_received_at=BASE,
                data_resolution="1d",
                data_freshness="fresh",
                data_quality="valid",
                data_snapshot_id="snap_pi11",
                validity="valid",
                validity_conditions=(),
                disagreement=0.1,
                digest="a" * 64,
                metadata={"regime": "sideways"},
                feature_set_version="pi04-features-v1",
                dataset_version="pi02-dataset-v1",
            )
            self.assertTrue(store.save_forecast(forecast))
            outcome = ForecastOutcome(
                outcome_id="pout_pi11_real",
                cycle_id="cycle_pi11_real",
                forecast_id=forecast.forecast_id,
                stage="t_minus_1",
                target_at=BASE + timedelta(days=5),
                actual_observed_at=BASE + timedelta(days=5),
                evaluated_at=BASE + timedelta(days=5, hours=1),
                observation_delay_seconds=0.0,
                actual_price=Decimal("102"),
                actual_return=0.02,
                predicted_price=Decimal("101"),
                predicted_return=0.01,
                predicted_direction="up",
                actual_direction="up",
                direction_correct=True,
                price_error=Decimal("-1"),
                absolute_price_error=Decimal("1"),
                percentage_price_error=0.01,
                return_error=-0.01,
                absolute_return_error=0.01,
                inside_interval=True,
                interval_lower=-0.05,
                interval_upper=0.08,
                interval_width=0.13,
                metadata={"regime": "sideways"},
            )
            self.assertTrue(store.save_outcome(outcome))
            service = PredictionEvaluationService(
                Path(self.tmp.name) / "real_evaluation.sqlite3",
                prediction_store=store,
            )
            try:
                body = service.evaluate(
                    {
                        "sources": ["lifecycle"],
                        "instrument_id": "XNSE:TEST",
                        "horizon": "5d",
                        "as_of": (BASE + timedelta(days=10)).isoformat(),
                    }
                )
                self.assertEqual(body["samples"], 1)
                self.assertEqual(body["metrics"]["direction"]["accuracy"], 1.0)
                self.assertEqual(body["metrics"]["probabilities"]["status"], "evaluated")
                self.assertEqual(body["provenance"]["operational_forecasts"], 1)
                self.assertTrue(body["provenance"]["no_regeneration"])
                self.assertEqual(body["records_sample"][0]["forecast_id"], "pfc_pi11_real")
                self.assertEqual(
                    body["records_sample"][0]["dataset_version"], "pi02-dataset-v1"
                )
            finally:
                service.close()
        finally:
            store.close()

    def test_forecast_assisted_accepts_pi11_evaluation_provenance(self) -> None:
        """The exact UI replay payload must validate, link provenance and stay inert."""
        strategy_config = {
            "trend_period": 20,
            "min_return": "0.005",
            "min_probability": "0.55",
            "max_uncertainty": "high",
            "forecast_horizon": "one_week",
            "max_age_hours": 168,
            "allowed_model_states": ["active", "validated"],
            "horizon_days": 5,
            "max_data_age_hours": 72,
            "evaluation_id": "pev_pi11_web",
        }
        config = validate_config("forecast_assisted", strategy_config)
        self.assertEqual(config["evaluation_id"], "pev_pi11_web")
        self.assertEqual(config["forecast_horizon"], "one_week")
        replay = BacktestConfig.from_mapping(
            {
                "name": "Forecast replay pev_pi11_web",
                "strategy_catalog_id": "forecast_assisted",
                "strategy_version": "pi11-web-1.0",
                "strategy_config": strategy_config,
                "instruments": ["XNSE:TEST"],
                "start": (BASE).isoformat(),
                "end": (BASE + timedelta(days=30)).isoformat(),
                "initial_capital": "100000",
                "evaluation_frequency": "1d",
                "horizon": "short_term",
                "forecast_model_version": "",
                "risk_policy_version": "nm08-backtest-v1",
                "risk_policy": {},
                "max_position_weight": "0.25",
                "max_gross_exposure": "1.00",
                "benchmark": "",
                "execution_config": {
                    "brokerage_bps": "3",
                    "spread_bps": "8",
                    "impact_coefficient_bps": "12",
                },
            }
        )
        self.assertEqual(
            replay.strategy_config["evaluation_id"], "pev_pi11_web"
        )
        self.assertIn("evaluation_id", replay.to_dict()["strategy_config"])

    def test_pi09_contract_adapter_is_point_in_time_and_preserves_horizon(self) -> None:
        """The NM-11 replay sees PI-09 contracts only when they already existed."""

        def contract(identifier: str, generated_at: datetime) -> SimpleNamespace:
            return SimpleNamespace(
                forecast_id=identifier,
                instrument_id="XNSE:TEST",
                generated_at=generated_at,
                data_observed_at=BASE,
                forecast_horizon=SimpleNamespace(value="5d"),
                target_at=BASE + timedelta(days=5),
                predicted_return=0.02,
                predicted_price=Decimal("102"),
                p_up=0.7,
                lower_return=-0.01,
                upper_return=0.05,
                uncertainty=SimpleNamespace(value="low"),
                model_id="ensemble",
                model_version="pi08-v1",
                ensemble_id="ens_pi11",
                validity=SimpleNamespace(value="valid"),
                probability_status=SimpleNamespace(value="calibrated"),
                metadata={"model_state": "validated"},
            )

        available = contract("pfc_available", BASE)
        future = contract("pfc_future", BASE + timedelta(days=2))
        service = NovaMarketService.__new__(NovaMarketService)
        service._forecast_engine = None
        service._prediction_service = SimpleNamespace(
            store=SimpleNamespace(list_forecasts=lambda **_: [available, future])
        )
        service._backtest_error = ""
        rows = service._backtest_forecasts("XNSE:TEST", BASE + timedelta(days=1))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["forecast_id"], "pfc_available")
        self.assertEqual(row["horizon"], "one_week")
        self.assertEqual(row["model_state"], "validated")
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["source"], "PI-09")
        self.assertEqual(row["created_at"], BASE.isoformat())

    def test_original_nm06_forecast_resolves_only_observed_target_bar(self) -> None:
        row = {
            "forecast_id": "nm_resolve",
            "instrument_id": "XNSE:TEST",
            "created_at": BASE.isoformat(),
            "target_at": (BASE + timedelta(days=5)).isoformat(),
            "data_cutoff": BASE.isoformat(),
            "horizon": "few_sessions",
            "model_id": "baseline",
            "model_version": "v1",
            "predicted_return": 0.02,
            "predicted_price": 102,
            "p_up": 0.6,
            "p_down": 0.3,
            "p_sideways": 0.1,
            "lower_return": -0.05,
            "upper_return": 0.08,
            "regime": "sideways",
            "status": "pending",
            "outcome": {},
        }
        bar = SimpleNamespace(
            observed_at=BASE + timedelta(days=6),
            close=105,
        )
        market = SimpleNamespace(
            bars=lambda *args, **kwargs: SimpleNamespace(items=[bar])
        )
        service = PredictionEvaluationService(
            self.path,
            forecast_store=_FakeForecastStore([row]),
            market_data=market,
        )
        try:
            body = service.evaluate(
                {
                    "sources": ["nm06"],
                    "as_of": (BASE + timedelta(days=10)).isoformat(),
                }
            )
            self.assertEqual(body["samples"], 1)
            record = body["records_sample"][0]
            self.assertEqual(record["actual_price"], 105.0)
            self.assertAlmostEqual(record["actual_return"], 0.05)
            self.assertEqual(record["actual_observed_at"], bar.observed_at.isoformat())
            self.assertEqual(record["metadata"]["target_resolution"], "first_observed_bar_at_or_after_target")
            self.assertEqual(record["origin"], "historical_operational")
            self.assertEqual(body["provenance"]["auto_resolved_targets"], 1)
            self.assertEqual(
                body["metrics"]["observations"]["delayed_observations"], 1
            )
            future_cutoff = service.evaluate(
                {
                    "sources": ["nm06"],
                    "as_of": (BASE + timedelta(days=5)).isoformat(),
                }
            )
            self.assertEqual(future_cutoff["samples"], 0)
            self.assertEqual(
                future_cutoff["leakage_audit"]["rejected_records"], 1
            )
            self.assertIn(
                "no observed target bar",
                future_cutoff["leakage_audit"]["rejected"][0]["reason"],
            )
        finally:
            service.close()

    def test_nm06_store_is_read_without_claiming_unavailable_fields(self) -> None:
        row = {
            "forecast_id": "nm_1",
            "instrument_id": "XNSE:TEST",
            "created_at": BASE.isoformat(),
            "target_at": (BASE + timedelta(days=5)).isoformat(),
            "data_cutoff": BASE.isoformat(),
            "horizon": "few_sessions",
            "model_id": "baseline",
            "model_version": "v1",
            "predicted_return": 0.01,
            "predicted_price": 101,
            "p_up": 0.6,
            "p_down": 0.3,
            "p_sideways": 0.1,
            "lower_return": -0.05,
            "upper_return": 0.08,
            "regime": "sideways",
            "status": "evaluated",
            "outcome": {
                "actual_price": 102,
                "actual_return": 0.02,
                "evaluated_at": (BASE + timedelta(days=5)).isoformat(),
                "inside_interval": True,
            },
        }
        service = PredictionEvaluationService(
            self.path,
            forecast_store=_FakeForecastStore([row]),
        )
        try:
            body = service.evaluate(
                {
                    "sources": ["nm06"],
                    "as_of": (BASE + timedelta(days=10)).isoformat(),
                }
            )
            self.assertEqual(body["samples"], 1)
            self.assertEqual(body["metrics"]["probabilities"]["status"], "unavailable")
            self.assertNotIn("brier", body["metrics"]["probabilities"])
            self.assertEqual(body["source_summary"]["counts"]["nm06"], 1)
            self.assertTrue(any("dataset" in item for item in body["limitations"]))
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
