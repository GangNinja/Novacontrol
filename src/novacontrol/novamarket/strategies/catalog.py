"""The NM-09 strategy catalog.

Six strategy families are implemented. Each family is a declarative definition
plus one pure evaluator over a :class:`StrategyContext`; configurations select a
family and parameterise it, they do not create a new code path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

from novacontrol.novamarket.strategies import indicators
from novacontrol.novamarket.strategies.models import (
    SignalType,
    StrategyCategory,
    StrategyDefinition,
)

VERIFIED_EVENT_STATES = frozenset({"officially_confirmed", "multi_source_corroborated"})
NEGATIVE_EVENT_WORDS = (
    "litigation",
    "lawsuit",
    "probe",
    "investigation",
    "downgrade",
    "miss",
    "decline",
    "disruption",
    "fraud",
    "default",
    "recall",
    "regulatory",
)
POSITIVE_EVENT_WORDS = (
    "beat",
    "upgrade",
    "approval",
    "contract",
    "partnership",
    "acquisition",
    "expansion",
    "dividend",
    "buyback",
)


@dataclass(frozen=True, slots=True)
class StrategyContext:
    instrument_id: str
    portfolio_id: str
    horizon: str
    closes: tuple[float, ...]
    highs: tuple[float, ...]
    lows: tuple[float, ...]
    volumes: tuple[float, ...]
    price: Decimal
    observed_at: datetime
    now: datetime
    forecasts: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]
    held: bool
    held_weight: Decimal
    cash: Decimal
    total_value: Decimal


@dataclass(frozen=True, slots=True)
class SignalDecision:
    signal_type: SignalType
    confidence: Decimal
    conditions: tuple[str, ...]
    indicators: dict[str, Any]
    explanation: str
    forecast_refs: tuple[str, ...] = ()
    event_refs: tuple[str, ...] = ()
    horizon_days: int = 5


def _number(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _forecast_rows(context: StrategyContext, horizon: str) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in context.forecasts
        if str(row.get("horizon") or "") == horizon
    ]
    return sorted(rows, key=lambda row: str(row.get("created_at") or ""))


def _event_sentiment(event: dict[str, Any]) -> str:
    text = " ".join(
        str(event.get(key) or "")
        for key in ("event_type", "category", "title", "summary", "description")
    ).lower()
    if any(word in text for word in NEGATIVE_EVENT_WORDS):
        return "negative"
    if any(word in text for word in POSITIVE_EVENT_WORDS):
        return "positive"
    explicit = str(event.get("sentiment") or event.get("direction") or "").lower()
    return explicit if explicit in {"positive", "negative"} else "unknown"


def _verified_events(context: StrategyContext, minimum: str) -> list[dict[str, Any]]:
    rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    threshold = rank.get(str(minimum).lower(), 2)
    rows = [
        dict(event)
        for event in context.events
        if str(event.get("verification") or "") in VERIFIED_EVENT_STATES
        and rank.get(str(event.get("importance") or "").lower(), -1) >= threshold
    ]
    return sorted(
        rows,
        key=lambda event: str(
            event.get("occurred_at")
            or event.get("published_at")
            or event.get("created_at")
            or ""
        ),
    )


def evaluate_trend(context: StrategyContext, config: dict[str, Any]) -> SignalDecision:
    fast_period = int(config["fast_period"])
    slow_period = int(config["slow_period"])
    minimum = slow_period + 5
    if len(context.closes) < minimum:
        return SignalDecision(
            SignalType.NO_SIGNAL,
            Decimal("0"),
            ("insufficient_history",),
            {"closes": len(context.closes), "required": minimum},
            f"{len(context.closes)} closes are available; the trend strategy needs {minimum}.",
        )
    fast_series = indicators.sma_series(context.closes, fast_period)
    slow_series = indicators.sma_series(context.closes, slow_period)
    fast = fast_series[-1]
    slow = slow_series[-1]
    previous_fast = fast_series[-2] if len(fast_series) > 1 else None
    previous_slow = slow_series[-2] if len(slow_series) > 1 else None
    if fast is None or slow is None or previous_fast is None or previous_slow is None:
        return SignalDecision(
            SignalType.NO_SIGNAL,
            Decimal("0"),
            ("insufficient_history",),
            {"fast": _number(fast), "slow": _number(slow)},
            "A complete moving-average pair is not available yet.",
        )
    gap = (fast - slow) / slow if slow else Decimal("0")
    payload = {
        "fast_sma": str(fast),
        "slow_sma": str(slow),
        "gap": str(indicators.q(gap)),
        "close": str(context.price),
    }
    if fast > slow and previous_fast <= previous_slow:
        confidence = min(Decimal("0.9"), Decimal("0.55") + abs(gap) * 4)
        return SignalDecision(
            SignalType.BUY,
            indicators.q(confidence, "0.01"),
            ("fast_cross_above_slow",),
            payload,
            f"SMA{fast_period} crossed above SMA{slow_period} (gap {indicators.q(gap)}).",
            horizon_days=int(config.get("horizon_days", 5)),
        )
    if fast < slow and previous_fast >= previous_slow:
        confidence = min(Decimal("0.9"), Decimal("0.55") + abs(gap) * 4)
        return SignalDecision(
            SignalType.EXIT if context.held else SignalType.NO_SIGNAL,
            indicators.q(confidence, "0.01"),
            ("fast_cross_below_slow",),
            payload,
            f"SMA{fast_period} crossed below SMA{slow_period} (gap {indicators.q(gap)}).",
            horizon_days=int(config.get("horizon_days", 5)),
        )
    if fast > slow:
        return SignalDecision(
            SignalType.HOLD,
            Decimal("0.50"),
            ("trend_positive",),
            payload,
            "The fast average remains above the slow average; no fresh cross.",
        )
    return SignalDecision(
        SignalType.HOLD if context.held else SignalType.NO_SIGNAL,
        Decimal("0.45"),
        ("trend_negative",),
        payload,
        "The fast average remains below the slow average; no fresh cross.",
    )


def evaluate_mean_reversion(
    context: StrategyContext, config: dict[str, Any]
) -> SignalDecision:
    rsi_period = int(config["rsi_period"])
    bb_period = int(config["bb_period"])
    minimum = max(rsi_period + 1, bb_period)
    if len(context.closes) < minimum:
        return SignalDecision(
            SignalType.NO_SIGNAL,
            Decimal("0"),
            ("insufficient_history",),
            {"closes": len(context.closes), "required": minimum},
            f"{len(context.closes)} closes are available; mean reversion needs {minimum}.",
        )
    value = indicators.rsi(context.closes, rsi_period)
    bands = indicators.bollinger(context.closes, bb_period, float(config["bb_k"]))
    if value is None or bands is None:
        return SignalDecision(
            SignalType.NO_SIGNAL,
            Decimal("0"),
            ("indicator_unavailable",),
            {},
            "RSI or Bollinger Bands are not available.",
        )
    middle, upper, lower = bands
    close = Decimal(str(context.price))
    indicators_payload = {
        "rsi": str(value),
        "middle": str(middle),
        "upper": str(upper),
        "lower": str(lower),
        "close": str(close),
    }
    if value <= Decimal(str(config["oversold"])) and close <= lower:
        return SignalDecision(
            SignalType.BUY,
            Decimal("0.70"),
            ("rsi_oversold", "close_below_lower_band"),
            indicators_payload,
            f"RSI {value} is oversold and the close is below the lower band {lower}.",
            horizon_days=int(config.get("horizon_days", 3)),
        )
    if value >= Decimal(str(config["overbought"])) and close >= upper and context.held:
        return SignalDecision(
            SignalType.SELL,
            Decimal("0.65"),
            ("rsi_overbought", "close_above_upper_band"),
            indicators_payload,
            f"RSI {value} is overbought and the close is above the upper band {upper}.",
            horizon_days=int(config.get("horizon_days", 3)),
        )
    return SignalDecision(
        SignalType.HOLD if context.held else SignalType.NO_SIGNAL,
        Decimal("0.40"),
        ("no_reversion_extreme",),
        indicators_payload,
        "Neither the oversold entry nor the overbought exit condition is met.",
    )


def evaluate_momentum(context: StrategyContext, config: dict[str, Any]) -> SignalDecision:
    lookback = int(config["lookback"])
    exit_period = int(config["exit_period"])
    minimum = max(lookback + 1, exit_period + 1, 21)
    if len(context.closes) < minimum:
        return SignalDecision(
            SignalType.NO_SIGNAL,
            Decimal("0"),
            ("insufficient_history",),
            {"closes": len(context.closes), "required": minimum},
            f"{len(context.closes)} closes are available; momentum needs {minimum}.",
        )
    previous_high = indicators.highest(context.closes[:-1], lookback)
    volume_average = indicators.average_volume(context.volumes, 20)
    exit_average = indicators.sma(context.closes, exit_period)
    if previous_high is None or volume_average is None or exit_average is None:
        return SignalDecision(
            SignalType.NO_SIGNAL,
            Decimal("0"),
            ("indicator_unavailable",),
            {},
            "The breakout high, volume average or exit average is unavailable.",
        )
    volume = Decimal(str(context.volumes[-1])) if context.volumes else Decimal("0")
    volume_ratio = volume / volume_average if volume_average else Decimal("0")
    indicators_payload = {
        "previous_high": str(previous_high),
        "volume_ratio": str(indicators.q(volume_ratio)),
        "exit_sma": str(exit_average),
        "close": str(context.price),
    }
    if context.price > previous_high and volume_ratio >= Decimal(str(config["volume_factor"])):
        return SignalDecision(
            SignalType.BUY,
            Decimal("0.72"),
            ("breakout_above_range", "volume_confirmed"),
            indicators_payload,
            f"Close broke the {lookback}-bar high {previous_high} on {indicators.q(volume_ratio)}x volume.",
            horizon_days=int(config.get("horizon_days", 5)),
        )
    if context.held and context.price < exit_average:
        return SignalDecision(
            SignalType.EXIT,
            Decimal("0.65"),
            ("close_below_exit_average",),
            indicators_payload,
            f"Close {context.price} fell below the exit average {exit_average}.",
            horizon_days=int(config.get("horizon_days", 5)),
        )
    return SignalDecision(
        SignalType.HOLD if context.held else SignalType.NO_SIGNAL,
        Decimal("0.40"),
        ("range_intact",),
        indicators_payload,
        "The breakout condition is not met and the exit average is intact.",
    )


def evaluate_volatility(
    context: StrategyContext, config: dict[str, Any]
) -> SignalDecision:
    lookback = int(config["lookback"])
    bb_period = int(config["bb_period"])
    minimum = max(bb_period, lookback + 1, 20)
    if len(context.closes) < minimum:
        return SignalDecision(
            SignalType.NO_SIGNAL,
            Decimal("0"),
            ("insufficient_history",),
            {"closes": len(context.closes), "required": minimum},
            f"{len(context.closes)} closes are available; volatility needs {minimum}.",
        )
    bands = [indicators.bollinger(context.closes[: index + 1], bb_period) for index in range(len(context.closes))]
    recent = [item for item in bands[-lookback:] if item is not None]
    if len(recent) < lookback:
        return SignalDecision(
            SignalType.NO_SIGNAL,
            Decimal("0"),
            ("indicator_unavailable",),
            {},
            "A complete Bollinger-width history is not available.",
        )
    width = Decimal(str(recent[-1][1] - recent[-1][2]))
    average_width = sum(
        (Decimal(str(item[1] - item[2])) for item in recent[:-1]), Decimal("0")
    ) / Decimal(str(max(1, len(recent) - 1)))
    contraction = width / average_width if average_width else Decimal("0")
    previous_high = indicators.highest(context.closes[:-1], lookback)
    indicators_payload = {
        "band_width": str(indicators.q(width)),
        "average_width": str(indicators.q(average_width)),
        "contraction": str(indicators.q(contraction)),
        "previous_high": str(previous_high) if previous_high is not None else "",
        "close": str(context.price),
    }
    if previous_high is not None and contraction <= Decimal(str(config["contraction"])):
        if context.price > previous_high:
            return SignalDecision(
                SignalType.BUY,
                Decimal("0.68"),
                ("volatility_contraction", "breakout_above_range"),
                indicators_payload,
                f"Width contracted to {indicators.q(contraction)} of its average and the close broke {previous_high}.",
                horizon_days=int(config.get("horizon_days", 7)),
            )
        return SignalDecision(
            SignalType.HOLD,
            Decimal("0.45"),
            ("volatility_contraction",),
            indicators_payload,
            "Volatility has contracted but no upside breakout has occurred.",
        )
    if context.held and context.price < recent[-1][0]:
        return SignalDecision(
            SignalType.EXIT,
            Decimal("0.60"),
            ("close_below_middle_band",),
            indicators_payload,
            f"Close {context.price} fell below the middle band {recent[-1][0]}.",
            horizon_days=int(config.get("horizon_days", 7)),
        )
    return SignalDecision(
        SignalType.HOLD if context.held else SignalType.NO_SIGNAL,
        Decimal("0.35"),
        ("no_volatility_setup",),
        indicators_payload,
        "No contraction breakout or middle-band exit condition is met.",
    )


def evaluate_event(context: StrategyContext, config: dict[str, Any]) -> SignalDecision:
    events = _verified_events(context, str(config["min_importance"]))
    if not events:
        return SignalDecision(
            SignalType.NO_SIGNAL,
            Decimal("0"),
            ("no_verified_high_importance_event",),
            {"verified_events": 0},
            "No confirmed high- or critical-importance event is available.",
        )
    trend = indicators.sma(context.closes, int(config["trend_period"]))
    if trend is None:
        return SignalDecision(
            SignalType.NO_SIGNAL,
            Decimal("0"),
            ("insufficient_history",),
            {},
            "A trend average is required to confirm an event signal.",
        )
    selected = events[-1]
    sentiment = _event_sentiment(selected)
    event_ref = str(selected.get("event_id") or selected.get("id") or "")
    indicators_payload = {
        "event_verification": str(selected.get("verification")),
        "event_sentiment": sentiment,
        "trend_sma": str(trend),
        "close": str(context.price),
    }
    if sentiment == "positive" and context.price > trend:
        return SignalDecision(
            SignalType.BUY,
            Decimal("0.66"),
            ("verified_positive_event", "close_above_trend"),
            indicators_payload,
            "A confirmed positive event aligns with a price above its trend average.",
            event_refs=(event_ref,) if event_ref else (),
            horizon_days=int(config.get("horizon_days", 10)),
        )
    if sentiment == "negative" and context.held:
        return SignalDecision(
            SignalType.EXIT,
            Decimal("0.75"),
            ("verified_negative_event",),
            indicators_payload,
            "A confirmed negative event affects a held position; exit is proposed for risk review.",
            event_refs=(event_ref,) if event_ref else (),
            horizon_days=int(config.get("horizon_days", 10)),
        )
    return SignalDecision(
        SignalType.HOLD if context.held else SignalType.NO_SIGNAL,
        Decimal("0.35"),
        ("event_not_actionable",),
        indicators_payload,
        "A verified event is present but its direction or confirmation does not pass the entry rule.",
    )


def evaluate_forecast(
    context: StrategyContext, config: dict[str, Any]
) -> SignalDecision:
    rows = _forecast_rows(context, str(config["forecast_horizon"]))
    if not rows:
        return SignalDecision(
            SignalType.NO_SIGNAL,
            Decimal("0"),
            ("forecast_missing",),
            {},
            f"No {config['forecast_horizon']} forecast is available for {context.instrument_id}.",
        )
    latest = rows[-1]
    try:
        created = datetime.fromisoformat(str(latest.get("created_at")))
        age_hours = (context.now - created).total_seconds() / 3600.0
    except (TypeError, ValueError):
        age_hours = float("inf")
    state = str(latest.get("model_state") or "")
    uncertainty = str(latest.get("uncertainty") or "unknown")
    probability = latest.get("probability_up")
    predicted = latest.get("predicted_return")
    indicators_payload = {
        "predicted_return": str(predicted) if predicted is not None else "",
        "probability_up": str(probability) if probability is not None else "",
        "uncertainty": uncertainty,
        "model_state": state,
        "age_hours": age_hours,
    }
    if age_hours > float(config["max_age_hours"]):
        return SignalDecision(
            SignalType.NO_SIGNAL,
            Decimal("0"),
            ("forecast_stale",),
            indicators_payload,
            f"The newest forecast is {age_hours:.1f}h old; the limit is {config['max_age_hours']}h.",
        )
    if state not in set(config["allowed_model_states"]):
        return SignalDecision(
            SignalType.NO_SIGNAL,
            Decimal("0"),
            ("model_not_validated",),
            indicators_payload,
            f"The forecast model state {state!r} is not accepted by this strategy.",
        )
    allowed = {"low": 0, "medium": 1, "high": 2}
    if allowed.get(uncertainty, 9) > allowed[str(config["max_uncertainty"])]:
        return SignalDecision(
            SignalType.NO_SIGNAL,
            Decimal("0"),
            ("forecast_uncertainty_too_high",),
            indicators_payload,
            f"Forecast uncertainty {uncertainty} exceeds {config['max_uncertainty']}.",
        )
    if predicted is None or probability is None:
        return SignalDecision(
            SignalType.NO_SIGNAL,
            Decimal("0"),
            ("forecast_inputs_missing",),
            indicators_payload,
            "The forecast does not carry both a predicted return and a directional probability.",
        )
    predicted_value = Decimal(str(predicted))
    probability_value = Decimal(str(probability))
    trend = indicators.sma(context.closes, int(config["trend_period"]))
    if trend is None:
        return SignalDecision(
            SignalType.NO_SIGNAL,
            Decimal("0"),
            ("insufficient_history",),
            indicators_payload,
            "A trend average is required to confirm a forecast signal.",
        )
    indicators_payload["trend_sma"] = str(trend)
    threshold = Decimal(str(config["min_return"]))
    probability_threshold = Decimal(str(config["min_probability"]))
    forecast_ref = str(latest.get("forecast_id") or "")
    if (
        predicted_value >= threshold
        and probability_value >= probability_threshold
        and context.price > trend
    ):
        return SignalDecision(
            SignalType.BUY,
            Decimal("0.60"),
            ("positive_predicted_return", "probability_threshold", "close_above_trend"),
            indicators_payload,
            f"Forecast return {predicted_value} with probability {probability_value} passes the entry rule.",
            forecast_refs=(forecast_ref,) if forecast_ref else (),
            horizon_days=int(config.get("horizon_days", 5)),
        )
    if predicted_value <= -threshold and probability_value <= (Decimal("1") - probability_threshold) and context.held:
        return SignalDecision(
            SignalType.EXIT,
            Decimal("0.60"),
            ("negative_predicted_return", "probability_threshold"),
            indicators_payload,
            f"Forecast return {predicted_value} with probability {probability_value} is negative for a held position.",
            forecast_refs=(forecast_ref,) if forecast_ref else (),
            horizon_days=int(config.get("horizon_days", 5)),
        )
    return SignalDecision(
        SignalType.HOLD if context.held else SignalType.NO_SIGNAL,
        Decimal("0.40"),
        ("forecast_below_threshold",),
        indicators_payload,
        "The forecast does not pass the configured directional and probability thresholds.",
    )


EVALUATORS: dict[str, Callable[[StrategyContext, dict[str, Any]], SignalDecision]] = {
    "trend_sma": evaluate_trend,
    "mean_reversion_rsi": evaluate_mean_reversion,
    "momentum_breakout": evaluate_momentum,
    "volatility_breakout": evaluate_volatility,
    "event_driven": evaluate_event,
    "forecast_assisted": evaluate_forecast,
}


def _check_int(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not low <= value <= high:
        raise ValueError(f"{name} must be within {low}..{high}")
    return value


def _check_number(value: Any, name: str, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not low <= number <= high:
        raise ValueError(f"{name} must be within {low}..{high}")
    return number


def validate_config(catalog_id: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge overrides over the family defaults and validate every parameter."""
    definition = STRATEGIES.get(catalog_id)
    if definition is None:
        raise ValueError(f"unknown strategy catalog entry {catalog_id!r}")
    supplied = dict(overrides or {})
    unknown = sorted(set(supplied) - set(definition.default_config))
    if unknown:
        raise ValueError(f"unknown strategy parameter(s): {', '.join(unknown)}")
    config = dict(definition.default_config)
    config.update(supplied)
    if catalog_id == "trend_sma":
        config["fast_period"] = _check_int(config["fast_period"], "fast_period", 2, 200)
        config["slow_period"] = _check_int(config["slow_period"], "slow_period", 3, 400)
        if config["fast_period"] >= config["slow_period"]:
            raise ValueError("fast_period must be shorter than slow_period")
    elif catalog_id == "mean_reversion_rsi":
        config["rsi_period"] = _check_int(config["rsi_period"], "rsi_period", 2, 100)
        config["bb_period"] = _check_int(config["bb_period"], "bb_period", 2, 200)
        config["bb_k"] = _check_number(config["bb_k"], "bb_k", 0.5, 4.0)
        config["oversold"] = _check_number(config["oversold"], "oversold", 1, 49)
        config["overbought"] = _check_number(config["overbought"], "overbought", 51, 99)
    elif catalog_id == "momentum_breakout":
        config["lookback"] = _check_int(config["lookback"], "lookback", 2, 250)
        config["exit_period"] = _check_int(config["exit_period"], "exit_period", 2, 250)
        config["volume_factor"] = _check_number(config["volume_factor"], "volume_factor", 0.1, 10.0)
    elif catalog_id == "volatility_breakout":
        config["bb_period"] = _check_int(config["bb_period"], "bb_period", 5, 200)
        config["lookback"] = _check_int(config["lookback"], "lookback", 5, 250)
        config["contraction"] = _check_number(config["contraction"], "contraction", 0.1, 1.5)
    elif catalog_id == "event_driven":
        config["trend_period"] = _check_int(config["trend_period"], "trend_period", 2, 250)
        if str(config["min_importance"]).lower() not in {"high", "critical"}:
            raise ValueError("min_importance must be high or critical")
    elif catalog_id == "forecast_assisted":
        config["trend_period"] = _check_int(config["trend_period"], "trend_period", 2, 250)
        config["min_return"] = _check_number(config["min_return"], "min_return", 0.0, 1.0)
        config["min_probability"] = _check_number(config["min_probability"], "min_probability", 0.5, 1.0)
        if str(config["max_uncertainty"]) not in {"low", "medium", "high"}:
            raise ValueError("max_uncertainty must be low, medium or high")
        if str(config["forecast_horizon"]) not in {
            "next_session", "few_sessions", "one_week", "one_month", "three_months"
        }:
            raise ValueError("forecast_horizon is not an NM-06 horizon")
        states = config.get("allowed_model_states")
        if not isinstance(states, list) or not states:
            raise ValueError("allowed_model_states must be a non-empty list")
        if not set(str(item) for item in states) <= {"active", "validated", "degraded"}:
            raise ValueError("allowed_model_states contains an unknown state")
    config["horizon_days"] = _check_int(config["horizon_days"], "horizon_days", 1, 120)
    config["max_data_age_hours"] = _check_number(
        config["max_data_age_hours"], "max_data_age_hours", 0.1, 720.0
    )
    return config


STRATEGIES: dict[str, StrategyDefinition] = {
    "trend_sma": StrategyDefinition(
        catalog_id="trend_sma",
        name="SMA trend crossover",
        category=StrategyCategory.TREND,
        description="Fast/slow simple moving-average crossover with trend confirmation.",
        asset_classes=("equity",),
        horizons=("short_term", "medium_term", "long_term"),
        entry_rules=("fast SMA crosses above slow SMA",),
        exit_rules=("fast SMA crosses below slow SMA",),
        position_rules=("no short selling; BUY adds exposure through NM-08",),
        required_data=("bars", "latest price"),
        required_forecasts=(),
        risk_requirements=("NM-08 assessment before any proposal",),
        default_config={
            "fast_period": 10,
            "slow_period": 30,
            "horizon_days": 5,
            "max_data_age_hours": 72,
        },
    ),
    "mean_reversion_rsi": StrategyDefinition(
        catalog_id="mean_reversion_rsi",
        name="RSI/Bollinger mean reversion",
        category=StrategyCategory.MEAN_REVERSION,
        description="Buy oversold closes below the lower band; sell overbought held positions.",
        asset_classes=("equity",),
        horizons=("short_term", "medium_term"),
        entry_rules=("RSI at or below oversold and close below the lower band",),
        exit_rules=("RSI at or above overbought and close above the upper band",),
        position_rules=("SELL is a partial reduction of a held position; shorts are unsupported",),
        required_data=("bars", "latest price"),
        required_forecasts=(),
        risk_requirements=("NM-08 assessment before any proposal",),
        default_config={
            "rsi_period": 14,
            "bb_period": 20,
            "bb_k": 2.0,
            "oversold": 30,
            "overbought": 70,
            "horizon_days": 3,
            "max_data_age_hours": 72,
        },
    ),
    "momentum_breakout": StrategyDefinition(
        catalog_id="momentum_breakout",
        name="Volume-confirmed momentum breakout",
        category=StrategyCategory.MOMENTUM,
        description="Buy a close above the prior range on above-average volume; exit below the exit average.",
        asset_classes=("equity",),
        horizons=("short_term", "medium_term"),
        entry_rules=("close above prior N-bar high", "volume at least the configured multiple"),
        exit_rules=("close below the exit SMA",),
        position_rules=("no short selling; EXIT only for an existing holding",),
        required_data=("bars with volume", "latest price"),
        required_forecasts=(),
        risk_requirements=("NM-08 assessment before any proposal",),
        default_config={
            "lookback": 20,
            "volume_factor": 1.2,
            "exit_period": 10,
            "horizon_days": 5,
            "max_data_age_hours": 72,
        },
    ),
    "volatility_breakout": StrategyDefinition(
        catalog_id="volatility_breakout",
        name="Volatility contraction breakout",
        category=StrategyCategory.VOLATILITY,
        description="Buy an upside range break after Bollinger width contracts; exit below the middle band.",
        asset_classes=("equity",),
        horizons=("short_term", "medium_term"),
        entry_rules=("band width at most the contraction fraction of its average", "close above prior high"),
        exit_rules=("close below the middle band",),
        position_rules=("no short selling; EXIT only for an existing holding",),
        required_data=("bars", "latest price"),
        required_forecasts=(),
        risk_requirements=("NM-08 assessment before any proposal",),
        default_config={
            "bb_period": 20,
            "lookback": 20,
            "contraction": 0.8,
            "horizon_days": 7,
            "max_data_age_hours": 72,
        },
    ),
    "event_driven": StrategyDefinition(
        catalog_id="event_driven",
        name="Verified event reaction",
        category=StrategyCategory.EVENT,
        description="Act only on confirmed high-importance NM-04 events, with price-trend confirmation.",
        asset_classes=("equity",),
        horizons=("short_term", "medium_term"),
        entry_rules=("confirmed positive high-importance event", "close above trend average"),
        exit_rules=("confirmed negative high-importance event on a held position",),
        position_rules=("rumours never generate a signal; SELL/EXIT only for holdings",),
        required_data=("bars", "latest price", "NM-04 events"),
        required_forecasts=(),
        risk_requirements=("NM-08 assessment before any proposal",),
        default_config={
            "trend_period": 20,
            "min_importance": "high",
            "horizon_days": 10,
            "max_data_age_hours": 72,
        },
    ),
    "forecast_assisted": StrategyDefinition(
        catalog_id="forecast_assisted",
        name="Forecast-confirmed trend",
        category=StrategyCategory.FORECAST,
        description="Combine NM-06 predicted return, directional probability and model reliability with a trend filter.",
        asset_classes=("equity",),
        horizons=("short_term", "medium_term", "long_term"),
        entry_rules=("predicted return above threshold", "directional probability above threshold", "close above trend average"),
        exit_rules=("negative predicted return with low probability on a held position",),
        position_rules=("forecast is one input, never an unconditional instruction",),
        required_data=("bars", "latest price", "NM-06 forecast"),
        required_forecasts=("predicted_return", "probability_up", "uncertainty", "model state"),
        risk_requirements=("NM-08 assessment before any proposal",),
        default_config={
            "trend_period": 20,
            "min_return": 0.01,
            "min_probability": 0.55,
            "max_uncertainty": "medium",
            "forecast_horizon": "few_sessions",
            "max_age_hours": 12,
            "allowed_model_states": ["active", "validated"],
            "horizon_days": 5,
            "max_data_age_hours": 72,
        },
    ),
}
