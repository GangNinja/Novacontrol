"""Evidence-based, reproducible ensemble weights.

Only validation/walk-forward evidence is scored.  Test results are deliberately
not part of this module: using them to choose weights would tune the ensemble on
the same observations that are supposed to report its performance.
"""

from __future__ import annotations

import math
import statistics
from datetime import UTC, datetime
from typing import Mapping, Sequence

from .models import (
    EligibilityDecision,
    EnsembleConfig,
    EnsembleValidationError,
    ModelEvidence,
    WeightResult,
)

_UNSUITABLE_STATUSES = frozenset(
    {
        "failed",
        "unsupported",
        "insufficient_data",
        "baselines_preferred",
        "unavailable",
        "suspended",
        "retired",
    }
)


def _key(model_id: str, version: str) -> str:
    return f"{model_id}@{version}"


def _age_factor(evidence: ModelEvidence, now: datetime, half_life_days: float) -> float:
    trained = evidence.trained_at
    if trained is None:
        return 1.0
    if trained.tzinfo is None or trained.utcoffset() is None:
        return 1.0
    age_days = max(0.0, (now.astimezone(UTC) - trained.astimezone(UTC)).total_seconds() / 86400.0)
    if half_life_days <= 0:
        return 1.0
    return float(0.5 ** (age_days / half_life_days))


def _direction_factor(value: float | None) -> float:
    if value is None:
        return 0.75
    return 0.5 + 0.5 * max(0.0, min(1.0, float(value)))


def _evidence_mae(evidence: ModelEvidence) -> float | None:
    if evidence.validation_mae is not None:
        return float(evidence.validation_mae)
    if evidence.walk_forward_mae is not None:
        return float(evidence.walk_forward_mae)
    return None


def assess_evidence(
    evidence: Sequence[ModelEvidence],
    config: EnsembleConfig,
    *,
    regime: str = "",
    previous_weights: Mapping[str, float] | None = None,
    previous_config_id: str = "",
    now: datetime | None = None,
    base_reasons: Mapping[str, Sequence[str]] | None = None,
) -> WeightResult:
    """Assess eligibility and return normalized, smoothed weights.

    A model without usable validation evidence is excluded.  Equal weights are
    only an explicit baseline when every included model has evidence; they are
    never used to make an unevidenced model look competitive.
    """
    chosen_now = now if now is not None else datetime.now(UTC)
    reasons_by_key = {
        str(key): tuple(str(item) for item in value)
        for key, value in (base_reasons or {}).items()
    }
    decisions: list[EligibilityDecision] = []
    eligible: list[tuple[ModelEvidence, float, float | None]] = []
    for item in evidence:
        key = _key(item.model_id, item.version)
        reasons = list(reasons_by_key.get(key, ()))
        if item.state.value in {"suspended", "unavailable", "retired"}:
            reasons.append(f"model state is {item.state.value}")
        elif item.state.value == "degraded" and not config.allow_degraded:
            reasons.append("degraded models are disabled by configuration")
        if item.status.strip().lower() in _UNSUITABLE_STATUSES:
            reasons.append(f"existing evaluation status is {item.status}")
        regime_mae = (
            item.regime_mae
            if config.regime_aware
            and str(regime or "").strip()
            and item.regime == str(regime).strip()
            and item.regime_samples >= config.min_regime_samples
            else None
        )
        mae = regime_mae if regime_mae is not None else _evidence_mae(item)
        if config.require_training_evidence:
            if mae is None:
                reasons.append("no validation or walk-forward evidence")
            if item.validation_samples < config.min_validation_samples:
                reasons.append(
                    f"{item.validation_samples} validation samples; "
                    f"{config.min_validation_samples} required"
                )
        if mae is not None and (
            not math.isfinite(mae) or mae < 0.0
        ):
            reasons.append("validation error is not finite and non-negative")
        if reasons:
            decisions.append(
                EligibilityDecision(
                    model_id=item.model_id,
                    version=item.version,
                    family=item.family,
                    eligible=False,
                    weight=0.0,
                    reasons=tuple(dict.fromkeys(reasons)),
                    evidence=item.to_dict(),
                )
            )
            continue
        accuracy = (
            item.regime_direction_accuracy
            if regime_mae is not None and item.regime_direction_accuracy is not None
            else item.validation_direction_accuracy
            if item.validation_direction_accuracy is not None
            else item.walk_forward_direction_accuracy
        )
        eligible.append((item, mae if mae is not None else 0.0, accuracy))

    if not eligible:
        return WeightResult(
            weights={},
            decisions=tuple(decisions),
            methodology=config.weighting_method,
            config_digest=config.digest,
            previous_config_id=previous_config_id,
            reasons=("no model has usable validation evidence",),
        )

    maes = [max(0.0, mae) for _item, mae, _accuracy in eligible]
    usable = [value for value in maes if value > 0.0]
    scale = statistics.median(usable) if usable else 1.0
    raw: dict[str, float] = {}
    for item, mae, accuracy in eligible:
        key = _key(item.model_id, item.version)
        # A relative error scale prevents a single tiny MAE from dominating.
        relative = mae / max(scale, 1e-12)
        score = 1.0 / (1.0 + relative)
        score *= _direction_factor(accuracy)
        score *= _age_factor(item, chosen_now, config.recency_half_life_days)
        if item.state.value == "degraded":
            score *= config.degraded_penalty
        if score <= 0.0 or not math.isfinite(score):
            score = 1e-12
        raw[key] = score

    total = sum(raw.values())
    if total <= 0.0:
        raw = {key: 1.0 for key in raw}
        total = float(len(raw))
    weights = {key: value / total for key, value in raw.items()}

    if previous_weights:
        prior = {
            key: max(0.0, float(previous_weights.get(key, 0.0)))
            for key in weights
        }
        prior_total = sum(prior.values())
        if prior_total > 0.0:
            prior = {key: value / prior_total for key, value in prior.items()}
            # Equal-weight prior keeps a small sample from suddenly changing
            # the whole configuration; it is a shrink toward stability, not a
            # claim that all models are equally good.
            blend = config.smoothing
            weights = {
                key: blend * prior[key] + (1.0 - blend) * weights[key]
                for key in weights
            }

    # Project onto the bounded simplex.  Clamping and then renormalizing can
    # push the largest weight back above the cap; redistribute the excess to
    # the remaining headroom instead.
    weights = _bounded_weights(weights, config.min_weight, config.max_weight)
    total = sum(weights.values())
    if total <= 0.0:
        weights = {key: 1.0 / len(weights) for key in weights}
    else:
        weights = {key: value / total for key, value in weights.items()}

    validate_weights(weights)
    for item, _mae, _accuracy in eligible:
        key = _key(item.model_id, item.version)
        decision_reasons = [
            "validation evidence accepted",
            "weight is inverse relative validation error, smoothed and capped",
        ]
        if (
            config.regime_aware
            and str(regime or "").strip()
            and item.regime == str(regime).strip()
            and item.regime_samples >= config.min_regime_samples
            and item.regime_mae is not None
        ):
            decision_reasons.append(
                f"regime-specific evidence used for {item.regime}"
            )
        decisions.append(
            EligibilityDecision(
                model_id=item.model_id,
                version=item.version,
                family=item.family,
                eligible=True,
                weight=weights[key],
                reasons=tuple(decision_reasons),
                evidence={
                    **item.to_dict(),
                    "effective_weight_before_normalization": raw.get(key, 0.0),
                },
            )
        )

    return WeightResult(
        weights=weights,
        decisions=tuple(decisions),
        methodology=config.weighting_method,
        config_digest=config.digest,
        previous_config_id=previous_config_id,
        reasons=(
            "weights use validation/walk-forward evidence only",
            "no test-set result is used to select or reweight a model",
        ),
    )


def equal_weights(evidence: Sequence[ModelEvidence]) -> dict[str, float]:
    """Explicit equal-weight baseline for tests and transparent comparisons."""
    if not evidence:
        return {}
    value = 1.0 / len(evidence)
    return {_key(item.model_id, item.version): value for item in evidence}


def _bounded_weights(
    weights: Mapping[str, float], minimum: float, maximum: float
) -> dict[str, float]:
    """Project weights onto ``[minimum, maximum]`` while summing to one."""
    values = {str(key): float(value) for key, value in weights.items()}
    if not values:
        return {}
    if len(values) == 1:
        # A sole eligible model necessarily carries all the weight; a per-model
        # cap cannot make a one-model weighted blend sum to one.
        return {next(iter(values)): 1.0}
    if minimum * len(values) > 1.0 + 1e-12:
        raise EnsembleValidationError(
            "minimum weight multiplied by the model count exceeds one"
        )
    if maximum * len(values) < 1.0 - 1e-12:
        raise EnsembleValidationError(
            "maximum weight multiplied by the model count is below one"
        )
    total = sum(values.values())
    if total <= 0.0:
        values = {key: 1.0 / len(values) for key in values}
    else:
        values = {key: value / total for key, value in values.items()}
    for _ in range(100):
        values = {
            key: min(maximum, max(minimum, value)) for key, value in values.items()
        }
        total = sum(values.values())
        difference = 1.0 - total
        if abs(difference) <= 1e-12:
            break
        if difference > 0.0:
            movable = {
                key: maximum - value for key, value in values.items() if value < maximum
            }
            capacity = sum(movable.values())
            if capacity <= 0.0:
                break
            share = min(difference, capacity)
            for key, room in movable.items():
                values[key] += share * room / capacity
        else:
            movable = {
                key: value - minimum for key, value in values.items() if value > minimum
            }
            capacity = sum(movable.values())
            if capacity <= 0.0:
                break
            share = min(-difference, capacity)
            for key, room in movable.items():
                values[key] -= share * room / capacity
    return values


def validate_weights(weights: Mapping[str, float]) -> None:
    if not weights:
        raise EnsembleValidationError("ensemble weights may not be empty")
    values = []
    for key, value in weights.items():
        number = float(value)
        if not str(key).strip():
            raise EnsembleValidationError("a weight must name its model")
        if not math.isfinite(number) or number < 0.0:
            raise EnsembleValidationError(f"weight for {key!r} is invalid")
        values.append(number)
    if abs(sum(values) - 1.0) > 1e-6:
        raise EnsembleValidationError("ensemble weights must sum to one")


__all__ = ["assess_evidence", "equal_weights", "validate_weights"]
