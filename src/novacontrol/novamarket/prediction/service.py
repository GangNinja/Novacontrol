"""Prediction Intelligence service: contracts, lifecycle and evidence.

This is a foundation, not a forecasting scheduler.  Callers supply model
outputs at the lifecycle points; the service validates the market snapshot,
assembles eligible models, persists immutable forecasts and stage records, and
exposes read-only decision evidence.  It never calls NM-08 or NM-10.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from novacontrol.novamarket.prediction.data import (
    PredictionDataGateway,
    low_latency_contract,
)
from novacontrol.novamarket.prediction.ensemble import EnsembleAssembler
from novacontrol.novamarket.prediction.lifecycle import (
    LifecycleSchedule,
    changed_fields,
    schedule_for,
)
from novacontrol.novamarket.prediction.models import (
    AUTOMATIC_PROMOTION,
    BROKER_CONNECTED,
    DATA_CONTRACT_VERSION,
    ENSEMBLE_CONTRACT_VERSION,
    EXECUTION_AUTHORITY,
    FORECAST_CONTRACT_VERSION,
    HORIZON_SPECS,
    LIFECYCLE_CONTRACT_VERSION,
    LEARNING_CONTRACT_VERSION,
    LIVE_TRADING,
    PAPER_ONLY,
    PI_CONTRACT_VERSION,
    PI_PHASE,
    PI_SCHEMA_VERSION,
    RISK_AUTHORITY,
    AcquisitionPolicy,
    CalibrationStatus,
    ConfirmationStage,
    DecisionForecastEvidence,
    Direction,
    EnsembleResult,
    ForecastEvaluation,
    ForecastFamily,
    ForecastHorizon,
    ForecastLifecycle,
    ForecastOutput,
    ForecastValidity,
    LifecycleStageRecord,
    LifecycleStatus,
    MarketDataSnapshot,
    ModelFamily,
    ModelMetadata,
    ModelPrediction,
    ModelState,
    PredictionAbstainedError,
    PredictionDataError,
    PredictionNotFoundError,
    PredictionPolicy,
    PredictionRequest,
    PredictionStateError,
    PredictionValidationError,
    ProbabilityStatus,
    ResourcePolicy,
    StageStatus,
    direction_for_return,
    learning_boundary_contract,
    prediction_id,
)
from novacontrol.novamarket.prediction.store import PredictionStore
from novacontrol.core.events import EventType


class PredictionIntelligenceService:
    """Own prediction contracts and immutable evidence; never financial authority."""

    def __init__(
        self,
        store_path: str | Path | None = None,
        *,
        store: PredictionStore | None = None,
        market_data: Any | None = None,
        policy: PredictionPolicy | None = None,
        acquisition: AcquisitionPolicy | None = None,
        publisher: Any | None = None,
        clock: Any | None = None,
    ) -> None:
        self.policy = policy or PredictionPolicy()
        self.acquisition = acquisition or AcquisitionPolicy()
        self._store = store or PredictionStore(
            store_path or ":memory:"
        )
        self._gateway = (
            PredictionDataGateway(
                market_data,
                policy=self.policy,
                acquisition=self.acquisition,
                clock=clock,
            )
            if market_data is not None
            else None
        )
        self._assembler = EnsembleAssembler(self.policy)
        self._publisher = publisher
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def store(self) -> PredictionStore:
        return self._store

    @property
    def market_data_attached(self) -> bool:
        return self._gateway is not None

    def attach_market_data(self, market_data: Any) -> None:
        self._gateway = PredictionDataGateway(
            market_data,
            policy=self.policy,
            acquisition=self.acquisition,
            clock=self._clock,
        )

    def attach_publisher(self, publisher: Any | None) -> None:
        self._publisher = publisher

    # -- models -----------------------------------------------------------
    async def register_model(self, model: ModelMetadata) -> dict[str, Any]:
        self._store.register_model(model)
        await self._publish(
            EventType.NOVAMARKET_PREDICTION_MODEL_REGISTERED,
            model_id=model.model_id,
            version=model.version,
            state=model.state.value,
            calibration=model.calibration.value,
            horizons=",".join(item.value for item in model.supported_horizons),
        )
        return model.to_dict()

    def models(self, horizon: ForecastHorizon | str | None = None) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self._store.models(horizon)]

    # -- lifecycle --------------------------------------------------------
    def data_snapshot(
        self,
        request: PredictionRequest,
        *,
        stage: ConfirmationStage = ConfirmationStage.INITIAL,
        at: datetime | None = None,
    ) -> MarketDataSnapshot:
        """Expose the read-only snapshot models must cite in their outputs."""
        self._require_market_data()
        return self._snapshot(request, stage, self._at(at))

    async def create_lifecycle(
        self,
        request: PredictionRequest,
        predictions: Sequence[ModelPrediction],
        *,
        models: Sequence[ModelMetadata] | None = None,
        snapshot: MarketDataSnapshot | None = None,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """Create the T-20 forecast and its lifecycle identity.

        The initial forecast is never overwritten.  A repeated request returns
        the existing lifecycle instead of creating a second forecast.
        """
        self._require_market_data()
        now = self._at(at)
        if request.created_at > now:
            raise PredictionStateError("request created_at is in the future")
        schedule = schedule_for(request.target_at, self.policy)
        lifecycle_id = self._lifecycle_id(request)
        existing = self._store.get_lifecycle(lifecycle_id)
        if existing is not None:
            # A retry of an already-created lifecycle is idempotent even if the
            # retry arrives after the initial confirmation window.
            return self._lifecycle_view(existing, replayed=True)
        if now < schedule.initial_at - self._tolerance():
            raise PredictionStateError(
                f"the initial forecast is not due until {schedule.initial_at.isoformat()}"
            )
        if now >= schedule.first_confirmation_at:
            raise PredictionStateError(
                "the initial forecast window has passed; a lifecycle cannot start "
                "at or after the T-5 confirmation point"
            )

        snapshot = snapshot or self._snapshot(request, ConfirmationStage.INITIAL, now)
        self._validate_snapshot(request, snapshot, ConfirmationStage.INITIAL, now)
        self._store.save_snapshot(snapshot)
        ensemble = self._assembler.assemble(
            request,
            snapshot,
            predictions,
            list(models) if models is not None else self._store.models(request.horizon),
            lifecycle_id=lifecycle_id,
            stage=ConfirmationStage.INITIAL,
            now=now,
        )
        if ensemble.output is None:
            await self._publish(
                EventType.NOVAMARKET_PREDICTION_ABSTAINED,
                lifecycle_id=lifecycle_id,
                stage=ConfirmationStage.INITIAL.value,
                reasons="; ".join(ensemble.reasons),
            )
            raise PredictionAbstainedError(
                "the ensemble abstained; no initial forecast was created",
                reasons=ensemble.reasons,
            )
        forecast = replace(
            ensemble.output,
            lifecycle_id=lifecycle_id,
            validity=ForecastValidity.UNCONFIRMED,
        )
        status = (
            LifecycleStatus.AWAITING_FIRST_CONFIRMATION
            if now < schedule.first_confirmation_at
            else LifecycleStatus.AWAITING_FINAL_CONFIRMATION
        )
        lifecycle = ForecastLifecycle(
            lifecycle_id=lifecycle_id,
            request_id=request.request_id,
            instrument_id=request.instrument_id,
            exchange=request.exchange,
            horizon=request.horizon,
            target_at=request.target_at,
            initial_at=schedule.initial_at,
            first_confirmation_at=schedule.first_confirmation_at,
            final_confirmation_at=schedule.final_confirmation_at,
            status=status,
            created_at=now,
            updated_at=now,
            initial_forecast_id=forecast.forecast_id,
            metadata={
                "portfolio_id": request.portfolio_id,
                "strategy_id": request.strategy_id,
                "data_snapshot_id": snapshot.snapshot_id,
                "ensemble_id": ensemble.ensemble_id,
            },
        )
        stage = LifecycleStageRecord(
            stage_id=prediction_id("pst_", lifecycle_id, ConfirmationStage.INITIAL.value),
            lifecycle_id=lifecycle_id,
            stage=ConfirmationStage.INITIAL,
            scheduled_at=schedule.initial_at,
            recorded_at=now,
            status=StageStatus.RECORDED,
            forecast_id=forecast.forecast_id,
            data_observed_at=forecast.data_observed_at,
            data_freshness=forecast.data_freshness,
            data_resolution=forecast.data_resolution,
        )
        self._store.save_request(request)
        self._store.save_lifecycle(lifecycle)
        self._store.save_ensemble(ensemble)
        self._store.save_forecast(forecast)
        self._store.save_stage(stage)
        await self._publish(
            EventType.NOVAMARKET_PREDICTION_LIFECYCLE_CREATED,
            lifecycle_id=lifecycle_id,
            request_id=request.request_id,
            instrument_id=request.instrument_id,
            horizon=request.horizon.value,
            target_at=request.target_at.isoformat(),
        )
        await self._publish_forecast(forecast)
        return self._lifecycle_view(lifecycle)

    async def confirm(
        self,
        lifecycle_id: str,
        stage: ConfirmationStage | str,
        predictions: Sequence[ModelPrediction],
        *,
        models: Sequence[ModelMetadata] | None = None,
        snapshot: MarketDataSnapshot | None = None,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """Record T-5 or T-1 evidence without changing an earlier forecast."""
        self._require_market_data()
        lifecycle = self._require_lifecycle(lifecycle_id)
        try:
            chosen = ConfirmationStage(stage)
        except ValueError as exc:
            raise PredictionValidationError(f"unknown confirmation stage {stage!r}") from exc
        if chosen is ConfirmationStage.INITIAL:
            raise PredictionStateError("initial forecasts are created, not confirmed")
        now = self._at(at)
        existing_stage = self._store.stage_for(lifecycle_id, chosen)
        if existing_stage is not None:
            return self._lifecycle_view(lifecycle, replayed=True)
        if now >= lifecycle.target_at:
            raise PredictionStateError("confirmation must finish before the forecast target")
        expected = (
            ConfirmationStage.FIRST
            if lifecycle.status is LifecycleStatus.AWAITING_FIRST_CONFIRMATION
            else ConfirmationStage.FINAL
            if lifecycle.status is LifecycleStatus.AWAITING_FINAL_CONFIRMATION
            else None
        )
        if expected is None:
            raise PredictionStateError(
                f"lifecycle {lifecycle_id} is {lifecycle.status.value}; no confirmation is allowed"
            )
        if chosen is not expected:
            raise PredictionStateError(
                f"next confirmation is {expected.value}, not {chosen.value}"
            )
        scheduled = (
            lifecycle.first_confirmation_at
            if chosen is ConfirmationStage.FIRST
            else lifecycle.final_confirmation_at
        )
        if now < scheduled - self._tolerance():
            raise PredictionStateError(
                f"{chosen.value} is not due until {scheduled.isoformat()}"
            )

        request = self._store.get_request(lifecycle.request_id)
        if request is None:
            raise PredictionNotFoundError(
                f"request {lifecycle.request_id!r} for lifecycle {lifecycle_id} is missing"
            )
        snapshot = snapshot or self._snapshot(request, chosen, now)
        self._validate_snapshot(request, snapshot, chosen, now)
        self._store.save_snapshot(snapshot)
        previous = self._latest_forecast(lifecycle_id)
        if not snapshot.usable:
            return await self._record_unconfirmed_stage(
                lifecycle,
                chosen,
                snapshot,
                tuple(snapshot.reasons) or ("market data was not usable",),
                now,
            )
        ensemble = self._assembler.assemble(
            request,
            snapshot,
            predictions,
            list(models) if models is not None else self._store.models(request.horizon),
            lifecycle_id=lifecycle_id,
            stage=chosen,
            now=now,
        )
        if ensemble.output is None:
            return await self._record_unconfirmed_stage(
                lifecycle,
                chosen,
                snapshot,
                tuple(ensemble.reasons) or ("the ensemble abstained",),
                now,
            )
        validity = (
            ForecastValidity.CONFIRMED
            if chosen is ConfirmationStage.FINAL
            else ForecastValidity.UNCONFIRMED
        )
        forecast = replace(ensemble.output, lifecycle_id=lifecycle_id, validity=validity)
        changes = changed_fields(previous, forecast) if previous is not None else ()
        stage_record = LifecycleStageRecord(
            stage_id=prediction_id("pst_", lifecycle_id, chosen.value),
            lifecycle_id=lifecycle_id,
            stage=chosen,
            scheduled_at=scheduled,
            recorded_at=now,
            status=StageStatus.RECORDED,
            forecast_id=forecast.forecast_id,
            previous_forecast_id=previous.forecast_id if previous else "",
            data_observed_at=forecast.data_observed_at,
            data_freshness=forecast.data_freshness,
            data_resolution=forecast.data_resolution,
            changed_fields=changes,
        )
        if chosen is ConfirmationStage.FIRST:
            lifecycle = replace(
                lifecycle,
                status=LifecycleStatus.AWAITING_FINAL_CONFIRMATION,
                updated_at=now,
                first_forecast_id=forecast.forecast_id,
            )
        else:
            lifecycle = replace(
                lifecycle,
                status=LifecycleStatus.CONFIRMED,
                updated_at=now,
                final_forecast_id=forecast.forecast_id,
            )
        self._store.save_ensemble(ensemble)
        self._store.save_forecast(forecast)
        self._store.save_stage(stage_record)
        self._store.update_lifecycle(lifecycle)
        await self._publish(
            EventType.NOVAMARKET_PREDICTION_FORECAST_CONFIRMED,
            lifecycle_id=lifecycle_id,
            stage=chosen.value,
            forecast_id=forecast.forecast_id,
            direction=forecast.direction.value,
            changed_fields=",".join(changes),
        )
        await self._publish_forecast(forecast)
        return self._lifecycle_view(lifecycle)

    async def _record_unconfirmed_stage(
        self,
        lifecycle: ForecastLifecycle,
        stage: ConfirmationStage,
        snapshot: MarketDataSnapshot,
        reasons: Sequence[str],
        now: datetime,
    ) -> dict[str, Any]:
        status = StageStatus.SKIPPED if stage is ConfirmationStage.FIRST else StageStatus.INVALID
        record = LifecycleStageRecord(
            stage_id=prediction_id("pst_", lifecycle.lifecycle_id, stage.value),
            lifecycle_id=lifecycle.lifecycle_id,
            stage=stage,
            scheduled_at=(
                lifecycle.first_confirmation_at
                if stage is ConfirmationStage.FIRST
                else lifecycle.final_confirmation_at
            ),
            recorded_at=now,
            status=status,
            data_observed_at=(
                snapshot.source_observed_at if snapshot.missing_observation is False else None
            ),
            data_freshness=snapshot.freshness,
            data_resolution=snapshot.resolution,
            reasons=tuple(reasons),
        )
        if stage is ConfirmationStage.FIRST:
            lifecycle = replace(
                lifecycle,
                status=LifecycleStatus.AWAITING_FINAL_CONFIRMATION,
                updated_at=now,
            )
        else:
            lifecycle = replace(
                lifecycle,
                status=LifecycleStatus.UNCONFIRMED,
                updated_at=now,
                invalidation_reason="; ".join(reasons),
            )
        self._store.save_stage(record)
        self._store.update_lifecycle(lifecycle)
        await self._publish(
            EventType.NOVAMARKET_PREDICTION_CONFIRMATION_SKIPPED,
            lifecycle_id=lifecycle.lifecycle_id,
            stage=stage.value,
            status=status.value,
            reason="; ".join(reasons),
        )
        return self._lifecycle_view(lifecycle)

    async def evaluate(
        self,
        lifecycle_id: str,
        actual_price: Decimal | str,
        *,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """Evaluate every preserved forecast against the actual market outcome."""
        lifecycle = self._require_lifecycle(lifecycle_id)
        now = self._at(at)
        if now < lifecycle.target_at:
            raise PredictionStateError(
                f"target {lifecycle.target_at.isoformat()} has not been reached"
            )
        try:
            actual = actual_price if isinstance(actual_price, Decimal) else Decimal(str(actual_price))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise PredictionValidationError("actual_price is not a decimal") from exc
        if not actual.is_finite() or actual <= 0:
            raise PredictionValidationError("actual_price must be finite and positive")
        forecasts = self._store.forecasts(lifecycle_id)
        if not forecasts:
            raise PredictionNotFoundError(f"lifecycle {lifecycle_id} has no forecasts")
        existing = self._store.evaluations(lifecycle_id)
        if existing and any(item.actual_price != actual for item in existing):
            raise PredictionStateError(
                "this lifecycle already has evaluations for a different actual price"
            )
        evaluations: list[ForecastEvaluation] = []
        for forecast in forecasts:
            if any(item.forecast_id == forecast.forecast_id for item in existing):
                continue
            evaluation = self._make_evaluation(forecast, actual, now)
            if self._store.save_evaluation(evaluation):
                evaluations.append(evaluation)
                await self._publish(
                    EventType.NOVAMARKET_PREDICTION_FORECAST_EVALUATED,
                    lifecycle_id=lifecycle_id,
                    forecast_id=forecast.forecast_id,
                    stage=forecast.lifecycle_stage.value,
                    direction_correct=evaluation.direction_correct,
                    status=evaluation.status,
                )
        existing_ids = tuple(
            item.evaluation_id for item in self._store.evaluations(lifecycle_id)
        )
        lifecycle = replace(
            lifecycle,
            status=LifecycleStatus.EVALUATED,
            updated_at=now,
            evaluation_ids=existing_ids,
        )
        self._store.update_lifecycle(lifecycle)
        return {
            **self._lifecycle_view(lifecycle),
            "actual_price": format(actual, "f"),
            "evaluations": [item.to_dict() for item in self._store.evaluations(lifecycle_id)],
        }

    async def decision_evidence(
        self,
        lifecycle_id: str,
        *,
        portfolio_id: str = "",
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """Return read-only evidence for the existing decision pipeline."""
        lifecycle = self._require_lifecycle(lifecycle_id)
        final = self._store.forecast_for_stage(lifecycle_id, ConfirmationStage.FINAL)
        if final is None:
            raise PredictionStateError(
                f"lifecycle {lifecycle_id} has no final confirmation evidence"
            )
        final_stage = self._store.stage_for(lifecycle_id, ConfirmationStage.FINAL)
        reasons: list[str] = []
        if final_stage is None or final_stage.status is not StageStatus.RECORDED:
            reasons.append("the final confirmation stage was not recorded")
        if final.validity not in (ForecastValidity.CONFIRMED, ForecastValidity.VALID):
            reasons.append(f"final forecast validity is {final.validity.value}")
        if final.direction in (Direction.INVALID, Direction.UNCERTAIN):
            reasons.append(f"final direction is {final.direction.value}")
        if final.data_freshness.value in ("missing", "stale", "provider_unavailable"):
            reasons.append(f"final data freshness is {final.data_freshness.value}")
        eligible = not reasons and final.direction in (
            Direction.UP,
            Direction.DOWN,
            Direction.NEUTRAL,
        )
        evidence = DecisionForecastEvidence(
            evidence_id=prediction_id(
                "pev_", lifecycle_id, final.forecast_id, portfolio_id
            ),
            lifecycle_id=lifecycle_id,
            forecast_id=final.forecast_id,
            instrument_id=final.instrument_id,
            portfolio_id=portfolio_id,
            direction=final.direction,
            expected_return=final.predicted_return,
            uncertainty=final.uncertainty,
            p_up=final.p_up,
            p_down=final.p_down,
            p_neutral=final.p_neutral,
            reference_price=final.reference_price,
            reference_at=final.reference_at,
            target_at=final.target_at,
            generated_at=final.generated_at,
            data_observed_at=final.data_observed_at,
            data_freshness=final.data_freshness,
            final_confirmation=final.lifecycle_stage is ConfirmationStage.FINAL,
            eligible=eligible,
            reasons=tuple(reasons),
            metadata={
                "lifecycle_status": lifecycle.status.value,
                "data_snapshot_id": final.data_snapshot_id,
                "ensemble_id": final.ensemble_id,
                "authorizes_action": False,
            },
        )
        self._store.save_evidence(evidence)
        await self._publish(
            EventType.NOVAMARKET_PREDICTION_DECISION_EVIDENCE_READY,
            evidence_id=evidence.evidence_id,
            lifecycle_id=lifecycle_id,
            instrument_id=final.instrument_id,
            eligible=eligible,
        )
        return {
            **self._lifecycle_view(lifecycle),
            "evidence": evidence.to_dict(),
        }

    def lifecycle(self, lifecycle_id: str) -> dict[str, Any]:
        return self._lifecycle_view(self._require_lifecycle(lifecycle_id))

    def forecast_history(self, lifecycle_id: str) -> dict[str, Any]:
        lifecycle = self._require_lifecycle(lifecycle_id)
        return {
            "lifecycle_id": lifecycle_id,
            "forecasts": [item.to_dict() for item in self._store.forecasts(lifecycle_id)],
            "stages": [item.to_dict() for item in self._store.stages(lifecycle_id)],
            "evaluations": [item.to_dict() for item in self._store.evaluations(lifecycle_id)],
        }

    # -- introspection ----------------------------------------------------
    def status(self) -> dict[str, Any]:
        counts = self._store.counts()
        models = self._store.models()
        return {
            "phase": PI_PHASE,
            "contract_version": PI_CONTRACT_VERSION,
            "schema_version": self._store.schema_version(),
            "forecast_contract_version": FORECAST_CONTRACT_VERSION,
            "lifecycle_contract_version": LIFECYCLE_CONTRACT_VERSION,
            "ensemble_contract_version": ENSEMBLE_CONTRACT_VERSION,
            "data_contract_version": DATA_CONTRACT_VERSION,
            "market_data_attached": self.market_data_attached,
            "store": counts,
            "models": {
                "registered": len({(item.model_id, item.version) for item in models}),
                "by_state": self._count_by(models, "state"),
                "by_family": self._count_by(models, "family"),
            },
            "horizons": {
                family.value: [
                    spec.horizon.value
                    for spec in HORIZON_SPECS.values()
                    if spec.family is family
                ]
                for family in ForecastFamily
            },
            "lifecycle": {
                "initial": "t_minus_20",
                "first_confirmation": "t_minus_5",
                "final_confirmation": "t_minus_1",
                "target": "t",
                "policy": self.policy.to_dict(),
                "missing_data": "skipped/unconfirmed, never fabricated",
            },
            "ensemble": {
                "contract_version": ENSEMBLE_CONTRACT_VERSION,
                "can_abstain": True,
                "calibrated_probabilities_only": True,
                "automatic_promotion": AUTOMATIC_PROMOTION,
            },
            "acquisition": self.acquisition.to_dict(),
            "resources": ResourcePolicy().to_dict(),
            "low_latency": low_latency_contract(self.acquisition),
            "authorities": {
                "risk": RISK_AUTHORITY,
                "execution": EXECUTION_AUTHORITY,
                "prediction_authorizes_action": False,
                "decision_authority": "existing autonomous trading pipeline",
            },
            "safety": {
                "paper_only": PAPER_ONLY,
                "live_trading": LIVE_TRADING,
                "broker_connected": BROKER_CONNECTED,
                "automatic_promotion": AUTOMATIC_PROMOTION,
                "at_07": False,
            },
            "learning": learning_boundary_contract(),
        }

    def contract(self) -> dict[str, Any]:
        return {
            "phase": PI_PHASE,
            "status": "implemented",
            "contract_version": PI_CONTRACT_VERSION,
            "schema_version": PI_SCHEMA_VERSION,
            "domains": [
                "virtual_market_and_financial_system",
                "market_data_intelligence",
                "prediction_intelligence",
                "forecast_lifecycle_and_confirmation",
                "autonomous_trading_intelligence",
                "reinforcement_learning_and_adaptive_intelligence",
                "performance_evaluation_and_observability",
            ],
            "prediction": {
                "forecast_contract_version": FORECAST_CONTRACT_VERSION,
                "lifecycle_contract_version": LIFECYCLE_CONTRACT_VERSION,
                "ensemble_contract_version": ENSEMBLE_CONTRACT_VERSION,
                "data_contract_version": DATA_CONTRACT_VERSION,
                "horizons": {
                    spec.horizon.value: spec.to_dict() for spec in HORIZON_SPECS.values()
                },
                "lifecycle_stages": [stage.value for stage in ConfirmationStage],
                "directions": [item.value for item in Direction],
                "validity": [item.value for item in ForecastValidity],
                "probability_statuses": [item.value for item in ProbabilityStatus],
                "model_families": [item.value for item in ModelFamily],
                "model_states": [item.value for item in ModelState],
                "data_paths": [
                    "general_refresh",
                    "intraday_quote",
                    "historical",
                    "news_and_events",
                    "fundamentals",
                ],
            },
            "authorities": {
                "risk": RISK_AUTHORITY,
                "execution": EXECUTION_AUTHORITY,
                "prediction_authorizes_action": False,
                "decision_authority": "existing autonomous trading pipeline",
            },
            "decision_evidence": {
                "read_only": True,
                "authorizes_action": False,
                "risk_authority": RISK_AUTHORITY,
                "execution_authority": EXECUTION_AUTHORITY,
                "fields": [
                    "instrument_id",
                    "direction",
                    "expected_return",
                    "uncertainty",
                    "p_up",
                    "p_down",
                    "p_neutral",
                    "reference_price",
                    "reference_at",
                    "target_at",
                    "generated_at",
                    "data_observed_at",
                    "data_freshness",
                    "final_confirmation",
                    "eligible",
                ],
            },
            "acquisition": self.acquisition.to_dict(),
            "resources": ResourcePolicy().to_dict(),
            "data_contracts": {
                "market_observation": DATA_CONTRACT_VERSION,
                "feature_definition": "prediction.feature-definition.1.0",
                "feature_vector": "prediction.features.1.0",
                "fundamental_snapshot": "prediction.fundamentals.1.0",
                "news_event_evidence": "prediction.news-events.1.0",
                "historical_dataset": "prediction.dataset.1.0",
                "evidence_bundle": "prediction.evidence-bundle.1.0",
            },
            "learning": learning_boundary_contract(),
            "safety": {
                "paper_only": PAPER_ONLY,
                "live_trading": LIVE_TRADING,
                "broker_connected": BROKER_CONNECTED,
                "automatic_promotion": AUTOMATIC_PROMOTION,
                "at_07": False,
            },
        }

    def close(self) -> None:
        self._store.close()

    # -- internals --------------------------------------------------------
    def _at(self, value: datetime | None) -> datetime:
        chosen = value if value is not None else self._clock()
        if not isinstance(chosen, datetime):
            raise PredictionValidationError("the prediction clock must return a datetime")
        if chosen.tzinfo is None or chosen.utcoffset() is None:
            raise PredictionValidationError("prediction timestamps must be timezone-aware")
        return chosen.astimezone(UTC)

    def _tolerance(self) -> timedelta:
        return timedelta(seconds=self.policy.stage_tolerance_seconds)

    def _require_market_data(self) -> None:
        if self._gateway is None:
            raise PredictionDataError(
                "no market-data engine is attached; prediction data must be read "
                "from the existing MarketDataEngine"
            )

    def _snapshot(
        self, request: PredictionRequest, stage: ConfirmationStage, now: datetime
    ) -> MarketDataSnapshot:
        assert self._gateway is not None
        return self._gateway.snapshot(request, stage=stage, now=now)

    def _validate_snapshot(
        self,
        request: PredictionRequest,
        snapshot: MarketDataSnapshot,
        stage: ConfirmationStage,
        now: datetime,
    ) -> None:
        """Refuse a supplied snapshot that silently bypasses stage freshness."""
        if snapshot.instrument_id != request.instrument_id:
            raise PredictionDataError("the supplied snapshot belongs to another instrument")
        if snapshot.exchange != request.exchange:
            raise PredictionDataError("the supplied snapshot belongs to another exchange")
        if snapshot.source_observed_at > now:
            raise PredictionDataError("the supplied snapshot observation is in the future")
        if not snapshot.usable:
            return
        if snapshot.provider_conflict or snapshot.missing_observation:
            raise PredictionDataError(
                "a usable snapshot may not be missing or provider-conflicted"
            )
        if snapshot.freshness.value in (
            "stale",
            "missing",
            "provider_unavailable",
            "unknown",
        ):
            raise PredictionDataError(
                f"the supplied snapshot freshness is {snapshot.freshness.value}"
            )
        if snapshot.quality.value not in ("valid", "warning"):
            raise PredictionDataError(
                f"the supplied snapshot quality is {snapshot.quality.value}"
            )
        spec = HORIZON_SPECS[request.horizon]
        age = max(now - snapshot.source_observed_at, timedelta(0))
        max_age = self.policy.max_age_seconds(spec.family, stage)
        if age > timedelta(seconds=max_age):
            raise PredictionDataError(
                f"the supplied snapshot is {int(age.total_seconds())}s old; "
                f"stage {stage.value} allows {max_age}s"
            )
        if not spec.accepts_resolution(snapshot.resolution):
            raise PredictionDataError(
                f"snapshot resolution {snapshot.resolution.value} cannot support "
                f"horizon {request.horizon.value}"
            )

    def _require_lifecycle(self, lifecycle_id: str) -> ForecastLifecycle:
        lifecycle = self._store.get_lifecycle(lifecycle_id)
        if lifecycle is None:
            raise PredictionNotFoundError(f"lifecycle {lifecycle_id!r} does not exist")
        return lifecycle

    def _latest_forecast(self, lifecycle_id: str) -> ForecastOutput | None:
        forecasts = self._store.forecasts(lifecycle_id)
        return forecasts[-1] if forecasts else None

    def _lifecycle_view(
        self, lifecycle: ForecastLifecycle, *, replayed: bool = False
    ) -> dict[str, Any]:
        forecasts = self._store.forecasts(lifecycle.lifecycle_id)
        stages = self._store.stages(lifecycle.lifecycle_id)
        ensembles: list[dict[str, Any]] = []
        for forecast in forecasts:
            payload = self._store.ensemble(forecast.ensemble_id)
            if payload is not None and payload not in ensembles:
                ensembles.append(payload)
        return {
            "lifecycle": lifecycle.to_dict(),
            "forecasts": [item.to_dict() for item in forecasts],
            "stages": [item.to_dict() for item in stages],
            "ensembles": ensembles,
            "evaluations": [
                item.to_dict() for item in self._store.evaluations(lifecycle.lifecycle_id)
            ],
            "replayed": replayed,
        }

    def _lifecycle_id(self, request: PredictionRequest) -> str:
        return prediction_id(
            "plc_",
            request.request_id,
            request.instrument_id,
            request.horizon.value,
            request.target_at.isoformat(),
        )

    def _make_evaluation(
        self, forecast: ForecastOutput, actual_price: Decimal, at: datetime
    ) -> ForecastEvaluation:
        actual_return = float((actual_price / forecast.reference_price) - Decimal("1"))
        spec = HORIZON_SPECS[forecast.forecast_horizon]
        actual_direction = direction_for_return(actual_return, spec.neutral_band_pct)
        error = forecast.predicted_return - actual_return
        brier: float | None = None
        log_loss: float | None = None
        if (
            forecast.probability_status is ProbabilityStatus.CALIBRATED
            and forecast.p_up is not None
            and forecast.p_down is not None
            and forecast.p_neutral is not None
        ):
            target = {
                Direction.UP: (1.0, 0.0, 0.0),
                Direction.DOWN: (0.0, 1.0, 0.0),
                Direction.NEUTRAL: (0.0, 0.0, 1.0),
            }.get(actual_direction)
            if target is not None:
                probabilities = (forecast.p_up, forecast.p_down, forecast.p_neutral)
                brier = sum((p - y) ** 2 for p, y in zip(probabilities, target))
                chosen = max(
                    probabilities[target.index(1.0)],
                    1e-12,
                )
                log_loss = -math.log(chosen)
        inside = forecast.lower_return <= actual_return <= forecast.upper_return
        return ForecastEvaluation(
            evaluation_id=prediction_id(
                "pev_", forecast.lifecycle_id, forecast.forecast_id, format(actual_price, "f")
            ),
            lifecycle_id=forecast.lifecycle_id,
            forecast_id=forecast.forecast_id,
            stage=forecast.lifecycle_stage,
            evaluated_at=at,
            target_at=forecast.target_at,
            reference_price=forecast.reference_price,
            actual_price=actual_price,
            actual_return=actual_return,
            predicted_return=forecast.predicted_return,
            predicted_direction=forecast.direction,
            actual_direction=actual_direction,
            direction_correct=forecast.direction is actual_direction,
            error=error,
            absolute_error=abs(error),
            inside_interval=inside,
            brier=brier,
            log_loss=log_loss,
            status="evaluated",
            reason=(
                "actual direction was neutral"
                if actual_direction is Direction.NEUTRAL
                else ""
            ),
        )

    @staticmethod
    def _count_by(items: Iterable[Any], attribute: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            value = getattr(item, attribute)
            key = getattr(value, "value", str(value))
            counts[key] = counts.get(key, 0) + 1
        return counts

    async def _publish_forecast(self, forecast: ForecastOutput) -> None:
        await self._publish(
            EventType.NOVAMARKET_PREDICTION_FORECAST_GENERATED,
            forecast_id=forecast.forecast_id,
            lifecycle_id=forecast.lifecycle_id,
            instrument_id=forecast.instrument_id,
            horizon=forecast.forecast_horizon.value,
            direction=forecast.direction.value,
            stage=forecast.lifecycle_stage.value,
            target_at=forecast.target_at.isoformat(),
        )

    async def _publish(self, event_type: str, **payload: Any) -> None:
        if self._publisher is None:
            return
        await self._publisher.publish(event_type, **payload)
