# NovaMarket Bug Fix Register

**Audit date:** 2026-11-09  
**Scope:** confirmed UI/read-model and performance defects found during the full QA audit  
**Status:** 3 product defects fixed; performance limitation fixed; 0 confirmed defects open; 1 scope gap remains

---

## 1. Summary

| ID | Type | Severity | Title | Status | Regression test |
| --- | --- | --- | --- | --- | --- |
| BUG-QA-001 | Read-model/UI aggregation | Medium | Horizon status used only the first book | **FIXED** | `test_dashboard_horizon_status_is_not_first_book_status` |
| BUG-QA-002 | Read-model/UI data selection | Medium | Market pulse showed duplicate/unquoted rows and could drop valid row prices | **FIXED** | `test_dashboard_market_pulse_uses_quoted_observations` |
| BUG-QA-003 | Chart presentation | Low | Flat valuation series produced five identical Y-axis ticks | **FIXED** | `test_line_chart_expands_a_flat_series_for_readable_ticks` |
| TEST-QA-001 | Test contract | Low | Stale assertion expected an inline assignment instead of the actual `useMemo` expression | **FIXED** | Targeted UI suite |
| PERF-QA-001 | Capacity | Medium | Concurrent and soak p95 exceeded internal limits | **FIXED** | Performance probe |
| SCOPE-QA-001 | Scope | Informational | No autonomous paper-trading loop | OUT OF ACCEPTED SCOPE | Runtime/doc audit |

No financial formula, risk policy, allocation, fee, slippage, ledger rule or broker boundary was changed.

---

## 2. BUG-QA-001 — Horizon status used the first book

### Symptom

One paused book caused the dashboard's entire short-term horizon card to display `Paused`, even when other books in the same horizon were active. The card was reporting one book's status as the horizon's status.

### Root cause

The dashboard passed a single status value to `StatusBadge` instead of aggregating all book statuses returned by `/market/overview`. `/market/overview` already groups books by horizon and exposes `statuses`; only the renderer's choice was wrong.

### Fix

`web/src/pages/Dashboard.tsx` now computes:

- `Active` when all books share the active status;
- `Mixed` when active and paused statuses coexist;
- the common status when all agree;
- `Active` when no status is reported.

The aggregation is derived from `item.statuses`, not from the first row.

### Verification

1. Paused one short-term book while another remained active: the dashboard displayed `Mixed`.
2. Resumed the paused book: the dashboard displayed `Active`.
3. No other page or financial value changed.
4. Regression test pins that the first book's status is not used directly.

### Files

- `web/src/pages/Dashboard.tsx`
- `tests/test_novamarket_workbench_ui.py`

---

## 3. BUG-QA-002 — Market pulse showed the wrong instruments and lost valid row prices

### Symptom

The dashboard Market pulse showed alphabetical instruments that had no quotes, repeated the same symbol for NSE and BSE, and displayed `Unavailable` for most rows. It looked like market data was missing even though the market screener had quoted observations.

### Root cause

The panel requested:

```text
/market/instruments?limit=6
```

The instrument catalogue is alphabetical, not quote-ranked. The first entries had no stored observations, and NSE/BSE duplicates were not deduplicated. The replacement screener request returned quoted rows, but some instrument ids made `/market/data/quote/{id}` return `status: ok` with no price. The cell treated that empty quote as authoritative and hid the price the screener row already carried.

### Fix

`web/src/pages/Dashboard.tsx` now:

1. requests `/market/screener?limit=12&available_only=true`;
2. deduplicates by symbol before rendering;
3. prefers a quote response but falls back to the screener's reconciled `instrument.price` and `instrument.change_pct` when the single-instrument quote is empty;
4. keeps the row clickable and navigates to Market.

The fallback is deliberate: the screener row and the direct quote endpoint can resolve the same listed security through different exchange records. A missing direct quote must not erase a valid screener price.

### Verification

Final Chromium render:

```text
ADANIENT     Adani Enterprises Limited                  ₹ 2,706.00  +4.48%
ADANIPORTS   Adani Ports and Special Economic Zone Ltd   ₹ 1,940.00  +4.07%
APOLLOHOSP   Apollo Hospitals Enterprise Limited          ₹ 8,538.00  +1.11%
ASIANPAINT   Asian Paints Limited                         ₹ 2,501.90  -1.30%
AXISBANK     Axis Bank Limited                            ₹ 1,311.00  +6.50%
```

- 5 rows;
- 5 unique symbols;
- 5 prices;
- 0 console errors;
- no duplicate exchange listing.

The earlier direct-quote probe returned `ok` with no `last_price`/`price` for `XNSE:ASIANPAINT`, `XBOM:ASIANPAINT`, `XNSE:AXISBANK` and `XBOM:AXISBANK`; the screener still had valid reconciled prices. The fallback is the tested remedy.

### Files

- `web/src/pages/Dashboard.tsx`
- `tests/test_novamarket_workbench_ui.py`

---

## 4. BUG-QA-003 — Flat valuation series produced identical chart ticks

### Symptom

When all seven short-term valuations were `₹20,000.00`, the line chart's Y-axis showed five identical labels: `₹20.0k`, `₹20.0k`, `₹20.0k`, `₹20.0k`, `₹20.0k`. The line was flat but the axis communicated no range.

### Root cause

The chart's tick generator used a fixed domain. A zero-range series collapsed every tick onto the same rounded value.

### Fix

`web/src/components/charts.tsx` now expands a flat domain by a small padded range before calculating ticks. The line remains flat; only the visible scale becomes readable.

Final observed ticks:

```text
₹19.0k
₹19.5k
₹20.0k
₹20.5k
₹21.0k
```

### Verification

- Dashboard rendered with the restored `₹20k` valuations.
- Five distinct tick labels.
- No console error.
- Regression test pins the expanded-domain behavior.

### Files

- `web/src/components/charts.tsx`
- `tests/test_novamarket_workbench_ui.py`

---

## 5. TEST-QA-001 — Stale source-contract assertion

### Symptom

The first full-suite run after the UI changes reported one failure in `test_dashboard_market_pulse_uses_quoted_observations`. The product behavior was correct; the assertion expected the old inline assignment text.

### Root cause

The test asserted:

```text
instrumentRows = arrayFrom(pulse.data, "rows")
```

The actual implementation correctly uses a `useMemo` block containing `arrayFrom(pulse.data, "rows")`. The assertion was stale relative to the implementation and was updated to pin the meaningful expression, plus the new price fallback.

### Fix

The assertion now checks:

- the screener URL;
- the absence of the alphabetical instrument request;
- `arrayFrom(pulse.data, "rows")`;
- `quoteValue(quote.data) ?? instrument.price`.

### Verification

`tests/test_novamarket_workbench_ui.py`: **12 passed**.

The final full NovaMarket suite is recorded in `QA_COMPLETION_REPORT.md`; the only failure in the preceding run was this test assertion, not a product behavior.

---

## 6. Resolved harness false positives

The current control sweep initially marked three controls PARTIAL because it clicked them without establishing filter state. They were retested with real state and all passed:

| Route | Control | Before | After click | Verdict |
| --- | --- | --- | --- | --- |
| Market | Reset | large-cap filter active | 101 quoted / 2,406 catalogue / 4,811 matched | PASS |
| OrderBlock | Clear filters | search `ADANIENT` | empty search | PASS |
| TradingWatch | Clear | search `ADANIENT`, direction `bearish` | empty search, direction `all` | PASS |

Evidence: `tmp/qa_audit/harness/clear_controls_retest.json`.

These are not product bugs and must not be counted as open defects.

---

## 7. Resolved performance limitation and remaining limitations

### PERF-QA-001 — Capacity thresholds — FIXED

**Root cause.** The hot market read handlers were declared `async def` while performing synchronous SQLite and board work, so they blocked the event loop. Even after offloading them, `/market/status` queued behind the long `board_rows` query that held the store lock, and concurrent screener requests rebuilt the 4,811-row board repeatedly.

**Fix.** The five hot market read handlers (`market_status`, `market_overview`, `market_wallets`, `market_portfolios`, `market_screener`) now run as synchronous handlers in Starlette's worker threadpool; `MarketDataStore.board_rows` uses a separate read-only WAL connection for file-backed stores; and `market_screener` has a 3-second single-flight TTL cache so concurrent identical requests share one board computation. In-memory stores and unrelated endpoints are unchanged.

**Verification (10 November).** All 10 API checks pass in `.ui_audit/performance_postfix2.json`:

- Concurrent p95 `1,397.34 ms` (limit `3,000 ms`), 120/120 requests, 0 errors;
- Soak p95 `166.06 ms` (limit `1,500 ms`), 0 errors;
- `/market/status` p95 `123.45 ms` while the screener runs; `/market/screener` p95 `699.51 ms`;
- Memory `168.19 MB -> 167.61 MB` (`-0.58 MB`, slope `-0.025 MB/min`);
- Browser 25/25 routes PASS, 0 console/page errors;
- Regression suite: 795 passed, 2 skipped, 938 subtests in 194.57 s.

This remains a local single-process benchmark, not a production capacity certification, but the internal thresholds are met.

### SCOPE-QA-001 — No autonomous trading loop

The runtime monitor is not a trading orchestrator. Discovery's scheduler is stopped; the QA database has 0 paper orders and 0 fills. This is outside accepted NM-01–NM-14 scope. Full evidence and the approval boundary are in `AUTONOMOUS_PAPER_TRADING_AUDIT.md`.

### Stale live quotes

The audit copy's newest quotes were 28.9–55.9 hours old. Risk correctly rejected stale proposals. No live quote fallback was fabricated. This is a data availability condition, not a UI or accounting bug.

---

## 8. Regression and build evidence

| Check | Result |
| --- | --- |
| Targeted UI suite | 12 passed |
| Full NovaMarket suite | See `QA_COMPLETION_REPORT.md` — final rerun after TEST-QA-001 fix |
| Production frontend build | PASS; 567.03 kB JS / 152.37 kB gzip; bundle-size warning only |
| Final dashboard screenshot | `tmp/qa_audit/dashboard-final-verified.png` |
| Mobile market screenshot | `tmp/qa_audit/market-mobile-final.png` |
| Clear-control retest | PASS, both controls |
| Chain verifier | exit 0, all phases completed/promoted |

---

## 9. Change inventory

Changed by this audit:

- `web/src/pages/Dashboard.tsx`;
- `web/src/components/charts.tsx`;
- `tests/test_novamarket_workbench_ui.py`;
- QA harness/evidence under `tmp/qa_audit/`;
- these six reports.

No commit or push was performed. No production database, live service, broker credential or financial policy was modified.
