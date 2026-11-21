"""Application-facing PI-04 service.

It owns generation, persistence, preprocessing and selection diagnostics.  It
does not own acquisition, risk authorization or execution.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

from novacontrol.core.events import EventType
from novacontrol.novamarket.config import FeatureSettings
from novacontrol.novamarket.features.definitions import FEATURE_SPECS, profile_features
from novacontrol.novamarket.features.engine import FeatureEngine
from novacontrol.novamarket.features.models import (
    FEATURE_CONTRACT_VERSION,
    PreprocessingFit,
    TransformResult,
    FEATURE_SET_VERSION,
    PREPROCESSING_VERSION,
    REGIME_METHOD_VERSION,
    SELECTION_VERSION,
    FeatureGenerationResult,
    FeatureProfileName,
    FeatureQuality,
    FeatureRequest,
    FeatureSet,
    FeatureValidationError,
    MissingPolicy,
    PreprocessingMethod,
    RegimeResult,
    SelectionReport,
)
from novacontrol.novamarket.features.preprocessing import fit_preprocessor, transform
from novacontrol.novamarket.features.regime import RegimeDetector
from novacontrol.novamarket.features.selection import diagnose
from novacontrol.novamarket.features.store import FeatureStore, FeatureStoreError
from novacontrol.novamarket.prediction.models import FundamentalSnapshot

EventSink = Callable[[EventType, Mapping[str, Any]], Awaitable[None] | None]


class FeatureService:
    """The one feature-engineering composition root for NovaMarket."""

    def __init__(
        self,
        *,
        market_data: Any,
        store: FeatureStore | None = None,
        settings: FeatureSettings | None = None,
        radar: Any = None,
        dataset_store: Any = None,
        event_sink: EventSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings or FeatureSettings()
        self._store = store
        self._radar = radar
        self._event_sink = event_sink
        self._clock = clock or (lambda: datetime.now(UTC))
        self._last_error = ""
        self._generations = 0
        self._failures = 0
        self._regime = RegimeDetector(
            strong_adx=self._settings.strong_adx,
            trend_adx=self._settings.trend_adx,
            high_volatility_range_expansion=self._settings.high_volatility_range_expansion,
            low_volatility_range_expansion=self._settings.low_volatility_range_expansion,
        )
        self._engine = FeatureEngine(
            market_data=market_data,
            dataset_store=dataset_store,
            clock=self._clock,
            cache_size=self._settings.cache_size,
            regime=self._regime,
        )

    @property
    def engine(self) -> FeatureEngine:
        return self._engine

    def close(self) -> None:
        if self._store is not None:
            self._store.close()

    @property
    def settings(self) -> FeatureSettings:
        return self._settings

    def default_request(
        self,
        instrument_id: str,
        as_of: datetime,
        *,
        resolution: str = "1d",
        profile: FeatureProfileName | str | None = None,
    ) -> FeatureRequest:
        return FeatureRequest(
            instrument_id=instrument_id,
            as_of=as_of,
            resolution=resolution,
            profile=FeatureProfileName(profile or self._settings.default_profile),
            timeframes=tuple(
                item for item in self._settings.default_timeframes if item != resolution
            ),
            include_context=self._settings.include_context,
            include_fundamentals=self._settings.include_fundamentals,
            include_news=self._settings.include_news,
            max_bars=self._settings.max_bars,
        )

    async def generate(
        self,
        request: FeatureRequest,
        *,
        bars: Mapping[str, Sequence[Any]] | None = None,
        benchmark_bars: Mapping[str, Sequence[Any]] | None = None,
        sector_bars: Mapping[str, Sequence[Any]] | None = None,
        context_bars: Mapping[str, Mapping[str, Sequence[Any]]] | None = None,
        fundamentals: Sequence[FundamentalSnapshot] = (),
        news_events: Sequence[Any] | None = None,
        previous_regime: RegimeResult | None = None,
    ) -> FeatureGenerationResult:
        if not self._settings.enabled:
            raise FeatureValidationError("feature generation is disabled by configuration")
        if news_events is None:
            news_events = await self._events_from_radar(request.instrument_id)
        if previous_regime is None and self._store is not None:
            previous_regime = self._store.latest_regime(
                request.instrument_id,
                resolution=request.resolution,
                before=request.as_of,
            )
        try:
            result = self._engine.generate(
                request,
                bars=bars,
                benchmark_bars=benchmark_bars,
                sector_bars=sector_bars,
                context_bars=context_bars,
                fundamentals=fundamentals,
                news_events=news_events or (),
                previous_regime=previous_regime,
            )
        except Exception as exc:  # noqa: BLE001 - the caller receives a typed refusal
            self._failures += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            await self._emit(
                EventType.NOVAMARKET_FEATURES_FAILED,
                {
                    "instrument_id": request.instrument_id,
                    "resolution": request.resolution,
                    "profile": str(request.profile),
                    "error": self._last_error,
                },
            )
            if self._store is not None:
                try:
                    self._store.save_failure(
                        f"fg_failed_{self._failures}_{int(self._clock().timestamp())}",
                        request.instrument_id,
                        self._last_error,
                    )
                except Exception as store_exc:  # noqa: BLE001
                    self._last_error += f"; failure record: {store_exc}"
            raise
        self._generations += 1
        persisted = False
        duplicate = False
        store_error = ""
        if self._store is not None:
            try:
                persisted, duplicate = self._store.save(
                    result.feature_set,
                    result.regime,
                    generation_id=f"fg_{result.feature_set.feature_set_id}",
                    definitions=self._engine.definition_payload(),
                )
            except FeatureStoreError as exc:
                store_error = str(exc)
                self._last_error = store_error
        result = replace(result, persisted=persisted, duplicate=duplicate, store_error=store_error)
        await self._emit(
            EventType.NOVAMARKET_FEATURES_GENERATED,
            {
                "feature_set_id": result.feature_set.feature_set_id,
                "instrument_id": result.feature_set.instrument_id,
                "as_of": result.feature_set.as_of.isoformat(),
                "resolution": result.feature_set.resolution,
                "profile": str(result.feature_set.profile),
                "quality": str(result.feature_set.quality),
                "completeness": result.feature_set.completeness,
                "persisted": persisted,
                "duplicate": duplicate,
            },
        )
        await self._emit(
            EventType.NOVAMARKET_REGIME_DETECTED,
            {
                "regime_id": result.regime.regime_id,
                "feature_set_id": result.regime.feature_set_id,
                "instrument_id": result.regime.instrument_id,
                "as_of": result.regime.as_of.isoformat(),
                "primary_regime": str(result.regime.primary),
                "secondary": [str(item) for item in result.regime.secondary],
                "method_version": result.regime.method,
            },
        )
        if result.regime.transition and result.regime.transition.changed:
            await self._emit(
                EventType.NOVAMARKET_REGIME_TRANSITIONED,
                {
                    "regime_id": result.regime.regime_id,
                    "instrument_id": result.regime.instrument_id,
                    "previous": str(result.regime.transition.previous),
                    "current": str(result.regime.transition.current),
                    "as_of": result.regime.as_of.isoformat(),
                },
            )
        return result

    def fit_preprocessing(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        feature_names: Sequence[str] | None = None,
        method: PreprocessingMethod | str = PreprocessingMethod.STANDARD,
        missing_policy: MissingPolicy | str | None = None,
        outlier_z: float = 6.0,
    ) -> PreprocessingFit:
        fit = fit_preprocessor(
            rows,
            feature_names=feature_names,
            method=method,
            missing_policy=missing_policy or self._settings.missing_policy,
            outlier_z=outlier_z,
            fitted_at=self._clock(),
        )
        if self._store is not None:
            try:
                self._store.save_preprocessing(fit)
            except FeatureStoreError as exc:
                self._last_error = str(exc)
        self._emit_sync(
            EventType.NOVAMARKET_PREPROCESSING_FITTED,
            {
                "preprocessing_id": fit.preprocessing_id,
                "method": str(fit.method),
                "version": fit.version,
                "training_digest": fit.training_digest,
            },
        )
        return fit

    def transform_row(self, fit: PreprocessingFit, row: Mapping[str, Any]) -> TransformResult:
        return transform(fit, row)

    def diagnose(
        self,
        rows: Sequence[Mapping[str, Any]] | Sequence[FeatureSet],
        *,
        feature_names: Sequence[str] | None = None,
        correlation_threshold: float | None = None,
        missing_threshold: float | None = None,
        leakage_safe: bool = True,
    ) -> SelectionReport:
        report = diagnose(
            rows,
            feature_names=feature_names,
            correlation_threshold=(
                self._settings.selection_correlation_threshold
                if correlation_threshold is None
                else correlation_threshold
            ),
            missing_threshold=(
                self._settings.selection_missing_threshold
                if missing_threshold is None
                else missing_threshold
            ),
            leakage_safe=leakage_safe,
            generated_at=self._clock(),
        )
        if self._store is not None:
            try:
                self._store.save_selection(report)
            except FeatureStoreError as exc:
                self._last_error = str(exc)
        self._emit_sync(
            EventType.NOVAMARKET_FEATURE_SELECTION_COMPLETED,
            {
                "report_id": report.report_id,
                "version": report.version,
                "observations": report.observations,
                "decision_count": len(report.decisions),
            },
        )
        return report

    def generate_sync(
        self,
        request: FeatureRequest,
        *,
        bars: Mapping[str, Sequence[Any]] | None = None,
        fundamentals: Sequence[Any] = (),
        news_events: Sequence[Any] = (),
    ) -> FeatureGenerationResult:
        """Generate one point-in-time row without persisting or using live context.

        This is the bounded research read used by the PI-13 RL environment.  It
        reuses the same FeatureEngine and definitions as ``generate`` but never
        writes a feature set, so an experiment cannot mutate the authoritative
        feature store.
        """
        if not self._settings.enabled:
            raise FeatureValidationError("feature generation is disabled by configuration")
        return self._engine.generate(
            request,
            bars=bars,
            fundamentals=fundamentals,
            news_events=news_events,
        )

    def feature_set(self, feature_set_id: str) -> FeatureSet | None:
        return self._store.get_feature_set(feature_set_id) if self._store else None

    def latest_feature_set(
        self, instrument_id: str, *, resolution: str | None = None
    ) -> FeatureSet | None:
        return (
            self._store.latest_feature_set(instrument_id, resolution=resolution)
            if self._store
            else None
        )

    def latest_regime(
        self, instrument_id: str, *, resolution: str | None = None
    ) -> RegimeResult | None:
        return (
            self._store.latest_regime(instrument_id, resolution=resolution)
            if self._store
            else None
        )

    def feature_sets(self, *, instrument_id: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if self._store is None:
            return {"items": [], "total": 0, "limit": limit, "offset": offset, "has_more": False}
        return self._store.feature_sets(instrument_id=instrument_id, limit=limit, offset=offset)

    def regimes(self, *, instrument_id: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if self._store is None:
            return {"items": [], "total": 0, "limit": limit, "offset": offset, "has_more": False}
        return self._store.regimes(instrument_id=instrument_id, limit=limit, offset=offset)

    def status(self) -> dict[str, Any]:
        return {
            "implemented": True,
            "enabled": self._settings.enabled,
            "phase": "PI-04",
            "contract_version": FEATURE_CONTRACT_VERSION,
            "feature_set_version": FEATURE_SET_VERSION,
            "regime_method_version": REGIME_METHOD_VERSION,
            "preprocessing_version": PREPROCESSING_VERSION,
            "selection_version": SELECTION_VERSION,
            "default_profile": self._settings.default_profile,
            "default_timeframes": list(self._settings.default_timeframes),
            "feature_count": len(FEATURE_SPECS),
            "profiles": {
                str(profile): len(profile_features(profile))
                for profile in FeatureProfileName
            },
            "cache_entries": self._engine.cache_entries,
            "generations": self._generations,
            "failures": self._failures,
            "last_error": self._last_error,
            "repository": self._store.path if self._store else "",
            "store_health": self._store.health() if self._store else {"ok": False, "reason": "not configured"},
            "statistics": self._store.stats() if self._store else {},
        }

    def contract(self) -> dict[str, Any]:
        return {
            "implemented": True,
            "phase": "PI-04",
            "name": "Feature Engineering and Market Regime Detection",
            "contract_version": FEATURE_CONTRACT_VERSION,
            "feature_set_version": FEATURE_SET_VERSION,
            "regime_method_version": REGIME_METHOD_VERSION,
            "preprocessing_version": PREPROCESSING_VERSION,
            "selection_version": SELECTION_VERSION,
            "families": [
                "price",
                "trend",
                "momentum",
                "volatility",
                "volume",
                "structure",
                "multi_timeframe",
                "context",
                "fundamental",
                "news_event",
            ],
            "profiles": [str(item) for item in FeatureProfileName],
            "regimes": [
                "strong_up",
                "weak_up",
                "strong_down",
                "weak_down",
                "sideways",
                "high_volatility",
                "low_volatility",
                "potential_reversal",
                "breakout",
                "breakdown",
                "uncertain",
            ],
            "feature_count": len(FEATURE_SPECS),
            "definitions": {
                name: spec.to_dict() for name, spec in sorted(FEATURE_SPECS.items())
            },
            "preprocessing_methods": [str(item) for item in PreprocessingMethod],
            "missing_policies": [str(item) for item in MissingPolicy],
            "selection_issues": [
                "highly_correlated",
                "zero_variance",
                "low_variance",
                "excessive_missingness",
                "unstable",
                "leakage",
            ],
            "leakage_rule": (
                "Only completed bars whose bar-close timestamp is at or before as_of, "
                "events received at or before as_of, and fundamentals available at or "
                "before as_of are eligible. A regime is not a prediction."
            ),
            "confidence_policy": (
                "confidence is null until a separate phase calibrates the detector"
            ),
            "automatic_feature_deletion": False,
            "authority": {
                "risk": "NM-08 remains authoritative",
                "execution": "NM-10 remains authoritative",
                "orders_emitted": False,
            },
        }

    async def _events_from_radar(self, instrument_id: str) -> Sequence[Any]:
        if self._radar is None:
            return ()
        try:
            response = self._radar.instrument_events(instrument_id, limit=200)
            if inspect.isawaitable(response):
                response = await response
        except Exception as exc:  # noqa: BLE001 - missing context is explicit
            self._last_error = f"radar: {exc}"
            return ()
        if isinstance(response, Mapping):
            items = response.get("items") or response.get("events") or ()
        elif isinstance(response, Sequence) and not isinstance(response, (str, bytes)):
            items = response
        else:
            return ()
        return tuple(
            item
            for item in items
            if isinstance(item, Mapping) or hasattr(item, "evidence_id")
        )

    async def _emit(self, event_type: EventType, payload: Mapping[str, Any]) -> None:
        if self._event_sink is None:
            return
        try:
            value = self._event_sink(event_type, payload)
            if inspect.isawaitable(value):
                await value
        except Exception as exc:  # noqa: BLE001 - announcements never fail the work
            self._last_error = f"event: {exc}"

    def _emit_sync(self, event_type: EventType, payload: Mapping[str, Any]) -> None:
        """Announce a synchronous operation without making it fail on delivery."""
        if self._event_sink is None:
            return
        try:
            value = self._event_sink(event_type, payload)
            if inspect.isawaitable(value):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(cast(Coroutine[Any, Any, None], value))
                else:
                    asyncio.create_task(cast(Coroutine[Any, Any, None], value))
        except Exception as exc:  # noqa: BLE001 - announcements never fail the work
            self._last_error = f"event: {exc}"


__all__ = ["FeatureService"]
