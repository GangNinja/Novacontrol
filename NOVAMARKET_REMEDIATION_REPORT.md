# NovaMarket NM-R1 — Remediation Report

**Date:** 2026-11-13  
**Scope:** closure of the NM-R1 actionable findings and verification of the accepted NM-01–NM-14 / AT-01–AT-06 baseline  
**Branch:** `novamarket/nm-01`  
**HEAD:** `afa5ff64c7673553b333fd3cff07cf5dc7c832b7`  
**Software remediation verdict:** **REMEDIATION COMPLETE**  
**Operational release verdict:** **PARTIAL — live-session evidence remains pending**

No commit, staging, push or tag was performed during this remediation.

---

## 1. Work completed

The NM-R1 findings register was consolidated and the three actionable items were closed:

1. **NM-R1-04** — paused/disabled decision action now has an explicit UI gate and explanation.
2. **NM-R1-05** — the Autonomy page now exposes the existing AT-06 reliability report, recovery state, unresolved records and an authorized recovery action.
3. **NM-R1-06** — the isolated QA API now runs with distinct ordinary and autonomy-operator tokens; missing and incorrect operator credentials still fail closed with 403.

The provider verification gap was also closed as far as the environment permits. Existing timeout, connection, rate-limit, invalid-response, stale-data, circuit, fallback and both-providers-failed paths now have deterministic coverage. A live provider outage was not induced.

No backend risk, execution, portfolio, ledger, exit, strategy or accounting behavior was changed.

---

## 2. Fix 1 — NM-R1-04 paused decision affordance

### Original defect

`Run decision pipeline` was disabled only by operator permission and `decision.lease_valid`. In paused mode the button was enabled, the page offered no explanation, and only the backend refused the run with 409.

### Root cause

The React page had a partial copy of the backend mode gate. The runtime mode was displayed elsewhere, but the decision action did not incorporate paused/disabled mode into its disabled state or its copy.

### Change

**File:** `web/src/pages/Autonomy.tsx`

- Added one derived `decisionBlockedReason`:
  - paused → explain that new decision runs are blocked and that resume is required; protective AT-05 exits and reconciliation remain active;
  - disabled → explain that monitoring must be started;
  - invalid lease → explain that the backend would refuse the run and status/ownership must be checked.
- Changed the button condition to:
  `disabled={!controlsEnabled || Boolean(decisionBlockedReason)}`
- Added a visible `role="status"` paragraph with `Decision run unavailable.` and the specific reason.
- Kept the server-side enforcement unchanged.

### Verification

- Browser: started monitoring with the operator token, paused, confirmed `Run decision pipeline` was disabled, confirmed both explanation strings were visible, then recovered to disabled.
- Contract tests: five new source-level regression tests in `tests/test_novamarket_workbench_ui.py` pin the paused/disabled copy, the disabled condition, the existing endpoints, loading/empty/error states and the operator-only recovery flow.
- Backend behavior was not altered; the 409 refusal remains authoritative.

---

## 3. Fix 2 — NM-R1-05 recovery and reliability visibility

### Original defect

The Autonomy page had only a compact “Paper pipeline: healthy/needs attention” line. There was no dedicated recovery summary, no unresolved-reconciliation list, no pending-reconciliation visibility and no recovery action in the page.

### Root cause

The backend already exposed `GET /market/autonomy/reliability` and `POST /market/autonomy/recover`, and API tests covered them. The UI had no presentation layer for the report beyond a single health string.

### Change

**File:** `web/src/pages/Autonomy.tsx`

Added a **Pipeline reliability and recovery** card consuming the existing report:

- authoritative runtime state and desired mode;
- scheduler state and scheduler health;
- lease state, expiry, last heartbeat and recovery-required flag;
- pipeline counts: discovery, evaluations, proposals, NM-08 assessments, paper orders and fills;
- financial state: supervised positions, pending orders, filled/partial orders, open exit decisions, pending reconciliation and learning incomplete;
- recovery state: last action/time, recovered/reconciled counts, auto-resume flag and preserved pending orders;
- failure-domain classification and last failure domain;
- health reasons and last runtime error;
- unresolved reconciliation records, with an explicit empty state;
- loading, stale-report warning and terminal error states.

Added a **Recover and disable** action using the existing `POST /market/autonomy/recover` endpoint:

- requires the dedicated operator token;
- opens a confirmation dialog that states recovery is reconcile-only and never resumes;
- displays recovered, reconciled and unresolved counts after completion;
- refreshes status, monitoring, decisions, executions, exits, positions, learning, configurations and reliability.

No second recovery service, endpoint, scheduler or orchestrator was created.

### Verification

Browser probe against the isolated UI/API:

- panel visible and populated from the API;
- pipeline health, scheduler health, lease, unresolved list, empty unresolved state and recovery action visible;
- recovery with operator token returned HTTP 200 and returned runtime to `disabled`;
- recovery response recorded `auto_resumed=false`, `paper_only=true`, `live_trading=false`, `broker_connected=false`;
- after clearing the operator token, the decision and recovery actions disabled while ordinary API authentication could still read the panel;
- zero browser console errors.

The empty-unresolved browser state was observed. The loading and error branches are pinned by the frontend contract tests.

---

## 4. Fix 3 — NM-R1-06 operator-token configuration

### Original state

The isolated QA API on `127.0.0.1:8012` had no ordinary API token and no autonomy-operator token. Reads were unauthenticated on loopback, and operator controls were correctly locked.

### Change

- Generated independent URL-safe random token values in ignored QA files:
  - `tmp/full_qa/api_token.txt`
  - `tmp/full_qa/operator_token.txt`
- Updated the ignored QA launcher `tmp/full_qa/start_qa_api.py` to read those files into:
  - `NOVACONTROL_API_TOKEN`
  - `NOVACONTROL_AUTONOMY_OPERATOR_TOKEN`
- Did not print, report, commit or place either value in frontend source. The files are under the already-ignored `tmp/` tree.
- Documented the operator variable and secure setup in `docs/CONFIGURATION.md`.

### Verification

| Request | Result |
| --- | --- |
| `GET /market/autonomy/reliability` with no bearer token | **401** |
| Same request with ordinary bearer token | **200** |
| `POST /market/autonomy/recover` with ordinary token only | **403** — `autonomy_operator_forbidden` |
| Same request with wrong operator token | **403** — `autonomy_operator_forbidden` |
| Same request with correct distinct operator token | **200** — `status=completed`, `mode=disabled`, `auto_resumed=false` |
| UI with operator token | controls enabled; recovery succeeded |
| UI after operator token cleared | controls locked; read-only panel still available |

The server-side fail-closed behavior was not weakened. The ordinary token and operator token are distinct. The API remains bound to `127.0.0.1`.

---

## 5. Provider reliability verification

### Existing architecture

The market-data provider layer already implements:

- HTTPS-only bounded transport with response-size ceiling;
- connection/timeout classification;
- bounded retries with backoff and `Retry-After`;
- HTTP 429 classification as rate limited;
- `401/403`, `404`, `5xx` and invalid-JSON classification;
- per-provider rate limiting and daily quota accounting;
- circuit breaker with closed/open/half-open states;
- provider fallback governed by the registry order;
- no-averaging of provider disagreement;
- stale/missing/structural quality checks before use;
- explicit unavailable result when every provider fails.

### Deterministic tests

`tests/test_novamarket_marketdata.py` was extended with two tests:

- `test_a_connection_failure_is_retried_then_reported`
- `test_http_429_honors_retry_after_and_fails_closed_when_exhausted`

These join the existing tests for:

- timeout retry and final typed failure;
- bounded retry and recovery from 503;
- unknown symbol vs schema change;
- missing OHLCV rows skipped without fabrication;
- stale observation stored as stale;
- circuit opening and one half-open probe;
- primary failure followed by secondary success;
- both providers failing with no fabricated record;
- provider disagreement visible and not averaged.

**Result:** `ProviderAdapterTests`, `FallbackAndCircuitTests`, `QualityAndFreshnessTests` — **21 passed**.

### What is not claimed

No live provider outage, rate-limit response or failover was induced. Yahoo/CoinGecko live liveness and a real quote were verified in the earlier NM-R1 audit. The deterministic suite proves the application’s failure behavior, not the providers’ current availability. That live drill remains an environmental verification gap.

---

## 6. Preservation of prior NM-R1 fixes

The three earlier NM-R1 fixes remain present and are covered by the consolidated run:

- **NM-R1-01** — service-owned lease heartbeat; `test_running_lease_is_renewed_without_a_market_cycle` passed.
- **NM-R1-02** — recovery releases the caller’s live lease; `test_recovery_with_a_live_lease_releases_it_and_allows_restart` passed.
- **NM-R1-03** — successful control actions refresh the whole autonomy surface; the browser probe observed the decision and reliability cards change after start/pause, and the recovery action refreshed the same surface.

The previous 429 copy, API-reference sync and 400 ms accessibility-probe fixes were not reverted. No product code for them was changed in this closure.

---

## 7. Changed files

| File | Change |
| --- | --- |
| `web/src/pages/Autonomy.tsx` | Paused/disabled decision gate and explanation; reliability/recovery panel; authorized recovery action and confirmation. |
| `tests/test_novamarket_workbench_ui.py` | Five Autonomy frontend contract tests. |
| `tests/test_novamarket_marketdata.py` | Deterministic connection-failure and HTTP 429/Retry-After tests. |
| `docs/CONFIGURATION.md` | Secure operator-token variable and setup documentation. |
| `NOVAMARKET_FINDINGS_CONSOLIDATION.md` | New consolidated findings register. |
| `NOVAMARKET_REMEDIATION_REPORT.md` | This report. |
| `NOVAMARKET_RELEASE_AUDIT_REPORT.md` | Dated remediation-closure section appended; original verdict preserved. |
| `docs/BUG_FIX_REGISTER.md` | Actual status of every finding updated. |
| `docs/QA_COMPLETION_REPORT.md` | Closure addendum and revised QA status. |
| `tmp/full_qa/start_qa_api.py` | Ignored QA launcher reads ignored token files. |
| `tmp/full_qa/nm_r1_ui_probe.py` | Ignored browser verification probe; not product code. |

No risk, execution, portfolio, ledger, wallet, accounting, strategy, scheduler, lease or recovery implementation was modified.

---

## 8. Verification results

| Check | Command / scope | Result |
| --- | --- | --- |
| Frontend production build | `npm run build` | **PASS** — 1,599 modules; JS 610.37 kB / gzip 162.51 kB |
| Autonomy UI contract | `pytest tests/test_novamarket_workbench_ui.py -q` | **17 passed** |
| Provider failure/freshness | `pytest ... ProviderAdapterTests FallbackAndCircuitTests QualityAndFreshnessTests -q` | **21 passed** |
| Consolidated regression | `pytest test_novamarket_workbench_ui.py test_novamarket_autonomy.py test_novamarket_at04.py test_novamarket_at05.py test_novamarket_at06.py test_novamarket_marketdata.py test_security.py -q -rs` | **176 passed, 2 skipped** in 80.01 s |
| Skipped tests | Two live-provider tests | Skipped because `NOVAMARKET_LIVE_TESTS=1` was not set; not forced |
| Python type check | `mypy src` | **PASS** — 573 source files |
| API reference sync | `pytest tests/test_api_reference_sync.py -q` | **6 passed, 630 subtests** |
| Whitespace/diff check | `git diff --check` | **Clean** |
| Browser behavior | Playwright, isolated UI/API | **PASS** — 19 named checks true, 0 console errors, recovery HTTP 200 |
| Authorization | curl, tokenized isolated API | **401/403/200 as expected** |

The consolidated run includes the autonomy runtime/auth tests, AT-04 paper execution, AT-05 exits, AT-06 reliability/lifecycle, market-data adapters/fallback/freshness and the security suite. The two skips are the live-provider tests, not failures.

---

## 9. Remaining limitations

### Environment-dependent, not actionable software defects

1. **Live paper lifecycle** — market closed and zero paper orders/fills/positions. The deterministic lifecycle proves the engine chain; no live UI trade was forced.
2. **Continuous autonomy** — runtime is correctly `disabled/stopped` at rest; no sustained live cycle was observed.
3. **Live provider outage/failover** — deterministic failure injection is complete; no external provider outage was induced.
4. **Full 470-control replay** — the historical matrix is preserved; this closure did not re-operate every historical control.
5. **Market pagination and exhaustive chart/table cross-checks** — current evidence is partial, not a regression claim.

### Accepted limitations

- Long-only target/trailing exits.
- Kill switch does not cancel already-submitted NM-10 orders.
- No forced EOD liquidation, target-weight rebalancing or automatic learning promotion.
- No live broker, real-money order, paid provider or AT-07.
- Paper fills are simulated, not exchange-grade.

### Minor observations

- The 200% CSS-zoom simulation was not reproduced with real browser zoom and is not a confirmed product defect.
- The market table’s roughly two-second settle after a search is a UX latency observation with a loading state, not data loss.

---

## 10. Final verdict

# **REMEDIATION COMPLETE**

All actionable NM-R1 findings are closed. Every fix has focused automated and browser verification. The accepted NM-01–NM-14 / AT-01–AT-06 architecture and safety boundaries remain intact. No P0/P1, financial-correctness, security or authorization defect remains open.

This is not a full operational release acceptance. A genuine open-session paper-trading demonstration, continuous autonomous operation and a live provider failure drill remain explicitly pending environmental evidence.

No commit, staging, push or tag was performed.
