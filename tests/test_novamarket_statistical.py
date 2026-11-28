"""PI-05 statistical baseline tests.

These use deterministic synthetic observations.  They prove contracts, numerical
paths, leakage-safe splitting and failure isolation; they are not a claim of
market accuracy.
"""

from __future__ import annotations

import math
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Sequence
from unittest import mock
from zoneinfo import ZoneInfo

try:
    from fastapi.testclient import TestClient  # requires httpx
except Exception:  # pragma: no cover - optional dev dependency
    TestClient = None  # type: ignore[assignment]

from novacontrol.novamarket.forecasting.features import PriceBar
from novacontrol.novamarket.marketdata.models import (
    AssetClass,
    DataInterval,
    Instrument,
)
from novacontrol.novamarket.prediction.models import (
    CalibrationStatus,
    Direction,
    ForecastHorizon,
    ProbabilityStatus,
)
from novacontrol.novamarket.prediction.store import PredictionStore
from novacontrol.novamarket.statistical import (
    MODEL_IDS,
    ForecastDataError,
    ModelConfig,
    StatisticalForecastingService,
    StatisticalValidationError,
    UnsupportedHorizonError,
    adf_test,
    classify_direction,
    config_candidates,
    evaluate_models,
    fit_arima,
    forecast_arima,
    forecast_model,
    horizon_steps,
    walk_forward_origins,
)
from novacontrol.novamarket.statistical.metrics import (
    PredictionRow,
    StatisticalMetrics,
    evaluate_rows,
)
from novacontrol.novamarket.statistical.validation import (
    SELECTION_MARGIN_PCT,
    ModelComparison,
    _select_with_margin,
    default_resolution,
)
from novacontrol.api.app import create_app
from novacontrol.application import NovaControlApplication
from novacontrol.browser import NoopBrowserRunner
from novacontrol.desktop import NoopDesktopRunner

_START = datetime(2024, 1, 1, tzinfo=UTC)


def _bars(
    count: int = 90,
    *,
    start: datetime = _START,
    close: Any = lambda index: 100.0 + 0.15 * index + 2.0 * math.sin(index / 3.0),
    step: timedelta = timedelta(days=1),
) -> list[PriceBar]:
    rows: list[PriceBar] = []
    for index in range(count):
        price = float(close(index))
        rows.append(
            PriceBar(
                observed_at=start + step * index,
                open=price - 0.5,
                high=price + 1.0,
                low=max(0.01, price - 1.0),
                close=price,
                volume=10_000 + index,
            )
        )
    return rows


def _request_bars(rows: Sequence[PriceBar]) -> list[dict[str, Any]]:
    return [
        {
            "observed_at": row.observed_at.isoformat(),
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
        }
        for row in rows
    ]


class NaiveAndMovingAverageTests(unittest.TestCase):
    def test_last_value_and_random_walk_are_point_baselines(self) -> None:
        rows = _bars(40)
        last = forecast_model("naive_last", [row.close for row in rows], 1, bars=rows)
        walk = forecast_model("random_walk", [row.close for row in rows], 1, bars=rows)
        self.assertAlmostEqual(last.predicted_price, rows[-1].close)
        self.assertEqual(last.predicted_return, 0.0)
        self.assertFalse(last.interval_supported)
        self.assertEqual(last.direction, Direction.NEUTRAL)
        self.assertAlmostEqual(walk.predicted_price, rows[-1].close)
        self.assertTrue(walk.interval_supported)
        self.assertLessEqual(walk.lower_return, 0.0)
        self.assertGreaterEqual(walk.upper_return, 0.0)

    def test_historical_return_uses_the_sampled_returns(self) -> None:
        rows = _bars(
            45,
            close=lambda index: 100.0 * (1.01**index),
        )
        forecast = forecast_model(
            "historical_return", [row.close for row in rows], 1, bars=rows
        )
        self.assertGreater(forecast.predicted_return, 0.0)
        self.assertGreater(forecast.predicted_price, rows[-1].close)
        self.assertIsNotNone(forecast.fit.residual_std)

    def test_seasonal_naive_uses_the_equivalent_prior_period(self) -> None:
        pattern = [100.0, 101.0, 102.0, 103.0, 104.0]
        rows = _bars(60, close=lambda index: pattern[index % 5])
        forecast = forecast_model(
            "seasonal_naive",
            [row.close for row in rows],
            1,
            ModelConfig(seasonal=True, seasonal_period=5, min_observations=30),
            bars=rows,
        )
        # The last bar is the fifth value; one step ahead is the first value of
        # the next equivalent period.
        self.assertAlmostEqual(forecast.predicted_price, pattern[0])

    def test_moving_average_windows_are_configurable_and_explicit(self) -> None:
        values = [10.0] * 30 + [1.0, 2.0, 3.0]
        rows = _bars(33, close=lambda index: values[index])
        sma = forecast_model(
            "sma",
            [row.close for row in rows],
            1,
            ModelConfig(window=3, min_observations=30),
            bars=rows,
        )
        self.assertAlmostEqual(sma.predicted_price, 2.0)
        self.assertIn("level", sma.fit.parameters.get("method", ""))
        wma = forecast_model(
            "wma",
            [row.close for row in rows],
            1,
            ModelConfig(window=3, min_observations=30),
            bars=rows,
        )
        self.assertAlmostEqual(wma.predicted_price, (1.0 + 2 * 2.0 + 3 * 3.0) / 6.0)
        ema = forecast_model(
            "ema",
            [row.close for row in rows],
            1,
            ModelConfig(window=3, min_observations=30),
            bars=rows,
        )
        self.assertTrue(math.isfinite(ema.predicted_price))
        self.assertTrue(ema.interval_supported)

    def test_a_short_window_outside_bounds_is_rejected(self) -> None:
        with self.assertRaises(StatisticalValidationError):
            ModelConfig(window=1)

    def test_the_last_value_baseline_does_not_need_a_smoothing_window(self) -> None:
        rows = _bars(2, close=lambda index: 100.0 + index)
        forecast = forecast_model(
            "naive_last", [row.close for row in rows], 1, bars=rows
        )
        self.assertAlmostEqual(forecast.predicted_price, 101.0)

    def test_invalid_config_values_are_named_validation_errors(self) -> None:
        with self.assertRaises(StatisticalValidationError):
            ModelConfig.from_dict({"interval_level": "not-a-number"})
        with self.assertRaises(StatisticalValidationError):
            ModelConfig.from_dict({"max_arima_order": 2, "arima_order": [2, 1, 1]})

    def test_arima_candidates_respect_a_user_order_bound(self) -> None:
        candidates = config_candidates("arima", ModelConfig(max_arima_order=2))
        self.assertTrue(candidates)
        self.assertTrue(
            all(
                candidate.arima_order[0] + candidate.arima_order[2] <= 2
                for candidate in candidates
            )
        )

    def test_seasonal_interval_records_horizon_scaling(self) -> None:
        rows = _bars(
            100,
            close=lambda index: 100.0
            + 2.0 * (index % 5)
            + 0.1 * math.sin(index * 1.7),
        )
        forecast = forecast_model(
            "seasonal_naive",
            [row.close for row in rows],
            3,
            ModelConfig(
                seasonal=True,
                seasonal_period=5,
                min_observations=30,
            ),
            bars=rows,
        )
        self.assertEqual(forecast.diagnostics["horizon_scaling"], "sqrt(steps)")
        self.assertLessEqual(forecast.lower_return, forecast.predicted_return)
        self.assertGreaterEqual(forecast.upper_return, forecast.predicted_return)


class ExponentialAndArimaTests(unittest.TestCase):
    def test_simple_holt_and_holt_winters(self) -> None:
        rows = _bars(
            100,
            close=lambda index: 100.0 + 0.3 * index + 5.0 * (index % 5),
        )
        ses = forecast_model("ses", [row.close for row in rows], 1, bars=rows)
        holt = forecast_model("holt", [row.close for row in rows], 1, bars=rows)
        hw = forecast_model(
            "holt_winters",
            [row.close for row in rows],
            1,
            ModelConfig(
                seasonal=True,
                seasonal_period=5,
                allow_low_seasonality=True,
                min_observations=30,
            ),
            bars=rows,
        )
        for forecast in (ses, holt, hw):
            self.assertTrue(math.isfinite(forecast.predicted_price))
            self.assertGreater(forecast.predicted_price, 0.0)
            self.assertTrue(forecast.interval_supported)
        self.assertGreater(holt.predicted_price, ses.predicted_price - 10.0)

    def test_holt_winters_refuses_weak_seasonality(self) -> None:
        rows = _bars(90, close=lambda index: 100.0 + 0.01 * index)
        with self.assertRaises(Exception) as caught:
            forecast_model(
                "holt_winters",
                [row.close for row in rows],
                1,
                ModelConfig(
                    seasonal=True,
                    seasonal_period=5,
                    min_observations=30,
                ),
                bars=rows,
            )
        self.assertIn("seasonal", str(caught.exception).lower())

    def test_arima_fits_stationary_ar_data_and_diagnoses_residuals(self) -> None:
        rows = _bars(
            100,
            close=lambda index: 100.0
            + 0.6 ** index * 5.0
            + 0.8 * math.sin(index / 2.0),
        )
        result, config = fit_arima(
            [row.close for row in rows],
            ModelConfig(
                arima_order=(1, 0, 0),
                max_arima_order=3,
                min_observations=30,
            ),
        )
        self.assertEqual(config.arima_order, (1, 0, 0))
        self.assertTrue(math.isfinite(result.residual_std))
        self.assertIsNotNone(result.stationarity.get("after"))
        expected_return, lower, upper, diagnostics = forecast_arima(
            result,
            2,
            last_price=rows[-1].close,
            interval_level=config.interval_level,
        )
        self.assertEqual(diagnostics["steps"], 2)
        self.assertTrue(math.isfinite(expected_return))
        self.assertIsNotNone(lower)
        self.assertIsNotNone(upper)
        self.assertLessEqual(lower, upper)

    def test_stationarity_test_detects_a_random_walk(self) -> None:
        random_walk = [100.0]
        for index in range(1, 80):
            random_walk.append(random_walk[-1] + (1.0 if index % 3 else -0.9))
        report = adf_test(random_walk)
        self.assertGreater(report["statistic"], -2.86)
        self.assertFalse(report["stationary"])

    def test_arima_fails_cleanly_with_insufficient_history(self) -> None:
        rows = _bars(12)
        with self.assertRaises(Exception) as caught:
            forecast_model("arima", [row.close for row in rows], 1, bars=rows)
        self.assertIn("needs at least", str(caught.exception).lower())


class HorizonAndTargetTests(unittest.TestCase):
    def test_all_supported_horizons_have_step_conversions(self) -> None:
        expected = {
            "5m": ("1m", 5),
            "15m": ("5m", 3),
            "20m": ("5m", 4),
            "1h": ("15m", 4),
            "1d": ("1d", 1),
            "3d": ("1d", 3),
            "5d": ("1d", 5),
            "30d": ("1d", 30),
            "90d": ("1d", 90),
        }
        for horizon, (resolution, steps) in expected.items():
            with self.subTest(horizon=horizon):
                self.assertEqual(horizon_steps(horizon, resolution), steps)
                self.assertEqual(default_resolution(horizon).value, resolution)
        # Daily/session horizons are not silently reinterpreted from intraday
        # bars, because one hourly bar is not one trading day of model steps.
        for horizon in ("1d", "3d", "5d", "30d", "90d"):
            with self.subTest(horizon=horizon), self.assertRaises(
                StatisticalValidationError
            ):
                horizon_steps(horizon, "1h")

    def test_a_daily_bar_cannot_claim_a_five_minute_forecast(self) -> None:
        with self.assertRaises(UnsupportedHorizonError):
            horizon_steps("5m", DataInterval.DAY_1)
        with self.assertRaises(UnsupportedHorizonError):
            horizon_steps("1h", DataInterval.DAY_1)

    def test_target_uses_the_exchange_calendar(self) -> None:
        instrument = Instrument(
            instrument_id="XNSE:RELIANCE",
            symbol="RELIANCE",
            name="Reliance",
            asset_class=AssetClass.EQUITY,
            exchange="NSE",
            mic="XNSE",
            currency="INR",
            timezone="Asia/Kolkata",
            provider_symbols={"fixture": "RELIANCE"},
        )
        fake_market = SimpleNamespace(
            get_instrument=lambda instrument_id: instrument
        )
        service = StatisticalForecastingService(market_data=fake_market)
        cutoff = datetime(2025, 1, 3, 10, 15, tzinfo=UTC)  # Friday 15:45 IST
        target = service._target_at(
            "XNSE:RELIANCE", cutoff, ForecastHorizon.DAY_1, 1
        )
        local = target.astimezone(ZoneInfo("Asia/Kolkata"))
        self.assertEqual(local.weekday(), 0)  # the next session is Monday
        service.close()

    def test_walk_forward_targets_are_always_strictly_future(self) -> None:
        origins = walk_forward_origins(100, 50, 3, start=50, end=80)
        self.assertTrue(origins)
        for origin, target in origins:
            self.assertLessEqual(origin, target)
            self.assertGreaterEqual(target, origin + 2)
        self.assertEqual(len({target for _origin, target in origins}), len(origins))


class DirectionAndMetricsTests(unittest.TestCase):
    def test_direction_handles_neutral_and_crossing_intervals(self) -> None:
        direction, _ = classify_direction(
            0.001, -0.01, 0.02, neutral_band_pct=0.5, interval_supported=True
        )
        self.assertEqual(direction, Direction.NEUTRAL)
        direction, _ = classify_direction(
            0.02, -0.01, 0.05, neutral_band_pct=0.5, interval_supported=True
        )
        self.assertEqual(direction, Direction.UNCERTAIN)
        direction, _ = classify_direction(
            0.02, 0.001, 0.05, neutral_band_pct=0.5, interval_supported=True
        )
        self.assertEqual(direction, Direction.UP)
        direction, _ = classify_direction(
            -0.02, -0.05, -0.001, neutral_band_pct=0.5, interval_supported=True
        )
        self.assertEqual(direction, Direction.DOWN)

    def test_metrics_are_computed_row_by_row(self) -> None:
        rows = [
            PredictionRow(
                actual_price=102.0,
                predicted_price=101.0,
                actual_return=0.02,
                predicted_return=0.01,
                lower_return=-0.01,
                upper_return=0.03,
                actual_direction="up",
                predicted_direction="up",
                interval_supported=True,
                regime="weak_up",
                horizon="1d",
                instrument_id="XNSE:ALPHA",
                model_id="sma",
            ),
            PredictionRow(
                actual_price=98.0,
                predicted_price=99.0,
                actual_return=-0.02,
                predicted_return=-0.01,
                lower_return=-0.03,
                upper_return=0.01,
                actual_direction="down",
                predicted_direction="up",
                interval_supported=True,
                regime="weak_down",
                horizon="1d",
                instrument_id="XNSE:ALPHA",
                model_id="sma",
            ),
        ]
        metrics = evaluate_rows(
            rows, model_id="sma", instrument_id="XNSE:ALPHA", horizon="1d"
        )
        self.assertEqual(metrics.samples, 2)
        self.assertAlmostEqual(metrics.price_mae, 1.0)
        self.assertAlmostEqual(metrics.directional_accuracy, 0.5)
        self.assertEqual(metrics.confusion["up"]["up"], 1)
        self.assertEqual(metrics.confusion["down"]["up"], 1)
        self.assertAlmostEqual(metrics.interval_coverage, 1.0)
        self.assertAlmostEqual(metrics.mean_interval_width, 0.04)
        self.assertIn("weak_up", metrics.coverage_by_regime)


class LeakageSafeEvaluationTests(unittest.TestCase):
    def test_validation_selects_and_test_is_not_used(self) -> None:
        rows = _bars(
            280,
            close=lambda index: 100.0
            + 0.05 * index
            + 3.0 * math.sin(index / 5.0)
            + (12.0 if index > 250 else 0.0),
        )
        report = evaluate_models(
            [row.close for row in rows],
            rows,
            instrument_id="XNSE:ALPHA",
            horizon="1d",
            resolution="1d",
            models=("naive_last", "random_walk", "sma", "ses"),
            regime_lookup=lambda moment: (
                "strong_up" if moment.day % 2 else "sideways"
            ),
        )
        self.assertTrue(report.leakage_safe)
        self.assertFalse(report.test_used_for_selection)
        self.assertIn(report.selected_model, {"naive_last", "random_walk", "sma", "ses"})
        self.assertEqual(report.selection_metric, "validation_return_mae")
        self.assertEqual(report.selection_margin_pct, SELECTION_MARGIN_PCT)
        self.assertIn(report.selection_decision, {"baseline", "challenger"})
        if report.selection_decision == "baseline":
            self.assertEqual(report.selected_model, "random_walk")
        else:
            self.assertNotEqual(report.selected_model, "random_walk")
            self.assertIsNotNone(report.challenger_validation_mae)
            self.assertIsNotNone(report.required_challenger_mae)
            self.assertLessEqual(
                report.challenger_validation_mae or 0.0,
                report.required_challenger_mae or 0.0,
            )
        payload = report.to_dict()
        self.assertEqual(payload["selection_margin_pct"], SELECTION_MARGIN_PCT)
        self.assertEqual(payload["selection_decision"], report.selection_decision)
        self.assertGreater(report.validation_origins, 0)
        self.assertGreater(report.test_origins, 0)
        self.assertTrue(report.regime_samples)
        # The test tail is visible in the test range but is not part of the
        # validation range; selecting on validation remains reproducible.
        self.assertGreater(
            report.test_range.end or _START, report.validation_range.end or _START
        )
        for model in report.models.values():
            self.assertIsNotNone(model.validation.return_mae)
            self.assertIsNotNone(model.test)

    def test_invalid_selection_margin_is_rejected(self) -> None:
        rows = _bars(280)
        for margin in (-0.1, 100.0, float("nan")):
            with self.assertRaises(StatisticalValidationError):
                evaluate_models(
                    [row.close for row in rows],
                    rows,
                    instrument_id="XNSE:ALPHA",
                    horizon="1d",
                    resolution="1d",
                    models=("random_walk", "sma"),
                    selection_margin_pct=margin,
                )

    def test_random_walk_baseline_is_added_when_the_caller_omits_it(self) -> None:
        rows = _bars(280)
        report = evaluate_models(
            [row.close for row in rows],
            rows,
            instrument_id="XNSE:ALPHA",
            horizon="1d",
            resolution="1d",
            models=("sma",),
        )
        self.assertIn("random_walk", report.models)
        self.assertEqual(report.baseline_model, "random_walk")
        self.assertIn(report.selected_model, {"sma", "random_walk"})
        self.assertIn(report.selection_decision, {"baseline", "challenger"})

    def test_insufficient_history_is_reported_not_fabricated(self) -> None:
        rows = _bars(40)
        with self.assertRaises(StatisticalValidationError):
            evaluate_models(
                [row.close for row in rows],
                rows,
                instrument_id="XNSE:ALPHA",
                horizon="1d",
                resolution="1d",
                models=("random_walk",),
            )


class ValidationMarginSelectionTests(unittest.TestCase):
    """The challenger must clear a pre-registered validation-only hurdle."""

    @staticmethod
    def _comparison(model_id: str, mae: float, *, samples: int = 10) -> ModelComparison:
        return ModelComparison(
            model_id=model_id,
            config=ModelConfig(),
            validation=StatisticalMetrics(samples=samples, return_mae=mae),
            test=None,
            validation_rows=samples,
            test_rows=0,
        )

    def test_challenger_below_five_percent_margin_retains_baseline(self) -> None:
        self.assertEqual(SELECTION_MARGIN_PCT, 5.0)
        decision = _select_with_margin(
            {
                "random_walk": self._comparison("random_walk", 0.100),
                "sma": self._comparison("sma", 0.096),
            },
            minimum_samples=3,
            margin_pct=5.0,
        )
        self.assertEqual(decision.decision, "baseline")
        self.assertEqual(decision.selected.model_id, "random_walk")
        self.assertIsNotNone(decision.challenger)
        assert decision.challenger is not None
        self.assertEqual(decision.challenger.model_id, "sma")
        self.assertAlmostEqual(decision.required_challenger_mae, 0.095)
        self.assertAlmostEqual(decision.baseline_validation_mae, 0.100)
        self.assertAlmostEqual(decision.challenger_validation_mae or 0.0, 0.096)

    def test_challenger_clearing_five_percent_margin_is_selected(self) -> None:
        decision = _select_with_margin(
            {
                "random_walk": self._comparison("random_walk", 0.100),
                "sma": self._comparison("sma", 0.095),
            },
            minimum_samples=3,
            margin_pct=5.0,
        )
        self.assertEqual(decision.decision, "challenger")
        self.assertEqual(decision.selected.model_id, "sma")
        self.assertAlmostEqual(decision.required_challenger_mae, 0.095)

    def test_baseline_only_is_retained_not_relabelled(self) -> None:
        decision = _select_with_margin(
            {"random_walk": self._comparison("random_walk", 0.100)},
            minimum_samples=3,
            margin_pct=5.0,
        )
        self.assertEqual(decision.decision, "baseline")
        self.assertEqual(decision.selected.model_id, "random_walk")
        self.assertIsNone(decision.challenger)

    def test_missing_or_ineligible_baseline_abstains(self) -> None:
        with self.assertRaisesRegex(StatisticalValidationError, "baseline"):
            _select_with_margin(
                {"sma": self._comparison("sma", 0.050)},
                minimum_samples=3,
                margin_pct=5.0,
            )
        with self.assertRaisesRegex(StatisticalValidationError, "baseline"):
            _select_with_margin(
                {
                    "random_walk": self._comparison("random_walk", 0.100, samples=2),
                    "sma": self._comparison("sma", 0.050),
                },
                minimum_samples=3,
                margin_pct=5.0,
            )


class ServiceTests(unittest.TestCase):
    def test_generate_persists_reopens_and_withholds_probabilities(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "pi05.sqlite3"
            rows = _bars(90)
            service = StatisticalForecastingService(
                store_path=path,
                dataset_service=_FakeDatasetService(rows),
            )
            import asyncio

            try:
                result = asyncio.run(
                    service.generate(
                        "XNSE:ALPHA",
                        "1d",
                        as_of=rows[-1].observed_at,
                        model_id="random_walk",
                        bars=_request_bars(rows),
                        dataset_id="fixture",
                        dataset_version="v1",
                    )
                )
                self.assertEqual(result["probability_status"], "unavailable")
                self.assertIsNone(result["p_up"])
                self.assertEqual(result["calibration_status"], "not_applicable")
                self.assertIn("forecast_id", result)
                self.assertTrue(result["persisted"])
                stored = service.forecast(result["forecast_id"])
                self.assertEqual(stored["forecast_id"], result["forecast_id"])
                self.assertEqual(stored["dataset_version"], "v1")
                models = service.models()["models"]
                self.assertTrue(models)
                self.assertTrue(all(item["state"] == "eligible" for item in models))
            finally:
                service.close()
            reopened = PredictionStore(path)
            try:
                self.assertIsNotNone(reopened.get_forecast(result["forecast_id"]))
            finally:
                reopened.close()

    def test_quality_provenance_filters_invalid_and_preserves_stale(self) -> None:
        rows = _bars(90)
        service = StatisticalForecastingService()
        import asyncio

        try:
            stale_bars = _request_bars(rows)
            stale_bars[-1]["quality"] = "stale"
            stale_bars[-1]["freshness"] = "stale"
            stale = asyncio.run(
                service.generate(
                    "XNSE:ALPHA",
                    "1d",
                    as_of=rows[-1].observed_at,
                    model_id="naive_last",
                    bars=stale_bars,
                )
            )
            self.assertEqual(stale["data_quality"], "stale")
            self.assertEqual(stale["forecast_validity"], "stale")
            invalid_bars = _request_bars(rows)
            invalid_bars[-1]["quality"] = "invalid"
            valid = asyncio.run(
                service.generate(
                    "XNSE:ALPHA",
                    "1d",
                    as_of=rows[-1].observed_at,
                    model_id="naive_last",
                    bars=invalid_bars,
                )
            )
            self.assertEqual(valid["data_quality"], "valid")
            self.assertAlmostEqual(
                float(valid["predicted_price"]), rows[-2].close
            )
        finally:
            service.close()

    def test_each_cutoff_persists_a_distinct_forecast_lifecycle(self) -> None:
        import asyncio

        with TemporaryDirectory() as directory:
            path = Path(directory) / "pi05.sqlite3"
            rows = _bars(90)
            service = StatisticalForecastingService(
                store_path=path,
                dataset_service=_FakeDatasetService(rows),
            )
            try:
                first = asyncio.run(
                    service.generate(
                        "XNSE:ALPHA",
                        "1d",
                        as_of=rows[-2].observed_at,
                        model_id="random_walk",
                        bars=_request_bars(rows),
                        dataset_id="fixture",
                        dataset_version="v1",
                    )
                )
                second = asyncio.run(
                    service.generate(
                        "XNSE:ALPHA",
                        "1d",
                        as_of=rows[-1].observed_at,
                        model_id="random_walk",
                        bars=_request_bars(rows),
                        dataset_id="fixture",
                        dataset_version="v1",
                    )
                )
                self.assertTrue(first["persisted"])
                self.assertTrue(second["persisted"])
                self.assertNotEqual(first["lifecycle_id"], second["lifecycle_id"])
                self.assertIsNotNone(service.forecast(first["forecast_id"]))
                self.assertIsNotNone(service.forecast(second["forecast_id"]))
            finally:
                service.close()

    def test_future_bars_never_enter_the_fit(self) -> None:
        rows = _bars(90)
        cutoff = rows[89].observed_at
        future = _bars(
            10,
            start=cutoff + timedelta(days=1),
            close=lambda index: 10_000.0 + index,
        )
        service = StatisticalForecastingService()
        import asyncio

        try:
            body = asyncio.run(
                service.generate(
                    "XNSE:ALPHA",
                    "1d",
                    as_of=cutoff,
                    model_id="naive_last",
                    bars=_request_bars([*rows, *future]),
                )
            )
            self.assertAlmostEqual(float(body["predicted_price"]), rows[89].close)
            self.assertEqual(body["reference_price_timestamp"], cutoff.isoformat())
        finally:
            service.close()

    def test_one_failure_does_not_disable_another_model(self) -> None:
        rows = _bars(90)
        service = StatisticalForecastingService()
        import asyncio

        try:
            with self.assertRaises(Exception):
                asyncio.run(
                    service.generate(
                        "XNSE:ALPHA",
                        "1d",
                        as_of=rows[-1].observed_at,
                        model_id="not_a_model",
                        bars=_request_bars(rows),
                    )
                )
            body = asyncio.run(
                service.generate(
                    "XNSE:ALPHA",
                    "1d",
                    as_of=rows[-1].observed_at,
                    model_id="random_walk",
                    bars=_request_bars(rows),
                )
            )
            self.assertTrue(body["ok"])
            self.assertGreaterEqual(service.status()["failures"], 1)
            self.assertIn("last_error", service.status())
        finally:
            service.close()

    def test_a_model_fit_failure_is_registered_as_unavailable(self) -> None:
        rows = _bars(90)
        service = StatisticalForecastingService()
        try:
            service._register_unavailable(
                "arima",
                cutoff=rows[-1].observed_at,
                reason="controlled fixture failure",
            )
            models = {item["model_id"]: item for item in service.models()["models"]}
            self.assertIn("arima", models)
            self.assertEqual(models["arima"]["state"], "unavailable")
            self.assertIn("controlled fixture failure", str(models["arima"]))
        finally:
            service.close()


class _FakeDatasetService:
    def __init__(self, rows: Sequence[PriceBar]) -> None:
        self.rows = list(rows)

    def metadata(self, dataset_id: str, version: str | None = None) -> dict[str, Any]:
        return {
            "dataset_id": dataset_id,
            "version": version or "v-fixture",
            "resolution": "1d",
        }

    def bars(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        end = kwargs.get("end")
        chosen = [
            {
                "observed_at": row.observed_at.isoformat(),
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "quality": "valid",
            }
            for row in self.rows
            if end is None or row.observed_at <= end
        ]
        return {"items": chosen}


class _FakeFeatureEngine:
    def generate(self, request: Any, *, bars: Any = None) -> Any:
        # PI-04's real result types are exercised by the feature suite; this
        # adapter proves that PI-05 consumes the PI-04 regime/feature-set output.
        moment = request.as_of
        primary = "strong_down" if moment.day % 2 else "weak_up"
        return SimpleNamespace(
            regime=SimpleNamespace(primary=primary),
            feature_set=SimpleNamespace(
                version="pi04-feature-set-1.0", feature_set_id="fs-fixture"
            ),
        )


class _FakeFeatureService:
    def __init__(self) -> None:
        self.engine = _FakeFeatureEngine()


class FeatureRegimeIntegrationTests(unittest.TestCase):
    def test_pi04_regime_and_feature_set_labels_are_consumed(self) -> None:
        rows = _bars(220)
        service = StatisticalForecastingService(
            feature_service=_FakeFeatureService()
        )
        import asyncio

        try:
            body = asyncio.run(
                service.generate(
                    "XNSE:ALPHA",
                    "1d",
                    as_of=rows[-1].observed_at,
                    model_id="random_walk",
                    bars=_request_bars(rows),
                )
            )
            self.assertEqual(body["feature_set_version"], "pi04-feature-set-1.0")
            # The shared selection slot belongs to NM-06.  PI-05 records its
            # choice in the training run and must leave this value untouched.
            service.forecast_store.save_selection(
                "XNSE:ALPHA",
                "1d",
                "nm06-only-model",
                {},
                rows[-1].observed_at,
            )
            report = asyncio.run(
                service.evaluate(
                    "XNSE:ALPHA",
                    "1d",
                    as_of=rows[-1].observed_at,
                    bars=_request_bars(rows),
                    models=("random_walk", "sma"),
                )
            )
            self.assertIn("strong_down", report["regime_samples"])
            self.assertIn("weak_up", report["regime_samples"])
            self.assertIn("coverage_by_regime", report["models"]["sma"]["test"])
            self.assertEqual(
                service.forecast_store.selection("XNSE:ALPHA", "1d"),
                "nm06-only-model",
            )
            selected = report["selected_model"]
            follow_up = asyncio.run(
                service.generate(
                    "XNSE:ALPHA",
                    "1d",
                    as_of=rows[-1].observed_at,
                    bars=_request_bars(rows),
                )
            )
            self.assertEqual(follow_up["model_id"], selected)
            self.assertEqual(follow_up["model"]["config"], report["selected_config"])
            metadata = {
                item["model_id"]: item for item in service.models()["models"]
            }
            self.assertIn("validation", metadata[selected]["metrics"])
            self.assertIn("test", metadata[selected]["metrics"])
        finally:
            service.close()


class DatasetIntegrationTests(unittest.TestCase):
    def test_dataset_version_is_the_only_bar_source(self) -> None:
        rows = _bars(80)
        service = StatisticalForecastingService(
            dataset_service=_FakeDatasetService(rows)
        )
        import asyncio

        body = asyncio.run(
            service.generate(
                "XNSE:ALPHA",
                "1d",
                as_of=rows[-1].observed_at,
                model_id="naive_last",
                dataset_id="fixture",
                dataset_version="v-fixture",
            )
        )
        self.assertEqual(body["dataset_id"], "fixture")
        self.assertEqual(body["dataset_version"], "v-fixture")
        self.assertAlmostEqual(float(body["predicted_price"]), rows[-1].close)

    def test_dataset_without_training_quality_is_refused(self) -> None:
        rows = _bars(80)
        fake = _FakeDatasetService(rows)
        fake.metadata = lambda dataset_id, version=None: {  # type: ignore[method-assign]
            "dataset_id": dataset_id,
            "version": version or "v-fixture",
            "resolution": "1d",
            "quality": "unverified",
            "usable_for_training": False,
        }
        service = StatisticalForecastingService(dataset_service=fake)
        import asyncio

        try:
            with self.assertRaises(ForecastDataError):
                asyncio.run(
                    service.generate(
                        "XNSE:ALPHA",
                        "1d",
                        as_of=rows[-1].observed_at,
                        model_id="naive_last",
                        dataset_id="fixture",
                    )
                )
        finally:
            service.close()


class AppIntegrationTests(unittest.TestCase):
    """The real application and HTTP surface, not a mock service."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self._nova: NovaControlApplication | None = None
        self._patchers = [
            mock.patch(
                "novacontrol.api.app.NovaControlApplication",
                side_effect=self._application,
            ),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch(
                "novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner
            ),
        ]
        for patcher in self._patchers:
            patcher.start()
        self._client = TestClient(create_app())
        self._client.__enter__()

    def tearDown(self) -> None:
        self._client.__exit__(None, None, None)
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmp.cleanup()

    def _application(self, **kwargs: Any) -> NovaControlApplication:
        kwargs["data_dir"] = self._tmp.name
        application = NovaControlApplication(**kwargs)
        self._nova = application
        return application

    def test_status_exposes_the_statistical_subsystem_without_a_second_route(self) -> None:
        body = self._client.get("/market/forecasts/status").json()
        self.assertTrue(body["initialised"])
        statistical = body["statistical"]
        self.assertTrue(statistical["implemented"])
        self.assertEqual(statistical["phase"], "PI-05")
        self.assertIn("random_walk", statistical["models"])
        self.assertFalse(statistical["automatic_promotion"])
        self.assertEqual(statistical["risk_authority"], "NM-08")
        self.assertEqual(statistical["execution_authority"], "NM-10")

    def test_generate_list_read_and_models_use_the_existing_routes(self) -> None:
        rows = _bars(90)
        as_of = rows[-1].observed_at.isoformat()
        created = self._client.post(
            "/market/forecasts",
            json={
                "engine": "statistical",
                "instrument_id": "XNSE:ALPHA",
                "horizon": "1d",
                "model_id": "random_walk",
                "as_of": as_of,
                "bars": _request_bars(rows),
                "config": {"neutral_band_pct": 0.7},
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        body = created.json()
        self.assertEqual(body["model_id"], "random_walk")
        self.assertEqual(body["model"]["config"]["neutral_band_pct"], 0.7)
        self.assertEqual(body["probability_status"], "unavailable")
        forecast_id = body["forecast_id"]
        listed = self._client.get(
            "/market/forecasts", params={"engine": "statistical"}
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["total"], 1)
        one = self._client.get(f"/market/forecasts/{forecast_id}")
        self.assertEqual(one.status_code, 200, one.text)
        self.assertEqual(one.json()["forecast_id"], forecast_id)
        models = self._client.get(
            "/market/forecasts/models",
            params={
                "engine": "statistical",
                "instrument_id": "XNSE:ALPHA",
                "horizon": "1d",
            },
        )
        self.assertEqual(models.status_code, 200, models.text)
        self.assertTrue(models.json()["models"])
        self.assertIn("suitability", models.json())
        calibration = self._client.get(
            "/market/forecasts/calibration",
            params={"engine": "statistical", "model_id": "random_walk", "horizon": "1d"},
        )
        self.assertEqual(calibration.status_code, 200, calibration.text)
        self.assertEqual(calibration.json()["status"], "not_applicable")
        performance = self._client.get(
            "/market/forecasts/performance", params={"engine": "statistical"}
        )
        self.assertEqual(performance.status_code, 200, performance.text)
        self.assertEqual(performance.json()["engine"], "statistical")
        self.assertIsInstance(performance.json()["evaluations"], list)

    def test_bad_config_and_cutoff_are_client_errors_not_server_errors(self) -> None:
        rows = _bars(90)
        base = {
            "engine": "statistical",
            "instrument_id": "XNSE:ALPHA",
            "horizon": "1d",
            "model_id": "random_walk",
            "as_of": rows[-1].observed_at.isoformat(),
            "bars": _request_bars(rows),
        }
        bad_config = self._client.post(
            "/market/forecasts", json={**base, "config": {"window": 1}}
        )
        self.assertEqual(bad_config.status_code, 422, bad_config.text)
        bad_cutoff = self._client.post(
            "/market/forecasts", json={**base, "as_of": "not-a-timestamp"}
        )
        self.assertEqual(bad_cutoff.status_code, 422, bad_cutoff.text)


if __name__ == "__main__":
    unittest.main()
