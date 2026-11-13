# NovaMarket — Final Limitation-Elimination and Acceptance Report

**Date:** 9 November 2026 (baseline 8 November 2026; performance recheck 10 November 2026)  
**Workspace:** `novamarket/` (standalone; NovaControl core and NovaForge untouched)  
**Overall verdict:** **PASS — 25/25 routes, 470/470 current controls and 10/10 performance checks pass (480 baseline reconciled); internal acceptance scope, external certifications not claimed**  
**Paper-only boundary:** **CONFIRMED — no broker client, no live-order path, no real-money execution**

> **10 November 2026 post-fix addendum.** A fresh full 25-route sweep records **470/470 current entries PASS** (0 PARTIAL/BLOCKED/NOT PASS) in `docs/NM14_UI_CONTROL_MATRIX.json`. The 9 November **480** baseline is net 10 higher than the current rendered set: dashboard 47→39, market 34→29, shadow 5→4, tradingwatch 183→185, watchlists 19→20 and wallet 21→22. The 10 November performance recheck also passes all 10 API checks (concurrent p95 `1,397.34 ms`, soak p95 `166.06 ms`) and the 25-route browser sweep, so the QA completion verdict is **PASS**. This addendum supersedes only the control count and performance result; no 480/480 post-fix claim is made.

> **8 November 2026 addendum (superseded by the 9 November addendum below for all counts and verdicts).** The
> sections below describe the 7 November baseline. Official NSE and BSE
> bhavcopy ingestion, the official NSE index-option chain, cross-store fill
> recovery, the isolated operator paper flow and the latest regression results
> are recorded in the 9 November addendum below. Where the baseline says
> “126 instruments”, “4,635 without bars” or “Index Options BLOCKED”, the
> addendum supersedes it.

> This report supersedes the acceptance verdicts in
> `NM14_ACCEPTANCE.md` and `NOVAMARKET_STANDALONE_REPORT.md`. Those documents
> remain the baseline evidence for the executed NM-01–NM-14 chain, the
> production build and the 25-route render. They are not evidence that every
> advertised control was operated, and they do not remove the derivatives-data
> blocker.

---

## 9 November 2026 — final control, accessibility and performance addendum (authoritative)

This addendum supersedes the 8 November gap-closure addendum for every count
and verdict it states. No NovaControl or NovaForge source was modified, no
commit or push was made, and the paper-only boundary is unchanged.

### A. Verdict

**PASS (internal acceptance scope).** All 25 routes loaded in the real Chromium
UI and **470 of 470 current** recorded control results are PASS. The 9 November
480 baseline is net 10 higher than the current rendered set; the route
delta is recorded in `COMPLETE_UI_INTERACTION_MATRIX.md`. The final former
blocker, the OrderBlock `Filter by sector` select, was a sampling race: the
initial sweep read it before its data-derived options arrived. The targeted
retest observed 19 options, selected `Automobile and Auto Components`, changed
the visible rows from 108 to 100, and reset to `All sectors`. No external
certification is claimed.

### B. Requirement-level result

| Requirement | Result | Evidence |
|---|---|---|
| 25 routes | **PASS** | 25/25 route loads; 470 current results in `docs/NM14_UI_CONTROL_MATRIX.json` |
| 267-control baseline | **SUPERSEDED — PASS on 470/470 current** | Fresh all-route sweep plus targeted retests and write flows; no BLOCKED or PARTIAL row; 480 baseline reconciled |
| Refresh controls | **PASS** | Six refresh controls re-probed with a 12-second network window; each issued its authoritative read and received a response |
| Official NSE futures | **PASS for official EOD snapshot** | 667 futures, 224 underlyings; 18 IDF + 649 STF; NIFTY three expiries; expired contracts excluded; exact-underlying equity mapping only |
| Options verification | **PASS for official EOD data; IV is model-derived** | 6,625 contracts, 147 NIFTY strikes, PCR 1.6528; independent Black-Scholes reference; no provider IV feed |
| Workbench full scan | **PASS** | 4,535/4,535 eligible instruments in the audit store; 0 skipped, 0 failed, 0 duplicates; no 250-name cap; 0 synthetic bars |
| Performance / load / soak | **PASS** | All 10 checks; 120/120 concurrent requests with 0 errors; 90 s soak with 0 errors and −0.34 MB memory drift (9 November) |
| Performance / load / soak (10 November recheck) | **PASS** | All 10 checks; concurrent p95 `1,397.34 ms`; soak p95 `166.06 ms`; 0 errors; memory −0.58 MB; browser PASS |
| Accessibility | **PASS — internal automated review only** | 25 routes, 1,070 controls, 0 unlabeled controls, 0 heading/table/contrast/duplicate-ID/focus/overflow issues, 0 console/page errors; 10 `aria-sort` attributes across 6 sortable tables; modal and keyboard checks pass |
| Security | **PASS — internal review only** | 9/9 checks: headers, foreign origin 403, foreign host 400, same-origin validation, config secret scan, static unsafe-HTML/live-path scans, token enforcement, rate limit |
| Regression | **PASS** | 792 passed, 2 skipped, 938 subtests passed in 176.90 s (exit 0) |
| Production build | **PASS** | 1,598 modules; JS 566.64 kB / 152.21 kB gzip; CSS 91.25 kB / 16.80 kB gzip; chunk-size warning only |
| Acceptance chain | **PASS** | Proposal approved, paper order filled, backtest and shadow completed, evidence accepted, candidate promoted |
| Types, API docs, whitespace | **PASS** | `mypy src` and `mypy src --platform win32` clean (555 files); `docs/API.md` in sync; `git diff --check` clean (CRLF advisory only) |

### C. Final sector-filter retest

The initial all-route sweep sampled the sector select before its data-derived
options arrived and recorded only `All sectors`. The targeted retest waited for
the payload: the select exposed **19 options**; selecting `Automobile and Auto
Components` changed the visible rows from 108 to 100 and the main text from
17,219 to 15,874 characters; Reset restored `All sectors`. The row is therefore
PASS. This is recorded in `.ui_audit/final_control_retest.json`. The other four
former residuals are directly exercised:

- Dashboard `API key` dialog opened with the expected title and masked field and closed cleanly; no value was entered.
- Portfolios · Short changed from paused to active through the Resume confirmation and back to paused through the Pause confirmation; the final state matches the initial paused state.
- Paper `Submit from proposal` opened its approval dialog and kept `Submit paper order` disabled without an approved proposal; no order was created.
- Settings `Session API token` saved a clearly synthetic session value, showed `Token present`, and removed it with `Clear session token`; no backend credential was used.

### D. Performance and soak (9 November)

```text
API latency p95 (all HTTP 200)
  /market/status                    116.35 ms
  /market/overview                  416.94 ms
  /market/instruments               23.99 ms
  /market/screener                1,196.63 ms
  /market/portfolios?short_term      15.26 ms
  /market/portfolios?long_term       27.14 ms
  /market/wallets                    16.87 ms
  wallet transactions                33.49 ms
  wallet ledger                      25.89 ms
  /market/contracts                  28.46 ms
  /market/risk/alerts                24.07 ms
  /market/events                     25.57 ms
  open risk alerts                   12.75 ms
  full scan wall p95              1,817.55 ms  (limit 15,000 ms)

Concurrent load
  120/120 requests, 0 errors, 4.44 req/s
  median 1,859.32 ms; p95 2,360.51 ms; max 2,546.89 ms

90-second soak
  0 errors; p95 123.07 ms
  RSS 162.86 MB -> 162.52 MB; growth -0.34 MB; slope -0.27 MB/min

Browser sweep
  25 routes; max heading 356.70 ms; max settled 1,273.05 ms
  0 console errors; 0 page errors
```

An earlier run recorded one 1,281 ms cold-start `/market/status` sample; that
sample is not counted as a pass. The final 90-second run measured 116.35 ms p95
with five HTTP 200 responses, and eight independent warm calls measured
76–103 ms. Cold-start latency remains a deployment consideration, not a
steady-state failure.

#### D.1 Performance recheck (10 November)

The post-fix probe passes all 10 API checks and the browser sweep. Concurrent load was 120/120 requests with 0 errors, median `71.13 ms`, p95 `1,397.34 ms` (limit `3,000 ms`) and max `1,732.00 ms`. The 90-second soak had 0 errors and p95 `166.06 ms` (limit `1,500 ms`); memory moved from `168.19 MB` to `167.61 MB` (`-0.58 MB`, slope `-0.025 MB/min`). `/market/status` p95 was `123.45 ms` and `/market/screener` p95 was `699.51 ms`. Full scan p95 was `1,581.78 ms`; recovery took `2,357.95 ms` and returned HTTP 200. The browser sweep passed 25/25 routes with 0 console/page errors (max heading `281.60 ms`, max settled `1,189.86 ms`). Evidence: `.ui_audit/performance_postfix2.json`. Root cause and code changes: `BUG_FIX_REGISTER.md` (PERF-QA-001).

### E. Verification commands actually run

```bash
cd novamarket

PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest \
  tests/test_novamarket*.py tests/test_market_screener.py \
  tests/test_bse_bhavcopy.py tests/test_derivatives.py \
  tests/test_api_reference_sync.py -q
# 792 passed, 2 skipped, 938 subtests passed in 176.90s; exit 0

PYTHONPATH=src ../.venv/Scripts/python.exe .ui_audit/final_control_retest.py
# five residual controls; all PASS; sector waited for 19 options and reset

PYTHONPATH=src ../.venv/Scripts/python.exe .ui_audit/build_matrix.py
# 25/25 routes; 470 PASS (10 November current; 9 November baseline 480), 0 PARTIAL, 0 BLOCKED

PYTHONPATH=src ../.venv/Scripts/python.exe .ui_audit/perf_probe.py \
  --base http://127.0.0.1:8012 --pid 37400 \
  --browser-url http://127.0.0.1:5174 --soak-seconds 90 \
  --out .ui_audit/performance.json
# 9 November baseline: all 10 checks PASS; browser PASS

PYTHONPATH=src ../.venv/Scripts/python.exe .ui_audit/perf_probe.py \
  --base http://127.0.0.1:8012 --pid 27072 \
  --browser-url http://127.0.0.1:5174 --soak-seconds 90 \
  --out .ui_audit/performance_postfix2.json
# 10 November recheck: all 10 checks PASS; concurrent p95 1,397.34 ms; soak p95 166.06 ms
# 10 November regression rerun: 795 passed, 2 skipped, 938 subtests in 194.57 s; exit 0

PYTHONPATH=src ../.venv/Scripts/python.exe .ui_audit/a11y_probe.py
# 25 routes, 1,070 controls, no issue classes; internal review only

PYTHONPATH=src ../.venv/Scripts/python.exe .ui_audit/security_probe.py
# 9/9 checks PASS; internal review only

PYTHONPATH=src ../.venv/Scripts/python.exe scripts/verify_novamarket_chain.py
# exit 0; order filled; backtest/shadow completed; candidate promoted

cd web && npm run build
# 1,598 modules; built successfully; chunk-size warning only
```

### F. Limitations and non-claims

- The 25-route accessibility result is an internal automated review, not a WCAG conformance certification; manual assistive-technology review remains outstanding.
- The security result is an internal HTTP/static review, not an independent penetration test.
- The initial OrderBlock sector-filter sweep was a timing artifact: it sampled before the 19 data-derived options arrived. The final retest waited for the populated control, changed the visible rows, and reset; it is not a product limitation.
- The Paper submission dialog was verified only up to its correct disabled state because no approved proposal existed; no paper order was created by that test.
- The API-key dialog was opened and closed without entering a credential; Settings used a synthetic session value that was cleared.
- 99 BSE instruments still have no daily bar because the downloaded BSE session file contains no row for those symbols; 151 short histories are recent listings (3–29 bars). No bars were invented.
- Provider-reported option IV remains unavailable; the calculated IV is labelled as model output.
- No broker client, live-order path, real-money execution, commit or push was introduced. The performance work is confined to the NovaMarket API/read path and is recorded in `BUG_FIX_REGISTER.md`; NovaControl core and NovaForge were not modified.

---

## 8 November 2026 — NM-14 gap-closure addendum (superseded by 9 November)

This addendum supersedes the earlier 8 November implementation addendum for
the items it covers. The source-of-truth document is this file; the separately
named `NM14_FINAL_ACCEPTANCE_REPORT(2).md` is not present in the repository.
No NovaControl or NovaForge source was modified, and no commit or push was made.

### A. Executive verdict

**PARTIAL.** The two largest implementation gaps are now closed with evidence:
the Workbench no longer has a 250-instrument cap, and official NSE index and
stock futures are parsed and exposed from the same F&O bhavcopy. Historical
coverage is classified rather than treated as a single unexplained deficit.

The verdict remains PARTIAL because the full 25-route / 267-control browser
matrix, a formal load/memory soak, a WCAG review and an independent security
assessment were not completed. Provider-reported option IV is still
unavailable; the calculated IV is correctly labelled as model output.

### B. Requirement-level result

| Requirement | Previous status | Work performed | New status | Evidence / limitation |
|---|---|---|---|---|
| Remove the 250-instrument scan cap | PARTIAL | `DEFAULT_SCAN_LIMIT=None`; deterministic 1,000-row inventory pages; 250-row analysis batches; compact/no-chart analysis; duplicate, skip, failure and truncation reporting | **PASS** | 4,535 / 4,535 eligible instruments scanned; 0 skipped, 0 failed, 0 duplicates; 15.586 s |
| Full coverage must not silently truncate | PARTIAL | Page and batch boundary tests; inventory-failure and short-history tests; explicit counts in API and UI | **PASS** | `test_novamarket_workbench_scan.py`; `scan_cap=null`, `truncated=false`, `complete=true` |
| Historical-data completeness | PARTIAL | Classified every zero/short instrument against exchange and downloaded source file | **PARTIAL — data limitation documented** | 99 BSE names have no row in the downloaded BSE session; 151 short histories are recent listings (3–29 bars) |
| NSE stock and index futures | NOT IMPLEMENTED | Added `IDF`/`STF` parsing, expiry identity, OI, Δ OI, volume, value, trades and lot size to the existing F&O module and snapshot | **PASS for official EOD snapshot; PARTIAL for equity-scanner integration** | 667 futures, 224 underlyings; 18 IDF + 649 STF; NIFTY three expiries |
| Options verification | PARTIAL | Verified 6,625 contracts, 147 NIFTY strikes, PCR 1.6528, expiry/OI aggregation; added independent Black-Scholes reference and invalid-input tests | **PASS for official EOD data; IV is model-derived** | 8 derivative tests pass; no provider IV feed |
| 25 routes / 267 controls | PARTIAL | Targeted TradingWatch browser check of the new futures card; no full matrix rerun | **PARTIAL** | 5176 render: 667 contracts, 224 underlyings, 0 console errors; 267 controls not re-operated |
| Performance / load | PARTIAL | Measured full-catalogue scan; no soak | **PARTIAL** | 15.586 s, 5 pages, 19 batches; no RSS/soak measurement |
| Security / accessibility | NOT CERTIFIED | Preserved paper-only boundary; no new live path | **PARTIAL** | No independent penetration test, WCAG certification or formal soak |

### C. Market-data coverage (audit store)

| Measure | Count |
|---|---:|
| Catalogue instruments | 4,785 |
| Instruments with at least one daily bar | 4,686 |
| Instruments with at least 30 daily bars (eligible) | 4,535 |
| Zero-bar instruments | 99 |
| Instruments with 1–29 bars | 151 |
| NSE eligible / short | 2,341 / 52 |
| BSE eligible / short / zero | 2,194 / 99 / 99 |
| Stored daily bars | 201,947 |
| Distinct daily-bar sessions | 291 (2025-10-29 → 2026-11-06) |

The 99 zero-bar names are all BSE cross-listings (`universe_source = "BSE
listing of the NSE company"`) and **none of the 99 symbols appears in the
downloaded 5,059-symbol BSE session file**. No BSE row therefore exists to
backfill; no bar was invented. The 151 short histories all begin between
September and November 2026 (3–29 bars), consistent with recent listings or
recently commenced trading. This is a source/listing limitation, not a silent
ingestion success claim. Evidence: `.ui_audit/historical_coverage.json`.

### D. Workbench coverage and performance

```text
catalogue_total          4785
instruments_with_bars    4686
eligible                 4535
scanned                  4535
analysed                 4535
skipped                  0
failed                   0
duplicates               0
scan_cap                 null
truncated                false
complete                 true
inventory_pages          5
batches                  19
duration_ms              15585.81
```

The scan uses a read-only paged inventory (1,000 rows per page), deterministic
`latest_observed_at DESC, instrument_id ASC` ordering, and bounded 250-row
analysis batches. Analysis is compact and chart-free, so the full scan does not
retain thousands of chart payloads. A positive `scan_limit` remains supported
only as an explicit, disclosed sample.

### E. Futures and options

The 6 November 2026 official NSE F&O bhavcopy produced **6,625 index-option
contracts plus 667 futures contracts (7,292 total)**. Futures split into **18
index futures (`IDF`)** and **649 stock futures (`STF`)**, covering **224
underlyings**. NIFTY futures are present for 2026-11-23, 2026-12-29 and
2027-01-25; the near contract has OI 12,088,505, Δ OI −553,085, volume 125,137
and lot size 65. Stock futures retain expiry, OI, Δ OI, volume, settlement,
value, trades and lot size.

The NIFTY option chain has 147 strikes and PCR **1.6528** from official call OI
**145,332,135** and put OI **240,203,665**. Open interest, premium, volume and
settlement are provider values. IV is **Black-Scholes model
output** (6.5% rate, expiry 15:30 IST, bisection on the official premium), never
labelled as exchange-reported IV. The independent reference test recovers a
20.00% input within 0.05 percentage points; missing or expired inputs return
`None`.

The equity `fno` scanner remains **BLOCKED** because the NM-02 equity catalogue
has no futures instrument registry. Official futures are exposed in the
separate TradingWatch futures panel; equity setups are not relabelled.

### F. Frontend verification performed

The full-data API on `127.0.0.1:8014` returned `scan_cap=null`, **126 / 126**
eligible instruments for the default live database, `options.status=AVAILABLE`
(147 strikes, PCR 1.6528) and `futures.status=AVAILABLE` (667 contracts, 224
underlyings, three NIFTY expiries). The 5176 production-style dev page rendered
the futures card with provider values and **no console errors**.

The default live database contains only 126 stored-bar instruments, so the live
page truthfully reports 126 / 126. The 4,535 / 4,535 scan is the isolated audit
database above; no single UI number mixes the two populations.

The complete 25-route / 267-control matrix was **not rerun**. The historical
247 PASS / 12 PARTIAL / 8 BLOCKED matrix remains historical evidence, not a
current all-control verification.

### G. Regression and static verification

```text
pytest tests/test_novamarket*.py tests/test_market_screener.py tests/test_bse_bhavcopy.py \
  tests/test_derivatives.py tests/test_api_reference_sync.py -q
782 passed, 2 skipped, 938 subtests passed in 192.62s, EXIT:0

focused after final coverage-label correction
17 passed in 8.20s

mypy src                          Success: no issues found in 555 source files
mypy src --platform win32         Success: no issues found in 555 source files
generate_api_reference.py --check docs/API.md: in sync (586 routes)
npm run build                     tsc -b && vite build succeeded (562.40 kB JS / 151.14 kB gzip)
verify_novamarket_chain.py        exit 0; order filled; candidate promoted
git diff --check                  exit 0 (only CRLF advisory)
```

The 782-test run covers NovaMarket, market screener, BSE ingestion, derivatives
and API-reference synchronization. It is not the entire repository-wide suite:
the all-tests invocation exceeded the host's 295-second command limit, so no
full-suite pass is claimed here. The security/optimization/performance and
25-route browser suites were not rerun in this pass.

### H. Remaining blockers

1. **Implementation/verification:** full 25-route / 267-control browser matrix
   not rerun; PARTIAL and BLOCKED rows remain unverified.
2. **Performance:** no sustained RSS/memory soak; `psutil` is unavailable, so no
   independent process-memory measurement is claimed.
3. **Security/accessibility:** no independent penetration test, WCAG audit or
   certification. Paper-only execution and the absence of a broker path remain
   confirmed by the existing design and tests.
4. **Data source:** 99 BSE cross-listings are absent from the downloaded BSE
   session; 151 recent listings legitimately have 3–29 bars. No fabricated data
   will close either gap.
5. **Derivatives:** provider-reported IV is unavailable; the model IV is
   correctly disclosed. Futures are available in the F&O snapshot but not in
   the equity scanner's contract registry.

### I. Files changed in this gap-closure pass

- `src/novacontrol/novamarket/marketdata/store.py` — paginated inventory
- `src/novacontrol/novamarket/marketdata/engine.py` — offset pass-through
- `src/novacontrol/novamarket/workbench.py` — full scan, truthful coverage,
  futures status
- `src/novacontrol/novamarket/marketdata/derivatives.py` — `IDF`/`STF` parser
  and futures snapshot
- `web/src/pages/TradingWatch.tsx` — futures panel and coverage labels
- `web/src/pages/OrderBlock.tsx` — coverage labels
- `tests/test_novamarket_workbench_scan.py` — pagination/failure regressions
- `tests/test_derivatives.py` — futures, PCR, independent IV references
- `docs/API.md` — regenerated from the NovaMarket registries (586 routes)
- `docs/NM14_FINAL_ACCEPTANCE_REPORT.md` — this addendum
- `.ui_audit/derivatives_snapshot.json`, `data/derivatives_snapshot.json` —
  official EOD snapshot evidence
- `.ui_audit/historical_coverage.json` — zero/short classification

### J. Release readiness

NovaMarket remains a **paper-only** release candidate. The market desk, full
catalogue scan, official EOD ingestion, option chain and futures snapshot are
implemented and tested. It is **not** ready to be called fully accepted until
the control matrix, load/soak, security and accessibility evidence are actually
completed. No live broker or real-money path was added.

---

## 8 November 2026 — implementation addendum

### What changed

| Area | Current result | Evidence |
|---|---|---|
| Operator paper workflow | **PASS (isolated operator fixture).** A real proposal submitted from the Paper page produced a partial fill of `12.0000` against `12.1457`, retained the fill through cancellation, and reconciled 1 order, 1 fill, 2 ledger transactions, 1 wallet and 1 position with 0 issues. | `.ui_audit/paper_states_ui.json`; no console errors |
| Cross-store execution durability | **PASS.** Durable in-flight fill journal, idempotent wallet keys and position-effect reversal; recovery runs before submit/process/cancel/reconcile. Adversarial recovery and reconciliation round-trip tests pass. | `tests/test_novamarket_execution.py`; 25 focused execution tests |
| Catalogue ingestion | **PASS for official EOD ingestion.** 169,997 bars imported from 37 NSE and 37 BSE sessions: 87,348 NSE + 82,649 BSE. **4,686 / 4,785** catalogue instruments now have bars; **4,535** have at least 30. | `.ui_audit/nse_import.sqlite3`; `.ui_audit/nse_import.log`; `.ui_audit/bse_import_1.log`; `.ui_audit/bse_import_2.log` |
| Sector provenance | **PASS for the packaged/dated reference.** Analysis, sector-flow and heatmap rows carry `sector_source` and `sector_as_of`; the UI displays the source and effective date. Unmatched symbols remain `Unclassified`. | `workbench.py`, `OrderBlock.tsx`, `TradingWatch.tsx`, `tests/test_market_screener.py` |
| Index options | **PASS for official EOD data.** The official NSE F&O bhavcopy supplied 6,625 index-option contracts across 6 underlyings. The live TradingWatch card shows NIFTY, 147 strikes, real OI/Δ OI/volume and **PCR 1.6528** for 6 November 2026. IV is a **model output** (Black-Scholes bisection on the official premium), not a provider-reported IV. | `scripts/import_nse_fo_bhavcopy.py`; `.ui_audit/derivatives_snapshot.json`; `.ui_audit/tw_live.json`; browser check |
| F&O futures scanner | **PARTIAL.** The index-option contract master is real, but stock/index futures and the full derivatives scanner were not imported. The OrderBlock card remains explicit about that narrower scope. | `/market/orderblock`; `fno` payload |
| Performance / security | **PASS for the focused suites, not a certification.** 92 security/optimization/performance tests passed (5 subtests). No load test, leak soak, WCAG audit or penetration test was performed. | `.ui_audit/final_security_perf.log` |
| Data → learning integration | **PASS.** `verify_novamarket_chain.py` exits 0 with proposal approved, order filled, backtest and shadow completed, evidence accepted and candidate promoted. | `.ui_audit/final_chain.log` |

### Verification actually run

```text
pytest (NovaMarket + market screener + bhavcopy + derivatives)
  769 passed, 2 skipped, 352 subtests passed in 175.14s
pytest tests/test_security.py tests/test_optimization.py tests/test_performance.py
  92 passed, 5 subtests passed in 6.09s
mypy src                          Success: no issues found in 555 source files
mypy src --platform win32         Success: no issues found in 555 source files
generate_api_reference.py --check docs/API.md: in sync
npm run build                     tsc -b && vite build succeeded (559.75 kB JS / 150.67 kB gzip)
verify_novamarket_chain.py        exit 0; order_status=filled; candidate promoted
```

The browser check loaded `#/tradingwatch` with no console errors. The Index
Options card rendered `AVAILABLE`, `NIFTY`, 147 strikes, PCR 1.65, real OI and
Δ OI values, and model IV values. The API response reported `supported=true`,
`status=AVAILABLE`, `rows=147`, `pcr=1.6528` and trade date `2026-11-06`.

### Remaining limitations (the reason the overall verdict is still PARTIAL)

1. **Workbench scan cap is still 250.** 4,535 instruments meet the 30-bar
   threshold, but the API desk still scans at most 250, so its breadth and
   sector-flow figures are a sample, not full-market coverage. The data is now
   present; the remaining work is a full-scan/pagination path and performance
   budget.
2. **Index-option IV is model-computed.** OI, prices and volumes are official
   NSE values; IV is not. A licensed IV feed would replace the model output.
3. **F&O futures scanner is incomplete.** Only index-option contracts were
   imported. Stock/index futures are not claimed.
4. **151 instruments still have fewer than 30 bars and 99 have none** (4,686
   with bars; 4,535 eligible). No bar was synthesized to close the gap.
5. **The full 25-route / 267-control matrix was not rerun end to end.** The
   targeted TradingWatch, Paper, Risk, sector and recovery checks are current;
   the earlier matrix remains the broad baseline.
6. **No formal load, soak-memory, WCAG or penetration certification.** The
   focused security/performance suites pass; that is not a production
   certification.
7. **No commit or push was made.** All changes remain in the working tree.

---

## A. Executive summary

### Initial condition

The mission started with five reported limitations:

1. No paper orders or fills existed in the operator database, and no approved
   proposal was available for a live operator walkthrough.
2. OrderBlock and TradingWatch analysed only **80 of 126** instruments with
   stored bars.
3. Discovery candidates scored **0.0** because the catalogue page contained
   instruments with no usable history.
4. Several provider-dependent surfaces exposed stale or unavailable data
   without complete provenance.
5. Reversal status/reference presentation had not been proven with a real
   reversal transaction.

The wider audit also found that the discovery candidate list did not expose
watchlist membership, so the Discovery page reported **0 already tracked**
although 54 active memberships existed.

### Work completed

- Raised the workbench scan ceiling from 80 to 250 and added explicit
  excluded-instrument reporting.
- Changed discovery's universe from an alphabetical catalogue page to the
  stored-bar inventory, preserving catalogue failures and pruning candidates
  whose data had gone away.
- Repaired candidate scoring inputs and confirmed 126 non-zero, reproducible
  scores.
- Added `on_watchlist` / `watchlist_ids` to candidate list and detail payloads;
  the live UI now renders **18 already tracked**.
- Exercised the complete isolated paper chain and a live wallet reversal.
- Ran the full regression, both mypy views, the API-reference check, the chain
  verifier and the live scanner/UI checks.
- Wrote this report and corrected the two superseded acceptance verdicts.

### Final condition

- **126 / 126** instruments with at least 30 stored daily bars are analysed by
  both workbenches: 0 skipped, 0 failed, 0 pending, `complete=true`,
  `sample_only=false`.
- Discovery has **126 candidates**, **0 zero-score candidates**, scores from
  **20.3 to 60.72**, quality `good`, and 197 retired candidates.
- The isolated paper chain exits 0 with an approved proposal and a **filled**
  paper order; backtest, shadow, evidence and learning all complete.
- A real reversal exists on the operator database, is visible in the
  Transactions UI, and a duplicate reversal returns **409**.
- **735 passed, 2 skipped, 352 subtests** in 170.23 s.
- **552 source files** pass mypy in both the normal and `--platform win32`
  views. `docs/API.md` is in sync. `git diff --check` is clean.
- The remaining blockers are genuine: no derivatives master/option chain and
  most of the catalogue has never been ingested. The control-level browser
  matrix was executed on 7 November 2026 (25/25 routes; 247 PASS, 12 PARTIAL,
  8 BLOCKED, 0 NOT PASS) and is recorded in `NM14_UI_CONTROL_MATRIX.md`; the
  remaining non-PASS rows are data-dependent or ambiguous controls. The overall
  status therefore remains **PARTIAL**, not PASS.

---

## B. The five original limitations

### B1. Complete paper-trading workflow

**Original root cause.** The paper engine and its lifecycle existed and were
unit-tested, but the operator database had no approved proposal, no order and no
fill. The acceptance chain was not yet a reproducible end-to-end run. The live
operator database also had weekend-stale data, which the risk engine correctly
refused.

**Repair / evidence.** `scripts/verify_novamarket_chain.py` now runs the whole
chain in an isolated store, using the real services rather than inserted rows.
Current output:

```json
{
  "ok": true,
  "instrument": "XNSE:ACCEPT",
  "bars": 300,
  "proposal_status": "approved",
  "order_status": "filled",
  "portfolio_id": "pf_04bbd719f14ae6425d8b",
  "wallet_id": "wal_107855d011404dbab6eb2cc472e0f5e0",
  "forecast": {"forecast_id": "fc_6ad075b9cd7c65e1da53", "horizon": "few_sessions"},
  "strategy_id": "strat_0fe62cc854680768",
  "backtest_status": "completed",
  "shadow_status": "completed",
  "evidence_status": "accepted",
  "experiment_status": "completed",
  "candidate_status": "promoted"
}
```

The execution suite has **21 tests** covering buy, sell, duplicate submission,
rejected proposals, stale data, limit/stop behaviour, partial and complete
fills, cancellation, expiry, unsupported types, risk fail-closed behaviour,
fees/slippage, overfill detection, reconciliation and multiple partial fills.
The wallet suite has **76 tests**, including reversal, double-reversal, reversal
of a reversal, overdraw refusal and reconciliation. The combined
execution/wallet group is **97 passed**.

**Honest boundary.** The operator database still has **0 orders, 0 fills and 0
proposals**. No approved proposal was manufactured on stale weekend data. The
paper order/fill proof is the isolated deterministic chain, not a live operator
click-through from proposal to fill. The backend lifecycle is proven; the
operator UI walkthrough remains **PARTIAL**.

### B2. OrderBlock and TradingWatch coverage

**Root cause.** The workbench had `DEFAULT_SCAN_LIMIT = 80`, so the first 80
eligible instruments were scanned and the remaining 46 were silently outside
the cap. Discovery had a second, independent defect: it selected an
alphabetical page of the 4,785-row catalogue, so data-less rows displaced
instruments that had bars.

**Repair.**

- `workbench.py`: scan cap **80 → 250**, and the coverage payload now reports
  `excluded` instruments explicitly.
- `discovery/engine.py`: prefer `market_data.bar_inventory(interval="1d",
  min_bars=30)`, fall back to the catalogue only when the inventory facade is
  unavailable, keep catalogue failures visible, and prune candidates that are
  no longer in the stored-bar universe.
- `discovery/store.py`: added `prune_candidates_except`.
- `discovery/models.py`: `CycleReport.pruned`.

**Before / after.**

| Measure | Before | After |
|---|---:|---:|
| Workbench analysed | 80 / 126 | **126 / 126** |
| Workbench skipped | 46 outside cap | **0** |
| `complete` | false | **true** |
| `sample_only` | true | **false** |
| Discovery candidates | many data-less rows | **126, all `good`** |
| Zero-score candidates | present | **0** |
| Discovery scores | 0.0 | **20.3–60.72** |

Live endpoints currently report:

```text
orderblock.coverage   = {catalogue_total: 4785, instruments_with_bars: 126,
                         scanned: 126, analysed: 126, skipped: 0,
                         excluded: [], minimum_closed_bars: 30,
                         scan_cap: 250, complete: true, sample_only: false}
tradingwatch.coverage = same 126 / 126 / 0 / complete=true / sample_only=false
```

The 46 previously omitted instruments were omitted by the cap, not by symbol
mapping or exchange filtering. The 24 remaining instruments with bars but fewer
than 30 are XBOM cross-listings with only one or two bars; they are correctly
ineligible for a scanner that requires 30 closed bars. The other 4,635
catalogue rows have no stored daily bars at all. Those facts are reported, not
hidden by changing the denominator.

### B3. Discovery scores of 0.0

**Root cause.** Discovery cycled through the alphabetical instrument catalogue.
The first page was populated by rows without bars, so the scoring engine
received no usable history and returned a zero-quality result. This was a data
selection defect, not a market signal.

**Repair.** The stored-bar inventory now defines the cycle universe. Candidate
records are pruned to that universe, and membership fields are computed on read.
No score is forced above zero.

**Acceptance evidence.**

- 126 evaluated, 126 candidates, **0 zero-score**.
- Score range **20.3–60.72**; top score **60.72**.
- Confidence **1.0**; quality **good**; no missing required fields.
- 197 stale candidates pruned by the cycle.
- 18 candidates are on a watchlist, representing 54 active memberships across
  four lists.
- Discovery UI now renders **126 candidates** and **18 already tracked**.

### B4. Market-data reliability

**What is now traceable.**

- Workbench payloads declare `data_state: "stored_ohlcv"`, `live: false` and a
  `price_basis` of the last closed bar.
- Every row carries the observation timestamp/freshness used by the scanner.
- The UI states the market session (NSE/BSE weekend, closed) and does not
  present stored bars as live quotes.
- The last daily observation was approximately **54.5 hours** old at inspection;
  the freshness factor was **0.2658**. That is a weekend/stale-data state, not a
  live price.
- Provider failure, stale data, unusable quality and reconciliation failure have
  explicit refusal paths in the execution tests.

**Not proven / not available.**

- There is no derivatives contract master, no option-chain source, no OI/IV/PCR
  store and no licensed derivatives feed. F&O and Index Options remain
  **BLOCKED**, not fabricated.
- 4,635 catalogue instruments have never been ingested; 24 more have fewer than
  30 bars.
- The report does not claim that every provider fault mode (timeout, malformed
  body, rate limit, partial response, outage) was manually replayed against the
  live network in this session. Provider and quality tests are present, but the
  full fault-injection matrix is not proven.

**Verdict:** **PARTIAL** for market-data reliability; the equity stored-bar
surface is truthful and complete for its defined universe, derivatives are
blocked by missing external data.

### B5. Reversal workflow and UI

**Live evidence.**

| Item | Value |
|---|---|
| Wallet | `wal_83b4040b2bd24bb3a1d4540cfc2e09fa` |
| Original deposit | `txn_f97e0e09e6e74aa68a27bdaaf3f9aac5` |
| Reversal | `txn_72575075493447dfa550400bd8a8c6bc` |
| Amount | ₹25.00 |
| Relationship | reversal points to the deposit via `reverses_transaction_id` |
| Duplicate reversal | HTTP **409** |
| Global ledger | 32 entries, signed sum **₹0.00** |
| Wallets reconciled | 16 |

The original transaction row remains physically `posted`; the read model derives
`status=reversed` from the existence of `reversed_by_transaction_id`
(`WalletTransaction.status`). This is intentional immutability, not a stale
status. The API and UI expose the derived status.

The Transactions UI was reloaded and showed the deposit as **Reversed**, the
reversal as **Posted**, and the reversed-by reference. The reversal is therefore
verified at the database, API and browser layers. **PASS** for the reversal
workflow.

---

## C. Tab-by-tab verification matrix

The 25 navigation surfaces all rendered. The evidence column below predates the
control-level sweep and is retained for the backend/API history; the authoritative
control results are in `NM14_UI_CONTROL_MATRIX.md`. That sweep drove every unique
visible control component and every form, and recorded the remaining failures.
A `PARTIAL` row below is not evidence that its controls were untested; use the
matrix for the current control verdict.

| Tab / route | Implemented controls inspected | Backend | API | UI | E2E | Status | Evidence |
|---|---|---|---|---|---|---|---|
| Dashboard `#/dashboard` | overview aggregates, allocation cards, activity | `/market/overview` tests | 200 | rendered | read-only | PARTIAL | route sweep; no control-by-control drive |
| Market Overview `#/market` | search, session state, instrument browser | market-data tests | 200 | rendered | read-only | PARTIAL | route sweep; no exhaustive filter drive |
| OrderBlock `#/orderblock` | bullish/bearish boards, filters, F&O status, detection, liquidity, X-Ray, sector flow, charts, detail | workbench/API tests | 200 | live | Full Scan; 126/126 | PARTIAL | 126 analysed, 0 skipped; all chart toggles not driven |
| TradingWatch `#/tradingwatch` | momentum, zones, heatmap, flow, options status, replay, watchlists | workbench/API tests | 200 | live | Full Scan; 126/126 | PARTIAL | 126 analysed; replay/heatmap controls not exhaustively driven |
| Market Events `#/events` | search, source filters, verification, evidence | radar tests | 200 | rendered | read-only | PARTIAL | route sweep; filters not driven |
| Asset Discovery `#/discovery` | cycle, search, candidate rows, watchlist metric, add modal | discovery tests | 200 | live | cycle + membership count | PARTIAL | 126 candidates, 18 tracked; search/modal not exhaustively driven |
| Watchlists `#/watchlists` | create, select, add/remove, reason, live refresh | discovery/watchlist tests | 200 | live membership data | read + counts | PARTIAL | four lists, 54 memberships; rename/delete not driven in browser |
| Forecasts `#/forecasts` | horizon filter, generate, interval detail | forecasting tests | 200 | rendered | read-only | PARTIAL | route sweep; generation modal not driven |
| Short-Term Portfolio `#/portfolios/short` | positions, valuation, proposals, decisions | portfolio tests | 200 | rendered | read-only | PARTIAL | route sweep |
| Medium-Term Portfolio `#/portfolios/medium` | same surface, medium horizon | portfolio tests | 200 | rendered | read-only | PARTIAL | route sweep |
| Long-Term Portfolio `#/portfolios/long` | same surface, long horizon | portfolio tests | 200 | rendered | read-only | PARTIAL | route sweep |
| Portfolio Analytics `#/analytics` | equity, allocation, performance | portfolio tests | 200 | rendered | read-only | PARTIAL | route sweep; chart interactions not driven |
| Strategies `#/strategies` | catalogue, signals, proposal generation | strategy tests | 200 | rendered | chain approval | PARTIAL | isolated chain reached approved proposal |
| Risk Management `#/risk` | policies, evaluation, alerts, stress | risk tests | 200 | rendered | chain validation | PARTIAL | chain passed risk; every control not driven |
| Paper Trading `#/paper` | orders, fills, cancel, expiry, reconciliation | 21 execution tests | 200 | rendered | chain order filled | PARTIAL | isolated fill proven; operator UI order not placed |
| Backtesting `#/backtesting` | configuration, run, metrics | backtesting tests | 200 | rendered | chain completed | PARTIAL | chain result `completed`; UI controls not driven |
| Shadow Mode `#/shadow` | shadow runs, guards, comparison | shadow tests | 200 | rendered | chain completed | PARTIAL | chain result `completed` |
| Adaptive Learning `#/learning` | experiments, promotion, safeguards | learning tests | 200 | rendered | chain promoted | PARTIAL | candidate `promoted`; UI controls not driven |
| NovaCoin Wallet `#/wallet` | balance, deposit, withdraw, status, reconcile | 76 wallet tests | 200 | live deposit/reversal | deposit + reversal | PARTIAL | live IDs above; withdraw/status UI not driven |
| Transactions `#/transactions` | filter, status, reversal chain | wallet API tests | 200 | live | reversal + 409 | **PASS** | deposit Reversed, reversal Posted, reference shown |
| Ledger `#/ledger` | entries, balances, reconciliation | reconciliation tests | 200 | rendered | 16 wallets reconciled | PARTIAL | ledger sums to ₹0.00; every filter not driven |
| Notifications `#/notifications` | unread/read, alert list | notification tests | 200 | rendered | read-only | PARTIAL | route sweep |
| Activity and Audit `#/activity` | event trail, filters | event/audit tests | 200 | rendered | read-only | PARTIAL | route sweep |
| System Health `#/health` | provider and subsystem health | health tests | 200 | rendered | read-only | PARTIAL | route sweep |
| Settings `#/settings` | lifecycle, preferences, safety text | settings tests | 200 | rendered | read-only | PARTIAL | route sweep; save paths not driven |

**Control-level sweep:** completed 7 November 2026 — 25/25 routes loaded,
267 results (247 PASS, 12 PARTIAL, 8 BLOCKED, 0 NOT PASS). Remaining non-PASS
rows are listed with evidence in `NM14_UI_CONTROL_MATRIX.md`: six no-op Refresh
controls, data-dependent forms (paper submit, API key), single-option/filter
ambiguities, and controls that disappear after activation. Repeated row controls
are grouped with an instance count; not every repeated instance was clicked.

---

## D. Canonical roadmap matrix

### NM-01 through NM-14

| Phase | Objective | Implementation / verification | Status | Evidence |
|---|---|---|---|---|
| NM-01 | Architecture, contracts and integration | service, lifecycle, permissions, events, storage boundaries; `test_novamarket.py` | PASS | 735-test suite; API contract tests |
| NM-02 | Market data reliability | append-only bars, providers, freshness, quality, ingestion; market-data tests | PASS | validated daily bars; stale-data refusals |
| NM-03 | NovaCoin wallet and deterministic ledger | wallet engine/store; 76 tests | PASS | live reversal; ledger ₹0.00 |
| NM-04 | Evidence-backed market event radar | radar store/service; event tests | PASS | API and route render |
| NM-05 | Autonomous asset discovery and watchlists | discovery engine/store; 51 discovery tests | PASS | 126 candidates, 0 zero-score, 18 tracked |
| NM-06 | Measured probabilistic forecasting | forecast service/models; forecasting tests | PASS | isolated chain forecast stored |
| NM-07 | Three-horizon portfolio manager | portfolio engine/models; portfolio tests | PASS | chain portfolio created |
| NM-08 | Independent risk engine | risk policies/engine; risk tests | PASS | chain risk-approved; stale data refused |
| NM-09 | Trading strategies | strategy catalogue/signals; strategy tests | PASS | chain proposal approved |
| NM-10 | Realistic paper execution | 21 execution tests; paper-only guards | PASS | chain `order_status=filled` |
| NM-11 | Reproducible backtesting | backtest engine; backtesting tests | PASS | chain `backtest_status=completed` |
| NM-12 | Shadow mode | shadow service/guards; shadow tests | PASS | chain `shadow_status=completed` |
| NM-13 | Controlled adaptive learning | learning service; learning tests | PASS | chain candidate `promoted` |
| NM-14 | Integrated workspace and final acceptance | workspace/overview; 25-route render | **PARTIAL** | 25/25 routes load; 247 PASS / 12 PARTIAL / 8 BLOCKED / 0 NOT PASS control matrix; derivatives coverage still missing |

### OrderBlock / TradingWatch / Integration expansion

No canonical numbered `OB-01…`, `TW-01…` or `INT-01…` documents exist in the
repository. To avoid inventing acceptance artifacts, the requested identifiers
are mapped below to the implemented feature set and the evidence that actually
exists. This mapping is explicit, not a claim that numbered phase documents
were found.

| Item | Mapped objective | Verification | Status | Evidence |
|---|---|---|---|---|
| OB-01 | Instrument universe and coverage | workbench/API tests | PASS | 126/126, 0 skipped, complete true |
| OB-02 | Bullish and bearish boards | workbench tests; live render | PARTIAL | rows rendered; every filter/sort not driven |
| OB-03 | Order-block detection and evidence | workbench tests | PARTIAL | backend invariants; no exhaustive manual zone audit |
| OB-04 | Liquidity, X-Ray and sector flow | workbench tests; live render | PARTIAL | no exhaustive chart/control drive |
| OB-05 | F&O scanner | no contract master/provider | **BLOCKED** | explicit `supported:false`, `status:"BLOCKED"` |
| OB-06 | Interactive charts and instrument detail | live chart render | PARTIAL | chart values backend-owned; all toggles not driven |
| OB-07 | Honest coverage/performance reporting | API + tests | PASS | coverage object and 250 cap |
| TW-01 | Momentum radar and ranking | workbench tests; live 126/126 | PASS | populated radar; scores backend-owned |
| TW-02 | Zones, heatmap, flow, index options | workbench tests; live render | PARTIAL | heatmap/flow render; options **BLOCKED** |
| TW-03 | Replay and watchlists | replay tests; live membership | PARTIAL | look-ahead guard exists; full replay controls not driven |
| INT-01 | Shared instrument/watchlist integration | discovery/API tests | PASS | canonical ids; 54 memberships |
| INT-02 | UI integration and performance | route sweep; measured scans | PARTIAL | 25 routes render; exhaustive interactions absent |

---

## E. Market-data coverage report

| Measure | Value | Explanation |
|---|---:|---|
| Catalogue instruments | 4,785 | 2,393 NSE + 2,392 BSE; all `equity` |
| Instruments with any daily series | 150 | stored OHLCV only |
| Instruments with ≥30 closed daily bars | 126 | defined scanner universe |
| Workbench scanned | 126 | cap 250 did not truncate |
| Workbench analysed | 126 | complete |
| Skipped | 0 | none outside cap |
| Failed | 0 | no scanner exception |
| Pending | 0 | no queued work |
| Excluded | 4,659 | 4,635 no bars + 24 short history |
| Coverage of defined universe | **100%** | 126 / 126 |
| Coverage of catalogue | **2.63%** | 126 / 4,785; not presented as full-market coverage |
| F&O contracts | 0 | **BLOCKED** |
| Index-option chains | 0 | **BLOCKED** |
| Last daily observation age | ~54.5 h | weekend/stale, not live |

**The 24 short-history instruments:** `ASIANPAINT`, `BAJFINANCE`, `BEL`,
`CIPLA`, `DRREDDY`, `GRASIM`, `HDFCBANK`, `HINDALCO`, `HINDUNILVR`,
`ICICIBANK`, `INFY`, `ITC`, `KOTAKBANK`, `LT`, `M&M`, `NESTLEIND`, `ONGC`,
`SBIN`, `SUNPHARMA`, `TATASTEEL`, `TECHM`, `TITAN`, `TRENT` (all one bar),
and `RELIANCE` (two bars). All are `XBOM` cross-listings and all fail the
30-closed-bar minimum.

**Reasons for every exclusion category:**

- **4,635:** catalogue row has no stored daily OHLCV series. No synthetic bar
  was created.
- **24:** stored series exists but has 1–2 bars, below the 30-bar warm-up.
- **F&O/options:** no contract master, option chain, OI/IV/volume store or
  licensed derivatives feed. The surfaces report `BLOCKED`; they do not display
  invented strikes or Greeks.

**Discovery:** 126 evaluated, 126 persisted candidates, 0 zero-score, 197
pruned, top 60.72, quality `good`, confidence 1.0. The workbench and discovery
now use the same stored-bar reality; neither claims the full catalogue.

---

## F. Paper-trading execution report

### Isolated acceptance chain (test fixture, not the operator portfolio)

| Stage | Result |
|---|---|
| Market sessions | 300 bars |
| Forecast | `fc_6ad075b9cd7c65e1da53`, `few_sessions` |
| Portfolio | `pf_04bbd719f14ae6425d8b` |
| Strategy | `strat_0fe62cc854680768` |
| Proposal | **approved** |
| Paper order | **filled** |
| Backtest | **completed** |
| Shadow | **completed** |
| Evidence | **accepted** |
| Learning experiment | **completed** |
| Candidate | **promoted** |
| Script exit | **0** |

The chain uses the application services and isolated storage. It is not proof
that a human clicked Approve in the operator UI.

### Scenario coverage

The execution test file contains 21 tests. It covers the required lifecycle
families: successful buy and sell, duplicate submission replay, rejected
proposal, stale data refusal, limit/stop behaviour, partial fill, multiple
partial fills, complete fill, cancellation, expiry, unsupported order types,
risk fail-closed, fee/slippage audit, overfill detection and reconciliation.
The wallet file contains 76 tests covering deposit/withdraw, transfer,
reversal, reversal refusal, double reversal, reversal of a reversal, ledger
posting and reconciliation.

These tests establish the backend scenarios. The browser was not used to place
an order on the operator database; that remains a **PARTIAL** item rather than a
fabricated success.

### Live operator database

At inspection: **0 execution orders, 0 fills, 0 portfolio proposals**, 16
wallets, 16 transactions and 32 ledger entries. The absence of an order is
honest: the market was in a weekend/stale state and no approved proposal was
manufactured. The isolated chain is the order/fill proof.

---

## G. Bugs fixed

| Defect | Root cause | Repair | Regression evidence |
|---|---|---|---|
| Workbench only 80/126 | hard cap `DEFAULT_SCAN_LIMIT=80` | cap 250; `excluded` in coverage | workbench/API tests; 126/126 live |
| 46 instruments silently absent | cap treated as complete | explicit `complete`/`sample_only`/`excluded` | `orderblock.coverage` live |
| Discovery selected data-less symbols | alphabetical catalogue page | stored-bar inventory selection | discovery tests; 126 non-zero |
| Discovery scored 0.0 | no bars reached the scorer | inventory universe + quality gate | 0 zero-score live |
| Stale candidates survived | no retirement policy | `prune_candidates_except`; `CycleReport.pruned` | discovery prune test |
| Discovery showed 0 tracked | candidate payload lacked membership | `on_watchlist` and `watchlist_ids` on list/detail | 18 tracked UI; 51 discovery tests |
| Coverage note claimed “sample” after cap fix | documentation drift | workbench doc now states 126/126 stored-bar universe vs 4,785 catalogue | doc review |
| Prior frontend crashes/fabricated zero values | earlier repair round | retained; source and production build unchanged this cycle | 735 suite + prior 25-route sweep |

The candidate membership regression test was strengthened in the existing
discovery test (no new test count): list and detail must both expose the same
membership ids.

---

## H. Performance report

| Measurement | Result |
|---|---|
| Full NovaMarket suite | **170.23 s**, 735 passed, 2 skipped |
| Discovery suite | 51 passed in 0.70 s |
| OrderBlock live scan | 126 instruments in **~2.75–3.12 s** |
| TradingWatch live scan | 126 instruments in **~2.75–3.12 s** |
| Discovery cycle | 126 evaluations, ~0.8 s in the measured run |
| Previous 80-instrument scan | ~2.1 s (not a like-for-like benchmark after universe change) |
| Production frontend build | prior run: 1,598 modules, JS 554.91 kB |

**Not established:** a production load test, memory profile, long-running leak
test, or a before/after benchmark under controlled concurrency. The candidate
list read model performs two small lookups per row (latest evaluation and
memberships) for 126 rows; this is a small N+1 pattern, not observed as a
user-facing bottleneck, but it is a known optimization opportunity.

---

## I. Remaining limitations and blockers

1. **F&O futures scanner — PARTIAL.** Index options are now available from the
   official NSE F&O bhavcopy (6,625 contracts, 147 NIFTY strikes); stock/index
   futures were not imported. The API keeps the narrower F&O card explicit.
2. **Index Options — PASS for official EOD data.** Strikes, OI, change in OI,
   volume, PCR and expiry are provider values. IV is a Black-Scholes model
   output, not a provider-reported IV; a licensed IV feed would replace it.
3. **Workbench coverage is still capped at 250.** 4,686 of 4,785 instruments
   have bars and 4,535 have at least 30, but the API desk still analyses at most
   250. Data ingestion is largely complete; full-market scanning is not.
4. **151 short-history instruments and 99 with no bars.** They fail the
   30-bar minimum or have no stored series and are reported as excluded. No
   bar was synthesized.
5. **Operator-UI paper order not demonstrated.** The isolated chain proves the
   engine and API workflow; no order was placed from the Paper Trading page on
   the operator database.
6. **Exhaustive control matrix incomplete.** All 25 routes render, but not every
   filter, chart toggle, date picker, modal, validation path and pagination
   control was operated. The tab matrix marks these PARTIAL.
7. **Derivatives/options UI is now a working EOD surface.** The TradingWatch
   card renders the official NSE chain; it is not a live intraday option tick
   feed and its IV is model-computed.
8. **Sector metadata.** The registry lacks sector metadata, so heatmap and
   sector-flow groups can be `Unclassified`.
9. **Stale data.** The equity surface is stored-bar based. It correctly shows
   `live:false` and freshness; it does not substitute a live quote.
10. **No production load/memory/security certification.** No load test, leak
    test, WCAG certification or penetration test was performed.
11. **Small read-model N+1.** Candidate list membership/evaluation lookups are
    per row. 126 rows are fast, but a batch query would be better at scale.
12. **Cross-store atomicity caveat.** The prior NM-10 design note’s caveat about
    execution/wallet writes across separate stores was not re-tested in this
    cycle; it remains a design-level limitation, not a claimed pass.

These are not hidden or reclassified. They are the reason the overall verdict
is **PARTIAL**.

---

## I.b Final closure sprint — 7 November 2026

This addendum records work performed after the baseline above. The earlier
evidence and its limitations are retained; this section does not relabel an
unverified area as complete.

### Repairs

- Added the missing Watchlists **Rename** control. It posts to the existing
  `POST /market/watchlists/{watchlist_id}/rename` endpoint, refreshes the list
  and detail, and leaves the stable watchlist id and memberships intact.
- Added a workbench sector fallback. When an instrument's own metadata has no
  sector, the OrderBlock/TradingWatch analysis now uses the same dated NSE
  constituent snapshot already used by Market Overview, matched by exact
  symbol. Explicit instrument metadata still wins; a missing match stays
  unclassified. No sector is inferred from a name or a neighbouring row.
- Added regression tests for the packaged-reference fallback and metadata
  precedence in `tests/test_market_screener.py`.

### Browser verification

| Control / flow | Result | Evidence |
|---|---|---|
| Watchlists → Rename | **PASS** | The control opened the rename modal, saved `UI closure renamed`, closed the modal, raised the success toast, and the new name was visible in the list. Evidence: `.ui_audit/closure_ui.json`. |
| Market Overview → Industry | **PASS** | Selecting `Automobile and Auto Components` produced a `/market/screener` request with `sector=Automobile and Auto Components`, the control retained that value, and the API matched 98 instruments across 50 returned rows. Reset returned the control to `all`. Evidence: `.ui_audit/closure_ui.json`. |
| Paper → Submit from proposal | **PASS in isolated fixture** | The real form submitted the approved `sprop_a171b556d2ce0a1e` proposal. The engine returned a **filled** order `ord_f929162ebdcd11956064fccb` for 2.0243 units, with one fill, fees, two ledger transaction ids, and a position effect. The Orders table rendered the order with state `Filled`. Evidence: `.ui_audit/paper_ui.json`. |
| Paper → Reconciliation | **PASS in isolated fixture** | `Reconcile now` returned HTTP 200 with zero issues; checks covered 1 order, 1 fill, 2 ledger transactions, 1 wallet and 1 position. Evidence: `.ui_audit/paper_ui.json`. |
| Paper-only guard | **PASS** | The same execution status reported `paper_only=true`, `live_trading=false` and `broker_connected=false`; the order response is simulated paper execution only. |
| Sector flow / heatmap source | **PASS for packaged reference** | After restarting the patched API, `/market/tradingwatch` returned real groups including Financial Services (30), Healthcare (12), Capital Goods (9), Oil Gas & Consumable Fuels (8); 125 of 126 stored-bar instruments were classified and 1 remained unclassified. |

### Verification run

```bash
cd novamarket
PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest tests/test_novamarket*.py tests/test_market_screener.py -q
# 748 passed, 2 skipped, 352 subtests passed in 265.49s

PYTHONPATH=src ../.venv/Scripts/python.exe -m mypy src
# Success: no issues found in 552 source files

PYTHONPATH=src ../.venv/Scripts/python.exe -m mypy src --platform win32
# Success: no issues found in 552 source files

PYTHONPATH=src ../.venv/Scripts/python.exe scripts/generate_api_reference.py --check
# docs/API.md: in sync

cd web && npm run build
# tsc -b && vite build succeeded; 1598 modules transformed

cd ..
PYTHONPATH=src ../.venv/Scripts/python.exe scripts/verify_novamarket_chain.py
# exit 0; proposal approved, order filled, backtest/shadow completed,
# evidence accepted, candidate promoted
```

The production build retains the existing chunk-size warning
(556.08 kB JavaScript, 149.63 kB gzip); it is a warning, not a failed build.

### Remaining limitations after this sprint

- The full 25-route / 267-control matrix was **not rerun** end to end. The
  previous matrix remains the broad baseline; this sprint added targeted browser
  evidence for rename, the Market industry filter, the Paper order, and
  reconciliation. Other routes and controls retain their earlier PARTIAL or
  BLOCKED rows.
- **Catalogue ingestion remains incomplete.** The isolated audit database reports
  4,789 catalogue instruments, 150 quoted listings and 4,639 listings without a
  stored quote. No bulk ingestion of the missing catalogue was performed, so
  the 126-instrument stored-bar universe is not full-catalogue coverage.
- **F&O and Index Options remain BLOCKED.** No licensed derivatives contract
  master, option-chain feed, open-interest/IV store or approved credentials were
  supplied. The UI correctly reports the missing contract, not fabricated rows.
- **Provider fault injection, cross-store recovery, and formal performance or
  security testing were not newly executed in this sprint.** The full backend
  suite passes, but that is not a load test, leak test, penetration test or
  transactional proof. Those claims are not made here.
- The paper walkthrough used deterministic fixture bars, not live market data.
  It proves the UI, approval gate, execution, ledger and reconciliation wiring;
  it does not prove a live broker connection, and no broker connection exists.

---

## J. Final verdict

| Area | Status |
|---|---|
| Paper-only boundary | **PASS** — no broker client, no live-order path, `paper_only=True`, `live_trading=False`, `broker_connected=False` |
| Paper engine / isolated lifecycle | **PASS** — chain order filled; 21 execution tests |
| Operator paper order click-through | **PASS (isolated fixture)** — actual Paper form submitted an approved proposal; order filled and reconciled; operator database unchanged |
| Reversal / ledger / duplicate refusal | **PASS** — live reversal, 409 duplicate, ₹0.00 ledger |
| OrderBlock/TradingWatch coverage | **PASS** for 126/126 defined stored-bar universe |
| Full catalogue coverage | **PARTIAL** — 4,686 / 4,785 instruments have bars; 4,535 meet 30 bars; the workbench still scans at most 250 |
| Discovery scoring | **PASS** — 126 non-zero, reproducible |
| Equity market-data honesty | **PASS** — stored-bar timestamps, stale/live labels |
| Sector metadata | **PASS for packaged reference; PARTIAL for catalogue** — dated NSE snapshot used by Market Overview and as the workbench fallback; 125/126 stored-bar instruments classified; no guessed sectors |
| Derivatives market data | **PARTIAL** — official NSE index-option chain (6,625 contracts) is live; futures and provider IV remain unavailable |
| Tab and control verification | **PARTIAL** — prior 25-route matrix retained; targeted closure controls PASS, full matrix not rerun |
| NM-01–NM-13 backend objectives | **PASS** — executed suite/chain |
| NM-14 final integration | **PARTIAL** — workspace works; acceptance breadth incomplete |
| Regression | **PASS** — 769 passed, 2 skipped, 352 subtests in 175.14 s; 92 focused security/performance tests |
| Type/API/docs checks | **PASS** — mypy 555 both views; API reference in sync; production frontend build succeeds |

**Overall: PARTIAL.** The five reported limitations were either repaired or
bounded honestly: coverage and scoring are complete for the stored-bar universe,
the reversal is proven live, and the paper engine is proven in isolation. The
remaining gaps are external data (derivatives, catalogue ingestion) and
verification breadth (exhaustive control-level browser interaction), not hidden
zeroes or fabricated success.

---

## K. Files changed in the repair cycle

**Implementation**

- `web/src/pages/Intelligence.tsx` (Watchlists rename control)
- `src/novacontrol/novamarket/workbench.py` (packaged-sector fallback)
- `src/novacontrol/novamarket/discovery/engine.py`
- `src/novacontrol/novamarket/discovery/store.py`
- `src/novacontrol/novamarket/discovery/models.py`

**Tests**

- `tests/test_market_screener.py` (workbench sector fallback/precedence)
- `tests/test_novamarket_discovery.py` (bar-inventory/prune regressions;
  candidate list/detail membership assertions)

**Browser evidence**

- `.ui_audit/closure_ui.json`
- `.ui_audit/paper_ui.json`
- `.ui_audit/paper_fixture.json`

**Documentation**

- `docs/NM14_FINAL_ACCEPTANCE_REPORT.md` (this report)
- `docs/NM14_ACCEPTANCE.md` (verdict/count corrections)
- `docs/NOVAMARKET_STANDALONE_REPORT.md` (verdict/count corrections)
- `docs/NOVAMARKET_WORKBENCH.md` (coverage wording and limitations)

No NovaControl or NovaForge source was modified. No broker or live-trading
credential was added. No commit was made.

### Commands and actual results

```bash
cd novamarket
PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest tests/test_novamarket*.py -q
# 735 passed, 2 skipped, 352 subtests passed in 170.23s

PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest tests/test_novamarket_discovery.py -q
# 51 passed in 0.70s

PYTHONPATH=src ../.venv/Scripts/python.exe -m mypy src
# Success: no issues found in 552 source files

PYTHONPATH=src ../.venv/Scripts/python.exe -m mypy src --platform win32
# Success: no issues found in 552 source files

PYTHONPATH=src ../.venv/Scripts/python.exe scripts/generate_api_reference.py --check
# docs/API.md: in sync

PYTHONPATH=src ../.venv/Scripts/python.exe scripts/verify_novamarket_chain.py
# exit 0; proposal approved, order filled, backtest/shadow completed,
# evidence accepted, candidate promoted

git diff --check
# clean (only CRLF advisory, no whitespace errors)
```
