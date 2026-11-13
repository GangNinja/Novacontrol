# NovaMarket Autonomous Paper Trading Audit

**Audit date:** 2026-11-11  
**Question:** Does NovaMarket genuinely connect market data → discovery → strategy evaluation → NM-08 risk → NM-10 paper execution → position monitoring → protective exit → ledger, and does it operate that chain without manual intervention?  
**Short answer:** **The autonomous paper-trading architecture is implemented and accepted through AT-01–AT-06. Continuous autonomous operation is not demonstrated in the current runtime.**  
**Runtime verdict:** **NOT DEMONSTRATED / OPERATIONALLY BLOCKED** — the runtime is disabled/stopped, the market is closed, and there are no positions.

> **2026-11-13 closure:** NM-R1-05 is fixed and the dedicated operator token is configured in the isolated QA environment. The runtime remains disabled at rest and no live continuous cycle was forced; the operational limitation is unchanged.

---

## 1. Executive answer

NovaMarket is no longer the NM-14-only manual paper workbench described by the archived audit. AT-01–AT-06 added:

- a durable runtime lease and four operating modes;
- session-aware autonomous monitoring with a verified exchange calendar;
- NM-09 strategy evaluation and NM-08 risk decisions;
- AT-04 forwarding of only NM-08-approved entries to NM-10 paper execution;
- AT-05 protective stop-loss, target and trailing exits;
- AT-06 reliability, reconciliation and recovery.

The current application also exposes the Autonomy page, with all mutating controls locked unless a dedicated operator token is configured and supplied.

However, the current runtime state is:

| Signal | Current observation | Interpretation |
| --- | --- | --- |
| `mode` | `disabled` | No autonomous scans or entries |
| `runtime_state` | `stopped` | No active cycle |
| Lease | not held / invalid / no owner | No runtime owns the autonomy lease |
| `paper_only` | `true` | Paper only |
| `live_trading` | `false` | No live trading |
| `broker_connected` | `false` | No broker path |
| Paper orders | **0** | No order was generated in this state |
| Paper fills | **0** | No fill was generated |
| Open positions | **0** | No position for AT-05 to supervise |
| Shadow sessions | **0** | No current observation |
| Learning candidates | **0** | No promotion |
| Market session | closed | No verified live session to trade |

Therefore the correct claim is: **the lifecycle exists and is covered by accepted tests; the application has not been observed running it continuously in this pass.**

---

## 2. Lifecycle verification

| Stage | Implementation / authority | Current runtime evidence | Verdict |
| --- | --- | --- | --- |
| Market data | Existing NM-02 market-data service | Market session closed; no fresh eligible session | **BLOCKED by market state** |
| Opportunity discovery | AT-02 monitoring, fair rotation, 20-instrument cap | Runtime disabled; scheduler not running | **Not executed** |
| Strategy evaluation | NM-09 strategy catalog/evaluator | No current monitoring cycle | **Not executed** |
| Signal / proposal | AT-03 decision pipeline | No proposals recorded | **Not executed** |
| Risk assessment | NM-08 sole risk engine | No current proposal | **Not executed** |
| Paper execution | NM-10 sole execution authority | 0 orders / 0 fills | **Not executed** |
| Fill/accounting | NM-10 fill, fee, slippage and ledger services | No fill in current state | **Not executed** |
| Position monitoring | Existing portfolio + AT-05 supervision | No open position | **Not executed** |
| Protective exit | AT-05 → NM-08 → NM-10 | No position/exit | **Not executed** |
| Reconciliation | NM-10 reconciliation + AT-06 reliability | 0 orders; no pending reconciliation | **No live reconciliation to observe** |
| Learning evidence | Advisory NM-13 evidence | 0 learning candidates | **No evidence generated** |

The table is deliberately not filled with “PASS” from unit tests. Tests prove the code paths; they do not prove a current continuous cycle.

---

## 3. Operating modes and authorization

| Mode | Intended permitted behaviour | Current verification |
| --- | --- | --- |
| `disabled` | No autonomous scans, proposals, entries or exits | Current state; controls fail closed |
| `monitoring` | Discovery and NM-08 decision recording; **no order submission** | Backend tests; not live-started |
| `autonomous` | Only NM-08-approved entries are forwarded through NM-10 | Tests; not live-started |
| `paused` | No new entries; permitted protective exits and reconciliation remain active | Tests; not live-started |

Authorization is server-side and separate from ordinary API authentication: `X-Autonomy-Operator-Token` must match `NOVACONTROL_AUTONOMY_OPERATOR_TOKEN`. The UI keeps the token in page memory only. The current environment has no token configured/presented, so Start, Forget token and Run monitoring cycle are correctly disabled.

The kill switch stops new entries. It does **not** cancel already-submitted NM-10 orders; those must be reconciled through existing execution state.

---

## 4. AT-05 protective exits

Verified implementation paths:

- stop-loss from the horizon's `stop_loss_fraction`;
- optional fixed target price;
- optional trailing stop percentage;
- durable target/trailing configuration and high-water mark;
- high-water-mark recovery after restart;
- trigger precedence: stop-loss → trailing-stop → target price;
- partial exit fills;
- duplicate-cycle suppression;
- pending exit-order reconciliation before retry;
- stale-data fail-closed;
- protective exits while runtime mode is paused.

Accepted limitations preserved in this audit:

- target and trailing exits are **long-only**;
- short-position target/trailing exits are not implemented;
- no forced end-of-day liquidation;
- no target-weight rebalancing;
- an already-submitted order is not cancelled by the kill switch.

No exit could be executed in this pass because the current portfolio has no open positions and the market is closed. The deterministic AT-05 tests are the evidence for the mechanisms; the current runtime is not evidence of a live exit.

---

## 5. AT-06 reliability and recovery

The application exposes:

- `GET /market/autonomy/reliability` — runtime/scheduler/lease health, pipeline and financial counts, reconciliation-pending state, learning completeness, unresolved records and failure-domain classification;
- `POST /market/autonomy/recover` — recovery using existing authoritative reconciliation and execution services, operator-authorized, idempotent and returning the runtime to **disabled**.

Recovery never automatically resumes autonomous activity. The current Autonomy page shows a reliability health line driven by `reliabilityData.health.state` and the paper pipeline counts; the full unresolved-record/recovery action surface is not yet presented as a dedicated panel (open NM-R1-05 from the prior register).

No recovery was needed or invoked in this pass because there were no interrupted orders, no lease and no positions.

---

## 6. Financial authority and paper-only safety

- NM-08 remains the sole risk authority.
- NM-10 remains the sole paper-execution authority.
- AT-04/AT-05 call existing NM-10 services; they do not write a duplicate ledger or portfolio row.
- Portfolio, position, valuation, ledger and accounting services remain authoritative.
- The current execution status reports `paper_only=true`, `live_trading=false`, `broker_connected=false`, 0 fills.
- No live broker, real-money order, paid data provider or AT-07 path exists.
- Learning remains advisory; no automatic promotion or risk-control modification was introduced.

The current runtime cannot demonstrate financial mutation because it has not generated an order. The financial-integrity evidence is therefore the accepted regression/AT tests and the isolated wallet/ledger workflows, not a fresh autonomous fill.

---

## 7. Test evidence

| Evidence | Result |
| --- | --- |
| Consolidated NovaMarket suite | **978 passed, 2 skipped, 991 subtests** in 384.91 s |
| Prior autonomy/AT-06 focused group | **41 passed** (accepted baseline) |
| Targeted API/UI suite | **139 passed, 284 subtests** |
| 26-route UI sweep | **26/26 clean** |
| Accessibility | 26 routes / 1,140 controls, 0 issue classes |
| Security | 9/9 checks |
| Current runtime status | disabled/stopped, no lease |
| Current execution | 0 orders / 0 fills |

The tests cover the AT-01–AT-06 mechanisms, including lease heartbeat, recovery lease release, mode gates, decisions, paper forwarding, protective exits, idempotency and recovery. The current application-level run did not produce a continuous cycle.

---

## 8. Can NovaMarket genuinely operate as an autonomous paper trader?

**Implemented, yes. Operationally demonstrated in this pass, no.**

A defensible statement:

> NovaMarket contains an accepted autonomous paper-trading pipeline through AT-01–AT-06. In the tested environment the runtime was disabled, the market was closed and there were no positions, so continuous autonomous discovery, entry, monitoring, exit and ledger reconciliation were not observed end to end. The individual stages and their authority boundaries are covered by tests; the application must not be described as “operationally proven autonomous” until a bounded, supervised cycle is run and recorded.

A single paper trade, a risk approval, or a passing test does not prove continuous autonomy. Conversely, the disabled state is not evidence that the pipeline is missing: it is the required fail-closed state at rest.

---

## 9. Outstanding limitations

1. No live/current continuous autonomy cycle.
2. No current paper order, fill, open position or protective exit.
3. No live provider outage, rate-limit or failover drill.
4. Target/trailing exits remain long-only.
5. Kill switch does not cancel already-submitted NM-10 orders.
6. Forced end-of-day liquidation and target-weight rebalancing are excluded.
7. Automatic learning promotion is excluded.
8. Live trading is excluded.
9. AT-07 is not authorized.
10. The current Autonomy page does not expose a dedicated recovery action/unresolved list (NM-R1-05).

---

## 10. Recommendation for operational validation

Run a bounded acceptance exercise in an isolated environment with a verified open session (or an explicitly labelled deterministic fixture):

1. configure the dedicated operator token and keep the API loopback-only;
2. start in `monitoring`; observe a session-gated monitoring cycle and NM-08 decisions without orders;
3. move to `autonomous` only for the bounded window;
4. observe one risk-approved AT-04 paper entry through NM-10, including order/fill/fee/slippage and ledger reconciliation;
5. supervise the open position; trigger one protective exit (stop-loss, target or trailing) and reconcile it;
6. confirm the runtime returns to `disabled`; verify no duplicate order or ledger entry;
7. preserve the evidence and do not promote learning or enable live trading.

Until that exercise is recorded, the overall autonomous-trading verdict remains **PARTIAL / NOT DEMONSTRATED**, not PASS.

---

## 11. Evidence index

- `AUTONOMOUS_TRADING_AT01_REPORT.md`
- `AUTONOMOUS_TRADING_AT02_REPORT.md`
- `AUTONOMOUS_TRADING_AT03_REPORT.md`
- `AUTONOMOUS_TRADING_AT04_REPORT.md`
- `AUTONOMOUS_TRADING_AT05_REPORT.md`
- `AUTONOMOUS_TRADING_AT06_REPORT.md`
- `AUTONOMOUS_TRADING_FINAL_ACCEPTANCE_REPORT.md`
- `docs/AT05_PREREQUISITE_CLOSURE.md`
- `tmp/full_qa/final_status.json` — current disabled/paper-only runtime snapshot
- `tmp/full_qa/route_sweep.json`
- `.ui_audit/qa_full_regression2.log`
- `docs/FULL_APPLICATION_QA_REPORT.md`
- `docs/BUG_FIX_REGISTER.md`
