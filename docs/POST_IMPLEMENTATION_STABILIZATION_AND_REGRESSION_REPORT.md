# Post-Implementation Stabilization and Regression Report

**Repository:** NovaControl / NovaMarket
**Date:** 26 November 2026
**Status:** **PASS — stabilized on the current worktree.**
**Scope:** Close the 17 broader-suite failures, recover every timeout, preserve
Stage A/B invariants, and produce a genuinely green broad regression without
feature work, live trading, promotion, commit, or push.

## 1. Summary

The prior broader-suite baseline was **8,137 passed / 17 failed / 3 skipped /
6,426 subtests** across 44 bounded groups. It also had three groups cut off by a
300-second guard, so the run was not a completed green regression.

This stabilization pass:

- reproduced and root-caused all 17 failures;
- fixed the genuine defects and corrected stale or environment-dependent test
  contracts;
- isolated developer `.env` credentials from spawned test servers;
- fixed a production event-loop starvation path in radar ingestion exposed by
  the browser E2E recovery;
- replaced the 300-second group guard with a 1,800-second guard so slow but
  healthy groups finish rather than being recorded as timeouts;
- reran the complete 44-group inventory after the fixes.

Final result:

| Measure | Final |
|---|---:|
| Groups | **44 / 44 exit 0** |
| Passed | **8,154** |
| Failed | **0** |
| Skipped | **3** (intentional, pre-existing) |
| Subtests passed | **6,426** |
| Runtime outcomes | **8,157** |
| Collected tests | 8,156 |
| Omitted test files | **0** |
| Timed-out groups | **0** |

The small difference between collected tests and runtime outcomes is the
pre-existing module-level skip in `tests/test_database.py`. The 44 chunk
manifests contain exactly 215 unique files, matching all 215 `tests/test_*.py`
files on disk — no missing or duplicated file.

## 2. Invariants preserved

- No production authentication, authorization, token gate, trading control, or
  risk gate was weakened.
- No assertion was deleted to make a test pass; assertions were narrowed only
  where they over-specified unrelated rows (audit lifecycle rows) or corrected
  where their contract had drifted (four valid timeline routes).
- No test file was deselected, excluded, or converted to a skip.
- Paper-only, NM-08, NM-10, lease, duplicate-order, and advisory-RL invariants
  are unchanged.
- No Stage C, live-market acceptance, live order, soak, RL promotion, commit,
  or push was performed.
- The existing dirty worktree and user changes were preserved.

## 3. What changed

### 3.1 Production

**Radar ingestion no longer blocks the API event loop.**

`radar/extraction.py` now caches compiled entity-term patterns with an
`lru_cache`, and `radar/engine.py` builds the instrument table/entity hints once
per refresh and runs extraction through `asyncio.to_thread`.

This was not cosmetic. During a browser E2E run the API process was observed
stacked in:

```text
_run_runtime -> _runtime_tick -> radar_refresh -> _ingest_entry
-> extraction._matched_entities -> re._compile
```

While that synchronous burst ran, the page could not complete its readiness
call and Playwright timed out. Caching plus the worker-thread offload restored
API responsiveness. Radar classification tests still pass unchanged.

**Focus rings in `market-workspace.css`.**

The combined hover/focus rules suppressed keyboard outlines. Hover and
`:focus-visible` are now separate, and nav items/buttons declare an explicit
2 px solid fallback ring with an offset.

### 3.2 Test infrastructure

`tests/conftest.py` now exports two explicit test-only settings to spawned
servers:

- `NOVACONTROL_ALLOW_ANONYMOUS_LOCAL=1`;
- `NOVACONTROL_SKIP_DOTENV=1`.

This prevents a developer `.env` admin token from switching browser-test
subprocesses into authenticated mode while leaving production `.env` loading
untouched.

### 3.3 Test contracts and harnesses

| Area | Corrected contract |
|---|---|
| Automation audit | No automation-source row is allowed; exactly one matching request row is required. Legitimate NovaMarket lifecycle rows are allowed. |
| Build/save shutdown | The mocked NovaMarket runtime stop is an `AsyncMock`, matching the real async lifecycle. |
| Evaluation retention | “Fresh” is relative to the current clock; only the deliberately 90-day-old row expires. |
| NovaForge observer | A synthetic frame without an explicit timestamp is current at read time; explicit stale/unknown timestamps still exercise rejection. |
| NovaForge timeline | The exact four authenticated GET timeline routes are asserted. |
| Guided click cleanup | Temporary SQLite cleanup retries for up to 10 s on the Windows handle-release window; a persistent lock still fails. |
| Telemetry memory bar | The bar is compared with the percentage implied by the card’s own displayed used/total bytes, avoiding a live-memory race with a later API read. |
| Reduced-motion tilt probe | The probe selects the first on-screen card **without interactive descendants**, matching `effects.js`’s deliberate tilt eligibility rule. |
| Focus walker output | Node stdout is decoded as UTF-8, not the Windows locale code page. |
| Preference API tests | A deterministic 64 GiB free-memory estimate isolates run lifecycle tests from live machine pressure; estimator guard tests remain real. |

## 4. Verification

### 4.1 Broad regression

Command:

```bash
PYTHONPATH=src ../.venv/Scripts/python.exe -u \
  tmp/full_suite_groups/run_all.py 1 44
```

Final status: `DONE`, 44 chunks, 215 files, `failures: 0`.

The slowest chunk was 310.2 s; the 1,800-second guard leaves ample headroom and
no timeout occurred.

### 4.2 Focused and integration checks

| Check | Result |
|---|---|
| Stage A focused regression (13 files) | **377 passed, 2 skipped, 9 subtests**, exit 0 |
| Stage B `test_novamarket_at06_end_to_end.py` | **1 passed**, exit 0 |
| Radar / PI-24 | 71 passed + 9 subtests / 32 passed |
| Browser token reuse E2E | 1 passed |
| Guided click E2E | 2 passed |
| Telemetry E2E | 3 passed |
| Reduced-motion E2E | 2 passed |
| Focus-ring CSS + CDP walk | 7 passed |
| Tab focus walk E2E | 2 passed |
| Preference affected group | 212 passed in the six-file block |

### 4.3 Static, type, frontend and API

| Check | Result |
|---|---|
| Mypy `src` | 728 files, no issues, exit 0 |
| Mypy `src --platform win32` | 728 files, no issues, exit 0 |
| `web` production build | exit 0; only pre-existing dynamic-import and chunk-size warnings |
| API reference `--check` | in sync, 936 routes, exit 0 |
| Security + database resilience | 27 passed, exit 0 |
| `git diff --check` | exit 0 |

## 5. Recovered failures and timeouts

- The three original 300-second group timeouts (chunks 9, 23, 43) all completed
  within the longer guard; their files ran in the final green inventory.
- One intermediate all-group run had three preference API failures caused by
  live free-memory pressure. The test environment was made deterministic and
  the affected six-file group passed; the subsequent complete run was green.
- Browser E2E timeouts after the `.env` fix were traced to synchronous radar
  extraction and fixed in production, not by extending Playwright timeouts.

## 6. Skips

The final three skips are intentional:

- two `test_novamarket_marketdata.py` live-provider tests gated by
  `NOVAMARKET_LIVE_TESTS=1`;
- the `test_database.py` module-level optional-SQLAlchemy skip.

No skip was added by this pass. No offline result is presented as live-provider
verification.

## 7. Residual limitations

- No real-market acceptance, Stage C, live trading, or multi-hour soak was run.
- Browser checks use the installed Edge/Chromium on this Windows host, not a
  cross-browser matrix.
- The broader suite is a bounded-group regression (44 independent processes),
  not a single-process run; every collected file is covered.
- The worktree remains dirty with pre-existing user/implementation changes.
  Nothing has been committed or pushed.

## 8. Handoff

The current tree is stable enough to hand to the next authorized stage:

- all 8,154 runnable tests pass;
- the three intentional skips are documented;
- Stage A and Stage B remain green;
- mypy, frontend build, API reference, security/database checks and whitespace
  checks are green.

The next step is a separately authorized real-market acceptance/soak run, not
a code change. Stage C and live trading remain out of scope.
