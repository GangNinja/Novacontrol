# NovaMarket NM-R1 — Consolidated Findings Register

**Date:** 2026-11-13  
**Baseline:** NM-01–NM-14 and AT-01–AT-06 accepted  
**Branch:** `novamarket/nm-01`  
**HEAD:** `afa5ff64c7673553b333fd3cff07cf5dc7c832b7`  
**Purpose:** one canonical register of every finding in the current NM-R1 and full-application QA reports, with duplicates removed, false positives identified, and disposition recorded.

No application audit was repeated. This register consolidates the existing evidence and records the remediation completed on 2026-11-13.

---

## 1. Sources inspected

| Abbreviation | Report |
| --- | --- |
| RAR | `NOVAMARKET_RELEASE_AUDIT_REPORT.md` |
| FAQ | `docs/FULL_APPLICATION_QA_REPORT.md` |
| RTR | `docs/REQUIREMENTS_TRACEABILITY_REPORT.md` |
| UIM | `docs/COMPLETE_UI_INTERACTION_MATRIX.md` |
| BFR | `docs/BUG_FIX_REGISTER.md` |
| APA | `docs/AUTONOMOUS_PAPER_TRADING_AUDIT.md` |
| QCR | `docs/QA_COMPLETION_REPORT.md` |
| AT05C | `docs/AT05_PREREQUISITE_CLOSURE.md` |
| ATF | `AUTONOMOUS_TRADING_FINAL_ACCEPTANCE_REPORT.md` |
| AT01–AT06 | `AUTONOMOUS_TRADING_AT03_REPORT.md` through `AUTONOMOUS_TRADING_AT06_REPORT.md`, plus `docs/AT-01_IMPLEMENTATION_REPORT.md` and `docs/AUTONOMOUS_TRADING_AT02_REPORT.md` |
| ARCH | `docs/archive/qa_2026-11-09/` (historical context only) |

The archived 9–10 November reports were used only to understand prior fixes and preserve historical evidence. They were not treated as current findings.

---

## 2. Method and deduplication

- Every finding was assigned one canonical ID.
- Repeated mentions across RAR, FAQ, UIM, BFR, APA and QCR were merged into the canonical row rather than counted again.
- A live-market observation was not classified as a software defect.
- A test-harness artifact was not classified as a product defect.
- A previously fixed issue was not reopened without reproduction.
- Accepted architecture limits were kept separate from actionable defects.
- The three actionable NM-R1 items were rechecked against the current source and runtime before remediation.

**Duplicate findings after normalization: 0.** Twenty-one findings had repeated mentions across two to five reports; those mentions are represented once in the master table.

---

## 3. Counts

| Metric | Count |
| --- | ---: |
| Unique findings | 22 |
| Duplicate canonical findings | 0 |
| Already resolved findings | 13 |
| Confirmed actionable findings at closure start | 3 |
| Actionable findings still open after closure | 0 |
| Environmental or evidence limitations | 6 |
| Accepted limitations/observations | 3 |
| False positives | 1 |
| Open P0/P1 defects | 0 |
| Open financial-correctness defects | 0 |
| Open security/authorization defects | 0 |

The 13 resolved findings comprise 10 findings verified before this closure (three earlier read-model/chart fixes, 429, route test, API documentation, the harness false positive, and NM-R1-01/02/03), plus the 3 NM-R1 items closed on 2026-11-13.

---

## 4. Master findings register

| ID | Source(s) | Original finding | Area | Severity | Primary category | Reproducible? | Duplicate? | External dependency? | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BUG-QA-001 | FAQ, BFR, ARCH | Horizon status used only the first book | Dashboard read model | P2 | Software defect | Was | No | No | **FIXED (prior)** |
| BUG-QA-002 | FAQ, BFR, ARCH | Market pulse showed duplicate/unquoted rows | Dashboard read model | P2 | Software defect | Was | No | No | **FIXED (prior)** |
| BUG-QA-003 | FAQ, BFR, ARCH | Flat valuation series produced identical Y-axis ticks | Chart presentation | P3 | Software defect | Was | No | No | **FIXED (prior)** |
| BUG-QA-429 | FAQ, BFR, QCR | HTTP 429 rendered as “Backend offline” | Global/Dashboard error copy | P2 | Software defect | Yes | No | No | **FIXED** |
| TEST-QA-ROUTE | FAQ, BFR, QCR | Route test expected 221 decorators; actual 258 | Test contract | P3 | Test defect | Yes | No | No | **FIXED** |
| DOC-QA-API | FAQ, BFR, QCR | `docs/API.md` out of sync with 630-route registry | Documentation contract | P3 | Documentation defect | Yes | No | No | **FIXED** |
| QA-HARNESS-MOBILE | FAQ, BFR, UIM, QCR | 150 ms mobile probe raced the 220 ms sidebar transition | Test harness | P3 | False positive / harness defect | Yes | No | No | **FIXED — false positive** |
| NM-R1-01 | RAR, BFR | Lease heartbeat never started; runtime degraded after 60 s | Autonomy runtime | P1 (prior) | Software defect | Was | No | No | **FIXED (prior)** |
| NM-R1-02 | RAR, BFR | Recovery left the caller's live lease held | Autonomy recovery | P1 (prior) | Software defect | Was | No | No | **FIXED (prior)** |
| NM-R1-03 | RAR, BFR | Autonomy UI stale after start/pause | UI integration | P2 (prior) | Software defect | Was | No | No | **FIXED (prior)** |
| NM-R1-04 | RAR, FAQ, BFR, UIM, QCR | `Run decision pipeline` appeared enabled while paused | Autonomy UI | P3 | Incomplete functionality | Yes | No | No | **FIXED — 2026-11-13** |
| NM-R1-05 | RAR, FAQ, BFR, UIM, QCR | No dedicated recovery summary/action or unresolved list | Autonomy UI | P2 | Incomplete functionality | Yes | No | No | **FIXED — 2026-11-13** |
| NM-R1-06 | RAR, FAQ, BFR, QCR | Ordinary API token and operator token unset in the isolated environment | Configuration / auth | P2 (config) | Security/auth configuration | Yes | No | No | **FIXED — configured 2026-11-13** |
| NM-R1-07 | RAR, BFR, APA | AT-01 shell fields report release-gate values | Documentation/contract semantics | Info | Accepted limitation | By design | No | No | **ACCEPTED** |
| NM-R1-08 | RAR, FAQ, BFR, QCR, APA | No live provider outage/rate-limit/failover drill | Market-data verification | P3 | Missing evidence | N/A | No | Yes — provider availability | **PARTIAL — deterministic coverage complete; live drill not induced** |
| OBS-QA-ZOOM | FAQ, BFR, UIM, QCR | 200% CSS-zoom simulation showed overflow on 3 routes | Responsive observation | P3 | Accepted observation | Not in real zoom | No | No | **ACCEPTED — not reproduced; not a confirmed defect** |
| OBS-QA-MARKET-LATENCY | FAQ | Market table takes about two seconds to settle after a query | UX observation | Info | Accepted observation | Yes | No | No | **ACCEPTED — loading state present; no data loss** |
| QA-COVERAGE | FAQ, BFR, UIM, QCR | Not all 470 historical controls were re-operated | Verification scope | Process | Missing evidence | N/A | No | No | **PARTIAL — historical matrix preserved** |
| QA-PAGINATION | FAQ, UIM | Market next-page behavior not re-proven | Verification scope | Process | Missing evidence | N/A | No | No | **PARTIAL** |
| QA-VALUE-CROSSCHECK | FAQ, UIM | Not every chart point/table row was re-diffed to its API response | Verification scope | Process | Missing evidence | N/A | No | No | **PARTIAL** |
| QA-LIVE-LIFECYCLE | FAQ, BFR, QCR, APA | No current paper order → fill → position → exit → ledger chain | Live-market evidence | External state | Environmental limitation | N/A | No | Yes — session and positions | **BLOCKED — market closed; 0 records** |
| QA-AUTONOMY-LIVE | FAQ, BFR, QCR, APA | No continuous autonomous cycle observed | Runtime evidence | External state | Environmental limitation | N/A | No | Yes — runtime intentionally disabled at rest | **NOT DEMONSTRATED** |

---

## 5. Category summary

### 5.1 Confirmed software defects

Nine product-level findings were confirmed and fixed across the baseline and the two QA passes:

- BUG-QA-001, BUG-QA-002, BUG-QA-003 — read-model/chart correctness.
- BUG-QA-429 — incorrect 429/offline error copy.
- NM-R1-01 — missing lease heartbeat.
- NM-R1-02 — recovery retained a live lease.
- NM-R1-03 — stale Autonomy cards after control actions.
- NM-R1-04 — paused-mode decision affordance.
- NM-R1-05 — missing recovery visibility/action.

No confirmed financial, risk, ledger, portfolio, execution or broker-boundary defect remains open.

### 5.2 Incomplete functionality

NM-R1-04 and NM-R1-05 were the two incomplete UI behaviors. Both are closed. The backend gates and recovery service were already correct and were not replaced.

### 5.3 Frontend/backend integration gaps

Only the UI presentation gap for existing `GET /market/autonomy/reliability` and `POST /market/autonomy/recover` was actionable. The UI now consumes those existing contracts; no new endpoint or service was created.

### 5.4 Security and authorization issues

No code-level authorization defect was found. NM-R1-06 was an environment configuration gap: the isolated QA API had no ordinary bearer token and no dedicated operator token. Both are now configured for the isolated environment, and 401/403 behavior is verified. The server already failed closed when the operator token was missing or wrong.

### 5.5 Financial correctness issues

No open financial-correctness finding. The accepted NM-08 risk authority, NM-10 paper-execution authority, portfolio/ledger/accounting services and AT-05 exit paths were preserved. The remediation did not touch risk policy, fees, slippage, allocation, ledger rules or paper-only flags.

### 5.6 Missing verification evidence

- QA-COVERAGE — full 470-control replay was not repeated.
- QA-PAGINATION — current market pagination not re-proven.
- QA-VALUE-CROSSCHECK — not every chart point/table row was re-diffed.
- NM-R1-08 — deterministic provider failure paths are covered; a live provider outage was not induced.
- QA-LIVE-LIFECYCLE — external market/position state.
- QA-AUTONOMY-LIVE — continuous runtime state.

These are evidence gaps, not evidence of a code failure.

### 5.7 External/environmental limitations

- NSE session was closed during the live QA.
- The isolated store had zero paper orders, fills and open positions.
- The autonomy runtime correctly sits `disabled/stopped` at rest.
- A live provider outage cannot be created without affecting a real external service; deterministic injection is the valid substitute.

### 5.8 False positives

- QA-HARNESS-MOBILE — the reported overflow was a 150 ms measurement during the 220 ms sidebar transition. At 400 ms both routes measured 320 px with no overflowing visible element. No product CSS was changed for this finding.

### 5.9 Already fixed findings

BUG-QA-001/002/003, BUG-QA-429, TEST-QA-ROUTE, DOC-QA-API and QA-HARNESS-MOBILE were verified before this closure. NM-R1-01/02/03 were verified again through their focused regression tests. They were not rewritten.

### 5.10 Accepted architectural limitations

These are not defects and were not changed:

- long-only target/trailing exits;
- kill switch stops new work but does not cancel already-submitted NM-10 orders;
- no forced end-of-day liquidation or target-weight rebalancing;
- no automatic learning promotion;
- no live broker or live trading;
- AT-07 not authorized.

NM-R1-07 (AT-01 shell fields) is an accepted contract/documentation limitation.

---

## 6. Root-cause clusters

| Cluster | Findings | Root cause | Resolution |
| --- | --- | --- | --- |
| UI duplicated only part of the server gate | NM-R1-04 | The button checked operator permission and lease validity but not the paused/disabled mode now displayed by the backend. | UI now derives one explicit blocked reason, disables the action and shows it. Server enforcement unchanged. |
| Existing reliability API lacked a presentation layer | NM-R1-05 | The page consumed the report only for one aggregate health line. | Added an authoritative panel with runtime, scheduler, lease, pipeline, financial, recovery, unresolved records, failure domains and errors. |
| QA environment did not supply secrets | NM-R1-06 | The start helper did not read token files. | Helper reads ignored token files; both tokens configured; 401/403/200 verified. |
| Provider failure paths were deterministic but not live-induced | NM-R1-08 | No safe way to cause a real outage without affecting an external service. | Timeout, connection failure, 429/Retry-After, invalid response, stale data, circuit, fallback and both-failed paths are deterministic tests. Live outage remains an evidence gap. |
| Closed market and no positions | QA-LIVE-LIFECYCLE | External state. | Not forced; full deterministic lifecycle remains the accepted evidence. |
| Runtime intentionally disabled at rest | QA-AUTONOMY-LIVE | Required fail-closed startup semantics. | Not changed; continuous operation remains undemonstrated. |
| Test/doc drift | TEST-QA-ROUTE, DOC-QA-API, QA-HARNESS-MOBILE | Stale counts, missing regeneration, measurement race. | Fixed before closure and preserved. |

---

## 7. Requirements reconciliation

| Scope | Result | Evidence |
| --- | --- | --- |
| NM-01–NM-14 | **Accepted baseline intact** | Current consolidated regression; no financial/risk/ledger changes |
| AT-01–AT-06 | **Accepted baseline intact** | Autonomy, AT-04, AT-05 and AT-06 suites pass |
| NM-R1 actionable items | **3/3 closed** | NM-R1-04/05 fixed; NM-R1-06 configured and verified |
| NM-R1 prior fixes | **Preserved** | Lease heartbeat, recovery lease release and UI refresh tests pass |
| Live paper lifecycle | **Environmentally blocked** | Closed market; zero orders/fills/positions |
| Continuous autonomy | **Not demonstrated** | Runtime disabled/stopped at rest |
| Live provider failure drill | **Not performed** | Deterministic failure/failover tests pass |
| New requirements invented | **None** | No AT-07, no broker, no new engine |

**Final finding disposition: 0 actionable defects open; 13 resolved; 6 evidence/environment limitations; 3 accepted limitations/observations.**

---

## 8. Register verdict

**Software remediation verdict: REMEDIATION COMPLETE.**  
**Operational release acceptance: PARTIAL — pending a verified open-session paper-trading run, continuous autonomy evidence and a live provider failure drill.**

No commit, staging, push or tag was performed.
