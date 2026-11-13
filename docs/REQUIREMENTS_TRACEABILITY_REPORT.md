# NovaMarket Requirements Traceability Report

**Audit date:** 2026-11-11  
**Baselines:** NM-01–NM-14 accepted 9 November 2026; AT-01–AT-06 accepted before this re-audit  
**Method:** original milestone objectives → implementation modules → API/UI location → test performed → current result  
**Overall application-level result:** **PARTIAL** (accepted baseline intact; live lifecycle and continuous autonomy evidence incomplete)

---

## 1. Reading this report

The result column distinguishes:

- **PASS** — the accepted implementation is present, the current UI/API path works at the level tested, and no regression was found;
- **PASS (accepted), runtime PARTIAL** — the implementation and deterministic tests pass, but the current closed-market/disabled-runtime state prevented a live end-to-end demonstration;
- **PARTIAL** — the surface works but this pass did not fully verify every value or interaction;
- **BLOCKED** — an external state (market closed, no operator token, no position) prevents the test.

The original objective wording is preserved rather than replaced with newly invented goals. Where an original requirement cannot be independently verified in the current state, that is stated explicitly.

---

## 2. NM-01 – NM-14 traceability

| NM phase | Original objective | Implementation | UI/API location | Test performed | Result | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| NM-01 | Architecture and contracts | `api/app.py`, `api/models.py`, `api/route_consumers.py`, subsystem packages | `/market/contracts`, `/market/status`; Health, Settings | 26-route sweep; contract/API-reference tests; `mypy` | **PASS** | `tmp/full_qa/route_sweep.json`; `tests/test_api_reference_sync.py`; 573-source mypy clean |
| NM-02 | Market-data reliability | `marketdata/`, market service, provider adapters | `/market/overview`, `/market/instruments`, `/market/screener`; Market, TradingWatch | Route/render; RELIANCE search; NSE filter; sort; provider tests in suite | **PASS** (provider reliability caveat) | `tmp/full_qa/route_sweep.json`; current market trace; full suite |
| NM-03 | NovaCoin wallet and deterministic ledger | Wallet, ledger, transaction services | `/market/wallets*`; Wallet, Transactions, Ledger | Create/deposit/withdraw/reconcile; wallet filters; ledger tests | **PASS** | `.ui_audit/write_flows2.json`; full suite |
| NM-04 | Evidence-backed market event radar | Radar/event service and source registry | `/market/events`; Events | Route/empty-state; event tests | **PASS** | route sweep; full suite |
| NM-05 | Autonomous asset discovery and watchlists | Discovery, candidate scoring, watchlist service | `/market/discovery*`, `/market/watchlists*`; Discovery, Watchlists | Create/add/remove/persistence; discovery route | **PASS** | `.ui_audit/write_flows2.json`; route sweep |
| NM-06 | Probabilistic forecasting | Forecast engine, calibration/data-quality modules | `/market/forecasts*`; Forecasts | Stored forecast table; model/data-quality tests | **PASS** (no new calibration claim) | route sweep; full suite |
| NM-07 | Three-horizon portfolio manager | Portfolio, allocation, accounting services | `/market/portfolios*`; short/medium/long, Analytics | Pause/resume; empty-book enablement; portfolio tests | **PASS** | targeted workflow; `.ui_audit/write_flows2.json` |
| NM-08 | Independent risk engine | Risk policies, decision engine, stress/alerts | `/market/risk*`; Risk | Stress dialog; risk alerts; risk tests | **PASS** | targeted modal; full suite |
| NM-09 | Trading strategies | Strategy catalog, signal/config services | `/market/strategies*`; Strategies | Catalog/evaluation surface; strategy tests | **PASS** | route sweep; full suite |
| NM-10 | Realistic paper execution | NM-10 order, fill, fee, slippage, accounting services | `/market/execution*`; Paper | Surface; disabled-without-approval probe; execution tests | **PASS (accepted), current lifecycle PARTIAL** | `postfix_closure_restarted2.log`; full suite; 0 orders |
| NM-11 | Reproducible backtesting | Replay engine, leakage guard, metrics | `/market/backtests*`; Backtesting | Existing result surface; backtest tests | **PASS (surface); fresh replay not run** | route sweep; full suite |
| NM-12 | Shadow mode | Shadow service, session and analytics isolation | `/market/shadow*`; Shadow | Create-dialog validation; shadow tests | **PASS (surface); no session retained** | targeted workflow; full suite |
| NM-13 | Controlled adaptive learning | Learning governance, evidence, candidate approval | `/market/learning*`; Learning | Governance/approval queue; learning tests | **PASS; no automatic promotion** | route sweep; full suite |
| NM-14 | Integrated workspace and final acceptance | App shell, all routes, navigation, build | All 26 routes | Route sweep; build; a11y; security; regression | **PASS** | `route_sweep.json`; 1,599-module build; 978 tests |

### NM notes

- NM-02 remains subject to the documented free-provider rate limits, delayed data and coverage limits. No paid provider was introduced.
- NM-10 and NM-11 results are not evidence of real-money execution. Paper fills are NM-10 simulations; backtests are historical replays.
- NM-13 learning remains advisory; no model was promoted during this pass.

---

## 3. AT-01 – AT-06 traceability

| AT phase | Original objective | Implementation | UI/API location | Test performed | Result | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| AT-01 | Runtime, durable lease and operating modes | Autonomy runtime/service, lease store, mode state machine | `/market/autonomy/start`, `/pause`, `/resume`, `/stop`, `/status`; Autonomy | Route; locked controls; lease/recovery tests | **PASS (accepted); current runtime disabled** | `AUTONOMOUS_TRADING_AT01_REPORT.md`; full suite |
| AT-02 | Session-aware autonomous monitoring | Monitoring scheduler, verified exchange calendar, fair rotation | `/market/autonomy/monitoring*`; Autonomy | Route/status; monitoring tests; no current cycle | **PASS (accepted); runtime PARTIAL** | `AUTONOMOUS_TRADING_AT02_REPORT.md`; full suite; market closed |
| AT-03 | Autonomous strategy evaluation and NM-08 decisions | Decision pipeline; NM-09 evaluation → NM-08 | `/market/autonomy/decisions*`; Autonomy | Gated controls; decision/risk tests | **PASS (accepted); no current proposal** | `AUTONOMOUS_TRADING_AT03_REPORT.md`; full suite |
| AT-04 | Automatic paper execution through NM-10 | Execution intents/journal, idempotency, NM-10 forwarding | `/market/autonomy/executions*`; Autonomy, Paper | Surface; 0 orders; execution/idempotency tests | **PASS (accepted); current lifecycle not executed** | `AUTONOMOUS_TRADING_AT04_REPORT.md`; full suite |
| AT-05 | Protective exits | Stop-loss, target, trailing, high-water persistence, partial fills, precedence | `/market/autonomy/exits*`, configurations/positions/learning; Autonomy | Exit surface; no positions; exit tests | **PASS (accepted); live exit PARTIAL; long-only target/trailing** | `AUTONOMOUS_TRADING_AT05_REPORT.md`; `docs/AT05_PREREQUISITE_CLOSURE.md`; full suite |
| AT-06 | Reliability, reconciliation and recovery | Reliability/recovery endpoints using existing reconciliation and execution services | `/market/autonomy/reliability`, `/market/autonomy/recover`; Autonomy reliability line | API/UI surface; recovery/idempotency tests; no live recovery | **PASS (accepted); live recovery not demonstrated** | `AUTONOMOUS_TRADING_AT06_REPORT.md`; `AUTONOMOUS_TRADING_FINAL_ACCEPTANCE_REPORT.md`; full suite |

### AT notes

- Recovery is designed to return the runtime to **disabled**, never automatically resume autonomy.
- The kill switch stops new entries; it does **not** cancel already-submitted NM-10 paper orders. Reconciliation remains authoritative.
- AT-05 target and trailing exits remain **long-only**. Short-position target/trailing exits were not implemented or claimed.
- No AT-07 was created or authorized.
- The current Autonomy route correctly leaves mutating controls locked without the dedicated operator token.

---

## 4. Requirement coverage summary

| Baseline | Requirements traced | Result |
| --- | ---: | --- |
| NM-01–NM-14 | 14/14 | **PASS** for accepted implementation; NM-10 live lifecycle and NM-11 fresh replay remain partial evidence |
| AT-01–AT-06 | 6/6 | **PASS** for accepted implementation/tests; continuous operational demonstration remains PARTIAL |
| Combined application-level claim | 20/20 traced | **PARTIAL** due to current external-state and exhaustive-control gaps |

The traceability requirement is satisfied: every milestone has an implementation location, a UI/API location, a test performed, a result and evidence. The result is not inflated to PASS where the current state prevented execution.

---

## 5. Original objectives not redefined

No requirement was replaced by a new objective. The following are explicitly out of scope and were not introduced:

- live trading or broker integration;
- real-money orders or deposits;
- paid market-data subscriptions;
- short-position target/trailing exits;
- automatic learning promotion;
- forced end-of-day liquidation;
- target-weight rebalancing;
- AT-07.

---

## 6. Evidence index

- `docs/NM14_FINAL_ACCEPTANCE_REPORT.md` — accepted NM-14 baseline.
- `AUTONOMOUS_TRADING_AT01_REPORT.md` … `AUTONOMOUS_TRADING_AT06_REPORT.md` — accepted AT reports.
- `AUTONOMOUS_TRADING_FINAL_ACCEPTANCE_REPORT.md` — AT integration verdict.
- `docs/AT05_PREREQUISITE_CLOSURE.md` — AT-05 prerequisite closure.
- `tmp/full_qa/route_sweep.json` — 26-route current load evidence.
- `tmp/full_qa/a11y_results_26_final.json` — 26-route accessibility evidence.
- `.ui_audit/qa_full_regression2.log` — consolidated current regression.
- `.ui_audit/write_flows2.json` — isolated write workflows.
- `.ui_audit/security_results.json` — 9/9 security checks.
- `docs/FULL_APPLICATION_QA_REPORT.md` — current findings and limitations.
