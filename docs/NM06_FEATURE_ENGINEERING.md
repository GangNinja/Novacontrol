# NM-06 — Feature Engineering

`forecasting/features.py` is the only place that turns bars into inputs for a
model. It is a pure function: no clock, no network, no randomness, no state.

## The cutoff rule

A feature set is built for one instrument at one **cutoff** timestamp. A bar is
eligible only when all of the following hold:

1. `observed_at <= cutoff` — a later bar cannot exist for this forecast;
2. open, high, low and close are finite and positive;
3. `high >= low` and `low <= close <= high` — an incoherent bar is dropped rather
   than repaired;
4. the bar is complete (the engine drops `partial=True` bars before this point);
5. its quality is not `invalid` or `rejected` (dropped by the engine).

Bars are deduplicated by timestamp (`observed_at.isoformat()`), keeping the last
occurrence, then sorted oldest-first. The same input always yields the same
ordered sequence, so two calls at the same cutoff produce identical values and
an identical digest.

`compute_features` returns `None` when fewer than `min_history_bars` (80) usable
bars remain. `None` is a refusal, not an empty feature set: the engine turns it
into a typed data error.

## The nineteen features

| Feature | Definition | Why it is here |
| --- | --- | --- |
| `last_close` | the newest eligible close | the base for every price conversion |
| `ret_1` | 1-session return | last move |
| `ret_5` | 5-session return | weekly momentum |
| `ret_21` | 21-session return | monthly momentum |
| `vol_21` | stdev of daily returns × √252 over 21 sessions | short-horizon risk |
| `vol_63` | stdev of daily returns × √252 over 63 sessions | longer-horizon risk |
| `atr_14_pct` | 14-session average true range ÷ `last_close` | range-based volatility |
| `range_20_pct` | mean `(high − low) / close` over 20 bars | intraday range |
| `rel_volume` | mean volume (5) ÷ mean volume (20) | participation change; 1.0 when volume is unavailable |
| `drawdown_60` | `last_close / max(close[-60:]) − 1` | distance from the recent high |
| `trend_slope_20` | least-squares slope over 20 closes, normalised by the mean and the count | direction of the recent trend |
| `momentum_10` | 10-session return | short trend confirmation |
| `sma_20_gap` | `last_close / SMA(20) − 1` | price versus its short mean |
| `sma_60_gap` | `last_close / SMA(60) − 1` | price versus its long mean |
| `ema_12_26` | `(EMA(12) − EMA(26)) / last_close` | MACD-style spread, scale-free |
| `rsi_14` | 14-session relative strength index | overbought/oversold context |
| `observation_count` | eligible bar count | how much evidence the features rest on |
| `first_observed_at` / `last_observed_at` | coverage window | freshness and coverage checks |

Every value is finite and asserted as such by the test suite. Where a window is
shorter than required (for example `sma_60_gap` with 40 bars) the feature is
reported as `0.0` rather than a fabricated number, and the bar-count floor
prevents a feature set from resting on almost no history.

## Feature set digest

The digest is `sha256` over a canonical JSON document containing:

- `feature_set_version` (`nm06-features-1.0`),
- `instrument_id`,
- `cutoff` (ISO-8601),
- the full policy (`ForecastPolicy.to_dict()`),
- every eligible bar as `[observed_at, open, high, low, close, volume]`.

Because the policy and the exact bars are inside the digest, changing a
threshold, a bar, or the version changes the digest. The forecast stores both the
feature digest and its own payload digest, so a stored forecast can be checked
against a recomputation. The test suite proves that appending a bar **after** the
cutoff leaves the digest and the last close unchanged.

## Regime classification

`classify_regime` labels the state the features describe. The rules are ordered
and explicit:

| Condition | Regime |
| --- | --- |
| `vol_21 > max(1.5 × vol_63, 2.5%)` | `high_volatility` |
| `sma_20_gap > 2%` and `trend_slope_20 > 0` | `trending_up` |
| `sma_20_gap < −2%` and `trend_slope_20 < 0` | `trending_down` |
| `vol_21 < 1.2%` | `low_volatility` |
| `|sma_20_gap| < 1.5%` | `sideways` |
| otherwise | `uncertain` |

`uncertain` is a real answer: mixed evidence is labelled as mixed. The regime is
recorded on the forecast and carried into performance analysis; it never changes
the probabilities by itself.

## What is deliberately excluded

- **NM-04 events** are not estimator inputs. They are recorded on the forecast and
  can widen the interval (see `NM06_FORECAST_CONTRACTS.md`); no event text is
  parsed into a feature, and no unconfirmed event is used at all.
- **NM-05 candidate scores** are provenance, not features. A discovery score is
  not a return observation and using it as one would make the forecast's
  reproducibility depend on another subsystem's ranking.
- **Later bars, live quotes and partial bars** are excluded by construction.
- **Future events** are excluded by the same cutoff comparison on
  `published_at`.

## Reproducibility contract

Given the same instrument, cutoff, policy and bar set:

- the feature set is identical, including the digest;
- the forecast id, digest and every value of the forecast are identical;
- re-running the estimators changes nothing (the bootstrap's seed is derived from
  the input closes, not from a global RNG).

This is what makes a stored forecast auditable: anyone with the same history can
recompute it and compare digests rather than trusting the claim.
