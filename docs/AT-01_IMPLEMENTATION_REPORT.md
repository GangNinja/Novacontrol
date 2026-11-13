# AT-01 — Runtime and Safety Shell: Implementation and Verification Report

**Verdict:** **PASS** for the approved AT-01 scope, with the limitations stated in §9.

**Date:** 10 November 2026

**Authoritative decisions:** `AUTONOMOUS_TRADING_OWNER_DECISIONS.md` (owner-approved 10 November 2026).

**Baseline:** branch `novamarket/nm-01`, HEAD `afa5ff64c7673553b333fd3cff07cf5dc7c832b7`, captured in `AT-01_GIT_BASELINE.md`.

---

## 1. Scope executed

AT-01 only: the durable runtime and safety shell.

Implemented:

- operating modes `disabled`, `monitoring`, `autonomous`, `paused` with validated transitions;
- durable SQLite state, configuration row and runtime row;
- one runtime owner enforced by a durable lease (acquire, heartbeat, release, expiry, recovery);
- at most one active cycle record, enforced by a unique partial index;
- safe restart: every restart comes up `disabled` with no automatic resume;
- dedicated server-side autonomy-operator authentication for controls;
- server-authoritative status, health and contract surfaces;
- an additive React Autonomy page with monitoring controls only.

Deliberately **not** implemented (AT-02–AT-06 are not approved):

- scanning, discovery, signals, proposals, decisions or risk evaluation;
- paper orders, fills, exits, cancellation or any execution call;
- cash, position, holdings or ledger mutation; `PortfolioEngine.apply()` is never called;
- configuration editing routes, cycle history, kill switch, learning promotion;
- day-trading forced flattening — **PENDING_OWNER_DECISION**, neither implemented nor invented.

No buy/sell signal, proposal, order, fill or exit can be produced by this code, and no route under `/market/autonomy` creates one.

---

## 2. Owner decisions recorded

`AUTONOMOUS_TRADING_OWNER_DECISIONS.md` records six approved decisions and one pending question:

| ID | Decision | Status |
|---|---|---|
| OD-01 | `short_term → day_trading`, `medium_term → swing_trading`, `long_term → long_term` | Owner-approved |
| OD-02 | Paused blocks entries, keeps protective exits; `paused_exit_policy="protective"` fixed, `monitor_only` unsupported | Owner-approved |
| OD-03 | No target-weight autonomy in v1; eventual path is proposal → NM-08 → NM-10 | Owner-approved |
| OD-04 | Only verified calendar coverage (2026) is used; never extrapolated; fail closed outside it | Owner-approved |
| OD-05 | Dedicated server-side autonomy-operator permission; ordinary auth insufficient; unset fails closed | Owner-approved |
| OD-06 | No automatic resume; a restart comes up `disabled` | Owner-approved |
| PENDING-1 | Forced flattening of day-trading positions before the close | Pending owner decision |

The earlier autonomy documents were reconciled to these decisions: `AUTONOMOUS_TRADING_ARCHITECTURE.md`, `AUTONOMOUS_TRADING_API_DESIGN.md`, `AUTONOMOUS_TRADING_ARCHITECTURE_REVIEW.md`, `AUTONOMOUS_TRADING_IMPLEMENTATION_ROADMAP.md`, `AUTONOMOUS_TRADING_STATE_MACHINE.md`, `AUTONOMOUS_TRADING_DESIGN_DECISIONS.md`, `AUTONOMOUS_TRADING_COMPONENT_MAPPING.md`, `AUTONOMOUS_TRADING_GAP_MATRIX.md` and `AUTONOMOUS_TRADING_TEST_STRATEGY.md`.

Removed or replaced active contradictions: “Proposed” statuses, `monitor_only` as a supported paused policy, `resume_on_restart`, `require_auth` sufficiency, `start(autonomous)=409` for the mode shell, lease-per-cycle wording for the runtime-use lease, and 2027 calendar assumptions.

---

## 3. Implementation inventory

### Backend

- `src/novacontrol/novamarket/autonomy/__init__.py` — public exports.
- `src/novacontrol/novamarket/autonomy/models.py` — modes, runtime states, transitions, constants, `AutonomyConfig`, horizon mapping, calendar status, typed errors.
- `src/novacontrol/novamarket/autonomy/store.py` — SQLite schema v1 (`autonomy_meta`, `autonomy_config`, `autonomy_runtime`, `autonomy_cycles`), lease operations, cycle claim/finish, recovery, `close()`.
- `src/novacontrol/novamarket/autonomy/runtime.py` — `AutonomyRuntime` mode/lease/cycle shell, paper-only assertions, explicit `recover()`, idempotent `stop()`, `close()`.
- `src/novacontrol/novamarket/service.py` — lazy runtime construction, recovery point at `start_runtime()`, status/contract readers, lifecycle controls, teardown that closes the autonomy store.
- `src/novacontrol/api/app.py` — seven routes, operator-token dependency, error mapping, CORS header allowance.
- `src/novacontrol/api/models.py` — `ApiSurface` rows for the seven routes.
- `src/novacontrol/api/route_consumers.py` — UI consumer declarations.
- `src/novacontrol/novamarket/horizons.py` — trading-style horizon mapping (`horizon_mapping` in the contract).
- `src/novacontrol/novamarket/marketdata/calendar.py` — `verified` and `maintenance` metadata; 2026 remains the only verified year.

### Frontend

- `web/src/pages/Autonomy.tsx` — authoritative status, operator-token field, lease/health cards, monitoring start/pause/resume/stop, explicit “trading not implemented” messaging.
- `web/src/App.tsx`, `web/src/nav.ts` — route and navigation entry.
- `web/src/api.ts`, `web/src/hooks.ts` — `operatorToken` support.
- `web/src/pages/Dashboard.tsx`, `web/src/pages/Portfolios.tsx`, `web/src/pages/System.tsx` — day/swing terminology, autonomy card.
- No autonomous-start button and no order/scan/decision UI.

### Schema v1

Additive tables only; the market-data store’s `PRAGMA user_version` is untouched.

- `autonomy_meta` — schema version.
- `autonomy_config` — single validated configuration row (`config_version`, portfolio allowlist, `paused_exit_policy`, lease/cycle bounds).
- `autonomy_runtime` — single runtime row (`mode`, `desired_mode`, `runtime_state`, owner, lease token/expiry, heartbeat, active cycle, last error/reason).
- `autonomy_cycles` — durable cycle intent with unique `cycle_key` and a unique partial index on `status='running'` (one active cycle).

---

## 4. Safety invariants and acceptance matrix

| AT-01 requirement | Implementation | Evidence | Status |
|---|---|---|---|
| Four modes, durable | `AutonomyMode` + `autonomy_runtime.mode` | `test_modes_and_transitions_are_validated`, restart tests | PASS |
| Validated transitions; illegal ones raise | `transition_allowed`, `_set_mode` | `test_modes_and_transitions_are_validated`, `test_paused_requires_resume` | PASS |
| Safe restart, no auto-resume | `recover()` forces `disabled`; restart test reopens store | `test_restart_comes_up_disabled_and_never_resumes` | PASS |
| One runtime owner | Conditional lease acquire in SQLite | `test_lease_prevents_a_second_live_owner`, `test_expired_lease_is_reported_and_recoverable` | PASS |
| One active cycle | Unique partial index + `claim_cycle` | `test_one_cycle_at_a_time_is_enforced_by_sqlite` | PASS |
| Durable state survives reopen | Same SQLite file, schema version | `test_state_survives_a_restart` | PASS |
| Dedicated operator permission | `X-Autonomy-Operator-Token` vs `NOVACONTROL_AUTONOMY_OPERATOR_TOKEN` | `AutonomyApiAuthTests` (ordinary auth 403, token works, unset fails closed) | PASS |
| No order path | No execution/portfolio/wallet import or route | AST-scan test + `test_no_order_route_exists` | PASS |
| Paper-only preserved | `paper_only=true`, `live_trading=false`, `broker_connected=false`, `orders_possible=false`, `release_gate="closed"` | status/contract assertions | PASS |
| One activity surface / no financial mutation | No engine imports; status is read-only | AST-scan test | PASS |
| Health honest under foreign/expired lease | `degraded`, `recovery_required=true`, never fake running | lease conflict tests | PASS |
| Flattening not invented | No flattening code or claim | grep + owner decision PENDING-1 | PASS (by exclusion) |

`autonomous` is a **state-only shell**: it persists as a mode, but `trading_implemented=false` and `orders_possible=false` are reported, and the release gate stays closed. The old `409 autonomy_not_released` rule now applies only to future order/scan work (AT-04/AT-06), not to entering the shell.

---

## 5. Verification evidence

All commands were run in `novamarket/` on 10 November 2026.

| Check | Command | Result |
|---|---|---|
| AT-01 unit/integration/API tests | `PYTHONPATH=src python -m pytest tests/test_novamarket_autonomy.py -q` | **14 passed** |
| Focused regression set | `PYTHONPATH=src python -m pytest tests/test_novamarket_autonomy.py tests/test_novamarket_api.py tests/test_novamarket.py -q` | **153 passed, 282 subtests passed** |
| Type check (default) | `python -m mypy src` | **Success: no issues found in 559 source files** |
| Type check (Windows view) | `python -m mypy src --platform win32` | **Success: no issues found in 559 source files** |
| API reference in sync | `python scripts/generate_api_reference.py --check` | **`docs/API.md: in sync`** (404 documented routes) |
| Route registry | `ApiSurface.default().routes` | **593 routes** total; exactly **7** `/market/autonomy/*` routes |
| Frontend production build | `npm run build` (in `web/`) | **exit 0**, built in 6.77 s |
| Preserved QA fixes | BUG-QA-001 and BUG-QA-002 tests + `tests/test_novamarket_workbench_ui.py::…flat_series…` | **3 passed** |

### API surface added

| Method | Path |
|---|---|
| GET | `/market/autonomy/contracts` |
| GET | `/market/autonomy/status` |
| GET | `/market/autonomy/health` |
| POST | `/market/autonomy/start` |
| POST | `/market/autonomy/pause` |
| POST | `/market/autonomy/resume` |
| POST | `/market/autonomy/stop` |

No route creates, fills, cancels or edits an order.

Documented failure statuses:

- **401** — ordinary authentication missing;
- **403** — missing/wrong operator credential, or the server operator token is unset (fail closed);
- **409** — lease conflict or invalid state transition;
- **422** — request validation (for example `autonomous` without `acknowledge_paper_only=true`);
- **503** — autonomy store unavailable.

### Live API smoke test (run earlier in this task)

With a local server: ordinary authenticated control returned **403**; the same call with the operator token performed start → pause → resume → stop successfully. This was a manual smoke check, not part of the repeatable suite.

---

## 6. Defects found and fixed during verification

1. **SQLite handle leak on shutdown (Windows).** The autonomy store opened the NovaMarket SQLite file and nothing closed it. `TemporaryDirectory` cleanup failed with `PermissionError: [WinError 32]` in 57 existing tests. Fixed by closing the runtime/store in `NovaMarketService._withdraw()` and by reporting a static closed-shell status (`closed_status`) when the subsystem is not running, so a status read never reopens the store. `AutonomyRuntime.close()` is idempotent and releases the lease first. The 57 failures went to zero.
2. **Stale route-count assertions.** `test_novamarket.py` expected 214 `/market/` decorators (now 221) and `test_novamarket_api.py`'s forbidden-route set did not include the seven new autonomy paths. Both expectations were updated to the real surface; the test that enforces no live-execution route now includes the autonomy paths explicitly.
3. **Two mypy errors.** `AutonomyValidationError` was used in `store.py` without being imported, and `runtime.py` carried an unused `type: ignore`. Both fixed; mypy is clean in both views.

No test was skipped, weakened or deleted to obtain a pass.

---

## 7. Preserved QA fixes

- **BUG-QA-001** — dashboard horizon status is not the first book's status: test passes.
- **BUG-QA-002** — market pulse uses quoted observations: test passes.
- **BUG-QA-003** — flat valuation series expands to readable ticks: test passes.

No financial formula, risk policy, fee, slippage, ledger rule, valuation rule or broker boundary was changed.

---

## 8. Change control

- Branch `novamarket/nm-01`, HEAD `afa5ff64c7673553b333fd3cff07cf5dc7c832b7`.
- **No** commit, push, stage, reset, checkout, stash or discard was performed.
- The worktree was already dirty before AT-01 (16 modified tracked files; many untracked NovaMarket files hidden by the parent repository’s `.git/info/exclude`). `AT-01_GIT_BASELINE.md` records the pre-change status and the SHA-256 manifest; the dirty state is not attributable to AT-01 alone.
- New AT-01 files are untracked in this worktree, consistent with the existing NovaMarket baseline.

---

## 9. Limitations and unverified items

1. **UI not visually verified in a browser.** The production build passes and the renderer/static wiring is tested, but no live browser session or screenshot was taken in this verification. The Autonomy page's visual layout and interaction should be checked by loading it against a running server.
2. **No live server run in the final pass.** API behavior is proven through FastAPI `TestClient` tests and the earlier manual smoke test; the final repeatable verification did not start uvicorn.
3. **Full-suite run not repeated.** The focused 153-test set covers NovaMarket autonomic and API behavior; the repository-wide suite was not rerun for this report.
4. **Forced flattening remains open.** Day-trading positions are not flattened before the close; this is `PENDING_OWNER_DECISION` and no behavior is claimed.
5. **AT-02–AT-06 are not implemented.** Monitoring performs no scan, discovery, decision or order action; `autonomous` is a state-only shell.
6. **Operator token provisioning is deployment work.** The server reads `NOVACONTROL_AUTONOMY_OPERATOR_TOKEN`; without it, controls return 403 by design. The UI field carries the operator credential per request and does not persist it.

---

## 10. Verdict

**PASS** for AT-01 — Runtime and Safety Shell, subject to the limitations in §9. The shell is durable, single-owner, fail-closed, operator-gated, restart-safe, free of any order or financial-mutation path, and leaves the paper-only boundary and the three QA fixes intact. AT-02 must not start until this report and the owner decisions are accepted.
