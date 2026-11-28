# NovaMarket — Final System-Wide Readiness Assessment

> **Historical baseline.** This is the pre-remediation assessment. The security and operational findings below were re-assessed and remediated in [SECURITY_REMEDIATION_AND_OPERATIONAL_CLOSURE_REPORT.md](SECURITY_REMEDIATION_AND_OPERATIONAL_CLOSURE_REPORT.md); that report is current where the two differ. The body below is retained unchanged as the point-in-time record.

**Date:** 22 November 2026  
**Suite rerun recorded:** 23 November 2026

**Repository:** `C:\Users\NARASIMHA\OneDrive\Desktop\NovaControl`

**Worktree:** `novamarket`

**Branch:** `feature/novamarket`

**HEAD:** `9f0aec8 feat(novamarket): close PI-01–PI-24 with fixes, verification and refreshed docs`
**Mode:** controlled paper trading only. No live broker, no live order route, no real-money trading.

## 1. Executive summary

All six requested reports exist and were inspected. There is no missing report:

| Report | Latest evidence used |
|---|---|
| `docs/FINAL_PI01_PI24_CLOSURE_AND_READINESS_REPORT.md` | Historical closure: 1,907 passed, 933 routes, 14 stores |
| `docs/MULTITIMEFRAME_TRADING_INTELLIGENCE.md` | 20m/5m/1m implementation and real-series replay |
| `docs/FORECASTING_INTELLIGENCE_OPTIMIZATION_REPORT.md` | 11 PI-05 estimators, NM-06 scorecards, calibration and abstention |
| `docs/CONTINUOUS_PAPER_TRADING_VALIDATION_REPORT.md` | Paper chain, real-data replay, fixture lifecycle and failures |
| `docs/RL_STRATEGY_EVALUATION_AND_ADAPTIVE_INTELLIGENCE_REPORT.md` | Zero-trade RL result and governance boundary |
| `docs/OPERATIONAL_RELIABILITY_SECURITY_AND_RESILIENCE_REPORT.md` | Current reliability, security and recovery audit |

The reports are cumulative. The older counts are not the current state: the closure report predates the MTF, forecasting-optimization, continuous-paper and RL reports, and the operational report predates the final regression-test addition. Current verified facts are:

- **1,972 NovaMarket tests collected.**
- The 23 November 2026 rerun covered all 1,972 tests in two ordered groups: **697 passed, 390 subtests** (Group A) and **1,272 passed, 1 failed, 2 skipped, 57 subtests** (Group B). The failure is a date-sensitive scheduler fixture (`2026-11-24` is a verified NSE holiday), confirmed in isolation; it is not evidence of a product execution defect.
- The same 1,972 tests ran in one process and completed: **1,969 passed, 1 failed, 2 skipped and 447 subtests passed** in 514.43 s. The historical LSTM crash was not reproduced. One completed run does not resolve the prior instability or make the suite green.
- **935 API routes**, `generate_api_reference.py --check` in sync.
- **15 SQLite files** in `data/`: 14 NovaMarket stores plus `memory.sqlite3`; 14 non-empty files pass integrity checks, and the 0-byte `novamarket_adaptive.sqlite3` is a not-yet-opened placeholder.
- `mypy src` and `mypy src --platform win32` are clean.
- The primary financial DB copy has 127 tables and 62,408 rows; backup/restore preserved both and detected a deliberately corrupted backup.
- The isolated wallet ledger reconciles: 17 wallets, 40 entries, 20 transactions, ledger sum `0.00`, zero discrepancies.
- A real SQLite write-lock drill failed closed after 5.585 s with no balance mutation.

The final result is deliberately not a profitability claim:

- The MTF hierarchy is implemented and tested, but the real-data replay contains only **two sessions and two enhanced trades**; both the baseline and enhanced paths lost after 7 bps costs.
- The best NM-06 probabilistic test result, `bootstrap` at Brier 0.6423, is only 0.0019 better than `random_walk` and is within sampling noise. Five resolved real forecasts produced 40% directional accuracy and mean Brier 0.6839, worse than the uniform 0.6667 reference.
- Calibration is report-only. All five served forecasts were `uncalibrated`; the accepted-calibrator test panel is 135 observations and one of four cases regressed.
- The enhanced/forecast-assisted/RL strategy comparison is insufficient. The forecast-assisted strategy made **zero** trades; every evaluated RL policy made **zero** trades.
- There was no live-market paper session. All real-data evaluation is historical replay; the fixture lifecycle is deterministic synthetic trading and is not market evidence.
- No live broker exists, no live order route exists, and the autonomy runtime reports `trading_implemented=false`, `orders_possible=false`, `exit_supervision_implemented=false` and `learning_promotion_implemented=false`.

**Readiness conclusions:**

- **Level 1 — Software readiness: PASS (conditional).** The tested software paths are internally consistent and pass type, contract, database and accounting checks. The historical single-process test-runner crash was not reproduced in the 23 November rerun, but that run still failed on a date-sensitive scheduler fixture; the unresolved runner instability and audit-log rename failure are real operational limitations, and neither establishes a financial-control failure.
- **Level 2 — Paper-trading readiness: PARTIAL.** The paper chain works on controlled data, but sustained real-market operation over repeated sessions has not been demonstrated.
- **Level 3 — Live-trading readiness: BLOCKED.** There is no live data, broker, live paper session, production deployment, regulatory approval or operational evidence for real money.

No PI-25 was created, the 24-PI roadmap was not modified, no feature was added, no live trading was enabled, no RL policy was promoted, no financial records were deleted, and no commit or push was made.

## 2. Verified architecture

The implemented system is a set of layered domain services composed by `NovaMarketService`, not a single monolithic trading loop. The following trace is based on the actual constructors and call sites, not only the architecture document.

| Stage | Implemented path | Connection verified | Assessment |
|---|---|---|---|
| Market data providers | `marketdata/` provider adapters, gateway, retries, circuit breaker, quota and provenance | Engine/store reads are shared by forecast, portfolio, risk and execution | PASS at contract/test level; no live outage |
| Data ingestion | Market-data engine and historical/interval store | Bars carry interval, quality, `observed_at` and partial flags | PASS |
| Market monitoring | Radar, discovery and autonomy monitoring/checkpoint shell | Radar/discovery feed strategy context; autonomy has durable cycles and leases | PASS for controls; 1m/5m autonomy refresh is not the normal path |
| 20m/5m/1m analysis | `multitimeframe.MultiTimeframeAnalyzer` | Strategy engine receives the analyzer via `set_timeframe_analyzer()` and gates short-term entries | PASS as analysis/gate; small real sample |
| Feature engineering | PI-04 `FeatureService` over the market store | Used by gradient/LSTM and adaptive services; **not** a mandatory input to the NM-06 probabilistic forecast engine | PARTIAL integration |
| Forecasting | NM-06 `ForecastEngine`; PI-05 statistical estimators; PI-06 gradient; PI-07 LSTM; PI-08 ensemble | Forecast records and skill gate; strategy and risk receive forecast evidence | PASS implementation; weak/insufficient skill |
| Forecast skill gate | `ForecastEngine` selection with `random_walk` reference and abstention; PI-05 5% validation margin | A challenger without positive validation skill is not promoted; baseline is retained | PASS as a conservative gate |
| Strategy selection | NM-09 catalogue: trend, mean reversion, momentum, volatility, event-driven, forecast-assisted | Strategy engine evaluates bars, forecasts, radar and discovery; duplicate BUY consolidation and conflict policy are present | PASS implementation; effectiveness unproven |
| Trading decision | Signal → `ProposedAction`; no execution authority | Proposal records strategy/version, cutoff, risk fields and expiry | PASS |
| NM-08 risk validation | Independent `RiskEngine.assess()` called before proposal approval | Strategy rejects non-approved proposals; execution re-reads the exact decision | PASS |
| NM-10 paper execution | `ExecutionEngine.submit()` with status/risk checks, actual-notional revalidation, reservations, fill journal | Exactly one forwarder from strategy; duplicate proposal reuses the stored order | PASS |
| Portfolio management | `PortfolioEngine` positions, valuation, performance and reconciliation | Execution updates the portfolio and wallet through shared stores | PASS |
| Financial ledger | NovaCoin wallet plus double-entry-style ledger | Execution fees, fills, deposits and realized P&L are recorded and reconciled | PASS |
| Performance evaluation | Portfolio performance, backtests, shadow mode and evaluation stores | Performance/paper/shadow loaders are available to `LearningService` | PASS implementation; short samples |
| Learning and feedback | NM-13 `LearningService` and NM-16/PI adaptive services | Operator-triggered experiments; approved promotion applies a strategy config; RL remains advisory | PARTIAL: not an automatic closed loop |

### Integration gaps and duplicate paths

1. **The analytical branches are parallel, not one mandatory chain.** NM-06 uses market data directly; PI-04 features feed gradient/LSTM/adaptive models but are not a required stage in the NM-06 forecast that risk and strategy consume. This is a real integration limitation for the requested “Feature Engineering → Forecasting” trace, not evidence that the individual services are broken.
2. **There is one execution authority.** The strategy engine only creates proposals and forwards approved ones; NM-10 is the only paper order gateway. No second execution path was found.
3. **MTF is evidence-only and short-term-only.** It can admit or reject a day-trading entry, but it never approves risk, submits an order or overrides NM-08. Medium-term/swing and long-term portfolios are intentionally not gated by it.
4. **Learning is operator-driven.** `LearningService` can load paper, shadow and backtest evidence and apply an approved strategy configuration, but no automatic promotion loop or performance-to-policy feedback was demonstrated. This preserves governance but does not satisfy a fully automatic “learning and feedback” chain.
5. **Autonomy is not a continuously running paper trader.** The status explicitly reports the missing trading/exit/learning authorities. Manual harnesses and scheduled components exist; unattended continuous operation does not.
6. **The forecast-assisted path is effectively inactive in the tested data.** Its gate correctly refused a forecast with zero up-probability, so no trade and no comparison sample exist.

No financial authority was found outside NM-08/NM-10. No route or service can turn an RL action, MTF decision or forecast directly into an order.

## 3. Original PI-01 → PI-24 completion status

The 24 original PI completion reports are present (`PI01` through `PI24`; the PI-24 file is titled “Final Completion and Integration Report”). The roadmap was not edited by this assessment.

| Original scope | PIs | Status |
|---|---|---|
| Prediction contracts, datasets, gateway, features | PI-01–PI-04 | **PASS (implementation)** |
| Statistical/ML/LSTM/ensemble forecasting | PI-05–PI-08 | **PASS (implementation); PARTIAL (demonstrated skill)** |
| Scheduler, swing/long horizons, evaluation, autonomous integration | PI-09–PI-12 | **PASS (implementation)** |
| RL environment, reward, training, adaptive intelligence | PI-13–PI-16 | **PASS (isolation and implementation); FAIL (policy effectiveness)** |
| Multi-agent, portfolio optimization, research, dashboard | PI-17–PI-20 | **PASS (implementation)** |
| Autonomous portfolio, risk intelligence, forecast optimization, final integration | PI-21–PI-24 | **PASS (implementation)** |

The distinction matters: “PI complete” means the contracted implementation, tests and API surface exist. It does not mean every strategy or model has demonstrated economic value. The original closure report itself rated forecasting/strategy intelligence **PARTIAL** because measured skill was weak; this assessment preserves that honesty.

No PI-25 was created.

## 4. Multi-timeframe evaluation

### Implemented hierarchy

- **20-minute context:** directional/neutral backdrop.
- **5-minute setup:** momentum, breakout or reversal confirmation.
- **1-minute timing:** momentum and structure alignment.
- **Conflict matrix:** all-agree, setup opposition, timing opposition, neutral context, high volatility, missing/stale/partial layer, exit and low-confidence paths.
- **Fail-closed data rules:** incomplete current candle excluded; missing or stale layer produces `WAIT_FOR_CONFIRMATION`; gaps and duplicate/out-of-order bars do not fabricate a series.
- **Authority:** analysis only. `executes=false`, `approves=false`; NM-08 remains risk authority and NM-10 execution authority.
- **Scope:** `short_term`/day-trading entries only. Medium-term/swing and long-term portfolios remain ungated.

### Real-data replay evidence

The published evaluation used stored real 1-minute bars for `XNSE:ADANIENT`/`XBOM:RELIANCE` (250 bars each), aggregated into 49 five-minute bars. It made 165 decisions with no parameter fitting.

| Metric | Baseline EMA(5/13) | Enhanced hierarchy |
|---|---:|---:|
| Decisions | 165 | 165 |
| Candidates/signals | 11 | 10 |
| Executed simulated trades | 11 | 2 |
| Win rate | 18.18% | 50.00% |
| Profit factor | 0.0955 | 0.0712 |
| Mean trade return | −0.0661% | −0.0149% |
| Cumulative return | −0.7245% | −0.0297% |
| Maximum drawdown | −0.7983% | −0.0320% |
| False signals | 9 | 1 |
| Rejection/abstention rate | 0% | 93.94% |
| Test-split trades | 4, 50% win, PF 0.3972 | 1, 0% win, PF 0 |

Both paths lost after the stated 5 bps cost plus 2 bps slippage. The hierarchy reduced false signals and exposure, but it also produced a worse profit factor on two trades. **No accuracy, profitability or superiority claim is made.**

The data also has only two real sessions, derived (not provider-native) five-minute bars, sparse one-minute coverage and no candidate on one instrument. The 32-test MTF suite covers resolution, stale/missing/partial/gap, future-bar exclusion, cache, API refusal, risk handoff and swing non-gating. The future-bar regression is important: before the fix, historical replay could see later observations.

**Verdict: PASS for implemented analysis and safety; PARTIAL for demonstrated decision quality.** The layer is usable as an evidence gate, not as proof of an edge.

## 5. Forecasting scorecards

### PI-05 eleven point estimators

The canonical model list is exactly 11: `naive_last`, `random_walk`, `historical_return`, `seasonal_naive`, `sma`, `wma`, `ema`, `ses`, `holt`, `holt_winters`, `arima`. The real-asset walk-forward results were:

| Model | n | Price MAE | Price RMSE | sMAPE | Direction | 90% coverage | Mean latency |
|---|---:|---:|---:|---:|---:|---:|---:|
| `naive_last` | 643 | **11.380** | **15.387** | **0.005327** | 80.09% | — | 0.07 ms |
| `random_walk` | 643 | 11.380 | 15.387 | 0.005327 | 80.09% | 81.18% | 1.99 ms |
| `historical_return` | 643 | 11.500 | 15.550 | 0.005347 | 80.09% | 81.18% | 1.87 ms |
| `arima` | 643 | 11.484 | 15.544 | 0.005344 | 80.09% | 86.47% | 10.70 ms |
| `ses` | 643 | 13.584 | 17.760 | 0.006484 | 79.63% | 84.14% | 1.25 ms |
| `holt` | 643 | 14.153 | 18.360 | 0.006627 | 79.78% | 85.85% | 1.04 ms |
| `wma` | 643 | 18.441 | 23.377 | 0.008790 | 79.78% | 86.63% | 2.33 ms |
| `seasonal_naive` | 643 | 19.835 | 24.743 | 0.009331 | 80.40% | 84.76% | 1.04 ms |
| `ema` | 643 | 19.962 | 25.154 | 0.009486 | 79.78% | 86.16% | 2.22 ms |
| `sma` | 643 | 22.413 | 27.930 | 0.010580 | 79.94% | 86.31% | 1.28 ms |
| `holt_winters` | 5 | 46.686 | 48.664 | 0.036369 | 20.00% | 20.00% | refused on daily data |

`holt_winters` only fitted five intraday cases and refused the daily series because seasonal amplitude was not distinguishable from noise. It should not be selected automatically on these assets.

The 80% directional accuracy is not an edge: the neutral class dominates. `naive_last` and `random_walk` have the best point error; ARIMA has better nominal interval coverage but not better MAE. Adding complexity did not improve out-of-sample point accuracy.

### NM-06 probabilistic models

Eight real daily instruments, horizons 1/3/5 sessions, 70/30 chronological split, 950 validation and 415 test observations. `zero_return` is point-only and has no Brier score.

| Model | Validation Brier | Validation skill | Test Brier | Test skill | Test MAE | Test ECE |
|---|---:|---:|---:|---:|---:|---:|
| `bootstrap` | 0.6621 | +0.68% | **0.6423** | **+3.66%** | 0.0214 | 0.1070 |
| `random_walk` | 0.6625 | +0.62% | 0.6442 | +3.38% | 0.0213 | 0.0725 |
| `historical_mean` | 0.6625 | +0.62% | 0.6442 | +3.38% | 0.0214 | 0.0725 |
| `ewma` | 0.6827 | −2.40% | 0.6800 | −1.99% | 0.0227 | 0.1844 |
| `drift` | 0.6792 | −1.88% | 0.6976 | −4.65% | 0.0227 | 0.1905 |
| `zero_return` | — | — | — | — | **0.0212** | — |

The reference Brier is `0.666667`, so skill is `1 − Brier / 0.666667`. `bootstrap` beats `random_walk` by only 0.0019 Brier (0.3% relative), inside sampling noise. `drift` and `ewma` are worse than uniform. No statistically significant superiority is claimed.

### Model selection and the skill gate

- NM-06 selects against `random_walk`, not against a fixture or the test slice.
- A challenger without positive validation skill is not promoted; the engine records `abstained=true` and retains the baseline.
- PI-05 applies a pre-registered 5% validation MAE margin. Six of ten cases retained `random_walk`; four challengers cleared the hurdle. Only one selection changed (ADANIENT 1d), and its test MAE moved from 43.712 to 43.142.
- The weighted selected test MAE was 11.717 versus `random_walk`’s 11.380. A validation margin filters marginal promotions; it cannot guarantee that a validation advantage transfers.
- In the five real continuous-paper forecasts, three of five skill-gate runs abstained to the baseline. The two RELIANCE challengers had small positive validation skill but lost directionally and had Brier worse than uniform out of sample.

### Calibration and abstention

- Calibration supports binary/three-class isotonic and temperature scaling with a validation holdout and strict Brier-improvement acceptance.
- Across 24 asset/horizon cases, 20 were insufficient or had no probabilistic model; 4 produced an accepted validation calibrator (all temperature, 1-session).
- The four accepted test cases moved from **0.6635 to 0.6564** weighted Brier, but one regressed and the panel is only 135 observations.
- **`apply_calibration=False` by default.** The five served forecasts were all `uncalibrated`; calibration is report-only until a larger panel supports application.
- Abstention is real: three of five ADANIENT forecasts retained the baseline; the forecast-assisted strategy made no trade when its probability gate was not satisfied.

### Horizons, instruments and leakage

- Evaluated daily horizons: 1, 3 and 5 sessions. 1-minute, 5-minute, 20-minute and 1-hour paths exist; 1m/5m directional 100% is a class-imbalance artifact; 20m had 19 test origins and 1h had 6.
- 30-session and 90-session horizons were not evaluated because the available daily history cannot support non-overlapping test origins.
- Multi-instrument evidence is 8 real daily instruments and 2 real intraday instruments, not a broad market panel.
- Leakage prevention was reviewed and tested: future bars are filtered before historical MTF decisions; forecasts request `interval="1d"` so intraday rows cannot displace daily history; test slices are chronological and are not used for selection or calibration; forecast performance-snapshot identity includes resolved outcomes and is idempotent.

**Verdict: PASS for implementation, walk-forward discipline, baseline comparison, calibration plumbing and abstention; PARTIAL/FAIL for demonstrated predictive value.** `bootstrap` is the only model with a positive test Brier skill, and its margin is too small to call an edge. The practical baseline remains `random_walk`/`naive_last`.

## 6. Trading strategy comparisons

The catalogue contains conventional strategies across day, swing and long horizons, plus `forecast_assisted`. The MTF layer gates only short-term/day-trading entries. All comparisons below use either the stated 7 bps all-in cost (MTF replay) or explicit fees/slippage (RL evaluation); none is a risk-free backtest.

| Strategy family | Evidence | Trades | Return / result | Statistical reading |
|---|---|---:|---:|---|
| Day momentum | Five-instrument chronological RL comparison | 40 | **+0.2543%** | 95% CI [−0.3960%, +1.1814%], crosses zero; fees ₹312.58 and slippage ₹327.66 |
| Swing trend | Same five-instrument comparison | 8 | −0.4361% | CI [−0.8347%, +0.0071%] |
| Long-horizon proxy | Same comparison | 1 | −0.0910% | One trade; not a long-term strategy evaluation |
| Baseline EMA 1m | Real two-session MTF replay | 11 | −0.7245% | 18.18% win rate; PF 0.0955 |
| Enhanced MTF | Same real replay | 2 | −0.0297% | 50% win rate; PF 0.0712; worse PF than baseline |
| Forecast-assisted | Fixture/real gate | **0** | No trade | Correct abstention; no comparison sample |
| RL default/trading | Ten fresh plus three stored policies | **0** | 0.0000% | No trading skill to compare |

### Day trading

The only positive average was day momentum, but its confidence interval includes zero, its costs were material and it lost on three of five instruments. The MTF-enhanced day path reduced false signals and trade count but lost after costs and had a worse profit factor. The enhanced path is therefore **not demonstrated to be superior**; it is a conservative filter.

### Swing and long-term investing

Swing lost on average in the five-instrument window. The long-horizon proxy made one trade and is not a meaningful long-term sample. No medium/long-term portfolio was gated by MTF in the continuous validation. There is no evidence for a long-term return claim.

### Entry, exit and sizing

- Entry signals are strategy/version-bound; duplicate evaluations replay the same signal.
- Position sizing is policy-driven (the fixture used 10% notional); the RL environment independently caps a position at 25%, gross exposure at 100%, and checks cash and long-only constraints.
- The EXIT path was corrected to size from **held quantity × exit reference price**, not a stale market value. The first oversized exit was correctly refused by NM-10; the corrected exit requested 19.8807 units and filled.
- Risk restrictions that execution cannot enforce cause NM-10 to refuse the order rather than ignore the restriction.
- No strategy has direct access to the wallet or order store. Every actionable proposal passes NM-08 and then the single NM-10 forwarder.

### Forecast-assisted decisions

`forecast_assisted` exists, but the tested forecast had `p_up=0.0`, `p_down=0.0`, `p_sideways=1.0` and `uncalibrated` status; the default gate requires positive predicted return and `p_up >= 0.55`. It correctly made no trade. There is **no forecast-assisted performance comparison**, and no claim that forecasts improve strategy returns.

**Verdict: FAIL for demonstrated net-of-cost trading effectiveness.** The conventional day-momentum result is the only positive sample, and it is not statistically distinguishable from zero. The enhanced and forecast-assisted paths are not proven superior; the RL path did not trade. This is not a claim that every strategy loses forever; it is the honest limit of the available evidence.

## 7. Reinforcement learning evaluation

### Isolation and governance

- The RL environment has no execution, wallet or risk-limit handle. It produces simulated transitions only.
- `automatic_policy_promotion=false`, `activation_available=false` and `production_authority=false`; no activation/promotion route for experimental policies was found.
- Training/validation/test separation is chronological. The test window was scored once with a frozen validation-selected checkpoint and did not feed back into training or reward selection.
- Policy artifacts are versioned and isolated; rollback/archival exists. The evaluated artifacts were local JSON files, not registered production policies.
- The registry vocabulary is `created`, `training`, `trained`, `validation_pending`, `validated`, `tested`, `rejected`, `archived`. There is **no `APPROVED` or `PRODUCTION` state**; `tested` must not be read as authorization.

### Results

- Ten freshly trained policies and three existing tested policies were evaluated on five instruments, with 88–91 test bars each.
- **Every policy made zero trades.** The default-reward family produced 261 `hold` and 177 `no_action` actions; the trading-reward family produced 438 `hold` actions.
- RL default return: **0.0000%**, 0 trades, ₹0 fees, ₹0 slippage, 95% CI [0, 0]. RL trading: the same.
- The default reward accumulated **31.8225** total reward despite zero portfolio return. That is an abstention/reward-specification artifact, not a return claim.
- The cash-drag experiment also produced zero trades after charging −38.13 total reward. The model is 21→24→5 with 653 parameters; inference is 38.6 µs per `q_values` call.
- Reward hacking is confirmed: a no-op can earn positive reward under the default specification, and removing the subsidy makes cash the risk-free optimum. The action space also cannot express “exit all” (fixed 20% exit sizing), and the state omits material information.

### Comparison

Day momentum (+0.2543%) was the only active baseline with positive average return; swing and long-proxy lost. RL returned 0.0% with no trades, so its “zero drawdown” is the absence of a strategy, not risk control. The paired RL-minus-day-momentum difference was −0.2543 pp (CI [−1.1814, +0.3960] pp).

**Verdict: FAIL for measurable trading improvement; PASS for isolation and no-promotion governance.** The current policy is rejected for promotion. Keep automatic promotion disabled and repair reward, action semantics and state before another out-of-sample claim.

## 8. Risk-management verification

### Independent approval and execution authorization

- NM-09 calls NM-08 `assess()` before a proposal can become approved.
- The proposal carries the risk decision id, policy version, state version and restrictions.
- NM-10 re-reads the exact NM-08 decision, checks the proposal is approved, checks `risk_outcome=approved`, checks the decision belongs to the proposal/portfolio/action, checks expiry and state version, and revalidates the **actual order notional**.
- A missing decision store, failed assessment, expired decision, restriction the execution model cannot enforce, stale price, missing liquidity or changed portfolio state fails closed.
- A rejected proposal cannot be executed; duplicate forwarding replays the stored order rather than creating a second one.

### Limits and controls verified

- Position, concentration, sector, cash-reserve, drawdown, volatility, liquidity, turnover and restricted-instrument constraints are policy-driven.
- The fixture’s artificial ₹1,000,000 notional was rejected with seven triggered constraints; no order was created.
- The duplicate forward returned `replayed=true`; order count stayed one.
- The real SQLite write-lock drill produced HTTP 500 `database is locked` with no order, fill, holding, wallet or ledger mutation; the proposal stayed approved and succeeded exactly once after release.
- The RL environment has a 25% position cap and 100% gross cap, but those are simulation constraints; the authoritative risk boundary remains NM-08.
- No strategy, forecast, MTF decision, RL policy, learning service or API route was found with a path that bypasses NM-08/NM-10.

**Verdict: PASS** for the tested risk and execution-authorization paths. This is not a live-risk validation.

## 9. Financial accounting verification

- **NovaCoin accounting:** ledger rows carry `nova_coin` equal to the INR amount; reconciliation compares the NovaCoin balance with the ledger and reports zero difference. The tested run is consistent with **1 NC = ₹1**. This is an accounting-equivalence check, not a market-price claim.
- **Isolated primary DB:** 17 wallets, 40 entries, 20 transactions; ledger sum `0.00`; zero issues; balances match summed entries; no negative user wallet.
- **Fixture paper lifecycle:** ₹20,000 starting capital; buy 19.8807 units at ₹100.69 with ₹0.78 fees and ₹1.79 slippage; exit 19.8807 units at ₹89.92 with ₹0.70 fees and ₹1.59 slippage; final equity ₹19,784.40; realized P&L −₹215.61 (−1.08%).
- **Reconciliation:** portfolio reconcile `ok=true` before/after exit and after restart; wallet stored balance equals ledger with difference ₹0.00; 52 entries/26 transactions after exit; realized book/history both −₹215.61.
- **Valuation:** cash, invested capital, unrealized and realized P&L, total equity and external flows are separated. Deposits are not counted as investment return. Performance ratios are withheld when fewer than 20 observations exist.
- **Persistence:** primary copy backup/restore preserved 127 tables and 62,408 rows; integrity `ok`. A deliberately corrupted backup was detected by `PRAGMA integrity_check`.
- The fixture P&L is **not** market evidence. It is a deterministic acceptance path that proves the accounting mechanics and fail-closed behavior.

**Verdict: PASS** for tested paper accounting, ledger consistency, valuation reconciliation and database recovery.

## 10. Paper-trading evidence

### What was actually run

- **Real daily history:** 149 instruments through 2026-11-19, used for forecasts and RL/strategy evaluation.
- **Real intraday history:** only three instruments with 1-minute bars, 66.67% of a 375-minute session; the latest ADANIENT session had 14 gaps >120 seconds. There are **no native 5-minute bars**; MTF five-minute buckets are derived from five contiguous 1-minute bars.
- **Real MTF replay:** two historical sessions, 165 decisions, 10 BUY_CANDIDATE, 0 SELL_CANDIDATE, 12 WAIT, 0 REJECT, 143 HOLD; two enhanced simulated trades; 93.94% abstention.
- **Resolved real forecasts:** 5/5, all `next_session`, 2/5 directional, mean Brier 0.6839, 3/5 skill-gate abstentions, all uncalibrated.
- **Fixture paper lifecycle:** one deterministic synthetic instrument, real strategy engine, NM-08, NM-10, ledger, exit and restart; fill/duplicate/oversized/lock drills passed.
- **Market-open attempt:** the stored-data smoke test produced one real signal, but MTF rejected it because the stored current-session 1m/5m series were incomplete; no order was forwarded. This is a historical replay, not a live open.

### What was not run

- No complete live-market session. The market was closed for the validation window; the next open was 2026-11-23.
- No sustained multi-session paper run, no unattended scheduler loop, no 24/7 process, no live provider outage and no live broker.
- No native 5m/15m/30m/1h history; the 20m/1h forecast samples are too small.
- No multi-hour RSS/CPU/DB-growth/queue-depth soak.
- No full OS-level kill during a live paper lifecycle; restart/recovery is tested at store/harness level.
- No production/user database was mutated. All drills used copies or temporary stores.

The runtime’s own status is explicit: `trading_implemented=false`, `orders_possible=false`, `exit_supervision_implemented=false`, `learning_promotion_implemented=false`, and the runtime was degraded/stopped. **The evidence supports controlled paper drills, not a claim of sustained real-market paper operation.**

## 11. Security findings

### Present controls

- Bearer authentication with constant-time comparison; a separate autonomy-operator credential.
- Loopback-only admin credential; no backend built-in admin default.
- Trusted-host and OriginGuard checks; exact-origin CORS with credentials disabled; security headers (`nosniff`, `DENY`, referrer policy, restrictive CSP).
- Parameterized SQL; no unsafe `pickle`/YAML/`eval`/`exec` in the NovaMarket surface.
- Provider error bodies are normalized; wallet logs do not record tokens or payloads; `.env` is untracked and gitignored.
- The launcher binds uvicorn and Vite to `127.0.0.1`, not `0.0.0.0`.

### Severity classification

| Severity | Finding | Status / impact |
|---|---|---|
| **High on a shared host** | Authentication is off when no API token is configured; any local process is principal `local`. | Deliberate local-first default; loopback reduces remote exposure, not local-process exposure. Set a private token. |
| **High for financial durability** | No scheduled backup, point-in-time recovery or replication for the single SQLite financial store. | Manual backup/restore and corruption detection pass; loss of the disk/process remains a single point of failure. |
| **Medium** | `start.ps1` defaults `NOVACONTROL_ADMIN_TOKEN` to a predictable value. | Loopback-only, but not a secret against another local process; the same key can satisfy operator checks on loopback. |
| **Medium** | Several API 500 handlers return `str(exc)`. | Can disclose paths/SQLite detail; inspected provider adapters scrub bodies, but the API is not uniformly generic. |
| **Medium** | SSE accepts `?token=`. | EventSource cannot send an Authorization header; query tokens can appear in history/proxy logs. Use a short-lived ticket before any remote deployment. |
| **Medium** | No dependency-vulnerability scan (`pip-audit`/`safety` absent). | No vulnerability result is claimed; optional heavy dependencies are not in the base install. |
| **Medium** | Audit-log rename failed twice on the OneDrive-synced workspace (`WinError 5`). | Financial state was unaffected; audit records were not durable. Keep audit logs outside a synchronised consumer folder and alert on write failure. |
| **Low** | No unified CPU/RSS/queue-depth metrics; no log rotation/correlation-id trace; execution journal has no retention policy. | Operational visibility and long-run hygiene are incomplete. |

**Verdict: PARTIAL.** No credential leak or unauthorized trade was observed, but the local auth default, predictable launcher key, error-detail leakage, SSE token and missing dependency scan prevent a security PASS.

## 12. Reliability and recovery results

| Area | Evidence | Result |
|---|---|---|
| Provider retry/quota/circuit | Bounded retry/backoff, circuit breaker, daily quota, normalized failures | PASS at contract/test level; no live outage |
| Scheduler durability | Atomic claims, leases, checkpoint recovery, duplicate-tick guard, target-time handling, session/calendar fail-closed | PASS |
| Execution recovery | Proposal idempotency, risk revalidation, reservations, durable fill journal, compensation, restart recovery | PASS |
| Database integrity | 14 non-empty SQLite files `integrity_check=ok`; 0 declared-FK violations; 0-byte adaptive placeholder identified | PASS with one placeholder |
| Backup/restore | SQLite backup API: 127 tables / 62,408 rows preserved; integrity `ok` | PASS |
| Corruption detection | Deliberately corrupted backup failed `integrity_check` | PASS |
| Write lock | Real SQLite `BEGIN IMMEDIATE` lock: deposit failed closed at 5.585 s with no mutation; paper forward recovered exactly once | PASS |
| Restart | Store reopen, scheduler checkpoint and paper lifecycle restart checks | PASS at store/harness level |
| Unexpected shutdown | No OS-level kill during a live paper lifecycle | PARTIAL |
| Live network/provider partition | No live partition was injected | PARTIAL |
| Audit durability | Two OneDrive rename failures | Open limitation |
| Long-run resource stability | No multi-hour soak; prior single-process crash near LSTM was not reproduced on 23 November | PARTIAL |

### Single-process regression-run incident

The historical `pytest tests/test_novamarket_*.py -q` attempt collected 1,971 tests before the final regression test was added and exited **1** at roughly 35% with no pytest failure summary. The last collected block was LSTM; `tests/test_novamarket_lstm.py` alone passed 24/24. The suite was rerun in two ordered file groups, and both groups passed. This is recorded as an unresolved test-process/resource-stability limitation, not as a green single-process run.

**23 November 2026 rerun.** The current tree collected 1,972 tests. Group A (the first 21 files through `test_novamarket_lstm.py`) passed **697 tests and 390 subtests** in 349.67 s. Group B (the remaining 35 files) produced **1,272 passed, 1 failed, 2 skipped and 57 subtests passed** in 231.97 s. The single-process pass completed all 1,972 tests in 514.43 s at `01:54:25Z` without the historical LSTM crash, but it had the same one failure: `tests/test_novamarket_scheduler.py::test_api_exposes_cycle_scheduler_and_missed_surfaces` expected HTTP 200 and received 422 because its fixture selected 24 November 2026, a verified NSE holiday. The test fails identically in isolation. That is a date-sensitive test fixture, not evidence of an execution or financial-control defect.

Because the single-process pass completed once, the historical crash was not reproduced; one completion does not establish runner stability and the suite is not green. Evidence is recorded in `tmp/ops_audit/current_novamarket_suite_record.json` with the logs `pytest_current_group_a.log`, `pytest_current_group_b.log`, `pytest_current_full_single_process.log` and `pytest_current_scheduler_isolated.log`.

**Verdict: PARTIAL for operational resilience.** Core safety controls, database recovery and controlled failure drills pass; unattended long-run behavior and live-provider recovery are not proven.

## 13. Performance benchmarks

These measurements come from the cited validation work; they were not all re-run in this final assessment.

| Measurement | Result |
|---|---|
| NM-06 inference, 320 daily closes, 30 calls | `zero_return` 0.049 ms mean / 0.079 p95; `random_walk` 0.052 / 0.053; `bootstrap` 1.117 / 1.211; max 1.527 ms |
| PI-05 inference, 320 daily closes | `naive_last` 0.07 ms; `holt` 1.04; `ses` 1.25; `sma` 1.28; `historical_return` 1.87; `random_walk` 1.99; `ema` 2.22; `wma` 2.33; `arima` 10.70 ms |
| MTF cold decision | 0.6126 ms mean / 0.6980 p95 / 0.7195 max (16 decisions) |
| MTF warm cache | 0.2308 ms mean / 0.3624 p95 |
| Baseline 1m EMA decision | 0.0043 ms |
| Full strategy-path MTF latency | 8.398 ms mean / 11.411 ms p95 |
| RL inference | 38.6 µs per `q_values` call (20,000 calls) |
| Full-suite duration | 23 Nov 2026: Group A 349.67 s; Group B 231.97 s; single-process 514.43 s |
| Uvicorn RSS (prior) | approximately 231–288 MB per process |
| Database sizes | primary 60.1 MB / 127 tables / 62,408 rows; RL 35.8 MB / 25 tables / 5,217 rows |
| Replay peak memory | 3.00 MB traced for 165 decisions |
| Audit run | 51.9 s for 123 instruments / 24 NM-06 cases / 10 PI-05 cases; ~118 MB working set |

The MTF hierarchy adds roughly 0.6 ms of in-process work per cold decision and 8.4 ms on the full strategy path in the measured environment. The baseline-to-enhanced difference is substantial (about 46× in the published real replay) but remains below the 12 ms p95 observed. This is **not** a production load test. There is no multi-hour memory-growth, queue-depth, API-throughput or sustained-writer benchmark.

**Verdict: PASS for short-run functional latency; PARTIAL for performance assurance.** No performance regression was observed in the tested paths, but endurance and concurrency remain unmeasured.

## 14. Remaining defects

### Fixed and verified in the validation cycle

1. **Nested portfolio transactions** issued a second `BEGIN IMMEDIATE`; now use savepoints with inner rollback isolation. Regression tests pass.
2. **Signal without a positive reference price** raised `TypeError` in proposal sizing; now fails closed with `StrategyValidationError`.
3. **Historical MTF look-ahead:** stored bars observed after the decision instant were visible; `_series` now filters `observed_at <= moment`. Regression test passes.
4. **Daily forecast query returned intraday rows** on instruments with recent 1-minute bars; the engine now requests `interval="1d"`. Regression test passes.
5. **Duplicate forecast performance snapshots** raised `UNIQUE constraint failed`; snapshot identity includes the resolved-outcome set and writes are idempotent.
6. **EXIT sizing used stale market value** and requested more units than held; the corrected path uses held quantity × exit reference price, and the oversized first exit was correctly refused by NM-10.

### Open defects and operational gaps

1. **Single-process full-suite instability.** The historical attempt exited 1 near the LSTM block with no failure summary; the two-group run is green. Root cause not established; this is a test-process/resource limitation.
2. **Audit-log durability on the OneDrive workspace.** Two audit-file renames failed with `WinError 5`; financial state continued correctly, but those audit records were lost. Move audit logs outside the synchronised folder and alert on failure.
3. **0-byte adaptive store.** `novamarket_adaptive.sqlite3` is an unopened placeholder, not a populated store. It is not counted as financial evidence.
4. **No automatic backup/retention.** The backup API works, but no scheduled job, retention policy or point-in-time recovery exists; the execution journal and audit logs also lack retention.
5. **No true single-process green run.** The split groups cover every test, but the one-process runner has not been proven reliable on this host.

**No unresolved critical defect was found in the tested financial, risk, execution, ledger or API-contract paths.** The open items are test-runner/operations gaps, not evidence of an unauthorized trade or ledger corruption.

## 15. Known limitations

1. **No sustained live-market paper trading.** Only two historical real-data sessions and one deterministic fixture lifecycle were covered. The market was closed; the next open was 2026-11-23.
2. **No live provider or broker.** There is no live broker adapter, live order route, live market session or regulatory/operational approval. This is structural, not a missing test.
3. **Sparse intraday evidence.** Three instruments have 1-minute history at 66.67% of a session; there are no native 5-minute bars; 20m/1h forecast origins are tiny.
4. **Small samples.** Two enhanced MTF trades, 11 baseline trades, 5 resolved forecasts and 5 RL instruments cannot establish superiority, calibration or profitability.
5. **No demonstrated forecast edge.** Best test Brier skill is +3.66% for `bootstrap` versus `random_walk`, within noise; the real continuous-paper mean Brier is worse than uniform.
6. **Calibration is off by default.** Four accepted calibrators is not enough to apply broadly; one test case regressed.
7. **RL is degenerate.** All evaluated policies made zero trades; the positive default reward is an abstention artifact. No measurable RL improvement exists.
8. **Forecast-assisted trading is unproven.** The correct gate produced zero trades; no performance comparison exists.
9. **No multi-hour soak, load test, OS-level crash during a live cycle or live network partition.**
10. **No dependency-vulnerability scan and no `ruff` result.** `pip-audit`/`safety` and `ruff` are not installed; no result is claimed.
11. **Single-node SQLite and one process.** No replication, failover or multi-writer throughput; process-local locks plus SQLite are the current boundary.
12. **2027 exchange calendar is not verified.** Session evaluation fails closed outside coverage, but 2027 scheduling is not production-verified.
13. **Audit and journal retention are undefined.** The system retains financial evidence indefinitely and has no pruning/rotation policy.
14. **No statistical significance test for strategy returns.** Confidence intervals are wide and mostly cross zero; no paired loss-difference test was implemented for forecast selection.

## 16. Recommended operational restrictions

1. **Paper only.** Keep `trading_implemented=false`, no live broker, no live-order route and no real-money credentials. Do not treat any PASS here as live authorization.
2. **Keep the API loopback-only.** Set private `NOVACONTROL_API_TOKEN` and `NOVACONTROL_AUTONOMY_OPERATOR_TOKEN`; replace the launcher’s predictable admin key before use on a shared PC.
3. **Do not expose the API or SSE remotely.** The query-token EventSource pattern is unsuitable for a remote deployment; use a short-lived ticket and TLS if that changes.
4. **Keep authentication on.** Do not run the shared/local API with authentication disabled when other local processes or users are present.
5. **Keep RL and adaptive promotion manual.** `automatic_policy_promotion` must remain false; no policy activation without a separate logged human review. Do not treat `tested` as `approved`.
6. **Do not tune on the current small sample.** No strategy, MTF threshold, calibration or selection-margin change should be justified by two sessions or five forecasts.
7. **Preserve data provenance.** Never replace a missing/stale real quote with a synthetic value; fail closed or abstain. Keep fixture results labelled as fixtures.
8. **Back up before every session and before any schema change.** Store backups and audit logs outside the OneDrive-synchronised folder; test restore, not only backup.
9. **Run controlled, attended paper drills first.** Record real provider status, data freshness, MTF abstentions, risk decisions, fills and reconciliation. Do not call a historical replay a live session.
10. **Do not run unattended continuous autonomy** until the autonomy status reports the trading/exit/learning authorities as implemented and a multi-session soak has passed.
11. **Do not schedule 2027 sessions** until the exchange calendar coverage is verified.
12. **Obtain more evidence before any effectiveness claim:** at least 2–3 years of fresh daily history for 20–30 instruments, native 5m/15m/30m/1h data, multiple folds/instruments, transaction costs and paired significance tests.

## 17. Final readiness verdict

### Readiness scorecard

| Area | Rating | Basis |
|---|---|---|
| **A. Original PI-01–PI-24 implementation** | **PASS** | All 24 reports present; implementation/tests/API surfaces exist; no PI-25 |
| **B. Software integration** | **PARTIAL** | Core paper chain is wired and tested; feature engineering is not a mandatory NM-06 input and analytical branches remain parallel |
| **C. Multi-timeframe intelligence** | **PASS** | 20m/5m/1m analyzer, conflict matrix, data-integrity rules, 32 tests and strategy gate verified; effectiveness not claimed |
| **D. Forecasting intelligence** | **PARTIAL** | 11 estimators, six probabilistic models, walk-forward, gate, calibration and abstention implemented; no robust out-of-sample edge |
| **E. Trading strategy effectiveness** | **FAIL** | No demonstrated positive net-of-cost edge; enhanced MTF and most conventional samples lost; day momentum CI crosses zero |
| **F. Reinforcement learning** | **FAIL** | Zero trades by all 10 fresh and 3 stored policies; no measurable improvement; reward hacking confirmed |
| **G. Risk management** | **PASS** | Independent NM-08, execution revalidation, limits, fail-closed restrictions and duplicate prevention verified |
| **H. Financial accounting** | **PASS** | NovaCoin 1:1, ledger reconciliation, valuation, realized/unrealized P&L and backup/restore verified |
| **I. Paper execution** | **PASS** | Single NM-10 gateway, idempotency, reservations, fill journal, duplicate and lock drills verified |
| **J. Market-data reliability** | **PARTIAL** | Provider controls and fail-closed quality checks pass; no live outage, sparse intraday coverage and no native 5m |
| **K. Security and operational resilience** | **PARTIAL** | Auth/origin/headers/SQL controls present; auth default, predictable local key, raw 500s, SSE token, no dependency scan, audit-log failure |
| **L. Sustained paper-trading readiness** | **PARTIAL** | Controlled historical replay and fixture chain pass; no repeated real-market session, continuous scheduler or multi-hour soak |
| **M. Live-trading readiness** | **BLOCKED** | No live data, broker, live paper session, production deployment or real-money authorization |

No overall percentage is calculated. The ratings intentionally expose the weak areas instead of averaging them away.

### Level 1 — Software readiness

**PASS (conditional).** The implementation is internally consistent on the tested paths: ordered Group A passes all 697 tests, ordered Group B passes 1,272 with one date-sensitive scheduler-fixture failure, type checking is clean, the API contract is synchronized at 935 routes, database integrity/recovery drills pass, and the paper execution/ledger/risk boundaries are enforced. The historical single-process crash was not reproduced, but the suite is not green and runner stability remains unproven; that unresolved limitation and the audit-log rename failure prevent an unconditional operational PASS, though neither is evidence of a financial-control failure.

### Level 2 — Paper-trading readiness

**PARTIAL.** NovaMarket can execute a controlled paper chain with real strategy/risk/execution/accounting components, including a real SQLite lock failure and recovery. It has **not** demonstrated sustained paper trading with real market data over repeated sessions: no live session, no continuous scheduler, no native 5-minute history, only two real intraday sessions, five resolved forecasts and two enhanced simulated trades. The current evidence supports attended paper drills, not unattended continuous paper operation.

### Level 3 — Live-trading readiness

**BLOCKED.** There is no live broker, live data path, live paper session, production deployment, regulatory/operational approval or real-money risk evidence. The paper-only boundary is structural. No amount of historical replay or fixture testing in this assessment authorizes live trading.

### Bottom line

NovaMarket’s **engineering and accounting foundations are ready for controlled paper drills**, and the risk/execution boundary is credible. Its **predictive and strategy evidence is not ready for a performance claim**, its **RL policy is rejected**, and its **operational evidence is not sufficient for sustained unattended paper trading**. Live trading remains blocked.

No PI-25 was created. No feature was added. No live trading was enabled. No RL policy was promoted. No commit or push was performed.
