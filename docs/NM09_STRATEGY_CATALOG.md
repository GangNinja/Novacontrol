# NM-09 Strategy Catalog

| id | category | core idea | default horizons |
|---|---|---|---|
| `trend_sma` | trend | fast SMA crossing a slow SMA | one_week, one_month |
| `mean_reversion_rsi` | mean reversion | RSI extremes with Bollinger location | few_sessions, one_week |
| `momentum_breakout` | momentum | close breaks a prior high with volume confirmation | next_session, few_sessions |
| `volatility_breakout` | volatility | ATR-scaled range expansion | next_session, few_sessions |
| `event_driven` | event-driven | verified, important instrument events | next_session, one_week |
| `forecast_assisted` | forecast-assisted | active forecast with bounded uncertainty | few_sessions, one_week |

Definitions are immutable catalog metadata; a configured strategy is a `StrategyRecord` with an explicit version. Config keys and ranges are validated and unknown keys are rejected. Each evaluator returns a `SignalDecision` with indicators, conditions, references and an explanation. Insufficient history produces `NO_SIGNAL` with `insufficient_history`; stale data produces `NO_SIGNAL` with `stale`.
