# NovaMarket NM-R1 — Final Integration and Release Audit

**Project:** NovaMarket  
**Audit:** NM-R1 — final integration and release readiness  
**Date:** 2026-11-11  
**Auditor:** Buffy / Codebuff  
**Environment:** Windows, Python 3.13, local SQLite stores, FastAPI on `127.0.0.1:8011`, Vite on `127.0.0.1:5173`  
**Verdict:** **PARTIAL — not released** (see §15)

---

## 1. Executive summary

NovaMarket was audited as an integrated application after the accepted AT-01 through AT-06 baseline. The audit covered the real API, the real browser UI, runtime modes, lease behaviour, recovery, provider connectivity, accounting authorities and targeted regressions.

### What passed

- All six milestone reports and the final acceptance report are present with PASS/ACCEPTED verdicts; the AT-05 prerequisite closure is CLOSED.
- The accepted authority boundaries remain intact: NM-08 is the only risk authority, NM-10 is the only paper-execution authority, and portfolio/cash/ledger records remain authoritative.
- Paper-only invariants remain true in every live status checked: `paper_only=true`, `live_trading=false`, `broker_connected=false`, no broker or live-order path.
- The real application started and served both the API and the Autonomy page. Runtime controls worked in the browser after operator authentication: start → monitoring, pause → paused, resume → monitoring, stop → disabled.
- Autonomous mode was entered through the operator API and rendered correctly in the UI; the scheduler reported Running and a real monitoring attempt was recorded as deferred because the market was closed.
- Yahoo and CoinGecko both responded to live probes. A real Yahoo quote for `XNSE:RELIANCE` was refreshed successfully and was correctly labelled as a closed-market (non-live) price. A pre-refresh read was correctly labelled stale.
- The complete deterministic financial lifecycle and all three protective-exit mechanisms passed their real-engine suites. The AT-05/AT-06 group reported **59 passed**; the final autonomy/AT-06 rerun reported **41 passed** after the recovery fix.
- Both mypy views, the API-reference check and the React/Vite production build passed. `docs/API.md` is in sync.

### Defects found and fixed during the audit

1. **High — lease heartbeat was never started.** The runtime lease expired after 60 seconds and the runtime degraded during a closed market, because the service's market loop slept for 900 seconds and nothing renewed the lease.
2. **High — recovery preserved the caller's own live lease.** Recovery reported `disabled`, but the durable lease remained held and could block the next start. Recovery now releases its own lease before clearing state.
3. **Medium — stale Autonomy UI after a control action.** Pausing resumed the runtime header but left downstream cards showing `disabled` until each card was refreshed manually. The control action now refreshes the whole autonomy surface.

The two backend fixes were verified by new regression tests and by live API checks; the UI refresh fix was verified in the browser. The final live state was then checked again after recovery.

### Why the verdict is PARTIAL

The market was closed during the entire live audit (23:19–23:36 IST) and the live application contained no open positions. Therefore:

- the complete paper-trade lifecycle was not exercised through the live UI with a real market session;
- stop-loss, target and trailing exits were proved by the deterministic real-engine suites, not by a live-market browser pass;
- provider failure, rate-limit and failover recovery were not exercised against the live providers;
- the UI does not expose a dedicated recovery summary/action or the unresolved-reconciliation list.

Those are required verification items, not cosmetic gaps. Under the NM-R1 verdict rules this is **PARTIAL**, not PASS.

No commit, staging, push or tag was performed.

---

## 2. Prerequisite verification

| Prerequisite | Document | Reported verdict | Audit result |
|---|---|---|---|
| AT-01 runtime and safety shell | `docs/AT-01_IMPLEMENTATION_REPORT.md` | PASS, scoped | Present and consistent |
| AT-02 session-aware monitoring | `docs/AUTONOMOUS_TRADING_AT02_REPORT.md` | PASS | Present and consistent |
| AT-03 decision and NM-08 pipeline | `AUTONOMOUS_TRADING_AT03_REPORT.md` | PASS | Present and consistent |
| AT-04 paper execution and reconciliation | `AUTONOMOUS_TRADING_AT04_REPORT.md` | PASS | Present and consistent |
| AT-05 exit supervision and learning evidence | `AUTONOMOUS_TRADING_AT05_REPORT.md` | PASS | Present and consistent |
| AT-06 recovery, observability and acceptance | `AUTONOMOUS_TRADING_AT06_REPORT.md` | PASS | Present and consistent |
| Final acceptance | `AUTONOMOUS_TRADING_FINAL_ACCEPTANCE_REPORT.md` | ACCEPTED — PASS | Present and consistent |
| AT-05 prerequisite closure | `docs/AT05_PREREQUISITE_CLOSURE.md` | CLOSED — AT-05 PASS | Present and consistent |

The accepted reports were not rewritten. Their documented AT-01/AT-06 limitations remain valid. The audit used the reports as the baseline and tested the integrated application against it.

---

## 3. Architecture preservation audit

| Boundary | Evidence | Result |
|---|---|---|
| NM-08 sole risk authority | AT-03 and AT-05 tests pass; no second risk engine was added | Preserved |
| NM-10 sole paper-execution authority | AT-04/AT-05 tests use `preflight`/`submit`; no synthetic fills | Preserved |
| Portfolio, cash, valuation and ledger authoritative | Exit and lifecycle tests read authoritative records and reconcile | Preserved |
| No direct financial mutation from autonomy | Safety/AST tests in AT-05/AT-06 pass; no `PortfolioEngine.apply()` path added | Preserved |
| Paper-only operation | Live status and tests: `paper_only=true`, `live_trading=false` | Preserved |
| Runtime modes | Live disabled, monitoring, paused and autonomous checks | Preserved |
| One active lease / one cycle | Runtime tests and live lease observations | Preserved |
| 20-instrument discovery cap | AT-02 configuration/tests; live scheduler reported batch 20 of 20 | Preserved |
| 15-minute data refresh floor | Live config reported 900 seconds | Preserved |
| Verified calendar / fail-closed | Live session reported verified NSE 2026 coverage and `MARKET_CLOSED` | Preserved |
| Stop-loss, target, trailing supervision | AT-05/AT-06 real-engine suites | Preserved |
| Long-only target/trailing limitation | AT-05 contract and tests | Preserved |
| Partial fills and duplicate suppression | AT-04/AT-05/AT-06 scenarios | Preserved |
| Recovery returns to disabled | Live recovery plus tests; recovery lease defect fixed in this audit | Preserved |
| No forced EOD liquidation, rebalancing, auto-promotion or short exits | No such path found; accepted non-goals remain | Preserved |

The audit changes are lifecycle/integration fixes only:

- `src/novacontrol/novamarket/service.py` — starts and stops the autonomy lease heartbeat with the service runtime.
- `src/novacontrol/novamarket/autonomy/runtime.py` — recovery releases this owner's live lease before clearing ownership.
- `web/src/pages/Autonomy.tsx` — refreshes all autonomy cards after a successful control action.
- `tests/test_novamarket_autonomy.py` — adds lease-renewal and live-lease recovery regressions.

No new risk engine, execution engine, broker adapter, order route or financial-store write was introduced.

### Shell-status note

`GET /market/autonomy/status` still reports AT-01 shell fields such as `trading_implemented=false`, `orders_possible=false`, `release_gate="closed"` and `exit_supervision_implemented=false`. These describe the AT-01 shell, not the complete AT-04/AT-05 pipeline. The Autonomy page and AT-06 reliability endpoint read the pipeline statuses separately. This is an established, documented limitation, not a newly discovered contradiction.

---

## 4. Real application test results

### Startup and serving

The normal server was launched on `127.0.0.1:8011` with a temporary local `NOVACONTROL_AUTONOMY_OPERATOR_TOKEN` for the audit. The UI was served by a fresh Vite process on `127.0.0.1:5173` with the proxy pointed at `8011`.

- `GET /market/status` responded successfully after startup.
- `GET /market/autonomy/status` returned 200.
- `GET /market/autonomy/reliability` returned 200.
- The Autonomy page loaded with no “Backend offline” state and no application 500s after the stale Vite proxy was replaced.
- The initial “Backend offline” page was caused by an old Vite process on 5174 proxying requests to a dead API port. Replacing that environmental process with a fresh server fixed it; it was not a NovaMarket API defect.

### Runtime modes

| Mode | Live check | Observed result |
|---|---|---|
| `disabled` | Fresh application start / post-stop / post-recovery | `disabled`, `stopped`, lease not held |
| `monitoring` | UI “Start monitoring” | `monitoring`, `running`, lease held |
| `paused` | UI “Pause” | `paused`, `running`, lease held; new entries blocked |
| `autonomous` | Operator API start with paper acknowledgement | `autonomous`, `running`; no broker or live order |
| Stop / kill switch | UI confirmation | `disabled`, lease released, no new work |
| Recovery | Operator API while autonomous | `disabled`, no lease, no auto-resume |

The live autonomous-mode checks produced:

- monitoring cycle: `deferred`, reason `OUTSIDE_TRADING_SESSION` / market closed;
- decision pipeline: 409 `MONITORING_CYCLE_REQUIRED` (no completed eligible cycle);
- execution pipeline: completed with **0** intents, orders and fills;
- exit supervision: completed with 0 positions and 0 triggers;
- all live status payloads still reported `paper_only=true`, `live_trading=false`, `broker_connected=false`.

### Scheduler, lease and one-cycle behaviour

The live service initially exposed the key integration defect: `start` acquired a lease with a 60-second expiry, but no application component called `heartbeat()`. After 60 seconds the runtime became `degraded` with `the runtime lease has expired`, even though the operator had not stopped it. The market was closed, so the 900-second market loop was not a renewal path.

After the fix:

- the service starts a dedicated heartbeat task with the runtime (`novamarket-autonomy-heartbeat`);
- it renews at one third of the lease duration (20 seconds for a 60-second lease);
- `stop_runtime()` cancels the task before stopping the runtime;
- a live 75-second observation remained `monitoring/running`, health `ok`, with a renewed expiry;
- the new regression `test_running_lease_is_renewed_without_a_market_cycle` passes.

One-cycle and lease exclusivity remain covered by the AT-01 runtime tests and the AT-06 lifecycle suite. No live concurrent-cycle injection was performed; the application exposes one scheduler and one cycle slot, and the deterministic suite covers duplicate/concurrent attempts.

---

## 5. UI and browser verification

Browser verification used the live Autonomy page at `http://127.0.0.1:5173/#/autonomy`.

### Verified

- The page loaded without the previous “Backend offline” banner.
- The dedicated operator token field accepted the local audit token; the page changed from “Not Presented / Controls Locked” to “Verified / Controls Enabled”.
- “Start monitoring” posted successfully and rendered `Monitoring / Running`, a held lease and health `Ok`.
- “Pause” rendered `Paused / Running`, kept the lease, disabled paper entry semantics and refreshed the decision/execution/exit cards.
- “Resume monitoring” returned the page to `Monitoring / Running`.
- “Stop and release lease” opened the confirmation dialog and, after confirmation, rendered `Disabled / Stopped` with the lease released.
- Reloading after recovery rendered `Disabled / Stopped`, `Lease Not held`, and no backend-offline banner.
- The page rendered the reliability line “Paper pipeline: needs attention” while the execution card was degraded because autonomous forwarding was inactive in monitoring mode. That is an honest state, not a fabricated success.
- The page rendered the verified NSE calendar, market-closed state, session dates and the 2026 coverage warning.

### Fixed during the audit

Before the UI refresh fix, a control action refreshed only `/market/autonomy/status`. After starting or pausing, the runtime header changed but downstream cards still showed the previous mode, including the contradictory “runtime mode is disabled” warnings. `control()` now refreshes status, monitoring, decisions, executions, exits, positions and reliability after a successful action. Pause was verified in the browser after the fix: the decision and execution cards changed to `Paused` without a manual card refresh.

### Remaining UI findings

- **Low:** “Run decision pipeline” is enabled while the runtime is `paused`; the backend correctly refuses it. The UI should also disable it in paused mode.
- **Medium:** the page has no dedicated recovery summary/action or unresolved-reconciliation list. The AT-06 recovery endpoint is not directly operable from the UI and the page only exposes an aggregate “Paper pipeline” line. API recovery was verified; UI recovery presentation was not.
- **Low / scope limitation:** the UI starts monitoring, not autonomous mode. Autonomous mode is reachable through the operator API. If the release requires every mode to be UI-operable, this is an additional gap.

No screenshot was used as acceptance evidence; the rendered page text and actual HTTP requests were used.

---

## 6. Complete financial lifecycle results

The deterministic lifecycle suites use the real wallet, portfolio, risk, strategy, decision, execution, ledger, exit and learning stores. Only market/forecast providers are deterministic fixtures.

Verified chain:

```text
Market data → Discovery → Strategy evaluation → Signal → Proposal
→ NM-08 risk assessment → AT-04 durable intent → NM-10 paper order
→ Fill → Ledger → Position → Exit supervision → Exit proposal
→ NM-08 → NM-10 exit order → Exit fill → Reconciliation → Learning evidence
```

| Lifecycle area | Evidence | Result |
|---|---|---|
| Approved entry creates one NM-10 order and fill | AT-06 scenario 5 | PASS |
| Rejected entry never reaches NM-10 | AT-06 scenario 6 | PASS |
| Duplicate proposal reuses the order | AT-06 scenario 7 | PASS |
| Submission timeout reconciles before retry | AT-06 scenario 8 | PASS |
| Partial entry keeps the authoritative remainder | AT-06 scenario 9 | PASS |
| Full entry reconciles position and ledger | AT-06 scenario 10 | PASS |
| Learning evidence links entry and exit, no promotion | AT-06 scenario 19 | PASS |
| Kill switch prevents new entries/exits | AT-06 scenario 20 | PASS |

Final targeted runs:

```text
tests/test_novamarket_at05.py tests/test_novamarket_at06.py     59 passed
tests/test_novamarket_autonomy.py tests/test_novamarket_at06.py 41 passed
```

The 41-test final run contains the 16 autonomy tests (including the two new lease regressions) and the 25 AT-06 tests. The 34 AT-05 tests were run separately in the 59-test group before the final runtime-only recovery fix; the final recovery fix does not alter exit logic, and AT-06 was rerun after it.

### Live limitation

The live application had no open positions and the market was closed. The live UI therefore showed 0 positions, 0 execution intents, 0 orders and 0 fills. The audit did **not** execute a live paper trade or a live exit. The deterministic lifecycle proves the engine path, not live-market execution.

---

## 7. Stop-loss, target-price and trailing-stop results

The three mechanisms were verified separately by the real-engine AT-05/AT-06 suites.

| Mechanism | Evidence | Result |
|---|---|---|
| Stop-loss | AT-05 `StopLossTests`; AT-06 scenario 11 | PASS — trigger, NM-08 approval, one NM-10 sell, fill, ledger, reconciliation |
| Fixed target price | AT-05 `TargetPriceTests`; AT-06 scenario 12 | PASS — target threshold, gap-through, order, fill, reconciliation |
| Trailing stop | AT-05 `TrailingStopTests`; AT-06 scenarios 21–22 | PASS — high-water mark rises, never falls, triggers after retracement, survives restart |
| Partial exit | AT-05 partial tests; AT-06 scenario 15 | PASS — positive remainder, no oversell, duplicate suppressed |
| Duplicate exit | AT-05/AT-06 duplicate tests | PASS — one decision and one sell order |
| Pending exit recovery | AT-05 restart tests; AT-06 scenario 18 | PASS — pending partial reconciled, no second order |
| Paused-mode protective exit | AT-05 mode tests; AT-06 scenario 14 | PASS — paused blocks entries but submits the protective exit |
| Stale/missing data | AT-05 stale/missing tests | PASS — no trigger and no order when data is stale or unusable |
| NM-08 rejection | AT-05 target/stop rejection tests | PASS — rejected decision, no NM-10 order |

No live position existed to exercise the exit UI or a live exit order. The accepted long-only limitation for target/trailing remains in force.

---

## 8. Recovery results

### Defect found and fixed

When recovery was called while the runtime held a live lease, `AutonomyStore.recover()` preserved the lease because it treats any unexpired owner as live. `AutonomyRuntime.recover()` cleared only its in-memory token, so the subsequent status read reported `disabled` while still showing a valid held lease. This could block the next start.

The runtime now releases its own lease through `release_lease()` before calling store recovery. The regression `test_recovery_with_a_live_lease_releases_it_and_allows_restart` passes.

### Live evidence

```text
POST /market/autonomy/recover while autonomous:
  200, status completed, mode disabled, recovered 0, reconciled 0,
  unresolved 0, auto_resumed false, runtime lease held false

GET /market/autonomy/status afterwards:
  mode disabled, runtime_state stopped, health ok, lease held false

POST /market/autonomy/start afterwards:
  200, mode monitoring, lease held true
```

Recovery latency in this live check was **34.81 ms**. The earlier disabled-state recovery check measured **2.56 ms**. Recovery correctly reports unresolved records instead of inventing success; no unresolved work existed in this clean audit store.

### Coverage still missing

The deterministic suite covers restart recovery, interrupted cycles, pending orders, partial exits and duplicate recovery. The live audit did not have a pending order or partial exit to reconcile, so live reconciliation of actual financial records was not exercised. The UI does not expose the recovery result or unresolved list.

---

## 9. Market-data provider verification

### Configured providers

| Provider | Enabled | Credentials | Live probe | Claim |
|---|---|---|---|---|
| Yahoo | Yes | None | 200, latency ~431.6 ms, verified `quote` and `ohlcv` | Unofficial delayed free data |
| CoinGecko | Yes | None | 200, latency ~617.6 ms, verified `probe` | Delayed aggregate crypto data |
| Alpha Vantage | No | Requires a key, none installed | Not enabled | Delayed free tier; unverified |

The store reported **4,785 instruments**, **33,424 bars** and **179 observations** before the quote refresh. It is append-only and provider-aware.

### Live quote check

`GET /market/data/quote/XNSE:RELIANCE?refresh=true` returned a real Yahoo observation:

- instrument `XNSE:RELIANCE`, currency INR;
- price `1335.4`;
- observed at `2026-11-11T09:59:54+00:00` (15:29:54 IST);
- retrieved at `2026-11-11T18:02:34Z`;
- quality `valid`, freshness `closed_market`, `live=false`;
- provenance `yahoo`, symbol `RELIANCE.NS`, market state closed.

A pre-refresh read returned an older observation (`1313.4`, observed 2026-11-05) with quality/freshness `stale`, `live=false`, age ~547,441 seconds and a reason that it was not a current quote. The refresh replaced it with the current completed-session price. This demonstrates real provider access and honest stale/closed labelling.

### Rate and coverage limits

- Yahoo: nominal 60 requests/minute; unofficial endpoint; intraday history is limited by interval; NSE/BSE use `.NS`/`.BO`.
- CoinGecko: nominal 10 requests/minute; free OHLC is 30-minute granularity and has no volume; crypto only.
- Alpha Vantage: installed but disabled; if enabled it requires credentials, with a 5/minute and 25/day free-tier limit.

No exchange-grade, tick-level or real-time claim is made.

### Failure behaviour

The deterministic tests cover missing/unusable/stale data rejection, provider circuit state and readiness. The live audit verified liveness and a real quote, but it did **not** induce a live provider outage, rate-limit response or failover. That remains required evidence for a fully verified provider-reliability claim. The application fails closed when the verified session is unavailable; the live monitoring attempt was correctly deferred because the market was closed.

---

## 10. Security and authorization findings

- Every autonomy mutation was tested without the operator token and with a wrong token. `pause`, `recover` and the other control routes returned **403** with `autonomy_operator_forbidden`. No state change occurred.
- The operator token is separate from the ordinary API bearer token. The UI holds it in page memory only and the status payload never returns it.
- The API was bound to `127.0.0.1`, not a public interface.
- Ordinary API authentication was **not configured** in this audit environment. Reads were therefore unauthenticated. This is a configuration finding, not proof that the code is unsafe when a token is configured: the server was loopback-only, and the unauthenticated control routes still failed closed.
- **Release blocker for any non-loopback deployment:** set `NOVACONTROL_API_TOKEN` and keep the process bound to a trusted interface; otherwise any local client can read portfolio and execution data.
- No broker credential, live-order route, order-cancellation path or direct financial mutation was added.
- Error responses for controls were structured and did not echo the token.

The temporary operator token used for the audit was supplied only in the process environment. It is not recorded in this report, the repository or the frontend storage.

---

## 11. Regression results

| Check | Command / scope | Result |
|---|---|---|
| Autonomy + AT-06 (final) | `pytest tests/test_novamarket_autonomy.py tests/test_novamarket_at06.py -q` | **41 passed** |
| AT-05 + AT-06 | `pytest tests/test_novamarket_at05.py tests/test_novamarket_at06.py -q` | **59 passed** |
| AT-02 + AT-03 + route contract | `pytest tests/test_novamarket_at02.py tests/test_novamarket_at03.py tests/test_novamarket_api.py::NovaMarketRouteContractTests -q` | **87 passed, 223 subtests passed** |
| API + workbench UI | `pytest tests/test_novamarket_api.py tests/test_novamarket_workbench_ui.py -q` | **64 passed, 231 subtests passed** |
| Risk, execution, wallet, learning | `pytest tests/test_novamarket_risk.py tests/test_novamarket_execution.py tests/test_novamarket_wallet.py tests/test_novamarket_learning.py -q` | **182 passed, 22 subtests passed** |
| Type checks | `mypy src` and `mypy src --platform win32` | **573 files clean in both views** |
| API reference | `python scripts/generate_api_reference.py --check` | **docs/API.md in sync** |
| Frontend | `npm run build` | **1,599 modules transformed; build succeeded** |
| Whitespace | `git diff --check` | Clean |

The full repository suite was not rerun; the audit ran targeted NovaMarket groups, which is the appropriate scope for this integration milestone. The 64-test API/workbench group was run before the final runtime-only recovery fix; the final autonomy/AT-06 group was rerun afterward and covers the changed API behaviour. No tests were skipped or weakened to obtain these results.

---

## 12. Performance measurements

Measurements are from this machine and this audit session. They are not a production SLA or capacity claim.

| Measurement | Method | Result |
|---|---|---|
| In-process application construction | `create_app()` | 1,129.31 ms |
| In-process lifespan startup to ready | `TestClient` startup | 456.15 ms |
| First `/market/status` | In-process, after startup | 17.98 ms |
| In-process shutdown | `TestClient` exit | 2,051.64 ms |
| Reliability endpoint | 25 sequential HTTP samples | min 1.93 ms, median 13.78 ms, p95 25.41 ms, max 29.99 ms |
| Live recovery while autonomous | timed `POST /recover` | 34.81 ms |
| AT-06 exit cycle mean | 60 deterministic duplicate cycles | 29.02 ms/cycle |
| AT-06 sustained elapsed | 60 cycles | 1.7412 s |
| Runtime start / stop | AT-06 benchmark | 1.96 ms / 0.97 ms |
| Asyncio tasks before/after | AT-06 benchmark | 1 / 1 |
| Traced memory current/peak | AT-06 benchmark | 987,179 / 1,072,385 bytes |
| Live API working set | process on port 8011 after restart | 165,797,888 bytes (~158.1 MiB) |

The AT-06 sustained test produced exactly one sell order and one exit decision across 60 cycles, with no task growth. This is a deterministic duplicate-suppression load, not a 24-hour soak. The live market was closed, so no live cycle-duration measurement was possible; the 29.02 ms figure comes from the accepted deterministic harness.

---

## 13. Defect register

| ID | Severity | Status | Finding | Resolution / evidence |
|---|---|---|---|---|
| NM-R1-01 | **High** | **Fixed** | No application code renewed the 60-second autonomy lease; the runtime degraded during a closed market even though the operator had started it. | Added a service-owned heartbeat task, started/stopped with the runtime. New regression; live 75-second check remained `running/ok` with a renewed expiry. |
| NM-R1-02 | **High** | **Fixed** | Recovery while the caller held a live lease reported `disabled` but left the durable lease held, potentially blocking restart. | `AutonomyRuntime.recover()` now releases its own lease before store recovery. New regression; live recovery showed no lease and a successful restart. |
| NM-R1-03 | **Medium** | **Fixed** | After start/pause, the UI refreshed only the runtime header; downstream cards showed the old mode until manually refreshed. | `control()` refreshes status, monitoring, decisions, executions, exits, positions and reliability. Browser pause/resume verified. |
| NM-R1-04 | Low | **Open** | “Run decision pipeline” remains enabled while paused; the backend refuses with 409. | No financial or safety impact; the backend gate is authoritative. UI guard remains to be added. |
| NM-R1-05 | **Medium** | **Open** | The Autonomy page has no dedicated recovery status/action or unresolved-reconciliation list; only the aggregate reliability line is shown. | Recovery was verified through the API. Required UI recovery visibility remains incomplete. |
| NM-R1-06 | **Medium** (configuration) | **Open** | Ordinary API authentication is unset in this environment, so reads are open. | API bound to loopback. Must configure a token before any non-loopback exposure. |
| NM-R1-07 | Informational | Accepted limitation | AT-01 shell status reports `trading_implemented=false`, `orders_possible=false`, `release_gate=closed` and `exit_supervision_implemented=false`. | Documented in AT-06/final acceptance; pipeline status comes from AT-04/05/06. Do not treat these as complete-pipeline fields. |
| NM-R1-08 | Low | **Open / verification gap** | No live provider outage, rate-limit or failover test was performed; Alpha Vantage remains disabled and unverified. | Liveness and a real quote were verified; failure/recovery evidence remains deterministic only. |

No Critical defect was found. The two High findings were fixed and verified during the audit; they are recorded here rather than hidden.

---

## 14. Remaining limitations

1. **Live financial lifecycle not exercised.** The market was closed and no live paper position existed. The full lifecycle is proven by deterministic real-engine suites, not by a live-market UI trade.
2. **Live exit UI not exercised.** Stop-loss, target and trailing exits passed the AT-05/AT-06 suites separately, but the browser could not show an actual position or exit because there were none.
3. **No live provider failure/failover test.** Yahoo and CoinGecko liveness and a real quote were verified; rate-limit, outage and recovery behaviour were not induced live. Alpha Vantage is unconfigured.
4. **No 24-hour production soak.** The AT-06 60-cycle sustained test is deterministic, not a long-running market soak.
5. **No UI autonomous start or recovery controls.** Autonomous mode was entered through the operator API; recovery was called through the API. The page exposes monitoring start/pause/resume/stop and exit configuration.
6. **No dedicated UI recovery/unresolved view.** The page renders aggregate pipeline health but not the AT-06 recovery result or an unresolved-record list.
7. **Ordinary API authentication is unset in this environment.** The server is loopback-only; this must be corrected before any wider exposure.
8. **Long-only target/trailing.** Short-position target/trailing remains unsupported by accepted scope.
9. **Kill switch scope.** Stop does not cancel already-submitted NM-10 orders; they remain authoritative and are reconciled after explicit recovery.
10. **Accepted exclusions remain excluded.** Forced end-of-day liquidation, target-weight rebalancing, automatic learning promotion, live broker integration and AT-07 were not added.
11. **Paper execution is simulated.** NM-10 paper fills and the simulated counterparty are not exchange-grade or tick-level execution.
12. **The 64-test API/workbench regression was not rerun after the final runtime-only recovery change.** The final autonomy/AT-06 group was rerun and passed; the changed behaviour is covered there.

---

## 15. Release-readiness verdict

# **PARTIAL — not released**

All of the following are true:

- prerequisites are present and accepted;
- the authority architecture is preserved;
- the application starts, serves the UI and enforces operator authorization;
- all four runtime modes and the stop/recovery lifecycle were exercised;
- the two High integration defects found by this audit were fixed and verified;
- the deterministic financial lifecycle and all three exit mechanisms pass;
- real provider liveness and a real quote were verified;
- type checks, API documentation and the frontend build pass.

Nevertheless, the release is **not PASS** because required evidence is still missing:

1. a live-market end-to-end paper trade through discovery, NM-08, NM-10, fill, position, ledger and reconciliation;
2. a live browser-verified stop-loss, target and trailing exit with authoritative accounting;
3. live provider failure, rate-limit and recovery behaviour;
4. UI recovery status/unresolved-reconciliation visibility;
5. a decision on whether autonomous-mode start belongs in the UI;
6. ordinary API authentication for any deployment beyond loopback.

The two High defects were remediated, but release readiness cannot be asserted while items 1–3 remain unverified. A market-open verification run and a provider-failure drill should be completed before changing this verdict to PASS.

**No commit, staging, push or tag was performed.**

---

## 16. NM-R1 Remediation Closure — 2026-11-13

**Closure date:** 2026-11-13  
**Scope:** actionable findings from the NM-R1 audit and the subsequent full-application QA  
**Software remediation verdict:** **REMEDIATION COMPLETE**  
**Operational release verdict:** **PARTIAL — live-session evidence remains pending**

This section is additive. The original NM-R1 verdict in §15 remains the historical verdict for the 2026-11-11 audit. It has not been rewritten as an original PASS.

### 16.1 Actionable findings closed

| ID | Prior status | Closure disposition | Evidence |
| --- | --- | --- | --- |
| NM-R1-04 | Open — paused decision action appeared enabled | **FIXED** | The Autonomy page now derives a paused/disabled/lease blocked reason, disables `Run decision pipeline`, and displays the reason with `role=status`. Server-side 409 enforcement is unchanged. |
| NM-R1-05 | Open — no dedicated recovery view/action | **FIXED** | The page now renders authoritative runtime, scheduler, lease, pipeline, financial, recovery, failure-domain, error and unresolved-record fields from `GET /market/autonomy/reliability`, plus an operator-authorized `POST /market/autonomy/recover` action with confirmation. |
| NM-R1-06 | Open — QA tokens unset | **FIXED (configuration)** | The isolated API now has distinct ordinary and autonomy-operator tokens read from ignored local files. No-token reads return 401; missing/wrong operator tokens return 403; the correct operator token returns 200. |
| NM-R1-08 | Open — no live provider outage/failover drill | **PARTIAL — deterministic coverage complete** | Timeout, connection failure, HTTP 429/Retry-After, invalid response, stale data, circuit, fallback and both-providers-failed paths now have deterministic tests. A live provider outage was not induced. |
| OBS-QA-ZOOM | Open P3 observation | **ACCEPTED — not a confirmed defect** | The 200% CSS-zoom simulation was not reproduced with real browser zoom; no CSS was changed. |

### 16.2 Prior NM-R1 fixes preserved

- NM-R1-01 lease heartbeat: `test_running_lease_is_renewed_without_a_market_cycle` passed.
- NM-R1-02 recovery lease release: `test_recovery_with_a_live_lease_releases_it_and_allows_restart` passed.
- NM-R1-03 stale Autonomy UI: the browser probe observed the decision and reliability cards update after start/pause and after recovery; the control action still refreshes the full surface.

The earlier 429 copy, API-reference synchronization and 400 ms mobile-probe fixes remain in place.

### 16.3 Verification evidence

| Check | Result |
| --- | --- |
| Autonomy frontend contract tests | **17 passed** |
| Provider adapter/fallback/freshness tests | **21 passed** |
| Consolidated autonomy, AT-04, AT-05, AT-06, UI, market-data and security regression | **176 passed, 2 skipped** in 80.01 s |
| Skipped tests | Two live-provider tests skipped because `NOVAMARKET_LIVE_TESTS=1` was not set; not forced |
| Frontend production build | **PASS** — 1,599 modules; JS 610.37 kB / gzip 162.51 kB |
| Python type check | **PASS** — 573 source files |
| API reference sync | **PASS** — 6 tests, 630 subtests |
| Browser behavior | **PASS** — 19 named checks, recovery HTTP 200, zero console errors |
| Git whitespace check | **PASS** — `git diff --check` clean |

### 16.4 Remaining operational evidence

The software remediation is complete, but the following are still not claimed as operational PASS:

1. A genuine open-session paper trade through discovery, NM-08, NM-10, fill, position, protective exit, ledger and reconciliation. The market was closed and the isolated book had zero orders, fills and positions.
2. Continuous autonomous operation. The runtime correctly sits `disabled/stopped` at rest; no sustained live cycle was observed.
3. A live provider outage, rate-limit or failover response. Deterministic failure injection is complete; the external-service event was not induced.
4. A fresh 470-control replay, current market pagination proof and exhaustive chart/table value cross-check. These remain partial evidence, not regressions.

Accepted limitations remain: long-only target/trailing exits, no cancellation of already-submitted orders by the kill switch, no forced EOD liquidation/rebalancing, no automatic learning promotion, no live broker and no AT-07.

### 16.5 Closure verdict

**NM-R1 software remediation: REMEDIATION COMPLETE.**  
**Operational release acceptance: PARTIAL.**

No P0/P1, financial-correctness, security or authorization defect remains open. The accepted NM-01–NM-14 and AT-01–AT-06 authority boundaries are intact. The release audit's original PARTIAL verdict is preserved as historical evidence for the 2026-11-11 environment.

No commit, staging, push or tag was performed during this closure.
