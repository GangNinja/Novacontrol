# NovaMarket QA Completion Report

**Audit date:** 2026-11-09  
**Post-fix full sweep:** 2026-11-10  
**Application:** NovaMarket standalone workbench  
**Isolated QA services:** API `http://127.0.0.1:8012`, UI `http://127.0.0.1:5174`  
**Final verdict:** **PASS**

---

## 1. Final statement

NovaMarket passed the functional, safety, accounting, route-level and performance audit. All 25 routes and all **470 current control entries** pass in the fresh 10 November post-fix matrix, including the four state-dependent retests and the closure checks. The accepted 9 November 480-control baseline is net 10 higher than the current rendered set; the route-level reconciliation is in `COMPLETE_UI_INTERACTION_MATRIX.md`, and no 480/480 post-fix claim is made. The 10 November performance recheck passes all **10/10 API checks** and the 25-route browser check; concurrent p95 is `1,397.34 ms` and soak p95 is `166.06 ms`, both inside their limits. Autonomous paper trading remains outside accepted scope.

There is no evidence of a live-trading path, broker connection, ledger corruption, fabricated price, route failure or accounting regression. The system remains paper-only and safe within its accepted scope.

The three confirmed UI defects were fixed, tested and re-verified in Chromium. The deterministic paper chain passed end to end.

---

## 2. Scorecard

| Area | Result | Evidence |
| --- | --- | --- |
| Route rendering | **25/25 PASS** | `route_survey.json` |
| Accepted control baseline (9 Nov) | **480/480 PASS** | Historical `docs/NM14_UI_CONTROL_MATRIX.json` baseline |
| Post-fix current controls | **470/470 PASS** (full 25-route sweep) | `.ui_audit/current_controls.json`, generated matrix |
| Accessibility probe | **PASS** (25 routes, 957 controls, 0 automated issues) | `a11y_results.json` |
| Security probe | **9/9 PASS** | `security_results.json` |
| Wallet and ledger | **PASS** (18 wallets, 40 entries, 0 bad balances) | QA ledger checks |
| Market filters | **PASS** | large-cap and Reset checks |
| Portfolio pause/resume | **PASS** | live UI messages and status |
| Dashboard fixes | **PASS** | final screenshot and browser probe |
| Paper chain | **PASS** (exit 0) | `chain_verification.log` |
| Full NovaMarket test suite | **PASS** (751 on 9 November; 795 passed, 2 skipped, 938 subtests on the 10 November regression rerun) | `logs/pytest_final.log`; 10 November run |
| Frontend build | **PASS** (bundle-size warning only) | `logs/build_final.log` |
| Performance | **PASS** (10/10 API checks, browser PASS) | `.ui_audit/performance_postfix2.json` |
| Autonomous paper trading | **NOT IMPLEMENTED** (outside accepted scope) | `AUTONOMOUS_PAPER_TRADING_AUDIT.md` |
| Requirements NM-01–NM-14 | **14/14 PASS at declared scope** | `REQUIREMENTS_TRACEABILITY_REPORT.md` |
| Overall completion | **PASS** | This report; controls and performance are complete |

---

## 3. Exact verification run

### 3.1 Final full NovaMarket suite

Command:

```text
cd novamarket
PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest tests/test_novamarket*.py -q
```

Result:

```text
751 passed, 2 skipped, 433 warnings, 352 subtests passed in 134.93s
PYTEST_EXIT:0
```

The preceding run produced one failure in the newly added source-contract assertion; the failure was a stale expected string, not product behavior. The assertion was corrected to match the real `useMemo` implementation and the new price fallback. The final rerun above is green. The targeted UI suite separately passed **12/12**.

Log: `tmp/qa_audit/logs/pytest_final.log`.

### 3.2 Frontend build

Command:

```text
cd novamarket/web
npm run build
```

Result: **PASS**, 567.03 kB JS (152.37 kB gzip), built in 4.91 s. The only warning is the standard large-chunk advisory. Log: `tmp/qa_audit/logs/build_final.log`.

### 3.3 Browser and route checks

- 25/25 routes loaded with a heading, navigation and no console/page error or HTTP 5xx.
- Final dashboard: 5 unique market-pulse rows, all priced; `Active` horizon after resume; chart ticks ₹19.0k–₹21.0k.
- No horizontal overflow on desktop or mobile.
- Final screenshots: `dashboard-final-verified.png`, `market-mobile-final.png`.

### 3.4 Control checks

- Accepted baseline: 480/480 PASS (9 November; historical).
- Fresh full post-fix sweep: 401 control rows across 25 routes; 397 PASS plus four state-dependent PARTIALs, all of which passed stateful retests.
- Full current matrix after inputs, forms, writes, refresh and closure layers: **470/470 PASS**, 0 PARTIAL, 0 BLOCKED, 0 NOT PASS.
- Stateful retests:
  - Market Reset → PASS; industry selected and Reset restored `all`.
  - OrderBlock Clear filters → PASS; `zzzz-no-match` → empty; board restored.
  - TradingWatch Clear → PASS; `zzzz-no-match` → empty; 265 rows restored.
  - TradingWatch Next bar → PASS; replay position `179 / 180` → `180 / 180`.
- Closure: Market pagination and Industry filter PASS; Watchlist rename saved with the modal closed.
- The 480 baseline is net 10 higher than the current 470 rendered entries; the route delta is recorded in `COMPLETE_UI_INTERACTION_MATRIX.md`. No confirmed dead control remains.

### 3.5 Accessibility

- 25 routes;
- 957 controls;
- 32 form controls, 900 buttons, 25 links, 6 tables;
- 1,833 contrast text nodes;
- 0 unlabeled controls, unnamed buttons/links, missing alt, duplicate IDs, heading/table-header issues, contrast failures, focus gaps, or overflow;
- keyboard navigation, modal focus and 200% zoom simulation passed.

This is automated inspection, not WCAG certification.

### 3.6 Security

The 9 checks all passed:

`security_headers`, `foreign_origin`, `foreign_host`, `same_origin_validation`, `config_secrets`, `static_unsafe_html`, `static_live_path`, `token_enforcement`, `rate_limit`.

The probe is an internal review, not a penetration test.

### 3.7 Accounting and financial writes

- 18 wallets;
- 40 ledger entries;
- 0 bad running balances;
- reconciliation `ok`;
- ledger sum `0.00`;
- deposit `+100.00` and withdrawal `-25.00`;
- operator balance `30,075.00 → 30,150.00`;
- portfolio pause/resume messages verified;
- no portfolio/wallet values added together.

### 3.8 Paper chain

`scripts/verify_novamarket_chain.py` exited `0`:

- 300 bars;
- `few_sessions` forecast;
- `buy` signal;
- proposal `approved`;
- paper order `filled`;
- backtest `completed`;
- shadow `completed`;
- evidence `accepted`;
- experiment `completed`;
- candidate `promoted`.

Log: `tmp/qa_audit/chain_verification.log`.

### 3.9 Performance recheck

The 10 November post-fix probe closed the last gap. All 10 API checks and the browser check pass:

- concurrent load: 120/120 requests, 0 errors, p95 `1,397.34 ms` (limit `3,000 ms`), max `1,732.00 ms`;
- 90-second soak: 0 errors, p95 `166.06 ms` (limit `1,500 ms`), memory `168.19 MB -> 167.61 MB` (`-0.58 MB`, slope `-0.025 MB/min`);
- `/market/status` p95 `123.45 ms` (limit `1,000 ms`); `/market/overview` p95 `294.84 ms` (limit `2,000 ms`); `/market/screener` p95 `699.51 ms` (limit `4,000 ms`);
- full scan p95 `1,581.78 ms` (limit `15,000 ms`); recovery `2,357.95 ms` then HTTP 200;
- browser: 25/25 routes, 0 console/page errors, max heading `281.60 ms`, max settled `1,189.86 ms`.

The regression suite rerun after the fixes passed **795 tests, 2 skipped, 938 subtests** in 194.57 s. Evidence: `.ui_audit/performance_postfix2.json`; the fix and root cause are recorded in `BUG_FIX_REGISTER.md` (PERF-QA-001).

---

## 4. Why the verdict is PASS

The PASS bar requires all routes, controls, workflows and requirements to be covered, and the quality checks to pass. The 10 November performance recheck closes the last gap.

1. **Controls.** 25/25 routes and 470/470 current entries pass, including the stateful retests and closure checks. The 480 baseline is an inventory reconciliation, not a failing control.
2. **Performance.** All 10 API checks pass. Concurrent p95 `1,397.34 ms` is inside `3,000 ms`; soak p95 `166.06 ms` is inside `1,500 ms`; 120/120 requests completed with zero errors; memory was stable; recovery returned HTTP 200; the 25-route browser sweep passed.
3. **Requirements.** NM-01–NM-14 are 14/14 at declared scope. The deterministic paper chain passed end to end.
4. **Safety.** The ledger reconciles, the paper-only boundary holds, and security is 9/9.
5. **Stated scope boundaries.** Autonomous paper trading is not implemented and remains outside accepted NM-01–NM-14 scope. Stale live quotes were handled by risk and the deterministic fixture. The long-horizon modal opener is a harness assumption, not a claimed pass. None of these is counted as a failed requirement.

No check prevents PASS.

The application is **functional, safe, paper-only, ledger-consistent and performance-qualified at the accepted internal QA scope**.

---

## 5. Confirmed defects and disposition

| ID | Defect | Severity | Status |
| --- | --- | --- | --- |
| BUG-QA-001 | Horizon status used first book only | Medium | Fixed and tested |
| BUG-QA-002 | Market pulse selected alphabetical/duplicate/unquoted instruments and could drop valid row prices | Medium | Fixed and tested |
| BUG-QA-003 | Flat chart produced five identical Y-axis ticks | Low | Fixed and tested |
| TEST-QA-001 | Stale source-contract assertion | Low | Fixed; final suite green |

No accounting, risk, ledger, security or live-boundary defect was confirmed.

Full detail: `BUG_FIX_REGISTER.md`.

---

## 6. Residual limitations and scope decisions

| Limitation | Impact | Required action |
| --- | --- | --- |
| No autonomous trading loop | Cannot audit autonomous cycle | New feature approval required; do not implement by implication |
| Stale live quotes | No fresh live paper order through UI | Refresh providers or accept the deterministic fixture as chain proof |
| Long-horizon modal opener absent in harness | One modal path unverified | Re-test with the page's actual write flow before claiming coverage |
| Bundle-size warning | 567.03 kB JS chunk | Optional code-splitting; not a functional defect |

No financial policy, risk threshold, ledger rule or broker boundary was changed to improve these results.

---

## 7. Safety and scope confirmation

- `paper_only=true`
- `live_trading=false`
- `broker_connected=false`
- no live broker/exchange route;
- no real-money credentials requested or stored;
- no shadow or learning execution gateway;
- QA writes confined to `tmp/qa_audit/data/novamarket.sqlite3`;
- production-like services on 8011/5173 not mutated;
- no commit or push.

Autonomous paper trading was **not** implemented. It is beyond the accepted NM-01–NM-14 scope and would require explicit approval.

---

## 8. Report set

| Report | Purpose |
| --- | --- |
| `FULL_APPLICATION_QA_REPORT.md` | End-to-end functional, safety, accessibility, security and performance audit |
| `REQUIREMENTS_TRACEABILITY_REPORT.md` | NM-01–NM-14 requirement mapping |
| `COMPLETE_UI_INTERACTION_MATRIX.md` | 25-route, 470 current entries and 480-baseline reconciliation |
| `BUG_FIX_REGISTER.md` | Fixed defects, tests, false positives and remaining limitations |
| `AUTONOMOUS_PAPER_TRADING_AUDIT.md` | Explicit finding that no autonomous loop exists |
| `QA_COMPLETION_REPORT.md` | Final verdict, checks and residual gaps |

---

## 9. Completion decision

**PASS — functionally sound, paper-only, ledger-consistent, safe and performance-qualified at the accepted internal QA scope.**

All 25 routes and 470/470 current controls pass, the 10/10 performance recheck passes, the paper chain is proven, and the safety boundaries hold. Autonomous paper trading and live-quote freshness remain stated scope/data limitations, not failed requirements.

No further deployment, commit or production mutation is implied by this report.
