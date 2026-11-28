# NovaMarket — Security Remediation and Operational Closure Report

**Date:** 23 November 2026  
**Scope:** the local NovaControl/NovaMarket API and its operational evidence, following the findings in [FINAL_SYSTEM_WIDE_READINESS_ASSESSMENT.md](FINAL_SYSTEM_WIDE_READINESS_ASSESSMENT.md) and [OPERATIONAL_RELIABILITY_SECURITY_AND_RESILIENCE_REPORT.md](OPERATIONAL_RELIABILITY_SECURITY_AND_RESILIENCE_REPORT.md). Those documents are retained as historical baselines; this report is current where they differ.

**Mode:** paper only. No PI-25 was created, no live trading was enabled, no broker was connected, no RL policy was activated or promoted, no financial data was deleted, and no commit or push was performed.

**Claim boundary:** this is remediation evidence, not a security certification or a production-readiness authorization. A PASS below means the named finding was corrected and checked on the tested host, not that the whole system is certified.

## 1. Executive result

The findings that were removable in code are closed:

- API authentication now fails closed; tokenless loopback operation requires an explicit `NOVACONTROL_ALLOW_ANONYMOUS_LOCAL=1` opt-in.
- Production-like and non-loopback configurations refuse to start without a 32+ character API token.
- The launcher no longer creates a predictable admin key; it generates a random per-installation value in the gitignored `.runtime/admin-token`.
- The API has one global 5xx boundary: generic client messages, redacted server logs and an `X-Request-ID`/body correlation id.
- SSE no longer accepts a query-string token. A short-lived, hashed, server-side session is exchanged at `POST /events/session` and carried in an HttpOnly, SameSite=Strict, `/events`-scoped cookie.
- `pip-audit` is installed as a development dependency. After upgrading vulnerable distributions, the final local scan reports no known vulnerabilities for the audited environment.
- Audit writes retry transient failures, use a unique temporary file for rewrites, and expose failures through `/audit/status` (`write_failures`, `last_write_error`, `degraded`) plus a CRITICAL log.
- The date-sensitive scheduler fixture now chooses a calendar-verified trading day instead of weakening the assertion.
- The full suite is green in ordered groups and in one process: **1,970 passed, 2 skipped, 0 failed**, 447 subtests, exit 0.
- A bounded 300-second soak passed with **406/406 HTTP 200s**, no failed requests, 20 SSE keep-alives and no CRITICAL/ERROR log lines.

**Overall ratings:** security-control remediation **PASS** for the identified local-API findings; audit durability, backup/retention and observability **PARTIAL**; sustained/mini-soak evidence **PARTIAL**; live-trading readiness **BLOCKED**. The remaining partial items are operational limitations, not evidence of an unauthorized trade, a credential leak or ledger corruption.

## 2. Finding-by-finding rating

| ID | Original finding | Rating | Current evidence / remaining boundary |
|---|---|---|---|
| S-1 | Authentication was off when no API token was configured; a local caller became principal `local`. | **PASS** | `ApiTokenAuthenticator` fails closed with no server token. `load_security_policy` allows anonymous mode only for an explicit loopback opt-in. Production-like/non-loopback startup exits 1 without a long token. |
| S-2 | `start.ps1` used a predictable admin key. | **PASS** | The retired literal is rejected by policy and by the launcher. `start.ps1` generates 32 random bytes (64 hex characters) once and stores them in `.runtime/admin-token`; Vite has no fallback secret. |
| S-3 | Several 500 handlers returned raw exception text. | **PASS** | One error boundary masks 5xx bodies (`Internal server error.`, stable `error` code, `request_id`), preserves 4xx validation detail, redacts internal logs and returns `X-Request-ID`. Tests cover unhandled exceptions, HTTP 500 and upstream 502. |
| S-4 | SSE accepted `?token=`. | **PASS** | `POST /events/session` mints an opaque session; only a SHA-256 digest is stored server-side; TTL is one hour and the table is capped. Cookie is HttpOnly, SameSite=Strict and Path=/events. `GET /events/stream` refuses missing, invalid and query-string credentials. |
| S-5 | No dependency-vulnerability scan. | **PASS** | Initial `pip-audit --local`: 61 raw findings / 52 unique across 4 packages. Pillow, certifi, pip and setuptools were upgraded; the final scan reports `No known vulnerabilities found`, exit 0. See the scan limitation in §8. |
| S-6 | Audit-log rename failed twice on the OneDrive workspace and the loss was not observable. | **PARTIAL** | Unique temporary names remove the cross-process collision, append/replace retry three times, and failures increment counters and log CRITICAL. A permanent failure still loses that record; there is no secondary sink or replication, so the OneDrive durability boundary is reduced but not eliminated. |
| S-7 | No scheduled backup, point-in-time recovery or replication for the single SQLite financial store. | **PARTIAL** | Backup/restore, corruption detection, lock contention, read-only, disk-full and uncommitted-WAL drills pass on disposable copies. No scheduler, retention job, PITR or replication was added; a disk/process loss remains a single point of failure. |
| S-8 | No unified CPU/RSS/queue-depth metrics; no correlation id across the request chain. | **PARTIAL** | Request ids, redacted error correlation, `/audit/status` counters and a measured soak exist. There is still no general CPU/RSS/queue-depth metrics endpoint, and no full market→forecast→risk→execution trace was demonstrated. |
| O-1 | Historical single-process suite exited 1 near the LSTM block with no failure summary. | **PARTIAL** | The current single-process run completed all 1,972 tests in 505.88 s with exit 0 and 819.3 MB peak sampled RSS. The historical native/resource crash did not recur; its original cause remains unproven. |
| O-2 | No true single-process green run. | **PASS** | The current single-process run is green: 1,970 passed, 2 skipped, 0 failed, exit 0. |
| O-3 | Date-sensitive scheduler test selected a market holiday. | **PASS** | The fixture now walks forward to a day accepted by `calendar_by_mic("XNSE").is_trading_day(...)`; the API's 422 holiday rejection is no longer bypassed or weakened. |
| O-4 | No multi-hour soak or sustained load test. | **PARTIAL** | A controlled five-minute, seven-endpoint soak is now recorded with memory, handle, latency, scheduler and audit evidence. This is not a multi-hour or concurrent-writer test. |
| O-5 | No live provider/broker or live paper lifecycle. | **BLOCKED** | Structurally absent by design. No broker, live data path, production deployment or real-money authorization exists; nothing here authorizes live trading. |
| O-6 | RL policies were degenerate and strategy effectiveness was not demonstrated. | **FAIL** | Unchanged and intentionally not addressed by this remediation. No policy was promoted and no performance claim is made. |
| O-7 | 0-byte adaptive SQLite placeholder. | **FAIL** | Still an unopened placeholder, not counted as financial evidence. It was not deleted or populated. |
| O-8 | Execution-journal and audit retention were undefined. | **PARTIAL** | Audit retention is configured and `/audit/status` reports it; the execution journal still has no retention/pruning policy. |
| O-9 | Single-node SQLite and one process. | **PARTIAL** | Unchanged architecture. The recovery drills prove fail-closed behavior on one node, not HA, replication or multi-writer throughput. |
| O-10 | 2027 exchange-calendar coverage is unverified. | **BLOCKED** | The scheduler fails closed outside calendar coverage. No 2027 holiday source was added. |

Previously fixed defects F-1 (nested portfolio transactions) and F-2 (missing positive reference price) remain covered by their regression tests and the green suite. No new PI or strategy feature was added.

## 3. Root causes and corrections

### 3.1 Authentication default

The primitive treated "no server token configured" as an authenticated local principal. That made a missing credential indistinguishable from a valid one. The correction separates the primitive from deployment policy:

- `ApiTokenAuthenticator` returns `authenticated=False` when its server token is empty.
- `load_security_policy` validates the environment before stores are opened.
- Production-like environments, non-loopback trusted hosts and configured CORS origins require a 32+ character API token.
- Tokenless loopback development is a named opt-in, not an inference.
- The API and autonomy-operator credentials must differ.

The consequence is deliberate: an operator who previously relied on tokenless localhost must now set `NOVACONTROL_ALLOW_ANONYMOUS_LOCAL=1` (or configure a token). The launcher continues to provide a local credential without a prompt.

### 3.2 Predictable launcher credential

The old literal was a shared secret committed in the launcher. `start.ps1` now generates a random value once per installation, persists it only under ignored `.runtime/admin-token`, and refuses to reuse the retired literal. `web/vite.config.ts` reads the value from the environment and has no code fallback.

### 3.3 Error leakage

Route handlers had no single owner for 5xx presentation. `install_error_handlers` now owns that boundary. Client mistakes keep their 4xx detail; every 5xx becomes a generic code/message pair. The internal log receives the redacted detail and trace, while the client receives the correlation id. This does not hide operational failures: the detail remains available server-side.

### 3.4 SSE credential exposure

`EventSource` cannot set an Authorization header, which is why the query-token exception existed. The query string is the wrong place for a bearer credential because it enters history, proxy logs and copied URLs. The new exchange is:

1. The page posts its bearer/admin credential to `POST /events/session`.
2. The server issues a random opaque cookie and stores only its digest, with a one-hour TTL and bounded table.
3. `GET /events/stream` accepts the cookie (or an Authorization header for non-browser clients) and rejects `?token=`.
4. The static UI and React dashboard re-exchange and reconnect after an SSE error; the session cookie is scoped to `/events` and never accepted by REST routes.
5. A separately hosted frontend must proxy `/events` through its own origin; cross-origin browser SSE is intentionally not enabled.

### 3.5 Dependency risk

The first scan was not a clean baseline. The vulnerable distributions were upgraded to Pillow `13.0.0`, certifi `2026.11.20`, pip `26.3` and setuptools `84.2.1`; the vision suite passed 289 tests after the Pillow upgrade. `pip-audit` is now declared in the development dependencies so future scans are reproducible.

### 3.6 Audit durability

The audit sink used a fixed `<name>.tmp` path, which collides when more than one writer or process rewrites the trail; OneDrive added sharing violations on top. The correction uses a unique temporary file in the same directory, retries append/replace three times, and reports a permanent failure without failing the business request. The trade-off is explicit: audit is still best-effort, but it is no longer silent.

### 3.7 Test fixture

The scheduler test chose "UTC tomorrow" and skipped weekends only. 24 November 2026 is a verified NSE holiday, so the API correctly returned 422. The fixture now asks the exchange calendar for the next trading day. No status assertion, route or product rule was weakened.

### 3.8 Single-process runner

The historical run ended with no pytest failure summary near the LSTM block, which is consistent with a process/native/resource failure rather than an assertion. The current run completed in one process without the crash. The cause of the historical failure was not established; the two-group runner remains the safer reproducible path, and both paths are now green.

## 4. Security verification

Focused final check:

```powershell
cd novamarket
$env:PYTHONPATH = "src"
$env:NOVACONTROL_ALLOW_ANONYMOUS_LOCAL = "1"
..\.venv\Scripts\python.exe -m pytest tests/test_security_remediation.py tests/test_novamarket_database_resilience.py -q -p no:cacheprovider
```

Result: **27 passed** in 6.80 s, exit 0 (`tmp/ops_audit/post_remediation_security_db_final.log`, SHA-256 `ce1bf1f1ff927ff0ddf3508eeed0805192fcdf878426852bb67b2890c7e4df02`).

The checks cover:

| Check | Evidence |
|---|---|
| Missing token fails closed | `test_authenticator_fails_closed_without_a_server_token` |
| Unicode credentials do not raise | `test_authenticator_handles_non_ascii_credentials_without_raising` |
| Tokenless local mode needs explicit opt-in | `test_tokenless_development_requires_an_explicit_opt_in` |
| Production/non-loopback requires a long token | `test_production_requires_a_long_api_token`, `test_a_non_loopback_host_or_cors_origin_requires_api_auth`, `test_application_creation_refuses_an_unsafe_production_config` |
| Predictable admin/reused operator key refused | `test_predictable_admin_key_and_reused_operator_key_are_refused` |
| Sensitive middleware refuses an unguarded route | `SensitivePathMiddlewareTests` |
| Anonymous mode never bypasses a non-loopback peer | `test_anonymous_local_never_bypasses_a_non_loopback_client` |
| 5xx body masked and correlated | `ApiErrorBoundaryTests` (500, 502, validation preservation) |
| SSE session/cookie/query refusal | `SseHttpTests`; cookie is HttpOnly, SameSite=strict, Path=/events; session body does not contain the API token |
| Audit failure visible/redacted/retried | `AuditFailureVisibilityTests` |
| SQLite lock/read-only/disk-full/WAL/restore | `tests/test_novamarket_database_resilience.py` |

Live uvicorn probe (`tmp/ops_audit/sse_live_probe.log`): missing `/events/stream` credential 401, query token 401, session 200, cookie flags observed, stream 200 `text/event-stream` with `: connected`. Production startup without a token was run separately and exited 1 with the policy error recorded in `tmp/ops_audit/production_startup_refusal.log`.

No test was skipped, removed, deselected or weakened by this remediation to obtain these results. The two pre-existing suite skips remain disclosed above: both are optional-Node renderer tests, neither is a security test, and neither was added by this remediation.

## 5. Database recovery

All drills used temporary or disposable stores; the real financial database was not mutated.

| Drill | Result |
|---|---|
| `BEGIN IMMEDIATE` write-lock contention | Fails closed, no partial mutation; recovers after release. |
| Read-only connection | Write is refused; integrity remains unchanged. |
| Disk-full simulation | Transaction fails rather than writing a partial row. |
| Uncommitted WAL transaction, reopen | Uncommitted state is not visible after reopen. |
| Backup → restore | Last consistent snapshot restores; integrity is preserved. |

Evidence: `tests/test_novamarket_database_resilience.py` (`SqliteRecoveryTests`), 5 passed in the 27-test focused run.

This does not add a backup schedule, retention job, replication or PITR. Those remain operational gaps (§7).

## 6. Logging, audit and soak

`/audit/status` now exposes `write_failures`, `last_write_error`, `last_write_failure_at` and `degraded`. A failed write is counted, redacted and logged at CRITICAL; the request path continues and the application logs `audit_record_failed` rather than hiding the failure.

The five-minute soak started the real app on `127.0.0.1:8766` with a temporary data directory, exercised seven read-only endpoints, held one SSE stream and sampled the process:

| Measurement | Result |
|---|---|
| Duration / cycles / requests | 300.3 s / 58 / 406 |
| HTTP statuses | 406 × 200; 0 failures |
| Latency | median 11.4 ms, p95 57.8 ms, max 319.5 ms |
| RSS | 391.9 MB baseline → 404.4 MB final; +12.5 MB; max 404.4 MB |
| Handles | 299 → 306; max 316; +7 |
| SSE | session 200, connected frame, 20 keep-alives, no errors |
| Scheduler | running, 31 ticks, 0 runs, 0 failures, empty last_error |
| Audit | 6 records, 0 write failures, not degraded |
| Logs | 932 lines, no CRITICAL/ERROR |

Evidence: `tmp/ops_audit/soak_evidence.json` (SHA-256 `40d86468009464ec238e2221ae61b6dbfabb03e5fee723eb46608312821f429d`), `soak_api.log`, `soak_console.log`.

The soak is a bounded single-process check. It does not exercise concurrent writers, multi-hour growth, provider outages or live market data.

## 7. Test-runner and full-suite evidence

The ordered runner executes Group A (21 files through `test_novamarket_lstm.py`), Group B (35 files) and then all 56 files in one process.

| Phase | Result | Exit | Wall time |
|---|---|---|---|
| Group A | 697 passed, 607 warnings, 390 subtests passed | 0 | 319.61 s |
| Group B | 1,273 passed, 2 skipped, 283 warnings, 57 subtests passed | 0 | 189.63 s |
| Single process | 1,970 passed, 2 skipped, 883 warnings, 447 subtests passed | 0 | 505.88 s |

The single-process run completed all 1,972 collected tests and did not reproduce the historical crash. Peak sampled pytest-child RSS was **819.3 MB** across 68 active samples; CPU time reached 424.1 s. This is not evidence of memory exhaustion, and no test was changed to avoid the prior block.

Evidence and hashes are in `tmp/ops_audit/post_remediation_novamarket_suite_record.json` and `docs/POST_REMEDIATION_TEST_SUITE_RECORD.md`. The historical record remains intact at `tmp/ops_audit/pre_remediation/current_novamarket_suite_record.json`.

The two skips are pre-existing and reported, not hidden; neither is a security test and neither was added by this remediation. The full-suite warning count is high but warnings are not failures; no warning was converted into a passing assertion.

## 8. Dependency scan

Initial `pip-audit --local` result: **61 raw findings / 52 unique across 4 packages**:

- Pillow 12.3.0: 42 findings (5 critical, 24 high, 13 moderate).
- certifi 2026.7.22: 1 critical.
- pip 24.2: 6.
- setuptools 78.1.0: 3.

Corrections installed: Pillow **13.0.0**, certifi **2026.11.20**, pip **26.3**, setuptools **84.2.1**. The vision suite passed **289 tests + 14 subtests** after the Pillow upgrade.

Final command and result:

```powershell
..\.venv\Scripts\python.exe -m pip_audit --local --progress-spinner off
# No known vulnerabilities found  (exit 0)
```

Raw evidence: `tmp/ops_audit/pip_audit_local.json` and `tmp/ops_audit/pip_audit_after_tooling.json`.

Limitation: `pip-audit` skips the local editable `novacontrol` project and the environment's `torch 2.15.0+cpu` build because neither resolves to a PyPI distribution. "No known vulnerabilities" therefore applies to the audited distributions, not to those two skipped entries. The scanner is installed as a dev dependency; no CI workflow was added.

## 9. Other verification

| Check | Result |
|---|---|
| `mypy src/novacontrol` | Success: no issues found in 725 source files. |
| `python scripts/generate_api_reference.py --check` | In sync: 936 routes. |
| `npm run build` in `web/` | Passed (991.38 kB JS bundle; Vite chunk-size warning only). |
| `ruff` on changed areas | New security modules clean; 4 pre-existing test-file findings remain (duplicate route item and line-length). No suppression was added. |
| `git status` | Worktree changes remain uncommitted; no commit or push. |

The API grew by one route (`POST /events/session`) and remains documented in `docs/API.md`. The manual SSE migration text is present at the end of that file.

## 10. Remaining limitations and non-claims

1. **No security certification.** The tests cover the named local-API findings, not every possible deployment, dependency or browser.
2. **Audit durability is still best-effort.** Three retries and a unique temp file reduce Windows/OneDrive sharing failures; a permanent failure still loses the record. There is no secondary sink.
3. **No scheduled backup/retention/PITR/replication.** Recovery drills pass, but the single SQLite store remains a single point of failure.
4. **No multi-hour or concurrent-load soak.** The 300-second run is a bounded smoke/soak, not a capacity test.
5. **No live provider, broker, market session or remote TLS/proxy test.** Live readiness remains blocked.
6. **No 2027 calendar verification.** The scheduler fails closed outside its verified coverage.
7. **`torch+cpu` and the local project were skipped by `pip-audit`.**
8. **`ruff` is not clean** because of pre-existing test-file findings; the full suite and mypy are green.
9. **The UI was built, not visually exercised in a browser.** The React build and static asset tests pass; no screenshot or click-through is claimed.
10. **The historical single-process crash cause is unresolved.** The current green run reduces, but does not explain, the original failure.

## 11. Readiness

| Area | Rating | Basis |
|---|---|---|
| Security controls for the local API | **PASS** | Fail-closed auth, explicit anonymous mode, random launcher key, masked 5xx, cookie-based SSE, dependency scan. |
| Audit and error observability | **PARTIAL** | Correlation and counters exist; audit remains best-effort and no general resource metrics endpoint exists. |
| Database recovery | **PASS** | Lock, read-only, disk-full, WAL and backup/restore drills pass on disposable stores. |
| Backup/retention/PITR | **PARTIAL** | Manual backup works; no schedule, retention job, PITR or replication. |
| Test runner | **PASS (current) / PARTIAL (historical)** | All phases green and single-process green; historical crash not reproduced or explained. |
| Bounded soak | **PARTIAL** | 300 s, 406/406 200s, stable RSS/handles; not multi-hour or concurrent. |
| Paper-trading readiness | **PARTIAL** | Safety controls are tested; no sustained live-market paper run. |
| Live-trading readiness | **BLOCKED** | No broker, live data, production deployment or real-money authorization. |
| Strategy/RL effectiveness | **FAIL** | No demonstrated edge; RL remains degenerate. Unchanged by this remediation. |

**Bottom line:** the identified local-API security weaknesses are corrected and the current suite is green, including a true single-process run. Audit durability, backup automation, multi-hour stability, live-provider recovery and the existing strategy/RL evidence remain limitations. The system is suitable for attended paper drills and local development, not for exposed production use or live trading.

No PI-25 was created. No live trading was enabled. No broker was connected. No RL policy was activated or promoted. No financial data was deleted. No commit or push was performed.
