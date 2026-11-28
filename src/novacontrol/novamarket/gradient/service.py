"""PI-06 application service for XGBoost and LightGBM forecasts.

The service reuses PI-05's data adapter (dataset versions, cutoff filtering,
exchange calendars and PI-04 feature construction) while keeping gradient
model identity and evaluation separate.  It has no order, position, risk or
execution surface; forecasts remain research evidence.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from novacontrol.novamarket.contracts import (
    ForecastDataError,
    ForecastNotFoundError,
    ForecastValidationError,
)
from novacontrol.novamarket.features.models import FeatureProfileName, FeatureRequest
from novacontrol.novamarket.features.provenance import event_feature_usage
from novacontrol.novamarket.forecasting.store import ForecastStore
from novacontrol.novamarket.marketdata.calendar import calendar_for
from novacontrol.novamarket.marketdata.models import DataInterval, DataQuality, Freshness
from novacontrol.novamarket.prediction.models import (
    CalibrationStatus,
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
from novacontrol.novamarket.statistical.models import (
    ModelConfig,
    StatisticalForecastRequest,
    StatisticalValidationError,
    classify_direction,
    decimal_price,
)
from novacontrol.novamarket.statistical.validation import horizon_steps
from novacontrol.novamarket.statistical.service import StatisticalForecastingService

from novacontrol.novamarket.gradient.artifacts import GradienArtifactStore
from novacontrol.novamarket.gradient.models import (
    GRADIENT_CONTRACT_VERSION,
    GRADIENT_METHODOLOGY_VERSION,
    GRADIENT_SCHEMA_VERSION,
    PI06_PHASE,
    FamilyResult,
    FeatureSchema,
    GradientConfig,
    GradientEvaluationReport,
    GradientExplanation,
    GradientFamily,
    GradientForecast,
    GradientStatus,
    GradientTrainingError,
    GradientUnavailableError,
    GradientValidationError,
    LoadedGradient,
    ModelArtifact,
    family_from,
    model_version,
)
from novacontrol.novamarket.gradient.training import (
    backend_available,
    backend_version,
    baseline_metrics,
    build_training_matrix,
    family_result,
    feature_vector,
    local_contributions,
    out_of_distribution,
    predict_return,
    selection_suitability,
    train_family,
)

_GRADIENT_MODEL_IDS = frozenset(item.value for item in GradientFamily)
_MIN_REGIME_SAMPLES = 5
_INTERVAL_SECONDS = {
    DataInterval.MINUTE_1: 60,
    DataInterval.MINUTE_5: 300,
    DataInterval.MINUTE_15: 900,
    DataInterval.MINUTE_20: 1_200,
    DataInterval.MINUTE_30: 1_800,
    DataInterval.HOUR_1: 3_600,
    DataInterval.DAY_1: 86_400,
    DataInterval.WEEK_1: 604_800,
    DataInterval.MONTH_1: 2_592_000,
}


def _aware(value: object, name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise GradientValidationError(f"{name} is not an ISO timestamp") from exc
    if not isinstance(value, datetime):
        raise GradientValidationError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise GradientValidationError(
            f"{name} must be timezone-aware; naive market time is not accepted"
        )
    return value.astimezone(UTC)


def _config(value: GradientConfig | Mapping[str, Any] | None) -> GradientConfig:
    if isinstance(value, GradientConfig):
        return value
    return GradientConfig.from_dict(value)


def _horizon(value: ForecastHorizon | str) -> ForecastHorizon:
    try:
        return ForecastHorizon(value)
    except ValueError as exc:
        known = ", ".join(item.value for item in ForecastHorizon)
        raise GradientValidationError(
            f"unknown forecast horizon {value!r}; known: {known}"
        ) from exc


def _schema_from(payload: Mapping[str, Any]) -> FeatureSchema:
    try:
        return FeatureSchema(
            names=tuple(str(item) for item in payload["names"]),
            feature_set_version=str(payload["feature_set_version"]),
            profile=str(payload["profile"]),
            source_names=tuple(str(item) for item in payload.get("source_names", ())),
            dropped={
                str(key): str(value)
                for key, value in (payload.get("dropped") or {}).items()
            },
            contract_version=str(payload.get("contract_version") or ""),
            digest_value=str(payload.get("digest") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GradientUnavailableError("persisted feature schema is unreadable") from exc


def _artifact_from(payload: Mapping[str, Any]) -> ModelArtifact:
    try:
        return ModelArtifact(
            family=GradientFamily(str(payload["family"])),
            version=str(payload["version"]),
            regressor_path=str(payload["regressor_path"]),
            regressor_sha256=str(payload["regressor_sha256"]),
            regressor_bytes=int(payload.get("regressor_bytes") or 0),
            classifier_path=str(payload.get("classifier_path") or ""),
            classifier_sha256=str(payload.get("classifier_sha256") or ""),
            classifier_bytes=int(payload.get("classifier_bytes") or 0),
            format=str(payload.get("format") or "native"),
            loaded_verified=bool(payload.get("loaded_verified", False)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GradientUnavailableError("persisted model artifact is unreadable") from exc


def _inferred_interval(bars: Sequence[Any]) -> DataInterval | None:
    """Infer the observation interval from supplied bars, never from a wish.

    Direct API bars carry no dataset metadata.  Assuming the horizon's default
    resolution would let 80 daily observations masquerade as intraday data, so
    the spacing of the observations themselves is the authority here.
    """
    stamps: list[datetime] = []
    for bar in bars:
        value = (
            bar.get("observed_at")
            if isinstance(bar, Mapping)
            else getattr(bar, "observed_at", None)
        )
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                continue
            stamps.append(value.astimezone(UTC))
    if len(stamps) < 2:
        return None
    stamps = sorted(set(stamps))
    if len(stamps) < 2:
        return None
    deltas = sorted(
        (stamps[index] - stamps[index - 1] for index in range(1, len(stamps))),
        key=lambda item: item.total_seconds(),
    )
    seconds = deltas[len(deltas) // 2].total_seconds()
    if seconds <= 0:
        return None
    for interval, target_seconds in _INTERVAL_SECONDS.items():
        if seconds <= target_seconds * 1.5:
            return interval
    return DataInterval.MONTH_1


def _profile(horizon: ForecastHorizon, resolution: DataInterval) -> FeatureProfileName:
    if HORIZON_SPECS[horizon].family.value == "intraday" or resolution.is_intraday:
        return FeatureProfileName.INTRADAY
    if horizon in (
        ForecastHorizon.DAY_1,
        ForecastHorizon.DAYS_3,
        ForecastHorizon.DAYS_5,
    ):
        return FeatureProfileName.SWING
    return FeatureProfileName.LONG_TERM


class GradientForecastingService(StatisticalForecastingService):
    """Orchestrates PI-06 over the existing market, feature and prediction stores."""

    def __init__(
        self,
        *,
        store_path: str | Path | None = None,
        market_data: Any | None = None,
        dataset_service: Any | None = None,
        feature_service: Any | None = None,
        prediction_store: PredictionStore | None = None,
        forecast_store: ForecastStore | None = None,
        publisher: Any | None = None,
        clock: Any | None = None,
        artifact_root: str | Path | None = None,
        default_config: GradientConfig | Mapping[str, Any] | None = None,
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
                artifact_root = Path(store_path).resolve().parent / "gradient_models"
            else:
                artifact_root = Path("data") / "novamarket" / "gradient_models"
        self._artifacts = GradienArtifactStore(artifact_root)
        self._default_gradient_config = _config(default_config)

    # -- data adapters -------------------------------------------------------

    def _data_request(
        self,
        instrument_id: str,
        horizon: ForecastHorizon,
        *,
        as_of: datetime,
        resolution: DataInterval | str | None,
        dataset_id: str,
        dataset_version: str,
        bars: Sequence[Any] | None,
    ) -> StatisticalForecastRequest:
        return StatisticalForecastRequest(
            instrument_id=instrument_id,
            horizon=horizon,
            as_of=as_of,
            resolution=resolution,
            config=ModelConfig(),
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            bars=tuple(bars or ()),
        )

    def _series_for(
        self,
        instrument_id: str,
        horizon: ForecastHorizon,
        *,
        as_of: datetime,
        resolution: DataInterval | str | None,
        dataset_id: str,
        dataset_version: str,
        bars: Sequence[Any] | None,
    ) -> tuple[list[Any], DataInterval, str, str]:
        request = self._data_request(
            instrument_id,
            horizon,
            as_of=as_of,
            resolution=resolution,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            bars=bars,
        )
        try:
            series, chosen_resolution, chosen_dataset_id, chosen_dataset_version = (
                self._series(request)
            )
        except StatisticalValidationError as exc:
            raise GradientValidationError(str(exc)) from exc
        if bars is not None and not dataset_id:
            inferred = _inferred_interval(series)
            if inferred is not None:
                if resolution is None:
                    chosen_resolution = inferred
                elif DataInterval(resolution) != inferred:
                    raise GradientValidationError(
                        f"supplied bars are spaced {inferred.value}, not the "
                        f"requested {DataInterval(resolution).value}"
                    )
        if not HORIZON_SPECS[horizon].accepts_resolution(chosen_resolution):
            raise GradientValidationError(
                f"{chosen_resolution.value} bars cannot support a {horizon.value} "
                f"target; the horizon needs "
                f"{HORIZON_SPECS[horizon].max_input_interval.value} or finer data"
            )
        return series, chosen_resolution, chosen_dataset_id, chosen_dataset_version

    def _training_matrix(
        self,
        instrument_id: str,
        horizon: ForecastHorizon,
        *,
        as_of: datetime,
        resolution: DataInterval | str | None,
        dataset_id: str,
        dataset_version: str,
        bars: Sequence[Any] | None,
        config: GradientConfig,
        fundamentals: Sequence[Any] = (),
        news_events: Sequence[Any] = (),
        benchmark_bars: Mapping[str, Sequence[Any]] | None = None,
        sector_bars: Mapping[str, Sequence[Any]] | None = None,
        context_bars: Mapping[str, Mapping[str, Sequence[Any]]] | None = None,
    ) -> tuple[Any, list[Any], list[Any], DataInterval, str, str]:
        if self._feature_service is None:
            raise GradientUnavailableError(
                "PI-04 feature service is not attached; gradient models cannot "
                "be trained without the validated feature engine"
            )
        series, chosen_resolution, chosen_dataset_id, chosen_dataset_version = self._series_for(
            instrument_id,
            horizon,
            as_of=as_of,
            resolution=resolution,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            bars=bars,
        )
        if not series:
            raise ForecastDataError("no observations are available at or before as_of")
        currency = ""
        try:
            currency = str(self._instrument_info(instrument_id).get("currency") or "")
        except Exception:  # noqa: BLE001 - currency is provenance only
            currency = ""
        ohlcv = self._feature_bars(series, instrument_id, chosen_resolution, currency)
        calendar = self._calendar_for(instrument_id)
        matrix = build_training_matrix(
            feature_engine=self._feature_service.engine,
            instrument_id=instrument_id,
            horizon=horizon,
            resolution=chosen_resolution,
            as_of=as_of,
            bars=ohlcv,
            dataset_id=chosen_dataset_id,
            dataset_version=chosen_dataset_version,
            config=config,
            calendars=(
                {instrument_id: calendar} if calendar is not None else None
            ),
            fundamentals=fundamentals,
            news_events=news_events,
            benchmark_bars=benchmark_bars,
            sector_bars=sector_bars,
            context_bars=context_bars,
        )
        return (
            matrix,
            ohlcv,
            series,
            chosen_resolution,
            chosen_dataset_id,
            chosen_dataset_version,
        )

    def _calendar_for(self, instrument_id: str) -> Any | None:
        """The verified exchange calendar, or ``None`` when it is unavailable."""
        if self._market_data is None:
            return None
        try:
            return calendar_for(self._market_data.get_instrument(instrument_id))
        except Exception:  # noqa: BLE001 - calendar evidence is optional here
            return None

    # -- model registry ------------------------------------------------------

    def _latest_model(
        self,
        family: GradientFamily,
        horizon: ForecastHorizon,
        version: str = "",
        instrument_id: str = "",
    ) -> ModelMetadata | None:
        candidates: list[ModelMetadata] = []
        for item in self._prediction_store.models(horizon):
            if item.model_id != family.value or horizon not in item.supported_horizons:
                continue
            if version and item.version != version:
                continue
            if not isinstance((item.metrics or {}).get("artifact"), Mapping):
                continue
            if instrument_id:
                metrics = item.metrics or {}
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

    def _load_model(
        self,
        family: GradientFamily,
        horizon: ForecastHorizon,
        version: str = "",
        instrument_id: str = "",
    ) -> tuple[ModelMetadata, LoadedGradient]:
        metadata = self._latest_model(
            family, horizon, version, instrument_id=instrument_id
        )
        if metadata is None:
            label = version or "latest"
            raise GradientUnavailableError(
                f"no trained {family.value} model for {horizon.value} ({label}); "
                "run a PI-06 training/evaluation first"
            )
        artifact_payload = (metadata.metrics or {}).get("artifact")
        schema_payload = (metadata.metrics or {}).get("feature_schema")
        if not isinstance(artifact_payload, Mapping) or not isinstance(
            schema_payload, Mapping
        ):
            raise GradientUnavailableError(
                "model metadata has no readable artifact/schema"
            )
        artifact = _artifact_from(artifact_payload)
        schema = _schema_from(schema_payload)
        loaded = self._artifacts.load(artifact, schema)
        return metadata, loaded

    def _register_gradient_model(
        self,
        result: FamilyResult,
        *,
        horizon: ForecastHorizon,
        cutoff: datetime,
        dataset_version: str,
        selected: bool,
        report_id: str,
        instrument_id: str = "",
        target_steps: int | None = None,
    ) -> None:
        try:
            existing_instruments: set[str] = set()
            for item in self._prediction_store.models(horizon):
                if item.model_id != result.family.value or item.version != result.version:
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
                }
            )
            state = (
                ModelState.DEGRADED
                if result.status
                in {
                    GradientStatus.BASELINES_PREFERRED,
                    GradientStatus.PROVISIONAL,
                    GradientStatus.INSUFFICIENT_DATA,
                    GradientStatus.FAILED,
                    GradientStatus.UNSUPPORTED,
                }
                else ModelState.ELIGIBLE
            )
            metadata = ModelMetadata(
                model_id=result.family.value,
                version=result.version,
                family=(
                    ModelFamily.XGBOOST
                    if result.family is GradientFamily.XGBOOST
                    else ModelFamily.LIGHTGBM
                ),
                state=state,
                supported_horizons=(horizon,),
                calibration=CalibrationStatus.UNCALIBRATED,
                metrics=metrics,
                trained_at=cutoff,
                updated_at=cutoff,
                notes=(
                    "PI-06 gradient model; research evidence only, not automatically "
                    "promoted, no risk or execution authority"
                ),
                feature_set_version=(
                    result.schema.feature_set_version if result.schema else "unspecified"
                ),
                dataset_version=dataset_version,
            )
            self._prediction_store.register_model(metadata)
        except Exception as exc:  # noqa: BLE001 - one metadata failure is isolated
            self._record_failure(result.family.value, "register_model", exc)
            raise

    def _register_gradient_unavailable(
        self,
        family: GradientFamily,
        *,
        horizon: ForecastHorizon,
        cutoff: datetime,
        reason: str,
        dataset_version: str,
    ) -> None:
        try:
            self._prediction_store.register_model(
                ModelMetadata(
                    model_id=family.value,
                    version=f"pi06-{family.value}-unavailable",
                    family=(
                        ModelFamily.XGBOOST
                        if family is GradientFamily.XGBOOST
                        else ModelFamily.LIGHTGBM
                    ),
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
                    notes="PI-06 gradient model unavailable for this evidence run",
                    feature_set_version="unspecified",
                    dataset_version=dataset_version,
                )
            )
        except Exception as exc:  # noqa: BLE001
            self._record_failure(family.value, "register_unavailable", exc)

    # -- training and evaluation --------------------------------------------

    async def evaluate(  # type: ignore[override]
        self,
        instrument_id: str,
        horizon: ForecastHorizon | str,
        *,
        as_of: datetime | None = None,
        resolution: DataInterval | str | None = None,
        dataset_id: str = "",
        dataset_version: str = "",
        config: GradientConfig | Mapping[str, Any] | None = None,
        bars: Sequence[Any] | None = None,
        fundamentals: Sequence[Any] = (),
        news_events: Sequence[Any] = (),
        benchmark_bars: Mapping[str, Sequence[Any]] | None = None,
        sector_bars: Mapping[str, Sequence[Any]] | None = None,
        context_bars: Mapping[str, Mapping[str, Sequence[Any]]] | None = None,
        families: Sequence[GradientFamily | str] | None = None,
        models: Sequence[str] | None = None,
        train_ratio: float | None = None,
        validation_ratio: float | None = None,
    ) -> dict[str, Any]:
        cutoff = _aware(as_of or self._clock(), "as_of")
        chosen_horizon = _horizon(horizon)
        chosen_config = _config(config) if config is not None else self._default_gradient_config
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
        families = families or models
        requested_families = tuple(
            family_from(item)
            for item in (families or (GradientFamily.XGBOOST, GradientFamily.LIGHTGBM))
        )
        try:
            (
                matrix,
                ohlcv,
                series,
                chosen_resolution,
                chosen_dataset_id,
                chosen_dataset_version,
            ) = (
                self._training_matrix(
                    instrument_id,
                    chosen_horizon,
                    as_of=cutoff,
                    resolution=resolution,
                    dataset_id=dataset_id,
                    dataset_version=dataset_version,
                    bars=bars,
                    config=chosen_config,
                    fundamentals=fundamentals,
                    news_events=news_events,
                    benchmark_bars=benchmark_bars,
                    sector_bars=sector_bars,
                    context_bars=context_bars,
                )
            )
        except (GradientTrainingError, GradientValidationError) as exc:
            self._record_failure("gradient", "prepare", exc)
            raise
        except Exception as exc:  # noqa: BLE001
            self._record_failure("gradient", "prepare", exc)
            raise ForecastDataError(
                f"gradient training data could not be prepared: {type(exc).__name__}: {exc}"
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
        except Exception as exc:  # noqa: BLE001 - baseline failure is reported, not fatal
            baselines = {}
            self._record_failure("baseline", "evaluate", exc)

        results: dict[str, FamilyResult] = {}
        failures: list[str] = []
        for family in requested_families:
            if not backend_available(family):
                reason = (
                    f"{family.value} is not installed; install the optional "
                    "'prediction' extra to train this family"
                )
                failures.append(reason)
                results[family.value] = self._failed_family(
                    family, matrix, reason, GradientStatus.UNSUPPORTED
                )
                self._register_gradient_unavailable(
                    family,
                    horizon=chosen_horizon,
                    cutoff=cutoff,
                    reason=reason,
                    dataset_version=chosen_dataset_version,
                )
                continue
            try:
                fitted = train_family(
                    family=family,
                    config=chosen_config,
                    matrix=matrix,
                    instrument_id=instrument_id,
                    horizon=chosen_horizon,
                    as_of=cutoff,
                )
                version = model_version(
                    family,
                    instrument_id=instrument_id,
                    horizon=chosen_horizon,
                    dataset_id=chosen_dataset_id,
                    dataset_version=chosen_dataset_version,
                    feature_set_version=matrix.schema.feature_set_version,
                    target_steps=horizon_steps(chosen_horizon, chosen_resolution),
                    config_digest=fitted.config.digest,
                    train_start=fitted.training_start,
                    train_end=fitted.training_end,
                )
                artifact = self._artifacts.save(
                    fitted, instrument_id=instrument_id, version=version
                )
                result = family_result(
                    fitted,
                    version=version,
                    artifact=artifact,
                    status=GradientStatus.TRAINED,
                    selected=False,
                )
                results[family.value] = result
            except Exception as exc:  # noqa: BLE001 - one family must not stop the other
                reason = f"{type(exc).__name__}: {exc}"
                failures.append(f"{family.value}: {reason}")
                self._record_failure(family.value, "train", exc)
                results[family.value] = self._failed_family(
                    family, matrix, reason, GradientStatus.FAILED
                )
                self._register_gradient_unavailable(
                    family,
                    horizon=chosen_horizon,
                    cutoff=cutoff,
                    reason=reason,
                    dataset_version=chosen_dataset_version,
                )

        train_indices = matrix.train_indices
        selected_family: str | None = None
        selected_config: GradientConfig | None = None
        selected_version: str | None = None
        suitability: dict[str, dict[str, Any]] = {}
        trained = [
            (key, value)
            for key, value in results.items()
            if value.test is not None and value.validation is not None
        ]
        if trained:
            selected_key, chosen_result = min(
                trained,
                key=lambda item: (
                    item[1].validation.price_mae
                    if item[1].validation and item[1].validation.price_mae is not None
                    else float("inf"),
                    item[0],
                ),
            )
            selected_family = selected_key
            selected_config = chosen_result.config
            selected_version = chosen_result.version
        for key, result in results.items():
            selection_report = selection_suitability(
                result,
                baselines,
                minimum_test_samples=chosen_config.min_test_samples,
            )
            suitability[key] = selection_report
            if result.test is None:
                # A failed/unsupported family keeps its actual status and root
                # cause; a suitability placeholder must not relabel it trained.
                results[key] = replace(result, selected=False)
                continue
            selection_status = str(selection_report.get("status") or "")
            status = {
                "gradient_suitable": GradientStatus.GRADIENT_SUITABLE,
                "provisional": GradientStatus.PROVISIONAL,
                "insufficient_data": GradientStatus.INSUFFICIENT_DATA,
            }.get(selection_status, GradientStatus.BASELINES_PREFERRED)
            results[key] = replace(
                result,
                status=status,
                selected=(key == selected_family),
                reason=str(selection_report.get("reason") or result.reason),
            )

        selected_result: FamilyResult | None = results.get(selected_family or "", None)
        best_baseline = min(
            (
                value.price_mae
                for value in baselines.values()
                if value.price_mae is not None
            ),
            default=None,
        )
        preferred_overall = "insufficient_data"
        if selected_result is not None and selected_result.test is not None:
            selected_mae = selected_result.test.price_mae
            if (
                selected_mae is not None
                and best_baseline is not None
                and selected_mae < best_baseline
                and suitability.get(selected_family or "", {}).get("status")
                == "gradient_suitable"
            ):
                preferred_overall = selected_family or "gradient"
            elif best_baseline is not None:
                preferred_overall = "statistical_baseline"
        elif baselines:
            preferred_overall = "statistical_baseline"

        test_regimes: dict[str, int] = {}
        for index in matrix.test_indices:
            regime = matrix.rows[index].regime
            test_regimes[regime] = test_regimes.get(regime, 0) + 1
        insufficient = tuple(
            sorted(
                regime
                for regime, count in test_regimes.items()
                if count < _MIN_REGIME_SAMPLES
            )
        )
        report_id = "pir_" + hashlib.sha256(
            f"{instrument_id}:{chosen_horizon.value}:{chosen_dataset_id}:"
            f"{chosen_dataset_version}:{chosen_config.digest}:{cutoff.isoformat()}".encode()
        ).hexdigest()[:24]
        train_start_at = min(
            (matrix.rows[index].decision_at for index in train_indices),
            default=None,
        )
        train_end_at = max(
            (matrix.rows[index].decision_at for index in train_indices),
            default=None,
        )
        evaluation_report = GradientEvaluationReport(
            instrument_id=instrument_id,
            horizon=chosen_horizon,
            resolution=chosen_resolution,
            horizon_steps=horizon_steps(chosen_horizon, chosen_resolution),
            dataset_id=chosen_dataset_id,
            dataset_version=chosen_dataset_version,
            feature_set_version=matrix.schema.feature_set_version,
            feature_profile=matrix.schema.profile,
            target_definition=(
                "future close / decision close - 1; direction uses "
                f"{chosen_config.effective_neutral_band_pct:.4f}% neutral band"
            ),
            train_range={
                "start": train_start_at.isoformat() if train_start_at else None,
                "end": train_end_at.isoformat() if train_end_at else None,
                "samples": len(train_indices),
            },
            validation_range={
                "start": (
                    matrix.validation_start.isoformat()
                    if matrix.validation_start
                    else None
                ),
                "end": (
                    matrix.validation_end.isoformat() if matrix.validation_end else None
                ),
                "samples": len(matrix.validation_indices),
            },
            test_range={
                "start": matrix.test_start.isoformat() if matrix.test_start else None,
                "end": matrix.test_end.isoformat() if matrix.test_end else None,
                "samples": len(matrix.test_indices),
            },
            families=results,
            baselines=baselines,
            selected_family=selected_family,
            selected_version=selected_version,
            selected_config=selected_config,
            preferred_overall=preferred_overall,
            selection_metric="validation_price_mae",
            suitability=suitability,
            regime_samples=test_regimes,
            insufficient_regimes=insufficient,
            failures=tuple(failures),
            notes=(
                "chronological PI-02 split with purged boundaries",
                "validation selected the family/configuration; test is reported once",
                "tree gain importance is global and does not explain every prediction",
                "no automatic promotion, risk decision or execution",
            ),
            report_id=report_id,
        )

        for key, result in results.items():
            try:
                self._register_gradient_model(
                    result,
                    horizon=chosen_horizon,
                    cutoff=cutoff,
                    dataset_version=chosen_dataset_version,
                    selected=(key == selected_family),
                    report_id=report_id,
                    instrument_id=instrument_id,
                    target_steps=horizon_steps(
                        chosen_horizon, chosen_resolution
                    ),
                )
            except Exception:  # noqa: BLE001 - already recorded inside
                pass
            if result.test is not None:
                try:
                    snapshot_id = "gperf_" + hashlib.sha256(
                        f"{report_id}:{key}:{cutoff.isoformat()}".encode()
                    ).hexdigest()[:24]
                    self._forecast_store.save_performance(
                        {
                            "snapshot_id": snapshot_id,
                            "model_id": key,
                            "instrument_id": instrument_id,
                            "horizon": chosen_horizon.value,
                            "samples": result.test.samples,
                            "created_at": cutoff.isoformat(),
                            "metrics": {
                                **result.test.to_dict(),
                                "family": key,
                                "version": result.version,
                                "interval_supported": bool(
                                    result.interval and result.interval.supported
                                ),
                            },
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    self._record_failure(key, "persist_performance", exc)

        try:
            self._forecast_store.save_training_run(
                {
                    "run_id": report_id,
                    "instrument_id": instrument_id,
                    "horizon": chosen_horizon.value,
                    "model_id": selected_family or "gradient",
                    "version": selected_version or "unavailable",
                    "started_at": (
                        min(
                            (matrix.rows[index].decision_at for index in train_indices),
                            default=cutoff,
                        ).isoformat()
                    ),
                    "finished_at": cutoff.isoformat(),
                    "samples": len(matrix.train_indices)
                    + len(matrix.validation_indices)
                    + len(matrix.test_indices),
                    "kind": "gradient",
                    "selected_model": selected_family,
                    "selected_config": (
                        selected_config.to_dict() if selected_config else None
                    ),
                    "report": evaluation_report.to_dict(),
                }
            )
        except Exception as exc:  # noqa: BLE001
            self._record_failure(selected_family or "gradient", "persist_evaluation", exc)

        self._evaluations += 1
        await self._publish(
            "novamarket.forecast.model.trained",
            model_id=selected_family or "gradient",
            instrument_id=instrument_id,
            horizon=chosen_horizon.value,
            state="research_candidate",
        )
        body = evaluation_report.to_dict()
        body.update(
            {
                "ok": True,
                "run_id": report_id,
                "selected_model": selected_family,
                "selected_version": selected_version,
                "selection_metric": evaluation_report.selection_metric,
                "automatic_promotion": False,
            }
        )
        return body

    async def train(self, instrument_id: str, horizon: ForecastHorizon | str, **kwargs: Any) -> dict[str, Any]:
        return await self.evaluate(instrument_id, horizon, **kwargs)

    async def compare(
        self, instrument_id: str, horizon: ForecastHorizon | str, **kwargs: Any
    ) -> dict[str, Any]:
        return await self.evaluate(instrument_id, horizon, **kwargs)

    def _failed_family(
        self,
        family: GradientFamily,
        matrix: Any,
        reason: str,
        status: GradientStatus,
    ) -> FamilyResult:
        return FamilyResult(
            family=family,
            version=f"pi06-{family.value}-unavailable",
            status=status,
            config=self._default_gradient_config,
            schema=matrix.schema,
            validation=None,
            test=None,
            classifier_validation=None,
            classifier_test=None,
            interval=None,
            feature_importance=(),
            artifact=None,
            train_samples=len(matrix.train_indices),
            validation_samples=len(matrix.validation_indices),
            test_samples=len(matrix.test_indices),
            training_start=None,
            training_end=None,
            dependency_version=backend_version(family),
            reason=reason,
            failures=(reason,),
        )

    # -- inference -----------------------------------------------------------

    def _model_for_forecast(
        self,
        instrument_id: str,
        horizon: ForecastHorizon,
        *,
        model_id: str,
        version: str,
    ) -> tuple[GradientFamily, ModelMetadata, LoadedGradient]:
        family: GradientFamily | None = None
        if model_id:
            family = family_from(model_id)
        else:
            try:
                for run in self._forecast_store.training_runs(limit=100):
                    if (
                        str(run.get("kind")) != "gradient"
                        or str(run.get("instrument_id")) != instrument_id
                        or str(run.get("horizon")) != horizon.value
                    ):
                        continue
                    candidate = str(run.get("selected_model") or "")
                    if candidate in _GRADIENT_MODEL_IDS:
                        family = GradientFamily(candidate)
                        break
            except Exception:  # noqa: BLE001 - selection read is optional
                family = None
        candidates = [family] if family else [GradientFamily.XGBOOST, GradientFamily.LIGHTGBM]
        errors: list[str] = []
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                metadata, loaded = self._load_model(
                    candidate, horizon, version, instrument_id=instrument_id
                )
                return candidate, metadata, loaded
            except Exception as exc:  # noqa: BLE001 - try the other family only if unspecified
                errors.append(f"{candidate.value}: {type(exc).__name__}: {exc}")
                continue
        raise GradientUnavailableError(
            "no usable trained gradient model for this instrument/horizon; "
            + "; ".join(errors[:2])
        )

    async def generate(  # type: ignore[override]
        self,
        instrument_id: str,
        horizon: ForecastHorizon | str,
        *,
        as_of: datetime | None = None,
        model_id: str = "",
        version: str = "",
        resolution: DataInterval | str | None = None,
        dataset_id: str = "",
        dataset_version: str = "",
        bars: Sequence[Any] | None = None,
        exchange: str = "",
        currency: str = "",
        fundamentals: Sequence[Any] = (),
        news_events: Sequence[Any] = (),
        benchmark_bars: Mapping[str, Sequence[Any]] | None = None,
        sector_bars: Mapping[str, Sequence[Any]] | None = None,
        context_bars: Mapping[str, Mapping[str, Sequence[Any]]] | None = None,
    ) -> dict[str, Any]:
        cutoff = _aware(as_of or self._clock(), "as_of")
        chosen_horizon = _horizon(horizon)
        family, metadata, loaded = self._model_for_forecast(
            instrument_id,
            chosen_horizon,
            model_id=model_id,
            version=version,
        )
        stored_horizon = str((metadata.metrics or {}).get("horizon") or "")
        if stored_horizon and stored_horizon != chosen_horizon.value:
            raise GradientValidationError(
                f"model {metadata.model_id!r} was trained for horizon "
                f"{stored_horizon!r}, not {chosen_horizon.value!r}"
            )
        if self._feature_service is None:
            raise GradientUnavailableError(
                "PI-04 feature service is not attached; gradient inference requires "
                "the validated feature engine"
            )
        series, chosen_resolution, chosen_dataset_id, chosen_dataset_version = self._series_for(
            instrument_id,
            chosen_horizon,
            as_of=cutoff,
            resolution=resolution,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            bars=bars,
        )
        if not series:
            raise ForecastDataError("no observations are available at or before as_of")
        stored_steps = (metadata.metrics or {}).get("target_steps")
        if stored_steps is not None:
            expected_steps = horizon_steps(chosen_horizon, chosen_resolution)
            if int(stored_steps) != int(expected_steps):
                raise GradientValidationError(
                    f"model {metadata.model_id!r} was trained with "
                    f"{stored_steps} target steps, but "
                    f"{chosen_horizon.value!r} at {chosen_resolution.value} "
                    f"requires {expected_steps}"
                )
        currency = currency or str(
            self._safe_instrument(instrument_id).get("currency") or ""
        )
        ohlcv = self._feature_bars(series, instrument_id, chosen_resolution, currency)
        profile = _profile(chosen_horizon, chosen_resolution)
        request = FeatureRequest(
            instrument_id=instrument_id,
            as_of=cutoff,
            resolution=chosen_resolution.value,
            profile=profile,
            timeframes=(),
            include_context=bool(benchmark_bars or sector_bars or context_bars),
            include_fundamentals=bool(fundamentals),
            include_news=bool(news_events),
            max_bars=max(30, min(len(series), 500)),
        )
        engine_bars: dict[str, Any] = {chosen_resolution.value: ohlcv}
        engine_bars.update(benchmark_bars or {})
        engine_bars.update(sector_bars or {})
        for role_bars in (context_bars or {}).values():
            engine_bars.update(role_bars)
        try:
            feature_result = self._feature_service.engine.generate(
                request,
                bars=engine_bars,
                fundamentals=fundamentals,
                news_events=news_events,
            )
        except Exception as exc:  # noqa: BLE001
            self._record_failure(family.value, "features", exc)
            raise GradientUnavailableError(
                f"forecast-time feature generation failed: {type(exc).__name__}: {exc}"
            ) from exc
        feature_set = feature_result.feature_set
        if feature_set.version != loaded.schema.feature_set_version:
            raise GradientUnavailableError(
                "feature-set version mismatch; the model was trained on "
                f"{loaded.schema.feature_set_version}, current features are "
                f"{feature_set.version}"
            )
        if loaded.schema.profile != profile.value:
            raise GradientUnavailableError(
                "feature-profile mismatch; the model was trained for "
                f"{loaded.schema.profile}, current features are {profile.value}"
            )
        try:
            x, feature_values, missing_fraction = feature_vector(
                feature_set, loaded.schema
            )
        except GradientValidationError as exc:
            raise GradientUnavailableError(str(exc)) from exc
        if missing_fraction > loaded.config.max_inference_missing_fraction:
            raise GradientUnavailableError(
                "abstained: "
                f"{missing_fraction:.1%} of the required feature columns are missing "
                f"(limit {loaded.config.max_inference_missing_fraction:.0%})"
            )
        if out_of_distribution(
            x,
            loaded.feature_stats,
            loaded.schema.names,
            z_limit=loaded.config.extrapolation_z,
            max_fraction=loaded.config.max_extrapolation_fraction,
        ):
            raise GradientUnavailableError(
                "abstained: input is outside the training feature distribution "
                f"(z>{loaded.config.extrapolation_z:g} on more than "
                f"{loaded.config.max_extrapolation_fraction:.0%} of usable features)"
            )
        predicted_return, probabilities = predict_return(loaded, x)
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
        if (
            loaded.interval.supported
            and width > loaded.config.max_interval_width
        ):
            raise GradientUnavailableError(
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
                "methodology": GRADIENT_METHODOLOGY_VERSION,
                "family": family.value,
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
            "gbf_",
            instrument_id,
            chosen_horizon.value,
            cutoff.isoformat(),
            family.value,
            loaded.version,
            chosen_dataset_id,
            chosen_dataset_version,
            payload_digest,
        )
        contract = ForecastOutput(
            forecast_id=forecast_id,
            lifecycle_id=prediction_id(
                "gfl_", "gradient", instrument_id, chosen_horizon.value
            ),
            lifecycle_stage=ConfirmationStage.INITIAL,
            instrument_id=instrument_id,
            exchange=str(info.get("exchange") or exchange),
            currency=str(info.get("currency") or currency or "USD"),
            reference_price=decimal_price(float(last.close)),
            reference_at=last.observed_at,
            predicted_price=decimal_price(predicted_price),
            predicted_return=predicted_return,
            direction=direction,
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
            ensemble_id="gradient-boosting",
            model_id=family.value,
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
                "features are observed at or before the data cutoff",
                "the model artifact checksum and feature schema were verified",
                "the interval is validation-residual based, not a guarantee",
                "raw tree scores are not represented as probabilities",
            ),
            disagreement=0.0,
            digest=payload_digest,
            notes=(
                f"PI-06 {family.value} gradient model {loaded.version}",
                f"feature_schema={loaded.schema.digest_value}",
                "research evidence only; not automatically promoted",
                "no risk or execution authority",
            ),
            metadata={
                "phase": PI06_PHASE,
                "gradient_contract_version": GRADIENT_CONTRACT_VERSION,
                "methodology_version": GRADIENT_METHODOLOGY_VERSION,
                "family": family.value,
                "config": loaded.config.to_dict(),
                "config_digest": loaded.config.digest,
                "artifact": loaded.artifact.to_dict(),
                "feature_schema": loaded.schema.to_dict(),
                "feature_values": dict(feature_values),
                "feature_importance": [dict(item) for item in loaded.feature_importance],
                "classifier_probabilities": probabilities,
                "classifier_probabilities_calibrated": False,
                "missing_feature_fraction": missing_fraction,
                "out_of_distribution": False,
                "interval": loaded.interval.to_dict(),
                "dataset_id": chosen_dataset_id,
                "dataset_version": chosen_dataset_version,
                "feature_set_id": feature_set.feature_set_id,
                # PI-24 needs the forecast to say whether event features were
                # consumed; the schema decides, not proximity to an event.
                **event_feature_usage(feature_set, loaded.schema.names),
                "horizon_steps": horizon_steps(chosen_horizon, chosen_resolution),
                "promotion": "not_promoted",
                "automatic_promotion": False,
                "production_ready": False,
            },
            feature_set_version=loaded.schema.feature_set_version,
            dataset_version=chosen_dataset_version,
        )
        stored = False
        try:
            stored = self._prediction_store.save_forecast(contract)
        except Exception as exc:  # noqa: BLE001
            self._record_failure(family.value, "persist", exc)
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
            model_id=family.value,
        )
        body = GradientForecast(
            contract=contract,
            family=self._model_result(metadata, family, loaded),
            schema=loaded.schema,
            feature_values=feature_values,
            classifier_probabilities=probabilities,
            interval=loaded.interval,
            out_of_distribution=False,
            missing_fraction=missing_fraction,
            persisted=stored,
        ).to_dict()
        body.update(
            {
                "ok": True,
                "status": "pending",
                "model": {
                    "model_id": family.value,
                    "model_version": loaded.version,
                    "family": family.value,
                    "config": loaded.config.to_dict(),
                },
                "persisted": stored,
            }
        )
        return body

    def _model_result(
        self,
        metadata: ModelMetadata,
        family: GradientFamily,
        loaded: LoadedGradient,
    ) -> FamilyResult:
        metrics = metadata.metrics or {}
        schema = loaded.schema
        try:
            status = GradientStatus(str(metrics.get("status") or "trained"))
        except ValueError:
            status = GradientStatus.TRAINED
        return FamilyResult(
            family=family,
            version=loaded.version,
            status=status,
            config=loaded.config,
            schema=schema,
            validation=None,
            test=None,
            classifier_validation=None,
            classifier_test=None,
            interval=loaded.interval,
            feature_importance=loaded.feature_importance,
            artifact=loaded.artifact,
            train_samples=int(metrics.get("train_samples") or 0),
            validation_samples=int(metrics.get("validation_samples") or 0),
            test_samples=int(metrics.get("test_samples") or 0),
            training_start=None,
            training_end=None,
            dependency_version=str(metrics.get("dependency_version") or ""),
            notes=("loaded from the existing model registry",),
            selected=bool(metrics.get("selected", False)),
        )

    def _safe_instrument(self, instrument_id: str) -> dict[str, Any]:
        try:
            return self._instrument_info(instrument_id)
        except Exception:  # noqa: BLE001 - dataset/bars forecasts may precede catalogue ingest
            exchange = instrument_id.split(":", 1)[0] if ":" in instrument_id else ""
            return {
                "instrument_id": instrument_id,
                "exchange": exchange,
                "currency": "INR" if exchange in {"XNSE", "XBOM"} else "USD",
            }

    @staticmethod
    def _quality(bar: Any) -> DataQuality:
        try:
            return DataQuality(str(getattr(bar, "quality", "valid")))
        except ValueError:
            return DataQuality.INVALID

    @staticmethod
    def _freshness(bar: Any) -> Freshness:
        try:
            return Freshness(str(getattr(bar, "freshness", "unknown")))
        except ValueError:
            return Freshness.UNKNOWN

    # -- explanation ---------------------------------------------------------

    def explain(self, forecast_id: str) -> dict[str, Any]:
        contract = self._require_gradient_forecast(forecast_id)
        metadata = contract.metadata or {}
        schema_payload = metadata.get("feature_schema")
        artifact_payload = metadata.get("artifact")
        feature_values = metadata.get("feature_values")
        if not isinstance(schema_payload, Mapping) or not isinstance(
            artifact_payload, Mapping
        ):
            raise GradientUnavailableError("forecast has no persisted gradient provenance")
        if not isinstance(feature_values, Mapping):
            raise GradientUnavailableError("forecast has no persisted feature row")
        schema = _schema_from(schema_payload)
        artifact = _artifact_from(artifact_payload)
        try:
            loaded = self._artifacts.load(artifact, schema)
        except Exception as exc:  # noqa: BLE001
            raise GradientUnavailableError(
                f"model artifact could not be loaded for explanation: {exc}"
            ) from exc
        row_values: list[float] = []
        for name in schema.names:
            value = feature_values.get(name)
            row_values.append(np.nan if value is None else float(value))
        row = np.asarray(row_values, dtype=np.float64)
        try:
            raw = local_contributions(loaded, row)
        except Exception as exc:  # noqa: BLE001
            raise GradientUnavailableError(
                f"local tree contributions are unavailable: {type(exc).__name__}: {exc}"
            ) from exc
        if len(raw) < len(schema.names):
            raise GradientUnavailableError(
                "local contribution vector does not match the feature schema"
            )
        contributions = [
            {
                "feature": name,
                "value": feature_values.get(name),
                "contribution": float(raw[index]),
            }
            for index, name in enumerate(schema.names)
        ]
        contributions.sort(
            key=lambda item: (
                -abs(float(item.get("contribution") or 0.0)),
                str(item.get("feature")),
            )
        )
        base = float(raw[-1]) if len(raw) > len(schema.names) else None
        importance = metadata.get("feature_importance")
        method = (
            "xgboost_pred_contribs_tree_shap"
            if loaded.family is GradientFamily.XGBOOST
            else "lightgbm_pred_contrib_tree_shap"
        )
        explanation = GradientExplanation(
            forecast_id=contract.forecast_id,
            instrument_id=contract.instrument_id,
            horizon=contract.forecast_horizon.value,
            model_id=contract.model_id,
            model_version=contract.model_version,
            method=method,
            global_importance=(
                tuple(dict(item) for item in importance)
                if isinstance(importance, Sequence)
                else loaded.feature_importance
            ),
            local_contributions=tuple(contributions[:30]),
            feature_values={name: feature_values.get(name) for name in schema.names},
            base_value=base,
            prediction=contract.predicted_return,
            limitations=(
                "SHAP-style contributions explain this model's inputs, not causality",
                "global gain importance does not explain every individual prediction",
                "the interval is a validation-residual estimate, not a guarantee",
                "tree contributions do not establish trading profitability",
            ),
        )
        return {"ok": True, **explanation.to_dict()}

    # -- reads ---------------------------------------------------------------

    def _require_gradient_forecast(self, forecast_id: str) -> ForecastOutput:
        contract = self._prediction_store.get_forecast(forecast_id)
        if contract is None:
            raise ForecastNotFoundError(f"unknown forecast {forecast_id!r}")
        if not contract.forecast_id.startswith("gbf_") and contract.model_id not in _GRADIENT_MODEL_IDS:
            raise ForecastNotFoundError(
                f"forecast {forecast_id!r} is not a PI-06 gradient forecast"
            )
        return contract

    def forecast(self, forecast_id: str) -> dict[str, Any]:
        contract = self._require_gradient_forecast(forecast_id)
        body = contract.to_dict()
        body["ok"] = True
        body["gradient"] = {
            "family": contract.model_id,
            "model_version": contract.model_version,
            "experimental": True,
            "production_ready": False,
            "automatic_promotion": False,
        }
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
        family = family_from(model_id) if model_id else None
        rows = self._prediction_store.list_forecasts(
            instrument_id=instrument_id,
            horizon=label,
            model_id=family.value if family else None,
            limit=limit,
        )
        gradient = [
            row
            for row in rows
            if row.forecast_id.startswith("gbf_") or row.model_id in _GRADIENT_MODEL_IDS
        ]
        return {
            "ok": True,
            "forecasts": [row.to_dict() for row in gradient],
            "total": len(gradient),
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
            if item.model_id in _GRADIENT_MODEL_IDS
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
            if str(row.get("kind")) == "gradient"
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
        rows = self._forecast_store.performance(
            model_id=model_id,
            instrument_id=instrument_id,
            horizon=horizon,
            limit=limit,
        )
        return [
            row
            for row in rows
            if str(row.get("snapshot_id") or "").startswith("gperf_")
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
        config: GradientConfig | Mapping[str, Any] | None = None,
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
            steps = horizon_steps(chosen_horizon, chosen_resolution)
            chosen_config = _config(config) if config is not None else self._default_gradient_config
            reasons: list[str] = []
            if not HORIZON_SPECS[chosen_horizon].accepts_resolution(chosen_resolution):
                reasons.append(
                    f"{chosen_horizon.value} cannot use {chosen_resolution.value} data"
                )
            if len(series) < max(chosen_config.window_bars + 1, chosen_config.min_train_samples):
                reasons.append(
                    f"{len(series)} observations are below the configuration minimum"
                )
            try:
                latest = [
                    run
                    for run in self._forecast_store.training_runs(limit=100)
                    if str(run.get("kind")) == "gradient"
                    and str(run.get("instrument_id")) == instrument_id
                    and str(run.get("horizon")) == chosen_horizon.value
                ]
            except Exception:  # noqa: BLE001
                latest = []
            latest_report = (
                (latest[0].get("report") or {}) if latest else {}
            )
            latest_suitability = latest_report.get("suitability") or {}
            return {
                "ok": True,
                "instrument_id": instrument_id,
                "horizon": chosen_horizon.value,
                "resolution": chosen_resolution.value,
                "horizon_steps": steps,
                "observations": len(series),
                "eligible": not reasons,
                "reasons": reasons,
                "latest_evaluation": latest_suitability,
                "latest_run_id": latest[0].get("run_id") if latest else None,
                "available_backends": {
                    family.value: backend_available(family)
                    for family in GradientFamily
                },
                "automatic_promotion": False,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "instrument_id": instrument_id,
                "horizon": str(horizon),
                "eligible": False,
                "reasons": [f"{type(exc).__name__}: {exc}"],
                "automatic_promotion": False,
            }

    # -- observability -------------------------------------------------------

    def status(self) -> dict[str, Any]:
        base = super().status()
        base.update(
            {
                "phase": PI06_PHASE,
                "contract_version": GRADIENT_CONTRACT_VERSION,
                "schema_version": GRADIENT_SCHEMA_VERSION,
                "methodology_version": GRADIENT_METHODOLOGY_VERSION,
                "models": [family.value for family in GradientFamily],
                "backends": {
                    family.value: {
                        "available": backend_available(family),
                        "version": backend_version(family),
                    }
                    for family in GradientFamily
                },
                "artifact_root": str(self._artifacts.root),
                "automatic_promotion": False,
                "production_ready": False,
                "risk_authority": "NM-08",
                "execution_authority": "NM-10",
            }
        )
        return base

    def contract(self) -> dict[str, Any]:
        return {
            "phase": PI06_PHASE,
            "implemented": True,
            "contract_version": GRADIENT_CONTRACT_VERSION,
            "schema_version": GRADIENT_SCHEMA_VERSION,
            "methodology_version": GRADIENT_METHODOLOGY_VERSION,
            "families": {
                family.value: {
                    "available": backend_available(family),
                    "dependency_version": backend_version(family),
                    "regression": True,
                    "classification": True,
                    "serialization": "native booster format",
                }
                for family in GradientFamily
            },
            "horizons": {
                item.value: {
                    "family": HORIZON_SPECS[item].family.value,
                    "max_input_interval": HORIZON_SPECS[item].max_input_interval.value,
                    "min_history_bars": HORIZON_SPECS[item].min_history_bars,
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
                "profile_by_horizon": True,
                "missing_values": "NaN; never zero-filled implicitly",
                "schema": "ordered names persisted with the artifact",
                "selection": "training-only missingness and variance screen",
            },
            "leakage": {
                "chronological": True,
                "purged_boundaries": True,
                "test_used_for_selection": False,
                "preprocessing_fit_on_train_only": True,
                "fundamentals_news_policy": (
                    "PI-04 FeatureEngine owns publication-time/availability checks; "
                    "PI-06 passes only caller-supplied rows into that engine"
                ),
            },
            "authorities": {
                "risk": "NM-08",
                "execution": "NM-10",
                "market_data": "MarketDataEngine/MarketDataStore",
                "datasets": "PI-02 HistoricalDatasetService",
                "features": "PI-04 FeatureService/FeatureEngine",
                "predictions": "PredictionStore.prediction_forecasts",
            },
            "guarantees": [
                "features use observations at or before the cutoff",
                "the target is strictly later than the decision bar",
                "validation selects; test is reported once",
                "one failed family does not stop the other",
                "model identity is preserved in every forecast",
                "no automatic promotion, risk decision or execution",
            ],
        }


__all__ = ["GradientForecastingService"]


_ = math  # numeric import kept explicit for downstream maintenance
