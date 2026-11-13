# AT-05 — Exit Supervision and Learning Evidence

**Milestone:** AT-05  
**Project:** NovaMarket  
**Date:** 2026-11-11  
**Overall verdict:** **PASS**

AT-05 now supervises three exit conditions for existing long paper positions: horizon stop-loss, a fixed per-position target price, and a percentage trailing stop. It reuses the accepted AT-01 runtime lease, AT-02 session gates, AT-03 decision provenance, NM-08 risk authority, NM-10 paper execution and the existing portfolio/wallet/ledger accounting. It adds durable exit configuration, high-water persistence, restart recovery, partial-fill handling, advisory learning evidence, authenticated configuration/history API routes and an Autonomy UI surface.

The milestone is **PASS**. The previously missing target-price and trailing-stop conditions are implemented, persisted, restart-safe, exercised through NM-08 and NM-10, and accepted in focused tests and a real-application run. No broker, live order, real-money path, rebalancing, forced end-of-day liquidation or automatic learning promotion was added.

---

## 1. Prerequisite gate

| Prerequisite | Verdict | Evidence |
|---|---|---|
| AT-01 autonomy runtime and lease | PASS | `docs/AT-01_IMPLEMENTATION_REPORT.md`; `tests/test_novamarket_autonomy.py` |
| AT-02 session-aware monitoring | PASS | `docs/AUTONOMOUS_TRADING_AT02_REPORT.md`; `tests/test_novamarket_at02.py` |
| AT-03 decisions and NM-08 risk assessment | PASS | `AUTONOMOUS_TRADING_AT03_REPORT.md`; `tests/test_novamarket_at03.py` |
| AT-04 paper execution and reconciliation | PASS | `AUTONOMOUS_TRADING_AT04_REPORT.md`; `tests/test_novamarket_at04.py` |

All four prerequisite reports are present and their focused suites pass. `docs/AT05_PREREQUISITE_CLOSURE.md` now records the AT-05 gate as **CLOSED**.

---

## 2. Implemented semantics

### Authorities remain unchanged

- **NM-08** is the only risk authority. Every target, trailing or stop-loss exit is submitted to `risk.assess()` before an order proposal exists.
- **NM-10** is the only paper-execution authority. AT-05 calls `preflight()`, `submit()` and `reconcile()`; it never creates a fill, order, cash movement or ledger entry itself.
- **Portfolio, wallet and ledger** remain authoritative. Exit quantities come from the portfolio book; cash, position and ledger results come from NM-10 settlement.
- **AT-01** remains the only mode/lease authority.
- **AT-03** remains the producer of entry proposals and NM-08 provenance.

No broker adapter, live-order path, direct position writer, cash writer or ledger writer exists in the AT-05 package. The safety test rejects broker/live imports and writable financial method names.

### Exit conditions

```text
stop_price     = average_cost × (1 − horizon stop_loss_fraction)
target_price   = persisted fixed price above the long position average cost
trailing_price = persisted high-water mark × (1 − trailing_stop_fraction)
```

Trigger comparisons:

```text
stop_loss      : observed_price <= stop_price
trailing_stop  : observed_price <= trailing_price
target_price   : observed_price >= target_price
```

Precedence when more than one condition is true:

```text
stop_loss → trailing_stop → target_price
```

The contract reports:

```json
{
  "contract_version": "1.1.0",
  "supported_exit_conditions": ["stop_loss", "target_price", "trailing_stop"],
  "unsupported_exit_conditions": [],
  "supported_directions": ["long"]
}
```

Target and trailing configuration is optional. A position with no configuration retains stop-loss-only behaviour.

### Configuration rules

- `target_price` must be a positive fixed price above the authoritative long-position average cost.
- `trailing_stop_percent` must satisfy `0 < percent < 100`.
- Either condition, or both, may be configured.
- Configuration is scoped to `(portfolio_id, instrument_id)` and versioned.
- Removing target/trailing configuration does not touch the position; stop-loss supervision remains active.
- Short positions are explicitly unsupported for target/trailing supervision. They are not approximated.

### High-water mark and restart

- A long trailing stop starts its high-water mark from the highest valid observed price.
- The high-water mark never decreases for a long position.
- It is persisted with the configuration and restored after process restart.
- Changing the trailing percentage resets the high-water basis; changing only the target preserves it.
- Reaching a trigger records a durable condition epoch. The condition re-arms only after the pending exit is clear and the price is no longer triggering.
- Restart loads the durable configuration, high-water mark, condition epochs, decisions and pending orders; it does not auto-resume the runtime.

### Modes

| Mode | Entries | Protective exits | Reconciliation |
|---|---|---|---|
| `disabled` | No | No action | No action |
| `monitoring` | No | Evaluate and persist trigger; no submission | No new submission |
| `autonomous` | Allowed through AT-04 | Allowed through NM-08/NM-10 | Allowed |
| `paused` | Blocked | Allowed through NM-08/NM-10 | Allowed |

`paused` is deliberately not disabled: new entries stop, but target, trailing and stop-loss protection remains active.

### Paper clearing liquidity

NM-10 now seeds a bounded **simulated** counterparty balance in `pfw_market_clearing` (`PAPER_CLEARING_LIQUIDITY = 1,000,000,000.00`, idempotent key `nm10:clearing:liquidity:v1:<balance>`). Without it, profitable sells could only settle from prior buy proceeds and failed even though portfolio and order records were valid.

This is simulated issuance by the paper market counterparty, not a portfolio direct write and not real money. The ledger remains balanced because the deposit debits the issuance account. Tests confirm the seed appears once and does not overwrite a portfolio wallet.

---

## 3. Requirement matrix

| # | Requirement | Verdict | Evidence |
|---|---|---|---|
| 1 | AT-01 through AT-04 prerequisite PASS | PASS | Prior reports; 146 focused prerequisite tests |
| 2 | Preserve `paper_only=true`, `live_trading=false` | PASS | Contract, status, API tests, real-app evidence |
| 3 | No broker or real-money path | PASS | AST safety test; no broker/live imports |
| 4 | NM-08 remains sole risk authority | PASS | Risk assessment before submission; target and stop rejection tests |
| 5 | NM-10 remains sole execution authority | PASS | Preflight/submit/reconcile; no direct fill writer |
| 6 | Portfolio/ledger remain authoritative | PASS | Real position, cash and ledger reconciliation |
| 7 | `disabled` performs no supervision or action | PASS | Disabled-mode test and real-app restart check |
| 8 | `monitoring` evaluates but never submits | PASS | Monitoring tests for stop, target and trailing |
| 9 | `autonomous` submits eligible exits | PASS | Full target exit and stop-loss tests |
| 10 | `paused` blocks entries but permits protective exits | PASS | Paused trailing partial-fill test |
| 11 | One cycle at a time | PASS | Asyncio lock; concurrent-cycle test |
| 12 | AT-01 lease enforced | PASS | Expired-lease test |
| 13 | Dedicated operator token enforced | PASS | 403 without operator token; authorized PUT reaches service |
| 14 | Stop-loss supervision | PASS | Full and partial stop-loss tests |
| 15 | Target-price supervision | PASS | Below-target, exact target, gap-through, partial, restart tests |
| 16 | Trailing-stop supervision | PASS | Rising high-water, falling non-trigger, breach, restart, partial tests |
| 17 | Target configuration persisted and versioned | PASS | Store/API tests; real-app restart |
| 18 | Trailing configuration and high-water persisted | PASS | Store/API tests; real-app restart at `118.9100` |
| 19 | High-water never decreases for long | PASS | Trailing tests; real-app fall to `117.81` kept `118.9100` |
| 20 | Invalid target refused | PASS | At-or-below average cost refused with 422 |
| 21 | Invalid trailing percentage refused | PASS | Zero, 100 and malformed values refused |
| 22 | Stable exit decision identity | PASS | Deterministic `xdec_…` key and unique constraint |
| 23 | Duplicate trigger prevention | PASS | Repeated, concurrent and restart tests; real-app 0 duplicates |
| 24 | No overselling | PASS | Quantity capped at available position |
| 25 | Partial-fill handling | PASS | Real-app trailing `1.0000` filled / `89.9091` remaining |
| 26 | Pending-order suppression | PASS | Pending exit reconciled before retry |
| 27 | Restart recovery | PASS | Configuration, high-water, decision and order recovered |
| 28 | Uncertain submission reconciliation | PASS | Timeout-after-commit test; one order only |
| 29 | NM-08 rejection creates no order | PASS | Target and stop-loss rejection tests |
| 30 | Stale/missing market data fails closed | PASS | Stale and unusable-data tests for all conditions |
| 31 | Learning evidence links entry to exit | PASS | Complete target evidence; partial trailing evidence |
| 32 | Learning is advisory only | PASS | `auto_promoted=false`; no strategy mutation |
| 33 | API authentication and operator separation | PASS | 401/403 tests; authorized configuration PUT test |
| 34 | Route registry and API docs synchronized | PASS | 628 routes; `generate_api_reference.py --check` in sync |
| 35 | UI represents target, trailing, high-water and partial states | PASS | Build plus live Autonomy inspection |
| 36 | Focused AT-05 tests | PASS | **34 passed** |
| 37 | AT-01/02/03 and NM-08 regression | PASS | **146 passed** |
| 38 | NM-10, wallet/ledger and learning regression | PASS | **134 passed + 22 subtests** |
| 39 | API and UI regression | PASS | **122 passed + 229 subtests** |
| 40 | Type checks | PASS | 572 files clean in both views |
| 41 | Frontend build | PASS | TypeScript + Vite build succeeded |
| 42 | Real application verification | PASS | **28/28 checks** |
| 43 | No commit/stage/push/tag | PASS | No Git mutation performed |

---

## 4. Test evidence

### Focused AT-05 suite

```text
PYTHONPATH=src python -m pytest tests/test_novamarket_at05.py -q
34 passed
```

Coverage includes:

- target below, at and gap-through trigger;
- target restart, partial fill and duplicate suppression;
- trailing high-water rise, non-lowering fall, breach and stop-price calculation;
- trailing restart preserving the high-water mark;
- trailing partial fill and repeated-cycle suppression;
- invalid target/trailing refusal;
- monitoring → autonomous target execution;
- paused protective trailing execution;
- disabled mode, expired lease, stale/unusable market data;
- NM-08 rejection without an order;
- timeout after NM-10 commit;
- learning linkage, partial evidence and no promotion;
- API authentication/operator gating and authorized configuration write.

### Selected regression suites

```text
PYTHONPATH=src python -m pytest \
  tests/test_novamarket_autonomy.py \
  tests/test_novamarket_at02.py \
  tests/test_novamarket_at03.py \
  tests/test_novamarket_risk.py -q
146 passed
```

```text
PYTHONPATH=src python -m pytest \
  tests/test_novamarket_execution.py \
  tests/test_novamarket_wallet.py \
  tests/test_novamarket_learning.py -q
134 passed, 22 subtests passed
```

```text
PYTHONPATH=src python -m pytest \
  tests/test_novamarket_at04.py \
  tests/test_novamarket_at05.py \
  tests/test_novamarket_workbench_ui.py \
  tests/test_novamarket_api.py -q
122 passed, 229 subtests passed
```

### Type checks

```text
PYTHONPATH=src python -m mypy src
Success: no issues found in 572 source files

PYTHONPATH=src python -m mypy src --platform win32
Success: no issues found in 572 source files
```

### API contract

```text
PYTHONPATH=src python scripts/generate_api_reference.py --check
docs/API.md: in sync
```

The surface contains **628 routes**. AT-05 exposes twelve authenticated routes:

```text
GET    /market/autonomy/exits/contracts
GET    /market/autonomy/exits/status
GET    /market/autonomy/exits/positions
GET    /market/autonomy/exits/configurations
PUT    /market/autonomy/exits/configurations/{portfolio_id}/{instrument_id}
DELETE /market/autonomy/exits/configurations/{portfolio_id}/{instrument_id}
GET    /market/autonomy/exits/decisions
GET    /market/autonomy/exits/decisions/{decision_id}
GET    /market/autonomy/exits/decisions/{decision_id}/events
GET    /market/autonomy/exits/learning
POST   /market/autonomy/exits/run
POST   /market/autonomy/exits/decisions/{decision_id}/reconcile
```

The GET reads require ordinary API authentication. The mutations additionally require `X-Autonomy-Operator-Token`; ordinary authentication alone returns 403.

### Frontend build

```text
cd web && npm run build
✓ 1599 modules transformed
✓ built in 5.68s
```

The existing Vite chunk-size and mixed static/dynamic import warnings remain; they are non-blocking and pre-existing.

---

## 5. Real application verification

Script: `tmp/at05/verify_at05_completion.py`  
Evidence: `tmp/at05/data/completion_evidence.json`  
Result: **PASS — 28/28 checks** (`target: 16`, `trailing: 12`)

The script runs the actual `NovaControlApplication` and `NovaMarketService` against two fresh data directories. Only external market and forecast feeds are deterministic fixtures because this environment has no live provider. Wallet, portfolio, strategy, NM-08 risk, AT-03 decision, NM-10 execution, ledger, exit journal and NM-13 learning stores are the real application stores.

### Target scenario

| Item | Result |
|---|---|
| Entry | BUY filled: `ord_626e520081ad0cea669f3bf9` |
| Quantity / average cost | 90.9091 / 110.10 |
| Target | `115.60` |
| Restart | configuration survived; runtime came up `disabled` |
| Disabled exit run | `skipped / runtime_disabled` |
| Below target | 0 submitted; no decision |
| Monitoring at target | 1 decision, `monitoring`; 0 orders |
| Autonomous at target | decision `xdec_48b25864d593a2fca8208e89`; NM-08 `rdec_ca7e4b0fd03bfe22` approved |
| NM-10 sell | `ord_1ef7196b4c38ab0e495fd04b`; filled 90.9091 / remaining 0 |
| Position | closed |
| Cash after | 100,490.16 |
| Ledger | 2 authoritative transaction ids |
| Reconciliation | `passed` |
| Duplicate cycle | 0 submitted; still 1 sell order |
| Learning | `complete`; `auto_promoted=false` |

### Trailing scenario

| Item | Result |
|---|---|
| Entry | BUY filled: `ord_1da8dbb8f1924b23847e90e8` |
| Average cost | 110.10 |
| Trailing | 5.00% |
| High-water observation | 118.91 |
| Persisted high-water after restart | 118.9100 |
| Fall to 117.81 | no trigger; high-water stayed 118.9100 |
| Trigger price | 112.30; trailing stop 112.96 |
| Mode | `paused` |
| Decision | `partially_filled`, condition `trailing_stop` |
| Filled / remaining | 1.0000 / 89.9091 |
| Sell orders | 1 |
| Reconciliation | `passed` |
| Duplicate cycle | 0 submitted; still 1 sell order |
| Disabled run | `skipped / runtime_disabled` |
| Learning | `partial` — missing remaining position quantity |

Both scenarios complete in **3.44 s** on this machine. This is one run, not a benchmark promise.

---

## 6. UI verification

The Autonomy page was served from the Vite dev server against the real FastAPI application on a separate local port, using the verified trailing-exit data directory.

Live result after authentication:

- page title: `Autonomy · NovaMarket`;
- service state: `NOVAMARKET SERVICE: RUNNING`;
- operator token entered in the page enabled the controls; status showed the operator as verified;
- target price and trailing stop fields rendered;
- persisted configuration row showed trailing `5.00%`, high-water `118.9100`, trailing trigger `112.96`, last price `112.3000`;
- supervised position showed quantity `89.9091`, available `0`, entry `110.10`, trailing `112.96`, high-water `118.9100`, trigger `trailing_stop`, pending `89.9091`;
- learning row showed `Partial`, entry `ord_1da8…` → exit `ord_5024…`, realized `1.98`, missing `remaining position quantity`;
- no browser console error remained after the authenticated reload; authenticated data requests returned 200.

The browser inspection verified rendering and operator authorization. Configuration save/delete were exercised at the service and HTTP route layers; they were not clicked through in this browser session.

---

## 7. Limitations

1. **Long-only target/trailing support.** Short positions are explicitly unsupported for these conditions rather than approximated.
2. **Target is a fixed price per position.** No return-fraction or strategy-generated target was invented.
3. **Trailing is a percentage of the high-water mark.** No activation floor other than the configured percentage was added.
4. **Learning is advisory.** NM-13 can label a single observation as rejected or partial; AT-05 records the authoritative local evidence and never promotes automatically.
5. **Paper clearing liquidity is simulated issuance.** It exists only in `pfw_market_clearing` so profitable paper sells can settle; it is not portfolio cash or real money.
6. **The real-app run uses deterministic external market/forecast fixtures.** Financial stores and execution/accounting paths are real; no live market provider is assumed.
7. **No full repository suite was run.** The scoped AT-01–AT-05, NM-08, NM-10, wallet/ledger, learning, API and UI suites were run as required.

---

## 8. Files

### New AT-05 package

- `src/novacontrol/novamarket/autonomy_exits/__init__.py`
- `src/novacontrol/novamarket/autonomy_exits/config.py`
- `src/novacontrol/novamarket/autonomy_exits/models.py`
- `src/novacontrol/novamarket/autonomy_exits/pipeline.py`

### Modified integration and storage

- `src/novacontrol/novamarket/execution/store.py`
- `src/novacontrol/novamarket/execution/engine.py`
- `src/novacontrol/novamarket/service.py`
- `src/novacontrol/api/app.py`
- `src/novacontrol/api/models.py`
- `src/novacontrol/api/route_consumers.py`
- `docs/API.md`

### Frontend

- `web/src/api.ts`
- `web/src/pages/Autonomy.tsx`

### Tests and evidence

- `tests/test_novamarket_at05.py`
- `tests/test_novamarket_api.py`
- `tmp/at05/verify_at05_completion.py`
- `tmp/at05/data/completion_evidence.json`

No commit, staging, push or tag was performed. The repository had pre-existing uncommitted and untracked work; AT-05 did not revert or stage it.

---

## 9. Final verdict

**PASS**

Target-price and trailing-stop supervision, persistence, high-water recovery, NM-08/NM-10 integration, partial-fill handling, API and UI are implemented and verified. The focused suite, prerequisite regressions, NM-10/wallet/learning regressions, API/UI regressions, both mypy views, the frontend build and the 28-check real-application run pass.

The AT-05 prerequisite gate is closed. AT-06 was subsequently implemented and accepted in `AUTONOMOUS_TRADING_AT06_REPORT.md`; this report records AT-05 only.
