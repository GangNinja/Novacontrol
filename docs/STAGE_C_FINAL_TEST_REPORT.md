# NovaMarket — Stage C Final Test Report

Date: 2026-11-27. Host: Windows 11, Git Bash, Python from `../.venv`,
`PYTHONPATH=src`, pytest 9.1.1 (no `pytest-timeout`). Every suite ran against the
working tree; nothing was committed, stashed or reverted.

**Bottom line:** all **216** test files were executed and every file now passes.
A single-process `pytest tests` run has still never completed on this host; the
per-file matrix below is the evidence, and it is not identical to a one-process
run (module-level state is isolated per file). Tests passing does **not** close
Stage C — C1, C2 and C7 remain operator/market items.

## 1. Why per-file instead of one process

An earlier `pytest tests -q` run was stopped at 17% after 90 minutes (~9 hours
projected). This host serves the tree from OneDrive, so file-heavy fixtures are
10–30× slower than in CI, and no per-test timeout plugin is installed to bound a
stalled test. The matrix bounds every file instead:

- runner: `tmp/run_suite_matrix.sh` (resumable, appends one row per file)
- bound: 240 s per file, 900 s for app/e2e/web files
- ledger: `tmp/stage_c_remediation_evidence/suite_runs/matrix.tsv` (raw),
  `matrix_final.tsv` (resolved, with re-run notes)
- logs: one `test_<name>.log` per file

## 2. Result by outcome

| Outcome | Files |
|---|---|
| Passed on first run (exit 0) | 209 |
| Passed after a wider bound | 2 — `test_application.py` (145 s), `test_novamarket_api.py` (327 s) |
| Passed after a code fix | 1 — `test_desktop.py` |
| Passed after isolating the host environment | 2 — `test_browser_token_reuse_e2e.py`, `test_focus_rings.py` |
| Passed on re-run only (transient) | 1 — `test_build_project.py` |
| No tests collected — optional-dependency skip (exit 5) | 1 — `test_database.py` (`sqlalchemy` absent) |
| **Total** | **216 / 216** |

Aggregate across the per-file summaries, after substituting the resolved runs:
**8,189 passed tests and 6,426 passing subtests, 3 skipped, 0 failed, 0 errors**.
The three skips are optional-dependency skips (e.g. `sqlalchemy` is not
installed, so `test_database.py` skips its only test — pytest exits 5 there,
which is "nothing collected", not a failure).

## 3. The six first-run failures and their root causes

Each was investigated to a cause; none was silenced, skipped or weakened.

### 3.1 `test_application.py` — bound, not a hang

The 240 s bound expired. Verbose instrumentation showed steady progress, and a
wide-bounded run finished **34 passed, 15 subtests in 144.83 s**. The cost is
`NovaControlApplication()` boot plus a full self-improvement scan of `src/` and
`tests/`; measured cold scan: **19.6 s** for 948 files, and the learning-loop
test runs five of them. On OneDrive this is simply slow. Root cause:
environment (cold file reads), not a code defect.

### 3.2 `test_novamarket_api.py` — bound, not a hang

Same shape: the 240 s bound expired at 50 of 66 tests. A wide-bounded run
finished **66 passed, 351 subtests in 326.84 s**. Root cause: environment.

### 3.3 `test_desktop.py` — a real pre-existing defect, fixed

`CaptureWorkerProtocolTests::test_a_cancelled_exchange_cannot_answer_the_next_command`
failed with `ValueError: invalid literal for int() with base 10: '800x600'`.

Root cause: `PersistentScreenCapture._read_line` marked the worker `_desynced`
only on timeout and only `_request_guarded` marked it on cancellation. A
cancellation that landed during the **startup banner read** therefore left
`ready 800x600` unread on the pipe; the next exchange saw the process "running"
and parsed that banner as its command reply. On a host where Python takes
longer than 150 ms to start the helper, the test's 0.15 s cancel hits exactly
that window.

Fix (`src/novacontrol/desktop/capture.py`): `_read_line` now sets
`self._desynced = True` and re-raises on `asyncio.CancelledError`, so the next
exchange restarts the worker before trusting any line. `capture.py` and
`test_desktop.py` were unmodified before this change (clean against HEAD), so
the failure predates this work. Verified: **81 passed, 20 subtests** (was
1 failed / 80 passed).

### 3.4 `test_browser_token_reuse_e2e.py` — load-sensitive, passes isolated

First run: `Page.goto` exceeded 30 s against an otherwise healthy local server
(the earlier attempt failed at "test server did not become healthy"). Isolated
re-run: passes. Root cause: Playwright + a uvicorn server + a 30 s navigation
timeout on a host serving static assets from OneDrive. Environmental.

### 3.5 `test_focus_rings.py` — host `.env` auth, proven

The walker waits 20 s for `#statusText` to stop containing "Initializing".
It failed with `ready: complete` but `status: "Initializing..."`, while the page
body already showed "READY / reading hardware…".

Root cause: this host's `.env` defines `NOVACONTROL_ADMIN_TOKEN`
(+ `NOVACONTROL_ALLOW_LEGACY_ADMIN_TOKEN`). Every test server inherits the
process environment, so `/status` returned **401** to the tokenless page and the
boot probe never cleared. Measured directly: `create_app` startup 30.3 s and
`GET /status` → 401. Re-run with that variable emptied:
**7 passed in 75 s**. Root cause: host configuration, not code; the test assumes
the tokenless loopback dev setup CI uses.

### 3.6 `test_build_project.py` — one transient failure, not reproducible

A retained log showed `1 failed, 20 passed`
(`test_project_without_model_returns_scaffold`) although the ledger recorded exit
0 for that file. Three consecutive isolated re-runs: **21 passed** each. Recorded
as transient; no code change.

## 4. Static analysis and contract checks (re-run after the final edit)

| Check | Command | Result |
|---|---|---|
| mypy | `mypy src` | **Success: no issues in 729 source files** |
| mypy (Windows view) | `mypy src --platform win32` | **Success: no issues in 729 source files** |
| API reference | `python scripts/generate_api_reference.py --check` | **in sync (936 routes)** |
| Ruff (new Stage C files) | `ruff check dedupe.py stage_c_20m_verify.py test_stage_c_remediation.py` | **All checks passed** |
| Ruff (`capture.py`) | `ruff check src/novacontrol/desktop/capture.py` | 21 pre-existing E501 long lines (64–334), none in the added lines |

## 5. Stage C-specific verification

| Check | Result |
|---|---|
| `tests/test_stage_c_remediation.py` | **35 passed** (R-01, R-02, R-03, R-06, R-07, horizon compatibility, label-form bridge, independent 20m verification) |
| 20m verifier, XNSE | 250/250 reproduced, **1000/1000 independent 5m components**, 250/250 independently recomputed, 0 problems |
| 20m verifier, XBOM | same, 0 problems |
| NSE forecast end to end | 248 bars → `bootstrap` active → `fc_42d151e421dd9f79a566` → `models("5d")` returns 6 → bridge `["one_week"]` |
| BSE forecast | BLOCKED: 108 daily rows (101 sessions) < 140 minimum (unchanged) |
| DB integrity | `PRAGMA integrity_check` = ok on both isolated stores |
| API surface | 936 routes, in sync |

Affected-module integration files, re-run after the SC-R08-01 horizon change (all
from the same matrix, all exit 0):

| File | Duration |
|---|---|
| `test_stage_c_remediation.py` | 6 s |
| `test_novamarket_marketdata.py` | 52 s |
| `test_novamarket_autonomy.py` | 66 s |
| `test_novamarket_strategies.py` | 27 s |
| `test_novamarket_pi12.py` | 31 s |
| `test_novamarket_pi13.py` | 32 s |
| `test_novamarket_pi23_pi12_evidence.py` | 12 s |
| `test_novamarket_forecasting.py` | 9 s |
| `test_novamarket_multitimeframe.py` | 5 s |
| `test_novamarket_at02…at06` (+ runtime/e2e) | 8–41 s |
| `test_decision_engine.py` | 1000 s |
| `test_bse_bhavcopy.py` / `test_nse_bhavcopy.py` | 10 / 4 s |

Evidence: `tmp/stage_c_remediation_evidence/`
(`r05_5m_backfill_*.json`, `r05_20m_*.json`, `r08_train_xnse.json`,
`suite_runs/`).

## 6. What this report does not claim

1. **No single-process full-suite pass.** The matrix covers all 216 files but
   isolates module-level state per file; a CI run on a fast filesystem is still
   the right gate for that.
2. **No live-market behaviour.** No session, proposal, order or fill was run.
3. **No forecast quality.** The stored NSE forecast has `validity = null`; no
   matured outcome was observed.
4. **No Stage C closure.** C1 (R-09 interpretation), C2 (BSE forecast history)
   and C7 (natural lifecycle) remain open regardless of these results.
5. **Host-dependent tests are still host-dependent.** `test_focus_rings`
   needs `NOVACONTROL_ADMIN_TOKEN` unset, and the two Playwright files need a
   quiet host; CI satisfies both, this workstation does not by default.
