# NovaMarket PI-01 → PI-24 — Final Closure and Operational Readiness Report

**Date:** 21 November 2026
**Branch:** `feature/novamarket`
**HEAD:** `2a73ca6 feat(novamarket): land the PI-01–PI-24 prediction stack and realign the docs`
**Working tree:** 56 uncommitted paths (PI-01…PI-24 work plus this cycle's fixes); nothing committed, pushed or released.
**Scope:** final engineering closure of the existing 24 PIs. No PI-25, no new roadmap stage, no new feature, no architectural redesign.

---

## 1. Executive summary

NovaMarket's 24 phases are integrated and the complete NovaMarket test suite is
green in a fully provisioned environment: **1907 passed, 0 failed, 0 errors,
2 skipped, 445 subtests** in 12 m 16 s. `mypy src` is clean (721 files), the API
reference is in sync at **933 routes**, and all 14 SQLite stores pass
`integrity_check` with zero foreign-key violations.

This cycle additionally:

- reproduced and **fixed** audit observation O-1 at its cause (`/holdings`
  served a position's fill price while `/valuation` refreshed to the newest
  observable), with a regression test;
- verified the three earlier fixes (LSTM unavailable path, shutdown handle leak,
  stale route contract) individually and in the full suite;
- closed the earlier “20 environmental failures” question: they were the 13
  LSTM tests needing PyTorch and 7 gradient tests needing XGBoost/LightGBM.
  With those extras installed the suite has **no failures**;
- measured every estimation and strategy path that can be measured offline; and
- identified one genuine scope gap: the intended **20m → 5m → 1m decision
  hierarchy is not implemented as a decision chain** (1-minute exists only as
  the T−1m confirmation checkpoint and a 1m data interval).

**Controlled paper-trading readiness: PASS with limitations.** Live-market
readiness: **BLOCKED** (no live data or live paper session was used).

---

## 2. Repository and Git baseline

| Item | State |
|---|---|
| HEAD commit | `2a73ca6` (PI-01…PI-24 landed) |
| Uncommitted paths | 56 (source, tests, docs; includes this cycle's changes) |
| PI reports | 24 (`PI01…PI24_COMPLETION_REPORT.md`) |
| NovaMarket test modules | 55 |
| API routes | 933, `generate_api_reference.py --check` → in sync |
| Working-tree changes made in this cycle | `portfolio/engine.py` (O-1), `portfolio_management/service.py` (manual review fallback + active-only rotation), `lstm/service.py`, `service.py` (shutdown), `tests/test_novamarket_portfolio.py`, `tests/test_novamarket_pi21.py`, `tests/test_novamarket_lstm.py`, `tests/test_novamarket_gateway.py`, `tests/test_novamarket_api.py`, audit/closure reports |

No file was staged, committed, tagged, pushed or published.

---

## 3. PI-01 → PI-24 completion confirmation

The 24 phase reports were read. Every phase reports PASS or scoped PASS for its
stated scope; no phase was rebuilt and no established decision was changed.
This closure re-verified the integration seams rather than re-implementing any
phase:

| Group | Phases | Closure evidence |
|---|---|---|
| Contracts, data, features | PI-01…PI-04 | full suite green; 14 store integrity checks; dataset split contract reports `chronological: true` |
| Forecasting | PI-05…PI-08, PI-23 | 11 estimators evaluated walk-forward; ensemble/calibration tests pass; selection is validation-only |
| Timing and evaluation | PI-09…PI-11 | 5m/15m/20m/1h policies and T-20/T-5/T-1 checkpoints; backtest replay hash identical |
| Autonomy and execution | PI-12, AT-02…AT-06 | autonomy/AT suites pass; risk rejection stays non-executable; duplicate forward replays without a second order |
| Learning | PI-13…PI-16 | isolated environment, 12 isolation flags false including `automatic_policy_promotion` |
| Intelligence surfaces | PI-17…PI-24 | agent/hand-off, optimisation, research, dashboard, portfolio, risk, forecast-intelligence and event-intelligence suites pass |

---

## 4. Full regression test results

Environment: Python 3.13.0, numpy 2.5.3, pytest 9.1.1, then optional extras
installed into `.venv`:

```
xgboost 3.4.1   lightgbm 4.7.0   pandas 3.1.1   scipy 1.18.1
torch 2.15.0+cpu (installed from https://download.pytorch.org/whl/cpu)

pip install --only-binary=:all: "xgboost>=2.1,<4" "lightgbm>=4.5,<5" "pandas>=2.2"
pip install --only-binary=:all: "torch>=2.2,<3" --index-url https://download.pytorch.org/whl/cpu
```

This is the declared `prediction` + `lstm` extra set plus pandas for the ARIMA
comparison environment; Python 3.13 wheels exist for all of them.

| Run | Passed | Failed | Errors | Skipped | Subtests | Duration |
|---|---|---|---|---|---|---|
| Initial audit, base install (no ML extras) | 1882 | 14 | 8 | 2 | 445 | 11 m 46 s |
| After the three fixes, still base install | (targeted only) | 20 | 0 | — | — | — |
| **Final: fixes + ML extras** | **1907** | **0** | **0** | **2** | **445** | **12 m 16 s** |

### 4.1 Resolution of the 14 + 8 vs “20 remaining” discrepancy

The initial run reported **14 failed + 8 errors = 22 red tests**. Two were real
defects in the test harness/product (stale route contract, shutdown handle
leak); after fixing them, **20** remained:

- **13 LSTM tests** — needed PyTorch; after the unavailable-path crash fix they
  failed honestly with “PyTorch is not installed”.
- **7 gradient tests** — needed XGBoost and LightGBM.

Installing the declared extras (`prediction`, `lstm`) makes all 20 pass. No
test was skipped or assertion weakened; the 2 skipped tests are the existing
optional-Node renderer tests.

### 4.2 The three earlier fixes verified individually

| Fix | Test | Result |
|---|---|---|
| LSTM unavailable runtime reports instead of crashing | `test_unavailable_runtime_reports_instead_of_crashing` | pass |
| Shutdown closes lazily opened stores (Windows handle leak) | `test_service_exposes_lazy_gateway_status_and_contract_without_network` | pass |
| Route contract lists `/market/training/datasets` | `test_no_live_execution_route_exists` | pass |

---

## 5. Defect resolution report

### D-1 — LSTM unavailable-runtime crash (fixed, earlier cycle)
`lstm/service.py` passed an empty dict as the training matrix; `_report` read
`matrix.train_indices` → `AttributeError`. Fixed by passing `None`. Pinned by a
test that forces `backend_available=False`.

### D-2 — SQLite handle leak on shutdown (fixed, earlier cycle)
`NovaMarketService.shutdown()` returned early on a disabled service before
`_withdraw()`, leaving `novamarket_evaluation.sqlite3` open. Fixed; the gateway
test now shuts the service down and cleans its temporary directory on Windows.

### D-3 — Stale route-contract test (fixed, earlier cycle)
`/market/training/datasets` was missing from the expected route set. Added; set
equality is still asserted.

### D-4 — O-1 holdings/valuation divergence (fixed, this cycle)
See §6.

### D-5 — Manual review fallback could target a closed book (fixed, earlier cycle)
`run_cycle` with no portfolio id used `rows[0]`. It now selects the first
**active** portfolio and keeps the “an active portfolio is required” refusal
when none is active; both the rotation test and a new no-active-book test cover
it.

---

## 6. Holdings / valuation consistency investigation (O-1)

### 6.1 What was observed

The audit's E2E run read `/market/portfolios/{id}/holdings` and
`/market/portfolios/{id}/valuation` back-to-back and got ₹100.54 vs ₹100.50 for
the same 19.9005-unit position (₹0.80 difference).

### 6.2 Investigation

| Question | Finding |
|---|---|
| Market-price sourcing | `PortfolioEngine._price()` uses `market_data.latest()` (freshest quote), falling back to the latest bar. No second price source. |
| Timestamp alignment | Both endpoints use the engine clock; the divergence was not a clock skew. |
| Precision/rounding | Prices are quantised to paise (`PAISE`) and quantities to `SHARE`; no float rounding. |
| Caching | No cache between the two reads; the store is read directly. |
| Quantity | Identical in both responses (19.9005). |
| Valuation snapshots | `/valuation` calls `_valuate()` → `_refresh_positions()` → `value_portfolio()` → append-only snapshot. `/holdings` did **not** refresh. |
| Concurrency | Single-process SQLite access; not a concurrent-writer race. |
| Root cause | A position created by a fill stores the **fill price** as `last_price`; `/holdings` served it unchanged, while `/valuation` refreshed to the newest observable. Deterministic reproduction: position at 100.00, market moves to 110.00 → holdings returned 100.00, valuation returned 110.00. |

This is a **real defect in the read path**, not an intended difference: both
endpoints claim to report the book's current value.

### 6.3 Fix and verification

`PortfolioEngine.holdings()` now calls `_refresh_positions(record, now)` before
building the payload, exactly as `/valuation` does. One price per position is
therefore used by both endpoints. The fix does not force equal numbers — it
makes both read the same refreshed position, and the regression test still
checks the price move and the arithmetic:

- `test_holdings_and_valuation_agree_on_one_price` — after a move to ₹110.00,
  holdings report ₹110.00 and Σ holdings market value equals the valuation's
  `invested` exactly.
- Re-run E2E: holdings and valuation both report ₹100.50 / ₹2,000.00 / −₹1.59.

---

## 7. Multi-timeframe verification matrix

Implemented horizons (`ForecastHorizon`): `5m, 15m, 20m, 1h, 1d, 3d, 5d, 30d, 90d`.
Canonical data intervals: `spot, 1m, 5m, 15m, 30m, 1h, 1d, 1wk, 1mo`.

| Timeframe | Implemented? | Execution path | Verification |
|---|---|---|---|
| **20-minute** | YES | own horizon cycle; initial T−20m, confirmation T−5m, final T−1m (`HORIZON_SCHEDULE_POLICIES`) | scheduler tests: one stable T-20/T-5/T-1 cycle, duplicate-tick guard, restart recovery, atomic claims |
| **15-minute** | YES | T−12m / T−5m / T−1m (kept distinct inside 15m) | policy test + scheduler suite |
| **5-minute** | YES | T−4m / T−2m / T−1m | policy test + scheduler suite |
| **1-hour** | YES | full T−20m/T−5m/T−1m lifecycle applied near the hour end | policy test |
| **1-minute** | **PARTIAL** | 1m exists as (a) a market-data interval, (b) the final **T−1m confirmation checkpoint** of every intraday cycle. There is **no 1m forecast horizon, no 1m cycle, and no 1m entry-timing evaluator**. | explicit in `ForecastHorizon` and policy table |
| Session horizons | YES | 1d/3d/5d/30d/90d with session leads | scheduler + horizons tests |

Other checks:

- **Timestamp alignment / late data** — nested gateway source timestamps are
  accepted; a late initial keeps its real execution timestamp; a checkpoint past
  its target is never executed as on-time.
- **Stale observations** — rejected (`test_stale_source_timestamp_is_rejected`);
  risk marks stale market data insufficient.
- **Missing candles** — skipped, never fabricated; a failed final observation is
  dead-lettered and unconfirmed.
- **Duplicates** — duplicate tick creates no duplicate version; checkpoint
  claims are atomic with lease recovery.
- **Session boundaries** — calendar/session validation fails closed (verified
  years only, 2027 unpublished).
- **Decision hierarchy (20m context → 5m setup → 1m timing)** — **not
  implemented as such**. Strategies are single-horizon evaluators
  (`trend_sma`, `mean_reversion_rsi`, `momentum_breakout`,
  `volatility_breakout`, `event_driven`, `forecast_assisted`); there is no
  cross-timeframe confirmation or conflict resolver. The only conflict policy
  today is between *strategies* (`review`/`priority`), not between timeframes.

**Verdict: PARTIAL.** 5m, 15m, 20m and 1h monitoring are real and tested; the
1-minute layer is only the T−1m checkpoint, and the intended three-tier
hierarchy is absent. No new strategy was invented to paper over the gap.

---

## 8. Forecasting model evaluation

### 8.1 The 11 estimators

All 11 are implemented in `statistical/estimators.py` and selectable by ID:
`naive_last`, `random_walk`, `historical_return`, `seasonal_naive`, `sma`,
`wma`, `ema`, `ses`, `holt`, `holt_winters`, `arima`.

ARIMA is the in-repo bounded conditional-least-squares implementation
(`statistical/arima.py`) — an explicit decision to avoid the statsmodels/pandas
dependency; it records its own limitations and refuses unstable AR polynomials.
Holt-Winters refuses when the seasonal amplitude is indistinguishable from
noise. The six forecasting-engine families (`zero_return`, `random_walk`,
`historical_mean`, `drift`, `ewma`, `bootstrap`) are a separate layer.

### 8.2 Chronological walk-forward (synthetic, 420 sessions)

`evaluate_models` with 11 models, 1-day horizon/resolution, 60/20/20 split:
84 validation origins, 84 test origins, `leakage_safe=true`,
`test_used_for_selection=false`, selected on validation return MAE.

| Model | Val MAE | Test MAE | Test directional | Test interval coverage | Failures |
|---|---|---|---|---|---|
| naive_last | 0.8090 | 0.7492 | 0.4286 | n/a (point) | 0 |
| random_walk | 0.8090 | 0.7492 | 0.4286 | 0.8810 | 0 |
| historical_return | 0.8383 | 0.7570 | 0.4286 | 0.8810 | 0 |
| seasonal_naive | 1.7424 | 1.6194 | 0.3929 | 0.8810 | 0 |
| sma | 1.1541 | 1.1118 | 0.4167 | 0.8690 | 0 |
| wma | 1.0210 | 0.9657 | 0.4167 | 0.8929 | 0 |
| ema | 1.0392 | 0.9912 | 0.4048 | 0.8810 | 0 |
| ses | 0.8358 | 0.7664 | 0.4286 | 0.8810 | 0 |
| holt | 0.8724 | 0.8093 | 0.4286 | 0.7976 | 0 |
| holt_winters | 1.3465 | 1.2949 | 0.4286 | 0.8810 | 2 refused origins |
| arima | 0.8203 | 0.7512 | 0.4286 | 0.8929 | 4 refused origins |

On a random-walk synthetic series none of the models beats the naive baseline,
which is the expected and honest result; directional accuracy ≈ 0.40–0.43.
The 84 test origins are scored once and never used for selection.

### 8.3 The ACCEPT scorecard and the Brier question

Stored scorecard (`XNSE:ACCEPT`, `few_sessions`, bootstrap, 20 validation
origins): directional accuracy 0.60, MAE 0.0242, Brier 0.681 vs baseline 0.669,
calibration error 0.059, interval coverage 0.60.

The Brier score here is the **multi-class Brier** — mean squared distance from
the one-hot outcome across up/down/sideways. An uninformed uniform forecast
scores exactly **0.667**. The baseline's 0.669 is essentially uninformed; the
bootstrap's 0.681 is *worse than uninformed* on this sample. That is why it is
worse than the baseline: the sample is small (20), down-heavy, and the
bootstrap's simulated-return probabilities are more confident than the outcome
frequencies support. This is a calibration finding, not a metric bug: the
selection path already prefers the baseline unless a candidate's validation
Brier is strictly lower, and the measured result is reported as weak skill.

Precision/recall are not separately claimed for a three-class, 20-sample
directional problem; the confusion matrix is stored with the scorecard.

---

## 9. Trading strategy validation

- **Styles / horizons:** `short_term → day_trading`, `medium_term →
  swing_trading`, `long_term → long_term`. Horizon profiles differ materially:
  day trading (no overnight, 10 % max position, 1 % stop, intraday cadence),
  swing (overnight allowed, 20 %, 5 %, daily), long-term (35 %, 15 %,
  quarterly).
- **Strategies:** six catalog entries with typed BUY/SELL/EXIT/HOLD/NO_SIGNAL
  decisions, confidence and indicator evidence. Position sizing and exposure
  come from the horizon policy and available cash; stops/exits are enforced by
  the AT-05 exit pipeline, still gated by risk and execution.
- **Assets and signals:** signal generation uses the strategy's own horizon
  forecast and closes; no strategy consumes a higher-timeframe context (see
  §7).
- **Conflict handling:** strategy-level `review`/`priority` policy; proposals
  are deduplicated by signal identity.

Measured backtests (synthetic, strategy `trend_sma`, ₹100,000, 207 sessions):

| Scenario | Orders / filled | Closed trades | Win rate | Annualised | Max DD |
|---|---|---|---|---|---|
| Trending up | 0 / 0 | 0 | — | 0.00 | 0.00 |
| Trending down | 0 / 0 | 0 | — | 0.00 | 0.00 |
| Sideways | 10 / 10 | 5 | 0.00 | −1.39 % | −1.16 % |
| High volatility | 5 / 5 | 2 | 0.00 | −1.66 % | −5.13 % |
| Sudden move | 1 / 1 | 0 | — | −0.001 % | −0.001 % |
| Low volume | 0 / 0 | 0 | — | 0.00 | 0.00 |

The zero-order trend cases are honest: a pure monotonic series has no crossover
after warm-up, so the strategy correctly does nothing. These are simulated
series, not market evidence.

---

## 10. Risk management and execution independence

- NM-08 remains the sole decision authority; every filled backtest order maps to
  exactly one **approved** risk decision: sideways 10/10, high volatility 5/5,
  sudden move 1/1.
- Live E2E: stale data + high uncertainty produced **REJECTED** decisions;
  the execution bridge returned **409** for a rejected proposal
  (“only an approved proposal can be forwarded”).
- Duplicate forwarding of an approved proposal returns an idempotent **replay**
  (200) and creates **no second order** — the final order list held only the
  buy and the sell.
- Tests confirm position-cap skips, stale-data insufficiency, repeated-run
  reuse of signal/proposal/risk, and that a rejected proposal stays
  non-executable.
- RL/adaptive layers have no risk-policy or order path (`nm08_bypass: false`,
  `nm10_order_submission: false`).

---

## 11. Reinforcement-learning validation

- Isolated environment (own store, feature loader, forecast cache); 12
  isolation flags verified false, including `automatic_policy_promotion`,
  `nm08_bypass`, `nm10_order_submission`, `live_trading`,
  `activation_control`.
- **`automatic_policy_promotion = false` confirmed** in code, contracts and
  tests; there is no promotion/activation endpoint for experimental policies.
- Rewards: bounded, renormalised, cost-aware, decision-time evidence only,
  abstention not rewarded without evidence, outliers capped, configuration
  changes do not rewrite history.
- Training/evaluation separation: chronological walk-forward evaluation;
  test results are reported once and cannot select a policy.
- Rollback: a versioned policy catalogue exists in PI-16, but **no automatic RL
  policy rollback or promotion switch is implemented** — promotion is manual
  and out of the automatic path.
- **Does RL improve out-of-sample trading outcomes?** No evidence exists in the
  repository. There is no trained policy applied to a portfolio, no OOS
  comparison of a policy against the rule baseline with capital, and training
  reward is not a trading result. This remains **BLOCKED** on evidence, not
  claimed.

---

## 12. End-to-end paper-trading scenarios

Run on a copy of the live data directory (originals untouched), fixture bars and
synthetic quotes. This is not live-market validation.

| Stage | Measured result |
|---|---|
| Data → forecast | 220 bars; EWMA forecast `pending`, 3-session horizon, p_up 0.5588 |
| Strategy | SMA3/SMA9 crossover → BUY proposal, ₹2,000 notional |
| Risk | APPROVED (`rdec_…`, no constraint breached); earlier stale run REJECTED |
| Execution | 19.9005 filled at avg cost 100.58; one order |
| Position/valuation | holding 19.9005 @ 100.50 = ₹2,000.00; unrealized −₹1.59 |
| Duplicate forward | 200 replay, no second order |
| Exit | death cross → `remove_position`/sell approved → position closed; sell order filled |
| Realized P&L | −₹212.04; cash ₹19,787.97; total return −1.06 % |
| Ledger / NovaCoin | wallet INR and NC equal (1 NC = ₹1); running balances maintained |
| Backtest replay | stored and replayed repro hashes identical |
| Portfolio performance | 4 valuations; Sharpe/Sortino null with explicit “≥20 valuations required” |

Risk rejection was also exercised end-to-end (stale quote, high uncertainty),
and rejected proposals could not be forwarded.

---

## 13. Financial accounting verification

- Deposits are ledger credits and external flows, never investment profit
  (performance separates `external_flows`; the funded portfolio starts at
  return 0).
- Realized and unrealized P&L are separate (`realized_pnl` changes only on a
  sell; `unrealized_pnl` from marked positions).
- The valuation identity holds: cash + invested = total value; holdings market
  values sum to `invested` after the O-1 fix.
- NovaCoin is 1:1 with INR in the same wallet record (`available`, `total`,
  `nova_coin` all 17,998.42 at the buy snapshot).
- Ledger entries carry signed amounts and running balances; the sell leg moved
  proceeds back to the portfolio wallet.
- Minor rounding: realized P&L (−212.04) and the equity change reported by
  performance (−212.03) differ by one paise due to per-leg paise quantisation;
  recorded as technical debt, not a correctness failure.

---

## 14. Performance and resource benchmarks

Measured in-process via `TestClient` on a fresh data dir (single Windows
machine, no warm-up beyond app boot):

| Operation | Time |
|---|---|
| Register instrument | 6.6 ms |
| Ingest 220 daily bars | 403 ms (~1.8 ms/bar) |
| Append one quote | 1.4 ms |
| `/market/status` | 19.1 ms |
| Forecast training (walk-forward, 20 origins) | 53.4 ms |
| Forecast generation | 25.7 ms |
| Strategy decision pass | 2.3 ms (no signal in this fixture) |
| `/holdings` | 2.9 ms (20 reads: 31.9 ms total) |
| `/valuation` (refresh + snapshot) | 7.8 ms |
| Backtest create / run (207 sessions) | 71.2 / 204.1 ms |

Live server on `127.0.0.1:8011`: `/market/status` 0.39–0.47 s,
event-intelligence overview 0.062 s cold / 0.17 s, forecast-intelligence
overview 0.005 s, portfolios/risk/strategies ≈ 3–5 ms; uvicorn RSS 231–288 MB.
Data directory 132 MB (largest stores: `novamarket.sqlite3` 58 MB,
`novamarket_rl.sqlite3` 35 MB).

Bottlenecks: app/status route cost dominates first-call latency; backtests are
CPU-bound per session; model loading is lazy and cached. CPU percentage was not
sampled reliably (psutil is absent) — the test run is CPU-bound and took 12 m
16 s on this machine. No optimisation was made because no measured path
threatened correctness or the paper-trading loop.

---

## 15. Remaining technical debt

1. **1-minute decision layer and 20m→5m→1m hierarchy are absent** (§7). This is
   the largest scope gap against the intended monitoring description.
2. **Base install is red without extras.** LSTM/gradient tests have no
   `importorskip` guard; CI or developers must install the `prediction` and
   `lstm` extras, or the tests should skip cleanly. Recorded, not hidden.
3. **One-paise rounding difference** between realized P&L and equity change.
4. **No coverage measurement** (line/branch) is produced or claimed.
5. **`detail()` still serves stored position prices** while `holdings()` and
   `valuation()` refresh; its valuation and holdings are internally consistent,
   but a future cross-endpoint comparison should go through one refresh path.
6. **Valuation refresh on a read** (`/valuation` writes a snapshot and updates
   positions) is intentional but asymmetric with `/holdings`; documented here.

---

## 16. Known limitations

- Market was closed during this cycle; **no live data** was used. Every price
  in the E2E runs came from the `fixture` provider and is labelled
  `fixture_priced=true`.
- No sustained live paper-trading session, no broker integration, no live
  provider failure drill, no 2027 NSE holiday calendar (fail-closed).
- Forecast skill is weak on the measured samples (directional ≈ 0.40–0.60,
  Brier at/below uninformed); no accuracy claim is made.
- RL has no OOS trading evidence and no promoted policy.
- The 2 skipped tests are pre-existing conditional skips in optional
  integration tests (present in the initial audit too), not failures.

---

## 17. Security and operational risks

- The API is a local service protected by a single bearer/operator token; there
  is no TLS, user directory, or per-user authorization. It must not be exposed
  beyond loopback.
- Paper execution only; no broker credentials or live order path exists.
  `nm10_order_submission`, `live_trading` and `broker_connection` are false in
  the isolation contract.
- Synthetic/fixture prices are labelled per position and per book; they must
  never be read as live market prices.
- SQLite stores are single-process; the shutdown path now closes handles
  (fixed), but concurrent external writers are not supported.
- The API reference is generated and checked; route drift is caught in CI.

---

## 18. Final readiness verdict

| Area | Verdict |
|---|---|
| **A. Software integration readiness** | **PASS** — 1907/1907 tests, mypy clean, 933 routes in sync, 14 stores integrity-clean, deterministic backtests |
| **B. Forecasting and strategy intelligence readiness** | **PARTIAL** — all 11 estimators and 6 strategies run under chronological validation, but measured skill is weak, selection is baseline-conservative, and the 20m→5m→1m hierarchy is not implemented |
| **C. Paper-trading readiness** | **PASS (controlled, simulated)** — data→forecast→strategy→risk→paper execution→position→ledger→performance verified end-to-end, including rejection, duplicate replay, exit and realized P&L; still no sustained live paper session |
| **D. Live-market operational readiness** | **BLOCKED** — no live data, no broker, no live provider drill, 2027 calendar unpublished; passing tests do not establish live profitability or live risk behaviour |

**Conclusion:** NovaMarket is ready for **controlled paper-trading operation**
on simulated or paper data, with the risk gate and accounting verified. It is
not ready for live trading. No PI-25 or new roadmap stage was created.

---

## Post-closure enhancement — multi-timeframe hierarchy (21 November 2026)

The one substantive gap recorded in §7 and §16 has since been implemented as a
post-PI-24 enhancement: a 20-minute context → 5-minute setup → 1-minute timing
layer with an explicit conflict matrix, a real one-minute forecast cycle, and a
day-trading-only proposal gate before NM-08. It is documented in
[MULTITIMEFRAME_TRADING_INTELLIGENCE.md](MULTITIMEFRAME_TRADING_INTELLIGENCE.md).

This addendum does not rewrite the historical closure evidence above. The
forecasting/strategy readiness verdict remains **PARTIAL** until the hierarchy
has a larger chronological sample; the real-data evaluation covers two stored
sessions and two enhanced simulated trades, and no accuracy or profitability
improvement is claimed. Live-market readiness remains **BLOCKED**.
