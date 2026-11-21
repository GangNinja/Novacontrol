"""Focused PI-23 predictive-intelligence and forecast-optimization tests.

All fixtures are deterministic synthetic evidence. They are not market data,
model performance claims or trading signals.
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from unittest import mock

from novacontrol.api.models import ApiSurface
from novacontrol.api.route_consumers import ROUTE_CONSUMERS
from novacontrol.novamarket.forecast_intelligence import (
    ForecastIntelligenceConfig,
    ForecastIntelligenceService,
    ForecastIntelligenceSources,
    ForecastIntelligenceValidationError,
)
from novacontrol.novamarket.forecast_intelligence.metrics import fit_temperature

NOW = datetime(2026, 3, 20, 12, 0, tzinfo=UTC)
INSTRUMENT = "XNSE:AAA"
HORIZON = "5d"


def _iso(at: datetime) -> str:
    return at.isoformat()


def _training_run(
    *models: tuple[str, float, float],
    test_used_for_selection: bool = False,
    finished_at: datetime | None = None,
    instrument: str = INSTRUMENT,
    horizon: str = HORIZON,
) -> dict[str, object]:
    block: dict[str, object] = {
        "dataset_version": "dataset-pi23",
        "feature_set_version": "features-pi23",
        "splits": {
            "validation": {
                "start": _iso(NOW - timedelta(days=120)),
                "end": _iso(NOW - timedelta(days=30)),
            }
        },
        "models": {},
        "test_used_for_selection": test_used_for_selection,
    }
    for model_id, validation_mae, test_mae in models:
        block["models"][model_id] = {  # type: ignore[index]
            "family": model_id,
            "version": "v1",
            "validation": {
                "mae": validation_mae,
                "samples": 40,
                "directional_accuracy": 0.61,
                "interval_coverage": 0.82,
                "interval_width": 0.04,
                "calibration_error": 0.08,
            },
            "test": {
                "mae": test_mae,
                "samples": 50,
                "directional_accuracy": 0.59,
                "interval_coverage": 0.80,
            },
        }
    return {
        "kind": "statistical",
        "instrument_id": instrument,
        "horizon": horizon,
        "finished_at": _iso(finished_at or (NOW - timedelta(days=10))),
        "report": block,
    }


def _record(index: int, *, actual_return: float, predicted_return: float) -> dict[str, object]:
    generated = NOW - timedelta(days=40) + timedelta(hours=index)
    probability = min(0.95, max(0.05, 0.5 + actual_return * 8.0))
    return {
        "forecast_id": f"fc-{index}",
        "model_id": "naive",
        "instrument_id": INSTRUMENT,
        "horizon": HORIZON,
        "generated_at": _iso(generated),
        "target_at": _iso(generated + timedelta(days=5)),
        "predicted_return": predicted_return,
        "actual_return": actual_return,
        "lower_return": predicted_return - 0.02,
        "upper_return": predicted_return + 0.02,
        "p_up": probability,
        "p_down": 1.0 - probability,
        "p_neutral": 0.0,
    }


def _records(*, degraded: bool = False) -> list[dict[str, object]]:
    rows = []
    for index in range(60):
        actual = 0.001 if index % 3 else -0.001
        predicted = actual + (0.006 if degraded and index > 35 else 0.0008)
        rows.append(_record(index, actual_return=actual, predicted_return=predicted))
    return rows


def _forecasts() -> list[dict[str, object]]:
    rows = []
    for offset, horizon in enumerate(("1d", "5d")):
        generated = NOW - timedelta(hours=1)
        rows.append(
            {
                "forecast_id": f"forecast-{horizon}",
                "model_id": "naive",
                "instrument_id": INSTRUMENT,
                "horizon": horizon,
                "generated_at": _iso(generated),
                "target_at": _iso(NOW + timedelta(days=5)),
                "predicted_return": 0.002 + offset * 0.001,
                "direction": "up",
                "lower_return": -0.01,
                "upper_return": 0.015,
                "uncertainty": "moderate",
                "status": "current",
            }
        )
    return rows


class FixtureSources:
    def __init__(
        self,
        *,
        test_used_for_selection: bool = False,
        performance: list[dict[str, object]] | None = None,
        forecasts: list[dict[str, object]] | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        self.test_used_for_selection = test_used_for_selection
        self.performance = performance or []
        self.forecast_rows = forecasts
        self.finished_at = finished_at

    def sources(self) -> ForecastIntelligenceSources:
        return ForecastIntelligenceSources(
            models=lambda instrument_id=None, horizon=None: [],
            training_runs=lambda limit=20: [
                _training_run(
                    ("naive", 0.010, 0.090),
                    ("xgboost", 0.005, 0.120),
                    test_used_for_selection=self.test_used_for_selection,
                    finished_at=self.finished_at,
                )
            ],
            performance=lambda **_: self.performance,
            forecasts=lambda instrument_id, horizon, limit=100: [
                row
                for row in (self.forecast_rows if self.forecast_rows is not None else _forecasts())
                if row["horizon"] == horizon
            ],
            calibration=lambda *_, **__: {"status": "unavailable"},
            evaluations=lambda limit=8: [],
            evaluation=lambda evaluation_id: {},
            regime=lambda instrument_id: {},
            ensemble_models=lambda instrument_id=None, horizon=None: [],
            health=lambda: {"ok": True},
        )


def _service(**kwargs: object) -> ForecastIntelligenceService:
    sources = FixtureSources(
        test_used_for_selection=bool(kwargs.pop("test_used_for_selection", False))
    )
    return ForecastIntelligenceService(
        sources.sources(), store_path=":memory:", clock=lambda: NOW, **kwargs
    )


class SelectionEvidenceTests(unittest.TestCase):
    def test_selection_uses_validation_and_never_the_test_scorecard(self) -> None:
        service = _service()
        try:
            body = service.select({"instrument_id": INSTRUMENT, "horizon": HORIZON})
        finally:
            service.close()
        self.assertEqual(body["selected_model_id"], "xgboost")
        self.assertEqual(body["outcome"], "conditionally_selected")
        self.assertFalse(body["test_used_for_selection"])
        self.assertFalse(body["automatic_promotion"])
        self.assertEqual(body["selected_model"]["selection_metrics"]["mae"], 0.005)

    def test_a_report_that_used_test_selection_withholds_validation_evidence(self) -> None:
        service = _service(test_used_for_selection=True)
        try:
            body = service.select({"instrument_id": INSTRUMENT, "horizon": HORIZON})
        finally:
            service.close()
        self.assertEqual(body["outcome"], "no_eligible_model")
        self.assertIsNone(body["selected_model"])
        self.assertEqual(body["sample_count"], 0)
        self.assertFalse(body["test_used_for_selection"])

    def test_missing_evidence_is_explicit_not_zero_error(self) -> None:
        service = ForecastIntelligenceService(
            ForecastIntelligenceSources(), store_path=":memory:", clock=lambda: NOW
        )
        try:
            body = service.evaluate({"instrument_id": INSTRUMENT, "horizon": HORIZON})
            selection = service.select({"instrument_id": INSTRUMENT, "horizon": HORIZON})
        finally:
            service.close()
        self.assertEqual(body["data_status"], "insufficient_data")
        self.assertEqual(selection["outcome"], "insufficient_evidence")
        self.assertIsNone(selection["selected_model_id"])
        self.assertIn("no model evidence was available for this scope", selection["rationale"])

    def test_unknown_task_is_rejected(self) -> None:
        service = _service()
        try:
            with self.assertRaises(ForecastIntelligenceValidationError):
                service.evaluate(
                    {"instrument_id": INSTRUMENT, "horizon": HORIZON, "task": "invented"}
                )
        finally:
            service.close()

    def test_repeating_evaluation_reuses_identical_evidence(self) -> None:
        service = _service()
        try:
            first = service.evaluate({"instrument_id": INSTRUMENT, "horizon": HORIZON})
            second = service.evaluate({"instrument_id": INSTRUMENT, "horizon": HORIZON})
            counts = service.store.counts()
        finally:
            service.close()
        self.assertEqual(first["evaluation_id"], second["evaluation_id"])
        self.assertEqual(counts["by_kind"]["evaluation"], 1)

    def test_recent_performance_snapshot_prevents_false_staleness(self) -> None:
        sources = FixtureSources(
            finished_at=NOW - timedelta(days=10),
            performance=[
                {
                    "model_id": "naive",
                    "instrument_id": INSTRUMENT,
                    "horizon": HORIZON,
                    "created_at": _iso(NOW - timedelta(hours=1)),
                    "metrics": {"mae": 0.012, "samples": 40},
                }
            ],
        ).sources()
        service = ForecastIntelligenceService(sources, store_path=":memory:", clock=lambda: NOW)
        try:
            body = service.evaluate({"instrument_id": INSTRUMENT, "horizon": HORIZON})
        finally:
            service.close()
        naive = next(row for row in body["models"] if row["model_id"] == "naive")
        self.assertEqual(naive["freshness"]["status"], "fresh")
        self.assertEqual(naive["status"], "completed")

    def test_same_resolution_is_compatible_with_its_horizon(self) -> None:
        self.assertTrue(ForecastIntelligenceService._resolution_compatible("5m", "5m"))
        self.assertTrue(ForecastIntelligenceService._resolution_compatible("1h", "1h"))
        self.assertFalse(ForecastIntelligenceService._resolution_compatible("5m", "1d"))


class CalibrationAndDriftTests(unittest.TestCase):
    def test_calibration_fits_earliest_segment_and_evaluates_later_holdout(self) -> None:
        service = _service()
        try:
            body = service.calibrate(
                {"instrument_id": INSTRUMENT, "horizon": HORIZON, "method": "platt"},
                records=_records(),
            )
        finally:
            service.close()
        self.assertNotEqual(body["status"], "insufficient_data")
        self.assertGreaterEqual(body["samples"], 40)
        self.assertFalse(body["applied_to_existing_forecasts"])
        self.assertIsNotNone(body["calibration"])
        self.assertNotEqual(body["fitted_on"], body["evaluation_on"])
        self.assertIsNotNone(body["before"])
        self.assertIsNotNone(body["after"])

    def test_insufficient_probability_pairs_remain_uncalibrated(self) -> None:
        service = _service()
        try:
            body = service.calibrate(
                {"instrument_id": INSTRUMENT, "horizon": HORIZON},
                records=_records()[:4],
            )
        finally:
            service.close()
        self.assertEqual(body["status"], "insufficient_data")
        self.assertIsNone(body["calibration"])

    def test_unknown_calibration_method_is_rejected_even_with_few_samples(self) -> None:
        service = _service()
        try:
            with self.assertRaises(ForecastIntelligenceValidationError):
                service.calibrate(
                    {"instrument_id": INSTRUMENT, "horizon": HORIZON, "method": "invented"},
                    records=_records()[:2],
                )
        finally:
            service.close()

    def test_temperature_scaling_uses_realised_outcomes(self) -> None:
        parameters = fit_temperature(
            [(0.60, 0.30, 0.10, "down") for _ in range(40)],
            minimum=0.50,
            maximum=3.0,
            step=0.05,
        )
        self.assertGreaterEqual(parameters["temperature"], 1.0)

    def test_input_drift_reports_psi_and_ks_without_claiming_production_impact(self) -> None:
        service = _service(config=ForecastIntelligenceConfig(drift_persistence_windows=1))
        try:
            body = service.drift(
                {
                    "instrument_id": INSTRUMENT,
                    "horizon": HORIZON,
                    "reference_features": {"momentum": [0.01] * 40},
                    "current_features": {"momentum": [0.30] * 40},
                }
            )
        finally:
            service.close()
        self.assertEqual(body["status"], "detected")
        self.assertTrue(body["detected"])
        self.assertFalse(body["automatic_deactivation"])
        methods = {event["method"] for event in body["events"]}
        self.assertIn("psi+ks", methods)

    def test_one_drift_window_is_not_confirmed_when_persistence_is_required(self) -> None:
        service = _service()
        try:
            body = service.drift(
                {
                    "instrument_id": INSTRUMENT,
                    "horizon": HORIZON,
                    "reference_features": {"momentum": [0.01] * 40},
                    "current_features": {"momentum": [0.30] * 40},
                }
            )
        finally:
            service.close()
        self.assertFalse(body["detected"])
        self.assertEqual(body["status"], "drift_unconfirmed")
        self.assertTrue(body["window_detected"])


class UncertaintyAndFallbackTests(unittest.TestCase):
    def test_weights_are_bounded_advisory_and_never_applied(self) -> None:
        service = _service()
        try:
            body = service.weights({"instrument_id": INSTRUMENT, "horizon": HORIZON})
        finally:
            service.close()
        self.assertFalse(body["applied"])
        self.assertFalse(body["production_weights_mutated"])
        self.assertTrue(body["recommendations"])
        for row in body["recommendations"]:
            self.assertFalse(row["applied"])
            self.assertLessEqual(abs(row["proposed_weight"] - row["current_weight"]), 0.10)

    def test_reliability_is_categorical_only(self) -> None:
        service = _service()
        try:
            body = service.reliability({"instrument_id": INSTRUMENT, "horizon": HORIZON})
        finally:
            service.close()
        self.assertTrue(body["classification_only"])
        self.assertTrue(body["no_probability_claim"])
        self.assertIsNone(body["numeric_score"])
        for row in body["assessments"]:
            self.assertIsNone(row["numeric_score"])
            self.assertIn(
                row["classification"],
                {
                    "high_reliability",
                    "moderate_reliability",
                    "low_reliability",
                    "unreliable",
                    "insufficient_data",
                },
            )

    def test_fallback_abstains_when_no_current_forecast_is_compatible(self) -> None:
        service = ForecastIntelligenceService(
            ForecastIntelligenceSources(),
            store_path=":memory:",
            clock=lambda: NOW,
        )
        try:
            body = service.fallback({"instrument_id": INSTRUMENT, "horizon": HORIZON})
        finally:
            service.close()
        self.assertEqual(body["outcome"], "insufficient_data")
        self.assertIsNone(body["selected"])
        self.assertFalse(body["new_forecast_created"])

    def test_full_analysis_runs_every_advisory_surface_without_mutation(self) -> None:
        service = _service()
        try:
            body = service.analyze({"instrument_id": INSTRUMENT, "horizon": HORIZON})
        finally:
            service.close()
        self.assertEqual(
            set(body["results"]),
            {
                "evaluation",
                "selection",
                "calibration",
                "uncertainty",
                "disagreement",
                "consistency",
                "drift",
                "degradation",
                "weights",
                "reliability",
                "fallback",
            },
        )
        self.assertFalse(body["automatic_promotion"])
        self.assertFalse(body["authority"]["production_weight_mutation"])
        uncertainty = body["results"]["uncertainty"]
        self.assertEqual(uncertainty["components"]["data_uncertainty"], "stale")
        self.assertTrue(uncertainty["stale_models"])

    def test_expired_forecasts_are_not_used_by_fallback_or_consistency(self) -> None:
        expired = []
        for row in _forecasts():
            expired.append(
                {
                    **row,
                    "generated_at": _iso(NOW - timedelta(days=10)),
                    "target_at": _iso(NOW - timedelta(days=1)),
                }
            )
        sources = FixtureSources(forecasts=expired).sources()
        service = ForecastIntelligenceService(sources, store_path=":memory:", clock=lambda: NOW)
        try:
            fallback = service.fallback(
                {"instrument_id": INSTRUMENT, "horizon": HORIZON, "preferred_model_id": "naive"}
            )
            consistency = service.consistency(INSTRUMENT)
        finally:
            service.close()
        self.assertEqual(fallback["outcome"], "insufficient_data")
        self.assertIsNone(fallback["selected"])
        self.assertEqual(consistency["status"], "insufficient_data")
        self.assertEqual(consistency["horizons"], [])

    def test_two_way_direction_split_is_critical(self) -> None:
        generated = NOW - timedelta(hours=1)
        forecasts = []
        for model_id, direction in (("naive", "up"), ("xgboost", "down")):
            forecasts.append(
                {
                    "forecast_id": f"split-{model_id}",
                    "model_id": model_id,
                    "instrument_id": INSTRUMENT,
                    "horizon": HORIZON,
                    "generated_at": _iso(generated),
                    "target_at": _iso(NOW + timedelta(days=5)),
                    "predicted_return": 0.002,
                    "direction": direction,
                    "lower_return": -0.01,
                    "upper_return": 0.012,
                    "status": "current",
                }
            )
        service = ForecastIntelligenceService(
            FixtureSources(forecasts=forecasts).sources(),
            store_path=":memory:",
            clock=lambda: NOW,
        )
        try:
            body = service.disagreement({"instrument_id": INSTRUMENT, "horizon": HORIZON})
        finally:
            service.close()
        self.assertEqual(body["state"], "critical_disagreement")
        self.assertEqual(body["measurements"]["direction_split"], 0.5)

    def test_multihorizon_consistency_preserves_each_horizon(self) -> None:
        service = _service()
        try:
            body = service.consistency(INSTRUMENT)
        finally:
            service.close()
        self.assertEqual(body["status"], "completed")
        self.assertEqual([row["horizon"] for row in body["horizons"]], ["1d", "5d"])
        self.assertEqual(body["pairs"][0]["state"], "aligned")


class ApiSurfaceTests(unittest.TestCase):
    EXPECTED = {
        ("GET", "/market/forecast-intelligence/status"),
        ("GET", "/market/forecast-intelligence/contracts"),
        ("GET", "/market/forecast-intelligence/navigation"),
        ("GET", "/market/forecast-intelligence/config"),
        ("PUT", "/market/forecast-intelligence/config"),
        ("GET", "/market/forecast-intelligence/registry"),
        ("GET", "/market/forecast-intelligence/history"),
        ("GET", "/market/forecast-intelligence/overview"),
        ("GET", "/market/forecast-intelligence/comparison/{instrument_id}"),
        ("GET", "/market/forecast-intelligence/horizons/{instrument_id}"),
        ("GET", "/market/forecast-intelligence/regimes/{instrument_id}"),
        ("POST", "/market/forecast-intelligence/instruments"),
        ("POST", "/market/forecast-intelligence/evaluate"),
        ("POST", "/market/forecast-intelligence/select"),
        ("POST", "/market/forecast-intelligence/calibration"),
        ("POST", "/market/forecast-intelligence/uncertainty"),
        ("POST", "/market/forecast-intelligence/disagreement"),
        ("POST", "/market/forecast-intelligence/consistency"),
        ("POST", "/market/forecast-intelligence/drift"),
        ("POST", "/market/forecast-intelligence/degradation"),
        ("POST", "/market/forecast-intelligence/weights"),
        ("POST", "/market/forecast-intelligence/reliability"),
        ("POST", "/market/forecast-intelligence/fallback"),
        ("POST", "/market/forecast-intelligence/analyze"),
        ("GET", "/market/forecast-intelligence/health"),
    }

    def test_every_pi23_route_is_registered_and_non_rendering(self) -> None:
        routes = {(row.method, row.path) for row in ApiSurface.default().routes}
        self.assertEqual(self.EXPECTED - routes, set())
        for route in self.EXPECTED:
            self.assertEqual(ROUTE_CONSUMERS[route], "no-render")

    def test_pi23_routes_contain_no_order_execution_or_broker_path(self) -> None:
        rows = [
            row
            for row in ApiSurface.default().routes
            if row.path.startswith("/market/forecast-intelligence")
        ]
        self.assertEqual(len(rows), len(self.EXPECTED))
        for row in rows:
            for forbidden in ("order", "fill", "execute", "broker", "ledger"):
                self.assertNotIn(forbidden, row.path)
        self.assertTrue(all(row.authenticated for row in rows))

    def test_contract_names_the_authorities_and_bounded_policy(self) -> None:
        service = ForecastIntelligenceService(
            ForecastIntelligenceSources(), store_path=":memory:", clock=lambda: NOW
        )
        try:
            contract = service.contract()
            navigation = service.navigation()
        finally:
            service.close()
        self.assertEqual(contract["authority"]["risk_authority"], "NM-08")
        self.assertEqual(contract["authority"]["execution_authority"], "NM-10")
        self.assertFalse(contract["selection"]["automatic_promotion"])
        self.assertFalse(contract["selection"]["test_used_for_selection"])
        self.assertTrue(contract["leakage_controls"]["calibration_holdout"])
        self.assertEqual(len(navigation["views"]), 9)

    def test_config_is_bounded_and_versioned(self) -> None:
        service = ForecastIntelligenceService(
            ForecastIntelligenceSources(),
            store_path=":memory:",
            clock=lambda: NOW,
            config=ForecastIntelligenceConfig(min_selection_samples=30),
        )
        try:
            updated = service.update_config({"min_selection_samples": 35}, actor="tester")
        finally:
            service.close()
        self.assertEqual(updated["config"]["min_selection_samples"], 35)
        self.assertEqual(updated["actor"], "tester")

    def test_batch_instruments_report_invalid_ids_instead_of_dropping_them(self) -> None:
        service = _service()
        try:
            body = service.instruments(["not-canonical", INSTRUMENT], HORIZON)
        finally:
            service.close()
        self.assertEqual(body["requested"], 2)
        self.assertEqual(len(body["invalid"]), 1)
        self.assertEqual(body["invalid"][0]["instrument_id"], "not-canonical")
        self.assertEqual(body["total"], 1)

    def test_history_total_is_not_truncated_by_the_page_limit(self) -> None:
        service = _service()
        try:
            for index in range(3):
                service.store.save(
                    "selection",
                    {"created_at": _iso(NOW + timedelta(seconds=index))},
                    record_id=f"history-{index}",
                    instrument_id=INSTRUMENT,
                    horizon=HORIZON,
                )
            body = service.history(instrument_id=INSTRUMENT, horizon=HORIZON, limit=1)
        finally:
            service.close()
        self.assertEqual(len(body["records"]), 1)
        self.assertEqual(body["total"], 3)

    def test_global_registry_preserves_the_same_model_across_instruments(self) -> None:
        sources = ForecastIntelligenceSources(
            models=lambda instrument_id=None, horizon=None: [
                {
                    "model_id": "naive",
                    "version": "v1",
                    "family": "naive",
                    "instrument_id": INSTRUMENT,
                    "state": "eligible",
                },
                {
                    "model_id": "naive",
                    "version": "v1",
                    "family": "naive",
                    "instrument_id": "XNSE:BBB",
                    "state": "eligible",
                },
            ],
            training_runs=lambda limit=20: [
                _training_run(("naive", 0.010, 0.020), instrument=INSTRUMENT),
                _training_run(("naive", 0.030, 0.040), instrument="XNSE:BBB"),
            ],
            ensemble_models=lambda instrument_id=None, horizon=None: [],
        )
        service = ForecastIntelligenceService(sources, store_path=":memory:", clock=lambda: NOW)
        try:
            body = service.registry()
        finally:
            service.close()
        self.assertEqual(body["scope"], "all")
        self.assertEqual(body["total"], 2)
        self.assertEqual(
            {row["instrument_id"] for row in body["models"]},
            {INSTRUMENT, "XNSE:BBB"},
        )

    def test_global_registry_reports_registry_only_rows_per_instrument(self) -> None:
        sources = ForecastIntelligenceSources(
            models=lambda instrument_id=None, horizon=None: [
                {
                    "model_id": "naive",
                    "version": "v1",
                    "family": "naive",
                    "instrument_id": INSTRUMENT,
                    "state": "eligible",
                },
                {
                    "model_id": "naive",
                    "version": "v1",
                    "family": "naive",
                    "instrument_id": "XNSE:BBB",
                    "state": "retired",
                },
            ],
            training_runs=lambda limit=20: [],
            ensemble_models=lambda instrument_id=None, horizon=None: [],
        )
        service = ForecastIntelligenceService(sources, store_path=":memory:", clock=lambda: NOW)
        try:
            body = service.registry()
        finally:
            service.close()
        self.assertEqual(body["total"], 2)
        self.assertEqual(
            {row["instrument_id"]: row["state"] for row in body["models"]},
            {INSTRUMENT: "eligible", "XNSE:BBB": "retired"},
        )


class ApplicationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
        except Exception as exc:  # pragma: no cover - optional dev dependency
            raise unittest.SkipTest(f"fastapi.testclient requires httpx: {exc}") from exc
        cls._tmp = TemporaryDirectory()
        cls._env = mock.patch.dict(
            os.environ,
            {
                "NOVACONTROL_API_TOKEN": "pi23-test-token",
                "NOVACONTROL_AUTONOMY_OPERATOR_TOKEN": "pi23-operator-token",
            },
            clear=False,
        )
        cls._env.start()
        from novacontrol.api.app import create_app

        cls._client = TestClient(create_app(data_dir=cls._tmp.name))
        cls._client.__enter__()
        cls._headers = {"Authorization": "Bearer pi23-test-token"}
        cls._operator = {**cls._headers, "X-Autonomy-Operator-Token": "pi23-operator-token"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls._client.__exit__(None, None, None)
        cls._env.stop()
        cls._tmp.cleanup()

    def test_actual_application_opens_pi23_service(self) -> None:
        response = self._client.get("/market/forecast-intelligence/status", headers=self._headers)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["phase"], "PI-23")
        self.assertTrue(body["initialised"])
        self.assertFalse(body["authority"]["automatic_promotion"])

    def test_pi23_reads_require_authentication(self) -> None:
        for path in (
            "/market/forecast-intelligence/status",
            "/market/forecast-intelligence/contracts",
            "/market/forecast-intelligence/health",
        ):
            self.assertEqual(self._client.get(path).status_code, 401)

    def test_analysis_requires_operator_permission(self) -> None:
        response = self._client.post(
            "/market/forecast-intelligence/analyze",
            headers=self._headers,
            json={"instrument_id": INSTRUMENT, "horizon": HORIZON},
        )
        self.assertEqual(response.status_code, 403, response.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
