"""Point-in-time observation construction and validation for PI-13.

Only completed bars whose ``available_at`` is no later than the observation
timestamp are read.  A feature or forecast row stamped after that timestamp is
withheld and reported as a blocked future-leak attempt rather than silently
used.  Missing, zero, imputed and invalid values remain distinct.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from novacontrol.novamarket.backtesting.core import HistoricalBar

from .models import (
    RL_OBSERVATION_SCHEMA_VERSION,
    RLDataError,
    RLEnvironmentConfig,
    RLObservation,
    RLPortfolioState,
    RLValidationError,
    decimal_text,
    parse_time,
    sanitize_observation_number,
)

# PI-01 marks a forecast ``stale`` when its input data was old at generation
# time.  Historical replay reads those same rows later, so freshness must be
# measured from the forecast's generation time at the observation timestamp.
_MAX_REPLAY_FORECAST_AGE = timedelta(hours=72)


def _observation_id(
    environment_id: str,
    episode_id: str,
    timestamp: datetime,
    instrument_id: str,
    bar: HistoricalBar,
) -> str:
    payload = "|".join(
        (
            environment_id,
            episode_id,
            timestamp.isoformat(),
            instrument_id,
            bar.source,
            decimal_text(bar.close),
            decimal_text(bar.volume),
        )
    )
    return "rl_obs_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _average(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, Decimal("0")) / Decimal(len(values)) if values else None


class ObservationBuilder:
    """Build one validated observation from history ending at the active bar."""

    def __init__(self, config: RLEnvironmentConfig) -> None:
        self._config = config

    def build(
        self,
        *,
        environment_id: str,
        episode_id: str,
        timestamp: datetime,
        bar: HistoricalBar,
        history: Sequence[HistoricalBar],
        portfolio: RLPortfolioState,
        feature_snapshot: Mapping[str, Any] | None,
        forecast_snapshot: Mapping[str, Any] | None,
        phase: str = "decision_after_completed_bar",
    ) -> RLObservation:
        at = parse_time(timestamp, "observation timestamp")
        if bar.instrument_id != self._config.instrument_id:
            raise RLDataError("observation instrument does not match the environment")
        if bar.observed_at != at:
            raise RLDataError("observation timestamp does not match the active bar")
        if bar.available_at is None or bar.available_at > at:
            raise RLDataError("bar was not available at the observation timestamp")
        if not history or history[-1].observed_at != at:
            raise RLDataError("observation history must end at the active bar")
        if any(item.observed_at > at for item in history):
            raise RLDataError("observation history contains future bars")
        if any(item.available_at is None or item.available_at > at for item in history):
            raise RLDataError("observation history contains unavailable bars")

        market = self._market(history)
        features, feature_quality = self._features(feature_snapshot, at)
        forecasts, forecast_quality = self._forecasts(forecast_snapshot, at)
        missing = list(feature_quality["missing"]) + list(forecast_quality["missing"])
        invalid = list(feature_quality["invalid"]) + list(forecast_quality["invalid"])
        imputed = list(feature_quality["imputed"])
        blocked = list(feature_quality["future_leak_blocked"]) + list(
            forecast_quality["future_leak_blocked"]
        )
        if invalid:
            quality = "invalid"
        elif missing or blocked or imputed or feature_quality["stale"] or forecast_quality["stale"]:
            quality = "partial"
        else:
            quality = "valid"
        feature_version = str(features.get("version") or "unavailable")
        feature_set_id = str(features.get("feature_set_id") or "")
        feature_as_of = features.get("as_of")
        forecast_id = str(forecasts.get("forecast_id") or "")
        forecast_generated_at = forecasts.get("generated_at")
        observation = RLObservation(
            observation_id=_observation_id(environment_id, episode_id, at, bar.instrument_id, bar),
            environment_id=environment_id,
            episode_id=episode_id,
            timestamp=at,
            instrument_id=bar.instrument_id,
            phase=phase,
            market=market,
            features=features,
            forecasts=forecasts,
            portfolio=portfolio.to_dict(),
            provenance={
                "dataset_id": self._config.dataset_id,
                "dataset_version": self._config.dataset_version,
                "bar_source": bar.source,
                "bar_observed_at": bar.observed_at.isoformat(),
                "bar_available_at": bar.available_at.isoformat(),
                "bar_retrieved_at": bar.retrieved_at.isoformat() if bar.retrieved_at else None,
                "adjusted": bar.adjusted,
                "feature_set_version": feature_version,
                "feature_set_id": feature_set_id,
                "feature_as_of": feature_as_of,
                "forecast_id": forecast_id,
                "forecast_generated_at": forecast_generated_at,
                "environment_version": self._config.environment_version,
                "observation_schema_version": RL_OBSERVATION_SCHEMA_VERSION,
            },
            quality={
                "status": quality,
                "missing": sorted(set(missing)),
                "invalid": sorted(set(invalid)),
                "imputed": sorted(set(imputed)),
                "stale": sorted(set(feature_quality["stale"]) | set(forecast_quality["stale"])),
                "future_leak_blocked": sorted(set(blocked)),
                "feature_quality": feature_quality["status"],
                "forecast_quality": forecast_quality["status"],
                "portfolio_present": True,
                "distinguishes_missing_zero_imputed_invalid": True,
            },
        )
        self.validate(observation)
        return observation

    def validate(self, observation: RLObservation) -> None:
        if observation.timestamp > self._config.end:
            raise RLDataError("observation falls after the configured historical end")
        if observation.instrument_id != self._config.instrument_id:
            raise RLDataError("observation instrument is outside the environment universe")
        for name in ("open", "high", "low", "close", "volume"):
            if name not in observation.market:
                raise RLDataError(f"observation market is missing {name}")
        if observation.market["high"] < max(
            observation.market["open"], observation.market["close"], observation.market["low"]
        ):
            raise RLDataError("observation OHLC is inconsistent")
        if observation.market["low"] > min(
            observation.market["open"], observation.market["close"], observation.market["high"]
        ):
            raise RLDataError("observation OHLC is inconsistent")
        _validate_numbers(observation.market, path="market")
        _validate_numbers(observation.portfolio, path="portfolio")
        for name, value in observation.features.get("values", {}).items():
            if value is not None and not _finite(value):
                raise RLDataError(f"feature {name!r} is not finite")
        if observation.provenance.get("dataset_version") != self._config.dataset_version:
            raise RLDataError("observation dataset version does not match the environment")

    def _market(self, history: Sequence[HistoricalBar]) -> dict[str, Any]:
        closes = [item.close for item in history]
        volumes = [item.volume for item in history]
        last = history[-1]
        previous = history[-2] if len(history) > 1 else None
        one = _return(closes, 1)
        five = _return(closes, 5)
        twenty = _return(closes, 20)
        vol_window = closes[-21:]
        returns = [
            float(vol_window[index] / vol_window[index - 1] - Decimal("1"))
            for index in range(1, len(vol_window))
            if vol_window[index - 1]
        ]
        volatility = statistics.pstdev(returns) if len(returns) > 1 else None
        avg_volume = _average(volumes[-20:])
        sma20 = _average(closes[-20:])
        sma50 = _average(closes[-50:])
        support = min(item.low for item in history[-20:])
        resistance = max(item.high for item in history[-20:])
        close = last.close
        return {
            "open": float(last.open),
            "high": float(last.high),
            "low": float(last.low),
            "close": float(close),
            "volume": float(last.volume),
            "adjusted": last.adjusted,
            "return_1": one,
            "return_5": five,
            "return_20": twenty,
            "gap_pct": float((last.open / previous.close) - Decimal("1"))
            if previous and previous.close
            else None,
            "intraday_range_pct": float((last.high - last.low) / close) if close else None,
            "volatility_20": volatility,
            "relative_volume_20": float(last.volume / avg_volume)
            if avg_volume and avg_volume > 0
            else None,
            "liquidity_notional": float(last.volume * close),
            "sma_20": float(sma20) if sma20 is not None else None,
            "sma_50": float(sma50) if sma50 is not None else None,
            "price_vs_sma_20": float(close / sma20 - Decimal("1")) if sma20 else None,
            "price_vs_sma_50": float(close / sma50 - Decimal("1")) if sma50 else None,
            "support_distance_pct": float(close / support - Decimal("1")) if support else None,
            "resistance_distance_pct": float(resistance / close - Decimal("1")) if close else None,
            "history_bars": len(history),
        }

    def _features(
        self, snapshot: Mapping[str, Any] | None, at: datetime
    ) -> tuple[dict[str, Any], dict[str, list[str]]]:
        quality: dict[str, Any] = {
            "status": "unavailable",
            "missing": [],
            "invalid": [],
            "imputed": [],
            "stale": [],
            "future_leak_blocked": [],
        }
        if not self._config.include_features:
            return (
                {
                    "available": False,
                    "status": "disabled",
                    "values": {},
                    "statuses": {},
                    "imputed": {},
                    "version": "disabled",
                },
                quality,
            )
        if not snapshot:
            quality["status"] = "missing"
            quality["missing"].append("features")
            return (
                {
                    "available": False,
                    "status": "missing",
                    "values": {},
                    "statuses": {},
                    "imputed": {},
                    "version": "unavailable",
                },
                quality,
            )
        as_of_raw = snapshot.get("as_of")
        as_of = parse_time(as_of_raw, "feature as_of") if as_of_raw else None
        if as_of is None or as_of > at:
            quality["status"] = "future_leak_blocked"
            quality["future_leak_blocked"].append("features.as_of")
            if not as_of:
                quality["missing"].append("features.as_of")
            return (
                {
                    "available": False,
                    "status": "future_leak_blocked",
                    "values": {},
                    "statuses": {},
                    "imputed": {},
                    "version": "unavailable",
                    "reason": "feature row is undated or post-dates the observation",
                },
                quality,
            )
        raw_values: Mapping[str, Any] = (
            snapshot["values"] if isinstance(snapshot.get("values"), Mapping) else {}
        )
        raw_statuses: Mapping[str, Any] = (
            snapshot["statuses"] if isinstance(snapshot.get("statuses"), Mapping) else {}
        )
        raw_imputed: Mapping[str, Any] = (
            snapshot["imputed"] if isinstance(snapshot.get("imputed"), Mapping) else {}
        )
        values: dict[str, float | None] = {}
        statuses: dict[str, str] = {}
        imputed: dict[str, float] = {}
        for name, raw in raw_values.items():
            status = str(raw_statuses.get(name, "present")).lower()
            number = sanitize_observation_number(raw)
            if status in ("missing", "unavailable"):
                values[str(name)] = None
                statuses[str(name)] = "missing"
                quality["missing"].append(f"features.{name}")
                continue
            if status in ("invalid", "error"):
                values[str(name)] = None
                statuses[str(name)] = "invalid"
                quality["invalid"].append(f"features.{name}")
                continue
            if number is None:
                values[str(name)] = None
                statuses[str(name)] = "invalid"
                quality["invalid"].append(f"features.{name}")
                continue
            values[str(name)] = number
            if status in ("imputed", "stale"):
                statuses[str(name)] = status
                if name in raw_imputed:
                    imputed[str(name)] = float(raw_imputed[name])
                if status == "imputed":
                    quality["imputed"].append(f"features.{name}")
                else:
                    quality["stale"].append(f"features.{name}")
            else:
                statuses[str(name)] = "present"
        if quality["invalid"]:
            quality["status"] = "invalid"
        elif not values:
            quality["status"] = "missing"
        elif quality["missing"] or quality["stale"] or quality["imputed"]:
            quality["status"] = "partial"
        else:
            quality["status"] = "valid"
        if not values:
            quality["missing"].append("features.values")
        return (
            {
                "available": bool(values),
                "status": quality["status"],
                "values": values,
                "statuses": statuses,
                "imputed": imputed,
                "version": str(
                    snapshot.get("version") or snapshot.get("feature_set_version") or "unspecified"
                ),
                "feature_set_id": str(snapshot.get("feature_set_id") or ""),
                "as_of": as_of.isoformat(),
                "quality": str(snapshot.get("quality") or "unspecified"),
            },
            quality,
        )

    def _forecasts(
        self, snapshot: Mapping[str, Any] | None, at: datetime
    ) -> tuple[dict[str, Any], dict[str, list[str]]]:
        quality: dict[str, Any] = {
            "status": "unavailable",
            "missing": [],
            "invalid": [],
            "imputed": [],
            "stale": [],
            "future_leak_blocked": [],
        }
        if not self._config.include_forecasts:
            return ({"available": False, "status": "disabled", "validity": "disabled"}, quality)
        if not snapshot:
            quality["status"] = "missing"
            quality["missing"].append("forecasts")
            return ({"available": False, "status": "missing", "validity": "unavailable"}, quality)
        generated_raw = snapshot.get("generated_at") or snapshot.get(
            "forecast_generation_timestamp"
        )
        generated_at = parse_time(generated_raw, "forecast generated_at") if generated_raw else None
        if generated_at is None or generated_at > at:
            quality["status"] = "future_leak_blocked"
            quality["future_leak_blocked"].append("forecasts.generated_at")
            if generated_at is None:
                quality["missing"].append("forecasts.generated_at")
            return (
                {
                    "available": False,
                    "status": "future_leak_blocked",
                    "validity": "unavailable",
                    "reason": "forecast is undated or post-dates the observation",
                },
                quality,
            )
        stored_validity = str(
            snapshot.get("validity") or snapshot.get("forecast_validity") or "unconfirmed"
        ).lower()
        direction = str(
            snapshot.get("direction") or snapshot.get("predicted_direction") or "uncertain"
        ).lower()
        age_seconds = max(0.0, (at - generated_at).total_seconds())
        invalid_validity = stored_validity in ("invalid", "error")
        # ``expired`` is an explicit terminal status.  A stored ``stale`` is
        # re-evaluated with replay age because PI-01 records it against the wall
        # clock at generation, not against the historical observation.
        stale = (
            invalid_validity
            or stored_validity == "expired"
            or age_seconds > _MAX_REPLAY_FORECAST_AGE.total_seconds()
        )
        if invalid_validity:
            quality["status"] = "invalid"
            quality["invalid"].append("forecasts.validity")
        elif stale:
            quality["status"] = "stale"
            if stored_validity == "expired":
                quality["stale"].append("forecasts.validity")
            else:
                quality["stale"].append("forecasts.age_seconds")
        validity = (
            "invalid"
            if invalid_validity
            else "stale"
            if stale
            else "valid"
            if stored_validity == "stale"
            else stored_validity
        )
        expected_return = sanitize_observation_number(snapshot.get("predicted_return"))
        if expected_return is None:
            quality["invalid"].append("forecasts.predicted_return")
        if not stale:
            quality["status"] = "valid" if expected_return is not None else "partial"
        return (
            {
                "available": not stale,
                "status": (
                    "invalid"
                    if invalid_validity
                    else "stale"
                    if stale
                    else "valid"
                    if expected_return is not None
                    else "partial"
                ),
                "forecast_id": str(snapshot.get("forecast_id") or ""),
                "generated_at": generated_at.isoformat(),
                "age_seconds": age_seconds,
                "expected_return": expected_return,
                "predicted_price": sanitize_observation_number(snapshot.get("predicted_price")),
                "direction": direction,
                "p_up": sanitize_observation_number(snapshot.get("p_up")),
                "p_down": sanitize_observation_number(snapshot.get("p_down")),
                "p_neutral": sanitize_observation_number(snapshot.get("p_neutral")),
                "probability_status": str(
                    snapshot.get("probability_status") or "unavailable"
                ).lower(),
                "lower_return": sanitize_observation_number(snapshot.get("lower_return")),
                "upper_return": sanitize_observation_number(snapshot.get("upper_return")),
                "uncertainty": str(
                    snapshot.get("uncertainty") or snapshot.get("forecast_uncertainty") or "unknown"
                ).lower(),
                "uncertainty_width": sanitize_observation_number(snapshot.get("uncertainty_width")),
                "disagreement": sanitize_observation_number(
                    snapshot.get("disagreement") or snapshot.get("ensemble_disagreement")
                ),
                "horizon": str(snapshot.get("horizon") or snapshot.get("forecast_horizon") or ""),
                "target_at": snapshot.get("target_at") or snapshot.get("forecast_target_timestamp"),
                "validity": validity,
                "model_version": str(snapshot.get("model_version") or ""),
                "feature_set_version": str(snapshot.get("feature_set_version") or "unspecified"),
                "dataset_version": str(snapshot.get("dataset_version") or "unspecified"),
            },
            quality,
        )

    def coverage(
        self,
        *,
        at: datetime,
        feature_snapshot: Mapping[str, Any] | None,
        forecast_snapshot: Mapping[str, Any] | None,
    ) -> dict[str, str]:
        """Bucket one bar's feature and forecast evidence for coverage counts.

        The rules are this builder's own point-in-time rules, so a dataset can
        never report coverage for evidence the environment would drop:

        ``available``  a value the environment can actually read at ``at``
        ``stale``      evidence present but aged past the replay freshness bound
        ``missing``    no snapshot, no usable value, or an undated/post-dated row
        ``invalid``    present but rejected (undated validity, unusable fields)
        ``disabled``   the environment config excludes this source

        A snapshot whose values are all null carries no coverage: key presence
        alone is not evidence.
        """
        features, feature_quality = self._features(feature_snapshot, at)
        forecasts, forecast_quality = self._forecasts(forecast_snapshot, at)
        return {
            "features": _coverage_bucket(
                str(features.get("status") or feature_quality.get("status") or ""),
                feature_quality,
                has_value=any(
                    value is not None for value in features.get("values", {}).values()
                ),
            ),
            "forecasts": _coverage_bucket(
                str(forecasts.get("status") or forecast_quality.get("status") or ""),
                forecast_quality,
                has_value=bool(forecasts.get("available")),
            ),
        }


def _coverage_bucket(
    status: str, quality: Mapping[str, Any], *, has_value: bool
) -> str:
    if status == "disabled":
        return "disabled"
    if has_value:
        return "available"
    if status in {"missing", "future_leak_blocked"}:
        return "missing"
    if status == "invalid" or (status != "stale" and quality.get("invalid")):
        return "invalid"
    if status == "stale" or quality.get("stale"):
        return "stale"
    return "missing"


def _finite(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


def _validate_numbers(payload: Mapping[str, Any], *, path: str) -> None:
    for name, value in payload.items():
        if isinstance(value, Mapping):
            _validate_numbers(value, path=f"{path}.{name}")
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                if isinstance(item, Mapping):
                    _validate_numbers(item, path=f"{path}.{name}[{index}]")
        elif (
            isinstance(value, (int, float, Decimal))
            and not isinstance(value, bool)
            and not _finite(value)
        ):
            raise RLDataError(f"{path}.{name} is NaN or infinite")


def _return(closes: Sequence[Decimal], periods: int) -> float | None:
    if periods <= 0 or len(closes) <= periods:
        return None
    base = closes[-periods - 1]
    if base == 0:
        return None
    return float(closes[-1] / base - Decimal("1"))


def validate_action_shape(action: Mapping[str, Any]) -> None:
    if not isinstance(action, Mapping):
        raise RLValidationError("action must be an object")
    if "decision_id" in action or "order_id" in action or "portfolio_id" in action:
        raise RLValidationError(
            "RL actions cannot carry production decision, order or portfolio identifiers"
        )
