"""Application-facing PI-05 statistical forecasting service.

The service owns orchestration, not a second market-data, dataset, feature,
prediction-result or evaluation authority:

* bars come from a PI-02 dataset version when one is named, otherwise from the
  existing market-data engine;
* regime labels come from the PI-04 feature/regime engine when it is attached;
* forecasts are persisted as the existing PI-01 ``ForecastOutput`` contract in
  ``PredictionStore.prediction_forecasts``;
* model metadata is registered in ``prediction_models``;
* evaluation history uses the existing ``ForecastStore`` training/performance
  tables.

A failed model is reported or omitted from a comparison; it never takes down the
whole service and its identity is never silently replaced.
"""

from __future__ import annotations

import hashlib
import inspect
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, cast
from zoneinfo import ZoneInfo

from novacontrol.novamarket.contracts import (
    ForecastDataError,
    ForecastNotFoundError,
    ForecastValidationError,
)
from novacontrol.novamarket.features.models import FeatureProfileName, FeatureRequest
from novacontrol.novamarket.forecasting.features import PriceBar
from novacontrol.novamarket.forecasting.store import (
    SCHEMA_VERSION as FORECAST_SCHEMA,
    ForecastStore,
)
from novacontrol.novamarket.marketdata.calendar import calendar_for
from novacontrol.novamarket.marketdata.models import (
    DataInterval,
    DataQuality,
    Freshness,
    Instrument,
    OhlcvBar,
)
from novacontrol.novamarket.prediction.models import (
    ConfirmationStage,
    CalibrationStatus,
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
    direction_for_return,
    prediction_id,
)
from novacontrol.novamarket.prediction.store import PredictionStore
from novacontrol.novamarket.statistical.estimators import (
    MODEL_IDS,
    MODEL_METADATA,
    forecast_model,
)
from novacontrol.novamarket.statistical.metrics import StatisticalMetrics
from novacontrol.novamarket.statistical.models import (
    PI05_PHASE,
    STATISTICAL_CONTRACT_VERSION,
    STATISTICAL_METHODOLOGY_VERSION,
    STATISTICAL_MODEL_VERSION,
    STATISTICAL_SCHEMA_VERSION,
    FitStatus,
    IntervalMethod,
    ModelConfig,
    ModelFit,
    ModelForecast,
    StatisticalForecast,
    StatisticalForecastRequest,
    StatisticalModelError,
    StatisticalValidationError,
    SuitabilityReport,
    config_candidates,
    decimal_price,
    normalise_model_id,
)
from novacontrol.novamarket.statistical.validation import (
    EvaluationReport,
    default_resolution,
    evaluate_models,
    horizon_steps,
)

Publisher = Callable[..., Any]
RegimeLookup = Callable[[datetime], str | None]

_INVALID_QUALITY = {"invalid", "rejected", "suspect", "unavailable"}
#: Hard bound on the history a single fit/evaluation may inspect.  The most
#: recent rows are kept; the bound prevents an accidental full-history refit.
MAX_HISTORY_BARS = 5_000
_STATISTICAL_MODEL_IDS = frozenset(MODEL_IDS)


@dataclass(frozen=True, slots=True)
class _ServiceBar(PriceBar):
    """A PI-05 read bar carrying the source quality/freshness provenance."""

    quality: DataQuality = DataQuality.VALID
    freshness: Freshness = Freshness.UNKNOWN


def _float(value: object, name: str) -> float:
    try:
        number = float(str(value))
    except (TypeError, ValueError) as exc:
        raise StatisticalValidationError(f"{name} is not numeric: {value!r}") from exc
    if not math.isfinite(number):
        raise StatisticalValidationError(f"{name} must be finite")
    return number


def _aware(value: object, name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise StatisticalValidationError(f"{name} is not an ISO timestamp") from exc
    if not isinstance(value, datetime):
        raise StatisticalValidationError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise StatisticalValidationError(
            f"{name} must be timezone-aware; a naive timestamp cannot be used"
        )
    return value.astimezone(UTC)


def _bar_from_any(value: Any) -> _ServiceBar:
    if isinstance(value, _ServiceBar):
        return value
    if isinstance(value, PriceBar):
        return _ServiceBar(
            observed_at=value.observed_at,
            open=value.open,
            high=value.high,
            low=value.low,
            close=value.close,
            volume=value.volume,
        )
    if isinstance(value, OhlcvBar):
        return _ServiceBar(
            observed_at=value.observed_at,
            open=float(value.open),
            high=float(value.high),
            low=float(value.low),
            close=float(value.close),
            volume=float(value.volume) if value.volume is not None else None,
            quality=DataQuality(value.quality),
            freshness=Freshness(value.freshness),
        )
    if isinstance(value, Mapping):
        quality = str(value.get("quality") or "valid").lower()
        freshness = str(value.get("freshness") or "unknown").lower()
        try:
            quality_value = DataQuality(quality)
        except ValueError:
            quality_value = DataQuality.INVALID
        try:
            freshness_value = Freshness(freshness)
        except ValueError:
            freshness_value = Freshness.UNKNOWN
        return _ServiceBar(
            observed_at=_aware(value.get("observed_at"), "observed_at"),
            open=_float(value.get("open"), "open"),
            high=_float(value.get("high"), "high"),
            low=_float(value.get("low"), "low"),
            close=_float(value.get("close"), "close"),
            volume=(
                None
                if value.get("volume") is None
                else _float(value.get("volume"), "volume")
            ),
            quality=quality_value,
            freshness=freshness_value,
        )
    try:
        observed = value.observed_at
        open_value = value.open
        high = value.high
        low = value.low
        close = value.close
        volume = getattr(value, "volume", None)
        quality_value = DataQuality(str(getattr(value, "quality", "valid")))
        freshness_value = Freshness(str(getattr(value, "freshness", "unknown")))
        return _ServiceBar(
            observed_at=_aware(observed, "observed_at"),
            open=float(open_value),
            high=float(high),
            low=float(low),
            close=float(close),
            volume=None if volume is None else float(volume),
            quality=quality_value,
            freshness=freshness_value,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise StatisticalValidationError(
            f"a price observation has no usable OHLC fields: {type(value).__name__}"
        ) from exc


def _horizon(value: ForecastHorizon | str) -> ForecastHorizon:
    try:
        return ForecastHorizon(value)
    except ValueError as exc:
        known = ", ".join(item.value for item in ForecastHorizon)
        raise StatisticalValidationError(
            f"unknown forecast horizon {value!r}; known: {known}"
        ) from exc


def _as_config(value: ModelConfig | Mapping[str, Any] | None) -> ModelConfig:
    """Accept a typed config or the JSON object used by the API."""
    if value is None:
        return ModelConfig()
    if isinstance(value, ModelConfig):
        return value
    return ModelConfig.from_dict(value)


def _page_items(page: Any) -> Sequence[Any]:
    """Accept the project's page object, a page mapping, or a plain sequence."""
    if isinstance(page, Mapping):
        value = page.get("items", ())
    else:
        value = getattr(page, "items", None)
        if value is None and isinstance(page, (list, tuple)):
            value = page
    if value is None or isinstance(value, (str, bytes)):
        return ()
    return cast(Sequence[Any], value)


def _usable(bar: PriceBar, cutoff: datetime) -> bool:
    if bar.observed_at > cutoff:
        return False
    if not all(
        math.isfinite(value) and value > 0
        for value in (bar.open, bar.high, bar.low, bar.close)
    ):
        return False
    if bar.high < bar.low or not bar.low <= bar.close <= bar.high:
        return False
    return True


class StatisticalForecastingService:
    """PI-05 orchestration over the existing NovaMarket authorities."""

    def __init__(
        self,
        *,
        store_path: str | Path | None = None,
        market_data: Any | None = None,
        dataset_service: Any | None = None,
        feature_service: Any | None = None,
        prediction_store: PredictionStore | None = None,
        forecast_store: ForecastStore | None = None,
        publisher: Publisher | None = None,
        clock: Callable[[], datetime] | None = None,
        default_config: ModelConfig | None = None,
    ) -> None:
        self._market_data = market_data
        self._dataset_service = dataset_service
        self._feature_service = feature_service
        self._owns_prediction_store = prediction_store is None
        self._owns_forecast_store = forecast_store is None
        self._prediction_store = prediction_store or PredictionStore(
            store_path or ":memory:"
        )
        self._forecast_store = forecast_store or ForecastStore(
            str(store_path or ":memory:"), clock=clock
        )
        self._publisher = publisher
        self._clock = clock or (lambda: datetime.now(UTC))
        self._default_config = default_config or ModelConfig()
        self._last_error = ""
        self._generations = 0
        self._failures = 0
        self._evaluations = 0
        self._fit_cache_hits = 0
        self._fit_cache: dict[str, ModelForecast] = {}
        self._failure_reports: list[dict[str, str]] = []

    @property
    def prediction_store(self) -> PredictionStore:
        return self._prediction_store

    @property
    def forecast_store(self) -> ForecastStore:
        return self._forecast_store

    @property
    def failure_reports(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(item) for item in self._failure_reports[-20:])

    def close(self) -> None:
        # The application shares these two stores with PI-01/NM-06.  Closing a
        # borrowed store here would make an unrelated subsystem fail its next
        # read, so only stores this service opened are closed.
        if self._owns_prediction_store:
            self._prediction_store.close()
        if self._owns_forecast_store:
            self._forecast_store.close()

    # -- events ---------------------------------------------------------------

    async def _publish(self, name: str, **payload: Any) -> None:
        if self._publisher is None:
            return
        try:
            outcome = self._publisher(name, payload)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as exc:  # noqa: BLE001 - a stored forecast stands
            self._last_error = f"{name}: {type(exc).__name__}: {exc}"

    def _record_failure(self, model_id: str, stage: str, exc: Exception) -> None:
        self._failures += 1
        self._last_error = f"{stage}/{model_id}: {type(exc).__name__}: {exc}"
        self._failure_reports.append(
            {
                "model_id": model_id,
                "stage": stage,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        if len(self._failure_reports) > 100:
            del self._failure_reports[:-100]

    # -- data -----------------------------------------------------------------

    def _instrument_info(self, instrument_id: str) -> dict[str, Any]:
        if self._market_data is None:
            exchange = instrument_id.split(":", 1)[0] if ":" in instrument_id else ""
            return {
                "instrument_id": instrument_id,
                "exchange": exchange,
                "currency": "INR" if exchange in {"XNSE", "XBOM"} else "USD",
            }
        try:
            instrument = self._market_data.get_instrument(instrument_id)
        except Exception as exc:  # noqa: BLE001
            raise ForecastNotFoundError(
                f"unknown instrument {instrument_id!r}"
            ) from exc
        if hasattr(instrument, "to_dict"):
            body = instrument.to_dict()
        elif isinstance(instrument, Mapping):
            body = dict(instrument)
        else:
            body = {
                "instrument_id": getattr(instrument, "instrument_id", instrument_id),
                "exchange": getattr(instrument, "exchange", ""),
                "currency": getattr(instrument, "currency", ""),
            }
        body.setdefault("instrument_id", instrument_id)
        body.setdefault("exchange", instrument_id.split(":", 1)[0])
        body.setdefault(
            "currency",
            "INR" if str(body.get("exchange", "")).upper() in {"XNSE", "XBOM"} else "USD",
        )
        return dict(body)

    def _bars_from_items(
        self, items: Sequence[Any], cutoff: datetime
    ) -> list[PriceBar]:
        accepted: dict[str, PriceBar] = {}
        for item in items:
            if getattr(item, "partial", False):
                continue
            quality = str(
                getattr(item, "quality", "valid")
                if not isinstance(item, Mapping)
                else item.get("quality", "valid")
            ).lower()
            if quality in _INVALID_QUALITY:
                continue
            if isinstance(item, Mapping) and item.get("missing"):
                continue
            try:
                bar = _bar_from_any(item)
            except (StatisticalValidationError, TypeError, ValueError):
                # A malformed observation is excluded with the rest of the
                # order flow; it must not abort the whole forecast request.
                continue
            if bar.quality in {DataQuality.INVALID, DataQuality.UNAVAILABLE}:
                continue
            if bar.freshness in {Freshness.MISSING, Freshness.PROVIDER_UNAVAILABLE}:
                continue
            if not _usable(bar, cutoff):
                continue
            accepted[bar.observed_at.isoformat()] = bar
        return [accepted[key] for key in sorted(accepted)]

    def _dataset_bars(
        self, request: StatisticalForecastRequest
    ) -> tuple[list[PriceBar], DataInterval, str, str] | None:
        if not request.dataset_id:
            return None
        if self._dataset_service is None:
            raise ForecastDataError(
                f"dataset {request.dataset_id!r} was requested but the dataset "
                "engine is not attached"
            )
        try:
            metadata = self._dataset_service.metadata(
                request.dataset_id, request.dataset_version or None
            )
            version = str(metadata.get("version") or request.dataset_version or "")
            if not version:
                raise ForecastDataError(
                    f"dataset {request.dataset_id!r} did not report a version"
                )
            if metadata.get("usable_for_training") is False:
                raise ForecastDataError(
                    f"dataset {request.dataset_id}:{version} is "
                    f"not usable for training (quality "
                    f"{metadata.get('quality', 'unknown')!r}); it cannot be used "
                    "as a validated forecast input"
                )
            resolution = DataInterval(str(metadata.get("resolution") or ""))
            if request.resolution is not None and request.resolution != resolution:
                requested_resolution = (
                    request.resolution.value
                    if isinstance(request.resolution, DataInterval)
                    else request.resolution
                )
                raise StatisticalValidationError(
                    f"dataset resolution {resolution.value} does not match the "
                    f"requested resolution {requested_resolution}"
                )
            page = self._dataset_service.bars(
                request.dataset_id,
                version,
                instrument_id=request.instrument_id,
                start=None,
                end=request.as_of,
                limit=5_000,
                # The newest bounded window matters most for a forecast; the
                # adapter sorts the selected rows back into chronological order.
                ascending=False,
                adjusted=request.config.adjusted,
            )
        except StatisticalValidationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ForecastDataError(
                f"dataset bars are unavailable: {type(exc).__name__}: {exc}"
            ) from exc
        bars = self._bars_from_items(_page_items(page), request.as_of)
        if not bars:
            raise ForecastDataError(
                f"dataset {request.dataset_id}:{version} has no usable "
                f"{request.instrument_id} bars at or before {request.as_of.isoformat()}"
            )
        return (
            bars,
            resolution,
            str(metadata.get("dataset_id") or request.dataset_id),
            version,
        )

    def _market_bars(
        self, request: StatisticalForecastRequest, resolution: DataInterval
    ) -> list[PriceBar]:
        if self._market_data is None:
            raise ForecastDataError("market data is not initialised")
        limit = MAX_HISTORY_BARS
        try:
            try:
                page = self._market_data.bars(
                    request.instrument_id,
                    interval=resolution,
                    end=request.as_of,
                    limit=limit,
                    ascending=False,
                )
            except TypeError:
                # Compatibility with an older injected engine that predates
                # the interval/end keywords.  The cutoff is still enforced by
                # _bars_from_items below; never widen past request.as_of.
                page = self._market_data.bars(
                    request.instrument_id, limit=limit, ascending=False
                )
        except Exception as exc:  # noqa: BLE001
            raise ForecastDataError(
                f"historical bars are unavailable: {type(exc).__name__}"
            ) from exc
        bars = self._bars_from_items(_page_items(page), request.as_of)
        if not bars:
            raise ForecastDataError(
                f"{request.instrument_id} has no usable {resolution.value} bars at "
                f"or before {request.as_of.isoformat()}"
            )
        return bars

    def _series(
        self, request: StatisticalForecastRequest
    ) -> tuple[list[PriceBar], DataInterval, str, str]:
        dataset = self._dataset_bars(request)
        if dataset is not None:
            return dataset
        resolution: DataInterval = (
            request.resolution
            if isinstance(request.resolution, DataInterval)
            else DataInterval(request.resolution)
            if request.resolution
            else default_resolution(request.horizon)
        )
        if request.bars:
            bars = self._bars_from_items(request.bars, request.as_of)
            if not bars:
                raise ForecastDataError("the supplied bars contain no usable observations")
            bars = bars[-MAX_HISTORY_BARS:]
            return (
                bars,
                resolution,
                request.dataset_id or "market-data",
                request.dataset_version or "market-data",
            )
        bars = self._market_bars(request, resolution)
        return bars, resolution, request.dataset_id or "market-data", request.dataset_version or "market-data"

    # -- target and contract --------------------------------------------------

    def _calendar(self, instrument_id: str) -> Any | None:
        if self._market_data is None:
            return None
        try:
            instrument = self._market_data.get_instrument(instrument_id)
            return calendar_for(instrument) if isinstance(instrument, Instrument) else None
        except Exception:  # noqa: BLE001
            return None

    def _target_at(
        self,
        instrument_id: str,
        cutoff: datetime,
        horizon: ForecastHorizon,
        steps: int,
    ) -> datetime:
        calendar = self._calendar(instrument_id)
        if calendar is None:
            duration = HORIZON_SPECS[horizon].duration
            if horizon in {
                ForecastHorizon.DAY_1,
                ForecastHorizon.DAYS_3,
                ForecastHorizon.DAYS_5,
                ForecastHorizon.DAYS_30,
                ForecastHorizon.DAYS_90,
            }:
                duration = timedelta(days=steps)
            return cutoff + duration
        zone = ZoneInfo(calendar.timezone)
        local = cutoff.astimezone(zone)
        if horizon in {
            ForecastHorizon.DAY_1,
            ForecastHorizon.DAYS_3,
            ForecastHorizon.DAYS_5,
            ForecastHorizon.DAYS_30,
            ForecastHorizon.DAYS_90,
        }:
            if horizon in {
                ForecastHorizon.DAY_1,
                ForecastHorizon.DAYS_3,
                ForecastHorizon.DAYS_5,
            }:
                # Swing targets are verified sessions, not elapsed weekdays.
                counted = 0
                for offset in range(1, 400):
                    day = local.date() + timedelta(days=offset)
                    bounds = calendar.session_bounds(day)
                    if bounds is None:
                        continue
                    counted += 1
                    if counted >= steps:
                        close = _aware(bounds[1], "exchange session close")
                        return close.astimezone(UTC)
            else:
                # Long-term targets are elapsed calendar days, then the first
                # verified session close at or after that date.
                required_day = local.date() + timedelta(days=steps)
                for offset in range(0, 20):
                    day = required_day + timedelta(days=offset)
                    bounds = calendar.session_bounds(day)
                    if bounds is not None:
                        close = _aware(bounds[1], "exchange session close")
                        return close.astimezone(UTC)
            raise ForecastDataError("no exchange session is available for the target date")
        seconds = max(
            1,
            int(HORIZON_SPECS[horizon].duration.total_seconds()),
        )
        local = cutoff.astimezone(zone)
        remaining = seconds
        for _ in range(0, 30):
            day = local.date()
            bounds = calendar.session_bounds(day)
            if bounds is None:
                local = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=zone)
                continue
            opening, closing = bounds
            if local < opening:
                local = opening
            if local >= closing:
                local = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=zone)
                continue
            available = max(0, int((closing - local).total_seconds()))
            if remaining <= available:
                return (local + timedelta(seconds=remaining)).astimezone(UTC)
            remaining -= available
            local = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=zone)
        raise ForecastDataError("no exchange session is available for the forecast target")

    def _suitability(
        self,
        request: StatisticalForecastRequest,
        bars: Sequence[PriceBar],
        resolution: DataInterval,
        steps: int,
    ) -> SuitabilityReport:
        reasons: list[str] = []
        spec = HORIZON_SPECS[request.horizon]
        if not spec.accepts_resolution(resolution):
            reasons.append(
                f"{request.horizon.value} cannot use {resolution.value} input; "
                f"the finest permitted input is {spec.max_input_interval.value}"
            )
        required = max(
            spec.min_history_bars,
            request.config.min_observations,
            # A model cannot forecast a target whose step count exceeds the
            # available history: 80 one-minute bars are not one daily bar of
            # history for a 1d/1m forecast.
            steps,
        )
        if len(bars) < required:
            reasons.append(
                f"{len(bars)} observations; at least {required} are required "
                f"(horizon needs {steps} model steps)"
            )
        if steps < 1:
            reasons.append("the horizon maps to fewer than one model step")
        return SuitabilityReport(
            instrument_id=request.instrument_id,
            horizon=request.horizon,
            resolution=resolution,
            observations=len(bars),
            eligible=not reasons,
            reasons=tuple(reasons),
            requirements={
                "minimum_observations": required,
                "horizon_steps": steps,
                "accepted_resolution": spec.max_input_interval.value,
                "dataset_required": False,
            },
        )

    def _feature_bars(
        self,
        bars: Sequence[PriceBar],
        instrument_id: str,
        resolution: DataInterval,
        currency: str = "",
    ) -> list[OhlcvBar]:
        """Adapt the PI-05 read model to PI-04's existing OHLCV observation type."""
        return [
            OhlcvBar(
                bar_id=f"pi05-{bar.observed_at.isoformat()}",
                instrument_id=instrument_id,
                provider="pi05-point-in-time",
                interval=resolution,
                open=Decimal(str(bar.open)),
                high=Decimal(str(bar.high)),
                low=Decimal(str(bar.low)),
                close=Decimal(str(bar.close)),
                currency=currency or "USD",
                observed_at=bar.observed_at,
                retrieved_at=bar.observed_at,
                source_timezone="UTC",
                volume=None if bar.volume is None else Decimal(str(bar.volume)),
                quality=getattr(bar, "quality", DataQuality.VALID),
                freshness=getattr(bar, "freshness", Freshness.UNKNOWN),
            )
            for bar in bars
        ]

    def _feature_context(
        self,
        request: StatisticalForecastRequest,
        bars: Sequence[PriceBar],
        resolution: DataInterval,
    ) -> tuple[str, str, str | None]:
        if self._feature_service is None:
            return request.feature_set_version, request.feature_set_id, None
        try:
            profile = (
                FeatureProfileName.INTRADAY
                if HORIZON_SPECS[request.horizon].family.value == "intraday"
                else FeatureProfileName.SWING
                if request.horizon in {
                    ForecastHorizon.DAY_1,
                    ForecastHorizon.DAYS_3,
                    ForecastHorizon.DAYS_5,
                }
                else FeatureProfileName.LONG_TERM
            )
            currency = request.currency
            if not currency:
                try:
                    currency = str(
                        self._instrument_info(request.instrument_id).get("currency") or ""
                    )
                except Exception:  # noqa: BLE001 - currency is provenance only
                    currency = ""
            feature_request = FeatureRequest(
                instrument_id=request.instrument_id,
                as_of=request.as_of,
                resolution=resolution.value,
                profile=profile,
                timeframes=(),
                include_context=False,
                include_fundamentals=False,
                include_news=False,
                max_bars=max(30, min(len(bars), 500)),
            )
            result = self._feature_service.engine.generate(
                feature_request,
                bars={
                    resolution.value: self._feature_bars(
                        bars,
                        request.instrument_id,
                        resolution,
                        currency,
                    )
                },
            )
            regime = str(result.regime.primary)
            return result.feature_set.version, result.feature_set.feature_set_id, regime
        except Exception as exc:  # noqa: BLE001 - feature context is optional
            self._last_error = f"feature context: {type(exc).__name__}: {exc}"
            return request.feature_set_version, request.feature_set_id, None

    def _register_model(
        self,
        forecast: ModelForecast,
        *,
        cutoff: datetime,
        metrics: Mapping[str, Any] | None = None,
        feature_set_version: str = "unspecified",
        dataset_version: str = "dataset-version-is-recorded-per-forecast",
        instrument_id: str = "",
    ) -> ModelMetadata:
        existing_metrics: dict[str, Any] = {}
        try:
            # Model identity is (model_id, version), not instrument.  A later
            # ordinary forecast must not erase the validation/test evidence
            # stored by an explicit evaluation.
            for item in self._prediction_store.models():
                if (
                    item.model_id == forecast.model_id
                    and item.version == STATISTICAL_MODEL_VERSION
                ):
                    existing_metrics = dict(item.metrics)
                    break
        except Exception:  # noqa: BLE001 - a metadata read is best effort
            existing_metrics = {}
        instruments = {
            str(item)
            for item in (existing_metrics.get("instruments") or ())
            if str(item)
        }
        if existing_metrics.get("instrument_id"):
            instruments.add(str(existing_metrics["instrument_id"]))
        if instrument_id:
            instruments.add(instrument_id)
        metadata = ModelMetadata(
            model_id=forecast.model_id,
            version=STATISTICAL_MODEL_VERSION,
            family=forecast.family,
            state=ModelState.ELIGIBLE,
            supported_horizons=tuple(ForecastHorizon),
            calibration=CalibrationStatus.UNCALIBRATED,
            metrics={
                **existing_metrics,
                "config": forecast.config.to_dict(),
                "config_digest": forecast.config.digest,
                "fit": forecast.fit.to_dict(),
                "interval_method": forecast.interval_method.value,
                "dependency_version": "python-stdlib",
                "instrument_id": instrument_id or None,
                "instruments": sorted(instruments),
                **(dict(metrics or {})),
            },
            trained_at=cutoff,
            updated_at=cutoff,
            notes="PI-05 statistical baseline; no automatic promotion",
            feature_set_version=feature_set_version,
            dataset_version=dataset_version,
        )
        self._prediction_store.register_model(metadata)
        return metadata

    def _register_unavailable(
        self,
        model_id: str,
        *,
        cutoff: datetime,
        reason: str,
        instrument_id: str = "",
    ) -> None:
        """Persist a failed fit as an explicitly unavailable model, not a gap."""
        from novacontrol.novamarket.statistical.models import model_family

        existing_metrics: dict[str, Any] = {}
        existing_feature_version = "unspecified"
        existing_dataset_version = "unspecified"
        try:
            for item in self._prediction_store.models():
                if item.model_id == model_id and item.version == STATISTICAL_MODEL_VERSION:
                    existing_metrics = dict(item.metrics)
                    existing_feature_version = item.feature_set_version
                    existing_dataset_version = item.dataset_version
                    break
        except Exception:  # noqa: BLE001 - failure reporting is best effort
            existing_metrics = {}
        instruments = {
            str(item)
            for item in (existing_metrics.get("instruments") or ())
            if str(item)
        }
        if existing_metrics.get("instrument_id"):
            instruments.add(str(existing_metrics["instrument_id"]))
        if instrument_id:
            instruments.add(instrument_id)
        metadata = ModelMetadata(
            model_id=model_id,
            version=STATISTICAL_MODEL_VERSION,
            family=model_family(model_id),
            state=ModelState.UNAVAILABLE,
            supported_horizons=tuple(ForecastHorizon),
            calibration=CalibrationStatus.UNCALIBRATED,
            metrics={
                **existing_metrics,
                "fit_status": FitStatus.FAILED.value,
                "failure": reason,
                "instrument_id": instrument_id or None,
                "instruments": sorted(instruments),
            },
            trained_at=cutoff,
            updated_at=cutoff,
            notes="PI-05 fit failed; this model is unavailable for this evidence run",
            feature_set_version=existing_feature_version,
            dataset_version=existing_dataset_version,
        )
        try:
            self._prediction_store.register_model(metadata)
        except Exception as exc:  # noqa: BLE001 - failure reporting is best effort
            self._record_failure(model_id, "register_unavailable", exc)

    def _build_output(
        self,
        request: StatisticalForecastRequest,
        forecast: ModelForecast,
        bars: Sequence[PriceBar],
        resolution: DataInterval,
        dataset_id: str,
        dataset_version: str,
    ) -> ForecastOutput:
        last = bars[-1]
        steps = horizon_steps(request.horizon, resolution)
        target = self._target_at(
            request.instrument_id, request.as_of, request.horizon, steps
        )
        if target <= request.as_of:
            target = request.as_of + HORIZON_SPECS[request.horizon].duration
        if target <= request.as_of:
            raise ForecastDataError("the forecast target is not after its generation time")
        interval_supported = forecast.interval_supported
        lower_return = (
            float(forecast.lower_return)
            if forecast.lower_return is not None
            else forecast.predicted_return
        )
        upper_return = (
            float(forecast.upper_return)
            if forecast.upper_return is not None
            else forecast.predicted_return
        )
        lower_return = min(lower_return, forecast.predicted_return)
        upper_return = max(upper_return, forecast.predicted_return)
        lower_price = last.close * (1.0 + lower_return)
        upper_price = last.close * (1.0 + upper_return)
        if lower_price <= 0:
            lower_return = -0.999999
            lower_price = last.close * (1.0 + lower_return)
        if upper_price <= 0:
            raise ForecastDataError("a statistical model produced a non-positive bound")
        width = upper_return - lower_return
        if not interval_supported:
            uncertainty = PredictionUncertainty.UNKNOWN
        elif width <= 0.02:
            uncertainty = PredictionUncertainty.LOW
        elif width >= 0.08:
            uncertainty = PredictionUncertainty.HIGH
        else:
            uncertainty = PredictionUncertainty.MEDIUM
        try:
            info = self._instrument_info(request.instrument_id)
        except ForecastNotFoundError:
            # A point-in-time bars/dataset evaluation may carry its own
            # observations for a symbol the live catalogue has not ingested.
            # The exchange is still explicit; never invent prices from it.
            if not (request.bars or request.dataset_id):
                raise
            exchange = request.exchange or request.instrument_id.split(":", 1)[0]
            info = {
                "instrument_id": request.instrument_id,
                "exchange": exchange,
                "currency": request.currency
                or ("INR" if exchange in {"XNSE", "XBOM"} else "USD"),
            }
        quality_value = getattr(last, "quality", DataQuality.VALID)
        try:
            quality = DataQuality(str(quality_value))
        except ValueError:
            quality = DataQuality.INVALID
        freshness_value = getattr(last, "freshness", Freshness.UNKNOWN)
        try:
            freshness = Freshness(str(freshness_value))
        except ValueError:
            freshness = Freshness.UNKNOWN
        if quality in {DataQuality.INVALID, DataQuality.UNAVAILABLE} or freshness in {
            Freshness.MISSING,
            Freshness.PROVIDER_UNAVAILABLE,
        }:
            validity = ForecastValidity.INVALID
        elif quality is DataQuality.STALE or freshness is Freshness.STALE:
            validity = ForecastValidity.STALE
        else:
            validity = ForecastValidity.VALID
        payload_digest = digest(
            {
                "methodology": STATISTICAL_METHODOLOGY_VERSION,
                "model": forecast.model_id,
                "model_version": STATISTICAL_MODEL_VERSION,
                "config": forecast.config.to_dict(),
                "dataset_id": dataset_id,
                "dataset_version": dataset_version,
                "feature_set_version": request.feature_set_version,
                "exchange": request.exchange,
                "currency": request.currency,
                "resolution": resolution.value,
                "as_of": request.as_of.isoformat(),
                "bars": [
                    [
                        bar.observed_at.isoformat(),
                        round(bar.open, 10),
                        round(bar.high, 10),
                        round(bar.low, 10),
                        round(bar.close, 10),
                        None if bar.volume is None else round(bar.volume, 10),
                        str(getattr(bar, "quality", DataQuality.VALID)),
                        str(getattr(bar, "freshness", Freshness.UNKNOWN)),
                    ]
                    for bar in bars
                ],
                "predicted_return": forecast.predicted_return,
                "lower_return": lower_return,
                "upper_return": upper_return,
            }
        )
        forecast_id = prediction_id(
            "sft_",
            request.instrument_id,
            request.horizon.value,
            request.as_of.isoformat(),
            forecast.model_id,
            forecast.config.digest,
            dataset_id,
            dataset_version,
            payload_digest,
        )
        # The direction is the model's explicit PI-05 rule: a model whose
        # interval straddles zero reports UNCERTAIN instead of inventing UP/DOWN.
        direction = forecast.direction
        return ForecastOutput(
            forecast_id=forecast_id,
            lifecycle_id=prediction_id(
                "sfl_", "statistical", forecast_id
            ),
            lifecycle_stage=ConfirmationStage.INITIAL,
            instrument_id=request.instrument_id,
            exchange=str(info.get("exchange") or request.exchange),
            currency=str(info.get("currency") or request.currency or "USD"),
            reference_price=decimal_price(last.close),
            reference_at=last.observed_at,
            predicted_price=decimal_price(forecast.predicted_price),
            predicted_return=forecast.predicted_return,
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
            ensemble_id="statistical-baseline",
            model_id=forecast.model_id,
            model_version=STATISTICAL_MODEL_VERSION,
            member_versions=(STATISTICAL_MODEL_VERSION,),
            forecast_horizon=request.horizon,
            target_at=target,
            generated_at=request.as_of,
            data_observed_at=last.observed_at,
            data_received_at=request.as_of,
            data_resolution=resolution,
            data_freshness=freshness,
            data_quality=quality,
            data_snapshot_id="sb_" + payload_digest[:24],
            validity=validity,
            validity_conditions=(
                "generated from bars observed at or before the data cutoff",
                "no calibrated probability is claimed",
                "the interval is a statistical estimate, not a guarantee",
                "no automatic model promotion or execution authority",
            ),
            disagreement=0.0,
            digest=payload_digest,
            notes=tuple(forecast.notes)
            + (
                f"dataset={dataset_id}:{dataset_version}",
                f"model_config={forecast.config.digest}",
            ),
            metadata={
                "statistical_contract_version": STATISTICAL_CONTRACT_VERSION,
                "model_family": forecast.family.value,
                "model_config": forecast.config.to_dict(),
                "fit": forecast.fit.to_dict(),
                "interval_method": forecast.interval_method.value,
                "direction_rule": forecast.direction_rule,
                "dataset_id": dataset_id,
                "dataset_version": dataset_version,
                "feature_set_id": request.feature_set_id,
                "horizon_steps": steps,
                "adjusted": request.config.adjusted,
                "raw_interval_supported": interval_supported,
            },
            feature_set_version=request.feature_set_version,
            dataset_version=dataset_version,
        )

    def _selected_statistical_choice(
        self, instrument_id: str, horizon: str
    ) -> tuple[str, ModelConfig | None]:
        """Read the latest PI-05 model/config without touching NM-06's slot."""
        try:
            for run in self._forecast_store.training_runs(limit=100):
                if (
                    str(run.get("instrument_id") or "") != instrument_id
                    or str(run.get("horizon") or "") != horizon
                    or str(run.get("kind") or "") != "statistical"
                ):
                    continue
                candidate = str(
                    run.get("selected_model") or run.get("model_id") or ""
                )
                if candidate not in _STATISTICAL_MODEL_IDS:
                    continue
                config_payload = run.get("selected_config")
                if not isinstance(config_payload, Mapping):
                    report = run.get("report")
                    if isinstance(report, Mapping):
                        config_payload = report.get("selected_config")
                selected_config: ModelConfig | None = None
                if isinstance(config_payload, Mapping):
                    try:
                        selected_config = ModelConfig.from_dict(config_payload)
                    except StatisticalValidationError:
                        selected_config = None
                return candidate, selected_config
        except Exception:  # noqa: BLE001 - selection is an optional read
            pass
        # Backwards compatibility for an older PI-05 selection row; never
        # accept an NM-06 model id from the shared table.
        try:
            legacy = self._forecast_store.selection(instrument_id, horizon)
        except Exception:  # noqa: BLE001
            legacy = None
        return (legacy, None) if legacy in _STATISTICAL_MODEL_IDS else ("", None)

    def _selected_statistical_model(self, instrument_id: str, horizon: str) -> str:
        return self._selected_statistical_choice(instrument_id, horizon)[0]

    def _forecast(
        self,
        request: StatisticalForecastRequest,
        *,
        model_id: str | None = None,
    ) -> tuple[
        ForecastOutput,
        ModelForecast,
        StatisticalForecast,
        list[PriceBar],
        DataInterval,
        bool,
    ]:
        bars, resolution, dataset_id, dataset_version = self._series(request)
        steps = horizon_steps(request.horizon, resolution)
        chosen_model = (
            normalise_model_id(model_id)
            if model_id
            else request.model_id
            or self._selected_statistical_model(
                request.instrument_id, request.horizon.value
            )
            or "random_walk"
        )
        suitability = self._suitability(request, bars, resolution, steps)
        if not suitability.eligible:
            raise ForecastDataError(
                f"{request.instrument_id} {request.horizon.value} is not suitable: "
                + "; ".join(suitability.reasons)
            )
        cache_key = digest(
            {
                "model": chosen_model,
                "version": STATISTICAL_MODEL_VERSION,
                "config": request.config.digest,
                "steps": steps,
                "bars": [
                    [bar.observed_at.isoformat(), bar.close] for bar in bars
                ],
            }
        )
        forecast = self._fit_cache.get(cache_key)
        if forecast is not None:
            self._fit_cache_hits += 1
        else:
            try:
                forecast = forecast_model(
                    chosen_model,
                    [bar.close for bar in bars],
                    steps,
                    request.config,
                    bars=bars,
                )
            except Exception as exc:
                self._record_failure(chosen_model, "forecast", exc)
                if chosen_model in _STATISTICAL_MODEL_IDS:
                    self._register_unavailable(
                        chosen_model,
                        cutoff=request.as_of,
                        reason=f"{type(exc).__name__}: {exc}",
                        instrument_id=request.instrument_id,
                    )
                if isinstance(exc, StatisticalModelError):
                    raise
                raise StatisticalModelError(
                    f"{chosen_model} could not produce a forecast: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            self._fit_cache[cache_key] = forecast
            if len(self._fit_cache) > 64:
                self._fit_cache.pop(next(iter(self._fit_cache)))
        feature_version, feature_id, regime = self._feature_context(
            request, bars, resolution
        )
        if feature_id or feature_version != request.feature_set_version:
            request = StatisticalForecastRequest(
                instrument_id=request.instrument_id,
                horizon=request.horizon,
                as_of=request.as_of,
                exchange=request.exchange,
                currency=request.currency,
                resolution=request.resolution,
                model_id=request.model_id,
                config=request.config,
                dataset_id=request.dataset_id,
                dataset_version=request.dataset_version,
                feature_set_version=feature_version,
                feature_set_id=feature_id or request.feature_set_id,
                bars=request.bars,
                notes=request.notes,
            )
        output = self._build_output(
            request, forecast, bars, resolution, dataset_id, dataset_version
        )
        try:
            self._register_model(
                forecast,
                cutoff=request.as_of,
                feature_set_version=request.feature_set_version,
                dataset_version=dataset_version,
                instrument_id=request.instrument_id,
            )
        except Exception as exc:  # noqa: BLE001 - metadata is evidence, not the result
            self._record_failure(chosen_model, "register_model", exc)
        stored = False
        try:
            stored = self._prediction_store.save_forecast(output)
        except Exception as exc:  # noqa: BLE001 - the result remains readable
            self._record_failure(chosen_model, "persist", exc)
        if not stored:
            # A duplicate deterministic id is already durable; a failed insert
            # is not.  Report what the store actually contains, not an assumption.
            try:
                stored = (
                    self._prediction_store.get_forecast(output.forecast_id) is not None
                )
            except Exception:  # noqa: BLE001
                stored = False
        statistical = StatisticalForecast(
            contract=output,
            fit=forecast.fit,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            feature_set_version=request.feature_set_version,
            interval_level=forecast.interval_level,
            interval_method=forecast.interval_method,
            interval_supported=forecast.interval_supported,
            suitability=suitability,
            notes=tuple(forecast.notes),
        )
        self._generations += 1
        return output, forecast, statistical, bars, resolution, stored

    async def generate(
        self,
        instrument_id: str,
        horizon: ForecastHorizon | str,
        *,
        as_of: datetime | None = None,
        model_id: str = "",
        config: ModelConfig | Mapping[str, Any] | None = None,
        resolution: DataInterval | str | None = None,
        dataset_id: str = "",
        dataset_version: str = "",
        feature_set_version: str = "unspecified",
        feature_set_id: str = "",
        exchange: str = "",
        currency: str = "",
        bars: Sequence[Any] | None = None,
    ) -> dict[str, Any]:
        """Generate one forecast, persist the PI-01 contract and return it."""
        failures_before = self._failures
        try:
            chosen_horizon = _horizon(horizon)
            selected_model = ""
            chosen_config = (
                _as_config(config) if config is not None else self._default_config
            )
            if config is None and not model_id:
                selected_model, selected_config = self._selected_statistical_choice(
                    instrument_id, chosen_horizon.value
                )
                if selected_config is not None:
                    chosen_config = selected_config
            request = StatisticalForecastRequest(
                instrument_id=instrument_id,
                horizon=chosen_horizon,
                as_of=as_of or self._clock(),
                exchange=exchange,
                currency=currency,
                resolution=resolution,
                model_id=selected_model or model_id,
                config=chosen_config,
                dataset_id=dataset_id,
                dataset_version=dataset_version,
                feature_set_version=feature_set_version,
                feature_set_id=feature_set_id,
                bars=tuple(bars or ()),
            )
            output, forecast, statistical, _bars, _resolution, stored = self._forecast(request)
        except Exception as exc:
            # A rejection before model fitting (for example an unknown model id
            # or an invalid request) is still a model/service failure and must be
            # visible, not silently omitted from the status counters.
            if self._failures == failures_before:
                self._record_failure(model_id or "unknown", "generate", exc)
            raise
        await self._publish(
            "novamarket.forecast.created",
            forecast_id=output.forecast_id,
            instrument_id=output.instrument_id,
            horizon=output.forecast_horizon.value,
            model_id=output.model_id,
        )
        body = statistical.to_dict()
        body.update(
            {
                "ok": True,
                "status": "pending",
                "model": {
                    "model_id": forecast.model_id,
                    "model_version": STATISTICAL_MODEL_VERSION,
                    "family": forecast.family.value,
                    "config": forecast.config.to_dict(),
                },
                "fit": forecast.fit.to_dict(),
                "dataset_id": statistical.dataset_id,
                "dataset_version": statistical.dataset_version,
                "feature_set_version": statistical.feature_set_version,
                "persisted": stored,
            }
        )
        return body

    # -- evaluation -----------------------------------------------------------

    def _regime_lookup(
        self,
        instrument_id: str,
        resolution: DataInterval,
        bars: Sequence[PriceBar],
    ) -> RegimeLookup | None:
        if self._feature_service is None:
            return None
        try:
            currency = str(self._instrument_info(instrument_id).get("currency") or "")
        except Exception:  # noqa: BLE001 - currency is contextual only
            currency = ""

        def lookup(moment: datetime) -> str | None:
            prefix = [bar for bar in bars if bar.observed_at <= moment]
            if len(prefix) < 30:
                return None
            profile = (
                FeatureProfileName.INTRADAY
                if resolution.is_intraday
                else FeatureProfileName.SWING
            )
            request = FeatureRequest(
                instrument_id=instrument_id,
                as_of=moment,
                resolution=resolution.value,
                profile=profile,
                timeframes=(),
                include_context=False,
                include_fundamentals=False,
                include_news=False,
                max_bars=min(len(prefix), 500),
            )
            try:
                service = self._feature_service
                if service is None:
                    return None
                result = service.engine.generate(
                    request,
                    bars={
                        resolution.value: self._feature_bars(
                            prefix, instrument_id, resolution, currency
                        )
                    },
                )
                return str(result.regime.primary)
            except Exception:  # noqa: BLE001
                return None

        return lookup

    async def evaluate(
        self,
        instrument_id: str,
        horizon: ForecastHorizon | str,
        *,
        as_of: datetime | None = None,
        resolution: DataInterval | str | None = None,
        dataset_id: str = "",
        dataset_version: str = "",
        models: Sequence[str] | None = None,
        config: ModelConfig | Mapping[str, Any] | None = None,
        bars: Sequence[Any] | None = None,
        train_ratio: float = 0.6,
        validation_ratio: float = 0.2,
        **_ignored: Any,
    ) -> dict[str, Any]:
        """Chronological walk-forward comparison; validation selects, test reports."""
        cutoff = as_of or self._clock()
        chosen_horizon = _horizon(horizon)
        request = StatisticalForecastRequest(
            instrument_id=instrument_id,
            horizon=chosen_horizon,
            as_of=cutoff,
            resolution=resolution,
            config=_as_config(config) if config is not None else self._default_config,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            bars=tuple(bars or ()),
        )
        series, chosen_resolution, chosen_dataset_id, chosen_dataset_version = self._series(request)
        feature_version, _feature_id, _regime = self._feature_context(
            request, series, chosen_resolution
        )
        lookup = self._regime_lookup(instrument_id, chosen_resolution, series)
        try:
            report = evaluate_models(
                [bar.close for bar in series],
                series,
                instrument_id=instrument_id,
                horizon=chosen_horizon,
                resolution=chosen_resolution,
                dataset_id=chosen_dataset_id,
                dataset_version=chosen_dataset_version,
                base_config=_as_config(config) if config is not None else self._default_config,
                models=models,
                train_ratio=train_ratio,
                validation_ratio=validation_ratio,
                regime_lookup=lookup,
            )
        except Exception as exc:
            self._record_failure(",".join(models or MODEL_IDS), "evaluate", exc)
            raise
        if report.selected_model is None:
            raise ForecastDataError("no model was eligible for selection")
        # The shared forecast_selection table is NM-06's selection slot.  PI-05
        # records its selected model in the PI-05 training run instead, so the
        # two engines cannot overwrite each other's selection.
        for model_id, comparison in report.models.items():
            try:
                forecast = forecast_model(
                    model_id,
                    [bar.close for bar in series],
                    horizon_steps(chosen_horizon, chosen_resolution),
                    comparison.config,
                    bars=series,
                )
                self._register_model(
                    forecast,
                    cutoff=cutoff,
                    instrument_id=instrument_id,
                    metrics={
                        "validation": comparison.validation.to_dict(),
                        "test": comparison.test.to_dict() if comparison.test else None,
                        "selection_metric": report.selection_metric,
                        "training_range": report.train_range.to_dict(),
                        "validation_range": report.validation_range.to_dict(),
                        "test_range": report.test_range.to_dict(),
                        "selected": model_id == report.selected_model,
                    },
                    dataset_version=report.dataset_version,
                    feature_set_version=feature_version,
                )
            except Exception as exc:  # noqa: BLE001 - one failed metadata write is isolated
                self._record_failure(model_id, "register", exc)
                self._register_unavailable(
                    model_id,
                    cutoff=cutoff,
                    reason=f"{type(exc).__name__}: {exc}",
                    instrument_id=instrument_id,
                )
        payload = {
            "run_id": report.report_id,
            "instrument_id": instrument_id,
            "horizon": chosen_horizon.value,
            "model_id": report.selected_model,
            "selected_model": report.selected_model,
            "selected_config": (
                report.selected_config.to_dict() if report.selected_config else None
            ),
            "version": STATISTICAL_MODEL_VERSION,
            "started_at": series[0].observed_at.isoformat(),
            "finished_at": cutoff.isoformat(),
            "samples": report.validation_origins + report.test_origins,
            "kind": "statistical",
            "report": report.to_dict(),
        }
        try:
            self._forecast_store.save_training_run(payload)
        except Exception as exc:  # noqa: BLE001
            self._record_failure(report.selected_model, "persist_evaluation", exc)
        selected = report.models[report.selected_model]
        if selected.test is not None:
            try:
                snapshot_id = "sperf_" + hashlib.sha256(
                    f"{report.report_id}:{cutoff.isoformat()}:{self._evaluations}".encode()
                ).hexdigest()[:24]
                self._forecast_store.save_performance(
                    {
                        "snapshot_id": snapshot_id,
                        "model_id": report.selected_model,
                        "instrument_id": instrument_id,
                        "horizon": chosen_horizon.value,
                        "samples": selected.test.samples,
                        "created_at": cutoff.isoformat(),
                        "metrics": selected.test.to_dict(),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                self._record_failure(report.selected_model, "persist_performance", exc)
        self._evaluations += 1
        await self._publish(
            "novamarket.forecast.model.trained",
            model_id=report.selected_model,
            instrument_id=instrument_id,
            horizon=chosen_horizon.value,
            state="eligible",
        )
        result = report.to_dict()
        result.update(
            {
                "ok": True,
                "run_id": report.report_id,
                "selected_model": report.selected_model,
                "selection_metric": report.selection_metric,
                "automatic_promotion": False,
            }
        )
        return result

    async def compare(
        self,
        instrument_id: str,
        horizon: ForecastHorizon | str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.evaluate(instrument_id, horizon, **kwargs)

    # -- reads ----------------------------------------------------------------

    def _require_statistical(self, forecast: ForecastOutput | None) -> ForecastOutput:
        if forecast is None:
            raise ForecastNotFoundError("the requested statistical forecast does not exist")
        return forecast

    def forecast(self, forecast_id: str) -> dict[str, Any]:
        body = self._prediction_store.get_forecast(forecast_id)
        if body is None:
            raise ForecastNotFoundError(f"unknown forecast {forecast_id!r}")
        if not body.forecast_id.startswith("sft_") and body.model_id not in _STATISTICAL_MODEL_IDS:
            raise ForecastNotFoundError(
                f"forecast {forecast_id!r} is not a PI-05 statistical forecast"
            )
        return {"ok": True, **body.to_dict()}

    def forecasts(
        self,
        *,
        instrument_id: str | None = None,
        horizon: ForecastHorizon | str | None = None,
        model_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        label = _horizon(horizon) if horizon is not None else None
        rows = self._prediction_store.list_forecasts(
            instrument_id=instrument_id,
            horizon=label,
            model_id=normalise_model_id(model_id) if model_id else None,
            limit=limit,
        )
        statistical = [
            row
            for row in rows
            if row.forecast_id.startswith("sft_") or row.model_id in _STATISTICAL_MODEL_IDS
        ]
        return {
            "ok": True,
            "forecasts": [row.to_dict() for row in statistical],
            "total": len(statistical),
        }

    def models(
        self, *, instrument_id: str | None = None, horizon: ForecastHorizon | str | None = None
    ) -> dict[str, Any]:
        label = _horizon(horizon) if horizon is not None else None
        rows = [
            item.to_dict()
            for item in self._prediction_store.models(label)
            if item.model_id in _STATISTICAL_MODEL_IDS
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
        }

    def evaluations(self, *, limit: int = 20) -> dict[str, Any]:
        rows = [
            row
            for row in self._forecast_store.training_runs(limit=limit)
            if str(row.get("kind")) == "statistical"
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
        """PI-05 performance snapshots only; never mix NM-06 snapshots in."""
        rows = self._forecast_store.performance(
            model_id=model_id,
            instrument_id=instrument_id,
            horizon=horizon,
            limit=limit,
        )
        return [
            row
            for row in rows
            if str(row.get("snapshot_id") or "").startswith("sperf_")
        ]

    def suitability(
        self,
        instrument_id: str,
        horizon: ForecastHorizon | str,
        *,
        as_of: datetime | None = None,
        resolution: DataInterval | str | None = None,
        dataset_id: str = "",
        dataset_version: str = "",
        config: ModelConfig | Mapping[str, Any] | None = None,
        bars: Sequence[Any] | None = None,
    ) -> dict[str, Any]:
        chosen_horizon = _horizon(horizon)
        request = StatisticalForecastRequest(
            instrument_id=instrument_id,
            horizon=chosen_horizon,
            as_of=as_of or self._clock(),
            resolution=resolution,
            config=_as_config(config) if config is not None else self._default_config,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            bars=tuple(bars or ()),
        )
        try:
            series, chosen_resolution, _dataset_id, _dataset_version = self._series(request)
            steps = horizon_steps(request.horizon, chosen_resolution)
            report = self._suitability(request, series, chosen_resolution, steps)
            return {"ok": True, **report.to_dict()}
        except Exception as exc:
            return {
                "ok": False,
                "instrument_id": instrument_id,
                "horizon": str(horizon),
                "eligible": False,
                "reasons": [f"{type(exc).__name__}: {exc}"],
            }

    # -- observability --------------------------------------------------------

    def status(self) -> dict[str, Any]:
        try:
            counts = self._prediction_store.counts()
        except Exception:
            counts = {}
        return {
            "phase": PI05_PHASE,
            "implemented": True,
            "initialised": True,
            "contract_version": STATISTICAL_CONTRACT_VERSION,
            "schema_version": STATISTICAL_SCHEMA_VERSION,
            "forecast_store_schema": FORECAST_SCHEMA,
            "methodology_version": STATISTICAL_METHODOLOGY_VERSION,
            "model_version": STATISTICAL_MODEL_VERSION,
            "models": list(MODEL_IDS),
            "horizons": {
                item.value: {
                    "family": HORIZON_SPECS[item].family.value,
                    "duration_seconds": int(
                        HORIZON_SPECS[item].duration.total_seconds()
                    ),
                    "max_input_interval": HORIZON_SPECS[item].max_input_interval.value,
                    "min_history_bars": HORIZON_SPECS[item].min_history_bars,
                    "neutral_band_pct": HORIZON_SPECS[item].neutral_band_pct,
                }
                for item in ForecastHorizon
            },
            "generation_count": self._generations,
            "evaluation_count": self._evaluations,
            "fit_cache_hits": self._fit_cache_hits,
            "fit_cache_entries": len(self._fit_cache),
            "failures": self._failures,
            "last_error": self._last_error,
            "failure_reports": list(self.failure_reports),
            "dataset_service_attached": self._dataset_service is not None,
            "feature_service_attached": self._feature_service is not None,
            "prediction_counts": counts,
            "forecast_counts": self._forecast_store.metrics(),
            "automatic_promotion": False,
            "risk_authority": "NM-08",
            "execution_authority": "NM-10",
        }

    def contract(self) -> dict[str, Any]:
        return {
            "phase": PI05_PHASE,
            "implemented": True,
            "contract_version": STATISTICAL_CONTRACT_VERSION,
            "model_version": STATISTICAL_MODEL_VERSION,
            "methodology_version": STATISTICAL_METHODOLOGY_VERSION,
            "models": {
                model_id: {
                    "kind": metadata["kind"].value,
                    "family": metadata["family"].value,
                    "description": metadata["description"],
                    "min_observations": metadata["min_observations"],
                    "interval_method": metadata["interval"].value,
                }
                for model_id, metadata in MODEL_METADATA.items()
            },
            "horizons": {
                item.value: {
                    "family": HORIZON_SPECS[item].family.value,
                    "duration_seconds": int(
                        HORIZON_SPECS[item].duration.total_seconds()
                    ),
                    "max_input_interval": HORIZON_SPECS[item].max_input_interval.value,
                    "min_history_bars": HORIZON_SPECS[item].min_history_bars,
                    "neutral_band_pct": HORIZON_SPECS[item].neutral_band_pct,
                }
                for item in ForecastHorizon
            },
            "intervals": {
                "methods": [method.value for method in IntervalMethod],
                "probabilities": "withheld unless a separate calibration phase supplies calibrated values",
                "confidence": None,
                "guarantee": False,
            },
            "leakage": {
                "cutoff_filtered": True,
                "chronological_validation": True,
                "test_used_for_selection": False,
                "dataset_version_required_when_named": True,
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
                "no bar after the cutoff contributes to a forecast",
                "validation selects models; test data is only reported",
                "a point baseline is labelled as a point baseline",
                "a model failure is isolated and never silently replaced",
                "no model is automatically promoted or executed",
            ],
        }


__all__ = ["StatisticalForecastingService"]
