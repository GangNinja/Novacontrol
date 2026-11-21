"""Focused PI-08 tests: eligibility, weights, aggregation, calibration, API."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from novacontrol.novamarket.ensemble.calibration import (
    conformal_quantile,
    coverage_by_regime,
    fit_calibration,
    metrics_for,
)
from novacontrol.novamarket.ensemble.models import (
    ConformalCalibration,
    EnsembleConfig,
    EnsembleObservation,
    EnsembleValidationError,
    ModelEvidence,
)
from novacontrol.novamarket.ensemble.service import (
    EnsembleForecastingService,
    _point_in_time_data,
)
from novacontrol.novamarket.ensemble.weighting import (
    assess_evidence,
    equal_weights,
    validate_weights,
)
from novacontrol.novamarket.forecasting import ForecastStore
from novacontrol.novamarket.marketdata.models import (
    DataInterval,
    DataQuality,
    Freshness,
)
from novacontrol.novamarket.prediction.models import (
    CalibrationStatus,
    ConfirmationStage,
    Direction,
    ForecastHorizon,
    ForecastOutput,
    ForecastValidity,
    ModelFamily,
    ModelMetadata,
    ModelState,
    PredictionUncertainty,
    ProbabilityStatus,
    digest,
)
from novacontrol.novamarket.prediction.store import PredictionStore
from novacontrol.novamarket.service import NovaMarketService

NOW = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)
INSTRUMENT = "XNSE:NIFTY"


def _evidence(
    model_id: str,
    *,
    family: ModelFamily = ModelFamily.NAIVE,
    mae: float | None = 1.0,
    samples: int = 12,
    direction: float | None = 0.6,
    state: ModelState = ModelState.ELIGIBLE,
    walk_forward: float | None = None,
    version: str = "v1",
) -> ModelEvidence:
    return ModelEvidence(
        model_id=model_id,
        version=version,
        family=family,
        state=state,
        horizon=ForecastHorizon.DAY_1,
        instrument_id=INSTRUMENT,
        validation_mae=mae,
        validation_direction_accuracy=direction,
        validation_samples=samples,
        walk_forward_mae=walk_forward,
        trained_at=NOW - timedelta(days=10),
    )


def _forecast(
    model_id: str,
    family: ModelFamily,
    predicted_return: float,
    *,
    version: str = "v1",
    calibration: CalibrationStatus = CalibrationStatus.UNCALIBRATED,
    p_up: float | None = None,
    p_down: float | None = None,
    p_neutral: float | None = None,
    width: float = 0.05,
    reference_price: Decimal | None = None,
    feature_set_version: str = "fs-v1",
    dataset_version: str = "ds-v1",
) -> ForecastOutput:
    reference = reference_price or Decimal("100")
    predicted_price = reference * (Decimal("1") + Decimal(str(predicted_return)))
    lower = predicted_return - width
    upper = predicted_return + width
    if lower <= -1.0:
        lower = -0.999
    return ForecastOutput(
        forecast_id=f"fake_{model_id}_{predicted_return}_{calibration.value}",
        lifecycle_id=f"life_{model_id}",
        lifecycle_stage=ConfirmationStage.INITIAL,
        instrument_id=INSTRUMENT,
        exchange="XNSE",
        currency="INR",
        reference_price=reference,
        reference_at=NOW,
        predicted_price=predicted_price,
        predicted_return=predicted_return,
        direction=Direction.UP if predicted_return > 0 else Direction.DOWN,
        lower_return=lower,
        upper_return=upper,
        lower_price=reference * (Decimal("1") + Decimal(str(lower))),
        upper_price=reference * (Decimal("1") + Decimal(str(upper))),
        uncertainty=PredictionUncertainty.MEDIUM,
        uncertainty_width=width * 2,
        p_up=p_up,
        p_down=p_down,
        p_neutral=p_neutral,
        probability_status=(
            ProbabilityStatus.CALIBRATED
            if p_up is not None
            else ProbabilityStatus.UNAVAILABLE
        ),
        calibration_status=calibration,
        confidence=None,
        ensemble_id="",
        model_id=model_id,
        model_version=version,
        member_versions=(),
        forecast_horizon=ForecastHorizon.DAY_1,
        target_at=NOW + timedelta(days=1),
        generated_at=NOW,
        data_observed_at=NOW,
        data_received_at=NOW,
        data_resolution=DataInterval.DAY_1,
        data_freshness=Freshness.FRESH,
        data_quality=DataQuality.VALID,
        data_snapshot_id="snap",
        validity=ForecastValidity.VALID,
        validity_conditions=(),
        disagreement=0.0,
        digest=digest({"model_id": model_id, "predicted_return": predicted_return}),
        feature_set_version=feature_set_version,
        dataset_version=dataset_version,
    )


class _FakeFamily:
    """One constituent service that persists its own forecast contract."""

    def __init__(
        self,
        store: PredictionStore,
        *,
        model_id: str,
        family: ModelFamily,
        predicted_return: float,
        calibration: CalibrationStatus = CalibrationStatus.UNCALIBRATED,
        p_up: float | None = None,
        p_down: float | None = None,
        p_neutral: float | None = None,
        fail: bool = False,
    ) -> None:
        self.store = store
        self.model_id = model_id
        self.family = family
        self.predicted_return = predicted_return
        self.calibration = calibration
        self.probabilities = (p_up, p_down, p_neutral)
        self.fail = fail
        self.calls = 0
        self.reference_price: Decimal | None = None
        self.feature_set_version = "fs-v1"
        self.dataset_version = "ds-v1"

    async def generate(self, instrument_id: str, horizon: str, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("forced constituent inference failure")
        output = _forecast(
            self.model_id,
            self.family,
            self.predicted_return,
            calibration=self.calibration,
            p_up=self.probabilities[0],
            p_down=self.probabilities[1],
            p_neutral=self.probabilities[2],
            reference_price=self.reference_price,
            feature_set_version=self.feature_set_version,
            dataset_version=self.dataset_version,
        )
        self.store.save_forecast(output)
        return output.to_dict()


def _metadata(
    model_id: str,
    family: ModelFamily,
    *,
    mae: float = 1.0,
    samples: int = 12,
    instruments: tuple[str, ...] = (INSTRUMENT,),
    state: ModelState = ModelState.ELIGIBLE,
    calibration: CalibrationStatus = CalibrationStatus.UNCALIBRATED,
    extra: dict[str, Any] | None = None,
) -> ModelMetadata:
    metrics: dict[str, Any] = {
        "instruments": list(instruments),
        "instrument_id": instruments[0] if instruments else None,
        "validation": {
            "price_mae": mae,
            "samples": samples,
            "directional_accuracy": 0.6,
        },
        "dataset_version": "ds-v1",
        "feature_set_version": "fs-v1",
    }
    if family in {ModelFamily.XGBOOST, ModelFamily.LIGHTGBM}:
        metrics["artifact"] = {"version": "v1"}
    if family is ModelFamily.LSTM:
        metrics["artifact"] = {"version": "v1"}
        metrics["scaler"] = {"mean": []}
    metrics.update(extra or {})
    return ModelMetadata(
        model_id=model_id,
        version="v1",
        family=family,
        state=state,
        supported_horizons=(ForecastHorizon.DAY_1,),
        calibration=calibration,
        metrics=metrics,
        trained_at=NOW - timedelta(days=10),
        updated_at=NOW - timedelta(days=10),
        feature_set_version="fs-v1",
        dataset_version="ds-v1",
    )


class _Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.prediction = PredictionStore(tmp_path / "ensemble.sqlite")
        self.forecasts = ForecastStore(tmp_path / "forecast.sqlite")
        self.statistical = _FakeFamily(
            self.prediction,
            model_id="naive",
            family=ModelFamily.NAIVE,
            predicted_return=0.02,
        )
        self.gradient = _FakeFamily(
            self.prediction,
            model_id="xgboost",
            family=ModelFamily.XGBOOST,
            predicted_return=0.04,
        )
        self.lstm = _FakeFamily(
            self.prediction,
            model_id="lstm",
            family=ModelFamily.LSTM,
            predicted_return=0.03,
        )
        self.service = EnsembleForecastingService(
            statistical_service=self.statistical,
            gradient_service=self.gradient,
            lstm_service=self.lstm,
            prediction_store=self.prediction,
            forecast_store=self.forecasts,
            clock=lambda: NOW,
            default_config=EnsembleConfig(max_weight=0.9, min_weight=0.01, min_models=2),
        )

    def register(
        self,
        *,
        statistical_mae: float = 1.0,
        gradient_mae: float = 3.0,
        lstm_mae: float = 2.0,
        statistical_state: ModelState = ModelState.ELIGIBLE,
    ) -> None:
        self.prediction.register_model(
            _metadata(
                "naive",
                ModelFamily.NAIVE,
                mae=statistical_mae,
                state=statistical_state,
            )
        )
        self.prediction.register_model(
            _metadata("xgboost", ModelFamily.XGBOOST, mae=gradient_mae)
        )
        self.prediction.register_model(
            _metadata("lstm", ModelFamily.LSTM, mae=lstm_mae)
        )


@pytest.fixture()
def harness(tmp_path: Path) -> _Harness:
    return _Harness(tmp_path)


def _generate(harness: _Harness, **kwargs: Any) -> dict[str, Any]:
    return __import__("asyncio").run(
        harness.service.generate(
            INSTRUMENT,
            ForecastHorizon.DAY_1,
            as_of=NOW,
            bars=[],
            **kwargs,
        )
    )


def test_unified_constituent_interface_is_used(harness: _Harness) -> None:
    harness.register()
    body = _generate(harness)
    assert harness.statistical.calls == harness.gradient.calls == harness.lstm.calls == 1
    assert body["status"] == "assembled"
    assert body["model_id"] == "ensemble"
    assert len(body["ensemble"]["constituents"]) == 3


def test_regime_context_is_recorded_with_the_forecast(harness: _Harness) -> None:
    harness.register()
    body = _generate(harness, regime="strong_up")
    assert body["ensemble"]["regime"] == "strong_up"
    stored = harness.prediction.get_forecast(body["forecast_id"])
    assert stored is not None
    assert stored.metadata["regime"] == "strong_up"


def test_ineligible_models_are_excluded_and_reasons_are_recorded(harness: _Harness) -> None:
    harness.register(statistical_state=ModelState.UNAVAILABLE)
    body = _generate(harness)
    decisions = {item["model_id"]: item for item in body["ensemble"]["eligibility"]}
    assert decisions["naive"]["eligible"] is False
    assert any("state" in reason for reason in decisions["naive"]["reasons"])
    assert decisions["xgboost"]["eligible"] is True


def test_one_constituent_failure_is_isolated(harness: _Harness) -> None:
    harness.register()
    harness.gradient.fail = True
    body = _generate(harness)
    # Two survivors still form an ensemble; the failed model never contributes.
    assert body["status"] == "assembled"
    assert {item["model_id"] for item in body["ensemble"]["constituents"]} == {
        "naive",
        "lstm",
    }
    assert any("xgboost" in item for item in body["failures"])


@pytest.mark.parametrize(
    ("attribute", "value", "reason_fragment"),
    (
        ("reference_price", Decimal("101"), "reference price mismatch"),
        ("feature_set_version", "fs-v2", "feature-set version mismatch"),
        ("dataset_version", "ds-v2", "dataset version mismatch"),
    ),
)
def test_incompatible_constituent_is_excluded_not_blended(
    harness: _Harness, attribute: str, value: Any, reason_fragment: str
) -> None:
    harness.register()
    setattr(harness.gradient, attribute, value)
    body = _generate(harness)
    assert body["status"] == "assembled"
    assert any(reason_fragment in item for item in body["failures"])
    assert all(
        item["model_id"] != "xgboost" for item in body["ensemble"]["constituents"]
    )


def test_equal_weight_baseline_is_normalized() -> None:
    rows = [_evidence("a"), _evidence("b"), _evidence("c")]
    weights = equal_weights(rows)
    assert set(weights) == {"a@v1", "b@v1", "c@v1"}
    assert math.isclose(sum(weights.values()), 1.0)


def test_performance_weights_are_normalized_and_capped() -> None:
    rows = [_evidence("a", mae=1.0), _evidence("b", mae=9.0)]
    result = assess_evidence(
        rows,
        EnsembleConfig(max_weight=0.5, min_weight=0.01, min_models=2),
        now=NOW,
    )
    assert set(result.weights) == {"a@v1", "b@v1"}
    assert math.isclose(sum(result.weights.values()), 1.0)
    assert max(result.weights.values()) <= 0.5 + 1e-12
    validate_weights(result.weights)


def test_weights_smooth_instead_of_switching_abruptly() -> None:
    rows = [_evidence("a", mae=1.0), _evidence("b", mae=9.0)]
    config = EnsembleConfig(max_weight=0.9, min_weight=0.01, min_models=2, smoothing=0.5)
    raw = assess_evidence(rows, config, now=NOW)
    smoothed = assess_evidence(
        rows,
        config,
        now=NOW,
        previous_weights={"a@v1": 0.5, "b@v1": 0.5},
    )
    assert smoothed.weights["b@v1"] > raw.weights["b@v1"]
    assert smoothed.weights["a@v1"] < raw.weights["a@v1"]


def test_horizon_and_instrument_mismatches_abstain(harness: _Harness) -> None:
    harness.register()
    mismatch_horizon = __import__("asyncio").run(
        harness.service.generate(
            INSTRUMENT, ForecastHorizon.MINUTES_5, as_of=NOW, bars=[]
        )
    )
    assert mismatch_horizon["status"] == "abstained"
    assert "no registered model" in " ".join(mismatch_horizon["reasons"])

    other = __import__("asyncio").run(
        harness.service.generate("XNSE:OTHER", ForecastHorizon.DAY_1, as_of=NOW, bars=[])
    )
    assert other["status"] == "abstained"


def test_price_and_return_aggregation(harness: _Harness) -> None:
    harness.register(statistical_mae=1.0, gradient_mae=3.0, lstm_mae=3.0)
    body = _generate(harness)
    weights = {
        item["model_id"]: item["weight"] for item in body["ensemble"]["constituents"]
    }
    expected = (
        weights["naive"] * 0.02 + weights["xgboost"] * 0.04 + weights["lstm"] * 0.03
    )
    assert math.isclose(body["predicted_return"], expected, rel_tol=1e-6)
    assert math.isclose(
        float(body["predicted_price"]),
        100.0 * (1.0 + expected),
        rel_tol=1e-8,
    )
    assert float(body["lower_price"]) < float(body["predicted_price"]) < float(body["upper_price"])


def test_calibrated_direction_probabilities_are_aggregated(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    try:
        harness.statistical.calibration = CalibrationStatus.CALIBRATED
        harness.statistical.probabilities = (0.7, 0.2, 0.1)
        harness.gradient.calibration = CalibrationStatus.CALIBRATED
        harness.gradient.probabilities = (0.6, 0.3, 0.1)
        harness.prediction.register_model(
            _metadata("naive", ModelFamily.NAIVE, calibration=CalibrationStatus.CALIBRATED)
        )
        harness.prediction.register_model(
            _metadata("xgboost", ModelFamily.XGBOOST, calibration=CalibrationStatus.CALIBRATED)
        )
        body = _generate(harness)
        assert body["probability_status"] == "calibrated"
        assert body["p_up"] is not None
        assert math.isclose(body["p_up"] + body["p_down"] + body["p_neutral"], 1.0)
    finally:
        harness.prediction.close()
        harness.forecasts.close()


def test_disagreement_abstains_instead_of_averaging(harness: _Harness) -> None:
    harness.register()
    harness.statistical.predicted_return = 0.10
    harness.gradient.predicted_return = -0.10
    harness.lstm.predicted_return = 0.0
    body = _generate(harness)
    assert body["status"] == "abstained"
    assert body["forecast"] is None
    assert any("disagree" in reason for reason in body["reasons"])


def test_single_eligible_model_is_a_transparent_fallback(harness: _Harness) -> None:
    harness.prediction.register_model(_metadata("naive", ModelFamily.NAIVE))
    harness.prediction.register_model(
        _metadata("xgboost", ModelFamily.XGBOOST, state=ModelState.UNAVAILABLE)
    )
    body = _generate(harness)
    assert body["status"] == "fallback"
    assert body["model_id"] == "naive"
    assert body["ensemble"]["fallback"] is True
    assert body["ensemble"]["fallback_reason"]
    assert body["forecast_id"].startswith("ensfb_")


def test_no_eligible_model_returns_an_honest_abstention(harness: _Harness) -> None:
    harness.prediction.register_model(
        _metadata("naive", ModelFamily.NAIVE, state=ModelState.UNAVAILABLE)
    )
    body = _generate(harness)
    assert body["status"] == "abstained"
    assert body["forecast"] is None
    assert body["eligibility"][0]["eligible"] is False


def test_stale_bar_is_accepted_only_when_it_is_current_for_the_cutoff() -> None:
    current = replace(
        _forecast("stale", ModelFamily.NAIVE, 0.01),
        validity=ForecastValidity.STALE,
        data_freshness=Freshness.STALE,
        data_quality=DataQuality.STALE,
    )
    quality, freshness, ok = _point_in_time_data(
        current, cutoff=NOW, horizon=ForecastHorizon.DAY_1
    )
    assert ok is True
    assert quality is DataQuality.VALID
    assert freshness is Freshness.FRESH
    quality, freshness, ok = _point_in_time_data(
        current, cutoff=NOW + timedelta(days=3), horizon=ForecastHorizon.DAY_1
    )
    assert ok is False
    assert quality is DataQuality.STALE
    assert freshness is Freshness.STALE
    _, _, future = _point_in_time_data(
        current, cutoff=NOW - timedelta(minutes=1), horizon=ForecastHorizon.DAY_1
    )
    assert future is False
    _, _, missing = _point_in_time_data(
        replace(current, data_freshness=Freshness.MISSING),
        cutoff=NOW,
        horizon=ForecastHorizon.DAY_1,
    )
    assert missing is False


def test_walk_forward_evidence_can_substitute_for_validation() -> None:
    result = assess_evidence(
        [_evidence("a", mae=None, walk_forward=2.0), _evidence("b", mae=None, walk_forward=4.0)],
        EnsembleConfig(max_weight=0.9, min_weight=0.01, min_models=2),
        now=NOW,
    )
    assert set(result.weights) == {"a@v1", "b@v1"}


def _observation(
    predicted: float,
    actual: float,
    *,
    validation: bool,
    regime: str = "sideways",
    model_id: str = "ensemble",
) -> EnsembleObservation:
    return EnsembleObservation(
        instrument_id=INSTRUMENT,
        horizon=ForecastHorizon.DAY_1,
        target_at=NOW + timedelta(days=1),
        predicted_return=predicted,
        actual_return=actual,
        predicted_price=100.0 * (1.0 + predicted),
        actual_price=100.0 * (1.0 + actual),
        lower_return=predicted - 0.05,
        upper_return=predicted + 0.05,
        predicted_direction="up",
        actual_direction="up",
        regime=regime,
        validation=validation,
        model_id=model_id,
    )


def test_validation_only_calibration_and_interval_coverage() -> None:
    validation = [
        _observation(0.01, 0.02, validation=True),
        _observation(0.01, 0.00, validation=True),
        _observation(0.01, 0.03, validation=True),
    ]
    calibration = fit_calibration(
        validation,
        instrument_id=INSTRUMENT,
        horizon=ForecastHorizon.DAY_1,
        config=EnsembleConfig(min_validation_samples=3),
        dataset_version="ds-v1",
        created_at=NOW,
    )
    assert calibration.samples == 3
    assert calibration.fitted_on == "validation_only"
    assert conformal_quantile([0.1, -0.2, 0.05], 0.8) == 0.2
    lower, upper = calibration.apply(0.01)
    assert lower < 0.01 < upper
    test_rows = [_observation(0.01, 0.02, validation=False), _observation(0.01, 0.0, validation=False)]
    metrics = metrics_for(test_rows, calibration=calibration)
    assert metrics.samples == 2
    assert metrics.interval_coverage is not None
    assert 0.0 <= metrics.interval_coverage <= 1.0


def test_insufficient_validation_data_refuses_calibration() -> None:
    with pytest.raises(EnsembleValidationError):
        fit_calibration(
            [_observation(0.01, 0.02, validation=True)],
            instrument_id=INSTRUMENT,
            horizon=ForecastHorizon.DAY_1,
            config=EnsembleConfig(min_validation_samples=5),
            created_at=NOW,
        )


def test_calibration_refuses_test_rows() -> None:
    with pytest.raises(EnsembleValidationError):
        fit_calibration(
            [_observation(0.01, 0.02, validation=False)],
            instrument_id=INSTRUMENT,
            horizon=ForecastHorizon.DAY_1,
            config=EnsembleConfig(min_validation_samples=1),
            created_at=NOW,
        )


def test_calibration_with_a_different_nominal_level_is_not_applied() -> None:
    validation = [
        _observation(0.01, 0.02, validation=True),
        _observation(0.01, 0.00, validation=True),
        _observation(0.01, 0.03, validation=True),
    ]
    calibration = fit_calibration(
        validation,
        instrument_id=INSTRUMENT,
        horizon=ForecastHorizon.DAY_1,
        config=EnsembleConfig(min_validation_samples=3, interval_level=0.8),
        created_at=NOW,
    )
    service = EnsembleForecastingService(clock=lambda: NOW)
    output = _forecast("naive", ModelFamily.NAIVE, 0.01)
    applied, detail = service._apply_calibration(
        output,
        calibration,
        config=EnsembleConfig(interval_level=0.9),
    )
    assert detail["status"] == "uncalibrated"
    assert detail["registered_level"] == 0.8
    assert applied.lower_return == output.lower_return


def test_regime_evidence_reweights_only_with_enough_regime_samples() -> None:
    good = replace(
        _evidence("good"),
        regime="strong_up",
        regime_mae=1.0,
        regime_samples=8,
    )
    poor = replace(
        _evidence("poor"),
        regime="strong_up",
        regime_mae=20.0,
        regime_samples=8,
    )
    config = EnsembleConfig(
        min_models=2,
        min_weight=0.01,
        max_weight=0.95,
        min_regime_samples=5,
    )
    weighted = assess_evidence([good, poor], config, regime="strong_up")
    weights = {decision.model_id: decision.weight for decision in weighted.decisions}
    assert weights[good.model_id] > weights[poor.model_id]
    assert any(
        "regime-specific evidence" in reason
        for decision in weighted.decisions
        for reason in decision.reasons
    )
    insufficient = assess_evidence(
        [replace(good, regime_samples=2), replace(poor, regime_samples=2)],
        config,
        regime="strong_up",
    )
    assert not any(
        "regime-specific evidence" in reason
        for decision in insufficient.decisions
        for reason in decision.reasons
    )


def test_evaluation_fits_only_validation_and_persists_config_and_test_metrics(
    harness: _Harness,
) -> None:
    validation = [
        _observation(0.01, 0.02, validation=True),
        _observation(0.01, 0.00, validation=True),
        _observation(0.01, 0.03, validation=True),
        _observation(0.01, 0.01, validation=True),
        _observation(0.01, 0.02, validation=True),
    ]
    test_rows = [
        _observation(0.01, 0.02, validation=False),
        _observation(0.01, 0.00, validation=False),
    ]
    body = __import__("asyncio").run(
        harness.service.evaluate(
            INSTRUMENT,
            ForecastHorizon.DAY_1,
            validation=validation,
            test=test_rows,
            weights={"naive@v1": 1.0},
            dataset_version="ds-v1",
            now=NOW,
        )
    )
    assert body["calibration"]["samples"] == 5
    assert body["test"]["samples"] == 2
    assert body["validation"]["samples"] == 5
    # The random-walk baseline must price from the reference, not the actual;
    # using the actual price made the baseline look perfect (MAE 0).
    assert body["baseline_metrics"]["random_walk"]["price_mae"] > 0
    assert body["config_id"].startswith("enc_")
    assert harness.prediction.latest_ensemble_calibration(INSTRUMENT, "1d") is not None
    assert harness.prediction.ensemble_evaluations(instrument_id=INSTRUMENT, horizon="1d")
    assert any(
        item["kind"] == "ensemble"
        for item in harness.forecasts.training_runs(limit=10)
    )


def test_regime_metrics_are_grouped_separately() -> None:
    rows = [
        _observation(0.01, 0.02, validation=False, regime="sideways"),
        _observation(0.01, 0.02, validation=False, regime="high_volatility"),
    ]
    grouped = coverage_by_regime(rows)
    assert set(grouped) == {"high_volatility", "sideways"}
    assert grouped["sideways"]["samples"] == 1


def test_config_and_forecast_are_reproducible_from_the_store(harness: _Harness) -> None:
    harness.register()
    body = _generate(harness)
    first = harness.prediction.get_forecast(body["forecast_id"])
    assert first is not None
    assert first.metadata["config_id"] == body["config_id"]
    assert set(first.metadata["weights"]) == set(body["ensemble"]["weighting"]["weights"])
    for key, value in first.metadata["weights"].items():
        assert math.isclose(value, body["ensemble"]["weighting"]["weights"][key], rel_tol=1e-9)
    reread = harness.service.forecast(body["forecast_id"])
    assert reread["digest"] == first.digest
    assert reread["ensemble"]["metadata"]["config_id"] == body["config_id"]


def test_application_dispatch_and_conflict_validation() -> None:
    class _FakeEnsemble:
        async def generate(self, instrument_id: str, horizon: str, **kwargs: Any) -> dict[str, Any]:
            return {"ok": True, "engine": "ensemble", "instrument_id": instrument_id}

        def forecast(self, forecast_id: str) -> dict[str, Any]:
            return {"ok": True, "forecast_id": forecast_id}

        def forecasts(self, **kwargs: Any) -> dict[str, Any]:
            return {"ok": True, "forecasts": []}

        def models(self, **kwargs: Any) -> dict[str, Any]:
            return {"ok": True, "models": []}

        def calibration(self, instrument_id: str, horizon: str) -> dict[str, Any]:
            return {"ok": True, "status": "unavailable"}

        def performance(self, **kwargs: Any) -> dict[str, Any]:
            return {"ok": True, "evaluations": []}

    service = NovaMarketService.__new__(NovaMarketService)
    service._ensemble_service = _FakeEnsemble()  # type: ignore[assignment]
    body = __import__("asyncio").run(
        service.forecast_generate(INSTRUMENT, "1d", engine="ensemble")
    )
    assert body["engine"] == "ensemble"
    assert service.forecast_get("ens_abc")["forecast_id"] == "ens_abc"
    with pytest.raises(EnsembleValidationError):
        __import__("asyncio").run(
            service.forecast_generate(
                INSTRUMENT, "1d", engine="ensemble", statistical=True
            )
        )


def test_api_forecast_routes_accept_the_ensemble_engine() -> None:
    try:
        from fastapi.testclient import TestClient
    except Exception:  # pragma: no cover - optional dev dependency
        pytest.skip("httpx/TestClient is unavailable")

    from novacontrol.api.app import create_app
    from novacontrol.application import NovaControlApplication
    from novacontrol.browser import NoopBrowserRunner
    from novacontrol.desktop import NoopDesktopRunner

    with TemporaryDirectory() as tmp:
        def isolated(**kwargs: Any) -> NovaControlApplication:
            kwargs["data_dir"] = tmp
            return NovaControlApplication(**kwargs)

        with mock.patch(
            "novacontrol.api.app.NovaControlApplication", side_effect=isolated
        ), mock.patch(
            "novacontrol.application.LocalDesktopRunner", NoopDesktopRunner
        ), mock.patch(
            "novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner
        ):
            client = TestClient(create_app())
            with client:
                status = client.get("/market/forecasts/status")
                assert status.status_code == 200
                assert "ensemble" in status.json()
                body = client.post(
                    "/market/forecasts",
                    json={
                        "instrument_id": INSTRUMENT,
                        "horizon": "1d",
                        "engine": "ensemble",
                    },
                )
                assert body.status_code == 200, body.text
                assert body.json()["engine"] == "ensemble"
                assert body.json()["status"] in {"abstained", "fallback", "assembled"}
                calibration = client.get(
                    "/market/forecasts/calibration",
                    params={"model_id": "ensemble", "horizon": "1d", "engine": "ensemble"},
                )
                assert calibration.status_code == 200
                assert calibration.json()["status"] == "unavailable"


def test_newest_training_run_wins_when_runs_share_a_finished_cutoff(tmp_path: Path) -> None:
    """A shared finished_at must not let an older, thinner run be selected.

    Every run that stops at the same cutoff records the same finished_at, and
    started_at is the training window's FIRST BAR - so a run trained on a
    longer lookback carries an earlier started_at.  Sorting on either field
    selected the older three-sample run while a newer five-sample run existed
    (found on the live 30d regeneration).  The write timestamp decides, the
    older run is stored first, and its start date is the more recent one so an
    ordering mistake cannot pass by accident.
    """
    shared = "2026-09-15T10:00:00+00:00"

    def run(run_id: str, *, samples: int, started: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "instrument_id": INSTRUMENT,
            "horizon": "1d",
            "kind": "statistical",
            "model_id": "naive",
            "version": "",
            "started_at": started,
            "finished_at": shared,
            "samples": samples,
            "report": {
                "models": {
                    "naive": {
                        "model_id": "naive",
                        "validation": {"samples": samples, "mae": 1.0},
                    }
                }
            },
        }

    older = run("run_older", samples=3, started="2025-05-01T10:00:00+00:00")
    newer = run("run_newer", samples=5, started="2023-08-23T10:00:00+00:00")
    assert older["started_at"] > newer["started_at"], "the trap: data start is not run time"

    stamps = iter(
        [
            datetime(2026, 11, 20, 0, 0, 1, tzinfo=UTC),
            datetime(2026, 11, 20, 0, 0, 2, tzinfo=UTC),
        ]
    )
    forecasts = ForecastStore(tmp_path / "forecast.sqlite", clock=lambda: next(stamps))
    forecasts.save_training_run(older)
    forecasts.save_training_run(newer)

    runs = forecasts.training_runs(limit=10)
    assert [row["run_id"] for row in runs] == ["run_newer", "run_older"]
    assert runs[0]["recorded_at"] > runs[1]["recorded_at"]

    service = EnsembleForecastingService(forecast_store=forecasts, clock=lambda: NOW)
    selected = service._run_for(
        instrument_id=INSTRUMENT,
        horizon=ForecastHorizon.DAY_1,
        model_id="naive",
        family=ModelFamily.NAIVE,
        version="",
        runs=runs,
    )
    assert selected["run_id"] == "run_newer"
    assert selected["samples"] == 5

    # The pick must not depend on the order the rows are handed over.
    reversed_selection = service._run_for(
        instrument_id=INSTRUMENT,
        horizon=ForecastHorizon.DAY_1,
        model_id="naive",
        family=ModelFamily.NAIVE,
        version="",
        runs=list(reversed(runs)),
    )
    assert reversed_selection["run_id"] == "run_newer"
    assert reversed_selection["samples"] == 5
