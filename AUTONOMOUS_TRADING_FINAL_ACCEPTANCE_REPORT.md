# NovaMarket Autonomous Trading — Final Acceptance Report

**Scope:** AT-01 through AT-06  
**Project:** NovaMarket  
**Date:** 2026-11-11  
**Final verdict:** **ACCEPTED — PASS** (paper-only, documented limitations apply)

The complete autonomous trading architecture is accepted for strictly paper-only operation. All five acceptance gates pass. The system reuses NM-08 for risk, NM-10 for paper execution, and the existing portfolio, wallet, valuation and ledger services. No broker, live order, real-money execution, target-weight rebalancing, forced end-of-day liquidation or automatic learning promotion exists.

---

## 1. Milestone acceptance evidence

| Milestone | Document | Verdict |
|---|---|---|
| AT-01 — Runtime and safety shell | `docs/AT-01_IMPLEMENTATION_REPORT.md` | PASS |
| AT-02 — Session-aware monitoring | `docs/AUTONOMOUS_TRADING_AT02_REPORT.md` | PASS |
| AT-03 — Decision and NM-08 risk pipeline | `AUTONOMOUS_TRADING_AT03_REPORT.md` | PASS |
| AT-04 — Paper execution and reconciliation | `AUTONOMOUS_TRADING_AT04_REPORT.md` | PASS |
| AT-05 — Exit supervision and learning evidence | `AUTONOMOUS_TRADING_AT05_REPORT.md` | PASS |
| AT-06 — Recovery, observability and final acceptance | `AUTONOMOUS_TRADING_AT06_REPORT.md` | PASS |
| AT-05 prerequisite closure | `docs/AT05_PREREQUISITE_CLOSURE.md` | CLOSED |

AT-05 includes stop-loss, fixed target-price and percentage trailing-stop supervision, persisted target/trailing configuration, persistent long-position high-water marks, restart recovery, partial exits, duplicate suppression and advisory learning evidence.

---

## 2. AT-06 implementation summary

AT-06 added the missing reliability and acceptance surface without adding an engine or financial path:

- read-only AT-06 reliability aggregation (`autonomy_observability.py`);
- `GET /market/autonomy/reliability`;
- operator-gated `POST /market/autonomy/recover`;
- supervisor recovery over existing NM-10 pending-order recovery and per-exit reconciliation;
- authoritative order/fill totals in the execution store;
- failure-domain classification;
- API/documentation synchronization;
- 22 explicit lifecycle scenarios, including both AT-05 addendum trailing scenarios;
- measured performance, recovery and sustained duplicate-cycle evidence.

No accepted AT-01..AT-05 engine was forked or replaced.

---

## 3. Final architecture overview

```text
NovaControl authentication
        │
        ▼
AT-01 runtime + durable lease + one-cycle slot
        │
        ▼
AT-02 verified calendar → session → discovery (≤20) → monitoring
        │
        ▼
AT-03 strategy evaluation → signal → proposal → NM-08 assessment
        │
        ▼
AT-04 durable execution intent → NM-10 preflight/order/fill/reconcile
        │
        ▼
Portfolio + wallet + ledger accounting (authoritative)
        │
        ▼
AT-05 exit supervisor (stop-loss, target, trailing) → NM-08 → NM-10
        │
        ▼
Learning evidence (advisory, no promotion)
        │
        ▼
AT-06 reliability + safe recovery + acceptance
```

### Authority boundaries

| Concern | Sole authority |
|---|---|
| Risk | NM-08 |
| Paper order/fill execution | NM-10 |
| Portfolio/cash/positions/valuation | Existing portfolio and wallet engines |
| Accounting/ledger | Existing ledger implementation |
| Runtime mode/lease | AT-01 |
| Session/calendar/discovery | AT-02 |
| Strategy decision and proposal | AT-03 |
| Exit supervision | AT-05 |
| Reliability reporting | AT-06 read-only composition |

No duplicate risk engine, execution engine, portfolio engine, ledger, learning engine or scheduler exists.

---

## 4. Complete lifecycle

```text
Runtime
→ Session Monitoring
→ Discovery
→ Strategy Evaluation
→ Signal
→ Proposal
→ NM-08 Risk Assessment
→ Execution Intent
→ NM-10 Paper Order
→ Fill
→ Ledger
→ Position
→ Exit Supervision
→ Exit Proposal
→ NM-08 Risk Assessment
→ NM-10 Exit Order
→ Exit Fill
→ Position Reconciliation
→ Learning Evidence
```

The AT-06 suite verifies stable IDs and provenance from proposal through risk, intent, order, fill, position, exit, reconciliation and evidence.

---

## 5. Runtime-mode behaviour

| Mode | Discovery/monitoring | Strategy/risk | New entries | Protective exits | Reconciliation |
|---|---|---|---|---|---|
| `disabled` | No | No | No | No new supervision | No new action |
| `monitoring` | Yes | Yes | No | Evaluate only | Read-only |
| `autonomous` | Yes | Yes | Risk-approved only via NM-10 | Allowed via NM-08/NM-10 | Allowed |
| `paused` | Yes | No new entry decisions | Blocked | Allowed via NM-08/NM-10 | Allowed |

`paused` is distinct from `disabled`. It stops entries while keeping protective exits active. A restart always returns to disabled/stopped and never auto-resumes autonomous order submission.

---

## 6. Session and discovery behaviour

- Verified annual exchange calendars remain authoritative.
- 2026 verification does not imply 2027 coverage.
- Outside verified coverage the system fails closed.
- Weekdays are not assumed to be trading sessions; holidays are respected.
- The 15-minute market-data refresh floor is retained.
- The 20-instrument discovery batch cap is retained.
- Stale/missing/unusable market data cannot create an entry or exit order.
- Deterministic fixtures are separated from live-provider reliability claims.

---

## 7. Risk and proposal pipeline

- AT-03 creates a durable signal, proposal and NM-08 assessment.
- An entry can reach NM-10 only when the persisted proposal is risk-approved and its risk decision provenance matches.
- Rejected, expired, malformed or mismatched proposals fail closed with no order.
- Target/stop/trailing exit proposals also obtain a fresh NM-08 assessment and expiry before NM-10 submission.
- No autonomy code computes its own risk score or bypasses NM-08.

---

## 8. Paper-execution architecture

- AT-04 persists an execution intent before forwarding.
- NM-10 performs preflight, authoritative paper order creation, simulated fill, fees, settlement and reconciliation.
- A timeout or uncertain submission is reconciled before retry; no blind replay occurs.
- Proposal idempotency and durable intent uniqueness prevent duplicate orders.
- The paper clearing account is a simulated counterparty funded through the ledger, not real money and not a direct portfolio write.
- All executed values remain INR; NC = ₹1.

---

## 9. Protective-exit architecture

- Stop loss uses `average_cost × (1 − horizon stop_loss_fraction)`.
- Target uses a persisted fixed price above long-position average cost.
- Trailing uses persisted high-water mark × `(1 − trailing_stop_percent)`.
- Precedence is deterministic: `stop_loss → trailing_stop → target_price`.
- High-water marks never decrease for long positions and survive restart.
- Pending/partial exits are reconciled before another submission.
- Target/trailing remain long-only; short-position target/trailing is explicitly unsupported.
- Forced end-of-day liquidation is not implemented.

---

## 10. Learning evidence architecture

- Entry, risk, execution, fill, position, exit, realized outcome, fees and market provenance are linked.
- Partial outcomes remain labelled partial/incomplete.
- Duplicate evidence is prevented by the existing decision-scoped store.
- Evidence is advisory only: `auto_promoted=false`, `strategy_modified=false`.
- Learning cannot promote a model, change strategy parameters or bypass NM-08/NM-10.

---

## 11. Recovery architecture

Recovery is an orchestration of existing recovery doors, not a new engine:

1. AT-01 runtime recovery clears stale ownership and interrupts abandoned cycles.
2. AT-03 marks abandoned decision runs failed.
3. AT-04 `recover()` invokes NM-10 `recover_pending()` and reconciles durable intents.
4. AT-05 `recover()` reconciles durable pending/partial exit decisions.
5. AT-06 returns unresolved records explicitly and leaves the runtime disabled.

Recovery rules:

- inspect durable state first;
- reconcile with authoritative services;
- never blindly replay an uncertain financial operation;
- preserve idempotency keys;
- report unresolved inconsistencies;
- fail closed when financial state cannot be verified;
- never auto-resume autonomous mode.

Recovery measurement: 10.91 ms for the exercised partial-exit recovery, 1 record recovered and reconciled.

---

## 12. Observability architecture

`GET /market/autonomy/reliability` composes, without mutating or inventing:

- runtime mode/state, lease, heartbeat/expiry, active cycle, scheduler health;
- discovery/evaluation/signal/proposal/risk counts;
- execution intents/orders/fills and exit decisions/orders/fills;
- learning evidence count and incomplete evidence;
- pending orders, partial fills, open exits and reconciliation pending;
- failure domains and last failure category;
- recovery outcome and unresolved count;
- paper-only and authority boundaries.

The view is generated from existing status methods and stores. It does not create a second database or recompute financial values.

---

## 13. Security and authorization evidence

- All autonomy reads require authentication.
- Every state-changing autonomy route requires the dedicated autonomy-operator token.
- Ordinary bearer authentication is not sufficient for start, pause, resume, stop, recovery, monitoring, decision, execution or exit mutations.
- Unauthorized requests are rejected server-side; UI state is not the protection.
- Invalid mode transitions are rejected.
- No live broker credentials, broker adapter or live execution integration exists.
- Recovery and kill-switch operations are operator-gated.
- Sensitive token values are not returned by status or reliability payloads.

---

## 14. Paper-only safety evidence

| Invariant | Evidence | Verdict |
|---|---|---|
| `paper_only=true` | contracts, status, scenarios | PASS |
| `live_trading=false` | contracts/status/API | PASS |
| No broker integration | no broker route/import; safety tests | PASS |
| No real-money path | NM-10 simulated execution only | PASS |
| NM-08 sole risk authority | proposal/exit provenance tests | PASS |
| NM-10 sole execution authority | preflight/submit/reconcile tests | PASS |
| No direct ledger/cash/position writes from autonomy | AST/safety tests and existing engine reuse | PASS |
| No synthetic fills outside NM-10 | authoritative fill records only | PASS |
| No automatic learning promotion | scenario 19/contract | PASS |
| No target-weight rebalancing | contract/non-goal tests | PASS |
| No forced EOD liquidation | contract/non-goal tests | PASS |
| Long-only target/trailing | exit contract/tests | PASS |

---

## 15. Twenty-scenario acceptance results

All scenarios executed in `tests/test_novamarket_at06.py` against the real application engines and stores. Two additional AT-05 addendum scenarios cover trailing stops and high-water restart.

| ID | Scenario | Result |
|---|---|---|
| 1 | Clean application startup | PASS |
| 2 | Application restart | PASS |
| 3 | Monitoring mode | PASS |
| 4 | Autonomous mode | PASS |
| 5 | Approved entry | PASS |
| 6 | Rejected entry | PASS |
| 7 | Duplicate proposal | PASS |
| 8 | Submission timeout | PASS |
| 9 | Partial entry fill | PASS |
| 10 | Full entry fill | PASS |
| 11 | Stop-loss trigger | PASS |
| 12 | Target trigger | PASS |
| 13 | Paused mode | PASS |
| 14 | Paused protective exit | PASS |
| 15 | Partial exit fill | PASS |
| 16 | Full exit fill | PASS |
| 17 | Duplicate exit trigger | PASS |
| 18 | Crash during execution | PASS |
| 19 | Learning attribution | PASS |
| 20 | Kill switch | PASS |
| 21 | Trailing-stop trigger (addendum) | PASS |
| 22 | Trailing high-water restart (addendum) | PASS |

**AT-06 suite: 25 passed** (22 scenarios, 2 reliability tests, 1 sustained-cycle test).

---

## 16. Order and fill reconciliation evidence

- Every executed entry references a persisted risk-approved proposal and NM-10 order.
- Every exit references a durable exit decision, NM-08 assessment and NM-10 order.
- Fills are read from NM-10 authoritative records; the autonomy layer never writes one.
- Repeated execution/exit triggers reuse the existing order and decision.
- Partial fills keep the correct remaining order and position quantities.
- Full exits close the position only after fill and reconciliation.
- Timeout recovery inspects before retrying; no duplicate order.
- Exit recovery reconciles pending partial decisions without another submission.

---

## 17. Financial consistency evidence

- Entry and exit cash/position effects are produced by existing NM-10 settlement.
- Ledger transaction ids are attached to fills.
- Portfolio holdings match authoritative filled quantities.
- Full exit removes the position only after reconciliation passes.
- Realized outcomes come from authoritative accounting.
- No discrepancy was repaired by overwriting a balance.
- No unresolved financial discrepancy remains in the acceptance suite.

---

## 18. Performance measurements

Conditions: Windows host, local SQLite, deterministic paper fixtures, one process, real AT-01..AT-05 engines.

| Metric | Measured |
|---|---|
| Runtime startup | 1.95 ms |
| Runtime shutdown | 1.01 ms |
| Exit-supervision cycle mean (60 cycles) | 27.68 ms |
| Sustained elapsed (60 cycles) | 1.6608 s |
| Crash recovery | 10.91 ms |
| Recovery reconciled | 1 |
| Sell orders after 60 cycles | 1 |
| Exit decisions after 60 cycles | 1 |

No production SLA or live-provider capacity claim is made.

---

## 19. Sustained-load and resource results

- 60 consecutive duplicate-suppression exit cycles completed.
- Mean cycle: 27.68 ms.
- Asyncio task count: 1 before, 1 after.
- Traced current memory: 986,279 bytes.
- Traced peak memory: 1,071,385 bytes.
- One sell order and one exit decision throughout.
- No task leak, duplicate work, growing queue or duplicate order observed.
- This is a deterministic sustained test, not a 24-hour soak.

---

## 20. API verification

- New routes registered in FastAPI, `ApiSurface` and `ROUTE_CONSUMERS`.
- `GET /market/autonomy/reliability` is authenticated.
- `POST /market/autonomy/recover` requires the autonomy operator.
- Existing 628-route-era contracts remain valid; current generated reference contains 419 documented routes.
- `_MARKET_ROUTES` now has 223 entries.
- `python scripts/generate_api_reference.py --check` is in sync.
- Route-contract tests: 3 passed, 223 subtests passed.
- Full API + workbench group: 64 passed, 231 subtests passed.

---

## 21. UI verification

- The Autonomy page continues to render runtime, monitoring, decisions, execution, exits, positions, configurations and learning.
- The safety card no longer labels the AT-01 shell release gate as the complete paper pipeline's status; it reads the AT-06 reliability health.
- TypeScript type-check and Vite production build pass: 1,599 modules, built in 4.26 s.
- Documented limitation: a fresh authenticated browser interaction pass was not repeated for this single AT-06 line. The prior AT-05 live Autonomy UI verification remains the browser evidence. EP-05 browser save/delete interactions remain covered at API/service level only.

---

## 22. Regression verification

| Group | Result |
|---|---|
| AT-06 focused | **25 passed** |
| AT-04/AT-05/AT-06/runtime | **96 passed** |
| AT-02/AT-03/route contract | **87 passed + 223 subtests** |
| API + workbench UI | **64 passed + 231 subtests** |
| NM-08/NM-10/wallet/learning | **182 passed + 22 subtests** |

No failure, skip or weakened assertion was used to obtain these results.

---

## 23. Exact targeted commands

```text
PYTHONPATH=src python -m pytest tests/test_novamarket_at06.py -q
25 passed

PYTHONPATH=src python -m pytest \
  tests/test_novamarket_at06.py tests/test_novamarket_at05.py \
  tests/test_novamarket_at04.py tests/test_novamarket_autonomy.py -q
96 passed

PYTHONPATH=src python -m pytest \
  tests/test_novamarket_at02.py tests/test_novamarket_at03.py \
  tests/test_novamarket_api.py::NovaMarketRouteContractTests -q
87 passed, 223 subtests passed

PYTHONPATH=src python -m pytest \
  tests/test_novamarket_api.py tests/test_novamarket_workbench_ui.py -q
64 passed, 231 subtests passed

PYTHONPATH=src python -m pytest \
  tests/test_novamarket_risk.py tests/test_novamarket_execution.py \
  tests/test_novamarket_wallet.py tests/test_novamarket_learning.py -q
182 passed, 22 subtests passed
```

---

## 24. Type-checking and static analysis

```text
PYTHONPATH=src python -m mypy src
Success: no issues found in 573 source files

PYTHONPATH=src python -m mypy src --platform win32
Success: no issues found in 573 source files
```

`git diff --check` is clean.

---

## 25. Build results

```text
cd web && npm run build
✓ 1599 modules transformed
dist/index.html                   0.61 kB │ gzip:   0.36 kB
dist/assets/index-C3dDR33g.css   91.25 kB │ gzip:  16.80 kB
dist/assets/index-DtQq5Nb-.js   602.02 kB │ gzip: 160.60 kB
✓ built in 4.26s
```

The existing large-chunk advisory remains a non-blocking build warning.

---

## 26. Known limitations

1. Target and trailing-stop supervision support long positions only; short-position target/trailing is unsupported.
2. Deterministic market/forecast fixtures prove application and financial pathways, not live-provider reliability.
3. Paper fills use NM-10 simulation and a simulated clearing counterparty; no exchange-grade/tick-level execution is claimed.
4. The kill switch does not cancel already submitted NM-10 orders. It prevents new actions; pending orders remain authoritative and are reconciled by explicit recovery.
5. Forced end-of-day liquidation remains excluded and unimplemented.
6. Target-weight rebalancing remains excluded.
7. Automatic learning promotion remains excluded.
8. A fresh authenticated browser pass was not run for the one AT-06 UI line; build and prior AT-05 live UI evidence are retained.
9. AT-01 runtime-shell fields (`trading_implemented`, `orders_possible`, `release_gate`) describe the shell, not the complete pipeline. Complete state comes from AT-04/AT-05/AT-06 reliability.
10. No 24-hour soak or live-provider failover test was performed.

---

## 27. Unresolved issues

No critical safety, financial consistency, recovery or idempotency issue remains open from AT-06.

The limitations above are documented constraints and verification boundaries, not hidden failures.

---

## 28. Deferred requirements

- Short-position target/trailing supervision.
- Forced end-of-day liquidation policy.
- Target-weight autonomous rebalancing.
- Automatic learning/model promotion.
- Live broker integration or real-money execution.
- AT-07 and later milestones.
- 24-hour production soak and live market-data provider failover.

---

## 29. Files created and modified

### Created

- `src/novacontrol/novamarket/autonomy_observability.py`
- `tests/test_novamarket_at06.py`
- `AUTONOMOUS_TRADING_AT06_REPORT.md` (replaced from NOT STARTED)

### Modified

- `src/novacontrol/novamarket/autonomy_exits/pipeline.py`
- `src/novacontrol/novamarket/execution/store.py`
- `src/novacontrol/novamarket/service.py`
- `src/novacontrol/api/app.py`
- `src/novacontrol/api/models.py`
- `src/novacontrol/api/route_consumers.py`
- `web/src/pages/Autonomy.tsx`
- `tests/test_novamarket_api.py`
- `docs/API.md` (regenerated)
- `docs/AT05_PREREQUISITE_CLOSURE.md` (AT-06 completion note)
- `AUTONOMOUS_TRADING_FINAL_ACCEPTANCE_REPORT.md` (this document)

### Not changed

- No AT-01..AT-05 engine was rewritten.
- No unrelated NovaControl/NovaForge feature was modified.
- No commit, staging, push or tag was performed.

---

## 30. Requirement-level final matrix

| Requirement | Evidence | Verdict |
|---|---|---|
| AT-01 runtime/mode/lease | report + 96-test group | PASS |
| AT-02 session/discovery | report + 87-test group | PASS |
| AT-03 decision/NM-08 | report + scenarios 4/6 | PASS |
| AT-04 execution/reconciliation | report + scenarios 5/8/9/10 | PASS |
| AT-05 stop/target/trailing/learning | report + scenarios 11/12/14–19/21/22 | PASS |
| AT-06 lifecycle acceptance | 22 scenarios, 25 tests | PASS |
| Startup safe default | scenario 1 | PASS |
| Restart no auto-resume | scenario 2 | PASS |
| One cycle/lease/scheduler | runtime tests + scenarios | PASS |
| Crash recovery | scenario 18 + recovery endpoint | PASS |
| Runtime modes | scenarios 3/4/13/14/20 | PASS |
| Kill switch | scenario 20 | PASS |
| Duplicate prevention | scenarios 7/8/15/17/18 | PASS |
| Order/fill/ledger consistency | scenarios 5/9/10/15/16 | PASS |
| Observability | reliability endpoint + tests | PASS |
| Failure classification | domain tests | PASS |
| Security/operator enforcement | API tests | PASS |
| Paper-only invariants | contracts/safety tests | PASS |
| API consistency | route/doc tests | PASS |
| UI consistency | build + documented limitation | PASS* |
| Performance | measured hot paths | PASS |
| Sustained load/resources | 60-cycle test | PASS |
| Regression | 454 passed across groups (overlap noted) | PASS |
| Type checks | 573 files both views | PASS |
| Build | TypeScript + Vite | PASS |
| Documentation | six milestone + final reports | PASS |
| No Git mutation | diff check/no commit | PASS |

\* Build-level and prior live evidence; fresh browser interaction for the AT-06 line is the documented limitation.

---

## 31. Five acceptance gates

| Gate | Verdict | Evidence |
|---|---|---|
| Gate 1 — Functional completeness | **PASS** | Modes, monitoring, risk, execution, exits, learning and recovery verified |
| Gate 2 — Integration completeness | **PASS** | NM-08/NM-10/portfolio/ledger reused; lifecycle IDs linked |
| Gate 3 — Testing completeness | **PASS** | 25 AT-06 tests, 22 scenarios, focused regressions, types, build, performance |
| Gate 4 — Safety and reliability | **PASS** | Paper-only, no broker, no duplicates, safe restart, kill switch, fail-closed |
| Gate 5 — Documentation and evidence | **PASS** | Six milestone reports, final report, matrix, commands, limitations, deferred items |

---

## 32. Final architecture assessment

The autonomous paper-trading subsystem is cohesive and appropriately layered. AT-06 did not add a competing execution, risk, portfolio or learning path. Recovery and observability are thin compositions of existing durable records. The highest-risk areas — duplicate orders, partial fills, restart state, NM-08/NM-10 bypass and financial overwrite — are covered by direct scenarios rather than UI assertions.

The remaining constraints are explicit: long-only target/trailing, simulated paper fills, no forced EOD liquidation, no automatic promotion, no live broker and no 24-hour soak. None invalidates the approved paper-only scope.

---

## 33. Final verdict

**ACCEPTED — PASS** for the approved NovaMarket autonomous paper-trading scope AT-01 through AT-06.

All five gates pass. The system is strictly paper-only, recoverable, observable, reconciled, operator-controlled and backed by reproducible focused evidence.

No commit, staging, push or tag was performed.
