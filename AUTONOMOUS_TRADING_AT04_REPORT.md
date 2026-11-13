# AT-04 — Paper Execution and Order Lifecycle Integration

**Milestone:** AT-04  
**Project:** NovaMarket  
**Date:** 2026-11-11  
**Overall verdict:** **PASS**

AT-04 connects the existing AT-03 decision/risk pipeline to the existing NM-10 paper-execution engine. It adds a durable execution-intent journal, a no-order NM-10 preflight, controlled forwarding, order/fill reconciliation, idempotent recovery, API surfaces and a UI execution card. It does not add a broker, live trading, a second execution engine, direct ledger/position writes, protective exits, target-weight rebalancing or learning promotion.

---

## 1. Prerequisite gate

| Prerequisite | Verdict | Evidence |
|---|---|---|
| AT-01 autonomy runtime / lease | PASS | `docs/AT-01_IMPLEMENTATION_REPORT.md`; `tests/test_novamarket_autonomy.py` |
| AT-02 session-aware monitoring | PASS | `docs/AUTONOMOUS_TRADING_AT02_REPORT.md`; `tests/test_novamarket_at02.py` |
| AT-03 decisions and NM-08 risk assessment | PASS | `AUTONOMOUS_TRADING_AT03_REPORT.md` (24/24); `tests/test_novamarket_at03.py` |
| NM-10 paper execution and reconciliation | PASS | `tests/test_novamarket_execution.py`; real application run below |

AT-04 did not start from a failed prerequisite. AT-01, AT-02 and AT-03 were already documented PASS.

---

## 2. Architecture implemented

### Single execution authority

`ExecutionEngine` remains the only paper-order and fill implementation. AT-04 calls:

- `ExecutionEngine.preflight()` — additive, no order/fill/reservation/ledger write; runs the engine's own `_validate()` path and therefore revalidates NM-08, NM-02, cash, position and instrument rules.
- `ExecutionEngine.submit()` — the existing authoritative order path, including deterministic NM-10 order IDs and fill settlement.

No AT-04 code calls `PortfolioEngine.apply()`, changes cash/positions/ledger rows, or fabricates fills.

### AT-03 proposal adapter

AT-03 deliberately persists `status = "risk_approved"` and `execution_allowed = false`. NM-10 requires `status = "approved"`. AT-04 adds `AutonomyDecisionPipeline.execution_proposal(proposal_id)`, an additive read view that normalizes a proposal only when:

- persisted status is `risk_approved`,
- persisted risk outcome is `approved`,
- the stored NM-08 decision belongs to that proposal,
- the risk decision id matches the proposal provenance.

The persisted AT-03 proposal is never mutated, and AT-03 responses retain their non-executable semantics.

### Durable journal

`ExecutionStore` adds two additive tables:

- `autonomy_execution_intents` — one unique row per proposal and idempotency key.
- `autonomy_execution_events` — append-only lifecycle events, bounded to the newest 500 per execution.

No financial table is altered. NM-10 orders, fills and reconciliations remain authoritative.

### Service and runtime

- `NovaMarketService._ensure_autonomy_execution()` opens AT-04 lazily.
- `_runtime_tick()` runs AT-04 after AT-03, never before it.
- `_withdraw()` closes AT-04 before the NM-10 engine.
- AT-04 has its own `asyncio.Lock`; the scheduler and manual API path share it.
- Every forwarding path re-checks autonomous mode and the valid AT-01 lease.
- `monitoring`, `paused`, `disabled` and an expired/conflicting lease cannot forward entries.

### API

Eight authenticated routes were added and registered in `ApiSurface`, `ROUTE_CONSUMERS`, and `docs/API.md`:

```
GET  /market/autonomy/executions/contracts
GET  /market/autonomy/executions/status
GET  /market/autonomy/executions
GET  /market/autonomy/executions/failures
GET  /market/autonomy/executions/{execution_id}
GET  /market/autonomy/executions/{execution_id}/events
POST /market/autonomy/executions/run
POST /market/autonomy/executions/{execution_id}/reconcile
```

The two mutations require the dedicated autonomy operator token as well as ordinary API auth. The read routes never create an order. `POST /run` re-dispatches only to the gated AT-04 service path; there is no direct order-creation route.

### UI

The Autonomy page (`web/src/pages/Autonomy.tsx`) now shows:

- execution engine, risk authority, runtime mode and lease validity;
- intent/submitted/partial/filled/rejected/reconciliation counts;
- last intent status, NM-10 order status and authoritative order id;
- filled/remaining quantities, average paper price, fees and reconciliation state;
- failure code/reason and journal events count;
- latest NM-10 fill history (fill id, quantity, price, fees, timestamp);
- recent execution intents with replay-safe order ids.

The UI never labels `risk_approved` as executed. `partially_filled`, `filled`, `rejected`, `cancelled`, `unknown` and `reconciliation_required` are distinct states. The run button is disabled outside autonomous mode, without a valid lease, or without operator permission.

---

## 3. Requirement matrix

| # | Requirement | Verdict | Evidence |
|---|---|---|---|
| 1 | Prerequisites AT-01/02/03 pass | PASS | Prior reports; prerequisite regression tests |
| 2 | Preserve AT-01 lease/mode invariants | PASS | Runtime gate tests; `LEASE_INVALID`, `RUNTIME_NOT_AUTONOMOUS` |
| 3 | AT-02 session/calendar remains the monitoring gate | PASS | AT-02 regressions; no AT-04 session bypass |
| 4 | AT-03 remains the decision producer | PASS | Real application run began with a persisted `risk_approved` proposal |
| 5 | NM-08 remains sole risk authority | PASS | NM-08 approval checked before forwarding and revalidated inside NM-10 preflight/submit |
| 6 | Only risk-approved proposals enter | PASS | Rejected/missing/expired provenance tests fail closed |
| 7 | Durable intent persisted before forwarding | PASS | `save_autonomy_intent` before preflight/submit; journal tests |
| 8 | Deterministic idempotency key | PASS | `at04:<proposal_id>`; unique store constraints |
| 9 | Same proposal cannot create multiple orders | PASS | Repeated run, concurrent run and restart replay tests |
| 10 | No order on preflight failure | PASS | Invalid provenance, expiry, stale data, insufficient cash, unknown instrument tests |
| 11 | Controlled mode/lease/concurrency gates | PASS | Monitoring/paused/lease tests; one `asyncio.Lock` |
| 12 | NM-10 is the only execution engine | PASS | Engine seam; no second engine, order writer or fill creator |
| 13 | Order lifecycle mapping | PASS | `created`→`validated`→`open`→`filled`/`rejected`/`failed` via NM-10 transitions |
| 14 | Accepted/submitted is not reported as filled | PASS | `status_for_order`; submitted/open states remain submitted |
| 15 | Partial fills are represented honestly | PASS | Reduced-volume NM-10 partial-fill test; remaining quantity retained |
| 16 | Fill reconciliation | PASS | NM-10 reconciliation returns `ok=true`; fill ids and quantities match order |
| 17 | Cash/position/ledger consistency | PASS | Real wallet/portfolio stores; fill ledger ids; reconciliation test |
| 18 | Failure/unknown state and bounded retry | PASS | Synthetic timeout test; retry uses same key; no duplicate order |
| 19 | Restart recovery | PASS | New AT-04 pipeline over same durable store replays the existing order |
| 20 | Durable journal fields | PASS | Intent, submission, order, fill, reconciliation, retry, failure and recovery fields |
| 21 | Journal does not grow without bound | PASS | Events bounded to 500 per execution |
| 22 | API auth and operator separation | PASS | 401 without auth; 403 without operator token; route tests |
| 23 | API registry/docs synchronization | PASS | `docs/API.md` regenerated, 616 routes, `--check` in sync |
| 24 | UI distinguishes all execution states | PASS | AT-04 card and badges; TypeScript/Vite build |
| 25 | No live trading, broker or real-money path | PASS | Contract flags; AST safety test; no broker imports |
| 26 | Non-goals not implemented | PASS | No exits, rebalancing, learning promotion, forced EOD flattening or AT-05/06 |
| 27 | Focused AT-04 tests | PASS | **24 passed** |
| 28 | Modified-module and AT-01/02/03 regressions | PASS | **340 passed + 638 subtests** in the selected regression run |
| 29 | Type checks | PASS | `mypy src`: 568 files clean; `mypy src --platform win32`: 568 files clean |
| 30 | Frontend build | PASS | `tsc -b && vite build`; 1,599 modules; built |
| 31 | Real application verification | PASS | `NovaControlApplication` + `NovaMarketService` real run below |
| 32 | Performance measured | PASS | 214.788 ms first execution; 4.174 ms duplicate replay (single measured run) |
| 33 | No commit/push/stage/tag | PASS | No git mutation performed |

---

## 4. Test evidence

### Focused AT-04 suite

Command:

```text
PYTHONPATH=src python -m pytest tests/test_novamarket_at04.py -q
```

Result:

```text
24 passed
```

Coverage includes:

- real AT-03 proposal → NM-08 approval → NM-10 paper order and fill;
- authoritative order/fill ids, quantities, fees and reconciliation;
- duplicate full replay with no second order or fill;
- concurrent requests sharing one order;
- partial fill without a false completed state;
- monitoring, paused and expired-lease refusals;
- persisted-risk mismatch, rejected status, expired proposal, stale data, invalid instrument, insufficient cash;
- timeout with recovery and no duplicate order;
- terminal NM-10 rejection with reason;
- restart replay over the same journal;
- journal uniqueness and bounds;
- API auth/operator separation and route registry;
- source-level paper-only safety checks.

### Regression suite

Command result from the selected regression run:

```text
340 passed, 638 subtests passed
```

Included NM-10 execution, AT-03 decisions, AT-02 monitoring, AT-01 autonomy, NM-08 risk, NM-09 strategies, NM-07 portfolios, NM-03 wallets and API-reference sync.

Final combined focused run after the last changes:

```text
70 passed
```

### Static checks

```text
mypy src                     Success: no issues found in 568 source files
mypy src --platform win32    Success: no issues found in 568 source files
generate_api_reference --check   docs/API.md: in sync (616 routes)
```

### Build

```text
tsc -b && vite build
✓ 1,599 modules transformed
✓ built in 9.46s
```

Warnings are the existing Vite chunk-size/dynamic-import warnings; no TypeScript or build failure.

---

## 5. Real application verification

Command:

```text
PYTHONPATH=src python tmp/at04/verify_at04.py
```

Result: **VERIFY_STATUS=0**

The script ran the real `NovaControlApplication` and `NovaMarketService`, with deterministic market and forecast fixtures only (no live provider, no network, no broker). It used the application's real wallet, portfolio, risk, strategy, decision, execution, ledger and reconciliation stores.

Observed result:

```json
{
  "runtime_mode": "autonomous",
  "proposal_status": "risk_approved",
  "risk_outcome": "approved",
  "execution_status": "filled",
  "order_status": "filled",
  "order_id": "ord_ea7de23760f84e77c8014819",
  "fill_count": 1,
  "filled_quantity": "90.9091",
  "remaining_quantity": "0.0000",
  "reconciliation": "passed",
  "duplicate_replay": true,
  "same_order": true,
  "paper_only": true,
  "live_trading": false,
  "live_orders_possible": false,
  "first_execution_ms": 214.788,
  "duplicate_replay_ms": 4.174
}
```

Evidence is saved at `tmp/at04/data/evidence.json`.

The first attempt correctly refused with `market data is stale by 15817s (limit 300s)` because the fixture timestamp was four hours behind the application clock. After making the fixture current, the same application path filled. This confirms the stale-data refusal is enforced, not bypassed.

### Recovery validation

The focused timeout test uses a submit wrapper that raises after preflight, with no NM-10 order written. AT-04 records `unknown` with `EXECUTION_SUBMISSION_UNKNOWN`; a second run with the same durable intent and idempotency key creates exactly one NM-10 order and one fill. A separate restart test opens a new AT-04 pipeline over the same store and replays the existing order.

---

## 6. Safety invariants

Verified by source inspection, route contract and tests:

- `paper_only = true`
- `live_trading = false`
- `broker_connected = false`
- `live_orders_possible = false`
- no broker/live-order imports in AT-04;
- no `record_fill`, `apply_fill`, `deposit`, `withdraw`, `update_position` or `set_balance` in the AT-04 pipeline;
- NM-10 remains the only order/fill writer;
- AT-04 journal is not a ledger and never changes cash or positions;
- no synthetic fills are created;
- rejected, cancelled, expired and failed orders are terminal;
- reconciliation failure blocks a success claim and surfaces `reconciliation_required`;
- no AT-05 protective exits, rebalancing, learning promotion or forced EOD flattening were added.

---

## 7. Files

Added:

- `src/novacontrol/novamarket/autonomy_execution/__init__.py`
- `src/novacontrol/novamarket/autonomy_execution/models.py`
- `src/novacontrol/novamarket/autonomy_execution/pipeline.py`
- `tests/test_novamarket_at04.py`
- `tmp/at04/verify_at04.py`
- `tmp/at04/data/evidence.json`

Modified:

- `src/novacontrol/novamarket/execution/engine.py` — proposal loader, preflight, pricing-error classification
- `src/novacontrol/novamarket/execution/store.py` — additive AT-04 tables and bounded journal
- `src/novacontrol/novamarket/decision/pipeline.py` — additive `execution_proposal()` normalized view
- `src/novacontrol/novamarket/service.py` — AT-04 wiring, tick, shutdown, API methods
- `src/novacontrol/api/app.py` — eight AT-04 routes
- `src/novacontrol/api/models.py` — `ApiSurface` route contract
- `src/novacontrol/api/route_consumers.py` — consumer registry
- `docs/API.md` — regenerated
- `web/src/pages/Autonomy.tsx` — execution card and controls

No commit, push, stage or tag was performed.

---

## 8. Known limitations / verification notes

1. The UI was built successfully but **not visually screenshot-verified** in this run. The rendered card and state mappings were source-reviewed and compiled; a browser-level visual check remains advisable before release.
2. The real application verification used deterministic market and forecast fixtures. No live market provider or broker was contacted; this is intentional for a paper-only milestone.
3. Partial-fill verification uses a reduced-volume paper fixture, not an exchange partial fill. The authoritative NM-10 partial-fill state is exercised.
4. Timeout/recovery is simulated by a submit wrapper, not by a real network partition. It still preserves the durable intent/no-duplicate-order invariant.
5. AT-04 journal events with names outside the registered `EventType` vocabulary are retained in the journal but not published on the typed event bus. The service publisher filters unknown names so a missing enum entry cannot turn a completed paper execution into a publisher error. Journal records remain authoritative.
6. No production deployment, live trading enablement or AT-05/AT-06 implementation was performed.

---

## 9. Verdict

**PASS.**

Every mandatory AT-04 requirement is implemented and verified within the stated paper-only scope. The only non-code verification gap is a browser screenshot; the TypeScript/Vite build and source-level state mapping pass, and the real application service path filled a paper order with reconciliation and duplicate replay.
