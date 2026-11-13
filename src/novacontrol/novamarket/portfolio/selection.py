"""Asset selection for NM-07 portfolios.

Selection is a decision, not a sort by predicted return: an asset must pass the
horizon's discovery-score, data-quality, forecast-freshness, model-state and
uncertainty gates, and its rank combines those with a deliberately modest
expected-edge term. Every rejected candidate keeps its reason, so the decision is
explainable after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from novacontrol.novamarket.portfolio.models import PortfolioPolicy

UNCERTAINTY_RANK = {"low": 0, "medium": 1, "high": 2}
MODEL_FACTOR = {"active": 1.0, "validated": 0.85, "degraded": 0.5}
UNCERTAINTY_FACTOR = {"low": 1.0, "medium": 0.9, "high": 0.75}


@dataclass(frozen=True, slots=True)
class Assessment:
    """One candidate's selection evidence and its outcome."""

    instrument_id: str
    sector: str
    asset_class: str
    discovery_score: float
    confidence: float
    data_quality: str
    forecast_id: str | None
    forecast_horizon: str | None
    model_id: str | None
    model_state: str | None
    predicted_return: float
    uncertainty: str | None
    interval_width: float | None
    forecast_age_hours: float | None
    event_ids: tuple[str, ...]
    event_pressure: bool
    rank: float
    reasons: tuple[str, ...]
    rejection: str = ""

    @property
    def selected(self) -> bool:
        return not self.rejection

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "sector": self.sector,
            "asset_class": self.asset_class,
            "discovery_score": round(self.discovery_score, 2),
            "confidence": round(self.confidence, 3),
            "data_quality": self.data_quality,
            "forecast_id": self.forecast_id,
            "forecast_horizon": self.forecast_horizon,
            "model_id": self.model_id,
            "model_state": self.model_state,
            "predicted_return": round(self.predicted_return, 6),
            "uncertainty": self.uncertainty,
            "interval_width": (
                round(self.interval_width, 6) if self.interval_width is not None else None
            ),
            "forecast_age_hours": (
                round(self.forecast_age_hours, 3)
                if self.forecast_age_hours is not None
                else None
            ),
            "event_ids": list(self.event_ids),
            "event_pressure": self.event_pressure,
            "rank": round(self.rank, 4),
            "selected": self.selected,
            "reasons": list(self.reasons),
            "rejection": self.rejection,
        }


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _evaluation(row: Mapping[str, Any]) -> Mapping[str, Any]:
    evaluation = row.get("evaluation")
    return evaluation if isinstance(evaluation, Mapping) else {}


def _forecast_for(
    forecasts: Mapping[str, Mapping[str, Mapping[str, Any]]],
    instrument_id: str,
    policy: PortfolioPolicy,
    now: datetime,
) -> tuple[Mapping[str, Any] | None, str | None, str]:
    """The first policy horizon with a fresh forecast for this instrument."""
    available = forecasts.get(instrument_id) or {}
    for horizon in policy.forecast_horizons:
        body = available.get(horizon)
        if not body:
            continue
        created = body.get("created_at")
        age: float | None = None
        if created:
            try:
                moment = datetime.fromisoformat(str(created))
                age = (now - moment).total_seconds() / 3600.0
            except ValueError:
                age = None
        if age is None:
            return None, horizon, "forecast has no readable creation time"
        if age < 0:
            return None, horizon, "forecast is timestamped in the future"
        if age > policy.max_forecast_age_hours:
            return (
                None,
                horizon,
                f"{horizon} forecast is {age:.1f}h old; the limit is "
                f"{policy.max_forecast_age_hours:.0f}h",
            )
        return body, horizon, ""
    return None, None, "no forecast for any horizon this portfolio uses"


def assess_candidates(
    rows: Sequence[Mapping[str, Any]],
    forecasts: Mapping[str, Mapping[str, Mapping[str, Any]]],
    model_states: Mapping[tuple[str, str, str], str],
    policy: PortfolioPolicy,
    now: datetime,
    *,
    event_pressure: Mapping[str, bool] | None = None,
) -> tuple[list[Assessment], list[Assessment]]:
    """Assess every candidate; return (selected, rejected-with-reasons)."""
    pressure = event_pressure or {}
    assessed: list[Assessment] = []
    for row in rows:
        instrument_id = str(row.get("instrument_id") or "")
        if not instrument_id:
            continue
        evaluation = _evaluation(row)
        score = float(evaluation.get("score", row.get("last_score", 0.0)) or 0.0)
        confidence = float(evaluation.get("confidence", 0.0) or 0.0)
        data_quality = str(
            evaluation.get("data_quality") or row.get("data_quality") or "unknown"
        )
        sector = str(row.get("sector") or evaluation.get("sector") or "unknown")
        asset_class = str(
            row.get("asset_class") or evaluation.get("asset_class") or "unknown"
        )
        event_ids = tuple(str(item) for item in evaluation.get("event_ids", ()) or ())
        pressurised = bool(pressure.get(instrument_id))
        forecast, horizon, forecast_problem = _forecast_for(
            forecasts, instrument_id, policy, now
        )
        reasons: list[str] = []
        rejection = ""
        model_state: str | None = None
        age: float | None = None
        predicted = 0.0
        uncertainty: str | None = None
        width: float | None = None

        if score < policy.min_candidate_score:
            rejection = (
                f"discovery score {score:.1f} is below the policy minimum "
                f"{policy.min_candidate_score:.1f}"
            )
        elif data_quality in {"insufficient", "invalid"}:
            rejection = f"data quality is {data_quality}"
        elif forecast_problem:
            rejection = forecast_problem
        else:
            assert forecast is not None and horizon is not None
            model_id = str(forecast.get("model_id") or "")
            model_state = model_states.get((instrument_id, model_id, horizon))
            if model_state is None:
                rejection = f"model {model_id!r} has no measured state for this horizon"
            elif model_state not in policy.model_states:
                rejection = f"model state {model_state} is excluded by this policy"
            else:
                uncertainty = str(forecast.get("uncertainty") or "unknown")
                if uncertainty not in UNCERTAINTY_RANK:
                    rejection = "forecast uncertainty is not a recognised level"
                elif (
                    UNCERTAINTY_RANK[uncertainty]
                    > UNCERTAINTY_RANK[policy.max_uncertainty]
                ):
                    rejection = (
                        f"uncertainty {uncertainty} exceeds the policy limit "
                        f"{policy.max_uncertainty}"
                    )
                elif policy.exclude_event_pressure and pressurised:
                    rejection = (
                        "a confirmed high-importance event is open on this "
                        "instrument and this horizon excludes event pressure"
                    )

        if rejection:
            assessed.append(
                Assessment(
                    instrument_id=instrument_id,
                    sector=sector,
                    asset_class=asset_class,
                    discovery_score=score,
                    confidence=confidence,
                    data_quality=data_quality,
                    forecast_id=(str(forecast.get("forecast_id")) if forecast else None),
                    forecast_horizon=horizon,
                    model_id=(str(forecast.get("model_id")) if forecast else None),
                    model_state=model_state,
                    predicted_return=predicted,
                    uncertainty=uncertainty,
                    interval_width=width,
                    forecast_age_hours=age,
                    event_ids=event_ids,
                    event_pressure=pressurised,
                    rank=0.0,
                    reasons=tuple(reasons),
                    rejection=rejection,
                )
            )
            continue

        assert forecast is not None and horizon is not None
        predicted = float(forecast.get("predicted_return") or 0.0)
        lower = forecast.get("lower_return")
        upper = forecast.get("upper_return")
        if lower is not None and upper is not None:
            width = float(upper) - float(lower)
        created = datetime.fromisoformat(str(forecast.get("created_at")))
        age = (now - created).total_seconds() / 3600.0
        model_id = str(forecast.get("model_id") or "")
        model_factor = MODEL_FACTOR.get(model_state or "", 0.5)
        uncertainty_factor = UNCERTAINTY_FACTOR.get(uncertainty or "", 0.75)
        confidence_factor = 0.5 + 0.5 * _clamp(confidence, 0.0, 1.0)
        event_factor = 1.0
        if event_ids:
            event_factor *= 0.95
            reasons.append(f"{len(event_ids)} verified event(s) inform the forecast")
        if pressurised:
            event_factor *= 0.85
            reasons.append("confirmed high-importance event widens the interval")
        edge = _clamp(predicted * 5.0, -0.10, 0.10)
        rank = (
            max(score, 0.0)
            * confidence_factor
            * model_factor
            * uncertainty_factor
            * event_factor
            * (1.0 + edge)
        )
        reasons.extend(
            [
                f"discovery score {score:.1f} ({data_quality})",
                f"{horizon} forecast from {model_id} ({model_state})",
                f"uncertainty {uncertainty}, interval {width:.4f}"
                if width is not None
                else f"uncertainty {uncertainty}",
            ]
        )
        if model_state == "degraded":
            reasons.append("degraded model: rank halved")
        assessed.append(
            Assessment(
                instrument_id=instrument_id,
                sector=sector,
                asset_class=asset_class,
                discovery_score=score,
                confidence=confidence,
                data_quality=data_quality,
                forecast_id=str(forecast.get("forecast_id")),
                forecast_horizon=horizon,
                model_id=model_id,
                model_state=model_state,
                predicted_return=predicted,
                uncertainty=uncertainty,
                interval_width=width,
                forecast_age_hours=age,
                event_ids=event_ids,
                event_pressure=pressurised,
                rank=rank,
                reasons=tuple(reasons),
            )
        )

    eligible = sorted(
        (item for item in assessed if item.selected),
        key=lambda item: (-item.rank, item.instrument_id),
    )
    chosen = eligible[: policy.max_positions]
    rejected = [item for item in assessed if not item.selected]
    for item in eligible[policy.max_positions :]:
        rejected.append(
            Assessment(
                instrument_id=item.instrument_id,
                sector=item.sector,
                asset_class=item.asset_class,
                discovery_score=item.discovery_score,
                confidence=item.confidence,
                data_quality=item.data_quality,
                forecast_id=item.forecast_id,
                forecast_horizon=item.forecast_horizon,
                model_id=item.model_id,
                model_state=item.model_state,
                predicted_return=item.predicted_return,
                uncertainty=item.uncertainty,
                interval_width=item.interval_width,
                forecast_age_hours=item.forecast_age_hours,
                event_ids=item.event_ids,
                event_pressure=item.event_pressure,
                rank=item.rank,
                reasons=item.reasons,
                rejection=(
                    f"ranked below the policy's {policy.max_positions} position limit"
                ),
            )
        )
    rejected.sort(key=lambda item: item.instrument_id)
    return chosen, rejected


__all__ = ["Assessment", "assess_candidates"]
