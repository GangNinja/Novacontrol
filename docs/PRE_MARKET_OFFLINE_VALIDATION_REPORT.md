# NovaMarket — Pre-Market Offline Simulation and Full Trading Pipeline Validation

**Date:** 22 November 2026  
**Repository:** `C:\Users\NARASIMHA\OneDrive\Desktop\NovaControl`  
**Worktree:** `novamarket`  
**Branch:** `feature/novamarket`  
**HEAD:** `9f0aec8 feat(novamarket): close PI-01–PI-24 with fixes, verification and refreshed docs`  
**Mode:** offline validation only. The market was closed. No live provider session, live order, broker connection, real-money route or RL policy promotion was used.

## Evidence labels

Every result below carries one of these labels:

| Label | Meaning |
|---|---|
| **ACTUAL DATA (HISTORICAL)** | Stored observations previously acquired from a market provider. They are not a live session. |
| **HISTORICAL REPLAY** | A chronological replay over stored actual data. No future bar is visible to a decision. |
| **SIMULATION** | Synthetic bars, quotes or portfolio states produced to exercise a rule. Never market evidence. |
| **FIXTURE** | A deterministic synthetic lifecycle through real API/service objects. Never market evidence. |
| **FAULT INJECTION** | A controlled failure applied to a temporary or copied database/store. |
| **LIVE** | Actual current-market execution. **Not performed in this validation.** |

The market was closed for the validation window. The next regular NSE/BSE session is **Monday, 23 November 2026, 09:15–15:30 Asia/Kolkata**. Nothing in this report is a live-market validation or a profitability claim.

---

## 1. Test environment

| Item | Value |
|---|---|
| OS / shell | Windows, Git Bash |
| Python | Project `.venv` (`../.venv/Scripts/python.exe`) |
| Worktree | `novamarket` |
| Branch / HEAD | `feature/novamarket` / `9f0aec8` |
| NovaMarket tests collected | **1,972** |
| API routes | **935**, `generate_api_reference.py --check` in sync |
| Type checking | `mypy src`: **Success, 724 source files** |
| Source financial DB | `data/novamarket.sqlite3` (read-only for this validation) |
| Validation work | Copied stores under `tmp/continuous_paper_validation/` and `tmp/pre_market_offline/` |
| Production/user financial data | Not mutated. All actual-data reads used SQLite `mode=ro`; all lifecycle work used copied stores. |

The validation did **not** connect a real broker, enable live trading, create PI-25, modify the 24-PI roadmap, promote an RL policy, or commit/push.

---

## 2. Historical data provenance

### Actual stored data inventory (ACTUAL DATA — HISTORICAL)

Read directly from `data/novamarket.sqlite3` with `mode=ro` on 22 November 2026. The stored observations were previously acquired from Yahoo (unofficial/delayed); they are not a licensed real-time feed and they are not a live session:

| Series | Coverage |
|---|---|
| Daily bars | **150 instruments**, **31,974 bars**, observed **2025-10-29T03:45Z** to **2026-11-20T09:00Z** |
| 1-minute bars | **3 instruments**, **2,417 bars**, **10 instrument-days**, observed **2026-11-05T05:20Z** to **2026-11-20T09:58Z** |
| Native 5-minute bars | **0** |
| 5-minute series used in replay | **Derived only** from five contiguous real 1-minute observations |
| 20-minute context | **Derived only** from completed 5-minute buckets |
| Intraday data quality | **1,411 of 2,417 1-minute rows are not `valid`** (stale/suspect/other non-valid quality) |

The intraday coverage is deliberately reported as sparse:

- 2026-11-05: `XBOM:ADANIENT` 250 bars, `XNSE:ADANIENT` 250 bars; both rows are non-`valid` quality in the stored data.
- 2026-11-06: `XBOM:ADANIENT` 224/250 valid; `XBOM:RELIANCE` 239/250 valid.
- 2026-11-09: 220/250 valid.
- 2026-11-11: 83/318 valid.
- 2026-11-12: 36 bars, 0 valid.
- 2026-11-18: 250 bars, 0 valid.
- 2026-11-19: 313 bars, 0 valid.
- 2026-11-20: 240/250 valid.

The latest full intraday day, 2026-11-20, has 250 one-minute bars for `XBOM:ADANIENT`, or **66.67% of a 375-minute session**. This is not a complete trading day and is not a native 5-minute series.

The actual-data replay used the two complete contiguous instrument-days available for the evaluator:

- `XNSE:ADANIENT`, 250 one-minute bars, 2026-11-05 05:37Z–09:58Z.
- `XBOM:RELIANCE`, 250 one-minute bars, 2026-11-06 05:36Z–10:00Z.

**Provenance bottom line:** the daily history is broad but the intraday evidence is thin, gappy and partly non-valid. The system correctly treats missing/stale/partial intraday data as a reason to abstain; it does not synthesize a market bar.

### What was not used

- No live Yahoo/NSE/BSE websocket or streaming session.
- No current quote was substituted for a stored close.
- No synthetic bar was labelled as actual market data.
- No fixture result was treated as evidence of live profitability.

---

## 3. Simulated market conditions

The synthetic scenarios below were run through the **real `MultiTimeframeAnalyzer`** with a test market adapter. They are **SIMULATION**, not market evidence. The exact output is in `tmp/pre_market_offline/scenarios.json`.

| # | Scenario | MTF result | Decision behaviour |
|---|---|---|---|
| 1 | Continuous upward trend | `buy_candidate`, `aligned`, confidence `0.70` | 20m bullish, 5m confirmed, 1m `entry_long` |
| 2 | Continuous downward trend | `sell_candidate`, `aligned`, confidence `0.70` | 20m bearish, 5m confirmed, 1m `entry_short` |
| 3 | Sideways market | `hold`, `partial`, confidence not directional | Neutral context; weak setup; timing `wait` |
| 4 | Sudden price crash (−10% last 1m bar) | `wait_for_confirmation`, `partial` | “sudden one-minute move; timing refuses to chase it” |
| 5 | Sudden upward movement (+8% last 1m bar) | `wait_for_confirmation`, `partial` | Same anti-chase rule |
| 6 | High volatility | `wait_for_confirmation`, `partial` | Neutral/high-volatility context; “20m volatility is too high for a timed entry” |
| 7 | Low liquidity / low volume | `requires_review` in the NM-08 policy probe | `min_liquidity` review breach at ₹10,000 vs ₹25,000 minimum |
| 8 | Missing 1-minute data | `wait_for_confirmation`, `insufficient_data` | `missing: ["1m"]`; needs 15 bars, has 0 |
| 9 | Stale 5-minute data | `wait_for_confirmation`, `insufficient_data` | 5m older than 900s and 20m older than 1800s; “no new entry is timed” |
| 10 | Conflicting 20m/5m/1m signals | `wait_for_confirmation`, `partial` | Bullish context/setup but 1m `entry_short`; timing does not confirm setup |

The low-liquidity row is a **separate SIMULATION policy probe** (`tmp/pre_market_offline/liquidity.json`), because the MTF analyzer does not consume volume as a directional signal. The result was:

- Liquidity: ₹10,000.
- Policy minimum: ₹25,000.
- Outcome: **`requires_review`**, breach `min_liquidity` (severity `review`).

The earlier six-scenario backtest (`tmp/audit/pre_market_scenarios.log`, EXIT 0) is also **SIMULATION** and is retained only as a strategy-path check:

| Scenario | Orders | Filled | Closed | Return | Max drawdown |
|---|---:|---:|---:|---:|---:|
| trending_up | 0 | 0 | 0 | 0% | 0% |
| trending_down | 0 | 0 | 0 | 0% | 0% |
| sideways | 10 | 10 | 5 | −1.388% | −1.164% |
| high_volatility | 5 | 5 | 2 | −1.662% | −5.135% |
| sudden_move | 1 | 1 | 0 | approximately −0.001% | not closed |
| low_volume | 0 | 0 | 0 | 0% | 0% |

**Important limitation:** the zero-order trend and low-volume backtest rows do not prove that the strategies reject those conditions. That harness’s `trend_sma` did not trigger on the seeded series. The direct MTF and NM-08 probes above are the evidence for the abstention rules.

---

## 4. Pipeline results

### 4.1 Real chronological replay (ACTUAL DATA — HISTORICAL)

Source: `tmp/continuous_paper_validation/evidence.json`, section `real_multitimeframe`, generated 22 November 2026.

| Metric | Baseline 1m EMA | Enhanced 20m/5m/1m hierarchy |
|---|---:|---:|
| Decisions | 165 | 165 |
| Signals | 11 | 10 |
| Trades | 11 | 2 |
| Rejection rate | 0.00% | **93.94%** |
| Win rate | 18.18% | 50.00% |
| Mean return per trade | −0.0661% | −0.0149% |
| Cumulative return | −0.7245% | −0.0297% |
| Maximum drawdown | −0.7983% | −0.0320% |
| Profit factor | 0.0955 | 0.0712 |
| False signals | 9 | 1 |
| Mean latency | 0.2657 ms | 18.2906 ms |
| Recorded p95 latency | 0.3475 ms | 16.0587 ms |

Per instrument:

| Instrument | 1m bars | Derived 5m bars | Decisions | Candidates | Trades |
|---|---:|---:|---:|---:|---:|
| `XNSE:ADANIENT` | 250 | 49 | 83 | 0 | 0 |
| `XBOM:RELIANCE` | 250 | 49 | 82 | 10 | 2 |

Outcome mix: **143 hold**, **10 buy_candidate**, **12 wait_for_confirmation**.

The test split (later chronological slice) was worse for both paths: baseline 4 trades, −0.1165% cumulative; enhanced 1 trade, −0.0320%. This is a **very small sample** and does not establish strategy quality either way.

The enhanced path made fewer trades and rejected 93.94% of signals, which is consistent with the intended conservative gate, but it still lost after 7 bps assumed costs. **Neither path demonstrates a net edge.**

### 4.2 Stage-by-stage result

| Stage | Evidence class | Result |
|---|---|---|
| Market-data ingestion | ACTUAL DATA (HISTORICAL) | 150 daily instruments and 3 intraday instruments loaded read-only; sparse/non-valid intraday rows recorded, not repaired |
| 20m context | HISTORICAL REPLAY | Built from completed 5m buckets; 19 completed buckets in the fixture, 2 incomplete excluded |
| 5m setup | HISTORICAL REPLAY / FIXTURE | Derived from contiguous 1m; no native 5m source |
| 1m timing | HISTORICAL REPLAY / FIXTURE | Used completed 1m bars; stale/missing layers cause WAIT |
| Forecast generation | ACTUAL DATA (HISTORICAL) | Five chronological train/generate/resolve runs; 5 resolved outcomes |
| Forecast skill gate | ACTUAL DATA (HISTORICAL) | ADANIENT retained `random_walk` and abstained (negative skill); RELIANCE `bootstrap` passed its test margin |
| Strategy selection | FIXTURE | `trend_sma` BUY and exit signals generated through the real strategy engine |
| Asset selection | FIXTURE / PARTIAL | One fixture instrument in the end-to-end lifecycle; multi-instrument selection is not exercised end to end |
| Position sizing | FIXTURE | ₹2,000 notional, 19.8807 units at ₹100.60 reference; 10% portfolio weight |
| NM-08 approval | FIXTURE | Entry approved; oversized entry rejected; low-liquidity policy probe returned `requires_review` |
| NM-10 paper execution | FIXTURE | BUY and SELL filled through the paper-only gateway; no broker path |
| Portfolio updates | FIXTURE | Holding created, marked, then closed; holdings empty after exit |
| Ledger accounting | FIXTURE | Wallet/ledger reconciled; ledger sum ₹0.00 |
| Performance evaluation | FIXTURE | 4 observations; risk-adjusted ratios withheld until ≥20 observations |

### 4.3 Forecast results (ACTUAL DATA — HISTORICAL)

Five walk-forward runs were made on stored daily bars, each with a chronological cutoff:

| Instrument | Cutoff | Model | Baseline | Selected test skill | Abstained |
|---|---|---|---|---:|---|
| `XBOM:ADANIENT` | 2026-11-04 | `random_walk` | `random_walk` | −0.0238 | True |
| `XBOM:ADANIENT` | 2026-11-10 | `random_walk` | `random_walk` | −0.0189 | True |
| `XBOM:ADANIENT` | 2026-11-18 | `random_walk` | `random_walk` | −0.0155 | True |
| `XNSE:RELIANCE` | 2026-11-04 | `bootstrap` | `random_walk` | +0.0398 | False |
| `XNSE:RELIANCE` | 2026-11-10 | `bootstrap` | `random_walk` | +0.0474 | False |

Resolved outcomes: **5**, mean Brier **0.6839**, directional correctness **2/5 (40%)**. The sample warning is explicit: fewer than 20 resolved forecasts, so directional accuracy and Brier differences are not statistically reliable. The gate is conservative, but it has not demonstrated a forecast edge.

### 4.4 Look-ahead prevention

- The real replay advances `now` one completed 1-minute bar at a time; the analyzer receives only bars with `observed_at <= now`.
- The analyzer filters future bars even when they are already stored; the multi-timeframe tests include a future-bar exclusion case.
- The strategy uses the stored daily close series and a forecast cutoff; no test-time information is used in the decision.
- The fixture lifecycle seeds a future daily bar only for the **exit** path; that bar is intentionally a post-entry state change, not information available at entry.
- No claim is made that this proves the absence of every possible leakage path in every model; it is the tested pipeline’s chronological contract.

---

## 5. Paper-trading results

### 5.1 Full fixture lifecycle (FIXTURE, not market data)

Source: `tmp/continuous_paper_validation/paper_lifecycle.json`, generated 22 November 2026, EXIT 0. The copied data store is under `tmp/continuous_paper_validation/paper_work/`.

| Step | Result |
|---|---|
| BUY candidate | `trend_sma` BUY; MTF `buy_candidate`, aligned, confidence 0.70 |
| NM-08 entry | **APPROVED**; 1 position, invested 10%, cash 90%, drawdown 0%, volatility 20.25%; no constraint breached |
| Paper order | `ord_54ab5adc5aaedcde9f54ca4f`, BUY 19.8807 units, market, paper-only |
| Fill | 19.8807 @ **₹100.68** vs reference ₹100.60; slippage ₹1.59 (0.0795%); fees ₹0.78; net cash ₹2,002.37 |
| Position | Average cost ₹100.72; cost basis ₹2,002.38; unrealized P&L **−₹2.38** |
| Valuation after fill | Cash ₹17,997.63; invested ₹2,000.00; total ₹19,997.63; return −0.01% |
| Database-lock drill | Forward returned HTTP 500 “database is locked”; no order/fill/holding/wallet/ledger change; proposal stayed approved |
| Recovery after lock release | Retry returned 200; exactly one new order and one fill; proposal forwarded |
| Duplicate forward | HTTP 200 with `replayed: true` and `execution_replayed: true`; order count **1 → 1** |
| Oversized risk request | **REJECTED**; seven breaches including position weight, sector weight, invested fraction, cash minimum, volatility, aggregate sector and turnover; order count **1 → 1** |
| Exit signal | `trend_sma` SELL/exit after opposite crossover; MTF is entry-gated only, so it did not veto the exit |
| NM-08 exit | **APPROVED** |
| Exit order | SELL 19.8807 units, market, paper-only |
| Exit fill | 19.8807 @ **₹100.52** vs reference ₹100.60; slippage ₹1.59 (0.0795%); fees ₹0.78; net cash ₹1,997.63 |
| Holdings after exit | **0** |
| Realized P&L | **−₹4.76** |
| Valuation after exit | Cash and total value **₹19,995.26**; return −0.02%; unrealized ₹0.00 |
| Costs | Total fees **₹1.56**; total slippage **₹3.18** |
| Portfolio reconciliation | `ok: true`; funded ₹20,000.00; realized book −₹4.76; realized history −₹4.76; no discrepancies |
| Wallet reconciliation | Stored = ledger = ₹19,995.26; difference ₹0.00; 52 entries, 26 transactions; ledger sum ₹0.00 |
| Restart | Reopened copied stores; 2 orders and 0 holdings preserved; valuation, realized P&L, wallet and reconciliation unchanged |
| Deposit treatment | ₹20,000 funding is reported as an external flow, not as trading return; the trading result is the −₹4.76 net loss |

The exit was processed while the current MTF decision was still `buy_candidate` because **MTF applies to short-term entries only**, not to exits. That is documented scope, not a contradiction in the test. The exit still required strategy approval and NM-08 approval; NM-10 executed it as paper.

### 5.2 BUY, SELL, HOLD and WAIT

| Decision | Evidence | Result |
|---|---|---|
| BUY candidate | HISTORICAL REPLAY + FIXTURE | 10 real candidates; one fixture BUY approved and filled |
| SELL candidate | SIMULATION + FIXTURE | Down-trend scenario produced `sell_candidate`; fixture exit produced and filled a SELL |
| HOLD | HISTORICAL REPLAY + SIMULATION | 143 of 165 real replay decisions were `hold`; sideways simulation was `hold` |
| WAIT | HISTORICAL REPLAY + SIMULATION | 12 real replay decisions were `wait_for_confirmation`; crash, spike, high-vol, missing-1m, stale-5m and conflict scenarios all waited |
| Risk rejection | FIXTURE | Oversized request rejected with seven identified breaches; no order created |
| Risk review | SIMULATION | Low-liquidity probe returned `requires_review`, not an order |

### 5.3 What the paper result does and does not show

It shows that, on a deterministic fixture, the current code can carry a proposal through MTF, NM-08, NM-10, portfolio, wallet and ledger, reject unsafe size, refuse duplicates, recover from a locked database, exit the position and preserve the result across restart.

It does **not** show live-market readiness, profitability, fill availability, provider reliability or multi-session stability. The fixture is not market data, and the ₹4.76 loss is a cost-accounting result, not a strategy conclusion.

---

## 6. RL validation

### 6.1 Governance and interface

| Requirement | Result | Evidence |
|---|---|---|
| Reproducible evaluation harness | PASS | `evaluation/rl_strategy/run_rl_evaluation.py` and `evaluation/rl_strategy/run_cash_drag_experiment.py` evaluate out-of-sample through `TrainedPolicy.select` and write their compact extracts directly; `evaluation/rl_strategy/evaluate_trained_adapters.py` also regenerates both tracked extracts from the saved policy artifacts and tracked test-bar snapshots without retraining and records itself as their `reproduction_command` |
| Action masking | **INCOMPLETE / GAP** | `TrainedPolicy.select` masks SELL without a position and BUY at the position cap, but the adapter still attempted 433 BUY actions that the PI-13 environment rejected across the five cash-drag runs. `invalid_actions` stayed 0 because these were environment constraint rejections, not malformed actions. |
| Redesigned experimental reward | PASS as implementation | `evaluation/rl_strategy/cash_drag_results_compact.json`; config hash and experimental status recorded |
| No malformed actions | PASS in the tested runs | All five cash-drag policies: `invalid_actions: 0`; this does **not** mean clean execution (see the 433 constraint rejections below) |
| No reward from deposits | PASS by interface and run design | `TransitionView` exposes realized/unrealized P&L deltas, equity, exposure, drawdown, fees and costs; there is no deposit/funding field. The RL environment starts from a fixed capital and has no deposit action. |
| Realized/unrealized P&L treatment | PASS in tests | Reward components use realized/unrealized deltas and explicit costs; PI-14 tests pass |
| No automatic policy promotion | PASS | `automatic_policy_promotion: false`; no promotion/approval route invoked |
| Identical datasets and costs for baselines | PASS within the harness | Same five instruments, chronological split and NM-10 fee/slippage config for RL and baselines |

### 6.2 Results (out-of-sample harness run on 24 November 2026, simulation)

The prior compact evidence reported zero trades because the harness's local `choose_action` deliberately converted a zero-Q tie into HOLD. That is **not** the selector in the production adapter. `TrainedPolicy.select` uses a masked argmax, so a zero-Q tie selects the first legal action, BUY. The out-of-sample test episode now goes through `TrainedPolicy.select` inside the harnesses themselves (`run_rl_evaluation.py` and `run_cash_drag_experiment.py`), so rerunning either documented harness writes its compact evidence directly (`results_compact.json` or `cash_drag_results_compact.json`). `evaluate_trained_adapters.py` is the no-retraining path: it rescores the saved artifacts through `TrainedPolicy.select`, reads the exact test-split bars from the tracked snapshots under `evaluation/rl_strategy/artifacts/test_bars/` instead of opening `data/novamarket.sqlite3`, refreshes both tracked compacts, and records itself as their `reproduction_command`; it rejects any `source_artifacts` path that resolves outside `evaluation/rl_strategy/artifacts/`. The 21 November artifacts were also re-evaluated without retraining through that evaluator before the selector change.

| Policy set | Policies | Executed trades | Policies with ≥1 trade | Action counts | Invalid | Constraint violations | Total reward | Verdict |
|---|---:|---:|---:|---|---:|---:|---:|---|
| Fresh default-reward DQN | 5 | **3** | 3 | 261 BUY, 177 NO_ACTION | 0 | 258 | −171.3309 | BUY-on-tie churn; no SELL, no clean policy |
| RL trading-reward ablation | 5 | **5** | 5 | 438 BUY | 0 | 433 | −203.1958 | BUY-on-tie churn; most buy attempts rejected |
| Experimental cash-drag reward | 5 | **5** | 5 | 438 BUY | 0 | 433 | −218.3620 | Zero-Q tie, not learned; all test Q spreads are 0.0 |

**The trade count users actually get is not zero.** All five cash-drag policies executed at least one out-of-sample trade; the compact evidence reports 5 executed trades, not the previous 0. The default/trading set executes 8 trades across 10 policies. However, the adapter does not produce a clean trading policy: it chooses BUY on every decision in the cash-drag and trading runs, and **433 of those BUY attempts per five-policy set are rejected by the environment's cash/notional/liquidity/projected-fill constraints**. There are no SELL decisions. This is degenerate BUY churn, not evidence of an edge or improved behaviour.

Action distributions are therefore **not** all HOLD/no-action. The old zero-trade result was an artifact of the harness selector; the adapter's actual action is BUY on a zero-Q tie. No policy should be promoted, and these simulated losses are not live or profitability evidence.

### 6.2a Q-value spread and training-action diagnostic

The diagnostic is recorded per episode and in the compact training metadata (`training_action_distribution`, `training_q_value_diagnostics`, and test `q_value_diagnostics`). The key result is not ambiguous:

| Instrument | Training BUY / HOLD / SELL / WAIT / NO_ACTION | Training zero-Q observations | Test zero-Q observations | Test Q max spread | Saved best validation score |
|---|---:|---:|---:|---:|---:|
| `XNSE:HDFCBANK` | 20 / 1,977 / 107 / 129 / 127 | 2,132 of 2,360 | **87 of 87** | 0.0 | 0.0 |
| `XNSE:ITC` | 20 / 2,006 / 103 / 124 / 107 | 2,293 of 2,360 | **87 of 87** | 0.0 | 0.0 |
| `XNSE:RELIANCE` | 20 / 1,996 / 98 / 125 / 121 | 2,200 of 2,360 | **90 of 90** | 0.0 | 0.0 |
| `XNSE:TATASTEEL` | 20 / 2,004 / 110 / 125 / 101 | 2,292 of 2,360 | **87 of 87** | 0.0 | 0.0 |
| `XNSE:TCS` | 20 / 1,989 / 122 / 116 / 113 | 2,206 of 2,360 | **87 of 87** | 0.0 | 0.0 |
| **Total** | **100 / 9,972 / 540 / 619 / 569** | **11,123 of 11,800** | **438 of 438** | **0.0** | — |

**Did the experiment learn? No.** Every saved cash-drag checkpoint produces all-zero Q-values on every test observation, and its Q-value spread is exactly zero. The best validation score for every instrument is `0.0`, so the checkpoint selection never found a positive validation result. The training action distribution is dominated by HOLD (9,972 of 11,800 choices, 84.5%); BUY was selected only 100 times (0.85%) across all five training runs, which is consistent with exploration and the harness's tie-abstain rule, not with a learned BUY preference. The training Q-values were also zero in 11,123 of 11,800 observations, and two runs show later values above `1e153`, indicating numerical divergence rather than stable learning.

The five adapter trades are therefore a **zero-Q tie effect**, not evidence that the cash-drag reward taught a trading policy. The experiment failed to learn. Any claim that the cash-drag reward “induced trading” is unsupported; the correct conclusion is an untrained/degenerate network plus a masked-argmax tie selecting BUY.

### 6.2b Focused cash-drag weight/scale sweep

Because the weight and scale were fixed at `cash_drag=0.30` and `scale=0.02` in the failed run, three independent variants were trained on the same five instruments, split and seed. Each clone changed only `components["cash_drag"]` and/or `scales["cash_drag"]`; the default reward configuration was not changed. All variants were evaluated through `TrainedPolicy.select` on the test split. Evidence: `evaluation/rl_strategy/cash_drag_sweep_results.json`.

A result is counted as **genuine** only when it has at least one executed out-of-sample trade **and** non-zero Q-value spread (max spread `> 1e-9`). Zero-Q tie BUYs do not count.

| Variant | Weight | Scale | Executed trades | Policies with trades | Test zero-Q observations | Test max Q spread | Training BUY / HOLD / SELL / WAIT / NO_ACTION | Genuine? |
|---|---:|---:|---:|---:|---:|---:|---|---|
| `w0.10_s0.01` | 0.10 | 0.01 | 5 | 5/5 | 438 / 438 | 0.0 | 100 / 9,916 / 577 / 611 / 596 | **No** |
| `w0.60_s0.02` | 0.60 | 0.02 | 5 | 5/5 | 438 / 438 | 0.0 | 100 / 9,991 / 514 / 628 / 567 | **No** |
| `w0.30_s0.10` | 0.30 | 0.10 | 5 | 5/5 | 438 / 438 | 0.0 | 100 / 9,907 / 587 / 613 / 593 | **No** |

All three variants also recorded **433 environment constraint rejections** across their five policies, and their training Q-values were zero in 10,945, 11,166, and 11,009 of 11,800 observations respectively. Increasing the cash-drag weight or scale did not create a learned Q signal; every saved checkpoint still produced zero-spread Q-values on the test split, so every executed trade is the same zero-Q tie effect.

**Sweep conclusion:** no tested cash-drag weight/scale variant produced genuine out-of-sample trades, and changing those knobs did not make the experiment learn. The default reward was not modified. A broader sweep of more seeds or substantially different training settings would be a new experiment, not evidence of a fix.

### 6.3 P&L and deposit boundary

The RL environment has no deposit or funding operation. Its reward transitions are driven by simulated portfolio equity, realized/unrealized P&L, fees, slippage, exposure and drawdown. The separate portfolio fixture’s ₹20,000 funding is an external flow with its own reconciliation; it is not an RL reward and is not counted as investment return in the performance endpoint.

---

## 7. Failure-injection results

Detailed evidence: `tmp/continuous_paper_validation/paper_lifecycle.json` (`database_interruption`), `tmp/pre_market_offline/read_only.json`, and the targeted regression suite.

| # | Injected failure | Class | Result |
|---|---|---|---|
| 1 | SQLite write lock during an approved paper forward | FAULT INJECTION | **FAILED CLOSED.** Separate `BEGIN IMMEDIATE` lock; forward returned HTTP 500 `database is locked`; no order, fill, holding, wallet, ledger or portfolio mutation; proposal remained approved |
| 2 | Read-only database | FAULT INJECTION | **FAILED CLOSED.** Temporary wallet DB chmod read-only; `OperationalError: attempt to write a readonly database`; balance stayed ₹100.00, one transaction, `integrity_check=ok` |
| 3 | Interruption during an in-flight write | FAULT INJECTION | Covered by the same lock drill; all 12 checks true: request refused, all financial counts unchanged, proposal approved, no order for proposal, database integrity okay |
| 4 | Market-data provider timeout | TEST-LEVEL | PASS at unit level in `test_novamarket_marketdata.py`; the failure is surfaced, not converted into a fresh quote. No live network partition was created. |
| 5 | Provider disconnection | TEST-LEVEL | PASS at unit level; no live provider session was available. No fabricated quote was produced. |
| 6 | Scheduler interruption | TEST-LEVEL | PASS in the targeted scheduler suite (atomic claims, leases, duplicate-tick guard and recovery). No multi-hour scheduler soak was run. |
| 7 | Duplicate execution request | FIXTURE | **PASS.** Second forward returned 200 with `replayed: true`; order count 1 → 1; execution replayed |
| 8 | Application restart during/after an unfinished paper order | FIXTURE | **PASS for completed-order restart.** Stores reopened with 2 orders, 0 holdings, same ₹19,995.26 and same −₹4.76. A crash *during* an in-flight order was not reproduced end to end; execution unit tests cover the state transition. |
| 9 | Partial data ingestion | ACTUAL DATA + TEST-LEVEL | The real store contains partial/non-valid intraday rows; the analyzer excluded incomplete buckets and non-usable bars and abstained when the layer was missing/stale. No partial row was promoted to a completed bar. |
| 10 | Concurrent portfolio updates | TEST-LEVEL + FAULT INJECTION | Targeted wallet/portfolio/execution tests pass; the write-lock drill demonstrates atomic refusal under contention. No high-concurrency load test was performed. |

### Safety conclusions from failure injection

- No injected failure created an order, fill or ledger entry that was not authorized.
- A duplicate forward did not create a second order.
- A locked database did not leave a partial fill or a stuck write transaction; retry after release created exactly one order.
- Financial integrity checks (`PRAGMA integrity_check`, foreign-key check, ledger sum, wallet balance vs entries) remained clean.
- The HTTP 500 with a raw `"database is locked"` detail is fail-closed but is an **operational UX/security gap**; it is not a data-integrity failure.

---

## 8. Performance measurements

### 8.1 In-process synthetic benchmark (SIMULATION)

Source: `tmp/pre_market_offline/perf.json`, 100 repetitions unless noted. These are short-run measurements on synthetic bars, not a production load test.

| Measurement | Mean | p95 | Max | CPU |
|---|---:|---:|---:|---:|
| 20m context analysis | 1.640 ms | 2.448 ms | 2.815 ms | 156.25 ms / 100 |
| 5m setup analysis | 0.418 ms | 0.743 ms | 0.957 ms | 46.88 ms / 100 |
| 1m timing analysis | 0.223 ms | 0.383 ms | 0.522 ms | 15.63 ms / 100 |
| Full MTF, warm cache | 2.731 ms | 3.876 ms | 4.211 ms | 265.63 ms / 100 |
| Full MTF, cold cache | 3.723 ms | 4.927 ms | 9.057 ms | 187.50 ms / 50 |
| Wallet transaction (temporary DB) | 3.410 ms | 4.661 ms | 7.077 ms | 234.38 ms / 100 |

Memory: traced peak **0.067 MB** for the synthetic MTF probe. Real replay peak: **3,008,217 bytes (~2.87 MiB)**. These are not memory-stability measurements; there was no multi-hour soak.

### 8.2 Real replay and full lifecycle

| Measurement | Result | Class |
|---|---:|---|
| Real 165-decision MTF replay wall time | 3.365 s | HISTORICAL REPLAY |
| Real replay peak traced memory | 3,008,217 bytes | HISTORICAL REPLAY |
| Baseline 1m decision latency | 0.2657 ms mean / 0.3475 ms p95 | HISTORICAL REPLAY |
| Enhanced MTF decision latency | 18.2906 ms mean / 16.0587 ms recorded p95 | HISTORICAL REPLAY; p95 below mean is recorded as-is, so treat as indicative |
| Full fixture lifecycle, before-state through restart | approximately **96 s**, including a deliberate database lock and an application restart | FIXTURE |
| Database lock refusal | fail closed; recovery retry immediately returned 200 | FAULT INJECTION |
| Prior lock-wait drill | 5.585 s to fail closed with no balance mutation | FAULT INJECTION (prior run on this build) |

### 8.3 Forecast and model inference

The current session did not add a new inference benchmark. The most recent current-build measurements are:

| Model/path | Mean latency | p95 |
|---|---:|---:|
| NM-06 `zero_return` | 0.049 ms | 0.079 ms |
| NM-06 `random_walk` | 0.052 ms | 0.053 ms |
| NM-06 `bootstrap` | 1.117 ms | 1.211 ms |
| NM-06 maximum observed | 1.527 ms | — |
| PI-05 estimators | 0.07–10.70 ms depending on model | — |
| RL `q_values` | 38.6 µs per call (20,000 calls) | — |

**Performance verdict:** sufficient for attended, once-per-minute paper operation in the measured paths. There is no throughput, queue-depth, multi-hour memory-growth or sustained-writer benchmark.

---

## 9. Defects discovered and fixed

### 9.1 Validation-harness defect fixed (not a production-code change)

The comprehensive fixture lifecycle initially failed with `fixture entry produced no proposal`. Investigation showed:

- The harness captured `CLOCK` at import and seeded intraday bars only once.
- Forecast training then took longer than the 180-second 1-minute freshness limit.
- The product correctly returned `wait_for_confirmation` with `latest 1m bar is older than 180s`.
- A second attempt reseeded bars without aligning them to 5-minute/20-minute boundaries, producing duplicate/unaligned buckets; the product again correctly abstained.

**Fix:** `tmp/continuous_paper_validation/run_validation.py` now:

1. Seeds the intraday fixture immediately before the hierarchy, not before forecast training.
2. Aligns the latest completed 1m and 5m bars to the clock boundary.
3. Uses a live clock for the copied market-data engine.
4. Does not seed duplicate intraday series.

**Result:** the fixed harness was rerun end to end with `--only all` (EXIT 0, generated `2026-11-22T15:47:52Z`). That single run produced the cited log, `evidence.json`, `paper_lifecycle.json` and `database_interruption.json`; it contains the full BUY → lock → retry → duplicate → risk-rejection → exit → restart lifecycle. `tmp/continuous_paper_validation/full_validation_manifest.json` records the run timestamp and SHA-256 hashes, and confirms that the lifecycle embedded in `evidence.json` equals `paper_lifecycle.json`. No product freshness threshold, risk assertion or test assertion was weakened.

The earlier full-run log predates the harness fix and is retained as `tmp/continuous_paper_validation/pre_market_all_prefix.log` with an explicit pre-fix label. The later `pre_market_paper_fixed3.log` is a partial post-fix `--only paper` rerun; neither is presented as the end-to-end lifecycle evidence.

### 9.2 Older harness limitation

`tmp/audit/e2e_paper_chain.py` exits 4 with one signal and no proposal. Its copied strategy/portfolio path is not the complete MTF-aware fixture path and it uses the older short-term setup. It is **not** evidence of a production execution defect; it was superseded by the repaired `run_validation.py` lifecycle. The older `pre_market_chain.log` is retained as a failed-harness record, not a market result.

### 9.3 No new production financial-control defect found

The tested production paths showed:

- one NM-10 paper execution gateway;
- no unapproved order from a rejected risk request;
- no duplicate order from a repeated forward;
- no partial financial write after a lock;
- ledger and wallet reconciliation at zero difference;
- restart preservation of orders, holdings, valuation and realized P&L.

The existing operational defects from the final system assessment remain open and are not hidden by this run: the single-process full-suite instability, audit-log rename failure, raw 500 responses, authentication defaults, no `ruff`/dependency-vulnerability scan, and sparse intraday data.

---

## 10. Remaining limitations

1. **No live session.** The market was closed. All actual-data work is historical replay; there was no live provider, live quote or live paper session.
2. **Thin intraday history.** Three instruments, ten instrument-days, 2,417 one-minute bars, no native 5-minute bars and 66.67% coverage on the latest full day. The two-instrument replay is not a broad universe.
3. **Small sample.** 165 decisions, 2 enhanced trades, 11 baseline trades and 5 resolved forecasts cannot establish strategy or forecast skill.
4. **No demonstrated edge.** Both the baseline and the MTF-enhanced replay lost after 7 bps assumed costs; the forecast mean Brier is 0.6839 and directional accuracy is 40% on five outcomes.
5. **RL did not learn.** All five cash-drag checkpoints have exactly zero Q-value spread on every test observation, their best validation score is 0.0, and training is dominated by HOLD (84.5% of choices) with only 100 BUY choices in 11,800 decisions. The adapter's five executed trades and BUY actions are a zero-Q tie effect, not learned trading; 433 buy attempts per five-policy set are rejected by environment constraints. A focused sweep of three cash-drag weight/scale variants also produced zero Q-spread and no genuine trades. No policy was promoted.
6. **Fixture lifecycle is not market evidence.** The BUY/SELL/exit chain, including its −₹4.76 result, is deterministic synthetic trading through real code. It proves plumbing, not profitability or fill availability.
7. **Restart was after a completed order, not a crash mid-write.** The lock drill covered an in-flight refused write; a true process kill during an order commit was not reproduced end to end.
8. **Provider timeout/disconnection is unit-tested, not live-tested.** No real network partition or exchange outage was injected.
9. **Scheduler interruption is test-level.** No multi-hour scheduler or unattended autonomy soak was run.
10. **Concurrency is not load-tested.** One process, SQLite and process-local locks; no multi-writer throughput or high-concurrency benchmark.
11. **No full single-process suite green.** The suite collects 1,972 tests. The 23 November 2026 single-process rerun completed with **1,969 passed, 1 failed, 2 skipped and 447 subtests** and did not reproduce the earlier LSTM crash; the one failure was a date-sensitive scheduler fixture that selected 24 November 2026, a verified NSE holiday. Ordered Group A passed 697; Group B had the same failure. The historical runner instability remains unresolved. This validation reran 534 targeted tests successfully; it does not claim a new full-suite green.
12. **No dependency/lint scan.** `ruff`, `pip-audit` and `safety` are unavailable; no clean lint or vulnerability result is claimed.
13. **No 2027 calendar verification.** Session evaluation fails closed outside verified calendar coverage.
14. **No production deployment or broker authorization.** Live trading remains structurally blocked.

---

## 11. Monday live-session preparation checklist

Target: **Monday, 23 November 2026, 09:15–15:30 IST**. The next exchange holiday is 24 November 2026. This checklist is for a **supervised paper session**, not live trading.

### Sunday 22 November — preparation only

- [ ] Confirm `tmp/market_open_paper/session_context.json` exists.
- [ ] Confirm `tmp/market_open_paper/market_open_readiness.json` has `"safe": true` and the Yahoo probe is `ok: true`.
- [ ] Confirm `data/novamarket.json` was not modified by preparation; only the prepared work directory may change.
- [ ] Confirm no broker credentials, live endpoint or real-money route is configured.
- [ ] Back up the current financial stores and keep the backup outside the OneDrive-synchronised folder.
- [ ] Preserve this report and the evidence paths in section 12.
- [ ] Choose **capture-only** for the first run. Do not start with `--execute-paper`.

### Monday 09:00–09:10 IST — start

- [ ] Open a foreground terminal in `novamarket`; do not use Task Scheduler, `nohup`, a service or autonomy mode.
- [ ] Do not run `--prepare --reset` on the session day.
- [ ] Start capture-only:
  ```bash
  cd novamarket
  PYTHONPATH=src ../.venv/Scripts/python.exe \
    tmp/continuous_paper_validation/run_market_open.py \
    --run --wait-for-open
  ```
- [ ] Confirm the safety gate says paper-only, `live_trading=false`, `broker_connected=false`, `automatic_policy_promotion=false`.
- [ ] Confirm the process is in the prepared 2026-11-23 directory and refuses to run outside the session date.

### During the session

- [ ] Let the loop collect `1m` and `5m` every 60 seconds; do not substitute a stored close for a missing quote.
- [ ] Treat a stale/missing quote or MTF `wait_for_confirmation` as a valid **no-trade** result.
- [ ] Do not lower risk limits, widen notional, bypass NM-08 or force a fill.
- [ ] Do not call learning approval, strategy promotion, autonomy start or any RL promotion route.
- [ ] If an order is rejected, record it and continue.
- [ ] If reconciliation returns `ok=false`, stop forwarding, preserve files and investigate; never hand-edit the ledger.

### Stop conditions

Stop immediately if any of these occurs:

- a safety flag is not paper-only;
- a fill exists without its approved proposal/order;
- an order belongs to the wrong portfolio or instrument;
- portfolio or wallet reconciliation is not `ok`;
- database integrity or foreign-key check fails;
- a provider conflict cannot be reconciled;
- the process attempts a live/broker path.

### After close — reconciliation

- [ ] Confirm the capture status is `completed` or an explicit aborted reason.
- [ ] Compare before/final order, fill, holding and valuation counts.
- [ ] Confirm every new fill traces to a proposal and an NM-08 decision.
- [ ] Confirm `final.reconcile.ok`, `final.wallet_reconcile.ok` and `integrity.all_ok`.
- [ ] Report cycles with same-session quotes, signals, proposals, fills, rejections and paper P&L.
- [ ] Label the session **PAPER ONLY**; do not call it live trading or profitability evidence.
- [ ] Preserve `market_open_capture.json`, `market_open_events.jsonl` and the terminal log.

### Only after a clean capture-only session

- [ ] Review the capture with the operator and confirm no unresolved reconciliation or data-quality issue.
- [ ] If, and only if, the operator remains present, run the supervised paper path:
  ```bash
  PYTHONPATH=src ../.venv/Scripts/python.exe \
    tmp/continuous_paper_validation/run_market_open.py \
    --run --wait-for-open --execute-paper
  ```
- [ ] Keep the same stop conditions; `--execute-paper` still uses only NM-10 paper execution and never a broker.
- [ ] Do not run unattended, do not promote a policy and do not connect live money.

---

## 12. Evidence index

| Path | Contents |
|---|---|
| `tmp/continuous_paper_validation/evidence.json` | Current compact evidence: inventory, status, real MTF, forecasts, paper lifecycle, database interruption |
| `tmp/continuous_paper_validation/paper_lifecycle.json` | Full current fixture lifecycle details |
| `tmp/continuous_paper_validation/real_multitimeframe.json` | Per-instrument real replay and metrics |
| `tmp/continuous_paper_validation/forecasts.json` | Five chronological forecast runs and outcomes |
| `tmp/continuous_paper_validation/pre_market_all_fixed.log` | Fixed-harness end-to-end `--only all` run (EXIT 0, generated `2026-11-22T15:47:52Z`) |
| `tmp/continuous_paper_validation/full_validation_manifest.json` | Same-run binding and SHA-256 hashes for the log, evidence and lifecycle |
| `tmp/continuous_paper_validation/pre_market_all_prefix.log` | Pre-fix full-attempt log, retained only as pre-fix history; not lifecycle evidence |
| `tmp/continuous_paper_validation/pre_market_paper_fixed3.log` | Partial post-fix `--only paper` rerun; not the end-to-end lifecycle evidence |
| `tmp/continuous_paper_validation/database_interruption.json` | Lock drill detail |
| `tmp/pre_market_offline/scenarios.json` | Ten synthetic MTF/policy scenarios |
| `tmp/pre_market_offline/read_only.json` | Read-only database drill |
| `tmp/pre_market_offline/perf.json` | MTF/wallet latency and memory probe |
| `tmp/pre_market_offline/liquidity.json` | Low-liquidity NM-08 probe |
| `tmp/pre_market_offline/db_integrity.log` | Source DB integrity/foreign-key/ledger check |
| `tmp/pre_market_offline/targeted.log` | 534 passed, 2 skipped targeted regression |
| `tmp/pre_market_offline/mypy.log` | `mypy src` clean (724 files) |
| `tmp/pre_market_offline/api_check.log` | API reference in sync (935 routes) |
| `tmp/pre_market_offline/collect.log` | 1,972 NovaMarket tests collected |
| `evaluation/rl_strategy/results_compact.json` | Ten-policy out-of-sample evidence re-scored through `TrainedPolicy`; 8 policies execute a trade |
| `evaluation/rl_strategy/cash_drag_results_compact.json` | Five-policy experimental cash-drag evidence re-scored through `TrainedPolicy`; 5 executed trades, 433 constraint rejections, and training/test Q-value diagnostics |
| `evaluation/rl_strategy/artifacts/test_bars/*.json` | Five tracked exact test-split OHLCV snapshots (91 RELIANCE bars; 88 each for the other four instruments); the adapter evaluator reads these instead of the database |
| `evaluation/rl_strategy/evaluate_trained_adapters.py` | Re-evaluates persisted policy artifacts through the production adapter and regenerates both compact extracts without retraining or opening the database; verifies `source_artifacts` stays under `evaluation/rl_strategy/artifacts/` |
| `evaluation/rl_strategy/cash_drag_sweep_results.json` | Focused three-variant cash-drag weight/scale sweep; all variants zero-Q and non-genuine |
| `evaluation/rl_strategy/run_cash_drag_sweep.py` | Reproducible sweep runner; default reward unchanged |
| `docs/MARKET_OPEN_PAPER_RUNBOOK.md` | Supervised session procedure and safety contract |

---

## 13. Final regression and readiness verdict

### Regression checks actually run in this validation

| Check | Result |
|---|---|
| Targeted NovaMarket suite (12 files: MTF, PI-14/15, stability, execution, portfolio, wallet, risk, scheduler, forecasting, strategies, market data) | **534 passed, 2 skipped, 59 subtests passed, 0 failed** in 193.68 s |
| NovaMarket tests collected | **1,972** |
| 23 Nov ordered Group A (21 files through LSTM) | **697 passed, 390 subtests, 0 failed** in 349.67 s |
| 23 Nov ordered Group B (35 files) | **1,272 passed, 1 failed, 2 skipped, 57 subtests** in 231.97 s |
| 23 Nov single-process full suite (56 files) | **1,969 passed, 1 failed, 2 skipped, 447 subtests** in 514.43 s; historical LSTM crash not reproduced |
| Prior ordered full-suite result on this build (22 Nov) | **1,970 passed, 2 skipped, 0 failed** (697 + 1,273); historical single-process run remains unstable |
| `mypy src` | **Success, 724 files** |
| API reference | **935 routes, in sync** |
| Source DB integrity | 14 non-empty stores, **0** integrity failures, **0** foreign-key violations; one 0-byte placeholder |
| Wallet ledger | 17 wallets, 40 entries, 20 transactions, ledger sum **₹0.00**, 0 wallet/ledger mismatches |
| Paper lifecycle | EXIT 0; BUY, lock refusal/recovery, duplicate replay, oversized rejection, SELL/exit, restart all verified |
| Read-only DB drill | Refused write; no mutation; integrity okay |
| Low-liquidity policy probe | `requires_review`, `min_liquidity` breach |
| Full suite note | Not rerun as one process; the ordered-group result is the complete coverage evidence, and the runner instability is an open limitation |

### Readiness conclusion

| Level | Verdict | Basis |
|---|---|---|
| **Software / paper plumbing** | **PASS (conditional)** | Full fixture lifecycle, risk/execution boundary, ledger reconciliation, DB integrity, type/API checks and targeted tests pass. Conditional on the open runner/operational defects. |
| **Supervised real-market paper session** | **PARTIAL — prepared for capture-only; conditional for `--execute-paper`** | Historical replay and fixture plumbing are sound; no live session, sparse intraday data, small sample and no demonstrated edge. Capture-only is the correct first Monday run. |
| **Live trading** | **BLOCKED** | No broker, live data, live paper session, real-money authorization or sustained operational evidence. |

**Bottom line:** NovaMarket is technically prepared for an **attended, capture-only paper session** on Monday 23 November 2026, and conditionally prepared for a **supervised paper-execution** run after a clean capture-only session. It is **not** prepared for live trading, unattended autonomy or any profitability claim. No PI-25 was created, no live trading was enabled, no broker was connected, no RL policy was promoted and no commit or push was made.
