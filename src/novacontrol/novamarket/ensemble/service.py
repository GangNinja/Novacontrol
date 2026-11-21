"""PI-08 ensemble forecasting service.

The service composes the existing PI-05/PI-06/PI-07 inference methods.  It does
not fit or replace their models, does not write to their registries except for
the PI-08 ensemble's own metadata, and delegates the final PI-01 forecast shape
to ``EnsembleAssembler``.  Eligibility, weights, disagreement and conformal
calibration are recorded on the forecast for later inspection.
"""

from __future__ import annotations

import math
from dataclasses import fields, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from novacontrol.novamarket.contracts import ForecastNotFoundError, ForecastValidationError
from novacontrol.novamarket.forecasting.store import ForecastStore
from novacontrol.novamarket.marketdata.models import DataInterval, DataQuality, Freshness
from novacontrol.novamarket.prediction.ensemble import EnsembleAssembler
from novacontrol.novamarket.prediction.models import (
    CalibrationStatus,
    ConfirmationStage,
    Direction,
    EnsembleResult,
    ForecastHorizon,
    ForecastOutput,
    ForecastValidity,
    HORIZON_SPECS,
    MarketDataSnapshot,
    ModelFamily,
    ModelMetadata,
    ModelPrediction,
    ModelState,
    PredictionPolicy,
    PredictionRequest,
    PredictionUncertainty,
    ProbabilityStatus,
    digest,
    direction_for_return,
    prediction_id,
    uncertainty_for_width,
)
from novacontrol.novamarket.prediction.store import PredictionStore
from novacontrol.novamarket.statistical.metrics import StatisticalMetrics

from .calibration import (
    coverage_by_regime,
    fit_calibration,
    metrics_for,
)
from .models import (
    ENSEMBLE_CALIBRATION_VERSION,
    ENSEMBLE_CONFIG_PREFIX,
    ENSEMBLE_CONTRACT_VERSION,
    ENSEMBLE_EVALUATION_PREFIX,
    ENSEMBLE_FALLBACK_PREFIX,
    ENSEMBLE_FORECAST_PREFIX,
    ENSEMBLE_METHODOLOGY_VERSION,
    ENSEMBLE_MODEL_VERSION,
    ENSEMBLE_PHASE,
    ENSEMBLE_SCHEMA_VERSION,
    SUPPORTED_FAMILIES,
    ConformalCalibration,
    EligibilityDecision,
    EnsembleConfig,
    EnsembleEvaluation,
    EnsembleObservation,
    EnsembleUnavailableError,
    EnsembleValidationError,
    ModelEvidence,
    WeightResult,
    ensemble_id,
)
from .weighting import assess_evidence

_MAX_TRAINING_RUNS = 200
_VALIDITY_VALUES = {ForecastValidity.VALID}
_INTERVAL_SECONDS = {
    DataInterval.SPOT: 0.0,
    DataInterval.MINUTE_1: 60.0,
    DataInterval.MINUTE_5: 300.0,
    DataInterval.MINUTE_15: 900.0,
    DataInterval.MINUTE_30: 1800.0,
    DataInterval.HOUR_1: 3600.0,
    DataInterval.DAY_1: 86400.0,
    DataInterval.WEEK_1: 604800.0,
    DataInterval.MONTH_1: 2592000.0,
}
_FORECAST_FIELDS = frozenset(item.name for item in fields(ForecastOutput))
_PUBLIC_FORECAST_NAMES = {
    "reference_at": "reference_price_timestamp",
    "direction": "predicted_direction",
    "uncertainty": "forecast_uncertainty",
    "target_at": "forecast_target_timestamp",
    "generated_at": "forecast_generation_timestamp",
    "data_observed_at": "underlying_data_timestamp",
    "data_received_at": "data_received_timestamp",
    "data_freshness": "data_freshness_status",
    "validity": "forecast_validity",
    "validity_conditions": "forecast_validity_conditions",
    "disagreement": "ensemble_disagreement",
}


def _horizon(value: ForecastHorizon | str) -> ForecastHorizon:
    try:
        return ForecastHorizon(value)
    except ValueError as exc:
        known = ", ".join(item.value for item in ForecastHorizon)
        raise EnsembleValidationError(
            f"unknown forecast horizon {value!r}; known: {known}"
        ) from exc


def _aware(value: object, name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EnsembleValidationError(f"{name} is not an ISO timestamp") from exc
    if not isinstance(value, datetime):
        raise EnsembleValidationError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise EnsembleValidationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _number(value: object) -> float | None:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _metric(metrics: object, name: str) -> float | None:
    payload = _mapping(metrics)
    return _number(payload.get(name))


def _median(values: Iterable[float]) -> float | None:
    usable = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not usable:
        return None
    middle = len(usable) // 2
    if len(usable) % 2:
        return usable[middle]
    return (usable[middle - 1] + usable[middle]) / 2.0


def _weight_key(model_id: str, version: str) -> str:
    return f"{model_id}@{version}"


def _worst_enum(values: Sequence[Any], order: Sequence[Any], default: Any) -> Any:
    if not values:
        return default
    return max(
        values,
        key=lambda item: order.index(item) if item in order else -1,
    )


def _quality_rank(value: DataQuality) -> int:
    return {
        DataQuality.VALID: 0,
        DataQuality.SUSPECT: 1,
        DataQuality.STALE: 2,
        DataQuality.INVALID: 3,
        DataQuality.UNAVAILABLE: 4,
    }.get(value, 4)


def _freshness_rank(value: Freshness) -> int:
    return {
        Freshness.FRESH: 0,
        Freshness.DELAYED: 1,
        Freshness.CLOSED_MARKET: 1,
        Freshness.UNKNOWN: 2,
        Freshness.STALE: 3,
        Freshness.MISSING: 4,
        Freshness.PROVIDER_UNAVAILABLE: 5,
    }.get(value, 5)


def _point_in_time_data(
    output: ForecastOutput,
    *,
    cutoff: datetime,
    horizon: ForecastHorizon,
) -> tuple[DataQuality, Freshness, bool]:
    """Judge freshness relative to the forecast cutoff, not wall-clock now.

    Historical point-in-time evaluation stores the ingestion-time freshness of
    a bar.  A bar observed at the cutoff is on time for that forecast even when
    its stored label says the row was ingested later.  A genuinely old bar is
    still rejected because its age exceeds the horizon's input interval.
    """
    quality = output.data_quality
    if quality in {DataQuality.INVALID, DataQuality.UNAVAILABLE}:
        return quality, output.data_freshness, False
    if output.data_freshness in {
        Freshness.MISSING,
        Freshness.PROVIDER_UNAVAILABLE,
    }:
        return quality, output.data_freshness, False
    age = (cutoff - output.data_observed_at).total_seconds()
    max_age = _INTERVAL_SECONDS[
        HORIZON_SPECS[horizon].max_input_interval
    ]
    # Reject a bar that arrives after the forecast cutoff as well as one that
    # is too old: either would break the point-in-time boundary.
    current = -1.0 <= age <= max_age + 1.0
    if not current:
        return quality, Freshness.STALE, False
    return DataQuality.VALID, Freshness.FRESH, True


class EnsembleForecastingService:
    """Evidence-weighted, calibrated ensemble over the three model families."""

    def __init__(
        self,
        *,
        statistical_service: Any | None = None,
        gradient_service: Any | None = None,
        lstm_service: Any | None = None,
        prediction_store: PredictionStore | None = None,
        forecast_store: ForecastStore | None = None,
        publisher: Any | None = None,
        clock: Any | None = None,
        default_config: EnsembleConfig | Mapping[str, Any] | None = None,
    ) -> None:
        self._statistical = statistical_service
        self._gradient = gradient_service
        self._lstm = lstm_service
        self._prediction_store = prediction_store
        self._forecast_store = forecast_store
        self._publisher = publisher
        self._clock = clock or (lambda: datetime.now(UTC))
        self._default_config = (
            default_config
            if isinstance(default_config, EnsembleConfig)
            else EnsembleConfig.from_dict(default_config)
        )
        self._generations = 0
        self._evaluations = 0
        self._failures = 0
        self._last_error = ""
        self._failure_reports: list[dict[str, str]] = []

    # -- read-only wiring ----------------------------------------------------
    @property
    def config(self) -> EnsembleConfig:
        return self._default_config

    @property
    def failure_reports(self) -> tuple[dict[str, str], ...]:
        return tuple(self._failure_reports)

    def _store(self) -> PredictionStore:
        if self._prediction_store is None:
            raise EnsembleUnavailableError("the PI-01 prediction store is not attached")
        return self._prediction_store

    def _forecasts(self) -> ForecastStore:
        if self._forecast_store is None:
            raise EnsembleUnavailableError("the NM-06 forecast store is not attached")
        return self._forecast_store

    def _record_failure(self, stage: str, exc: Exception) -> None:
        self._failures += 1
        text = f"{stage}: {type(exc).__name__}: {exc}"
        self._last_error = text
        self._failure_reports.append(
            {"model_id": "ensemble", "stage": stage, "error": f"{type(exc).__name__}: {exc}"}
        )

    async def _publish(self, name: str, **payload: Any) -> None:
        if self._publisher is None:
            return
        try:
            outcome = self._publisher(name, payload)
            if hasattr(outcome, "__await__"):
                await outcome
        except Exception as exc:  # noqa: BLE001 - a stored result stands
            self._record_failure("publish", exc)

    # -- evidence ------------------------------------------------------------
    def _training_runs(self) -> list[dict[str, Any]]:
        try:
            return list(self._forecasts().training_runs(limit=_MAX_TRAINING_RUNS))
        except Exception as exc:  # noqa: BLE001
            self._record_failure("read_training_runs", exc)
            return []

    def _run_for(
        self,
        *,
        instrument_id: str,
        horizon: ForecastHorizon,
        model_id: str,
        family: ModelFamily,
        version: str,
        runs: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        matches: list[Mapping[str, Any]] = []
        for run in runs:
            if str(run.get("instrument_id") or "") != instrument_id:
                continue
            if str(run.get("horizon") or "") != horizon.value:
                continue
            if str(run.get("kind") or "") not in {
                "statistical",
                "gradient",
                "lstm",
            }:
                continue
            report = run.get("report")
            if not isinstance(report, Mapping):
                continue
            if family in {
                ModelFamily.XGBOOST,
                ModelFamily.LIGHTGBM,
                ModelFamily.LSTM,
            }:
                families = report.get("families")
                if not isinstance(families, Mapping):
                    continue
                entry = families.get(model_id) or families.get(family.value)
                if not isinstance(entry, Mapping):
                    continue
                entry_version = str(entry.get("version") or "")
                if version and entry_version and entry_version != version:
                    continue
            else:
                models = report.get("models")
                if not isinstance(models, Mapping):
                    continue
                entry = models.get(model_id)
                if not isinstance(entry, Mapping):
                    continue
            matches.append(run)
        if not matches:
            return {}
        # Runs that share a stop cutoff share one finished_at, so ordering on
        # that column alone left the winner to the store's row order: an older
        # three-sample run could beat a newer five-sample one.  started_at is
        # the training window's first bar, not the run time, so it cannot break
        # the tie; recorded_at (the write timestamp) is the newest-run signal,
        # with run_id keeping the pick deterministic when that is also equal.
        return sorted(
            matches,
            key=lambda item: (
                str(item.get("finished_at") or ""),
                str(item.get("recorded_at") or ""),
                str(item.get("run_id") or ""),
            ),
            reverse=True,
        )[0]

    def _evidence_for(
        self,
        metadata: ModelMetadata,
        *,
        instrument_id: str,
        horizon: ForecastHorizon,
        runs: Sequence[Mapping[str, Any]],
        regime: str = "",
    ) -> ModelEvidence:
        run = self._run_for(
            instrument_id=instrument_id,
            horizon=horizon,
            model_id=metadata.model_id,
            family=metadata.family,
            version=metadata.version,
            runs=runs,
        )
        report = _mapping(run.get("report"))
        entry: Mapping[str, Any] = {}
        if report:
            families = report.get("families")
            models = report.get("models")
            if isinstance(families, Mapping):
                raw = families.get(metadata.model_id) or families.get(
                    metadata.family.value
                )
                entry = _mapping(raw)
            elif isinstance(models, Mapping):
                entry = _mapping(models.get(metadata.model_id))
        metrics = _mapping(metadata.metrics)
        validation = _mapping(entry.get("validation"))
        if not validation:
            validation = _mapping(metrics.get("validation"))
        test = _mapping(entry.get("test"))
        if not test:
            test = _mapping(metrics.get("test"))
        walk_forward = entry.get("walk_forward") or metrics.get("walk_forward") or ()
        folds = [
            _mapping(item)
            for item in walk_forward
            if isinstance(item, Mapping) and str(item.get("status")) == "evaluated"
        ]
        fold_mae = [
            value
            for item in folds
            for value in [_metric(item.get("metrics"), "price_mae")]
            if value is not None
        ]
        fold_direction = [
            value
            for item in folds
            for value in [_metric(item.get("metrics"), "directional_accuracy")]
            if value is not None
        ]
        status = str(entry.get("status") or metrics.get("status") or "")
        regime_metrics = _mapping(metrics.get("regime_metrics"))
        regime_entry = _mapping(regime_metrics.get(str(regime or "").strip()))
        regime_mae = _metric(regime_entry, "price_mae")
        regime_accuracy = _metric(regime_entry, "directional_accuracy")
        regime_samples = int(
            _number(
                regime_entry.get("samples")
                or metrics.get("regime_samples")
                or 0
            )
            or 0
        )
        trained_at: datetime | None = None
        for candidate in (
            run.get("finished_at"),
            metadata.updated_at,
            metadata.trained_at,
        ):
            if candidate is None:
                continue
            try:
                trained_at = _aware(candidate, "trained_at")
                break
            except EnsembleValidationError:
                continue
        return ModelEvidence(
            model_id=metadata.model_id,
            version=metadata.version,
            family=metadata.family,
            state=metadata.state,
            horizon=horizon,
            instrument_id=instrument_id,
            validation_mae=_metric(validation, "price_mae"),
            validation_direction_accuracy=_metric(
                validation, "directional_accuracy"
            ),
            validation_samples=int(_number(validation.get("samples")) or 0),
            walk_forward_mae=_median(fold_mae),
            walk_forward_direction_accuracy=_median(fold_direction),
            regime_mae=regime_mae,
            regime_direction_accuracy=regime_accuracy,
            regime=str(regime or "").strip(),
            regime_samples=regime_samples,
            trained_at=trained_at,
            dataset_version=str(
                report.get("dataset_version")
                or metrics.get("dataset_version")
                or metadata.dataset_version
                or "unspecified"
            ),
            feature_set_version=str(
                metrics.get("feature_set_version")
                or metadata.feature_set_version
                or "unspecified"
            ),
            status=status,
            reasons=(str(entry.get("reason") or ""),)
            if entry.get("reason")
            else (),
        )

    def _metadata_for(
        self,
        instrument_id: str,
        horizon: ForecastHorizon,
        *,
        cutoff: datetime | None = None,
    ) -> list[ModelMetadata]:
        rows: list[ModelMetadata] = []
        try:
            models = self._store().models(horizon)
        except Exception as exc:  # noqa: BLE001
            self._record_failure("read_models", exc)
            return []
        seen: set[tuple[str, str]] = set()
        for metadata in models:
            if metadata.family not in SUPPORTED_FAMILIES:
                continue
            if metadata.model_id == "ensemble":
                continue
            if not metadata.supports(horizon):
                continue
            # Statistical baselines are refitted from the request's own bars
            # (the PI-05 service fits on demand), so their registry timestamp
            # records the latest fit and is not a train/serve leakage boundary.
            # Persistent gradient/LSTM artifacts are different: an artifact
            # fitted after the cutoff must never contribute to that forecast.
            if (
                cutoff is not None
                and metadata.trained_at is not None
                and metadata.family
                in {ModelFamily.XGBOOST, ModelFamily.LIGHTGBM, ModelFamily.LSTM}
            ):
                trained_at = metadata.trained_at
                if trained_at.tzinfo is None or trained_at.utcoffset() is None:
                    trained_at = trained_at.replace(tzinfo=UTC)
                if trained_at > cutoff:
                    # A model fitted after the forecast cutoff would leak future
                    # bars into the ensemble, even if its metrics look good.
                    continue
            metrics = _mapping(metadata.metrics)
            instruments = {
                str(item) for item in (metrics.get("instruments") or ()) if str(item)
            }
            exact = str(metrics.get("instrument_id") or "")
            if instrument_id not in instruments and exact != instrument_id:
                continue
            key = (metadata.model_id, metadata.version)
            if key in seen:
                continue
            seen.add(key)
            rows.append(metadata)
        return rows

    def _base_reasons(
        self,
        metadata: ModelMetadata,
        *,
        horizon: ForecastHorizon,
        config: EnsembleConfig,
    ) -> list[str]:
        metrics = _mapping(metadata.metrics)
        reasons: list[str] = []
        if metadata.state in {ModelState.SUSPENDED, ModelState.UNAVAILABLE, ModelState.RETIRED}:
            reasons.append(f"model state is {metadata.state.value}")
        if metadata.state is ModelState.DEGRADED and not config.allow_degraded:
            reasons.append("degraded models are disabled by configuration")
        if not metadata.supports(horizon):
            reasons.append("model does not support this horizon")
        if str(metrics.get("horizon") or "") and str(metrics.get("horizon")) != horizon.value:
            reasons.append("registry horizon does not match the request")
        if metadata.family in {
            ModelFamily.XGBOOST,
            ModelFamily.LIGHTGBM,
            ModelFamily.LSTM,
        } and not isinstance(metrics.get("artifact"), Mapping):
            reasons.append("model has no readable artifact")
        if metadata.family is ModelFamily.LSTM and not isinstance(
            metrics.get("scaler"), Mapping
        ):
            reasons.append("LSTM model has no persisted preprocessing")
        return reasons

    def _eligibility(
        self,
        *,
        instrument_id: str,
        horizon: ForecastHorizon,
        config: EnsembleConfig,
        cutoff: datetime | None = None,
        regime: str = "",
    ) -> tuple[WeightResult, dict[str, ModelEvidence], list[ModelMetadata]]:
        runs = self._training_runs()
        metadata_rows = self._metadata_for(
            instrument_id, horizon, cutoff=cutoff
        )
        evidence: list[ModelEvidence] = []
        base: dict[str, list[str]] = {}
        by_key: dict[str, ModelEvidence] = {}
        for metadata in metadata_rows:
            item = self._evidence_for(
                metadata,
                instrument_id=instrument_id,
                horizon=horizon,
                runs=runs,
                regime=regime,
            )
            key = _weight_key(item.model_id, item.version)
            evidence.append(item)
            by_key[key] = item
            base[key] = self._base_reasons(
                metadata, horizon=horizon, config=config
            )
        previous = self._latest_config(instrument_id, horizon, cutoff)
        previous_payload = _mapping(previous.get("payload"))
        previous_weights = _mapping(previous.get("weights")) or _mapping(
            previous_payload.get("weights")
        )
        previous_id = str(
            previous.get("config_id") or previous_payload.get("config_id") or ""
        )
        weighted = assess_evidence(
            evidence,
            config,
            previous_weights=previous_weights,
            previous_config_id=previous_id,
            now=_aware(self._clock(), "clock"),
            regime=regime,
            base_reasons=base,
        )
        return weighted, by_key, metadata_rows

    # -- generation ----------------------------------------------------------
    def _service_for(self, family: ModelFamily) -> Any:
        if family in {
            ModelFamily.NAIVE,
            ModelFamily.MOVING_AVERAGE,
            ModelFamily.EXPONENTIAL_SMOOTHING,
            ModelFamily.ARIMA,
        }:
            return self._statistical
        if family in {ModelFamily.XGBOOST, ModelFamily.LIGHTGBM}:
            return self._gradient
        if family is ModelFamily.LSTM:
            return self._lstm
        return None

    async def _generate_one(
        self,
        metadata: ModelMetadata,
        *,
        instrument_id: str,
        horizon: ForecastHorizon,
        cutoff: datetime,
        resolution: DataInterval | str | None,
        dataset_id: str,
        dataset_version: str,
        bars: Sequence[Any] | None,
        exchange: str,
        currency: str,
        fundamentals: Sequence[Any],
        news_events: Sequence[Any],
        benchmark_bars: Mapping[str, Sequence[Any]] | None = None,
        sector_bars: Mapping[str, Sequence[Any]] | None = None,
        context_bars: Mapping[str, Mapping[str, Sequence[Any]]] | None = None,
    ) -> ForecastOutput:
        service = self._service_for(metadata.family)
        if service is None:
            raise EnsembleUnavailableError(
                f"the {metadata.family.value} family service is not attached"
            )
        metrics = _mapping(metadata.metrics)
        kwargs: dict[str, Any] = {
            "as_of": cutoff,
            "resolution": resolution,
            "dataset_id": dataset_id,
            "dataset_version": dataset_version,
            "bars": bars,
            "exchange": exchange,
            "currency": currency,
        }
        if metadata.family in {
            ModelFamily.NAIVE,
            ModelFamily.MOVING_AVERAGE,
            ModelFamily.EXPONENTIAL_SMOOTHING,
            ModelFamily.ARIMA,
        }:
            # PI-05 uses one metadata version across all horizons, so the
            # registry metrics can describe the most recently evaluated
            # horizon.  Read the horizon's own run instead of reusing it.
            run = self._run_for(
                instrument_id=instrument_id,
                horizon=horizon,
                model_id=metadata.model_id,
                family=metadata.family,
                version=metadata.version,
                runs=self._training_runs(),
            )
            report = _mapping(run.get("report"))
            entry = _mapping(_mapping(report.get("models")).get(metadata.model_id))
            run_metrics = {**metrics, **entry}
            body = await service.generate(
                instrument_id,
                horizon,
                model_id=metadata.model_id,
                config=(
                    run_metrics.get("config")
                    if isinstance(run_metrics.get("config"), Mapping)
                    else None
                ),
                feature_set_version=str(
                    run_metrics.get("feature_set_version")
                    or metrics.get("feature_set_version")
                    or "unspecified"
                ),
                **kwargs,
            )
        elif metadata.family in {ModelFamily.XGBOOST, ModelFamily.LIGHTGBM}:
            body = await service.generate(
                instrument_id,
                horizon,
                model_id=metadata.family.value,
                version=metadata.version,
                fundamentals=fundamentals,
                news_events=news_events,
                benchmark_bars=benchmark_bars,
                sector_bars=sector_bars,
                context_bars=context_bars,
                **kwargs,
            )
        else:
            body = await service.generate(
                instrument_id,
                horizon,
                model_id=metadata.model_id,
                version=metadata.version,
                fundamentals=fundamentals,
                news_events=news_events,
                benchmark_bars=benchmark_bars,
                sector_bars=sector_bars,
                context_bars=context_bars,
                **kwargs,
            )
        forecast_id = str(body.get("forecast_id") or "")
        if not forecast_id:
            raise EnsembleUnavailableError(
                f"{metadata.model_id}@{metadata.version} returned no forecast id"
            )
        contract = self._store().get_forecast(forecast_id)
        if contract is None:
            # PI-05/PI-06 share a deterministic lifecycle id per horizon, and
            # PI-01's immutable table permits one initial-stage row per
            # lifecycle.  Their generate() still returns a fully validated
            # contract; use it as an ephemeral constituent while the ensemble
            # itself is persisted below.  This is not a failed inference and
            # must not be reported as one.
            try:
                payload = dict(body)
                for field_name, public_name in _PUBLIC_FORECAST_NAMES.items():
                    if field_name not in payload and public_name in payload:
                        payload[field_name] = payload[public_name]
                contract = ForecastOutput(
                    **{
                        key: value
                        for key, value in payload.items()
                        if key in _FORECAST_FIELDS
                    }
                )
            except Exception as exc:  # noqa: BLE001
                raise EnsembleUnavailableError(
                    f"{metadata.model_id}@{metadata.version} returned an invalid "
                    f"contract and did not persist it: {exc}"
                ) from exc
        if contract.instrument_id != instrument_id:
            raise EnsembleValidationError("constituent instrument does not match")
        if contract.forecast_horizon is not horizon:
            raise EnsembleValidationError("constituent horizon does not match")
        quality, freshness, current = _point_in_time_data(
            contract, cutoff=cutoff, horizon=horizon
        )
        if contract.validity is ForecastValidity.INVALID:
            raise EnsembleValidationError("constituent validity is invalid")
        if contract.validity not in _VALIDITY_VALUES and not current:
            raise EnsembleValidationError(
                f"constituent validity is {contract.validity.value} and its input "
                "is not current for the cutoff"
            )
        if not current:
            raise EnsembleValidationError(
                f"constituent input is not current for the cutoff "
                f"({contract.data_observed_at.isoformat()})"
            )
        if freshness in {Freshness.MISSING, Freshness.PROVIDER_UNAVAILABLE} or quality in {
            DataQuality.INVALID,
            DataQuality.UNAVAILABLE,
        }:
            raise EnsembleValidationError(
                f"constituent data is {quality.value}/{freshness.value}"
            )
        return contract

    def _snapshot(
        self,
        outputs: Sequence[ForecastOutput],
        *,
        instrument_id: str,
        horizon: ForecastHorizon,
        cutoff: datetime,
        history_bars: int,
        regime: str,
    ) -> MarketDataSnapshot:
        first = outputs[0]
        point_in_time = [
            _point_in_time_data(item, cutoff=cutoff, horizon=horizon)
            for item in outputs
        ]
        qualities = [item[0] for item in point_in_time]
        freshnesses = [item[1] for item in point_in_time]
        quality = max(qualities, key=_quality_rank)
        freshness = max(freshnesses, key=_freshness_rank)
        reference = first.reference_price
        snapshot_id = prediction_id(
            "ens_snap_",
            instrument_id,
            horizon.value,
            cutoff.isoformat(),
            format(reference, "f"),
            first.data_observed_at.isoformat(),
        )
        usable = (
            quality not in {DataQuality.INVALID, DataQuality.UNAVAILABLE}
            and freshness
            not in {Freshness.MISSING, Freshness.PROVIDER_UNAVAILABLE, Freshness.STALE}
        )
        return MarketDataSnapshot(
            snapshot_id=snapshot_id,
            instrument_id=instrument_id,
            exchange=first.exchange,
            provider="ensemble-constituents",
            source_observed_at=max(item.data_observed_at for item in outputs),
            received_at=cutoff,
            resolution=first.data_resolution,
            reference_price=reference,
            currency=first.currency,
            quality=quality,
            freshness=freshness,
            usable=usable,
            provider_conflict=False,
            missing_observation=False,
            rate_limited=False,
            provider_health="constituents_available",
            history_bars=max(0, int(history_bars)),
            history_sufficient=history_bars
            >= HORIZON_SPECS[horizon].min_history_bars,
            reasons=() if usable else ("constituent data quality/freshness is not usable",),
            provenance={
                "horizon": horizon.value,
                "regime": regime,
                "constituents": [
                    {
                        "model_id": item.model_id,
                        "version": item.model_version,
                        "forecast_id": item.forecast_id,
                    }
                    for item in outputs
                ],
            },
        )

    def _latest_calibration(
        self,
        instrument_id: str,
        horizon: ForecastHorizon,
        cutoff: datetime | None = None,
    ) -> ConformalCalibration | None:
        try:
            payload = self._store().latest_ensemble_calibration(
                instrument_id, horizon.value, before=cutoff
            )
        except Exception as exc:  # noqa: BLE001
            self._record_failure("read_calibration", exc)
            return None
        if not payload:
            return None
        try:
            return ConformalCalibration.from_dict(payload)
        except (KeyError, TypeError, ValueError, EnsembleValidationError) as exc:
            self._record_failure("decode_calibration", exc)
            return None

    def _apply_calibration(
        self,
        output: ForecastOutput,
        calibration: ConformalCalibration | None,
        *,
        config: EnsembleConfig,
    ) -> tuple[ForecastOutput, dict[str, Any]]:
        if calibration is None:
            detail = {
                "status": "uncalibrated",
                "method": "weighted_constituent_validation_intervals",
                "level": config.interval_level,
                "samples": 0,
                "guarantee": False,
                "note": (
                    "No validation-only conformal calibration is registered yet; "
                    "the interval is the weighted constituent interval."
                ),
            }
            return output, detail
        if not math.isclose(
            float(calibration.level), float(config.interval_level), rel_tol=1e-12
        ):
            # A registered quantile fitted for a different nominal level is not
            # an interval for this request.  Keep the constituent interval and
            # say why rather than mislabelling a 0.8 interval as 0.9.
            return output, {
                "status": "uncalibrated",
                "method": "weighted_constituent_validation_intervals",
                "level": config.interval_level,
                "registered_level": calibration.level,
                "samples": calibration.samples,
                "guarantee": False,
                "note": (
                    "The registered conformal calibration has a different "
                    "nominal level; it was not applied."
                ),
            }
        lower, upper = calibration.apply(output.predicted_return)
        reference = output.reference_price
        lower_price = reference * (Decimal("1") + Decimal(str(lower)))
        upper_price = reference * (Decimal("1") + Decimal(str(upper)))
        if lower_price <= 0 or upper_price <= 0:
            raise EnsembleValidationError("calibration produced a non-positive price")
        width = upper - lower
        if width <= 0 or not math.isfinite(width):
            raise EnsembleValidationError("calibration produced an invalid interval")
        if width > config.max_interval_width:
            raise EnsembleUnavailableError(
                f"calibrated interval width {width:.2%} exceeds "
                f"{config.max_interval_width:.0%}"
            )
        uncertainty = uncertainty_for_width(width, PredictionPolicy(
            uncertainty_low_pct=2.0,
            uncertainty_high_pct=8.0,
        ))
        updated = replace(
            output,
            lower_return=lower,
            upper_return=upper,
            lower_price=lower_price,
            upper_price=upper_price,
            uncertainty=uncertainty,
            uncertainty_width=width,
        )
        return updated, calibration.to_dict()

    def _config_id(
        self,
        instrument_id: str,
        horizon: ForecastHorizon,
        config: EnsembleConfig,
        cutoff: datetime,
    ) -> str:
        return ensemble_id(
            ENSEMBLE_CONFIG_PREFIX,
            instrument_id,
            horizon.value,
            config.digest,
            cutoff.isoformat(),
        )

    def _save_config(
        self,
        *,
        config_id: str,
        ensemble_ref: str,
        instrument_id: str,
        horizon: ForecastHorizon,
        config: EnsembleConfig,
        weights: Mapping[str, float],
        eligibility: Sequence[Mapping[str, Any]],
        cutoff: datetime,
        status: str,
        regime: str = "unclassified",
        calibration: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "config_id": config_id,
            "ensemble_id": ensemble_ref,
            "instrument_id": instrument_id,
            "horizon": horizon.value,
            "version": ENSEMBLE_MODEL_VERSION,
            "methodology_version": ENSEMBLE_METHODOLOGY_VERSION,
            "config": config.to_dict(),
            "config_digest": config.digest,
            "weights": dict(weights),
            "eligibility": [dict(item) for item in eligibility],
            "created_at": cutoff.isoformat(),
            "regime": str(regime or "unclassified"),
            "status": status,
            "calibration": dict(calibration or {}),
            "automatic_promotion": False,
        }
        try:
            self._store().save_ensemble_config(payload)
        except Exception as exc:  # noqa: BLE001 - the forecast remains usable
            self._record_failure("persist_config", exc)
        return payload

    def _abstained(
        self,
        *,
        instrument_id: str,
        horizon: ForecastHorizon,
        cutoff: datetime,
        config: EnsembleConfig,
        reasons: Sequence[str],
        weighted: WeightResult | None = None,
        ensemble_ref: str = "",
        failures: Sequence[str] = (),
        disagreement: float = 0.0,
        detail: Mapping[str, Any] | None = None,
        regime: str = "unclassified",
    ) -> dict[str, Any]:
        ref = ensemble_ref or ensemble_id(
            "ensb_",
            instrument_id,
            horizon.value,
            cutoff.isoformat(),
            config.digest,
        )
        eligibility = [item.to_dict() for item in weighted.decisions] if weighted else []
        weights = dict(weighted.weights) if weighted else {}
        config_id = self._config_id(instrument_id, horizon, config, cutoff)
        self._save_config(
            config_id=config_id,
            ensemble_ref=ref,
            instrument_id=instrument_id,
            horizon=horizon,
            config=config,
            weights=weights,
            eligibility=eligibility,
            cutoff=cutoff,
            status="abstained",
            regime=regime,
            calibration=detail,
        )
        result = EnsembleResult(
            ensemble_id=ref,
            version=ENSEMBLE_CONTRACT_VERSION,
            status="abstained",
            output=None,
            eligible_models=tuple(
                item.model_id for item in (weighted.decisions if weighted else ())
                if item.eligible
            ),
            excluded_models=tuple(
                item.to_dict() for item in (weighted.decisions if weighted else ())
                if not item.eligible
            ),
            weights=weights,
            disagreement=float(disagreement),
            reasons=tuple(dict.fromkeys(str(item) for item in reasons)),
        )
        try:
            self._store().save_ensemble(result)
        except Exception as exc:  # noqa: BLE001
            self._record_failure("persist_abstention", exc)
        self._generations += 1
        return {
            "ok": True,
            "status": "abstained",
            "engine": "ensemble",
            "ensemble_id": ref,
            "config_id": config_id,
            "regime": str(regime or "unclassified"),
            "forecast": None,
            "abstained": True,
            "reasons": list(result.reasons),
            "failures": list(failures),
            "weights": weights,
            "eligibility": eligibility,
            "disagreement": float(disagreement),
            "ensemble": {
                "phase": ENSEMBLE_PHASE,
                "contract_version": ENSEMBLE_CONTRACT_VERSION,
                "methodology_version": ENSEMBLE_METHODOLOGY_VERSION,
                "regime": str(regime or "unclassified"),
                "config": config.to_dict(),
                "config_id": config_id,
                "automatic_promotion": False,
                "production_ready": False,
            },
        }

    async def generate(
        self,
        instrument_id: str,
        horizon: ForecastHorizon | str,
        *,
        as_of: datetime | None = None,
        resolution: DataInterval | str | None = None,
        dataset_id: str = "",
        dataset_version: str = "",
        bars: Sequence[Any] | None = None,
        exchange: str = "",
        currency: str = "",
        fundamentals: Sequence[Any] = (),
        news_events: Sequence[Any] = (),
        regime: str = "",
        benchmark_bars: Mapping[str, Sequence[Any]] | None = None,
        sector_bars: Mapping[str, Sequence[Any]] | None = None,
        context_bars: Mapping[str, Mapping[str, Sequence[Any]]] | None = None,
        forecast_metadata: Mapping[str, Any] | None = None,
        config: EnsembleConfig | Mapping[str, Any] | None = None,
        model_id: str = "",
        version: str = "",
        **_ignored: Any,
    ) -> dict[str, Any]:
        chosen_horizon = _horizon(horizon)
        cutoff = _aware(as_of or self._clock(), "as_of")
        chosen_regime = str(regime or "").strip() or "unclassified"
        chosen_config = (
            config
            if isinstance(config, EnsembleConfig)
            else EnsembleConfig.from_dict(config)
            if config is not None
            else self._default_config
        )
        hint = str(model_id or "").strip().lower()
        if hint not in {"", "ensemble"}:
            raise EnsembleValidationError(
                f"the PI-08 engine does not accept model_id {model_id!r}"
            )
        if version:
            raise EnsembleValidationError(
                "the PI-08 ensemble version is derived from its configuration"
            )
        weighted, evidence_by_key, metadata_rows = self._eligibility(
            instrument_id=instrument_id,
            horizon=chosen_horizon,
            config=chosen_config,
            cutoff=cutoff,
            regime=chosen_regime,
        )
        if not metadata_rows:
            return self._abstained(
                instrument_id=instrument_id,
                horizon=chosen_horizon,
                cutoff=cutoff,
                config=chosen_config,
                reasons=("no registered model supports this instrument and horizon",),
                weighted=weighted,
                regime=chosen_regime,
            )
        eligible = [
            item for item in weighted.decisions if item.eligible and item.weight > 0.0
        ]
        if len(eligible) < chosen_config.min_models:
            if len(eligible) == 1:
                # A single constituent is a transparent fallback, not a
                # disguised ensemble prediction.
                only = eligible[0]
                metadata = next(
                    (
                        item
                        for item in metadata_rows
                        if item.model_id == only.model_id
                        and item.version == only.version
                    ),
                    None,
                )
                if metadata is not None:
                    try:
                        output = await self._generate_one(
                            metadata,
                            instrument_id=instrument_id,
                            horizon=chosen_horizon,
                            cutoff=cutoff,
                            resolution=resolution,
                            dataset_id=dataset_id,
                            dataset_version=dataset_version,
                            bars=bars,
                            exchange=exchange,
                            currency=currency,
                            fundamentals=fundamentals,
                            news_events=news_events,
                            benchmark_bars=benchmark_bars,
                            sector_bars=sector_bars,
                            context_bars=context_bars,
                        )
                    except Exception as exc:  # noqa: BLE001
                        self._record_failure("fallback_generate", exc)
                    else:
                        return self._fallback(
                            output,
                            instrument_id=instrument_id,
                            horizon=chosen_horizon,
                            cutoff=cutoff,
                            config=chosen_config,
                            weighted=weighted,
                            reason="not enough eligible models for an ensemble",
                            regime=chosen_regime,
                            forecast_metadata=forecast_metadata,
                        )
            return self._abstained(
                instrument_id=instrument_id,
                horizon=chosen_horizon,
                cutoff=cutoff,
                config=chosen_config,
                reasons=(
                    f"only {len(eligible)} eligible model(s); "
                    f"{chosen_config.min_models} required",
                ),
                weighted=weighted,
                regime=chosen_regime,
            )

        # Bound the number of contributions by the configured maximum, while
        # preserving the evidence-based ordering.
        eligible.sort(key=lambda item: (-item.weight, item.model_id, item.version))
        selected_decisions = eligible[: chosen_config.max_models]
        selected_keys = {
            _weight_key(item.model_id, item.version) for item in selected_decisions
        }
        selected_metadata = [
            metadata
            for metadata in metadata_rows
            if _weight_key(metadata.model_id, metadata.version) in selected_keys
        ]
        outputs: list[ForecastOutput] = []
        failures: list[str] = []
        output_by_key: dict[str, ForecastOutput] = {}
        for metadata in selected_metadata:
            try:
                output = await self._generate_one(
                    metadata,
                    instrument_id=instrument_id,
                    horizon=chosen_horizon,
                    cutoff=cutoff,
                    resolution=resolution,
                    dataset_id=dataset_id,
                    dataset_version=dataset_version,
                    bars=bars,
                    exchange=exchange,
                    currency=currency,
                    fundamentals=fundamentals,
                    news_events=news_events,
                    benchmark_bars=benchmark_bars,
                    sector_bars=sector_bars,
                    context_bars=context_bars,
                )
            except Exception as exc:  # noqa: BLE001 - isolate a constituent failure
                failures.append(
                    f"{metadata.model_id}@{metadata.version}: "
                    f"{type(exc).__name__}: {exc}"
                )
                self._record_failure(
                    f"generate/{metadata.model_id}@{metadata.version}", exc
                )
                continue
            outputs.append(output)
            output_by_key[_weight_key(metadata.model_id, metadata.version)] = output
        if not outputs:
            return self._abstained(
                instrument_id=instrument_id,
                horizon=chosen_horizon,
                cutoff=cutoff,
                config=chosen_config,
                reasons=("every eligible constituent failed to produce a forecast",),
                weighted=weighted,
                failures=failures,
                regime=chosen_regime,
            )

        reference = outputs[0].reference_price
        target = outputs[0].target_at
        compatible: list[ForecastOutput] = []
        for output in outputs:
            if output.reference_price != reference:
                failures.append(
                    f"{output.model_id}@{output.model_version}: reference price mismatch"
                )
                continue
            if output.target_at != target:
                failures.append(
                    f"{output.model_id}@{output.model_version}: target timestamp mismatch"
                )
                continue
            if output.exchange != outputs[0].exchange:
                failures.append(
                    f"{output.model_id}@{output.model_version}: exchange mismatch"
                )
                continue
            if output.currency != outputs[0].currency:
                failures.append(
                    f"{output.model_id}@{output.model_version}: currency mismatch"
                )
                continue
            if (
                outputs[0].feature_set_version
                and output.feature_set_version
                and output.feature_set_version != outputs[0].feature_set_version
            ):
                failures.append(
                    f"{output.model_id}@{output.model_version}: "
                    "feature-set version mismatch"
                )
                continue
            if (
                outputs[0].dataset_version
                and output.dataset_version
                and output.dataset_version != outputs[0].dataset_version
            ):
                failures.append(
                    f"{output.model_id}@{output.model_version}: "
                    "dataset version mismatch"
                )
                continue
            compatible.append(output)
        if len(compatible) < chosen_config.min_models:
            if len(compatible) == 1:
                return self._fallback(
                    compatible[0],
                    instrument_id=instrument_id,
                    horizon=chosen_horizon,
                    cutoff=cutoff,
                    config=chosen_config,
                    weighted=weighted,
                    reason="incompatible constituents left one usable baseline",
                    failures=failures,
                    regime=chosen_regime,
                )
            return self._abstained(
                instrument_id=instrument_id,
                horizon=chosen_horizon,
                cutoff=cutoff,
                config=chosen_config,
                reasons=("fewer compatible forecasts than the ensemble minimum",),
                weighted=weighted,
                failures=failures,
                regime=chosen_regime,
            )

        # Recompute weights only over contributions that actually survived.
        surviving_evidence = [
            evidence_by_key[_weight_key(output.model_id, output.model_version)]
            for output in compatible
            if _weight_key(output.model_id, output.model_version) in evidence_by_key
        ]
        if len(surviving_evidence) != len(compatible):
            return self._abstained(
                instrument_id=instrument_id,
                horizon=chosen_horizon,
                cutoff=cutoff,
                config=chosen_config,
                reasons=("a surviving forecast has no matching evidence record",),
                weighted=weighted,
                failures=failures,
            )
        reweighted = assess_evidence(
            surviving_evidence,
            chosen_config,
            previous_weights=weighted.weights,
            previous_config_id=weighted.previous_config_id,
            now=_aware(self._clock(), "clock"),
            regime=chosen_regime,
        )
        weights = {
            _weight_key(output.model_id, output.model_version): reweighted.weights[
                _weight_key(output.model_id, output.model_version)
            ]
            for output in compatible
        }
        metadata_by_key = {
            _weight_key(item.model_id, item.version): item for item in metadata_rows
        }
        model_metadata: list[ModelMetadata] = []
        predictions: list[ModelPrediction] = []
        snapshot = self._snapshot(
            compatible,
            instrument_id=instrument_id,
            horizon=chosen_horizon,
            cutoff=cutoff,
            history_bars=len(bars or ()),
            regime=chosen_regime,
        )
        for output in compatible:
            key = _weight_key(output.model_id, output.model_version)
            metadata = metadata_by_key.get(key)
            if metadata is None:
                return self._abstained(
                    instrument_id=instrument_id,
                    horizon=chosen_horizon,
                    cutoff=cutoff,
                    config=chosen_config,
                    reasons=(f"missing metadata for {key}",),
                    weighted=weighted,
                    failures=failures,
                    regime=chosen_regime,
                )
            model_metadata.append(replace(metadata, weight=weights[key]))
            predictions.append(
                ModelPrediction(
                    model_id=output.model_id,
                    model_version=output.model_version,
                    family=metadata.family,
                    instrument_id=instrument_id,
                    horizon=chosen_horizon,
                    generated_at=cutoff,
                    data_cutoff=cutoff,
                    data_snapshot_id=snapshot.snapshot_id,
                    reference_price=reference,
                    predicted_return=output.predicted_return,
                    lower_return=output.lower_return,
                    upper_return=output.upper_return,
                    p_up=output.p_up,
                    p_down=output.p_down,
                    p_neutral=output.p_neutral,
                    calibration=output.calibration_status,
                    valid=_point_in_time_data(
                        output, cutoff=cutoff, horizon=chosen_horizon
                    )[2]
                    and output.validity is not ForecastValidity.INVALID,
                    metadata={
                        "forecast_id": output.forecast_id,
                        "family": metadata.family.value,
                        "weight": weights[key],
                        "model_version": output.model_version,
                    },
                    feature_set_version=output.feature_set_version,
                    dataset_version=output.dataset_version,
                )
            )
        request = PredictionRequest(
            request_id=prediction_id(
                "ens_req_",
                instrument_id,
                chosen_horizon.value,
                cutoff.isoformat(),
                target.isoformat(),
            ),
            instrument_id=instrument_id,
            exchange=compatible[0].exchange,
            horizon=chosen_horizon,
            target_at=target,
            created_at=cutoff,
        )
        policy = PredictionPolicy(
            min_models=chosen_config.min_models,
            max_models=chosen_config.max_models,
            max_disagreement=chosen_config.max_disagreement,
            abstain_disagreement=chosen_config.abstain_disagreement,
            min_direction_probability=chosen_config.min_direction_probability,
            degraded_model_weight=1.0,
            uncertainty_low_pct=2.0,
            uncertainty_high_pct=8.0,
        )
        assembler = EnsembleAssembler(policy)
        assembled = assembler.assemble(
            request,
            snapshot,
            predictions,
            model_metadata,
            lifecycle_id=prediction_id(
                "ens_lifecycle_", instrument_id, chosen_horizon.value
            ),
            stage=ConfirmationStage.INITIAL,
            now=cutoff,
        )
        detail = self._disagreement_detail(compatible, weights)
        if assembled.output is None:
            return self._abstained(
                instrument_id=instrument_id,
                horizon=chosen_horizon,
                cutoff=cutoff,
                config=chosen_config,
                reasons=assembled.reasons,
                weighted=reweighted,
                ensemble_ref=assembled.ensemble_id,
                failures=failures,
                disagreement=assembled.disagreement,
                detail=detail,
                regime=chosen_regime,
            )
        calibration = self._latest_calibration(
            instrument_id, chosen_horizon, cutoff=cutoff
        )
        try:
            calibrated_output, calibration_detail = self._apply_calibration(
                assembled.output, calibration, config=chosen_config
            )
        except Exception as exc:  # noqa: BLE001 - an excessive interval abstains
            return self._abstained(
                instrument_id=instrument_id,
                horizon=chosen_horizon,
                cutoff=cutoff,
                config=chosen_config,
                reasons=(str(exc),),
                weighted=reweighted,
                ensemble_ref=assembled.ensemble_id,
                failures=failures,
                disagreement=assembled.disagreement,
                detail=detail,
                regime=chosen_regime,
            )
        config_id = self._config_id(
            instrument_id, chosen_horizon, chosen_config, cutoff
        )
        ensemble_ref = assembled.ensemble_id
        metadata_payload = {
            "phase": ENSEMBLE_PHASE,
            "ensemble_contract_version": ENSEMBLE_CONTRACT_VERSION,
            "schema_version": ENSEMBLE_SCHEMA_VERSION,
            "methodology_version": ENSEMBLE_METHODOLOGY_VERSION,
            "ensemble_version": ENSEMBLE_MODEL_VERSION,
            "regime": chosen_regime,
            "config": chosen_config.to_dict(),
            "config_id": config_id,
            "config_digest": chosen_config.digest,
            "weighting": reweighted.to_dict(),
            "eligibility": [item.to_dict() for item in weighted.decisions],
            "constituents": [
                {
                    "model_id": output.model_id,
                    "version": output.model_version,
                    "family": metadata_by_key[
                        _weight_key(output.model_id, output.model_version)
                    ].family.value,
                    "weight": weights[
                        _weight_key(output.model_id, output.model_version)
                    ],
                    "forecast_id": output.forecast_id,
                    "predicted_return": output.predicted_return,
                    "lower_return": output.lower_return,
                    "upper_return": output.upper_return,
                }
                for output in compatible
            ],
            "disagreement": detail,
            "calibration": calibration_detail,
            "fallback": False,
            "abstained": False,
            "automatic_promotion": False,
            "production_ready": False,
            "risk_authority": "NM-08",
            "execution_authority": "NM-10",
            "no_financial_authority": True,
        }
        enrichment = {
            "model_id": "ensemble",
            "model_version": ENSEMBLE_MODEL_VERSION,
            "forecast_id": prediction_id(
                ENSEMBLE_FORECAST_PREFIX,
                instrument_id,
                chosen_horizon.value,
                cutoff.isoformat(),
                ensemble_ref,
                config_id,
            ),
            "ensemble_id": ensemble_ref,
            "metadata": {
                **(calibrated_output.metadata or {}),
                **metadata_payload,
                **dict(forecast_metadata or {}),
            },
            "notes": tuple(calibrated_output.notes)
            + (
                "PI-08 evidence-weighted ensemble; not an order or a guarantee.",
                "Constituent test results are not used to choose weights.",
            ),
            "reasons": tuple(calibrated_output.reasons),
            "digest": digest(
                {
                    "ensemble_id": ensemble_ref,
                    "config_digest": chosen_config.digest,
                    "constituents": [item.forecast_id for item in compatible],
                    "predicted_return": calibrated_output.predicted_return,
                }
            ),
        }
        try:
            final_output = replace(
                calibrated_output,
                model_id="ensemble",
                model_version=ENSEMBLE_MODEL_VERSION,
                forecast_id=prediction_id(
                    ENSEMBLE_FORECAST_PREFIX,
                    instrument_id,
                    chosen_horizon.value,
                    cutoff.isoformat(),
                    ensemble_ref,
                    config_id,
                ),
                ensemble_id=ensemble_ref,
                metadata={
                    **(calibrated_output.metadata or {}),
                    **metadata_payload,
                    **dict(forecast_metadata or {}),
                },
                notes=tuple(calibrated_output.notes)
                + (
                    "PI-08 evidence-weighted ensemble; not an order or a guarantee.",
                    "Constituent test results are not used to choose weights.",
                ),
                reasons=tuple(calibrated_output.reasons),
                digest=digest(
                    {
                        "ensemble_id": ensemble_ref,
                        "config_digest": chosen_config.digest,
                        "constituents": [item.forecast_id for item in compatible],
                        "predicted_return": calibrated_output.predicted_return,
                    }
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return self._abstained(
                instrument_id=instrument_id,
                horizon=chosen_horizon,
                cutoff=cutoff,
                config=chosen_config,
                reasons=(f"the final ensemble contract is invalid: {exc}",),
                weighted=reweighted,
                ensemble_ref=ensemble_ref,
                failures=failures,
                disagreement=assembled.disagreement,
                detail=detail,
                regime=chosen_regime,
            )
        stored = False
        try:
            stored = self._store().save_forecast(final_output)
            if not stored and self._store().get_forecast(final_output.forecast_id) is None:
                raise EnsembleValidationError("the ensemble forecast was not persisted")
        except Exception as exc:  # noqa: BLE001
            self._record_failure("persist_forecast", exc)
        result = replace(
            assembled,
            output=final_output,
            weights=weights,
            eligible_models=tuple(
                _weight_key(output.model_id, output.model_version)
                for output in compatible
            ),
            excluded_models=tuple(
                item.to_dict() for item in weighted.decisions if not item.eligible
            ),
            reasons=tuple(dict.fromkeys((*assembled.reasons, *calibrated_output.reasons))),
        )
        try:
            self._store().save_ensemble(result)
        except Exception as exc:  # noqa: BLE001
            self._record_failure("persist_ensemble", exc)
        self._save_config(
            config_id=config_id,
            ensemble_ref=ensemble_ref,
            instrument_id=instrument_id,
            horizon=chosen_horizon,
            config=chosen_config,
            weights=weights,
            eligibility=[item.to_dict() for item in weighted.decisions],
            cutoff=cutoff,
            status="assembled",
            regime=chosen_regime,
            calibration=calibration_detail,
        )
        # Persist the ensemble model metadata through PI-01's existing registry.
        try:
            self._store().register_model(
                ModelMetadata(
                    model_id="ensemble",
                    version=ENSEMBLE_MODEL_VERSION,
                    family=ModelFamily.ENSEMBLE,
                    state=ModelState.DEGRADED,
                    supported_horizons=(chosen_horizon,),
                    calibration=CalibrationStatus.UNCALIBRATED,
                    metrics={
                        "phase": ENSEMBLE_PHASE,
                        "config_id": config_id,
                        "config_digest": chosen_config.digest,
                        "weights": dict(weights),
                        "eligibility": [item.to_dict() for item in weighted.decisions],
                        "disagreement": detail,
                        "calibration": calibration_detail,
                        "instrument_id": instrument_id,
                        "instruments": [instrument_id],
                        "experimental": True,
                        "production_ready": False,
                        "automatic_promotion": False,
                    },
                    trained_at=cutoff,
                    updated_at=cutoff,
                    notes=(
                        "PI-08 evidence-weighted ensemble; provisional and "
                        "experimental, never automatically promoted"
                    ),
                    feature_set_version=compatible[0].feature_set_version,
                    dataset_version=compatible[0].dataset_version,
                )
            )
        except Exception as exc:  # noqa: BLE001
            self._record_failure("register_ensemble_model", exc)
        self._generations += 1
        await self._publish(
            "novamarket.forecast.created",
            forecast_id=final_output.forecast_id,
            instrument_id=instrument_id,
            horizon=chosen_horizon.value,
            model_id="ensemble",
        )
        body = final_output.to_dict()
        body.update(
            {
                "ok": True,
                "status": "assembled",
                "engine": "ensemble",
                "config_id": config_id,
                "ensemble_id": ensemble_ref,
                "weighting": reweighted.to_dict(),
                "persisted": stored,
                "regime": chosen_regime,
                "ensemble": metadata_payload,
                "failures": failures,
            }
        )
        return body

    def _fallback(
        self,
        output: ForecastOutput,
        *,
        instrument_id: str,
        horizon: ForecastHorizon,
        cutoff: datetime,
        config: EnsembleConfig,
        weighted: WeightResult,
        reason: str,
        failures: Sequence[str] = (),
        regime: str = "unclassified",
        forecast_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        config_id = self._config_id(instrument_id, horizon, config, cutoff)
        fallback_ref = ensemble_id(
            ENSEMBLE_FALLBACK_PREFIX,
            instrument_id,
            horizon.value,
            cutoff.isoformat(),
            config.digest,
            output.forecast_id,
        )
        metadata = {
            "phase": ENSEMBLE_PHASE,
            "ensemble_contract_version": ENSEMBLE_CONTRACT_VERSION,
            "methodology_version": ENSEMBLE_METHODOLOGY_VERSION,
            "config": config.to_dict(),
            "config_id": config_id,
            "config_digest": config.digest,
            "weighting": weighted.to_dict(),
            "eligibility": [item.to_dict() for item in weighted.decisions],
            "regime": str(regime or "unclassified"),
            "fallback": True,
            "fallback_model_id": output.model_id,
            "fallback_model_version": output.model_version,
            "fallback_reason": reason,
            "ensemble_id": fallback_ref,
            "automatic_promotion": False,
            "production_ready": False,
            "risk_authority": "NM-08",
            "execution_authority": "NM-10",
        }
        fallback_output = replace(
            output,
            forecast_id=ensemble_id(
                ENSEMBLE_FALLBACK_PREFIX,
                instrument_id,
                horizon.value,
                cutoff.isoformat(),
                output.forecast_id,
                config_id,
            ),
            lifecycle_id=ensemble_id(
                "ensfb_lifecycle_", instrument_id, horizon.value, cutoff.isoformat()
            ),
            ensemble_id=fallback_ref,
            metadata={
                **dict(output.metadata),
                **metadata,
                **dict(forecast_metadata or {}),
            },
            notes=tuple(output.notes)
            + (
                "PI-08 fallback: this is a single constituent forecast, not an ensemble blend.",
            ),
        )
        stored = False
        try:
            stored = self._store().save_forecast(fallback_output)
        except Exception as exc:  # noqa: BLE001
            self._record_failure("persist_fallback", exc)
        self._save_config(
            config_id=config_id,
            ensemble_ref=fallback_ref,
            instrument_id=instrument_id,
            horizon=horizon,
            config=config,
            weights=dict(weighted.weights),
            eligibility=[item.to_dict() for item in weighted.decisions],
            cutoff=cutoff,
            status="fallback",
            regime=regime,
            calibration={"status": "not_applicable", "fallback": True},
        )
        result = EnsembleResult(
            ensemble_id=fallback_ref,
            version=ENSEMBLE_CONTRACT_VERSION,
            status="fallback",
            output=fallback_output,
            eligible_models=tuple(
                item.model_id for item in weighted.decisions if item.eligible
            ),
            excluded_models=tuple(
                item.to_dict() for item in weighted.decisions if not item.eligible
            ),
            weights=dict(weighted.weights),
            disagreement=0.0,
            reasons=(reason,),
        )
        try:
            self._store().save_ensemble(result)
        except Exception as exc:  # noqa: BLE001
            self._record_failure("persist_fallback_ensemble", exc)
        self._generations += 1
        body = fallback_output.to_dict()
        body.update(
            {
                "ok": True,
                "status": "fallback",
                "engine": "ensemble",
                "persisted": stored,
                "regime": str(regime or "unclassified"),
                "ensemble": {
                    **metadata,
                    "fallback": True,
                    "forecast": fallback_output.to_dict(),
                },
                "failures": list(failures),
            }
        )
        return body

    def _disagreement_detail(
        self,
        outputs: Sequence[ForecastOutput],
        weights: Mapping[str, float],
    ) -> dict[str, Any]:
        total = sum(float(value) for value in weights.values())
        if total <= 0.0:
            total = 1.0
        mean = sum(
            output.predicted_return * float(weights.get(
                _weight_key(output.model_id, output.model_version), 0.0
            ))
            for output in outputs
        ) / total
        dispersion = math.sqrt(
            sum(
                float(
                    weights.get(
                        _weight_key(output.model_id, output.model_version), 0.0
                    )
                )
                * (output.predicted_return - mean) ** 2
                for output in outputs
            )
            / total
        )
        votes: dict[str, float] = {"up": 0.0, "down": 0.0, "neutral": 0.0, "uncertain": 0.0}
        for output in outputs:
            key = output.direction.value if output.direction.value in votes else "uncertain"
            votes[key] += float(
                weights.get(
                    _weight_key(output.model_id, output.model_version), 0.0
                )
            ) / total
        widths = [
            output.uncertainty_width
            for output in outputs
            if math.isfinite(output.uncertainty_width)
        ]
        return {
            "weighted_return_mean": round(mean, 12),
            "weighted_return_dispersion": round(dispersion, 12),
            "normalized_return_std": round(dispersion, 12),
            "directional_vote_distribution": {
                key: round(value, 12) for key, value in votes.items()
            },
            "direction_disagreement": round(
                1.0 - max(votes.values()) if votes else 1.0, 12
            ),
            "uncertainty_disagreement": (
                round(max(widths) - min(widths), 12) if widths else None
            ),
            "model_regime_disagreement": None,
            "method": "weighted return dispersion and direction vote distribution",
            "reversal_claimed": False,
        }

    # -- evaluation ----------------------------------------------------------
    async def evaluate(
        self,
        instrument_id: str,
        horizon: ForecastHorizon | str,
        *,
        validation: Sequence[EnsembleObservation] = (),
        test: Sequence[EnsembleObservation] = (),
        config: EnsembleConfig | Mapping[str, Any] | None = None,
        weights: Mapping[str, float] | None = None,
        eligibility: Sequence[Mapping[str, Any]] = (),
        dataset_version: str = "unspecified",
        feature_set_version: str = "unspecified",
        now: datetime | None = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        """Persist validation-only calibration and a separate test report."""
        chosen_horizon = _horizon(horizon)
        chosen_config = (
            config
            if isinstance(config, EnsembleConfig)
            else EnsembleConfig.from_dict(config)
            if config is not None
            else self._default_config
        )
        created = _aware(now or self._clock(), "now")
        validation_rows = [item for item in validation if item.validation]
        test_rows = [item for item in test if not item.validation] or list(test)
        if not validation_rows:
            raise EnsembleValidationError("ensemble evaluation needs validation rows")
        if not test_rows:
            raise EnsembleValidationError("ensemble evaluation needs test rows")
        calibration = fit_calibration(
            validation_rows,
            instrument_id=instrument_id,
            horizon=chosen_horizon,
            config=chosen_config,
            dataset_version=dataset_version,
            created_at=created,
        )
        validation_metrics = metrics_for(validation_rows)
        test_metrics = metrics_for(test_rows, calibration=calibration)
        component_metrics: dict[str, Any] = {}
        for output in validation_rows:
            for name in output.constituent_returns:
                subset = [
                    replace(
                        row,
                        predicted_return=row.constituent_returns[name],
                        predicted_price=row.actual_price
                        * (1.0 + row.constituent_returns[name]),
                        lower_return=min(
                            row.lower_return, row.constituent_returns[name]
                        ),
                        upper_return=max(
                            row.upper_return, row.constituent_returns[name]
                        ),
                        model_id=name,
                    )
                    for row in test_rows
                    if name in row.constituent_returns
                ]
                if subset:
                    component_metrics[name] = metrics_for(subset).to_dict()
        baseline_metrics = {
            "random_walk": metrics_for(
                [
                    replace(
                        row,
                        predicted_return=0.0,
                        predicted_price=(
                            row.actual_price / (1.0 + row.actual_return)
                            if 1.0 + row.actual_return > 0.0
                            else row.predicted_price
                        ),
                        lower_return=-1.0,
                        upper_return=1.0,
                    )
                    for row in test_rows
                ],
                calibration=calibration,
            ).to_dict()
        }
        evaluation_fingerprint = ensemble_id(
            ENSEMBLE_EVALUATION_PREFIX,
            instrument_id,
            chosen_horizon.value,
            chosen_config.digest,
            created.isoformat(),
            len(validation_rows),
            len(test_rows),
        )
        config_id = self._config_id(
            instrument_id, chosen_horizon, chosen_config, created
        )
        evaluation = EnsembleEvaluation(
            evaluation_id=evaluation_fingerprint,
            ensemble_id=ensemble_id(
                "ensb_",
                instrument_id,
                chosen_horizon.value,
                chosen_config.digest,
                created.isoformat(),
            ),
            config_id=config_id,
            instrument_id=instrument_id,
            horizon=chosen_horizon,
            dataset_version=dataset_version,
            feature_set_version=feature_set_version,
            created_at=created,
            config=chosen_config,
            weights=dict(weights or {}),
            eligibility=tuple(dict(item) for item in eligibility),
            validation=validation_metrics.to_dict(),
            test=test_metrics.to_dict(),
            calibration=calibration,
            component_metrics=component_metrics,
            baseline_metrics=baseline_metrics,
            regime_metrics=coverage_by_regime(
                test_rows, calibration=calibration
            ),
            reasons=(
                "calibration was fitted on validation observations only",
                "test observations are reported once and never select the weights",
            ),
        )
        saved = False
        try:
            saved = self._store().save_ensemble_evaluation(evaluation)
            self._store().save_ensemble_calibration(calibration.to_dict())
            self._forecasts().save_calibration(
                {
                    "calibration_id": calibration.calibration_id,
                    "model_id": "ensemble",
                    "horizon": chosen_horizon.value,
                    "status": calibration.status,
                    "created_at": calibration.created_at.isoformat(),
                    "instrument_id": instrument_id,
                    "payload": calibration.to_dict(),
                }
            )
            self._forecasts().save_training_run(
                {
                    "run_id": evaluation.evaluation_id,
                    "instrument_id": instrument_id,
                    "horizon": chosen_horizon.value,
                    "model_id": "ensemble",
                    "version": ENSEMBLE_MODEL_VERSION,
                    "started_at": min(
                        row.target_at for row in validation_rows
                    ).isoformat(),
                    "finished_at": created.isoformat(),
                    "samples": len(validation_rows) + len(test_rows),
                    "kind": "ensemble",
                    "report": evaluation.to_dict(),
                }
            )
        except Exception as exc:  # noqa: BLE001
            self._record_failure("persist_evaluation", exc)
        self._evaluations += 1
        body = evaluation.to_dict()
        body.update({"ok": True, "persisted": saved})
        return body

    # -- read surfaces -------------------------------------------------------
    def explain(self, forecast_id: str) -> dict[str, Any]:
        body = self.forecast(forecast_id)
        metadata = dict(body.get("metadata") or {})
        constituents = list(metadata.get("constituents") or ())
        availability = [
            {
                **dict(item),
                "participated": True,
            }
            for item in constituents
        ]
        return {
            "ok": True,
            "engine": "ensemble",
            "forecast_id": forecast_id,
            "method": "weighted ensemble contribution decomposition",
            "model_id": "ensemble",
            "model_version": body.get("model_version"),
            "config_id": metadata.get("config_id"),
            "weights": dict(metadata.get("weights") or {}),
            "constituents": constituents,
            "eligibility": list(metadata.get("eligibility") or ()),
            "disagreement": metadata.get("disagreement") or {},
            "calibration": metadata.get("calibration") or {},
            "fallback": bool(metadata.get("fallback")),
            "fallback_reason": metadata.get("fallback_reason"),
            "model_availability": availability,
            "limitations": [
                "Weights describe contribution to this forecast, not causal market influence.",
                "An uncalibrated interval is not a guarantee and is not a probability.",
            ],
            "automatic_promotion": False,
        }

    def forecast(self, forecast_id: str) -> dict[str, Any]:
        contract = self._store().get_forecast(forecast_id)
        if contract is None:
            raise ForecastNotFoundError(f"unknown ensemble forecast {forecast_id!r}")
        metadata = dict(contract.metadata or {})
        if contract.model_id != "ensemble" and not (
            contract.forecast_id.startswith(ENSEMBLE_FALLBACK_PREFIX)
            or metadata.get("phase") == ENSEMBLE_PHASE
        ):
            raise ForecastNotFoundError(
                f"forecast {forecast_id!r} is not a PI-08 ensemble forecast"
            )
        body = contract.to_dict()
        body.update(
            {
                "ok": True,
                "engine": "ensemble",
                "ensemble": {
                    "phase": ENSEMBLE_PHASE,
                    "contract_version": ENSEMBLE_CONTRACT_VERSION,
                    "metadata": metadata,
                    "fallback": bool(metadata.get("fallback")),
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
        rows = self._store().list_forecasts(
            instrument_id=instrument_id,
            horizon=label,
            limit=min(max(int(limit), 1), 500),
        )
        rows = [
            row
            for row in rows
            if row.forecast_id.startswith(ENSEMBLE_FORECAST_PREFIX)
            or row.forecast_id.startswith(ENSEMBLE_FALLBACK_PREFIX)
            or row.model_id == "ensemble"
        ]
        if model_id and str(model_id).strip().lower() not in {"", "ensemble"}:
            rows = []
        return {
            "ok": True,
            "engine": "ensemble",
            "forecasts": [row.to_dict() for row in rows],
            "total": len(rows),
        }

    def models(
        self, *, instrument_id: str | None = None, horizon: ForecastHorizon | str | None = None
    ) -> dict[str, Any]:
        label = _horizon(horizon) if horizon is not None else None
        rows: list[dict[str, Any]] = []
        configs: list[dict[str, Any]] = []
        try:
            for item in self._store().models(label):
                if item.model_id == "ensemble":
                    rows.append(item.to_dict())
        except Exception as exc:  # noqa: BLE001
            self._record_failure("models", exc)
        if instrument_id and horizon:
            try:
                config = self._store().latest_ensemble_config(
                    instrument_id, label.value if label else str(horizon)
                )
                if config:
                    configs.append(config)
            except Exception as exc:  # noqa: BLE001
                self._record_failure("models_config", exc)
        return {
            "ok": True,
            "engine": "ensemble",
            "models": rows,
            "configs": configs,
            "total": len(rows),
            "experimental": True,
            "production_ready": False,
            "automatic_promotion": False,
        }

    def performance(
        self,
        *,
        model_id: str | None = None,
        instrument_id: str | None = None,
        horizon: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        rows = self._forecasts().performance(
            model_id="ensemble",
            instrument_id=instrument_id,
            horizon=horizon,
            limit=limit,
        )
        evaluations = [
            item
            for item in self._forecasts().training_runs(limit=min(limit, 100))
            if str(item.get("kind")) == "ensemble"
        ]
        return {
            "ok": True,
            "engine": "ensemble",
            "evaluations": evaluations,
            "performance": rows,
            "probabilities": (
                "withheld unless every contributor supplies calibrated directional "
                "probabilities; the current families do not"
            ),
        }

    def calibration(self, instrument_id: str, horizon: ForecastHorizon | str) -> dict[str, Any]:
        chosen = _horizon(horizon)
        payload = self._latest_calibration(instrument_id, chosen)
        return {
            "ok": True,
            "engine": "ensemble",
            "instrument_id": instrument_id,
            "horizon": chosen.value,
            "status": payload.status if payload else "unavailable",
            "calibration": payload.to_dict() if payload else None,
            "detail": (
                "validation-only split-conformal interval"
                if payload
                else "no validation-only ensemble calibration is available"
            ),
        }

    def _latest_config(
        self,
        instrument_id: str,
        horizon: ForecastHorizon,
        cutoff: datetime | None = None,
    ) -> dict[str, Any]:
        try:
            return self._store().latest_ensemble_config(
                instrument_id, horizon.value, before=cutoff
            ) or {}
        except Exception:
            return {}

    def status(self) -> dict[str, Any]:
        return {
            "phase": ENSEMBLE_PHASE,
            "implemented": True,
            "initialised": True,
            "contract_version": ENSEMBLE_CONTRACT_VERSION,
            "schema_version": ENSEMBLE_SCHEMA_VERSION,
            "methodology_version": ENSEMBLE_METHODOLOGY_VERSION,
            "model_version": ENSEMBLE_MODEL_VERSION,
            "calibration_version": ENSEMBLE_CALIBRATION_VERSION,
            "weighting_method": self._default_config.weighting_method,
            "calibration_method": self._default_config.calibration_method,
            "min_models": self._default_config.min_models,
            "max_models": self._default_config.max_models,
            "generation_count": self._generations,
            "evaluation_count": self._evaluations,
            "failures": self._failures,
            "last_error": self._last_error,
            "failure_reports": list(self._failure_reports),
            "families": [item.value for item in SUPPORTED_FAMILIES],
            "constituent_services": {
                "statistical": self._statistical is not None,
                "gradient": self._gradient is not None,
                "lstm": self._lstm is not None,
            },
            "automatic_promotion": False,
            "production_ready": False,
            "risk_authority": "NM-08",
            "execution_authority": "NM-10",
        }

    def contract(self) -> dict[str, Any]:
        return {
            "phase": ENSEMBLE_PHASE,
            "implemented": True,
            "contract_version": ENSEMBLE_CONTRACT_VERSION,
            "schema_version": ENSEMBLE_SCHEMA_VERSION,
            "methodology_version": ENSEMBLE_METHODOLOGY_VERSION,
            "model_version": ENSEMBLE_MODEL_VERSION,
            "weighting": {
                "method": self._default_config.weighting_method,
                "uses": "validation and walk-forward evidence only",
                "constraints": {
                    "non_negative": True,
                    "finite": True,
                    "normalized": True,
                    "max_weight": self._default_config.max_weight,
                    "min_weight": self._default_config.min_weight,
                    "smoothing": self._default_config.smoothing,
                    "test_set_used_for_weights": False,
                },
            },
            "calibration": {
                "method": ENSEMBLE_CALIBRATION_VERSION,
                "fitted_on": "validation_only",
                "guarantee": False,
                "probabilities": (
                    "withheld unless every contributing model supplies calibrated "
                    "directional probabilities"
                ),
            },
            "families": [item.value for item in SUPPORTED_FAMILIES],
            "horizons": [item.value for item in ForecastHorizon],
            "policy": self._default_config.to_dict(),
            "authorities": {"risk": "NM-08", "execution": "NM-10"},
            "automatic_promotion": False,
            "production_ready": False,
        }

    def close(self) -> None:
        # Constituent services own their connections; the ensemble borrows them.
        return None


__all__ = ["EnsembleForecastingService"]
