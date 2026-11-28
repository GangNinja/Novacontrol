# Multi-Timeframe Trading Intelligence (20m → 5m → 1m)

**Status:** implemented post-PI-24 enhancement; no PI-25 and no roadmap change.
**Authority boundary:** analysis only. Every candidate still passes the
independent NM-08 risk engine and the NM-10 paper-execution bridge before any
order exists.

## 1. Why this layer exists

The PI-01–PI-24 closure audit found that 5m, 15m, 20m and 1h forecast cycles and
their T-20/T-5/T-1 checkpoints were real, but the intended
**20-minute context → 5-minute setup → 1-minute timing** decision hierarchy was
not. One-minute data existed only as a final confirmation checkpoint and as a
data interval. No component combined the three views, and no strategy consumed
higher-timeframe context.

This layer adds that missing decision intelligence without creating a second
scheduler, indicator library, risk engine or execution path.

## 2. Data flow

```
stored market data (20m context built from completed 5m buckets;
                   5m setup; 1m timing)
        │
        ▼
MultiTimeframeAnalyzer  ── context/setup/timing assessments + conflict matrix
        │                   (no order, risk or wallet field)
        ▼
existing StrategyEngine.evaluate (unchanged signal generation)
        │
        ▼
StrategyEngine.run gate for short_term/day_trading ONLY
        │  BUY/SELL proposal only for a matching candidate side;
        │  EXIT/HOLD pass; WAIT/REJECT/HOLD block new entries
        ▼
existing NM-08 risk decision  (mandatory, unchanged)
        │
        ▼
existing NM-10 paper execution (mandatory, unchanged)
```

The gate sits after signal generation and before proposal/risk creation, so a
hierarchy rejection creates **no proposal** and therefore cannot be forwarded to
execution. It does not suppress the signal record: rejected signals remain
auditable evidence with the hierarchy's reasons.

## 3. The three assessments

| Layer | Source | What it measures | Outputs |
|---|---|---|---|
| **20m context** | completed 5m bars aggregated into 20-minute buckets (or 1m buckets when configured) | direction, trend vs sideways, ATR-based volatility | `direction`, `condition`, `confidence`, bucket counts, SMA/ATR metrics |
| **5m setup** | 5m bars | momentum (EMA 9/21), 20-bar breakout, RSI reversal, agreement with context | `direction`, `setup_state` (`confirmed`/`weak`/`conflicting`/`absent`), RSI/EMA/volume metrics |
| **1m timing** | 1m bars | EMA 5/13 structure, 3-bar return, cross direction, sudden-move refusal, completeness/freshness | `direction`, `timing_state` (`entry_long`, `entry_short`, `exit_long`, `exit_short`, `wait`, `invalid`) |

Indicators are the accepted NM-09 functions (`sma`, `ema`, `rsi`, `atr`,
`highest`, `lowest`, `average_volume`); the layer adds no second indicator
implementation. Twenty-minute bars are aggregated only from **complete,
contiguous, closed** 5-minute buckets. The forming bucket is excluded.

## 4. Conflict-resolution rules

The resolver is explicit and deterministic. It never forces a trade.

| Situation | Outcome |
|---|---|
| Any timeframe missing, unusable, or stale | `WAIT_FOR_CONFIRMATION`, conflict `insufficient_data`, uncertainty `high` |
| 20m volatility at/above the configured ATR threshold | `WAIT_FOR_CONFIRMATION` (no timed entry) |
| 20m context neutral | `HOLD` (or a candidate only when `allow_neutral_context` is explicitly enabled) |
| 5m setup opposes 20m context | `REJECT_SIGNAL`, conflict `conflict` |
| 5m setup weak/absent | `WAIT_FOR_CONFIRMATION` |
| 1m timing signals an exit, is neutral, or opposes the setup | `WAIT_FOR_CONFIRMATION` |
| 1m entry trigger does not match the context side | `WAIT_FOR_CONFIRMATION` |
| Aligned confidence below the floor | `WAIT_FOR_CONFIRMATION` |
| All three agree with an entry trigger | `BUY_CANDIDATE` / `SELL_CANDIDATE`, conflict `aligned`, uncertainty `low` |

Examples required by the brief:

| 20m | 5m | 1m | Result |
|---|---|---|---|
| bullish | bullish | bullish entry | `BUY_CANDIDATE` |
| bullish | bullish | bearish | `WAIT_FOR_CONFIRMATION` |
| bullish | bearish | bullish | `REJECT_SIGNAL` (setup opposes context) |
| bearish | bullish | bullish | `REJECT_SIGNAL` |
| all disagree | | | `REJECT_SIGNAL` |
| any layer missing/stale/partial | | | `WAIT_FOR_CONFIRMATION`; missing layer named |

Configurable thresholds are one immutable `MultiTimeframeConfig`: bucket width
(default 20m from 5m), minimum observations, freshness windows (20m 1800s, 5m
900s, 1m 180s), maximum intraday gaps (1800/600/120s), trend band, high-volatility
ATR percentage, RSI bounds, sudden-move limit, confidence floor, neutral-context
and high-volatility switches, and a 2-second data-reuse cache. Unknown or invalid
overrides are refused.

## 5. Data-integrity behaviour

- **Partial candle** — excluded; it is never treated as completed. If excluding
  it leaves too little history, the timing layer is unavailable.
- **Stale timestamp** — the layer is marked stale and the decision waits; age is
  computed against the supplied decision time, not ingestion time.
- **Missing candles inside a session** — a gap beyond the configured limit
  invalidates that layer (fail closed). Overnight/session breaks (≥4h) are not
  treated as missing candles.
- **Duplicate timestamps** — collapsed to the last observation and reported.
- **Out-of-order rows** — sorted chronologically before any indicator.
- **Incomplete 20m bucket** — excluded; it cannot vote. A completed bucket's
  observation time is its close, so freshness is not aged by the bucket width.
- **Sudden 1m move** — beyond the configured 3% limit the timing layer refuses to
  chase and returns `wait`.
- **Future/unparseable rows** — ignored as invalid.
- **Recovery** — the analyzer is stateless apart from its short-lived series
  cache; a later complete observation recovers normally, and there is no partial
  decision state to repair.

A missing timeframe therefore cannot become bullish or bearish by default: it
produces `WAIT_FOR_CONFIRMATION` with the missing label listed.

## 6. One-minute operational cycle

`ForecastHorizon.MINUTES_1` is now a first-class horizon:

- spec: 1-minute duration, 1m maximum input interval, 30-bar minimum history;
- schedule policy: initial at cycle creation, T−30s first confirmation, T−5s
  final confirmation, target T+1m;
- accepted by the scheduler, prediction/dashboard/forecast-intelligence horizon
  registries, and the PI-12 decision configuration;
- the 1m **timing assessment** is computed independently on every hierarchy run,
  so it is not merely the final checkpoint of a longer cycle.

It remains a paper/research cycle: no order is created from a forecast or cycle.

## 7. Strategy and horizon integration

`StrategyEngine.run` applies the gate only when the portfolio horizon is
`short_term` (the day-trading book). `medium_term` (swing) and `long_term`
portfolios keep their existing strategy logic; the output reports
`timeframe.applied=false` for them. This prevents a 1-minute entry rule from
silently changing a multi-week investment decision.

When the analyzer is unavailable for a short-term book, new BUY/SELL entries are
blocked and the error is reported; the system does not fall back to ungated
day-trading entries. EXIT signals remain admissible so protective exits are not
blocked.

Every allowed candidate still calls the existing NM-08 `assess`; a rejected
hierarchy never reaches risk, and a risk rejection remains non-executable.
Forwarding continues through the existing single-use proposal path.

Strategy signal generation reads **daily** bars explicitly. The store returns
every interval when no interval is requested, so once 1m/5m hierarchy history
exists it would otherwise become the strategy's input series and replace the
catalogue's daily SMA/RSI discussion with intraday closes. Older injected market
adapters that do not accept an interval keep working through a narrow fallback.

## 8. API and observability

| Route | Purpose |
|---|---|
| `GET /market/multi-timeframe/status` | contract, configured thresholds, authority boundary |
| `GET /market/multi-timeframe/analysis?portfolio_id=…&instrument_ids=…` | decisions for a portfolio's instruments |

`POST /market/strategies/run` now returns a `timeframe` block with `applied`,
the portfolio horizon, per-instrument decisions, rejected signals and analyzer
errors. The generated API reference is in sync at **935 routes**.

## 9. Tests

`tests/test_novamarket_multitimeframe.py` (31 tests) plus two API tests:

- resolution matrix (all agree, 1m opposition, setup opposition, bearish context,
  all-disagree, neutral context, high volatility, missing/stale/exit/low-confidence);
- real-series behaviour (bull/bear, missing 1m, stale 1m, gaps, partial final
  candle, duplicates, out-of-order, sudden move, high volatility, cache reuse,
  invalid config);
- one-minute horizon/spec and scheduler checkpoint ordering;
- strategy/risk integration: rejection creates no proposal and no risk call,
  alignment reaches risk, swing remains ungated, analyzer failure blocks new
  entries, and an approved candidate uses the existing forwarder.

Additional coverage: `tests/test_novamarket_api.py` exercises the status route
and the analysis refusal path; existing strategy, risk, PI-12, PI-23, scheduler
and API suites are regression-tested. Full suite: **1940 passed, 0 failed,
0 errors, 2 skipped, 447 subtests in 734.17 s**. `tmp/audit/mtf_paper_chain.py` drives the
full HTTP chain on a copied data dir: aligned 20m/5m/1m bars and a seeded daily
crossover produce `buy_candidate`, the strategy BUY is admitted, NM-08 returns
`approved`, forwarding returns 200 with a filled order and the position appears
in holdings. The same chain shows a missing/stale layer blocking the entry.

## 10. Chronological evaluation (real stored 1m bars)

Run: `tmp/audit/mtf_eval.py` (output `tmp/audit/mtf_eval.json`).

Data: the real 1-minute bars already stored for `XNSE:ADANIENT` (250 bars,
2026-11-05) and `XBOM:RELIANCE` (250 bars, 2026-11-06), aggregated into 49
five-minute bars each. Chronological walk-forward; no parameter fitting; the
test split is reported separately and was not used for selection. Holding period
5 bars; 5 bps cost + 2 bps slippage.

| Metric | Baseline EMA(5/13) 1m | Enhanced hierarchy |
|---|---|---|
| Decisions | 165 | 165 |
| Candidates / signals | 11 crossovers | 10 candidates |
| Executed simulated trades | 11 | 2 |
| Win rate | 18.18% | 50.00% |
| Profit factor | 0.0955 | 0.0712 |
| Mean trade return | −0.0661% | −0.0149% |
| Cumulative (additive, 2 trades) | −0.7245% | −0.0297% |
| Max drawdown | −0.7983% | −0.0320% |
| False signals | 9 | 1 |
| Signal rejection rate | 0% | 93.94% |
| Decision latency (mean) | 0.0127 ms | 0.9391 ms |
| Test-split trades | 4, win 50%, PF 0.3972 | 1, win 0%, PF 0 |

**No accuracy or profitability claim is made.** The sample is two sessions and
two simulated enhanced trades; baseline and enhanced both lose after costs. The
honest reading is that the hierarchy rejected 93.94% of decisions and produced
two trades, one of which lost on the chronological test split. More sessions and
walk-forward evidence are required before any quality claim.

## 11. Performance

Run: `tmp/audit/mtf_perf.py` (output `tmp/audit/mtf_perf.json`), 16 cold
decisions on stored 1m data:

| Measurement | Result |
|---|---|
| Cold decision mean / p95 / max | 0.6126 / 0.6980 / 0.7195 ms |
| Warm cache mean / p95 | 0.2308 / 0.3624 ms |
| Baseline 1m EMA decision | 0.0043 ms |
| Share of the 60,000 ms one-minute budget | 0.001021% |

The hierarchy adds roughly 0.6 ms of in-process work per instrument per
decision on this machine. It reads at most 400 stored bars per interval and
reuses identical `(instrument, interval, decision-time)` series for 2 seconds.
No model inference is performed by the hierarchy; it reuses deterministic
indicators and the existing store. A 1-minute cycle is therefore not a load
concern, and the analyzer never blocks on network or model calls.

## 12. Known limitations

- The evaluation sample is two stored sessions and two enhanced trades; it cannot
  establish improved accuracy or profitability. Enhanced profit factor (0.0712)
  remains negative after costs, as does the baseline's (0.0955).
- Stored 1m data is sparse for some instruments (quality `stale`/`suspect`, many
  missing minutes). The analyzer refuses incomplete layers rather than
  interpolating, so real-data coverage is limited by the provider's 1m feed.
- The 20m bucket anchor is UTC epoch-aligned; it is deterministic but not
  session-open-anchored.
- The hierarchy gates entries only for the `short_term`/day-trading book. Swing
  and long-term confirmation is deliberately unchanged.
- No browser UI was added; the capability is exposed through the existing API
  and strategy-run evidence.
- No live-market validation is claimed.
