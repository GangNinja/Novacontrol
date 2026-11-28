# Offline Integration Verification Report

**Repository:** NovaControl / NovaMarket
**Date:** 25 November 2026; suite status updated 26 November 2026
**Scope:** Close the two outstanding browser-level checks (D6 exchange-local timezone, D8 future relative timestamps), audit the rendered Autonomy integration, and determine Stage A / Stage B acceptance.
**Verdict:** **Stage A PASS (D1–D9 and Autonomy UI). Stage B PASS. Broader suite PASS.** These are offline / fixture and rendered-browser results, not a real-market acceptance or soak result. After the 26 November stabilization pass, the full suite completed in 44 bounded file groups with **8,154 passed, 0 failed, 3 skipped and 6,426 subtests passed**; all 17 prior failures are remediated and no group timed out. Stage C has not been started.

> No user `data/` directory was used or modified. The browser run used an isolated backend with a temporary data directory on `127.0.0.1:8012` and Vite on `127.0.0.1:5180`. The existing services on `127.0.0.1:8001` and `127.0.0.1:5173` were left untouched. No live-money trading was enabled, no real-market acceptance or soak was started, and no commit or push was performed.

---

## 1. What was verified

The two outstanding report items were checked on the **actual rendered Autonomy page**, not only by calling the formatting functions:

- D6: NSE `Asia/Kolkata`, NYSE `America/New_York`, the 1 November 2026 US fall-back transition, exchange-vs-browser timezone, exchange/timezone consistency, missing timestamps and invalid/missing IANA zones.
- D8: the rendered lease help text for recent past, future under 45 seconds, future over 45 seconds, the 44/45-second boundaries, zero, invalid timestamps, the live lease, the reliability lease and the exit-supervision `Last scan`.
- Integration: Autonomy page render, runtime status, pause/resume, lease-retry status, exchange selection, the separate market universe selector, other relative-time surfaces and application health.
- Stage B: the strengthened 20-step isolated paper lifecycle, including valid-candidate acceptance, stale-candidate rejection, independent NM-08 approvals, NM-10 paper execution, exit supervision, wallet/portfolio consistency, ledger reconciliation, realized P&L, forecast evaluation, advisory RL and `automatic_policy_promotion=false`.

The production paths exercised by the browser were:

| Concern | Production path |
| --- | --- |
| D6 formatter | `web/src/lib.ts` → `dateTimeInZone(value, timeZone)` |
| D6 caller | `web/src/pages/Autonomy.tsx` → `session.local_time`, `session.next_open` |
| D8 formatter | `web/src/lib.ts` → `timeAgo(value)` |
| D8 caller | `web/src/pages/Autonomy.tsx` → top Lease metric, reliability Lease metric, exit `Last scan` |
| Data path | `/market/autonomy/monitoring`, `/market/autonomy/status`, `/market/autonomy/reliability`, `/market/autonomy/exits/status` |
| Stage B | `tests/test_novamarket_at06_end_to_end.py` over the real AT-03/04/05 engines |

---

## 2. Browser harness

- Browser: installed Microsoft Edge/Chromium, headless through Playwright.
- Viewport: 1440 × 1000.
- Browser locale: `en-IN`.
- Browser timezone: explicitly `Asia/Kolkata`, so an NSE result alone could not prove that the exchange zone was used; NYSE is the discriminating case.
- Page: real Vite application at `http://127.0.0.1:5180/#/autonomy`.
- Backend: isolated FastAPI app created with `create_app(data_dir="tmp/stage_a_ui_acceptance/data")` on port `8012`.
- Route substitution: only `/market/autonomy/monitoring` and `/market/autonomy/status` were intercepted. The real API response was fetched and only the session or lease fields were replaced; every other request went to the isolated backend.
- D8 clock: frozen in the page with `Date.now = () => 1790000000000` so the rendered boundary text is exact. D6 used the real clock.
- Screenshots and machine-readable results were written under `tmp/stage_a_ui_acceptance/`.

A rehearsal run exceeded the isolated backend's 120 requests/minute limit. That produced HTTP 429 responses and intermittent stale fixture reads. It was a test-harness issue, not an application finding. The backend was restarted to clear the limiter and the final run used in-place `Refresh monitoring` / `Refresh` controls instead of repeated full page loads. The final run completed with **zero 429 responses**.

The browser's automatic `/favicon.ico` request is a 404 because the application defines no favicon. It is recorded below as a pre-existing, non-functional limitation. There were **zero page errors**, **zero application console errors**, and **zero failed API responses** in the final run.

---

## 3. Exact commands and results

All Python commands were run from `novamarket/` with the repository's parent virtual environment.

### 3.1 Isolated services

Backend (tokens redacted; values were passed only through the process environment):

```bash
mkdir -p tmp/stage_a_ui_acceptance/data
export NOVACONTROL_ENV=development
export NOVACONTROL_API_TOKEN='<isolated-api-token>'
export NOVACONTROL_ADMIN_TOKEN='<isolated-admin-token>'
export NOVACONTROL_AUTONOMY_OPERATOR_TOKEN='<isolated-operator-token>'
export NOVACONTROL_DISABLE_OLLAMA=1
export PYTHONPATH=src
../.venv/Scripts/python.exe -c \
  "import uvicorn; from novacontrol.api.app import create_app; \
   uvicorn.run(create_app(data_dir='tmp/stage_a_ui_acceptance/data'), \
   host='127.0.0.1', port=8012, log_level='warning')"
```

Frontend:

```bash
cd web
export NOVAMARKET_API_ORIGIN=http://127.0.0.1:8012
export NOVACONTROL_ADMIN_TOKEN='<isolated-admin-token>'
./node_modules/.bin/vite --host 127.0.0.1 --port 5180 --strictPort
```

Readiness was checked with HTTP 200 from `/health` and from the Vite origin before the browser run.

### 3.2 Browser D6 / D8 / Autonomy integration

```bash
cd novamarket
PYTHONPATH=src ../.venv/Scripts/python.exe \
  tmp/stage_a_ui_acceptance/verify_d6_d8.py
```

**Result: 39 checks, 0 failed (exit 0).**

Machine-readable evidence: `tmp/stage_a_ui_acceptance/ui_results.json` — 39 rows, all `ok: true`.

Screenshots:

- `tmp/stage_a_ui_acceptance/d6_nse.png`
- `tmp/stage_a_ui_acceptance/d6_nyse.png`
- `tmp/stage_a_ui_acceptance/d6_dst_fall_back.png`
- `tmp/stage_a_ui_acceptance/d6_invalid_zone.png`
- `tmp/stage_a_ui_acceptance/d8_future_90s.png`
- `tmp/stage_a_ui_acceptance/market_universe.png`

### 3.3 Stage A focused regression

```bash
PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest \
  tests/test_novamarket_at02.py tests/test_novamarket_at03.py \
  tests/test_novamarket_at04.py tests/test_novamarket_at05.py \
  tests/test_novamarket_at06.py tests/test_novamarket_at06_end_to_end.py \
  tests/test_novamarket_at06_runtime_integration.py \
  tests/test_novamarket_marketdata.py tests/test_novamarket_multitimeframe.py \
  tests/test_novamarket_scheduler.py tests/test_novamarket_autonomy.py \
  tests/test_novamarket_gateway.py tests/test_novamarket_features.py \
  -q -p no:cacheprovider
```

**Result: 377 passed, 2 skipped, 9 subtests passed in 231.11 s (exit 0).**

### 3.4 Stage B isolated paper lifecycle

```bash
PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest \
  tests/test_novamarket_at06_end_to_end.py -q -p no:cacheprovider
```

**Result: 1 passed in 1.83 s (exit 0).**

The same test was strengthened for this acceptance pass. It now explicitly asserts stale-candidate rejection, entry and exit NM-08 provenance, distinct entry/exit NM-08 decision IDs, NM-10 order linkage, portfolio-wallet cash equality, realized loss, and unchanged orders/wallet after the rejected candidate.

### 3.5 Type, frontend and API reference

| Check | Command | Result |
| --- | --- | --- |
| Mypy | `PYTHONPATH=src ../.venv/Scripts/python.exe -m mypy src` | **Success: no issues found in 728 source files** (exit 0) |
| Frontend production build | `cd web && npm run build` | **PASS**, exit 0, `tsc && vite`, built in 7.45 s |
| API reference sync | `PYTHONPATH=src ../.venv/Scripts/python.exe scripts/generate_api_reference.py --check` | **docs/API.md: in sync (936 routes)**, exit 0 |

The build emitted only the pre-existing chunk-size warning; there was no type or build error.

### 3.6 Full NovaMarket suite (broader regression)

The suite was run to completion in **44 bounded file groups** with a 1,800 s
guard, using `../.venv/Scripts/python.exe tmp/full_suite_groups/run_all.py 1 44`.
The final run covered 215 files and all 8,157 runtime outcomes. No collected
test file was omitted and no group timed out.

| Measure | Result |
| --- | --- |
| Groups | **44 / 44 exit 0** |
| Passed | **8,154** |
| Failed | **0** |
| Skipped | **3** (intentional, pre-existing) |
| Subtests passed | **6,426** |
| Runtime outcomes | **8,157** (8,156 collected + one module-level skip) |

The three skips are the two `test_novamarket_marketdata.py` live-provider tests
gated by `NOVAMARKET_LIVE_TESTS=1` and the `test_database.py` optional-SQLAlchemy
module skip. They are not hidden failures and no live-provider verification is
claimed.

The 17 failures from the prior baseline are all remediated. The full root-cause
matrix, fixes, targeted reruns and verification evidence are in
`FULL_SUITE_FAILURE_REMEDIATION_REPORT.md`. That pass also fixed a genuine radar
event-loop starvation defect exposed by the browser E2E recovery; it was traced
to synchronous regex extraction during a refresh, not to an authentication gate.

The two API activity-window tests that previously failed in a two-test `-k`
selection also pass in the full 66-test file and the ordered 220-test block;
those earlier failures were order-dependent and are not part of the 17.

---

## 4. D6 browser evidence

The following values are the actual text read from the rendered `Session-aware monitoring` card. The browser was in `Asia/Kolkata`; `06:45 pm` is the browser-local rendering that would appear if `dateTime()` were incorrectly used.

| Scenario | Exchange shown | Exchange-local time | Next open | Result |
| --- | --- | --- | --- | --- |
| NSE, `Asia/Kolkata` | `XNSE` | `25 Nov 2026, 06:45 pm IST` | `26 Nov 2026, 09:15 am IST` | PASS |
| NYSE, `America/New_York` | `XNYS` | `25 Nov 2026, 08:15 am GMT-5` | `25 Nov 2026, 09:30 am GMT-5` | PASS |
| US fall-back transition | `XNYS` | `01 Nov 2026, 01:30 am GMT-4` (before fall-back) | `02 Nov 2026, 09:30 am GMT-5` (after fall-back) | PASS |
| Invalid `local_time` and `next_open` | `XNSE` | `—` | `—` | PASS |
| Missing `local_time` | `XNSE` | `—` | not asserted (only `local_time` was removed; `next_open` remained present) | PASS for the missing local-time field |
| Missing `timezone` | `XNSE` | `—` | `—` | PASS |
| Unknown IANA zone (`Not/AZone`) | `XNSE` | `—` | `—` | PASS |

Additional assertions:

- For NYSE and the DST case, neither the local time nor the next open contained the browser-local `06:45 pm` rendering. The display followed the exchange zone, not the browser zone.
- For NSE, exchange identity was `XNSE` and both timestamps carried `IST`.
- For NYSE, exchange identity was `XNYS` and both timestamps carried `GMT-5`; the DST case showed `GMT-4` for the pre-transition instant and `GMT-5` for the next open.
- Invalid/missing local time, next open, and timezone returned `—`; no fallback to browser time occurred.
- The screenshots show the card at the rendered values; the machine-readable rows contain the exact strings above.

**D6 result: PASS.**

---

## 5. D8 browser evidence

The rendered value was the top `Lease` metric help text under a frozen page clock. The prefix is produced by the component (`Expires ${timeAgo(...)}`), so the table shows the complete rendered text.

| Scenario | Offset from frozen present | Rendered help | Result |
| --- | --- | --- | --- |
| Future, seconds | +30 s | `Expires in 30 seconds` | PASS |
| Future, last second boundary | +44 s | `Expires in 44 seconds` | PASS |
| Future, minute boundary | +45 s | `Expires in 1 minute` | PASS |
| Future, over one minute | +90 s | `Expires in 2 minutes` | PASS |
| Past, seconds | −30 s | `Expires 30 seconds ago` | PASS |
| Past, last second boundary | −44 s | `Expires 44 seconds ago` | PASS |
| Past, minute boundary | −45 s | `Expires 1 minute ago` | PASS |
| Near present | 0 s | `Expires now` | PASS |
| Invalid timestamp | n/a | `Expires —` | PASS |

No future value rendered as `just now` or `now`. The zero case correctly rendered `Expires now`.

Other relative-time surfaces on the real page:

| Surface | Actual rendered value | Result |
| --- | --- | --- |
| Live top Lease metric | `Expires in 1 minute` | PASS |
| Reliability Lease metric | `Expires in 1 minute` | PASS |
| Exit supervision `Last scan` | `11 minutes ago` | PASS |

**D8 result: PASS.**

---

## 6. Autonomy integration and regression evidence

| Check | Actual evidence | Result |
| --- | --- | --- |
| Autonomy page rendering | Real `#/autonomy` page loaded with the session card, warning/status banners, runtime metrics, decision/execution/exit sections and monitoring cycles | PASS |
| Runtime status | Mode metric `Autonomous`; Runtime metric `Running` | PASS |
| Pause control | Clicking `Pause` changed the controls to offer `Resume autonomous` | PASS |
| Resume control | Clicking `Resume autonomous` restored the `Pause` control against the real backend | PASS |
| Lease retry status | Controlled status payload rendered `Until 25 Nov 2026, 08:32 am · lease held by another runtime` in the `Lease and ownership` card | PASS |
| Exchange selection | NSE and NYSE session cards displayed the controlled configured exchange and its zone (see D6) | PASS |
| Universe display | The separate Market Overview index selector rendered `All index universes`, `Nifty 50`, `Nifty 100`, `Nifty Total Market`, `Nifty 500`, `Nifty Midcap 100`, `Nifty Smallcap 100` | PASS |
| Other relative-time displays | Top lease, reliability lease and exit `Last scan` all rendered correctly (see D8) | PASS |
| Page/API health | `page_errors=0 application_console_errors=0 failed_responses=0 favicon_404=1` | PASS |
| Known favicon limitation | Automatic `http://127.0.0.1:5180/favicon.ico` returned 404 because no favicon is defined | FAIL (non-functional) |

The Autonomy page does not contain a separate list widget named “universe”; its monitoring surface reports Exchange and Rotation (`0 · batch 20 of 20`), and the application’s universe selector is on Market Overview. That existing selector was loaded and checked so the shared UI surface was not left unexamined. This is recorded explicitly rather than claiming an Autonomy universe list exists.

The single favicon 404 is pre-existing and unrelated to D6/D8. It does not affect data, controls or rendering. It is listed under remaining defects rather than hidden.

---

## 7. Stage A acceptance matrix

| Acceptance item | Result | Evidence |
| --- | --- | --- |
| D1 — canonical instrument identity | PASS | Focused Stage A suite; AT-03 identity regressions |
| D2 — configured exchange/universe enforcement | PASS | Focused Stage A suite; AT-02 monitoring tests |
| D3 — 20-minute interval support | PASS | Focused Stage A suite; marketdata/gateway/features tests |
| D4 — XNYS daily bars during open session | PASS | Focused Stage A suite; marketdata tests |
| D5 — first scheduler tick before sleep | PASS | Focused Stage A suite; scheduler/runtime integration tests |
| D6 — exchange-local timezone rendering | PASS | 21 rendered checks; NSE, NYSE, DST, invalid/missing zones |
| D7 — pause/resume preserves intended mode | PASS | Real UI pause/resume in this run; focused autonomy tests |
| D8 — future relative timestamp rendering | PASS | 9 rendered boundary/invalid checks plus live/reliability/exit surfaces |
| D9 — bounded lease-conflict retry | PASS | Rendered retry text in this run; runtime integration tests |
| Existing Autonomy rendering and controls | PASS | Real page loaded; pause/resume exercised; no application errors |
| Exchange and universe surfaces | PASS | Exchange card in D6; Market Overview selector checked |
| Frontend production build | PASS | `npm run build`, exit 0 |
| Mypy | PASS | 728 files, no issues, exit 0 |
| API reference | PASS | 936 routes in sync, exit 0 |
| Focused Stage A regression | PASS | 377 passed, 2 skipped, 9 subtests, exit 0 |
| Full NovaMarket suite (broader, outside D1–D9/UI acceptance) | PASS | 44 bounded groups completed: **8,154 passed, 0 failed, 3 skipped, 6,426 subtests** (see §3.6 and `FULL_SUITE_FAILURE_REMEDIATION_REPORT.md`) |
| Real-market acceptance / soak | NOT_VERIFIED | Explicitly out of scope for this report and not started |

No D1–D9/UI Stage A item is marked PARTIAL. The broader full-suite regression is separately marked FAIL and is outside that acceptance matrix.

---

## 8. Stage B acceptance matrix

The single 20-step lifecycle test was inspected and strengthened. The matrix distinguishes assertions now made directly by that test from the production code that backs them.

| Acceptance condition | Result | Direct evidence in the lifecycle |
| --- | --- | --- |
| Valid candidate acceptance | PASS | Harness produces an `XNSE:ALPHA` candidate; proposal is `risk_approved`; entry execution reaches `completed`/`filled` |
| Invalid candidate rejection | PASS | A stale `XNSE:ALPHA` candidate cycle returns `invalid >= 1`; no proposal, no order, and no wallet change |
| Independent NM-08 approval | PASS | Entry risk record is `approved` with the proposal's `risk_decision_id`; exit risk record is separately `approved` and its assessment ID differs from the entry decision |
| NM-10 paper execution | PASS | Entry order ID starts `ord_`, fill has ledger transaction IDs, exit is a separate NM-10 paper sell order |
| Exit supervision | PASS | Price crash triggers one submitted exit; the decision is `filled`, `position_closed=true`, and the holding is gone |
| Wallet and portfolio consistency | PASS | Portfolio-wallet total equals `portfolios.detail(...).cash` before entry, after entry and after exit; funds decrease after entry and recover after exit |
| Ledger reconciliation | PASS | Exit decision `reconciliation_status == "passed"`; evidence status is `complete` |
| Realized P&L | PASS | Evidence contains an authoritative, non-zero realized P&L; the realized loss is negative and the wallet ends below its starting balance |
| Forecast evaluation | PASS | One due prediction is measured; error, absolute error, Brier score, provenance timestamp, MAE and directional accuracy are present |
| Advisory RL feedback | PASS | Feedback status `verified`, source evidence ID matches, fees > 0, unrealized P&L `0` |
| `automatic_policy_promotion=false` | PASS | Both forecast metrics and RL feedback assert false; `training_performed` is false |
| Restart reconciliation without replay | PASS | After restart, order IDs are unchanged, exactly one exit decision remains, and the learning evidence ID is unchanged |

**Stage B result: PASS** on the isolated 20-step paper lifecycle. This is fixture market data, not live-market acceptance.

---

## 9. Remaining defects

1. **The broader suite is green.** The final 44-group run is **8,154 passed, 0 failed, 3 skipped, 6,426 subtests**. The three skips are intentional (two live-provider opt-ins and the optional-SQLAlchemy module skip). All 17 prior failures are remediated; the detailed causes and fixes are in `FULL_SUITE_FAILURE_REMEDIATION_REPORT.md`. This does not change the real-market/Stage C boundary.
2. **Favicon 404.** `/favicon.ico` is requested automatically by the browser and returns 404 because the application defines no favicon. This is pre-existing, non-functional and unrelated to D6/D8. The page has no application console errors, page errors or failed API responses.
3. **No real-market acceptance or soak.** Not started. The Stage A–B evidence here is offline and fixture-backed; it does not claim live provider, live session, or multi-hour soak behavior.
4. **No cross-browser matrix.** Only the installed Edge/Chromium was used. D6 is exercised through Chromium's IANA/ICU time-zone data with `en-IN` formatting; other engines were not tested.
5. **D6/D8 used controlled session and lease fields.** The page and formatting code are real; the data values were deterministic. A live exchange session and natural clock passage remain Stage C.

No D6, D8, Autonomy integration or Stage B functional defect was found in this pass.

---

## 10. Stage C status

Not started. No authorization was given to proceed to real-market acceptance, a live session, a soak, live-money trading, or Stage C. The reports stop here.

The appropriate next step is operator review of these acceptance results before authorizing Stage C.

---

## 11. Evidence index

| Artifact | Purpose |
| --- | --- |
| `tmp/stage_a_ui_acceptance/verify_d6_d8.py` | Reproducible Playwright harness for D6, D8 and integration |
| `tmp/stage_a_ui_acceptance/ui_results.json` | 39 browser checks, all `ok: true` |
| `tmp/stage_a_ui_acceptance/d6_nse.png` | Rendered NSE session card |
| `tmp/stage_a_ui_acceptance/d6_nyse.png` | Rendered NYSE session card |
| `tmp/stage_a_ui_acceptance/d6_dst_fall_back.png` | Rendered fall-back transition card |
| `tmp/stage_a_ui_acceptance/d6_invalid_zone.png` | Rendered invalid-zone fallback |
| `tmp/stage_a_ui_acceptance/d8_future_90s.png` | Rendered future lease text |
| `tmp/stage_a_ui_acceptance/market_universe.png` | Market Overview universe selector |
| `tmp/stage_a_ui_acceptance/frontend_build.log` | Frontend build output |
| `tmp/stage_a_ui_acceptance/mypy.log` | Mypy output |
| `tmp/stage_a_ui_acceptance/api_reference.log` | API-reference sync output |
| `tmp/stage_a_ui_acceptance/backend.log` | Isolated backend log |
| `tmp/stage_a_ui_acceptance/vite.log` | Isolated Vite log |
| `tmp/stage_a_ui_acceptance/full_suite.log` | Old aborted full-suite partial output (64%, three `F` markers; failure indices replayed) |
| `tmp/full_suite_groups/status.json` | All 44 group results and guards |
| `tmp/full_suite_groups/chunk_manifest.tsv` | Reproducible chunk/file/test counts |
| `tmp/full_suite_groups/logs/chunk_*.log` | Per-group pytest output |
| `tmp/full_suite_groups/logs/files/*.log` | Per-file reruns for guarded groups |
| `tmp/full_suite_groups/chunk_23_rerun.log` | Ordered 220-test Novamarket block; all passed |
| `tmp/full_suite_groups/web_api_half1.log`, `web_api_half2.log` | 75 `test_web_api` tests split 38+37; all passed |
| `tmp/full_suite_groups/retest_*.log` | Isolated reruns of the current failures |
