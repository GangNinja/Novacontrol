"""PI-07 orchestration over NovaMarket's existing prediction authorities.

The service adds sequence inference and bounded training.  It does not fetch
market data, create a second model registry, or choose a model from test data:
bars come from PI-02/PI-05's existing series adapter, features from PI-04, model
rows from ``PredictionStore`` and evaluation history from ``ForecastStore``.
"""

from __future__ import annotations

import math
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from novacontrol.novamarket.contracts import (
    ForecastDataError,
    ForecastNotFoundError,
    ForecastValidationError,
)
from novacontrol.novamarket.features.models import FeatureProfileName
from novacontrol.novamarket.gradient.models import (
    ClassifierReport,
    FeatureSchema,
    FeatureStats,
    GradientConfig,
    GradientFamily,
    GradientStatus,
    IntervalCalibration,
)
from novacontrol.novamarket.gradient.service import (
    GradientForecastingService,
    _profile,
)
from novacontrol.novamarket.gradient.training import (
    backend_available as gradient_backend_available,
    baseline_metrics,
    family_result,
    out_of_distribution,
    selection_suitability,
    train_family,
)
from novacontrol.novamarket.marketdata.models import DataInterval, DataQuality, Freshness
from novacontrol.novamarket.prediction.models import (
    CalibrationStatus,
    Direction,
    ConfirmationStage,
    ForecastHorizon,
    ForecastOutput,
    ForecastValidity,
    HORIZON_SPECS,
    ModelFamily,
    ModelMetadata,
    ModelState,
    PredictionUncertainty,
    ProbabilityStatus,
    digest,
    prediction_id,
)
from novacontrol.novamarket.prediction.store import PredictionStore
from novacontrol.novamarket.statistical.metrics import StatisticalMetrics
from novacontrol.novamarket.statistical.models import (
    classify_direction,
    decimal_price,
)
from novacontrol.novamarket.statistical.service import (
    StatisticalForecastingService,
    _aware,
    _horizon,
)
from novacontrol.novamarket.statistical.validation import (
    default_resolution,
    horizon_steps,
)

from .artifacts import LSTMArtifactStore
from .models import (
    LSTM_CONTRACT_VERSION,
    LSTM_FORECAST_PREFIX,
    LSTM_MODEL_ID,
    LSTM_PERFORMANCE_PREFIX,
    LSTM_RUN_PREFIX,
    LSTM_SCHEMA_VERSION,
    LSTM_METHODOLOGY_VERSION,
    PI07_PHASE,
    LSTMArtifact,
    LSTMConfig,
    LSTMEvaluationReport,
    LSTMForecast,
    LSTMLoaded,
    LSTMResult,
    LSTMTrainingError,
    LSTMUnavailableError,
    LSTMValidationError,
    SequenceScaler,
    lstm_model_version,
)
from .network import backend_available, backend_version, hardware_status, torch_module
from .sequence import build_inference_sequence
from .training import FittedLSTM, fitted_to_result, train_lstm

_MODEL_ID = LSTM_MODEL_ID
_LSTM_MODEL_IDS = frozenset({_MODEL_ID})


def _range_for(matrix: Any, indices: Sequence[int]) -> dict[str, Any]:
    if not indices:
        return {"start": None, "end": None, "samples": 0}
    return {
        "start": matrix.rows[indices[0]].decision_at.isoformat(),
        "end": matrix.rows[indices[-1]].decision_at.isoformat(),
        "samples": len(indices),
    }


def _schema_from(payload: Mapping[str, Any]) -> FeatureSchema:
    names = tuple(str(item) for item in (payload.get("names") or ()))
    return FeatureSchema(
        names=names,
        feature_set_version=str(payload.get("feature_set_version") or ""),
        profile=str(payload.get("profile") or ""),
        source_names=tuple(str(item) for item in (payload.get("source_names") or ())),
        dropped={
            str(key): str(value)
            for key, value in (payload.get("dropped") or {}).items()
        },
        contract_version=str(payload.get("contract_version") or ""),
        digest_value=str(payload.get("digest") or ""),
    )


def _artifact_from(payload: Mapping[str, Any]) -> LSTMArtifact:
    return LSTMArtifact(
        version=str(payload.get("version") or ""),
        state_path=str(payload.get("state_path") or ""),
        state_sha256=str(payload.get("state_sha256") or ""),
        state_bytes=int(payload.get("state_bytes") or 0),
        architecture=dict(payload.get("architecture") or {}),
        format=str(payload.get("format") or "torch_state_dict"),
        loaded_verified=False,
    )


def _interval_from(payload: Mapping[str, Any]) -> IntervalCalibration:
    return IntervalCalibration(
        level=float(payload.get("level") or 0.8),
        lower_offset=float(payload.get("lower_offset") or 0.0),
        upper_offset=float(payload.get("upper_offset") or 0.0),
        samples=int(payload.get("samples") or 0),
        method=str(payload.get("method") or "validation_residual_quantiles"),
        supported=bool(payload.get("supported", True)),
    )


def _feature_stats_from(payload: Mapping[str, Any]) -> FeatureStats:
    return FeatureStats(
        medians={str(key): float(value) for key, value in (payload.get("medians") or {}).items()},
        scales={str(key): float(value) for key, value in (payload.get("scales") or {}).items()},
        minimums={
            str(key): float(value) for key, value in (payload.get("minimums") or {}).items()
        },
        maximums={
            str(key): float(value) for key, value in (payload.get("maximums") or {}).items()
        },
    )


def _status_from_suitability(status: str) -> str:
    if status == "gradient_suitable":
        return "lstm_suitable"
    if status in {"trained", "provisional", "baselines_preferred", "insufficient_data"}:
        return status
    return "provisional"


class LSTMForecastingService(GradientForecastingService):
    """PI-07 sequence family over PI-01/PI-02/PI-04/PI-05/PI-06 surfaces."""

    def __init__(
        self,
        *,
        store_path: str | Path | None = None,
        market_data: Any | None = None,
        dataset_service: Any | None = None,
        feature_service: Any | None = None,
        prediction_store: PredictionStore | None = None,
        forecast_store: Any | None = None,
        publisher: Any | None = None,
        clock: Any | None = None,
        artifact_root: str | Path | None = None,
        default_config: LSTMConfig | Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            store_path=store_path,
            market_data=market_data,
            dataset_service=dataset_service,
            feature_service=feature_service,
            prediction_store=prediction_store,
            forecast_store=forecast_store,
            publisher=publisher,
            clock=clock,
        )
        if artifact_root is None:
            if store_path and str(store_path) != ":memory:":
                artifact_root = Path(store_path).resolve().parent / "lstm_models"
            else:
                artifact_root = Path("data") / "novamarket" / "lstm_models"
        self._lstm_artifacts = LSTMArtifactStore(artifact_root)
        self._default_lstm_config = (
            default_config
            if isinstance(default_config, LSTMConfig)
            else LSTMConfig.from_dict(default_config)
        )

    @property
    def artifact_root(self) -> Path:
        return self._lstm_artifacts.root

    # -- model registry ------------------------------------------------------

    def _latest_lstm_model(
        self,
        horizon: ForecastHorizon,
        version: str = "",
        instrument_id: str = "",
        target_steps: int | None = None,
    ) -> ModelMetadata | None:
        candidates: list[ModelMetadata] = []
        for item in self._prediction_store.models(horizon):
            if item.model_id != _MODEL_ID or horizon not in item.supported_horizons:
                continue
            if version and item.version != version:
                continue
            metrics = item.metrics or {}
            if not isinstance(metrics.get("artifact"), Mapping):
                continue
            if not isinstance(metrics.get("scaler"), Mapping):
                continue
            if not isinstance(metrics.get("feature_schema"), Mapping):
                continue
            if target_steps is not None:
                # A model trained for a different bar resolution/target span
                # must not be used merely because it shares a horizon label.
                raw_steps = metrics.get("target_steps")
                if raw_steps is None:
                    continue
                try:
                    stored_steps = int(str(raw_steps))
                except (TypeError, ValueError):
                    continue
                if stored_steps != int(target_steps):
                    continue
            if instrument_id:
                instruments = {
                    str(value) for value in (metrics.get("instruments") or ())
                }
                if (
                    str(metrics.get("instrument_id") or "") != instrument_id
                    and instrument_id not in instruments
                ):
                    continue
            candidates.append(item)
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda item: (
                item.updated_at or datetime.min.replace(tzinfo=UTC),
                item.version,
            ),
            reverse=True,
        )[0]

    def _load_lstm_model(
        self,
        horizon: ForecastHorizon,
        version: str = "",
        instrument_id: str = "",
        target_steps: int | None = None,
    ) -> tuple[ModelMetadata, LSTMLoaded]:
        metadata = self._latest_lstm_model(
            horizon, version, instrument_id, target_steps
        )
        if metadata is None:
            label = version or "latest"
            raise LSTMUnavailableError(
                f"no trained LSTM model for {horizon.value} ({label}); "
                "run a PI-07 training/evaluation first"
            )
        metrics = metadata.metrics or {}
        artifact_payload = metrics.get("artifact")
        schema_payload = metrics.get("feature_schema")
        scaler_payload = metrics.get("scaler")
        config_payload = metrics.get("config")
        interval_payload = metrics.get("interval")
        stats_payload = metrics.get("feature_stats")
        if not isinstance(artifact_payload, Mapping):
            raise LSTMUnavailableError("model metadata has no readable artifact")
        if not isinstance(schema_payload, Mapping):
            raise LSTMUnavailableError("model metadata has no readable feature schema")
        if not isinstance(scaler_payload, Mapping):
            raise LSTMUnavailableError("model metadata has no readable scaler")
        if not isinstance(config_payload, Mapping):
            raise LSTMUnavailableError("model metadata has no readable config")
        if not isinstance(interval_payload, Mapping):
            raise LSTMUnavailableError("model metadata has no readable interval")
        schema = _schema_from(schema_payload)
        scaler = SequenceScaler.from_dict(scaler_payload)
        config = LSTMConfig.from_dict(config_payload)
        interval = _interval_from(interval_payload)
        stats = (
            _feature_stats_from(stats_payload)
            if isinstance(stats_payload, Mapping)
            else FeatureStats(medians={}, scales={}, minimums={}, maximums={})
        )
        artifact = _artifact_from(artifact_payload)
        network = self._lstm_artifacts.load(
            artifact, config=config, input_size=len(schema.names)
        )
        artifact = replace(artifact, loaded_verified=True)
        return metadata, LSTMLoaded(
            version=metadata.version,
            config=config,
            schema=schema,
            scaler=scaler,
            network=network,
            interval=interval,
            feature_stats=stats,
            feature_importance=tuple(
                dict(item) for item in (metrics.get("feature_importance") or ())
            ),
            artifact=artifact,
        )

    def _register_lstm_model(
        self,
        result: LSTMResult,
        *,
        horizon: ForecastHorizon,
        cutoff: datetime,
        dataset_version: str,
        selected: bool,
        report_id: str,
        instrument_id: str = "",
        target_steps: int | None = None,
        resolution: str = "",
    ) -> None:
        try:
            existing_instruments: set[str] = set()
            for item in self._prediction_store.models(horizon):
                if item.model_id != _MODEL_ID or item.version != result.version:
                    continue
                existing_instruments.update(
                    str(value)
                    for value in ((item.metrics or {}).get("instruments") or ())
                    if str(value)
                )
            if instrument_id:
                existing_instruments.add(instrument_id)
            metrics = result.registry_metrics(report_id)
            metrics.update(
                {
                    "horizon": horizon.value,
                    "target_steps": target_steps,
                    "resolution": resolution,
                    "selected": selected,
                    "instrument_id": instrument_id or None,
                    "instruments": sorted(existing_instruments),
                    "dataset_version": dataset_version,
                    "feature_set_version": (
                        result.schema.feature_set_version if result.schema else "unspecified"
                    ),
                    "experimental": True,
                    "production_ready": False,
                    "automatic_promotion": False,
                    "feature_stats": (
                        result.feature_stats.to_dict()
                        if result.feature_stats
                        else None
                    ),
                }
            )
            state = (
                ModelState.ELIGIBLE
                if result.status == "lstm_suitable"
                else ModelState.DEGRADED
            )
            metadata = ModelMetadata(
                model_id=_MODEL_ID,
                version=result.version,
                family=ModelFamily.LSTM,
                state=state,
                supported_horizons=(horizon,),
                calibration=CalibrationStatus.UNCALIBRATED,
                metrics=metrics,
                trained_at=cutoff,
                updated_at=cutoff,
                notes=(
                    "PI-07 LSTM sequence model; research evidence only, not "
                    "automatically promoted, no risk or execution authority"
                ),
                feature_set_version=(
                    result.schema.feature_set_version if result.schema else "unspecified"
                ),
                dataset_version=dataset_version,
            )
            self._prediction_store.register_model(metadata)
        except Exception as exc:  # noqa: BLE001 - registration isolation
            self._record_failure(_MODEL_ID, "register_model", exc)
            raise

    def _register_lstm_unavailable(
        self,
        horizon: ForecastHorizon,
        *,
        cutoff: datetime,
        reason: str,
        dataset_version: str,
    ) -> None:
        try:
            self._prediction_store.register_model(
                ModelMetadata(
                    model_id=_MODEL_ID,
                    version=f"pi07-lstm-unavailable-{horizon.value}",
                    family=ModelFamily.LSTM,
                    state=ModelState.UNAVAILABLE,
                    supported_horizons=(horizon,),
                    calibration=CalibrationStatus.UNCALIBRATED,
                    metrics={
                        "horizon": horizon.value,
                        "failure": reason,
                        "experimental": True,
                        "production_ready": False,
                        "automatic_promotion": False,
                    },
                    trained_at=cutoff,
                    updated_at=cutoff,
                    notes="PI-07 LSTM unavailable for this evidence run",
                    feature_set_version="unspecified",
                    dataset_version=dataset_version,
                )
            )
        except Exception as exc:  # noqa: BLE001
            self._record_failure(_MODEL_ID, "register_unavailable", exc)

    # -- training and evaluation --------------------------------------------

    def _failed_result(
        self,
        *,
        config: LSTMConfig,
        schema: FeatureSchema | None,
        reason: str,
        version: str = "pi07-lstm-unavailable",
        train_samples: int = 0,
        validation_samples: int = 0,
        test_samples: int = 0,
    ) -> LSTMResult:
        return LSTMResult(
            model_id=_MODEL_ID,
            version=version,
            status="failed",
            config=config,
            schema=schema,
            scaler=None,
            validation=None,
            test=None,
            classifier_validation=None,
            classifier_test=None,
            interval=None,
            feature_importance=(),
            artifact=None,
            feature_stats=None,
            train_samples=train_samples,
            validation_samples=validation_samples,
            test_samples=test_samples,
            training_start=None,
            training_end=None,
            dependency_version=backend_version(),
            sequence_length=config.sequence_length,
            reason=reason,
            notes=("failure isolated; previous valid LSTM artifacts remain untouched",),
            failures=(reason,),
        )

    async def evaluate(  # type: ignore[override]
        self,
        instrument_id: str,
        horizon: ForecastHorizon | str,
        *,
        as_of: datetime | None = None,
        resolution: DataInterval | str | None = None,
        dataset_id: str = "",
        dataset_version: str = "",
        config: LSTMConfig | Mapping[str, Any] | None = None,
        bars: Sequence[Any] | None = None,
        fundamentals: Sequence[Any] = (),
        news_events: Sequence[Any] = (),
        benchmark_bars: Mapping[str, Sequence[Any]] | None = None,
        sector_bars: Mapping[str, Sequence[Any]] | None = None,
        context_bars: Mapping[str, Mapping[str, Sequence[Any]]] | None = None,
        models: Sequence[str] | None = None,
        families: Sequence[str] | None = None,
        train_ratio: float | None = None,
        validation_ratio: float | None = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        cutoff = _aware(as_of or self._clock(), "as_of")
        chosen_horizon = _horizon(horizon)
        chosen_config = (
            config
            if isinstance(config, LSTMConfig)
            else LSTMConfig.from_dict(config)
            if config is not None
            else self._default_lstm_config
        )
        if train_ratio is not None or validation_ratio is not None:
            chosen_config = replace(
                chosen_config,
                train_ratio=(
                    chosen_config.train_ratio
                    if train_ratio is None
                    else float(train_ratio)
                ),
                validation_ratio=(
                    chosen_config.validation_ratio
                    if validation_ratio is None
                    else float(validation_ratio)
                ),
            )
        if not backend_available():
            reason = (
                "PyTorch is not installed; install the optional 'lstm' extra to "
                "train sequence models"
            )
            self._record_failure(_MODEL_ID, "prepare", LSTMUnavailableError(reason))
            self._register_lstm_unavailable(
                chosen_horizon,
                cutoff=cutoff,
                reason=reason,
                dataset_version=dataset_version or "unspecified",
            )
            result = self._failed_result(
                config=chosen_config, schema=None, reason=reason
            )
            report = self._report(
                instrument_id,
                chosen_horizon,
                default_resolution(chosen_horizon),
                cutoff,
                "",
                dataset_version or "unspecified",
                "",
                {},
                result,
                {},
                {},
                {},
                (reason,),
                {"device": "cpu", "torch_available": False},
            )
            return self._finish_report(report, cutoff)

        try:
            matrix, ohlcv, series, chosen_resolution, chosen_dataset_id, chosen_dataset_version = (
                self._training_matrix(
                    instrument_id,
                    chosen_horizon,
                    as_of=cutoff,
                    resolution=resolution,
                    dataset_id=dataset_id,
                    dataset_version=dataset_version,
                    bars=bars,
                    config=chosen_config.matrix_config(),
                    fundamentals=fundamentals,
                    news_events=news_events,
                    benchmark_bars=benchmark_bars,
                    sector_bars=sector_bars,
                    context_bars=context_bars,
                )
            )
        except Exception as exc:  # noqa: BLE001 - preparation refuses honestly
            self._record_failure(_MODEL_ID, "prepare", exc)
            raise ForecastDataError(
                f"LSTM training data could not be prepared: {type(exc).__name__}: {exc}"
            ) from exc

        try:
            baselines = baseline_metrics(
                rows=matrix.rows,
                test_indices=matrix.test_indices,
                closes=[float(bar.close) for bar in ohlcv],
                price_bars=series,
                horizon=chosen_horizon,
                resolution=chosen_resolution,
                instrument_id=instrument_id,
                neutral_band_pct=chosen_config.effective_neutral_band_pct,
                bar_id_to_index={
                    str(bar.bar_id): index for index, bar in enumerate(ohlcv)
                },
            )
        except Exception as exc:  # noqa: BLE001 - baseline isolation
            baselines = {}
            self._record_failure("baseline", "evaluate", exc)

        failures: list[str] = []
        fitted: FittedLSTM | None = None
        version = ""
        training_error = ""
        try:
            fitted = train_lstm(
                matrix=matrix,
                config=chosen_config,
                instrument_id=instrument_id,
                horizon=chosen_horizon,
                resolution=chosen_resolution,
                as_of=cutoff,
            )
            version = lstm_model_version(
                instrument_id=instrument_id,
                horizon=chosen_horizon,
                dataset_id=chosen_dataset_id,
                dataset_version=chosen_dataset_version,
                feature_set_version=matrix.schema.feature_set_version,
                target_steps=horizon_steps(chosen_horizon, chosen_resolution),
                sequence_length=fitted.sequence_length,
                config_digest=fitted.config.digest,
                train_start=fitted.training_start,
                train_end=fitted.training_end,
            )
            artifact = self._lstm_artifacts.save(
                instrument_id=instrument_id,
                version=version,
                network=fitted.network,
                config=fitted.config,
                input_size=len(matrix.schema.names),
            )
            result = fitted_to_result(
                fitted, version=version, dependency_version=backend_version()
            )
            result = replace(result, artifact=artifact)
        except Exception as exc:  # noqa: BLE001 - one family never aborts the app
            training_error = f"{type(exc).__name__}: {exc}"
            failures.append(training_error)
            self._record_failure(_MODEL_ID, "train", exc)
            result = self._failed_result(
                config=chosen_config,
                schema=matrix.schema,
                reason=training_error,
                version=version or "pi07-lstm-unavailable",
                train_samples=len(matrix.train_indices),
                validation_samples=len(matrix.validation_indices),
                test_samples=len(matrix.test_indices),
            )
            version = result.version

        # Candidate failures are isolated by train_lstm and must survive into
        # the result/report even when another candidate succeeded.
        if fitted is not None:
            failures.extend(fitted.failures)
            if result.status != "failed":
                result = replace(result, failures=tuple(failures))
        lm_suitability: dict[str, Any] = {}
        if result.test is not None and result.interval is not None:
            try:
                lm_suitability = selection_suitability(
                    result,  # type: ignore[arg-type]
                    baselines,
                    minimum_test_samples=chosen_config.min_test_samples,
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(f"suitability: {type(exc).__name__}: {exc}")
        if result.status != "failed":
            suitability_status = _status_from_suitability(
                str(lm_suitability.get("status") or "provisional")
            )
            result = replace(
                result,
                status=suitability_status,
                selected=True,
                reason=str(lm_suitability.get("reason") or ""),
                failures=tuple(failures),
            )
            if lm_suitability:
                lm_suitability = dict(lm_suitability)
                lm_suitability["status"] = suitability_status
                lm_suitability["model_id"] = _MODEL_ID

        comparisons, comparison_suitability = self._pi06_comparison(
            matrix=matrix,
            config=chosen_config,
            instrument_id=instrument_id,
            horizon=chosen_horizon,
            as_of=cutoff,
            baselines=baselines,
            failures=failures,
        )
        preferred = self._preferred_overall(result, comparisons, baselines)
        report_id = prediction_id(
            LSTM_RUN_PREFIX,
            instrument_id,
            chosen_horizon.value,
            cutoff.isoformat(),
            chosen_dataset_version,
            version,
        )
        if result.artifact is not None:
            try:
                self._register_lstm_model(
                    result,
                    horizon=chosen_horizon,
                    cutoff=cutoff,
                    dataset_version=chosen_dataset_version,
                    selected=True,
                    report_id=report_id,
                    instrument_id=instrument_id,
                    target_steps=horizon_steps(chosen_horizon, chosen_resolution),
                    resolution=chosen_resolution.value,
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(f"register_model: {type(exc).__name__}: {exc}")
        if result.status != "failed":
            # Keep every isolated failure visible on the final result, including
            # suitability and registration failures appended after selection.
            result = replace(result, failures=tuple(failures))
        report = self._report(
            instrument_id,
            chosen_horizon,
            chosen_resolution,
            cutoff,
            chosen_dataset_id,
            chosen_dataset_version,
            matrix.schema.feature_set_version,
            matrix,
            result,
            baselines,
            comparisons,
            comparison_suitability,
            failures,
            {
                "device": "cpu",
                "torch_version": backend_version(),
                "training_duration_seconds": result.training_duration_seconds,
                "inference_latency_ms": result.inference_latency_ms,
                "peak_memory_mb": result.peak_memory_mb,
                "sequence_length": result.sequence_length,
                "train_samples": result.train_samples,
                "validation_samples": result.validation_samples,
                "test_samples": result.test_samples,
                "hardware": hardware_status(),
            },
            report_id=report_id,
        )
        return self._finish_report(
            report,
            cutoff,
            result=result,
            baselines=baselines,
            lm_suitability=lm_suitability,
            fitted=fitted,
        )

    def _pi06_comparison(
        self,
        *,
        matrix: Any,
        config: LSTMConfig,
        instrument_id: str,
        horizon: ForecastHorizon,
        as_of: datetime,
        baselines: Mapping[str, StatisticalMetrics],
        failures: list[str],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        comparisons: dict[str, Any] = {}
        suitability: dict[str, Any] = {}
        for family in (GradientFamily.XGBOOST, GradientFamily.LIGHTGBM):
            if not gradient_backend_available(family):
                comparisons[family.value] = {
                    "status": "unsupported",
                    "reason": f"{family.value} is not installed",
                }
                continue
            started = time.perf_counter()
            try:
                fitted = train_family(
                    family=family,
                    config=config.matrix_config(),
                    matrix=matrix,
                    instrument_id=instrument_id,
                    horizon=horizon,
                    as_of=as_of,
                )
                duration = time.perf_counter() - started
                # These comparison models are intentionally ephemeral: PI-06
                # already owns their registry and artifacts.  PI-07 reports the
                # same-origin out-of-sample comparison without duplicating it.
                result = family_result(
                    fitted,
                    version=f"pi06-comparison-{family.value}",
                    artifact=None,
                    status=GradientStatus.TRAINED,
                    selected=False,
                    reason="PI-07 same-matrix comparison model, not registered",
                )
                comparisons[family.value] = {
                    "model_id": family.value,
                    "version": result.version,
                    "status": result.status.value,
                    "validation": result.validation.to_dict()
                    if result.validation
                    else None,
                    "test": result.test.to_dict() if result.test else None,
                    "classifier_test": result.classifier_test.to_dict()
                    if result.classifier_test
                    else None,
                    "interval": result.interval.to_dict()
                    if result.interval
                    else None,
                    "duration_seconds": duration,
                    "ephemeral": True,
                    "note": (
                        "fitted on exactly the same PI-02/PI-04 matrix and "
                        "evaluation origins; not written to the PI-06 registry"
                    ),
                }
                try:
                    suitability[family.value] = selection_suitability(
                        result,
                        baselines,
                        minimum_test_samples=config.min_test_samples,
                    )
                except Exception as exc:  # noqa: BLE001
                    failures.append(
                        f"{family.value} suitability: {type(exc).__name__}: {exc}"
                    )
            except Exception as exc:  # noqa: BLE001
                failures.append(f"pi06 {family.value}: {type(exc).__name__}: {exc}")
                comparisons[family.value] = {
                    "model_id": family.value,
                    "status": "failed",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "duration_seconds": time.perf_counter() - started,
                }
        return comparisons, suitability

    @staticmethod
    def _preferred_overall(
        result: LSTMResult,
        comparisons: Mapping[str, Any],
        baselines: Mapping[str, StatisticalMetrics],
    ) -> str:
        candidates: list[tuple[float, str]] = []
        if result.test is not None and result.test.price_mae is not None:
            candidates.append((float(result.test.price_mae), _MODEL_ID))
        for name, body in comparisons.items():
            test = body.get("test") if isinstance(body, Mapping) else None
            value = (test or {}).get("price_mae") if isinstance(test, Mapping) else None
            if value is not None:
                candidates.append((float(value), str(name)))
        for name, metrics in baselines.items():
            if metrics.price_mae is not None:
                candidates.append((float(metrics.price_mae), f"baseline:{name}"))
        if not candidates:
            return "statistical_baseline"
        candidates.sort(key=lambda item: item[0])
        if (
            candidates[0][1] == _MODEL_ID
            and result.status not in {"lstm_suitable", "trained"}
        ):
            candidates = [item for item in candidates if item[1] != _MODEL_ID]
        return candidates[0][1] if candidates else "statistical_baseline"

    def _report(
        self,
        instrument_id: str,
        horizon: ForecastHorizon,
        resolution: DataInterval,
        cutoff: datetime,
        dataset_id: str,
        dataset_version: str,
        feature_set_version: str,
        matrix: Any,
        result: LSTMResult,
        baselines: Mapping[str, StatisticalMetrics],
        comparisons: Mapping[str, Any],
        comparison_suitability: Mapping[str, Any],
        failures: Sequence[str],
        resource: Mapping[str, Any],
        report_id: str | None = None,
    ) -> LSTMEvaluationReport:
        if matrix is not None:
            train_range = _range_for(matrix, matrix.train_indices)
            validation_range = _range_for(matrix, matrix.validation_indices)
            test_range = _range_for(matrix, matrix.test_indices)
            feature_set_version = matrix.schema.feature_set_version
            regime_samples = dict(matrix.regime_samples)
        else:
            train_range = validation_range = test_range = {
                "start": None,
                "end": None,
                "samples": 0,
            }
            regime_samples = {}
        status = result.status
        suitability: dict[str, Any] = {
            _MODEL_ID: {
                "suitable": status == "lstm_suitable",
                "status": status,
                "reason": result.reason,
                "test_samples": result.test_samples,
                "interval_coverage": (
                    result.test.interval_coverage if result.test else None
                ),
            }
        }
        suitability.update(
            {str(key): dict(value) for key, value in comparison_suitability.items()}
        )
        insufficient = [
            name for name, count in regime_samples.items() if int(count) < 3
        ]
        if not report_id:
            report_id = prediction_id(
                LSTM_RUN_PREFIX,
                instrument_id,
                horizon.value,
                cutoff.isoformat(),
                dataset_version,
                result.version,
            )
        return LSTMEvaluationReport(
            instrument_id=instrument_id,
            horizon=horizon,
            resolution=resolution,
            horizon_steps=horizon_steps(horizon, resolution),
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            feature_set_version=feature_set_version,
            feature_profile=_profile(horizon, resolution).value,
            target_definition="future_close / decision_close - 1",
            train_range=train_range,
            validation_range=validation_range,
            test_range=test_range,
            families={_MODEL_ID: result},
            baselines=baselines,
            pi06_comparison=comparisons,
            selected_model=_MODEL_ID if result.artifact is not None else None,
            selected_version=result.version if result.artifact is not None else None,
            preferred_overall=self._preferred_overall(result, comparisons, baselines),
            selection_metric="validation_loss_and_price_error; test reported once",
            suitability=suitability,
            regime_samples=regime_samples,
            insufficient_regimes=insufficient,
            failures=tuple(failures),
            notes=(
                "LSTM is compared on the same PI-02 rows, PI-04 features, targets "
                "and test origins as PI-05/PI-06.",
                "Regime cells below three test samples are listed as insufficient.",
                "No calibrated probability or guaranteed direction is claimed.",
            ),
            resource=resource,
            report_id=report_id,
            test_used_for_selection=False,
            leakage_safe=True,
        )

    def _finish_report(
        self,
        report: LSTMEvaluationReport,
        cutoff: datetime,
        *,
        result: LSTMResult | None = None,
        baselines: Mapping[str, StatisticalMetrics] | None = None,
        lm_suitability: Mapping[str, Any] | None = None,
        fitted: FittedLSTM | None = None,
    ) -> dict[str, Any]:
        body = report.to_dict()
        if result is not None and result.test is not None:
            try:
                snapshot = {
                    "snapshot_id": prediction_id(
                        LSTM_PERFORMANCE_PREFIX,
                        report.instrument_id,
                        report.horizon.value,
                        result.version,
                        report.report_id,
                    ),
                    "model_id": _MODEL_ID,
                    "instrument_id": report.instrument_id,
                    "horizon": report.horizon.value,
                    "samples": result.test.samples,
                    "created_at": cutoff.isoformat(),
                    "metrics": result.test.to_dict(),
                }
                self._forecast_store.save_performance(snapshot)
            except Exception as exc:  # noqa: BLE001
                self._record_failure(_MODEL_ID, "persist_performance", exc)
        try:
            selected = report.families.get(_MODEL_ID)
            self._forecast_store.save_training_run(
                {
                    "run_id": report.report_id,
                    "instrument_id": report.instrument_id,
                    "horizon": report.horizon.value,
                    "model_id": _MODEL_ID,
                    "version": selected.version if selected else "unavailable",
                    "started_at": (
                        (selected.training_start or cutoff).isoformat()
                        if selected
                        else cutoff.isoformat()
                    ),
                    "finished_at": cutoff.isoformat(),
                    "samples": sum(
                        int(item.get("samples", 0))
                        for item in (
                            report.train_range,
                            report.validation_range,
                            report.test_range,
                        )
                    ),
                    "kind": "lstm",
                    "selected_model": report.selected_model,
                    "selected_config": selected.config.to_dict() if selected else None,
                    "report": body,
                }
            )
        except Exception as exc:  # noqa: BLE001
            self._record_failure(_MODEL_ID, "persist_evaluation", exc)
        body.update(
            {
                "ok": True,
                "run_id": report.report_id,
                "selected_model": report.selected_model,
                "selected_version": report.selected_version,
                "selection_metric": report.selection_metric,
                "automatic_promotion": False,
                "evidence_class": "measured on historical bars; not a guarantee",
            }
        )
        self._evaluations += 1
        return body

    # -- inference -----------------------------------------------------------

    async def generate(
        self,
        instrument_id: str,
        horizon: ForecastHorizon | str,
        *,
        as_of: datetime | None = None,
        resolution: DataInterval | str | None = None,
        dataset_id: str = "",
        dataset_version: str = "",
        model_id: str = _MODEL_ID,
        version: str = "",
        bars: Sequence[Any] | None = None,
        fundamentals: Sequence[Any] = (),
        news_events: Sequence[Any] = (),
        benchmark_bars: Mapping[str, Sequence[Any]] | None = None,
        sector_bars: Mapping[str, Sequence[Any]] | None = None,
        context_bars: Mapping[str, Mapping[str, Sequence[Any]]] | None = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        if str(model_id).strip().lower() not in {"", _MODEL_ID, "sequence"}:
            raise LSTMValidationError(
                f"this service only generates LSTM forecasts, not {model_id!r}"
            )
        cutoff = _aware(as_of or self._clock(), "as_of")
        chosen_horizon = _horizon(horizon)
        try:
            series, chosen_resolution, chosen_dataset_id, chosen_dataset_version = self._series_for(
                instrument_id,
                chosen_horizon,
                as_of=cutoff,
                resolution=resolution,
                dataset_id=dataset_id,
                dataset_version=dataset_version,
                bars=bars,
            )
        except ForecastValidationError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._record_failure(_MODEL_ID, "prepare_forecast", exc)
            raise ForecastDataError(
                f"LSTM forecast data could not be prepared: {type(exc).__name__}: {exc}"
            ) from exc
        # The caller may supply a page that contains rows after the cutoff
        # (for example a cached history response).  Inference must use only the
        # prefix observed at the forecast timestamp; otherwise the reference
        # close or the feature window could leak a future observation.
        series = [bar for bar in series if bar.observed_at <= cutoff]
        if not series:
            raise LSTMUnavailableError("no observations are available at or before as_of")
        metadata, loaded = self._load_lstm_model(
            chosen_horizon,
            version=version,
            instrument_id=instrument_id,
            target_steps=horizon_steps(chosen_horizon, chosen_resolution),
        )
        if self._feature_service is None:
            raise LSTMUnavailableError(
                "PI-04 feature service is not attached; LSTM inference cannot run"
            )
        ohlcv = self._feature_bars(
            series,
            instrument_id,
            chosen_resolution,
            str(self._safe_instrument(instrument_id).get("currency") or ""),
        )
        try:
            raw, feature_values, feature_set, missing_fraction = build_inference_sequence(
                engine=self._feature_service.engine,
                instrument_id=instrument_id,
                resolution=chosen_resolution,
                profile=_profile(chosen_horizon, chosen_resolution),
                as_of=cutoff,
                bars=ohlcv,
                schema=loaded.schema,
                sequence_length=loaded.config.sequence_length_for(
                    chosen_horizon, chosen_resolution
                ),
                fundamentals=fundamentals,
                news_events=news_events,
                benchmark_bars=benchmark_bars,
                sector_bars=sector_bars,
                context_bars=context_bars,
            )
        except LSTMUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._record_failure(_MODEL_ID, "sequence", exc)
            raise LSTMUnavailableError(
                f"sequence generation failed: {type(exc).__name__}: {exc}"
            ) from exc
        if feature_set.version != loaded.schema.feature_set_version:
            raise LSTMUnavailableError(
                "feature-set version mismatch; the model was trained on "
                f"{loaded.schema.feature_set_version}, current features are "
                f"{feature_set.version}"
            )
        if loaded.schema.profile != _profile(chosen_horizon, chosen_resolution).value:
            raise LSTMUnavailableError(
                "feature-profile mismatch; the model was trained for "
                f"{loaded.schema.profile}"
            )
        if missing_fraction > loaded.config.max_inference_missing_fraction:
            raise LSTMUnavailableError(
                "abstained: "
                f"{missing_fraction:.1%} of the required sequence cells are missing "
                f"(limit {loaded.config.max_inference_missing_fraction:.0%})"
            )
        last_vector = raw[0, -1, :]
        if out_of_distribution(
            last_vector,
            loaded.feature_stats,
            loaded.schema.names,
            z_limit=loaded.config.extrapolation_z,
            max_fraction=loaded.config.max_extrapolation_fraction,
        ):
            raise LSTMUnavailableError(
                "abstained: current features are outside the training distribution"
            )
        values, mask = loaded.scaler.transform(raw)
        torch = torch_module()
        with torch.no_grad():
            value_tensor = torch.from_numpy(values.astype("float32"))
            mask_tensor = torch.from_numpy(mask.astype("float32"))
            prediction, _logits = loaded.network(value_tensor, mask_tensor)
            raw_prediction = float(prediction.detach().cpu().numpy()[0])
        predicted_return = loaded.scaler.inverse_target(raw_prediction)
        lower_return, upper_return = loaded.interval.apply(predicted_return)
        direction, _reason = classify_direction(
            predicted_return,
            lower_return,
            upper_return,
            neutral_band_pct=loaded.config.effective_neutral_band_pct,
            interval_supported=loaded.interval.supported,
        )
        last = series[-1]
        predicted_price = float(last.close) * (1.0 + predicted_return)
        if predicted_price <= 0:
            raise ForecastDataError("the model produced a non-positive predicted price")
        if lower_return <= -1.0:
            lower_return = -0.999999
        lower_price = float(last.close) * (1.0 + lower_return)
        upper_price = float(last.close) * (1.0 + upper_return)
        if lower_price <= 0 or upper_price <= 0:
            raise ForecastDataError("the model produced a non-positive forecast bound")
        width = upper_return - lower_return
        if loaded.interval.supported and width > loaded.config.max_interval_width:
            raise LSTMUnavailableError(
                "abstained: forecast interval width "
                f"{width:.2%} exceeds the configured "
                f"{loaded.config.max_interval_width:.0%} limit"
            )
        if not loaded.interval.supported:
            uncertainty = PredictionUncertainty.UNKNOWN
        elif width <= 0.02:
            uncertainty = PredictionUncertainty.LOW
        elif width >= 0.08:
            uncertainty = PredictionUncertainty.HIGH
        else:
            uncertainty = PredictionUncertainty.MEDIUM
        info = self._safe_instrument(instrument_id)
        target = self._target_at(
            instrument_id,
            cutoff,
            chosen_horizon,
            horizon_steps(chosen_horizon, chosen_resolution),
        )
        if target <= cutoff:
            target = cutoff + HORIZON_SPECS[chosen_horizon].duration
        if target <= cutoff:
            raise ForecastDataError("the forecast target is not after its generation time")
        quality = self._quality(last)
        freshness = self._freshness(last)
        validity = ForecastValidity.VALID
        if quality in {DataQuality.INVALID, DataQuality.UNAVAILABLE} or freshness in {
            Freshness.MISSING,
            Freshness.PROVIDER_UNAVAILABLE,
        }:
            validity = ForecastValidity.INVALID
        elif quality is DataQuality.STALE or freshness is Freshness.STALE:
            validity = ForecastValidity.STALE
        payload_digest = digest(
            {
                "methodology": LSTM_METHODOLOGY_VERSION,
                "family": _MODEL_ID,
                "version": loaded.version,
                "schema": loaded.schema.digest_value,
                "dataset_id": chosen_dataset_id,
                "dataset_version": chosen_dataset_version,
                "as_of": cutoff.isoformat(),
                "predicted_return": predicted_return,
                "lower_return": lower_return,
                "upper_return": upper_return,
            }
        )
        forecast_id = prediction_id(
            LSTM_FORECAST_PREFIX,
            instrument_id,
            chosen_horizon.value,
            cutoff.isoformat(),
            loaded.version,
            chosen_dataset_id,
            chosen_dataset_version,
            payload_digest,
        )
        contract = ForecastOutput(
            forecast_id=forecast_id,
            lifecycle_id=prediction_id(
                "lstm_lifecycle_", _MODEL_ID, instrument_id, chosen_horizon.value
            ),
            lifecycle_stage=ConfirmationStage.INITIAL,
            instrument_id=instrument_id,
            exchange=str(info.get("exchange") or ""),
            currency=str(info.get("currency") or "USD"),
            reference_price=decimal_price(float(last.close)),
            reference_at=last.observed_at,
            predicted_price=decimal_price(predicted_price),
            predicted_return=predicted_return,
            direction=(
                direction
                if isinstance(direction, Direction)
                else Direction(direction)
            ),
            lower_return=lower_return,
            upper_return=upper_return,
            lower_price=decimal_price(lower_price),
            upper_price=decimal_price(upper_price),
            uncertainty=uncertainty,
            uncertainty_width=width,
            p_up=None,
            p_down=None,
            p_neutral=None,
            probability_status=ProbabilityStatus.UNAVAILABLE,
            calibration_status=CalibrationStatus.NOT_APPLICABLE,
            confidence=None,
            ensemble_id="lstm-sequence",
            model_id=_MODEL_ID,
            model_version=loaded.version,
            member_versions=(loaded.version,),
            forecast_horizon=chosen_horizon,
            target_at=target,
            generated_at=cutoff,
            data_observed_at=last.observed_at,
            data_received_at=cutoff,
            data_resolution=chosen_resolution,
            data_freshness=freshness,
            data_quality=quality,
            data_snapshot_id="sb_" + payload_digest[:24],
            validity=validity,
            validity_conditions=(
                "input windows are chronological and observed at or before the cutoff",
                "preprocessing was fitted on training windows and loaded with the artifact",
                "the interval is validation-residual based, not a guarantee",
                "classifier probabilities are uncalibrated and withheld",
                "not a recommendation, risk decision or execution instruction",
            ),
            disagreement=0.0,
            digest=payload_digest,
            notes=(
                f"PI-07 LSTM sequence model {loaded.version}",
                f"sequence_length={loaded.config.sequence_length_for(chosen_horizon, chosen_resolution)}",
                f"feature_schema={loaded.schema.digest_value}",
                "research evidence only; not automatically promoted",
                "no risk or execution authority",
            ),
            metadata={
                "phase": PI07_PHASE,
                "lstm_contract_version": LSTM_CONTRACT_VERSION,
                "schema_version": LSTM_SCHEMA_VERSION,
                "methodology_version": LSTM_METHODOLOGY_VERSION,
                "config": loaded.config.to_dict(),
                "config_digest": loaded.config.digest,
                "artifact": loaded.artifact.to_dict(),
                "feature_schema": loaded.schema.to_dict(),
                "scaler": loaded.scaler.to_dict(),
                "feature_values": dict(feature_values),
                "feature_importance": [dict(item) for item in loaded.feature_importance],
                "missing_feature_fraction": missing_fraction,
                "sequence_length": loaded.config.sequence_length_for(
                    chosen_horizon, chosen_resolution
                ),
                "interval": loaded.interval.to_dict(),
                "dataset_id": chosen_dataset_id,
                "dataset_version": chosen_dataset_version,
                "feature_set_id": feature_set.feature_set_id,
                "horizon_steps": horizon_steps(chosen_horizon, chosen_resolution),
                "promotion": "not_promoted",
                "automatic_promotion": False,
                "production_ready": False,
                "probabilities_withheld": True,
            },
            feature_set_version=loaded.schema.feature_set_version,
            dataset_version=chosen_dataset_version,
        )
        stored = False
        try:
            stored = self._prediction_store.save_forecast(contract)
        except Exception as exc:  # noqa: BLE001
            self._record_failure(_MODEL_ID, "persist", exc)
        if not stored:
            try:
                stored = self._prediction_store.get_forecast(forecast_id) is not None
            except Exception:  # noqa: BLE001
                stored = False
        self._generations += 1
        await self._publish(
            "novamarket.forecast.created",
            forecast_id=forecast_id,
            instrument_id=instrument_id,
            horizon=chosen_horizon.value,
            model_id=_MODEL_ID,
        )
        body = LSTMForecast(
            contract=contract,
            result=None,
            schema=loaded.schema,
            scaler=loaded.scaler,
            feature_values=feature_values,
            sequence_length=loaded.config.sequence_length_for(
                chosen_horizon, chosen_resolution
            ),
            missing_fraction=missing_fraction,
            interval=loaded.interval,
            persisted=stored,
        ).to_dict()
        body.update(
            {
                "ok": True,
                "status": "pending",
                "model": {
                    "model_id": _MODEL_ID,
                    "model_version": loaded.version,
                    "family": "lstm",
                    "config": loaded.config.to_dict(),
                },
                "persisted": stored,
                "behavioral_model_id": metadata.model_id,
            }
        )
        return body

    # -- read surfaces -------------------------------------------------------

    def forecast(self, forecast_id: str) -> dict[str, Any]:
        contract = self._prediction_store.get_forecast(forecast_id)
        if contract is None:
            raise ForecastNotFoundError(f"unknown forecast {forecast_id!r}")
        if not contract.forecast_id.startswith(LSTM_FORECAST_PREFIX) and contract.model_id != _MODEL_ID:
            raise ForecastNotFoundError(
                f"forecast {forecast_id!r} is not a PI-07 LSTM forecast"
            )
        body = contract.to_dict()
        body.update(
            {
                "ok": True,
                "lstm": {
                    "model_id": _MODEL_ID,
                    "model_version": contract.model_version,
                    "experimental": True,
                    "production_ready": False,
                    "automatic_promotion": False,
                },
            }
        )
        return body

    def forecasts(
        self,
        *,
        instrument_id: str | None = None,
        horizon: ForecastHorizon | str | None = None,
        model_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        label = _horizon(horizon) if horizon is not None else None
        if model_id and str(model_id).strip().lower() not in {"", _MODEL_ID, "sequence"}:
            return {"ok": True, "forecasts": [], "total": 0}
        rows = self._prediction_store.list_forecasts(
            instrument_id=instrument_id,
            horizon=label,
            model_id=_MODEL_ID,
            limit=limit,
        )
        lstm_rows = [
            row
            for row in rows
            if row.forecast_id.startswith(LSTM_FORECAST_PREFIX)
            or row.model_id == _MODEL_ID
        ]
        return {
            "ok": True,
            "forecasts": [row.to_dict() for row in lstm_rows],
            "total": len(lstm_rows),
        }

    def models(
        self,
        *,
        instrument_id: str | None = None,
        horizon: ForecastHorizon | str | None = None,
    ) -> dict[str, Any]:
        label = _horizon(horizon) if horizon is not None else None
        rows = [
            item.to_dict()
            for item in self._prediction_store.models(label)
            if item.model_id == _MODEL_ID
        ]
        if instrument_id:
            rows = [
                row
                for row in rows
                if instrument_id
                in ((row.get("metrics") or {}).get("instruments") or ())
                or (row.get("metrics") or {}).get("instrument_id") == instrument_id
            ]
        return {
            "ok": True,
            "models": rows,
            "total": len(rows),
            "instrument_filter": instrument_id,
            "experimental": True,
            "production_ready": False,
        }

    def evaluations(self, *, limit: int = 20) -> dict[str, Any]:
        rows = [
            row
            for row in self._forecast_store.training_runs(limit=limit)
            if str(row.get("kind")) == "lstm"
        ]
        return {"ok": True, "evaluations": rows, "total": len(rows)}

    def performance(
        self,
        *,
        model_id: str | None = None,
        instrument_id: str | None = None,
        horizon: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if model_id and str(model_id).strip().lower() not in {"", _MODEL_ID}:
            return []
        rows = self._forecast_store.performance(
            model_id=_MODEL_ID,
            instrument_id=instrument_id,
            horizon=horizon,
            limit=limit,
        )
        return [
            row
            for row in rows
            if str(row.get("snapshot_id") or "").startswith(LSTM_PERFORMANCE_PREFIX)
        ]

    def suitability(  # type: ignore[override]
        self,
        instrument_id: str,
        horizon: ForecastHorizon | str,
        *,
        as_of: datetime | None = None,
        resolution: DataInterval | str | None = None,
        dataset_id: str = "",
        dataset_version: str = "",
        bars: Sequence[Any] | None = None,
        config: LSTMConfig | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        chosen_horizon = _horizon(horizon)
        try:
            series, chosen_resolution, _ds, _dv = self._series_for(
                instrument_id,
                chosen_horizon,
                as_of=_aware(as_of or self._clock(), "as_of"),
                resolution=resolution,
                dataset_id=dataset_id,
                dataset_version=dataset_version,
                bars=bars,
            )
            chosen_config = (
                config
                if isinstance(config, LSTMConfig)
                else LSTMConfig.from_dict(config)
                if config is not None
                else self._default_lstm_config
            )
            reasons: list[str] = []
            if not HORIZON_SPECS[chosen_horizon].accepts_resolution(chosen_resolution):
                reasons.append(
                    f"{chosen_horizon.value} cannot use {chosen_resolution.value} data"
                )
            sequence_length = chosen_config.sequence_length_for(
                chosen_horizon, chosen_resolution
            )
            required = max(
                chosen_config.min_train_samples + sequence_length,
                HORIZON_SPECS[chosen_horizon].min_history_bars,
            )
            if len(series) < required:
                reasons.append(
                    f"{len(series)} observations are below the {required} required "
                    f"for a {sequence_length}-sequence LSTM"
                )
            latest = [
                run
                for run in self._forecast_store.training_runs(limit=100)
                if str(run.get("kind")) == "lstm"
                and str(run.get("instrument_id")) == instrument_id
                and str(run.get("horizon")) == chosen_horizon.value
            ]
            latest_body = (latest[0].get("report") or {}) if latest else {}
            latest_suitability = (latest_body.get("suitability") or {}).get(_MODEL_ID, {})
            return {
                "ok": True,
                "instrument_id": instrument_id,
                "horizon": chosen_horizon.value,
                "resolution": chosen_resolution.value,
                "horizon_steps": horizon_steps(chosen_horizon, chosen_resolution),
                "observations": len(series),
                "sequence_length": sequence_length,
                "eligible": not reasons,
                "reasons": reasons,
                "latest_evaluation": latest_suitability,
                "latest_run_id": latest[0].get("run_id") if latest else None,
                "available_backends": {
                    "lstm": {
                        "available": backend_available(),
                        "version": backend_version(),
                        "device": "cpu",
                    }
                },
                "automatic_promotion": False,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "instrument_id": instrument_id,
                "horizon": str(horizon),
                "eligible": False,
                "sequence_length": 0,
                "reasons": [f"{type(exc).__name__}: {exc}"],
                "automatic_promotion": False,
            }

    def explain(self, forecast_id: str) -> dict[str, Any]:
        """Return global validation importance and the persisted input row.

        A per-forecast sequence attribution is not claimed: an LSTM has no
        tree-path decomposition, and presenting one as if it did would be a
        false explanation.  The model-level occlusion ranking is honest and
        already persisted with the artifact.
        """
        contract = self._prediction_store.get_forecast(forecast_id)
        if contract is None or not contract.forecast_id.startswith(
            LSTM_FORECAST_PREFIX
        ):
            raise ForecastNotFoundError(f"unknown LSTM forecast {forecast_id!r}")
        metadata = contract.metadata or {}
        schema = metadata.get("feature_schema")
        feature_values = metadata.get("feature_values")
        if not isinstance(schema, Mapping) or not isinstance(feature_values, Mapping):
            raise LSTMUnavailableError(
                "forecast has no persisted LSTM sequence provenance"
            )
        return {
            "ok": True,
            "forecast_id": forecast_id,
            "model_id": _MODEL_ID,
            "model_version": contract.model_version,
            "method": "validation median occlusion; global, not a local Shapley claim",
            "feature_importance": [
                dict(item) for item in (metadata.get("feature_importance") or ())
            ],
            "feature_values": dict(feature_values),
            "sequence_length": metadata.get("sequence_length"),
            "probabilities_withheld": True,
            "automatic_promotion": False,
        }

    def status(self) -> dict[str, Any]:
        base = StatisticalForecastingService.status(self)
        base.update(
            {
                "phase": PI07_PHASE,
                "contract_version": LSTM_CONTRACT_VERSION,
                "schema_version": LSTM_SCHEMA_VERSION,
                "methodology_version": LSTM_METHODOLOGY_VERSION,
                "models": [_MODEL_ID],
                "backends": {
                    _MODEL_ID: {
                        "available": backend_available(),
                        "version": backend_version(),
                        "device": "cpu",
                    }
                },
                "hardware": hardware_status(),
                "artifact_root": str(self._lstm_artifacts.root),
                "automatic_promotion": False,
                "production_ready": False,
                "risk_authority": "NM-08",
                "execution_authority": "NM-10",
            }
        )
        return base

    def contract(self) -> dict[str, Any]:
        return {
            "phase": PI07_PHASE,
            "implemented": True,
            "contract_version": LSTM_CONTRACT_VERSION,
            "schema_version": LSTM_SCHEMA_VERSION,
            "methodology_version": LSTM_METHODOLOGY_VERSION,
            "families": {
                _MODEL_ID: {
                    "available": backend_available(),
                    "dependency_version": backend_version(),
                    "framework": "PyTorch",
                    "device": "cpu",
                    "regression": True,
                    "classification": True,
                    "serialization": "native torch state dict + JSON preprocessing metadata",
                }
            },
            "horizons": {
                item.value: {
                    "family": HORIZON_SPECS[item].family.value,
                    "max_input_interval": HORIZON_SPECS[item].max_input_interval.value,
                    "min_history_bars": HORIZON_SPECS[item].min_history_bars,
                    "default_sequence_length": self._default_lstm_config.sequence_length_for(
                        item, default_resolution(item)
                    ),
                }
                for item in ForecastHorizon
            },
            "targets": {
                "return": "future_close / decision_close - 1",
                "price": "decision_close * (1 + predicted_return)",
                "direction": (
                    "up/down only outside max(neutral_band_pct, round_trip_cost_bps/100); "
                    "neutral inside; uncertain when a supported interval crosses zero"
                ),
                "uncertainty": "validation-residual interval quantiles",
                "probability": "uncalibrated classifier probabilities withheld from the contract",
            },
            "features": {
                "source": "PI-04 FeatureEngine",
                "sequence": "chronological ordered feature windows; no future rows",
                "missing_values": "explicit mask; missing is not silently zero-filled",
                "preprocessing": "training-only robust median/IQR scaling; target mean/std",
            },
            "leakage": {
                "chronological": True,
                "purged_boundaries": "inherited from PI-02 build_training_dataset",
                "test_used_for_selection": False,
                "preprocessing_fit_on_train_only": True,
                "sequence_future_check": True,
            },
            "authorities": {
                "risk": "NM-08",
                "execution": "NM-10",
                "market_data": "MarketDataEngine/MarketDataStore",
                "datasets": "PI-02 HistoricalDatasetService",
                "features": "PI-04 FeatureService/FeatureEngine",
                "predictions": "PredictionStore.prediction_forecasts",
                "models": "PredictionStore.prediction_models",
            },
            "guarantees": [
                "sequence windows are chronological and cannot contain future observations",
                "preprocessing is fitted on training windows only and persisted with the model",
                "validation selects candidates; test is reported once",
                "one failed LSTM training does not stop PI-05 or PI-06",
                "model identity is preserved in every forecast",
                "no automatic promotion, risk decision or execution",
            ],
        }


__all__ = ["LSTMForecastingService"]
