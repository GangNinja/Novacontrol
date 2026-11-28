# NovaMarket — Continuous Paper-Trading Validation Report

**Validation date:** 2026-11-21  
**Scope:** operational validation of the completed PI-01–PI-24 system plus the multi-timeframe and forecasting enhancements. No PI-25 was created and the 24-PI roadmap was not changed.  
**Execution mode:** simulated/paper execution only. No live order, broker connection, or production promotion was enabled.

---

## 1. Environment and configuration

| Item | Value |
|---|---|
| Repository | `novamarket/` |
| HEAD | `9f0aec8 feat(novamarket): close PI-01–PI-24 with fixes, verification and refreshed docs` |
| Working tree | Pre-existing uncommitted changes were present; validation changes remain uncommitted. No commit or push was made. |
| Runtime | Windows, Python 3.13, project virtual environment `.venv` |
| Original data | `novamarket/data/` was treated as read-only |
| Mutation isolation | Every write used a copied SQLite directory under `novamarket/tmp/continuous_paper_validation/` |
| Harness | `tmp/continuous_paper_validation/run_validation.py` |
| Evidence | `tmp/continuous_paper_validation/*.json`, `real_multitimeframe_decisions.jsonl` |
| Safety flags | `/market/execution/status`: `paper_only=true`, `live_trading=false`, `broker_connected=false` |
| Autonomy flags | `/market/autonomy/status`: `trading_implemented=false`, `orders_possible=false`, `release_gate=closed` |
| Operator gate | `operator_authorization_required=true`, `operator_authorized=false` |
| RL gate | `/market/rl/status`: `simulated=true`, `advisory_only=true`, `production_authority=false` |
| Training gate | `/market/training/status`: `advisory_only=true`, `activation_available=false` |
| Forecast policy | `apply_calibration=false`; report-only calibration remains the default |

The stored market configuration on the copied environment was `mode=observe`, `data_providers=[]`, `ingestion.enabled=false`, and `gateway.enabled=false`. The API status is therefore honest about a system that is operational but not autonomously ingesting or trading.

Primary commands:

```bash
cd novamarket
PYTHONPATH=src ../.venv/Scripts/python.exe tmp/continuous_paper_validation/run_validation.py --only all
PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest tests/test_novamarket_*.py -q
```

---

## 2. Data-provider information

### Stored data inventory

| Interval | Provider | Bars | Instruments | First observation | Last observation |
|---|---|---:|---:|---|---|
| 1d | Yahoo | 30,883 | 149 | 2025-10-29T03:45Z | 2026-11-19T03:45Z |
| 1m | Yahoo | 2,417 | 3 | 2026-11-05T05:20Z | 2026-11-20T09:58Z |
| 1d | fixture | 1,091 | 1 | 2026-07-08T20:11Z | 2026-11-20T09:00Z |

The real intraday instruments are `XBOM:ADANIENT` (1,917 bars), `XBOM:RELIANCE` (250), and `XNSE:ADANIENT` (250). There are no stored native 5-minute bars. The MTF replay derives 5-minute bars only from five contiguous one-minute observations; the derived data is labelled as derived, not provider-native.

### Latest-session coverage

| Instrument | Latest session | 1m bars | Coverage of 375-minute session | Intraday gaps >120s | Largest gap |
|---|---|---:|---:|---:|---:|
| `XBOM:ADANIENT` | 2026-11-20 | 250 | 66.67% | 14 | 840s |
| `XBOM:RELIANCE` | 2026-11-06 | 250 | 66.67% | 1 | 960s |
| `XNSE:ADANIENT` | 2026-11-05 | 250 | 66.67% | 1 | 780s |

### Observation freshness

| Provider | Freshness | Observations | Latest |
|---|---|---:|---|
| Yahoo | fresh | 1,446 | 2026-11-20T09:28:44Z |
| Yahoo | closed_market | 219 | 2026-11-20T10:00:03Z |
| fixture | fresh | 8 | 2026-11-20T09:03:45Z |
| Yahoo | delayed | 7 | 2026-11-20T04:51:30Z |
| Yahoo | stale | 5 | 2026-11-19T09:41:35Z |

**Data-quality conclusion:** the stored history is real Yahoo data for daily bars, but intraday coverage is sparse, only three real intraday instruments exist, and 5-minute history must be derived. The current observations are `closed_market`, not current live quotes.

---

## 3. Validation duration and sessions

| Validation mode | Period / duration | What actually ran |
|---|---|---|
| Historical real-data replay | 2026-11-05 and 2026-11-06 | 165 chronological 1m/5m/20m hierarchy evaluations across two real sessions |
| Real forecast replay | Origins 2026-11-04, 2026-11-10, 2026-11-18 | Five chronological train/generate/resolve cycles; all five outcomes resolved by 2026-11-20T12:00Z |
| Fixture paper lifecycle | One synthetic validation session | Entry, NM-08 approval, NM-10 fill, duplicate replay, oversized risk rejection, exit fill, ledger reconciliation, restart |
| Database-write interruption | One controlled drill during the fixture lifecycle | Approved proposal forwarded while a separate SQLite connection held a write lock on the copied financial store; failed closed, then retried after release |
| Current wall-clock operation | Saturday 2026-11-21 | Market is closed (`weekend`); next open is 2026-11-23T09:15+05:30 |

**No continuous live session was run.** The real-data work is historical replay. The paper lifecycle is fixture-based. Neither is presented as live-market paper trading. The market was closed for the entire validation window, so a complete live market session could not be established.

---

## 4. Instruments monitored

| Universe | Count | Instruments |
|---|---:|---|
| Real daily-bar universe | 149 | Yahoo daily instruments; 24 had fewer than 21 usable daily bars for condition classification |
| Real intraday universe | 3 | `XBOM:ADANIENT`, `XBOM:RELIANCE`, `XNSE:ADANIENT` |
| Real multi-timeframe replay | 2 | `XNSE:ADANIENT`, `XBOM:RELIANCE` |
| Real forecast monitoring | 2 | `XBOM:ADANIENT`, `XNSE:RELIANCE` |
| Fixture paper lifecycle | 1 | `XNSE:MTFVAL01` (deterministic synthetic) |

### Market-condition coverage

The daily condition sample is broad but observational, not a performance sample. It contains 68 bullish, 35 bearish, 23 sideways, 53 high-volatility, 6 low-volume, and 24 insufficient-history classifications; direction and volatility/volume tags overlap.

| Condition | Coverage in this validation | Evidence / limitation |
|---|---|---|
| Bullish | 68 real daily instruments classified | Daily 20-session return > +2%; no separate intraday paper P&L per instrument |
| Bearish | 35 real daily instruments | Daily 20-session return < -2%; same limitation |
| Sideways | 23 real daily instruments | Between -2% and +2% |
| High volatility | 53 real daily instruments | 20-session daily-return standard deviation >= 2% |
| Low volume | 6 real daily instruments | Latest volume < 50% of the 20-session median |
| Sudden move | Fixture exit path | A -10.45% fixture move produced a protective exit; no claim about real gap behaviour |
| Market open/close | Partial only | Real 1m samples run 04:40–09:58Z or 05:36–10:00Z; no full 03:45–10:00Z session and no complete live open/close |
| Missing data / provider interruption | Contract and test coverage | MTF abstains on insufficient/stale/incomplete data; provider-failure tests pass; no live provider outage was injected |

No strategy-performance conclusion is drawn from these condition buckets, because the condition sample is daily and the paper execution sample is fixture-based. The system demonstrably abstains rather than forcing trades when a layer is neutral or data is incomplete; it does not demonstrate condition-specific profitability.

---

## 5. Multi-timeframe decision statistics

### Real-data replay

| Instrument | Decisions | BUY_CANDIDATE | SELL_CANDIDATE | WAIT_FOR_CONFIRMATION | REJECT_SIGNAL | HOLD |
|---|---:|---:|---:|---:|---:|---:|
| `XNSE:ADANIENT` | 83 | 0 | 0 | 0 | 0 | 83 |
| `XBOM:RELIANCE` | 82 | 10 | 0 | 12 | 0 | 60 |
| **Total** | **165** | **10** | **0** | **12** | **0** | **143** |

All 165 decisions had all three layers available and none were marked stale at the replay instant. No `REJECT_SIGNAL` occurred in this particular sample; that outcome is covered by the hierarchy contract and its test suite, but was not observed here.

The dominant reasons were:

- `20m context is neutral; there is no directional trade to time` — 143 decisions.
- `1 incomplete 20m bucket(s) excluded` — 120 decisions.
- `market context is neutral; the setup lacks a directional backdrop` — 73 decisions.
- `no momentum, breakout or reversal setup` — 70 decisions.
- `one-minute momentum and structure are not aligned` — 56 decisions.
- `all three timeframes agree` — 10 decisions.

The 10 BUY_CANDIDATE decisions were clustered on `XBOM:RELIANCE` between 09:20 and 09:41 UTC on 2026-11-06. Confidence was 0.6869 on most candidates and 0.7000 on the last two. Two were admitted as new entries; eight were suppressed by the five-bar cooldown. The resulting abstention/rejection rate was 93.94%.

The real sample contains no candidate on `XNSE:ADANIENT`; the hierarchy stayed neutral and did not force a trade. That is the correct safe-abstention behaviour, but it also means the real sample provides only one instrument's entry pattern.

### Data-integrity safeguard discovered and fixed

While probing historical replay, the hierarchy was found to include stored bars observed after the decision instant. `MultiTimeframeAnalyzer._series` now filters `observed_at <= moment` before normalisation. The regression test `test_future_bars_are_never_visible_to_a_historical_decision` passes as part of the 32-test MTF suite. Without this fix, replay results would have had look-ahead leakage.

---

## 6. Forecasting performance

Five chronological forecasts were trained from real daily bars, generated at historical cutoffs, and resolved against later actual bars. Training used only bars at or before each cutoff; the test slice was not used to choose a model or fit a calibrator. All five used the `next_session` horizon (one trading session).

All rows below used horizon `next_session` and baseline `random_walk`.

| Instrument | Cutoff | Selected model | Baseline | Skill-gate abstained | Predicted up/down/sideways | Actual return | Direction correct | Brier | Applied calibration |
|---|---|---|---:|---:|---|---:|---:|---:|---|
| `XBOM:ADANIENT` | 2026-11-04 | random_walk | random_walk | yes | 0.3743 / 0.3529 / 0.2727 | +5.45% | yes | 0.5904 | uncalibrated |
| `XBOM:ADANIENT` | 2026-11-10 | random_walk | random_walk | yes | 0.3750 / 0.3490 / 0.2760 | +1.03% | yes | 0.5886 | uncalibrated |
| `XBOM:ADANIENT` | 2026-11-18 | random_walk | random_walk | yes | 0.3756 / 0.3503 / 0.2741 | -1.64% | no | 0.6384 | uncalibrated |
| `XNSE:RELIANCE` | 2026-11-04 | bootstrap | random_walk | no | 0.2975 / 0.4250 / 0.2775 | +0.42% | no | 0.7911 | uncalibrated |
| `XNSE:RELIANCE` | 2026-11-10 | bootstrap | random_walk | no | 0.2650 / 0.3850 / 0.3500 | +0.83% | no | 0.8110 | uncalibrated |

Aggregate results:

- Resolved forecasts: **5 / 5**.
- Directional accuracy: **2 / 5 = 40%**.
- Mean Brier: **0.6839**.
- Uniform three-class reference Brier: **0.6667**.
- Brier skill versus uniform: **approximately -0.026** (worse than uniform).
- Skill-gate abstentions: **3 / 5**. The three ADANIENT runs retained the baseline because no challenger demonstrated validation skill.
- Applied calibration: **uncalibrated in all five forecasts**. Training produced some `calibrated` calibration reports and some `rejected` reports, but `apply_calibration=false` means none was silently applied.

The two RELIANCE forecasts had tiny positive validation skill scores (0.0398 and 0.0474), but both lost directionally out of sample and had Brier scores worse than uniform. This is not evidence of forecasting skill. Five observations are far below any useful sample size; no accuracy, calibration, or profitability claim is made.

The daily forecast path also had a real data-selection defect: the forecast engine requested bars without an interval, so an instrument with recent 1-minute bars could return 720 intraday rows instead of daily rows. It now requests `interval="1d"`. A regression test (`test_intraday_bars_never_displace_the_daily_forecast_history`) pins the fix.

A second forecasting defect was found during batch evaluation: two forecasts for the same model/instrument/horizon resolving in one evaluation cycle produced the same performance-snapshot id and raised `UNIQUE constraint failed`. Snapshot identity now includes the resolved-outcome set and performance writes are idempotent. The regression test `test_two_same_model_forecasts_due_together_make_distinct_snapshots` passes.

---

## 7. Strategy comparison

The real-data comparison is a chronological replay of the existing EMA(5/13) 1-minute baseline versus the same entry admitted only when the multi-timeframe hierarchy returns a directional candidate. Both use a five-bar holding period, 7 bps all-in cost, and no parameter fitting.

| Metric | Baseline | Enhanced hierarchy |
|---|---:|---:|
| Decisions | 165 | 165 |
| Signals | 11 | 10 |
| Trades | 11 | 2 |
| Win rate | 18.18% | 50.00% |
| Profit factor | 0.0955 | 0.0712 |
| Mean trade return | -0.0661% | -0.0149% |
| Cumulative return | -0.7245% | -0.0297% |
| Maximum drawdown | -0.7983% | -0.0320% |
| Sharpe per trade | -0.8393 | -0.8670 |
| False signals | 9 | 1 |
| Rejection/abstention rate | 0% | 93.94% |
| Mean decision latency | 0.181 ms | 8.398 ms |
| p95 decision latency | 0.235 ms | 11.411 ms |
| Average trade duration | 5 bars | 5 bars |

Chronological test-slice results were also negative for both: baseline 4 trades, 50% win rate, -0.1165% cumulative; enhanced 1 trade, 0% win rate, -0.0320% cumulative. The samples are far too small to support a superiority claim. The enhanced path did reduce trade count and false signals, but it also reduced exposure and had a worse profit factor. That is a description, not evidence of improvement.

**Forecast-assisted strategy (C):** the catalog contains `forecast_assisted`, but the fixture forecast produced `p_up=0.0`, `p_down=0.0`, `p_sideways=1.0` with `uncalibrated` status. Its default entry gate requires a positive predicted return and `p_up >= 0.55`. No forecast-assisted paper trade was forced, so there are **0 forecast-assisted trades** and no performance comparison. This is correct abstention, not a hidden negative result.

**Strategy-comparison conclusion:** insufficient sample size. Do not claim that the enhanced multi-timeframe strategy or the forecast-assisted strategy is superior.

---

## 8. Paper execution results

The fixture lifecycle used a fresh copy of the financial stores, a deterministic 1-minute/5-minute/daily fixture for `XNSE:MTFVAL01`, the real strategy engine, NM-08 risk, and NM-10 paper execution.

| Stage | Result |
|---|---|
| Hierarchy | `BUY_CANDIDATE`, aligned, confidence 0.7000, all layers available |
| Strategy | `trend_sma` MA3/MA9 upward cross |
| Risk | Approved by `short_term@nm08-risk-1.0`; 10% notional |
| Proposal | `add_position`, notional ₹2,000.00, reference ₹100.60 |
| Paper order | Buy 19.8807 units, filled |
| Fill | ₹100.69; reference ₹100.60; slippage ₹1.79 (0.0895%); fees ₹0.78; net cash ₹2,002.57 |
| Duplicate forward | HTTP 200 with `replayed=true`; no second order (order count stayed 1) |
| Oversized paper-order assessment | Rejected; no order was created (count stayed 1) |
| Exit | `remove_position`, 19.8807 units, reference ₹90.00, approved |
| Exit fill | ₹89.92; reference ₹90.00; slippage ₹1.59 (0.0889%); fees ₹0.70; net cash ₹1,786.97 |
| Final position | None |
| Final equity | ₹19,784.40 |
| Realized P&L | **-₹215.61 (-1.08%)** |

The buy fill cost ₹2,002.57 including fees. The sell returned ₹1,786.97 after fees. The realised loss therefore includes ₹3.38 of modelled slippage and ₹1.48 of fees, in addition to the adverse price move.

### Controlled database-write interruption

After NM-08 approved the entry but before the normal forward, the harness opened a **separate connection to the copied `novamarket.sqlite3` and held `BEGIN IMMEDIATE`** while the HTTP forward request ran. This is a real SQLite write lock, not a mocked exception.

| Check | Observed result |
|---|---|
| Forward during lock | HTTP `500`, `{"detail":"database is locked"}` |
| Orders before / during | 0 / 0; no order carried the proposal id |
| Fills before / during | 1 / 1; the pre-existing row came from the copied source store, not this drill |
| Ledger before / during | 1 entry / ₹20,000.00 funding; unchanged |
| Holdings / wallet / portfolio | Unchanged |
| Proposal status during | `approved`; not advanced to execution |
| Database integrity | 15 / 15 copied SQLite files `ok`; zero foreign-key violations |
| Post-release retry | HTTP `200`, one new order filled, one new fill, proposal `forwarded_to_execution`, reconcile `ok` |
| Verdict | `no_unauthorized_order=true`; `ledger_corruption_free=true`; fail-closed and recoverable |

The drill is recorded in `database_interruption.json` and under `stages.database_interruption` in `paper_lifecycle.json`. The lock was released before the retry; the retry did not replay a phantom order, did not duplicate a fill and did not create a second position. The normal lifecycle then continued with the same -₹215.61 realised result.

An exit-sizing defect was found here: the first exit proposal used the stale holding market value (₹2,000 at the old ₹100.60 reference) and requested 22.2222 units against 19.8807 held. NM-10 correctly refused the oversell. The strategy engine now sizes an EXIT from the held quantity and the exit signal's reference price; the corrected exit requested 19.8807 units and filled. Regression tests for the corrected exit and the bounded-notional rule pass.

**Important:** all prices, fills, fees, and P&L above are fixture-based. They are not live-market results and do not demonstrate profitability.

---

## 9. Portfolio and ledger reconciliation

| Item | Value |
|---|---:|
| Starting capital | ₹20,000.00 |
| Funding deposit | ₹20,000.00 (external flow) |
| Cash after entry | ₹17,997.43 |
| Invested after entry | ₹2,000.00 |
| Unrealized P&L after entry | -₹2.58 |
| Final cash | ₹19,784.40 |
| Final invested capital | ₹0.00 |
| Final realized P&L | -₹215.61 |
| Final unrealized P&L | ₹0.00 |
| Final total equity | ₹19,784.40 |
| Maximum drawdown | -1.08% |
| Performance observations | 4 (risk-adjusted ratios withheld; fewer than 20 required) |

Reconciliation results:

| Check | Result |
|---|---|
| Portfolio reconcile after fill | `ok=true`, funded ₹20,000.00, realized book/history ₹0.00, no discrepancies |
| Wallet reconcile after fill | stored ₹17,997.43 = ledger ₹17,997.43; difference ₹0.00; 48 entries / 24 transactions checked |
| Portfolio reconcile after exit | `ok=true`, realized book/history -₹215.61, no discrepancies |
| Wallet reconcile after exit | stored ₹19,784.40 = ledger ₹19,784.40; difference ₹0.00; 52 entries / 26 transactions checked |
| Restart reconcile | identical `ok=true`, -₹215.61, no discrepancies |
| Restart wallet reconcile | difference ₹0.00; 52 entries / 26 transactions checked |

The ledger records the ₹20,000 funding transfer separately from the paper buy, buy fees, paper sell, and sell fees. The portfolio performance endpoint reports the deposit under `external_flows` and explicitly states that capital deposited or released is not counted as investment return.

**NovaCoin:** each wallet ledger row carries `nova_coin` equal to its INR amount; the reconciliation endpoint compares the NovaCoin balance to the ledger and reports zero difference. The observed ledger is therefore consistent with **1 NC = ₹1** for this run. This is an accounting-equivalence check, not a market-price claim.

---

## 10. Risk-engine verification

- The entry was approved by NM-08 as `short_term@nm08-risk-1.0` with 10% invested, 90% cash, and no constraint breach.
- The exit was approved after the correction, at ₹1,789.26 notional for 19.8807 units.
- An artificial oversized `paper_order` assessment (₹1,000,000 notional) was **rejected**. The recorded explanation lists seven triggered constraints: maximum position weight, maximum sector weight, maximum invested fraction, minimum cash fraction, maximum volatility, aggregate sector weight, and turnover.
- The rejected assessment did **not** create a paper order: the portfolio order count was 1 before and 1 after.
- A duplicate forward returned the original execution with `replayed=true`; the order count remained 1. No duplicate fill or position was created.
- No strategy bypassed NM-08: the strategy proposal carries the risk decision id and the execution order carries the same id.
- A real SQLite write lock during the approved forward produced HTTP 500 `database is locked`; the forward created no order, fill, holding or ledger entry, left the proposal approved, and succeeded exactly once after the lock was released. The database-interruption drill therefore exercised the failure boundary without granting execution authority outside NM-08/NM-10.
- MTF authority is evidence-only: its API contract reports `executes=false`, `approves=false`, with NM-08 as risk authority and NM-10 as execution authority. The hierarchy is declared to apply only to `short_term / day_trading` and explicitly preserves `medium_term / swing_trading` and `long_term / long_term`; no medium- or long-term portfolio was gated by it in this run.

The risk engine did its job in these drills. The oversized and duplicate workflows failed safely rather than creating an unauthorized trade.

---

## 11. Operational reliability

### Observed runtime health

`/market/autonomy/reliability` returned `ok=false` with state `degraded`. The reasons were explicit:

- monitoring runtime mode is disabled;
- the runtime lease is not valid;
- the AT-01 runtime is not enabled;
- the latest AT-02 monitoring cycle is deferred;
- autonomous order forwarding is inactive outside autonomous mode.

`/market/autonomy/status` reports `trading_implemented=false`, `orders_possible=false`, `exit_supervision_implemented=false`, and `learning_promotion_implemented=false`. These are not failures hidden by the test; they are the current limits of the system. Continuous autonomous operation was not demonstrated.

The last stored monitoring cycle was a weekend `session_check` deferred with `OUTSIDE_TRADING_SESSION`; it correctly refused to fabricate a trading session. The last completed decision run was 2026-11-20 with 840 evaluations, 840 signals, 42 proposals, 36 rejected, and 0 execution intents.

### Reliability drills

| Drill | Evidence | Result |
|---|---|---|
| Graceful shutdown and restart | Copied app closed, reopened; portfolio, holdings, orders, reconciliation queried | State preserved; holdings empty; equity ₹19,784.40; reconcile clean |
| Duplicate order prevention | Duplicate proposal forward | Replayed original; one buy order only |
| Stale/out-of-order/future bars | Future-bar regression test; MTF suite | 32 MTF tests pass; future bars excluded |
| Provider failure | Existing provider/market-data tests in full suite | Tests pass; no fabricated quote accepted |
| Scheduler interruption / restart | AT-02/AT-03/AT-04/AT-05/AT-06 suite | 216 reliability tests pass |
| Database integrity | `PRAGMA integrity_check` and foreign-key check on 15 stores | All 15 `ok`; zero foreign-key violations |
| Database-write interruption | Write lock held on the copied financial store during an approved forward | HTTP 500 `database is locked`; no order/fill/ledger mutation; retry after release filled exactly once; integrity still `ok` |
| Repeated signals | Replay and strategy tests | Repeated evaluation replays signal; cooldown suppresses duplicates |
| Memory / long-running use | 165-decision replay with `tracemalloc` | 3.0 MB peak traced memory; not a multi-hour endurance test |

Targeted test totals:

- multi-timeframe: **32 passed**
- forecasting: **80 passed, 35 subtests**
- strategies: **42 passed**
- combined MTF/forecast/strategy/execution/risk/portfolio/wallet: **352 passed, 57 subtests**
- scheduler/autonomy/AT-02–AT-06: **216 passed**

### Reliability failures observed

During harness teardown, two audit-log writes failed with Windows `[WinError 5] Access is denied` while renaming the temporary audit file to `audit.jsonl` in the OneDrive-synced workspace. The financial state, orders, fills, and ledger were unaffected; the operation continued. This is recorded as an operational limitation, not hidden. A production deployment should keep audit logs outside a synchronised consumer folder and alert on audit-write failure.

The older `tmp/audit/e2e_paper_chain.py` harness failed because it seeds only daily bars and now encounters the correctly applied multi-timeframe gate; that is an outdated harness assumption, not a product failure. The new validation harness seeds the full hierarchy.

---

## 12. Performance and resource consumption

| Measure | Value |
|---|---:|
| Real replay decisions | 165 |
| Real replay wall time | 1.61 s |
| Peak traced memory | 3.00 MB |
| Baseline mean/p95 decision latency | 0.181 / 0.235 ms |
| Enhanced mean/p95 decision latency | 8.398 / 11.411 ms |
| Paper lifecycle wall time (approx.) | 30 s including app startup and two restarts |
| Full baseline suite (pre-fix) | 1,955 passed, 2 skipped, 447 subtests in 582.29 s |
| Final suite (post-fix) | **1,959 passed, 2 skipped, 447 subtests in 768.73 s** |

The enhanced hierarchy is roughly 46× slower per decision than the EMA baseline, but remains under 12 ms at p95 in this environment. No multi-hour memory-growth measurement was performed; the 3 MB figure is a replay peak, not proof of long-run stability.

---

## 13. Reinforcement-learning evaluation status

- RL is implemented but opens only on first use: `simulated=true`, `advisory_only=true`, `production_authority=false`.
- The stored training policy is `experimental=true`, `advisory_only=true`, `activation_available=false`.
- The policy's dataset provenance records chronological separation and `production_authority=false`.
- Source constants keep automatic promotion disabled: `AUTOMATIC_PROMOTION = False` in `forecast_intelligence/models.py` and `prediction/models.py`; the training isolation map reports `automatic_policy_promotion: False`.
- No RL policy submitted a paper order. No RL improvement is claimed.
- The system's existing strategies were compared with the hierarchy, but not with an RL policy, because there was no out-of-sample RL evidence and no isolated policy-simulation execution evidence in this run.

---

## 14. Failures and recovery

| Finding | Impact | Resolution / status |
|---|---|---|
| Future observations were visible to historical MTF decisions | Look-ahead leakage in replay | Fixed in `MultiTimeframeAnalyzer._series`; regression test added; 32 MTF tests pass |
| Daily forecast query could read 1-minute rows | BSE/ADANIENT reported 0 usable daily bars at a historical cutoff | Fixed by requesting `interval="1d"`; regression test added |
| Duplicate forecast performance snapshot ids | `UNIQUE constraint failed` when two same-model forecasts resolved together | Snapshot identity includes resolved outcomes; write is idempotent; regression test added |
| EXIT proposal used stale market value instead of held quantity | Oversell request (22.2222 vs 19.8807) was refused by NM-10, so the first exit did not close | Fixed to size EXIT from held quantity × exit reference price; regression test added; corrected exit filled |
| Audit log rename denied on OneDrive workspace | Two audit records were not written | Reported; financial state unaffected; deployment should store audit logs outside a synchronised folder |
| No live session available | Cannot validate live paper trading | Market closed; next open 2026-11-23; no live claim made |
| Autonomy runtime disabled | No continuous autonomous cycle | Explicit API state; manual harness only |
| Sparse 1m data and no native 5m bars | Only one real instrument produced candidates | Clearly labelled derived/fixture data; safe abstention elsewhere |
| SQLite write lock during an approved paper forward | Forward returned HTTP 500 `database is locked` | No order, fill, holding, wallet or ledger mutation; proposal stayed approved; retry after release filled exactly once |

No failed workflow created an unauthorized trade. The oversized risk assessment increased no order count; the duplicate forward replayed; the first bad exit was refused by execution and left the position open until the corrected exit.

---

## 15. Remaining limitations

1. **No live-market paper trading.** The market was closed for the entire validation window. All real-data work is historical replay.
2. **No complete live session.** A complete-session reliability run could not be performed. Only two historical sessions and one fixture lifecycle were covered.
3. **Sparse intraday data.** Real 1-minute coverage is 66.67% of a 375-minute session, only three instruments have intraday history, and there are no native 5-minute bars.
4. **Derived 5-minute bars.** Multi-timeframe replay uses only buckets built from five contiguous real 1-minute bars. This is honest but not equivalent to provider-native 5-minute data.
5. **Small samples.** 165 hierarchy decisions, 11 baseline trades, 2 enhanced trades, and 5 resolved forecasts are not enough for statistical superiority, calibration, or profitability claims.
6. **No continuous scheduler.** The stored market configuration has ingestion disabled and the autonomy runtime stopped. The harness is manual, not a 24/7 paper-trading process.
7. **Autonomy is not fully implemented.** `trading_implemented=false`, `exit_supervision_implemented=false`, and `learning_promotion_implemented=false` in the runtime status.
8. **Forecasting has no demonstrated edge.** Mean Brier 0.6839 is worse than the uniform reference 0.6667; directional accuracy was 2/5; three of five runs abstained to the baseline.
9. **Calibration is not applied.** Calibration reports exist, but `apply_calibration=false`; all five served forecasts were `uncalibrated`.
10. **No forecast-assisted trade.** The default gate correctly refused a forecast with zero up-probability; no comparison sample exists.
11. **No long-duration endurance run.** Memory was measured for a 1.6-second replay, not for hours of operation.
12. **Database-failure coverage is a controlled lock drill, not a corruption drill.** A real SQLite write lock during an approved forward was injected and failed closed, but disk-full, read-only filesystem, torn-write, WAL corruption, process kill during a commit and hardware failure were not tested.
13. **Fixture P&L is not evidence of profitability.** The -1.08% result is a deterministic synthetic path, not a market result.
14. **Audit logging is not durable on this workspace.** Two Windows/OneDrive rename failures occurred; the system continued without them.

### Final suite result

The complete suite was run after the fixes and test additions:

```text
1959 passed, 2 skipped, 883 warnings, 447 subtests passed in 768.73s (0:12:48)
```

The log is `tmp/audit/paper_validation_final_suite.log`; the exit status was `0`. The database-interruption drill was run separately with `--only paper` after this harness-only change; the full suite was not rerun for that documentation/harness update.

---

## 16. Readiness assessment

### Software reliability

**CONDITIONAL PASS for the tested workflows; not a continuous-runtime pass.** The tested paper chain (hierarchy → strategy → NM-08 → NM-10 → fill → valuation → ledger → exit → restart) completed, duplicate forwards replayed, oversized risk was rejected, and state survived restart. A controlled SQLite write lock during the approved forward failed closed with no order or ledger mutation and recovered exactly once after release. The **full suite passed: 1,959 passed, 2 skipped, 447 subtests in 768.73 s**, including the 32 MTF, 80 forecasting, 42 strategy, 352 combined core, and 216 reliability tests cited above. However, the runtime reports `ok=false` degraded because autonomy is stopped, and an audit-write failure occurred on this filesystem. The software is reliable enough for controlled paper drills, not yet for unattended continuous operation.

### Market-data reliability

**FAIL for live readiness; PASS for labelled historical replay.** Real daily history covers 149 instruments through 2026-11-19. Real intraday history covers only three instruments at 66.67% of a session, with 14 gaps >120s on the latest ADANIENT session. There are no native 5-minute bars. The system correctly labels stale, delayed, and closed-market observations and refuses to invent a session. It has not demonstrated reliable live ingestion.

### Trading-strategy effectiveness

**NOT DEMONSTRATED.** Baseline: 11 trades, 18.18% win rate, -0.72% cumulative, 0.0955 profit factor. Enhanced: 2 trades, 50% win rate, -0.03% cumulative, 0.0712 profit factor. Both are negative and the samples are tiny. The enhanced path reduced false signals and exposure but did not produce evidence of superior returns. No superiority claim is warranted.

### Forecasting effectiveness

**NOT DEMONSTRATED.** Five resolved forecasts, 40% directional accuracy, mean Brier 0.6839 versus 0.6667 uniform, negative Brier skill, and three skill-gate abstentions. Calibration is report-only and no calibrated probability was served. The forecasting system is honest about abstention and does not force predictions, but it has not shown predictive skill.

### Risk-control reliability

**PASS for the tested controls.** NM-08 approved the legitimate entry and exit, rejected an oversized order with seven explicit breaches, and did not create an order for the rejected request. NM-10 refused an invalid oversell, accepted the corrected exit, and prevented a duplicate fill. The approved forward under a real SQLite write lock returned HTTP 500 without creating an order, fill or ledger entry, and the retry after release filled exactly once. No tested failure produced an unauthorized trade or a corrupted ledger. The exit-sizing defect was a real control-path bug, now fixed and regression-tested.

### Sustained paper-trading readiness

**NOT ESTABLISHED.** One fixture lifecycle is not sustained paper trading. The system has not run through a complete live market session, repeated real sessions, or an unattended multi-hour schedule. Ingestion and autonomy are disabled, and the runtime is degraded. The correct verdict is “ready for supervised paper drills,” not “ready for sustained autonomous paper trading.”

### Live-market readiness

**NOT READY.** `paper_only=true`, `live_trading=false`, `broker_connected=false`, `release_gate=closed`, `operator_authorized=false`, and the autonomy runtime is disabled. No broker adapter or live order path was exercised. No live-trading claim is made.

---

## Evidence index

| Artifact | Content |
|---|---|
| `tmp/continuous_paper_validation/run_validation.py` | reproducible isolated harness |
| `tmp/continuous_paper_validation/database_inventory.json` | store integrity, bar/observation inventory, condition counts |
| `tmp/continuous_paper_validation/status.json` | API safety, runtime, reliability, RL/learning/adaptive flags |
| `tmp/continuous_paper_validation/real_multitimeframe.json` | baseline vs hierarchy metrics and resources |
| `tmp/continuous_paper_validation/real_multitimeframe_decisions.jsonl` | all 165 hierarchy decisions |
| `tmp/continuous_paper_validation/forecasts.json` | train/generate/resolve records and Brier calculations |
| `tmp/continuous_paper_validation/paper_lifecycle.json` | entry, fills, database interruption, duplicate, oversized risk, exit, ledger, reconciliation, restart |
| `tmp/continuous_paper_validation/database_interruption.json` | locked-store drill, before/during state, integrity, retry recovery and pass/fail checks |
| `tmp/continuous_paper_validation/evidence.json` | top-level evidence summary including `database_interruption` |
| `tmp/continuous_paper_validation/paper_interruption_run.log` | `--only paper` run log and exit status |
| `tmp/audit/paper_validation_baseline_suite.log` | pre-fix baseline suite |
| `tmp/audit/paper_validation_final_suite.log` | post-fix complete suite |

**Validation status:** completed as an operational validation exercise. The result is not a live-trading authorization, not a profitability claim, and not a PI-25 implementation.
