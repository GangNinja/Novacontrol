"""NM-06: measured probabilistic forecasting.

The tests drive the real feature builder, the real estimators, the real
chronological validator, the real SQLite store and the real engine. Only the
NM-02 *facade* (and, where an event is needed, the radar body) is supplied by
the test; no mock is used to claim accuracy, calibration or coverage.
"""

from __future__ import annotations

import asyncio
import math
import random
import shutil
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from novacontrol.novamarket.contracts import (
    ForecastDataError,
    ForecastNotFoundError,
    ForecastStateError,
    ForecastValidationError,
)
from novacontrol.novamarket.forecasting import (
    HORIZON_SESSIONS,
    MODEL_IDS,
    MODEL_METADATA,
    CalibrationSample,
    FeatureSet,
    ForecastEngine,
    ForecastOutcome,
    ForecastPolicy,
    ForecastRecord,
    ForecastStatus,
    ForecastStore,
    Horizon,
    ModelRecord,
    ModelState,
    PriceBar,
    Regime,
    UncertaintyLevel,
    WalkForwardResult,
    brier_score,
    calibrate_probability,
    classify_regime,
    compute_features,
    ensure_model_transition,
    evaluate_models,
    forecast as estimate,
    interval_coverage,
    log_loss,
    mae,
    rmse,
    walk_forward_origins,
)
from novacontrol.novamarket.forecasting import engine as engine_module
from novacontrol.novamarket.forecasting import estimators
from novacontrol.novamarket.forecasting.metrics import (
    DirectionRow,
    ModelScore,
    confusion_matrix,
    directional_accuracy,
    direction_of,
    mean_interval_width,
    median_absolute_error,
    pinball_loss,
    predicted_direction,
)
from novacontrol.novamarket.forecasting.validation import best_model

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
ALPHA = "XNSE:ALPHA"


def _closes(count: int, *, seed: int = 11, start: float = 100.0, drift: float = 0.0004) -> list[float]:
    generator = random.Random(seed)
    values = [start]
    for _ in range(count - 1):
        values.append(max(1.0, values[-1] * (1.0 + generator.gauss(drift, 0.011))))
    return values


def _bar(
    at: datetime,
    close: float,
    *,
    volume: float | None = 1000.0,
    partial: bool = False,
    quality: str = "valid",
) -> PriceBar:
    return PriceBar(
        observed_at=at,
        open=close * 0.996,
        high=close * 1.008,
        low=close * 0.992,
        close=close,
        volume=volume,
    )


def _bars(count: int, *, end: datetime | None = None, seed: int = 11) -> list[PriceBar]:
    end = end or (NOW - timedelta(days=1))
    values = _closes(count, seed=seed)
    return [
        _bar(end - timedelta(days=count - 1 - index), value)
        for index, value in enumerate(values)
    ]


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, payload: dict[str, Any]) -> None:
        self.events.append((name, dict(payload)))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]


class _FakeMarketData:
    """The NM-02 facade: instrument metadata plus completed daily bars."""

    def __init__(self, bars: dict[str, list[Any]]) -> None:
        self._bars = bars
        self.instruments = {
            ALPHA: {
                "instrument_id": ALPHA,
                "symbol": "ALPHA",
                "exchange": "XNSE",
                "currency": "INR",
                "asset_class": "equity",
            }
        }

    def get_instrument(self, instrument_id: str) -> Any:
        if instrument_id not in self.instruments:
            raise KeyError(instrument_id)
        body = dict(self.instruments[instrument_id])
        return SimpleNamespace(to_dict=lambda: body)

    def bars(
        self, instrument_id: str, *, limit: int = 200, ascending: bool = False, **_: Any
    ) -> Any:
        rows = list(self._bars.get(instrument_id, ()))[-limit:]
        if not ascending:
            rows.reverse()
        return SimpleNamespace(items=rows)

    def append(self, instrument_id: str, bar: Any) -> None:
        self._bars.setdefault(instrument_id, []).append(bar)


def _engine(
    bars: list[Any] | None = None,
    *,
    publisher: Any = None,
    clock: Any = None,
    radar: Any = None,
    discovery: Any = None,
    policy: ForecastPolicy | None = None,
) -> tuple[ForecastEngine, ForecastStore]:
    store = ForecastStore(":memory:")
    market = _FakeMarketData({ALPHA: bars if bars is not None else _bars(160)})
    engine = ForecastEngine(
        store,
        market_data=market,
        radar=radar,
        discovery=discovery,
        publisher=publisher,
        clock=clock or (lambda: NOW),
        policy=policy,
    )
    return engine, store


class HorizonTests(unittest.TestCase):
    def test_five_horizons_are_defined_in_trading_sessions(self) -> None:
        self.assertEqual(
            {item.value: HORIZON_SESSIONS[item] for item in Horizon},
            {
                "next_session": 1,
                "few_sessions": 3,
                "one_week": 5,
                "one_month": 21,
                "three_months": 63,
            },
        )

    def test_policy_rejects_incoherent_thresholds(self) -> None:
        with self.assertRaises(ValueError):
            ForecastPolicy(directional_threshold_pct=0)
        with self.assertRaises(ValueError):
            ForecastPolicy(interval_low=0.9, interval_high=0.1)
        with self.assertRaises(ValueError):
            ForecastPolicy(min_training_bars=10)


class RecordContractTests(unittest.TestCase):
    def _record(self, **overrides: Any) -> ForecastRecord:
        values: dict[str, Any] = {
            "forecast_id": "fc_test",
            "instrument_id": ALPHA,
            "created_at": NOW,
            "data_cutoff": NOW,
            "horizon": Horizon.NEXT_SESSION,
            "horizon_sessions": 1,
            "target_at": NOW + timedelta(days=1),
            "model_id": "random_walk",
            "model_version": "v1",
            "feature_set_version": "f1",
            "predicted_return": 0.001,
            "predicted_price": 100.1,
            "p_up": 0.4,
            "p_down": 0.3,
            "p_sideways": 0.3,
            "lower_return": -0.02,
            "upper_return": 0.02,
            "lower_price": 98.0,
            "upper_price": 102.0,
            "uncertainty": UncertaintyLevel.MEDIUM,
            "uncertainty_width": 0.04,
            "data_quality": "good",
            "regime": Regime.SIDEWAYS,
            "event_ids": (),
            "candidate_score": None,
            "methodology_version": "m1",
            "digest": "d1",
        }
        values.update(overrides)
        return ForecastRecord(**values)

    def test_probabilities_must_sum_to_one(self) -> None:
        with self.assertRaises(ValueError):
            self._record(p_up=0.5, p_down=0.3, p_sideways=0.3)

    def test_prediction_must_lie_inside_the_interval(self) -> None:
        with self.assertRaises(ValueError):
            self._record(predicted_return=0.5, upper_return=0.1)

    def test_target_must_be_after_creation(self) -> None:
        with self.assertRaises(ValueError):
            self._record(target_at=NOW)

    def test_cutoff_cannot_be_after_creation(self) -> None:
        with self.assertRaises(ValueError):
            self._record(data_cutoff=NOW + timedelta(hours=1))

    def test_record_declares_it_is_not_a_price_prediction(self) -> None:
        body = self._record().to_dict()
        self.assertFalse(body["is_price_prediction"])
        self.assertFalse(body["is_investment_advice"])
        self.assertFalse(body["guaranteed"])


class ModelLifecycleTests(unittest.TestCase):
    def test_illegal_transition_raises(self) -> None:
        with self.assertRaises(ValueError):
            ensure_model_transition(ModelState.RETIRED, ModelState.ACTIVE)
        with self.assertRaises(ValueError):
            ensure_model_transition(ModelState.EXPERIMENTAL, ModelState.ACTIVE)

    def test_legal_paths_are_defined(self) -> None:
        self.assertEqual(
            ensure_model_transition(ModelState.VALIDATED, ModelState.ACTIVE),
            (ModelState.VALIDATED, ModelState.ACTIVE),
        )
        self.assertEqual(
            ensure_model_transition(ModelState.DEGRADED, ModelState.ACTIVE),
            (ModelState.DEGRADED, ModelState.ACTIVE),
        )


class FeatureTests(unittest.TestCase):
    def test_features_are_reproducible_from_the_same_bars(self) -> None:
        policy = ForecastPolicy()
        bars = _bars(160)
        first = compute_features(bars, instrument_id=ALPHA, cutoff=NOW, policy=policy)
        second = compute_features(bars, instrument_id=ALPHA, cutoff=NOW, policy=policy)
        assert first is not None and second is not None
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.values, second.values)

    def test_a_bar_after_the_cutoff_is_never_used(self) -> None:
        policy = ForecastPolicy()
        bars = _bars(160)
        cutoff = bars[-6].observed_at
        without = compute_features(
            bars[: -5], instrument_id=ALPHA, cutoff=cutoff, policy=policy
        )
        with_future = compute_features(
            [*bars[:-5], _bar(bars[-1].observed_at, 9_999.0)],
            instrument_id=ALPHA,
            cutoff=cutoff,
            policy=policy,
        )
        assert without is not None and with_future is not None
        self.assertEqual(without.digest, with_future.digest)
        self.assertEqual(without.last_close, with_future.last_close)

    def test_insufficient_history_returns_none(self) -> None:
        policy = ForecastPolicy()
        self.assertIsNone(
            compute_features(
                _bars(40), instrument_id=ALPHA, cutoff=NOW, policy=policy
            )
        )

    def test_features_cover_price_volume_volatility_and_technicals(self) -> None:
        features = compute_features(
            _bars(160), instrument_id=ALPHA, cutoff=NOW, policy=ForecastPolicy()
        )
        assert features is not None
        for name in (
            "ret_1",
            "ret_5",
            "ret_21",
            "vol_21",
            "vol_63",
            "atr_14_pct",
            "range_20_pct",
            "rel_volume",
            "drawdown_60",
            "trend_slope_20",
            "momentum_10",
            "sma_20_gap",
            "sma_60_gap",
            "ema_12_26",
            "rsi_14",
        ):
            with self.subTest(feature=name):
                self.assertIn(name, features.values)
                self.assertTrue(math.isfinite(features.values[name]))

    def test_regime_is_measurable_and_can_be_uncertain(self) -> None:
        features = compute_features(
            _bars(160), instrument_id=ALPHA, cutoff=NOW, policy=ForecastPolicy()
        )
        assert features is not None
        self.assertIsInstance(classify_regime(features), Regime)
        self.assertIsInstance(features, FeatureSet)


class EstimatorTests(unittest.TestCase):
    def _forecast(self, model_id: str, closes: list[float], horizon: int = 1) -> Any:
        return estimate(model_id, closes, horizon, ForecastPolicy())

    def test_every_model_is_deterministic(self) -> None:
        closes = _closes(220)
        for model_id in MODEL_IDS:
            with self.subTest(model=model_id):
                first = self._forecast(model_id, closes)
                second = self._forecast(model_id, closes)
                self.assertEqual(first, second)

    def test_every_distributional_model_sums_to_one(self) -> None:
        closes = _closes(220)
        for model_id in estimators.PROBABILISTIC_MODELS:
            with self.subTest(model=model_id):
                output = self._forecast(model_id, closes)
                total = (output.p_up or 0) + (output.p_down or 0) + (output.p_sideways or 0)
                self.assertAlmostEqual(total, 1.0, places=9)

    def test_interval_contains_its_point_forecast(self) -> None:
        closes = _closes(220)
        for model_id in estimators.PROBABILISTIC_MODELS:
            with self.subTest(model=model_id):
                output = self._forecast(model_id, closes)
                assert output.lower_return is not None
                assert output.upper_return is not None
                self.assertLessEqual(output.lower_return, output.predicted_return)
                self.assertLessEqual(output.predicted_return, output.upper_return)

    def test_zero_return_is_a_point_baseline_only(self) -> None:
        output = self._forecast("zero_return", _closes(160))
        self.assertEqual(output.predicted_return, 0.0)
        self.assertIsNone(output.p_up)
        self.assertIsNone(output.lower_return)

    def test_historical_mean_predicts_the_mean_not_the_median(self) -> None:
        closes = _closes(220)
        daily = [
            closes[index] / closes[index - 1] - 1.0
            for index in range(1, len(closes))
        ]
        output = self._forecast("historical_mean", closes)
        self.assertAlmostEqual(
            output.predicted_return, sum(daily) / len(daily), places=12
        )

    def test_drift_predicts_the_trailing_drift(self) -> None:
        closes = _closes(220)
        daily = [
            closes[index] / closes[index - 1] - 1.0
            for index in range(1, len(closes))
        ]
        output = self._forecast("drift", closes)
        self.assertAlmostEqual(
            output.predicted_return, sum(daily[-21:]) / 21.0, places=12
        )

    def test_insufficient_history_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._forecast("random_walk", _closes(40))

    def test_unknown_model_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._forecast("magic", _closes(160))

    def test_bootstrap_changes_with_its_input_history(self) -> None:
        first = self._forecast("bootstrap", _closes(160, seed=1))
        second = self._forecast("bootstrap", _closes(160, seed=2))
        self.assertNotEqual(first.predicted_return, second.predicted_return)

    def test_model_metadata_names_an_implemented_kind(self) -> None:
        for model_id in MODEL_IDS:
            kind, description = MODEL_METADATA[model_id]
            self.assertIn(kind, {"baseline", "statistical", "machine_learning"})
            self.assertTrue(description)


class MetricTests(unittest.TestCase):
    def test_mae_rmse_and_median_error(self) -> None:
        rows = [(0.10, 0.05), (-0.10, -0.05), (0.05, 0.0), (0.15, 0.10)]
        self.assertAlmostEqual(mae(rows) or 0.0, 0.05)
        self.assertAlmostEqual(rmse(rows) or 0.0, 0.05)
        self.assertAlmostEqual(median_absolute_error(rows) or 0.0, 0.05)

    def test_brier_and_log_loss_on_a_perfect_forecast(self) -> None:
        rows = [DirectionRow(0.05, 1.0, 0.0, 0.0)]
        self.assertAlmostEqual(brier_score(rows, 0.005) or 0.0, 0.0)
        self.assertAlmostEqual(log_loss(rows, 0.005) or 0.0, 0.0)

    def test_directional_accuracy_and_confusion(self) -> None:
        threshold = 0.005
        rows = [
            DirectionRow(0.05, 0.8, 0.1, 0.1),
            DirectionRow(-0.05, 0.1, 0.8, 0.1),
            DirectionRow(0.001, 0.6, 0.2, 0.2),
        ]
        self.assertAlmostEqual(directional_accuracy(rows, threshold) or 0.0, 2 / 3)
        matrix = confusion_matrix(rows, threshold)
        self.assertEqual(matrix["up"]["up"], 1)
        self.assertEqual(matrix["down"]["down"], 1)
        self.assertEqual(matrix["sideways"]["up"], 1)

    def test_interval_coverage_and_width(self) -> None:
        rows = [(0.0, -0.01, 0.01), (0.05, -0.01, 0.01)]
        self.assertAlmostEqual(interval_coverage(rows) or 0.0, 0.5)
        self.assertAlmostEqual(mean_interval_width(rows) or 0.0, 0.02)

    def test_pinball_loss_prefers_the_correct_side(self) -> None:
        self.assertEqual(pinball_loss(0.10, 0.10, 0.9), 0.0)
        # At the 90th percentile, over-predicting is the cheaper mistake.
        self.assertGreater(
            pinball_loss(0.10, 0.09, 0.9), pinball_loss(0.10, 0.11, 0.9)
        )

    def test_direction_edges_are_explicit(self) -> None:
        self.assertEqual(direction_of(0.006, 0.005), "up")
        self.assertEqual(direction_of(-0.006, 0.005), "down")
        self.assertEqual(direction_of(0.005, 0.005), "sideways")
        self.assertEqual(predicted_direction(0.2, 0.3, 0.5), "sideways")


class CalibrationTests(unittest.TestCase):
    def test_insufficient_samples_are_reported_not_calibrated(self) -> None:
        report = calibrate_probability(
            [CalibrationSample(0.5, True)] * 5,
            min_samples=30,
            holdout_fraction=0.4,
        )
        self.assertEqual(report.status, "insufficient")
        self.assertIsNone(report.brier_after)
        self.assertTrue(report.reason)

    def test_the_holdout_is_scored_out_of_sample(self) -> None:
        samples = [
            CalibrationSample(0.05 + (index % 10) * 0.09, (index % 4) == 0)
            for index in range(80)
        ]
        report = calibrate_probability(
            samples, min_samples=30, holdout_fraction=0.4
        )
        self.assertEqual(report.status, "calibrated")
        self.assertEqual(report.samples, 80)
        self.assertGreater(report.holdout_samples, 0)
        self.assertIsNotNone(report.brier_before)
        self.assertIsNotNone(report.brier_after)
        self.assertIsNotNone(report.calibrator)
        self.assertIn("earlier", " ".join(report.notes))

    def test_a_calibrator_is_monotone(self) -> None:
        samples = [
            CalibrationSample(probability, outcome)
            for probability, outcome in [
                (0.1, False),
                (0.2, False),
                (0.6, True),
                (0.7, True),
                (0.9, True),
            ]
        ]
        report = calibrate_probability(
            samples, min_samples=4, holdout_fraction=0.4
        )
        assert report.calibrator is not None
        values = [
            report.calibrator.calibrate(value) for value in (0.0, 0.2, 0.5, 0.8, 1.0)
        ]
        self.assertEqual(values, sorted(values))


class ValidationTests(unittest.TestCase):
    def test_origins_are_chronological_and_non_overlapping(self) -> None:
        origins = walk_forward_origins(200, 140, 5)
        self.assertTrue(origins)
        for index, (origin, target) in enumerate(origins):
            self.assertLess(origin, target)
            self.assertLess(target, 200)
            if index:
                self.assertGreaterEqual(origin, origins[index - 1][1])

    def test_an_insufficient_history_produces_no_scores(self) -> None:
        result = evaluate_models(_closes(60), 1, ForecastPolicy())
        self.assertEqual(result.scores, {})

    def test_every_model_is_scored_on_the_same_origins(self) -> None:
        result = evaluate_models(_closes(260), 1, ForecastPolicy())
        self.assertEqual(set(result.scores), set(MODEL_IDS))
        self.assertTrue(all(score.samples == result.origins for score in result.scores.values()))

    def test_a_baseline_comparison_is_always_available(self) -> None:
        result = evaluate_models(_closes(260), 1, ForecastPolicy())
        baseline = result.scores["random_walk"]
        self.assertIsNotNone(baseline.brier)
        self.assertIsNotNone(baseline.mae)
        best_id, best = best_model(result, minimum_samples=5)
        assert best is not None and best_id is not None
        self.assertLessEqual(best.brier or 1.0, baseline.brier or 0.0)

    def test_the_point_baseline_carries_no_probabilities(self) -> None:
        result = evaluate_models(_closes(260), 1, ForecastPolicy())
        zero = result.scores["zero_return"]
        self.assertIsNone(zero.brier)
        self.assertIsNone(zero.directional_accuracy)
        self.assertTrue(zero.notes)


class StoreTests(unittest.TestCase):
    def _record(self, index: int = 0, *, model_id: str = "random_walk") -> ForecastRecord:
        created = NOW - timedelta(days=index)
        return ForecastRecord(
            forecast_id=f"fc_{index}",
            instrument_id=ALPHA,
            created_at=created,
            data_cutoff=created,
            horizon=Horizon.NEXT_SESSION,
            horizon_sessions=1,
            target_at=created + timedelta(days=1),
            model_id=model_id,
            model_version="v1",
            feature_set_version="f1",
            predicted_return=0.001,
            predicted_price=100.1,
            p_up=0.4,
            p_down=0.3,
            p_sideways=0.3,
            lower_return=-0.02,
            upper_return=0.02,
            lower_price=98.0,
            upper_price=102.0,
            uncertainty=UncertaintyLevel.MEDIUM,
            uncertainty_width=0.04,
            data_quality="good",
            regime=Regime.SIDEWAYS,
            event_ids=(),
            candidate_score=None,
            methodology_version="m1",
            digest=f"d{index}",
        )

    def test_a_stored_forecast_round_trips(self) -> None:
        store = ForecastStore(":memory:")
        self.addCleanup(store.close)
        store.save_forecast(self._record())
        body = store.forecast("fc_0")
        assert body is not None
        self.assertEqual(body["forecast_id"], "fc_0")
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["p_up"], 0.4)

    def test_forecasts_are_immutable_in_the_database(self) -> None:
        folder = tempfile.mkdtemp()
        path = str(Path(folder) / "forecasts.sqlite")
        store = ForecastStore(path)
        store.save_forecast(self._record())
        store.close()
        connection = sqlite3.connect(path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE forecast_records SET model_id='rewritten' "
                    "WHERE forecast_id='fc_0'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM forecast_records")
            connection.rollback()
        finally:
            connection.close()
            shutil.rmtree(folder, ignore_errors=True)

    def test_an_outcome_is_recorded_once(self) -> None:
        store = ForecastStore(":memory:")
        self.addCleanup(store.close)
        store.save_forecast(self._record())
        outcome = ForecastOutcome(
            forecast_id="fc_0",
            instrument_id=ALPHA,
            status=ForecastStatus.EVALUATED,
            evaluated_at=NOW,
            actual_return=0.01,
            actual_price=101.0,
            error=-0.009,
            direction_correct=True,
            brier=0.5,
            log_loss=0.9,
            inside_interval=True,
        )
        self.assertTrue(store.record_outcome(outcome))
        self.assertFalse(store.record_outcome(outcome))

    def test_pending_due_respects_the_target(self) -> None:
        store = ForecastStore(":memory:")
        self.addCleanup(store.close)
        store.save_forecast(self._record())
        self.assertEqual(store.pending_due(NOW), [])
        self.assertEqual(len(store.pending_due(NOW + timedelta(days=2))), 1)

    def test_an_illegal_state_move_is_refused_and_leaves_the_row(self) -> None:
        store = ForecastStore(":memory:")
        self.addCleanup(store.close)
        store.upsert_model(
            ModelRecord(
                model_id="ewma",
                version="v1",
                state=ModelState.RETIRED,
                kind="statistical",
                description="test",
                trained_at=NOW,
                updated_at=NOW,
                instrument_id=ALPHA,
                horizon="next_session",
                metrics={"brier": 0.6},
            )
        )
        with self.assertRaises(ValueError):
            store.set_model_state(
                "ewma", ALPHA, "next_session", ModelState.ACTIVE, "revive", NOW
            )
        stored = store.model("ewma", ALPHA, "next_session")
        assert stored is not None
        self.assertIs(stored.state, ModelState.RETIRED)
        self.assertEqual(store.reliability(model_id="ewma"), [])

    def test_selections_and_model_states_are_stored_per_horizon(self) -> None:
        store = ForecastStore(":memory:")
        self.addCleanup(store.close)
        store.save_selection(ALPHA, "next_session", "ewma", {"brier": 0.6}, NOW)
        self.assertEqual(store.selection(ALPHA, "next_session"), "ewma")
        self.assertIsNone(store.selection(ALPHA, "one_week"))
        store.upsert_model(
            ModelRecord(
                model_id="ewma",
                version="v1",
                state=ModelState.ACTIVE,
                kind="statistical",
                description="test",
                trained_at=NOW,
                updated_at=NOW,
                instrument_id=ALPHA,
                horizon="next_session",
                metrics={"brier": 0.6},
            )
        )
        updated = store.set_model_state(
            "ewma", ALPHA, "next_session", ModelState.DEGRADED, "test", NOW
        )
        assert updated is not None
        self.assertIs(updated.state, ModelState.DEGRADED)
        self.assertEqual(len(store.reliability(model_id="ewma")), 1)


class EngineTests(unittest.TestCase):
    def test_generate_stores_an_immutable_forecast_and_publishes(self) -> None:
        recorder = _Recorder()
        engine, store = _engine(publisher=recorder)
        self.addCleanup(store.close)
        body = asyncio.run(engine.generate(ALPHA, "next_session"))

        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["horizon_sessions"], 1)
        # Serialized probabilities are rounded to 6 places; the stored record
        # enforces the 1e-6 sum tolerance at full precision.
        self.assertLess(
            abs(body["p_up"] + body["p_down"] + body["p_sideways"] - 1.0),
            1e-5,
        )
        self.assertLessEqual(body["lower_return"], body["predicted_return"])
        self.assertLessEqual(body["predicted_return"], body["upper_return"])
        self.assertEqual(body["data_cutoff"], NOW.isoformat())
        self.assertTrue(body["digest"])
        self.assertFalse(body["is_price_prediction"])
        self.assertEqual(len(store.forecasts()), 1)
        self.assertEqual(
            recorder.names(),
            ["novamarket.forecast.created", "novamarket.forecast.completed"],
        )

    def test_the_target_is_a_future_weekday(self) -> None:
        engine, store = _engine()
        self.addCleanup(store.close)
        body = asyncio.run(engine.generate(ALPHA, "one_week"))
        target = datetime.fromisoformat(body["target_at"])
        self.assertGreater(target, NOW)
        self.assertLess(target.weekday(), 5)
        self.assertEqual(body["horizon_sessions"], 5)

    def test_insufficient_history_is_refused(self) -> None:
        engine, store = _engine(bars=_bars(40))
        self.addCleanup(store.close)
        with self.assertRaises(ForecastDataError):
            asyncio.run(engine.generate(ALPHA, "next_session"))

    def test_stale_bars_are_refused(self) -> None:
        stale = _bars(160, end=NOW - timedelta(days=10))
        engine, store = _engine(bars=stale)
        self.addCleanup(store.close)
        with self.assertRaises(ForecastDataError):
            asyncio.run(engine.generate(ALPHA, "next_session"))

    def test_mature_history_forecasts_from_the_newest_window(self) -> None:
        class _LimitBeforeOrderMarket(_FakeMarketData):
            """Mimic the SQLite facade: LIMIT is applied before ordering."""

            def bars(
                self,
                instrument_id: str,
                *,
                limit: int = 200,
                ascending: bool = False,
                **_: Any,
            ) -> Any:
                rows = list(self._bars.get(instrument_id, ()))
                rows = rows[:limit] if ascending else rows[-limit:]
                if not ascending:
                    rows.reverse()
                return SimpleNamespace(items=rows)

        market = _LimitBeforeOrderMarket({ALPHA: _bars(400)})
        store = ForecastStore(":memory:")
        self.addCleanup(store.close)
        engine = ForecastEngine(store, market_data=market, clock=lambda: NOW)
        body = asyncio.run(engine.generate(ALPHA, "next_session"))
        self.assertTrue(body["ok"])
        self.assertTrue(body["digest"])

    def test_an_unknown_instrument_is_refused(self) -> None:
        engine, store = _engine()
        self.addCleanup(store.close)
        with self.assertRaises(ForecastNotFoundError):
            asyncio.run(engine.generate("XNSE:MISSING", "next_session"))

    def test_an_unknown_horizon_is_refused(self) -> None:
        engine, store = _engine()
        self.addCleanup(store.close)
        with self.assertRaises(ForecastValidationError):
            asyncio.run(engine.generate(ALPHA, "a_decade"))

    def test_a_later_bar_cannot_change_a_stored_forecast(self) -> None:
        bars = _bars(160)
        first_engine, first_store = _engine(bars=bars)
        self.addCleanup(first_store.close)
        first = asyncio.run(first_engine.generate(ALPHA, "next_session"))

        extended = list(bars)
        extended.append(_bar(NOW + timedelta(days=3), 1_000.0))
        second_engine, second_store = _engine(bars=extended)
        self.addCleanup(second_store.close)
        second = asyncio.run(second_engine.generate(ALPHA, "next_session"))

        self.assertEqual(first["forecast_id"], second["forecast_id"])
        self.assertEqual(first["digest"], second["digest"])
        self.assertEqual(first["predicted_return"], second["predicted_return"])
        self.assertEqual(first["p_up"], second["p_up"])

    def test_check_health_degrades_a_model_with_bad_recent_outcomes(self) -> None:
        engine, store = _engine()
        self.addCleanup(store.close)
        for index in range(12):
            created = NOW - timedelta(days=index + 2)
            store.save_forecast(
                ForecastRecord(
                    forecast_id=f"fc_h{index}",
                    instrument_id=ALPHA,
                    created_at=created,
                    data_cutoff=created,
                    horizon=Horizon.NEXT_SESSION,
                    horizon_sessions=1,
                    target_at=created + timedelta(days=1),
                    model_id="ewma",
                    model_version="v1",
                    feature_set_version="f1",
                    predicted_return=0.0,
                    predicted_price=100.0,
                    p_up=0.9,
                    p_down=0.05,
                    p_sideways=0.05,
                    lower_return=-0.01,
                    upper_return=0.01,
                    lower_price=99.0,
                    upper_price=101.0,
                    uncertainty=UncertaintyLevel.LOW,
                    uncertainty_width=0.02,
                    data_quality="good",
                    regime=Regime.SIDEWAYS,
                    event_ids=(),
                    candidate_score=None,
                    methodology_version="m1",
                    digest=f"dh{index}",
                )
            )
            store.record_outcome(
                ForecastOutcome(
                    forecast_id=f"fc_h{index}",
                    instrument_id=ALPHA,
                    status=ForecastStatus.EVALUATED,
                    evaluated_at=created + timedelta(days=1),
                    actual_return=-0.03,
                    actual_price=97.0,
                    error=0.03,
                    direction_correct=False,
                    brier=1.8,
                    log_loss=3.0,
                    inside_interval=False,
                )
            )
        store.upsert_model(
            ModelRecord(
                model_id="ewma",
                version="v1",
                state=ModelState.ACTIVE,
                kind="statistical",
                description="test",
                trained_at=NOW,
                updated_at=NOW,
                instrument_id=ALPHA,
                horizon="next_session",
                metrics={"brier": 0.6, "baseline_brier": 0.6},
            )
        )
        body = asyncio.run(engine.check_health())
        self.assertEqual(body["checked"], 1)
        self.assertEqual(len(body["changes"]), 1)
        self.assertIs(
            store.model("ewma", ALPHA, "next_session").state,  # type: ignore[union-attr]
            ModelState.DEGRADED,
        )

    def test_unknown_horizon_on_a_read_is_a_validation_error(self) -> None:
        engine, store = _engine()
        self.addCleanup(store.close)
        self.assertEqual(engine.forecasts()["total"], 0)
        for call in (
            lambda: engine.forecasts(horizon="decade"),
            lambda: engine.models(horizon="decade"),
            lambda: engine.performance(horizon="decade"),
            lambda: engine.calibration("random_walk", "decade"),
        ):
            with self.subTest():
                with self.assertRaises(ForecastValidationError):
                    call()

    def test_calibration_requires_a_named_model(self) -> None:
        engine, store = _engine()
        self.addCleanup(store.close)
        with self.assertRaises(ForecastValidationError):
            engine.calibration("", "next_session")

    def test_the_contract_states_the_honest_limits(self) -> None:
        engine, store = _engine()
        self.addCleanup(store.close)
        contract = engine.contract()
        self.assertTrue(contract["implemented"])
        self.assertEqual(contract["probability_definition"]["tolerance"], 1e-6)
        joined = " ".join(contract["guarantees"])
        self.assertIn("immutable", joined)
        self.assertIn("cutoff", joined)
        self.assertIn("not certainties", joined)
        self.assertNotIn("order", joined)

    def test_a_failed_publication_never_loses_the_forecast(self) -> None:
        async def broken(name: str, payload: dict[str, Any]) -> None:
            raise RuntimeError("bus down")

        engine, store = _engine(publisher=broken)
        self.addCleanup(store.close)
        body = asyncio.run(engine.generate(ALPHA, "next_session"))
        self.assertTrue(body["ok"])
        self.assertEqual(len(store.forecasts()), 1)
        self.assertIn("bus down", engine.status()["last_error"])


class EngineEvaluationTests(unittest.TestCase):
    def _evaluated(self, *, horizon: str = "next_session", extra_days: int = 7) -> tuple[dict[str, Any], ForecastStore]:
        engine, store = _engine()
        self.addCleanup(store.close)
        created = asyncio.run(engine.generate(ALPHA, horizon))
        target = datetime.fromisoformat(created["target_at"])
        engine._market_data.append(  # type: ignore[union-attr]
            ALPHA, _bar(target, 103.0)
        )
        at = target + timedelta(days=extra_days)
        result = asyncio.run(engine.evaluate_due(at=at))
        return result, store

    def test_a_due_forecast_is_evaluated_and_snapshotted(self) -> None:
        result, store = self._evaluated()
        self.assertEqual(result["evaluated"], 1)
        self.assertEqual(result["waiting"], 0)
        self.assertEqual(len(store.performance()), 1)
        outcome = store.outcome(result["results"][0]["forecast_id"])
        assert outcome is not None
        self.assertEqual(outcome["status"], "evaluated")
        self.assertIsNotNone(outcome["brier"])
        self.assertIsNotNone(outcome["actual_return"])

    def test_evaluation_waits_for_the_target(self) -> None:
        engine, store = _engine()
        self.addCleanup(store.close)
        created = asyncio.run(engine.generate(ALPHA, "next_session"))
        target = datetime.fromisoformat(created["target_at"])
        # Before the target the forecast is not even due.
        early = asyncio.run(engine.evaluate_due(at=NOW))
        self.assertEqual(early["evaluated"], 0)
        self.assertEqual(early["unresolved"], 0)
        # Due, but no bar has closed at the target yet: it waits, inside grace.
        due = asyncio.run(engine.evaluate_due(at=target + timedelta(hours=1)))
        self.assertEqual(due["waiting"], 1)
        self.assertEqual(due["evaluated"], 0)
        self.assertEqual(store.outcome(created["forecast_id"]), None)

    def test_a_missing_outcome_becomes_unresolved_after_the_grace_period(self) -> None:
        engine, store = _engine()
        self.addCleanup(store.close)
        created = asyncio.run(engine.generate(ALPHA, "next_session"))
        target = datetime.fromisoformat(created["target_at"])
        result = asyncio.run(
            engine.evaluate_due(at=target + timedelta(days=11))
        )
        self.assertEqual(result["unresolved"], 1)
        outcome = store.outcome(created["forecast_id"])
        assert outcome is not None
        self.assertEqual(outcome["status"], "unresolved")
        self.assertIn("bar", outcome["reason"])

    def test_an_evaluated_forecast_is_never_rewritten(self) -> None:
        result, store = self._evaluated()
        forecast_id = result["results"][0]["forecast_id"]
        before = store.forecast(forecast_id)
        assert before is not None
        self.assertEqual(store.outcome(forecast_id)["status"], "evaluated")
        self.assertEqual(store.forecast(forecast_id)["p_up"], before["p_up"])


    def test_a_performance_snapshot_is_published(self) -> None:
        recorder = _Recorder()
        engine, store = _engine(publisher=recorder)
        self.addCleanup(store.close)
        created = asyncio.run(engine.generate(ALPHA, "next_session"))
        target = datetime.fromisoformat(created["target_at"])
        engine._market_data.append(  # type: ignore[union-attr]
            ALPHA, _bar(target, 101.5)
        )
        asyncio.run(engine.evaluate_due(at=target + timedelta(days=2)))
        self.assertIn(
            "novamarket.forecast.performance.updated", recorder.names()
        )


class EngineTrainingTests(unittest.TestCase):
    def test_training_persists_scores_selection_and_calibration(self) -> None:
        engine, store = _engine(bars=_bars(260))
        self.addCleanup(store.close)
        body = asyncio.run(engine.train(ALPHA, "next_session"))
        self.assertTrue(body["ok"])
        self.assertIn(body["selected_model"], MODEL_IDS)
        self.assertEqual(body["baseline_model"], "random_walk")
        self.assertEqual(set(body["scores"]), set(MODEL_IDS))
        self.assertGreaterEqual(body["origins"], 5)
        self.assertEqual(
            store.selection(ALPHA, "next_session"), body["selected_model"]
        )
        models = store.models(instrument_id=ALPHA, horizon="next_session")
        self.assertEqual({record.model_id for record in models}, set(MODEL_IDS))
        selected = next(
            record for record in models if record.model_id == body["selected_model"]
        )
        self.assertIs(selected.state, ModelState.ACTIVE)
        self.assertIsNotNone(selected.metrics.get("brier"))
        self.assertEqual(len(store.training_runs()), 1)
        self.assertIsNotNone(store.calibration(body["selected_model"], "next_session"))

    def test_training_the_baseline_calibrates_it_once(self) -> None:
        recorder = _Recorder()
        engine, store = _engine(publisher=recorder)
        self.addCleanup(store.close)
        samples = tuple(
            CalibrationSample(0.35 + index * 0.01, index % 2 == 0)
            for index in range(40)
        )
        baseline = ModelScore(
            model_id="random_walk",
            samples=20,
            mae=0.01,
            rmse=0.02,
            median_absolute_error=0.01,
            directional_accuracy=0.5,
            brier=0.60,
            log_loss=1.0,
            interval_coverage=0.8,
            interval_width=0.04,
            calibration_error=0.05,
        )
        challenger = ModelScore(
            model_id="ewma",
            samples=20,
            mae=0.01,
            rmse=0.02,
            median_absolute_error=0.01,
            directional_accuracy=0.5,
            brier=0.70,
            log_loss=1.1,
            interval_coverage=0.8,
            interval_width=0.04,
            calibration_error=0.06,
        )
        result = WalkForwardResult(
            1,
            20,
            {"random_walk": baseline, "ewma": challenger},
            {"random_walk": samples},
        )
        with mock.patch.object(engine_module, "evaluate_models", return_value=result):
            body = asyncio.run(engine.train(ALPHA, "next_session"))
        self.assertEqual(body["selected_model"], "random_walk")
        self.assertEqual(store.metrics()["calibration_reports"], 1)
        self.assertEqual(
            recorder.names().count("novamarket.forecast.calibration.completed"), 1
        )

    def test_training_twice_at_the_same_cutoff_is_idempotent(self) -> None:
        engine, store = _engine(bars=_bars(260))
        self.addCleanup(store.close)
        first = asyncio.run(engine.train(ALPHA, "next_session", at=NOW))
        second = asyncio.run(engine.train(ALPHA, "next_session", at=NOW))
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(store.metrics()["training_runs"], 1)

    def test_promotion_of_a_suspended_model_is_a_state_error(self) -> None:
        engine, store = _engine()
        self.addCleanup(store.close)
        store.upsert_model(
            ModelRecord(
                model_id="ewma",
                version="v1",
                state=ModelState.SUSPENDED,
                kind="statistical",
                description="test",
                trained_at=NOW,
                updated_at=NOW,
                instrument_id=ALPHA,
                horizon="next_session",
                metrics={"brier": 0.6},
            )
        )
        with self.assertRaises(ForecastStateError):
            asyncio.run(engine.promote("ewma", ALPHA, "next_session"))

    def test_a_forecast_uses_the_selected_model(self) -> None:
        engine, store = _engine(bars=_bars(260))
        self.addCleanup(store.close)
        store.save_selection(ALPHA, "next_session", "historical_mean", {"brier": 0.5}, NOW)
        body = asyncio.run(engine.generate(ALPHA, "next_session"))
        self.assertEqual(body["model_id"], "historical_mean")

    def test_training_refuses_insufficient_history(self) -> None:
        engine, store = _engine(bars=_bars(90))
        self.addCleanup(store.close)
        with self.assertRaises(ForecastDataError):
            asyncio.run(engine.train(ALPHA, "next_session"))

    def test_promotion_requires_measured_evidence(self) -> None:
        engine, store = _engine()
        self.addCleanup(store.close)
        store.upsert_model(
            ModelRecord(
                model_id="ewma",
                version="v1",
                state=ModelState.EXPERIMENTAL,
                kind="statistical",
                description="test",
                trained_at=None,
                updated_at=NOW,
                instrument_id=ALPHA,
                horizon="next_session",
                metrics={},
            )
        )
        with self.assertRaises(ForecastValidationError):
            asyncio.run(engine.promote("ewma", ALPHA, "next_session"))
        with self.assertRaises(ForecastNotFoundError):
            asyncio.run(engine.promote("missing", ALPHA, "next_session"))


if __name__ == "__main__":
    unittest.main()
