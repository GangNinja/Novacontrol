"""The PI-04 feature registry: names, families, formulas and profiles.

Every value the engine can emit has exactly one definition here.  A change to a
formula or window changes this versioned definition rather than silently
reinterpreting historical rows.
"""

from __future__ import annotations

from novacontrol.novamarket.features.models import (
    FEATURE_SET_VERSION,
    FeatureFamily,
    FeatureProfileName,
    FeatureSpec,
)

ALL: tuple[FeatureProfileName, ...] = (
    FeatureProfileName.INTRADAY,
    FeatureProfileName.SWING,
    FeatureProfileName.LONG_TERM,
)
INTRADAY: tuple[FeatureProfileName, ...] = (FeatureProfileName.INTRADAY,)
SWING: tuple[FeatureProfileName, ...] = (
    FeatureProfileName.SWING,
    FeatureProfileName.LONG_TERM,
)
LONG: tuple[FeatureProfileName, ...] = (FeatureProfileName.LONG_TERM,)


def _spec(
    name: str,
    family: FeatureFamily,
    description: str,
    formula: str,
    lookback: str,
    profiles: tuple[FeatureProfileName, ...] = ALL,
    *,
    provisional: bool = False,
) -> FeatureSpec:
    return FeatureSpec(
        name=name,
        family=family,
        description=description,
        formula=formula,
        version=FEATURE_SET_VERSION,
        lookback=lookback,
        profiles=profiles,
        provisional=provisional,
        source=(
            "fundamentals"
            if family is FeatureFamily.FUNDAMENTAL
            else "news"
            if family is FeatureFamily.NEWS_EVENT
            else "marketdata"
        ),
    )


_SPECS: tuple[FeatureSpec, ...] = (
    # Price -----------------------------------------------------------------
    _spec("close", FeatureFamily.PRICE, "Latest completed close.", "close_t", "1 bar"),
    _spec("price_abs_change_1", FeatureFamily.PRICE, "Close change since previous bar.", "close_t - close_t-1", "2 bars"),
    _spec("return_1", FeatureFamily.PRICE, "One-period simple return.", "close_t / close_t-1 - 1", "2 bars"),
    _spec("log_return_1", FeatureFamily.PRICE, "One-period logarithmic return.", "ln(close_t / close_t-1)", "2 bars"),
    _spec("open_close_return", FeatureFamily.PRICE, "Current bar open-to-close return.", "close_t / open_t - 1", "1 bar"),
    _spec("high_low_range_pct", FeatureFamily.PRICE, "Current high-low range relative to close.", "(high_t - low_t) / close_t", "1 bar"),
    _spec("return_prev_1", FeatureFamily.PRICE, "Return of the bar before the latest bar.", "close_t-1 / close_t-2 - 1", "3 bars"),
    _spec("return_5", FeatureFamily.PRICE, "Five-bar simple return.", "close_t / close_t-5 - 1", "6 bars"),
    _spec("return_10", FeatureFamily.PRICE, "Ten-bar simple return.", "close_t / close_t-10 - 1", "11 bars"),
    _spec("return_20", FeatureFamily.PRICE, "Twenty-bar simple return.", "close_t / close_t-20 - 1", "21 bars"),
    _spec("price_gap", FeatureFamily.PRICE, "Gap from previous close to current open.", "open_t / close_t-1 - 1", "2 bars"),
    _spec("candle_body_pct", FeatureFamily.PRICE, "Absolute candle body relative to close.", "abs(close_t - open_t) / close_t", "1 bar"),
    _spec("upper_wick_pct", FeatureFamily.PRICE, "Upper wick relative to close.", "(high_t - max(open_t, close_t)) / close_t", "1 bar"),
    _spec("lower_wick_pct", FeatureFamily.PRICE, "Lower wick relative to close.", "(min(open_t, close_t) - low_t) / close_t", "1 bar"),
    _spec("consecutive_up", FeatureFamily.PRICE, "Consecutive strictly rising closes.", "count from latest close backwards", "unbounded"),
    _spec("consecutive_down", FeatureFamily.PRICE, "Consecutive strictly falling closes.", "count from latest close backwards", "unbounded"),
    _spec("rolling_high_20", FeatureFamily.PRICE, "Highest high in the last 20 completed bars.", "max(high, 20)", "20 bars", SWING),
    _spec("rolling_low_20", FeatureFamily.PRICE, "Lowest low in the last 20 completed bars.", "min(low, 20)", "20 bars", SWING),
    _spec("distance_recent_high_20", FeatureFamily.PRICE, "Close distance below the 20-bar high.", "close_t / max(high, 20) - 1", "20 bars", SWING),
    _spec("distance_recent_low_20", FeatureFamily.PRICE, "Close distance above the 20-bar low.", "close_t / min(low, 20) - 1", "20 bars", SWING),
    # Trend -----------------------------------------------------------------
    _spec("sma_5", FeatureFamily.TREND, "Simple moving average, 5 bars.", "mean(close, 5)", "5 bars"),
    _spec("sma_10", FeatureFamily.TREND, "Simple moving average, 10 bars.", "mean(close, 10)", "10 bars"),
    _spec("sma_20", FeatureFamily.TREND, "Simple moving average, 20 bars.", "mean(close, 20)", "20 bars"),
    _spec("sma_50", FeatureFamily.TREND, "Simple moving average, 50 bars.", "mean(close, 50)", "50 bars", SWING),
    _spec("sma_200", FeatureFamily.TREND, "Simple moving average, 200 bars.", "mean(close, 200)", "200 bars", LONG),
    _spec("ema_5", FeatureFamily.TREND, "Exponential moving average, 5 bars.", "EMA(close, 5), SMA seed", "5 bars"),
    _spec("ema_12", FeatureFamily.TREND, "Exponential moving average, 12 bars.", "EMA(close, 12), SMA seed", "12 bars"),
    _spec("ema_26", FeatureFamily.TREND, "Exponential moving average, 26 bars.", "EMA(close, 26), SMA seed", "26 bars"),
    _spec("ema_50", FeatureFamily.TREND, "Exponential moving average, 50 bars.", "EMA(close, 50), SMA seed", "50 bars", SWING),
    _spec("wma_10", FeatureFamily.TREND, "Linearly weighted moving average, 10 bars.", "weighted mean(close, 10)", "10 bars"),
    _spec("wma_20", FeatureFamily.TREND, "Linearly weighted moving average, 20 bars.", "weighted mean(close, 20)", "20 bars"),
    _spec("macd", FeatureFamily.TREND, "MACD line.", "EMA(close, 12) - EMA(close, 26)", "34 bars"),
    _spec("macd_signal", FeatureFamily.TREND, "MACD signal line.", "EMA(MACD, 9)", "42 bars"),
    _spec("macd_histogram", FeatureFamily.TREND, "MACD histogram.", "MACD - signal", "42 bars"),
    _spec("adx_14", FeatureFamily.TREND, "Average Directional Index, Wilder.", "Wilder ADX(14)", "29 bars", SWING),
    _spec("plus_di_14", FeatureFamily.TREND, "Positive directional indicator.", "100 * smoothed(+DM) / smoothed(TR)", "15 bars", SWING),
    _spec("minus_di_14", FeatureFamily.TREND, "Negative directional indicator.", "100 * smoothed(-DM) / smoothed(TR)", "15 bars", SWING),
    _spec("directional_dx_14", FeatureFamily.TREND, "Latest directional index value.", "100 * abs(+DI - -DI) / (+DI + -DI)", "15 bars", SWING),
    _spec("distance_sma_20", FeatureFamily.TREND, "Close distance from the 20-bar SMA.", "close_t / SMA(20) - 1", "20 bars"),
    _spec("distance_ema_50", FeatureFamily.TREND, "Close distance from the 50-bar EMA.", "close_t / EMA(50) - 1", "50 bars", SWING),
    _spec("sma_cross_20_50", FeatureFamily.TREND, "SMA(20) minus SMA(50), normalized by close.", "(SMA20 - SMA50) / close_t", "50 bars", SWING),
    _spec("sma_slope_20", FeatureFamily.TREND, "Ten-bar change in SMA(20), normalized.", "(SMA20_t - SMA20_t-10) / close_t", "30 bars", SWING),
    # Momentum --------------------------------------------------------------
    _spec("rsi_14", FeatureFamily.MOMENTUM, "Wilder Relative Strength Index, 14.", "RSI Wilder(14)", "15 bars"),
    _spec("stoch_k_14", FeatureFamily.MOMENTUM, "Stochastic %K, 14.", "100 * (close - low14) / (high14 - low14)", "14 bars"),
    _spec("stoch_d_3", FeatureFamily.MOMENTUM, "Stochastic %D, 3-bar SMA of %K.", "SMA(%K, 3)", "16 bars"),
    _spec("roc_10", FeatureFamily.MOMENTUM, "Rate of change, 10 bars, percent.", "(close_t / close_t-10 - 1) * 100", "11 bars"),
    _spec("momentum_10", FeatureFamily.MOMENTUM, "Price momentum, 10 bars.", "close_t - close_t-10", "11 bars"),
    _spec("williams_r_14", FeatureFamily.MOMENTUM, "Williams %R, 14.", "-100 * (high14 - close) / (high14 - low14)", "14 bars"),
    _spec("macd_normalized", FeatureFamily.MOMENTUM, "MACD line normalized by close.", "MACD / close_t", "34 bars"),
    # Volatility ------------------------------------------------------------
    _spec("atr_14", FeatureFamily.VOLATILITY, "Wilder Average True Range, 14.", "Wilder ATR(14)", "15 bars"),
    _spec("atr_pct", FeatureFamily.VOLATILITY, "ATR relative to close.", "ATR14 / close_t", "15 bars"),
    _spec("atr_normalized_move_1", FeatureFamily.VOLATILITY, "Latest close change in ATR units.", "(close_t - close_t-1) / ATR14", "16 bars"),
    _spec("bollinger_mid_20", FeatureFamily.VOLATILITY, "Bollinger middle band.", "SMA(close, 20)", "20 bars"),
    _spec("bollinger_upper_20", FeatureFamily.VOLATILITY, "Bollinger upper band, 2 sigma.", "SMA20 + 2 * population_stdev20", "20 bars"),
    _spec("bollinger_lower_20", FeatureFamily.VOLATILITY, "Bollinger lower band, 2 sigma.", "SMA20 - 2 * population_stdev20", "20 bars"),
    _spec("bollinger_width_pct", FeatureFamily.VOLATILITY, "Band width relative to middle.", "(upper - lower) / middle", "20 bars"),
    _spec("bollinger_position", FeatureFamily.VOLATILITY, "Close position inside the bands.", "(close - lower) / (upper - lower)", "20 bars"),
    _spec("rolling_std_20", FeatureFamily.VOLATILITY, "Population standard deviation of close.", "stdev(close, 20)", "20 bars"),
    _spec("historical_volatility_20", FeatureFamily.VOLATILITY, "Annualized log-return volatility.", "stdev(log returns, 20) * sqrt(252)", "21 bars"),
    _spec("volatility_change_5_20", FeatureFamily.VOLATILITY, "Short versus long historical volatility.", "hist_vol_5 - hist_vol_20", "21 bars"),
    _spec("range_expansion_5_20", FeatureFamily.VOLATILITY, "Mean range 5 bars over mean range 20 bars.", "mean(range,5) / mean(range,20)", "20 bars"),
    # Volume ----------------------------------------------------------------
    _spec("volume_sma_20", FeatureFamily.VOLUME, "Average volume, 20 bars.", "mean(volume, 20)", "20 bars"),
    _spec("relative_volume_20", FeatureFamily.VOLUME, "Current volume over average volume.", "volume_t / mean(volume, 20)", "20 bars"),
    _spec("volume_roc_5", FeatureFamily.VOLUME, "Five-bar volume rate of change.", "volume_t / volume_t-5 - 1", "6 bars"),
    _spec("obv", FeatureFamily.VOLUME, "On-balance volume over the available window.", "cumulative signed volume", "2 bars"),
    _spec("obv_slope_10", FeatureFamily.VOLUME, "Ten-bar OBV change normalized by average volume.", "(OBV_t - OBV_t-10) / (mean(volume,20) * 10)", "21 bars"),
    _spec("vpt", FeatureFamily.VOLUME, "Volume price trend over the available window.", "cumulative volume * return", "2 bars"),
    _spec("vpt_slope_10", FeatureFamily.VOLUME, "Ten-bar VPT change normalized by average volume.", "(VPT_t - VPT_t-10) / (mean(volume,20) * 10)", "21 bars"),
    _spec("mfi_14", FeatureFamily.VOLUME, "Money Flow Index, 14.", "100 - 100 / (1 + positive_flow / negative_flow)", "15 bars"),
    _spec("vwap", FeatureFamily.VOLUME, "Session VWAP of typical price.", "sum(typical * volume) / sum(volume)", "intraday session", INTRADAY),
    _spec("distance_vwap", FeatureFamily.VOLUME, "Close distance from VWAP.", "close_t / VWAP - 1", "intraday session", INTRADAY),
    # Structure -------------------------------------------------------------
    _spec("swing_high_20", FeatureFamily.STRUCTURE, "Most recent confirmed 20-bar swing high.", "latest confirmed pivot high in the prior 20 bars", "20 bars", SWING),
    _spec("swing_low_20", FeatureFamily.STRUCTURE, "Most recent confirmed 20-bar swing low.", "latest confirmed pivot low in the prior 20 bars", "20 bars", SWING),
    _spec("higher_high", FeatureFamily.STRUCTURE, "Latest swing high exceeds the prior swing high.", "swing_high_t > swing_high_t-1", "40 bars", SWING),
    _spec("lower_high", FeatureFamily.STRUCTURE, "Latest swing high is below the prior swing high.", "swing_high_t < swing_high_t-1", "40 bars", SWING),
    _spec("higher_low", FeatureFamily.STRUCTURE, "Latest swing low exceeds the prior swing low.", "swing_low_t > swing_low_t-1", "40 bars", SWING),
    _spec("lower_low", FeatureFamily.STRUCTURE, "Latest swing low is below the prior swing low.", "swing_low_t < swing_low_t-1", "40 bars", SWING),
    _spec("trend_continuation", FeatureFamily.STRUCTURE, "Higher highs and higher lows, confirmed bars.", "higher_high and higher_low", "40 bars", SWING),
    _spec("trend_breakdown", FeatureFamily.STRUCTURE, "Lower highs and lower lows, confirmed bars.", "lower_high and lower_low", "40 bars", SWING),
    _spec("breakout_20", FeatureFamily.STRUCTURE, "Close above the prior 20-bar high.", "close_t > max(high_t-20..t-1)", "21 bars", SWING, provisional=False),
    _spec("breakdown_20", FeatureFamily.STRUCTURE, "Close below the prior 20-bar low.", "close_t < min(low_t-20..t-1)", "21 bars", SWING, provisional=False),
    _spec("range_bound_20", FeatureFamily.STRUCTURE, "Close remains inside the prior 20-bar range.", "prior_low <= close_t <= prior_high", "21 bars", SWING),
    _spec("support_proximity_20", FeatureFamily.STRUCTURE, "Distance above prior 20-bar support.", "close_t / min(low_t-20..t-1) - 1", "21 bars", SWING),
    _spec("resistance_proximity_20", FeatureFamily.STRUCTURE, "Distance below prior 20-bar resistance.", "max(high_t-20..t-1) / close_t - 1", "21 bars", SWING),
    _spec("distance_trading_range_20", FeatureFamily.STRUCTURE, "Close position within the prior 20-bar range.", "(close_t - prior_low) / (prior_high - prior_low)", "21 bars", SWING),
    # Multi-timeframe -------------------------------------------------------
    _spec("mtf_available_count", FeatureFamily.MULTI_TIMEFRAME, "Number of timeframes with validated data.", "count(available timeframes)", "all requested"),
    _spec("mtf_alignment", FeatureFamily.MULTI_TIMEFRAME, "Fraction of available timeframes with positive trend.", "positive trend count / available", "all requested"),
    _spec("mtf_trend_agreement", FeatureFamily.MULTI_TIMEFRAME, "Cross-timeframe trend agreement score.", "1 - dispersion of normalized trend signs", "all requested"),
    _spec("mtf_momentum_alignment", FeatureFamily.MULTI_TIMEFRAME, "Fraction of timeframes with positive momentum.", "positive return_5 count / available", "all requested"),
    _spec("mtf_volatility_ratio", FeatureFamily.MULTI_TIMEFRAME, "Lowest over highest available ATR%.", "min(atr_pct) / max(atr_pct)", "all requested"),
    _spec("mtf_short_within_long_structure", FeatureFamily.MULTI_TIMEFRAME, "Short close inside long timeframe range.", "0..1 position of shortest close in longest range", "all requested"),
    # Context ---------------------------------------------------------------
    _spec("relative_return_1", FeatureFamily.CONTEXT, "Instrument return minus benchmark return.", "return_1 - benchmark_return_1", "2 benchmark bars"),
    _spec("relative_return_20", FeatureFamily.CONTEXT, "Twenty-bar return minus benchmark return.", "return_20 - benchmark_return_20", "21 benchmark bars"),
    _spec("sector_relative_return_20", FeatureFamily.CONTEXT, "Twenty-bar return minus sector return.", "return_20 - sector_return_20", "21 sector bars"),
    _spec("market_volatility_20", FeatureFamily.CONTEXT, "Benchmark annualized volatility.", "hist_vol(benchmark close, 20)", "21 benchmark bars"),
    _spec("market_breadth", FeatureFamily.CONTEXT, "Share of supplied context instruments above their SMA20.", "advancing / supplied instruments", "20 context bars"),
    # Fundamentals ----------------------------------------------------------
    _spec("fundamental_revenue", FeatureFamily.FUNDAMENTAL, "Reported revenue for the latest filing.", "reported revenue", "latest filing", SWING),
    _spec("fundamental_net_profit", FeatureFamily.FUNDAMENTAL, "Reported net profit for the latest filing.", "reported net profit", "latest filing", SWING),
    _spec("fundamental_cash_flow", FeatureFamily.FUNDAMENTAL, "Reported operating cash flow for the latest filing.", "reported operating cash flow", "latest filing", SWING),
    _spec("fundamental_dividend_per_share", FeatureFamily.FUNDAMENTAL, "Reported dividend per share.", "reported dividend per share", "latest filing", SWING),
    _spec("fundamental_revenue_growth", FeatureFamily.FUNDAMENTAL, "YoY revenue growth.", "revenue_t / revenue_prior - 1", "two reported periods", SWING),
    _spec("fundamental_profit_growth", FeatureFamily.FUNDAMENTAL, "YoY net-profit growth.", "profit_t / profit_prior - 1", "two reported periods", SWING),
    _spec("fundamental_eps", FeatureFamily.FUNDAMENTAL, "Reported earnings per share.", "reported EPS", "latest filing", SWING),
    _spec("fundamental_pe", FeatureFamily.FUNDAMENTAL, "Derived price-to-earnings ratio.", "price / reported EPS", "latest filing + close", SWING),
    _spec("fundamental_pb", FeatureFamily.FUNDAMENTAL, "Derived price-to-book ratio.", "price / reported book value per share", "latest filing + close", SWING),
    _spec("fundamental_roe", FeatureFamily.FUNDAMENTAL, "Reported or derived return on equity.", "net profit / shareholders equity", "latest filing", SWING),
    _spec("fundamental_roce", FeatureFamily.FUNDAMENTAL, "Reported or derived return on capital employed.", "EBIT / capital employed", "latest filing", SWING),
    _spec("fundamental_debt_to_equity", FeatureFamily.FUNDAMENTAL, "Reported or derived debt-to-equity.", "total debt / equity", "latest filing", SWING),
    _spec("fundamental_operating_margin", FeatureFamily.FUNDAMENTAL, "Operating margin.", "operating income / revenue", "latest filing", SWING),
    _spec("fundamental_profit_margin", FeatureFamily.FUNDAMENTAL, "Net profit margin.", "net profit / revenue", "latest filing", SWING),
    _spec("fundamental_cash_flow_growth", FeatureFamily.FUNDAMENTAL, "YoY operating cash-flow growth.", "cash_flow_t / cash_flow_prior - 1", "two reported periods", SWING),
    _spec("fundamental_dividend_yield", FeatureFamily.FUNDAMENTAL, "Derived dividend yield.", "dividend per share / price", "latest filing + close", SWING),
    _spec("fundamental_age_days", FeatureFamily.FUNDAMENTAL, "Days since latest available publication.", "(as_of - received_at) in days", "latest filing", SWING),
    # News and events -------------------------------------------------------
    _spec("news_count_5d", FeatureFamily.NEWS_EVENT, "Deduplicated events available in the last 5 days.", "count unique evidence", "5 days"),
    _spec("news_count_20d", FeatureFamily.NEWS_EVENT, "Deduplicated events available in the last 20 days.", "count unique evidence", "20 days"),
    _spec("news_importance_max_20d", FeatureFamily.NEWS_EVENT, "Maximum importance in the last 20 days.", "max(importance)", "20 days"),
    _spec("news_relevance_max_20d", FeatureFamily.NEWS_EVENT, "Maximum supplied relevance in the last 20 days.", "max(relevance)", "20 days"),
    _spec("news_sentiment_mean_5d", FeatureFamily.NEWS_EVENT, "Mean validated sentiment in the last 5 days.", "mean(metadata.sentiment)", "5 days"),
    _spec("news_age_days", FeatureFamily.NEWS_EVENT, "Age of the latest available event.", "(as_of - received_at) in days", "latest event"),
    _spec("news_earnings_flag", FeatureFamily.NEWS_EVENT, "Earnings/results event available in the last 20 days.", "event category is earnings/results", "20 days"),
    _spec("news_corporate_action_flag", FeatureFamily.NEWS_EVENT, "Corporate action event available in the last 20 days.", "split/dividend/merger category", "20 days"),
    _spec("news_regulatory_flag", FeatureFamily.NEWS_EVENT, "Regulatory or management event available in the last 20 days.", "regulatory/management category", "20 days"),
)

FEATURE_SPECS: dict[str, FeatureSpec] = {spec.name: spec for spec in _SPECS}
if len(FEATURE_SPECS) != len(_SPECS):
    raise RuntimeError("duplicate PI-04 feature name in registry")


def feature_spec(name: str) -> FeatureSpec:
    try:
        return FEATURE_SPECS[name]
    except KeyError as exc:
        raise KeyError(f"unknown PI-04 feature {name!r}") from exc


def profile_features(profile: FeatureProfileName | str) -> tuple[FeatureSpec, ...]:
    selected = FeatureProfileName(profile)
    return tuple(spec for spec in _SPECS if selected in spec.profiles)


def registered_feature_names() -> tuple[str, ...]:
    return tuple(spec.name for spec in _SPECS)


__all__ = [
    "FEATURE_SPECS",
    "feature_spec",
    "profile_features",
    "registered_feature_names",
]
