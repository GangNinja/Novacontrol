"""Multi-model forecast assembly for PI-01.

The assembler is deliberately model-agnostic.  A future statistical, gradient
boosting or sequence model produces a :class:`ModelPrediction`; this module
decides whether that contribution is eligible, how much weight it carries, and
whether the combined evidence is strong enough to issue a forecast.  It can
abstain, which is a first-class result and not an error to hide.

No model is trained here.  No arbitrary score is relabelled as a calibrated
probability: probabilities are carried only when every participating model
declares calibrated output and supplies all three directional values.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Mapping, Sequence

from novacontrol.novamarket.prediction.models import (
    ENSEMBLE_CONTRACT_VERSION,
    HORIZON_SPECS,
    CalibrationStatus,
    ConfirmationStage,
    Direction,
    EnsembleResult,
    ForecastOutput,
    ForecastValidity,
    MarketDataSnapshot,
    ModelMetadata,
    ModelPrediction,
    ModelState,
    PredictionPolicy,
    PredictionRequest,
    PredictionValidationError,
    ProbabilityStatus,
    digest,
    direction_for_return,
    prediction_id,
    uncertainty_for_width,
)


class EnsembleAssembler:
    """Aggregate eligible model outputs, or abstain from manufacturing certainty."""

    def __init__(self, policy: PredictionPolicy | None = None) -> None:
        self.policy = policy or PredictionPolicy()

    def assemble(
        self,
        request: PredictionRequest,
        snapshot: MarketDataSnapshot,
        predictions: Sequence[ModelPrediction],
        models: Sequence[ModelMetadata],
        *,
        lifecycle_id: str = "",
        stage: ConfirmationStage = ConfirmationStage.INITIAL,
        now: datetime | None = None,
    ) -> EnsembleResult:
        chosen_now = now if now is not None else datetime.now(UTC)
        if chosen_now.tzinfo is None or chosen_now.utcoffset() is None:
            raise PredictionValidationError("ensemble evaluation time must be timezone-aware")
        now = chosen_now.astimezone(UTC)
        spec = HORIZON_SPECS[request.horizon]
        excluded: list[dict[str, object]] = []
        selected: list[tuple[ModelPrediction, ModelMetadata, float]] = []
        model_map: dict[tuple[str, str], ModelMetadata] = {
            (item.model_id, item.version): item for item in models
        }
        seen_models: set[str] = set()

        if not snapshot.usable:
            snapshot_reasons = snapshot.reasons or (
                "market data snapshot is not usable",
            )
            return self._abstain(
                request, snapshot, (), excluded, snapshot_reasons
            )
        if snapshot.instrument_id != request.instrument_id:
            return self._abstain(
                request,
                snapshot,
                (),
                excluded,
                ("the market snapshot belongs to a different instrument",),
            )
        if not spec.accepts_resolution(snapshot.resolution):
            return self._abstain(
                request,
                snapshot,
                (),
                excluded,
                (
                    f"a {request.horizon.value} forecast may not be made from "
                    f"{snapshot.resolution.value} data",
                ),
            )
        if snapshot.source_observed_at > now:
            return self._abstain(
                request,
                snapshot,
                (),
                excluded,
                ("the market observation timestamp is in the future",),
            )

        for prediction in predictions:
            exclusion_reasons: list[str] = []
            metadata = model_map.get((prediction.model_id, prediction.model_version))
            if not prediction.valid:
                exclusion_reasons.append("model marked its output invalid")
            if prediction.instrument_id != request.instrument_id:
                exclusion_reasons.append("instrument does not match the request")
            if prediction.horizon != request.horizon:
                exclusion_reasons.append("horizon does not match the request")
            if prediction.data_snapshot_id != snapshot.snapshot_id:
                exclusion_reasons.append("model did not use the request's data snapshot")
            if prediction.reference_price != snapshot.reference_price:
                exclusion_reasons.append("reference price differs from the data snapshot")
            if prediction.model_id in seen_models:
                exclusion_reasons.append("duplicate model contribution")
            if metadata is None:
                exclusion_reasons.append("model is not registered")
            elif metadata.version != prediction.model_version:
                exclusion_reasons.append("model version is not registered")
            elif metadata.state not in (ModelState.ELIGIBLE, ModelState.DEGRADED):
                exclusion_reasons.append(f"model state is {metadata.state.value}")
            elif prediction.family is not metadata.family:
                exclusion_reasons.append("model family does not match the registry")
            elif not metadata.supports(request.horizon):
                exclusion_reasons.append("model does not support this horizon")
            if prediction.generated_at > now:
                exclusion_reasons.append("model output was generated in the future")
            if prediction.generated_at >= request.target_at:
                exclusion_reasons.append("model output was generated at or after the target")
            if prediction.data_cutoff > snapshot.received_at:
                exclusion_reasons.append("model used data received after the snapshot")

            if exclusion_reasons:
                excluded.append(
                    {
                        "model_id": prediction.model_id,
                        "version": prediction.model_version,
                        "reasons": exclusion_reasons,
                    }
                )
                continue
            assert metadata is not None
            weight = float(metadata.weight)
            if metadata.state is ModelState.DEGRADED:
                weight *= self.policy.degraded_model_weight
            if weight <= 0:
                excluded.append(
                    {
                        "model_id": prediction.model_id,
                        "version": prediction.model_version,
                        "reasons": ["model weight is not positive"],
                    }
                )
                continue
            selected.append((prediction, metadata, weight))
            seen_models.add(prediction.model_id)

        selected.sort(key=lambda item: (-item[2], item[0].model_id, item[0].model_version))
        if len(selected) > self.policy.max_models:
            for prediction, _metadata, _weight in selected[self.policy.max_models :]:
                excluded.append(
                    {
                        "model_id": prediction.model_id,
                        "version": prediction.model_version,
                        "reasons": ["ensemble model limit reached"],
                    }
                )
            selected = selected[: self.policy.max_models]

        if len(selected) < self.policy.min_models:
            return self._abstain(
                request,
                snapshot,
                (),
                excluded,
                (
                    f"only {len(selected)} eligible model(s); "
                    f"{self.policy.min_models} required",
                ),
            )

        total_weight = sum(item[2] for item in selected)
        if total_weight <= 0:
            return self._abstain(
                request, snapshot, (), excluded, ("ensemble weight is zero",)
            )
        weights = {
            f"{prediction.model_id}@{prediction.model_version}": weight / total_weight
            for prediction, _metadata, weight in selected
        }
        mean_return = sum(
            prediction.predicted_return * weight
            for prediction, _metadata, weight in selected
        ) / total_weight
        mean_lower = sum(
            prediction.lower_return * weight for prediction, _metadata, weight in selected
        ) / total_weight
        mean_upper = sum(
            prediction.upper_return * weight for prediction, _metadata, weight in selected
        ) / total_weight
        disagreement = math.sqrt(
            sum(
                weight * (prediction.predicted_return - mean_return) ** 2
                for prediction, _metadata, weight in selected
            )
            / total_weight
        )
        ensemble_reasons: list[str] = []

        if disagreement >= self.policy.abstain_disagreement:
            return self._abstain(
                request,
                snapshot,
                tuple(item[0] for item in selected),
                excluded,
                ("ensemble abstained: model disagreement exceeds the configured limit",),
                disagreement=disagreement,
                weights=weights,
            )

        interval_floor = max(
            HORIZON_SPECS[request.horizon].neutral_band_pct / 100.0,
            0.0005,
        )
        half_width = max(
            mean_return - mean_lower,
            mean_upper - mean_return,
            interval_floor,
        )
        if disagreement > self.policy.max_disagreement:
            half_width = max(half_width, disagreement)
        lower_return = mean_return - half_width
        upper_return = mean_return + half_width
        width = upper_return - lower_return

        calibrated = all(
            metadata.calibration is CalibrationStatus.CALIBRATED
            and prediction.calibration is CalibrationStatus.CALIBRATED
            and prediction.p_up is not None
            and prediction.p_down is not None
            and prediction.p_neutral is not None
            for prediction, metadata, _weight in selected
        )
        p_up: float | None
        p_down: float | None
        p_neutral: float | None
        confidence: float | None
        if calibrated:
            p_up = sum(
                float(prediction.p_up or 0.0) * weight
                for prediction, _metadata, weight in selected
            ) / total_weight
            p_down = sum(
                float(prediction.p_down or 0.0) * weight
                for prediction, _metadata, weight in selected
            ) / total_weight
            p_neutral = sum(
                float(prediction.p_neutral or 0.0) * weight
                for prediction, _metadata, weight in selected
            ) / total_weight
            probability_status = ProbabilityStatus.CALIBRATED
            calibration_status = CalibrationStatus.CALIBRATED
            confidence = max(p_up, p_down, p_neutral)
        else:
            p_up = p_down = p_neutral = None
            probability_status = ProbabilityStatus.UNAVAILABLE
            calibration_status = (
                CalibrationStatus.UNCALIBRATED
                if any(
                    metadata.calibration is not CalibrationStatus.NOT_APPLICABLE
                    for _prediction, metadata, _weight in selected
                )
                else CalibrationStatus.NOT_APPLICABLE
            )
            confidence = None

        if disagreement > self.policy.max_disagreement:
            direction = Direction.UNCERTAIN
            validity = ForecastValidity.VALID
            ensemble_reasons.append(
                "models disagree above the actionable threshold; direction is uncertain"
            )
        elif (
            calibrated
            and confidence is not None
            and confidence < self.policy.min_direction_probability
        ):
            direction = Direction.UNCERTAIN
            validity = ForecastValidity.VALID
            ensemble_reasons.append(
                "no directional probability reaches the minimum confidence threshold"
            )
        else:
            direction = direction_for_return(mean_return, spec.neutral_band_pct)
            validity = ForecastValidity.VALID

        if not snapshot.usable:
            direction = Direction.INVALID
            validity = ForecastValidity.INVALID
        if not spec.accepts_resolution(snapshot.resolution):
            direction = Direction.INVALID
            validity = ForecastValidity.INVALID
        if direction is Direction.INVALID:
            validity = ForecastValidity.INVALID

        generated = now
        forecast_id = prediction_id(
            "pfc_",
            lifecycle_id or request.request_id,
            stage.value,
            request.instrument_id,
            request.horizon.value,
            generated.isoformat(),
            snapshot.snapshot_id,
        )
        ensemble_id = prediction_id(
            "pens_",
            request.instrument_id,
            request.horizon.value,
            request.target_at.isoformat(),
            ",".join(sorted(weights)),
        )
        member_versions = tuple(
            f"{prediction.model_id}@{prediction.model_version}"
            for prediction, _metadata, _weight in selected
        )
        feature_versions = {
            (
                prediction.feature_set_version
                if prediction.feature_set_version != "unspecified"
                else metadata.feature_set_version
            )
            for prediction, metadata, _weight in selected
        }
        dataset_versions = {
            (
                prediction.dataset_version
                if prediction.dataset_version != "unspecified"
                else metadata.dataset_version
            )
            for prediction, metadata, _weight in selected
        }
        feature_set_version = (
            next(iter(feature_versions)) if len(feature_versions) == 1 else "mixed"
                    )
        dataset_version = (
            next(iter(dataset_versions)) if len(dataset_versions) == 1 else "mixed"
        )
        if lower_return <= -1.0:
            return self._abstain(
                request,
                snapshot,
                tuple(item[0] for item in selected),
                excluded,
                (
                    "the weighted lower return would imply a non-positive price; "
                    "the ensemble refuses to construct that forecast",
                ),
                disagreement=disagreement,
                weights=weights,
            )
        predicted_price = snapshot.reference_price * (
            Decimal("1") + Decimal(str(mean_return))
        )
        lower_price = snapshot.reference_price * (
            Decimal("1") + Decimal(str(lower_return))
        )
        upper_price = snapshot.reference_price * (
            Decimal("1") + Decimal(str(upper_return))
        )
        uncertainty = uncertainty_for_width(width, self.policy)
        notes = tuple(dict.fromkeys(str(item) for item in ensemble_reasons))
        conditions = [
            "Forecast is evidence, not an order or a guarantee.",
            "NM-08 remains the sole risk authority and NM-10 the sole paper-execution authority.",
            "A forecast is invalid if its target, data snapshot or lifecycle is invalidated.",
            f"Input resolution {snapshot.resolution.value} is compatible with horizon {request.horizon.value}.",
        ]
        payload = {
            "forecast_id": forecast_id,
            "lifecycle_id": lifecycle_id,
            "stage": stage.value,
            "instrument_id": request.instrument_id,
            "horizon": request.horizon.value,
            "target_at": request.target_at.isoformat(),
            "generated_at": generated.isoformat(),
            "data_snapshot_id": snapshot.snapshot_id,
            "ensemble_id": ensemble_id,
            "members": list(member_versions),
            "predicted_return": round(mean_return, 10),
            "lower_return": round(lower_return, 10),
            "upper_return": round(upper_return, 10),
        }
        output = ForecastOutput(
            forecast_id=forecast_id,
            lifecycle_id=lifecycle_id,
            lifecycle_stage=stage,
            instrument_id=request.instrument_id,
            exchange=request.exchange,
            currency=snapshot.currency,
            reference_price=snapshot.reference_price,
            reference_at=snapshot.source_observed_at,
            predicted_price=predicted_price,
            predicted_return=mean_return,
            direction=direction,
            lower_return=lower_return,
            upper_return=upper_return,
            lower_price=lower_price,
            upper_price=upper_price,
            uncertainty=uncertainty,
            uncertainty_width=width,
            p_up=p_up,
            p_down=p_down,
            p_neutral=p_neutral,
            probability_status=probability_status,
            calibration_status=calibration_status,
            confidence=confidence,
            ensemble_id=ensemble_id,
            model_id="ensemble",
            model_version=ENSEMBLE_CONTRACT_VERSION,
            member_versions=member_versions,
            feature_set_version=feature_set_version,
            dataset_version=dataset_version,
            forecast_horizon=request.horizon,
            target_at=request.target_at,
            generated_at=generated,
            data_observed_at=snapshot.source_observed_at,
            data_received_at=snapshot.received_at,
            data_resolution=snapshot.resolution,
            data_freshness=snapshot.freshness,
            data_quality=snapshot.quality,
            data_snapshot_id=snapshot.snapshot_id,
            validity=validity,
            validity_conditions=tuple(conditions),
            disagreement=disagreement,
            digest=digest(payload),
            notes=notes,
            reasons=notes,
            metadata={
                "eligible_models": [prediction.model_id for prediction, _m, _w in selected],
                "weights": weights,
                "member_count": len(selected),
                "policy": self.policy.to_dict(),
                "calibration_note": (
                    "Probabilities are withheld unless every participating model is calibrated."
                ),
            },
        )
        return EnsembleResult(
            ensemble_id=ensemble_id,
            version=ENSEMBLE_CONTRACT_VERSION,
            status="assembled",
            output=output,
            eligible_models=tuple(prediction.model_id for prediction, _m, _w in selected),
            excluded_models=tuple(excluded),
            weights=weights,
            disagreement=disagreement,
            reasons=notes,
        )

    def _abstain(
        self,
        request: PredictionRequest,
        snapshot: MarketDataSnapshot,
        predictions: Sequence[ModelPrediction],
        excluded: Sequence[Mapping[str, object]],
        reasons: Sequence[str],
        *,
        disagreement: float = 0.0,
        weights: Mapping[str, float] | None = None,
    ) -> EnsembleResult:
        ensemble_id = prediction_id(
            "pens_",
            request.instrument_id,
            request.horizon.value,
            request.target_at.isoformat(),
            snapshot.snapshot_id,
        )
        return EnsembleResult(
            ensemble_id=ensemble_id,
            version=ENSEMBLE_CONTRACT_VERSION,
            status="abstained",
            output=None,
            eligible_models=tuple(item.model_id for item in predictions),
            excluded_models=tuple(dict(item) for item in excluded),
            weights=dict(weights or {}),
            disagreement=disagreement,
            reasons=tuple(dict.fromkeys(str(item) for item in reasons)),
        )

