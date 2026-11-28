# Release Notes

## NovaMarket NM-14 — Paper-Only Baseline

**Release identity:** NovaMarket `1.0.0` / contract `1.0.0` / configuration schema `1`

**Baseline:** accepted NM-01 through NM-14 implementation

**Final acceptance:** **9 November 2026**

**Disposition:** **PASS within internal acceptance scope** — 25/25 routes, 480/480 recorded controls, full regression green. External certifications are **not** claimed.

> **Paper-only release boundary:** there is no broker client, no live-order path and no real-money trading. Paper fills are simulated. `GET /market/execution/status` reports `paper_only: true`, `live_trading: false`, `broker_connected: false`.

## Contents

1. [What this release is](#1-what-this-release-is)
2. [NM-01–NM-14 completion](#2-nm-01nm-14-completion)
3. [Final acceptance evidence](#3-final-acceptance-evidence)
4. [Performance and soak](#4-performance-and-soak)
5. [Build, types and documentation](#5-build-types-and-documentation)
6. [Market-data acceptance](#6-market-data-acceptance)
7. [Accessibility and security](#7-accessibility-and-security)
8. [Safety and execution boundary](#8-safety-and-execution-boundary)
9. [Known limitations](#9-known-limitations)
10. [Compatibility and upgrade notes](#10-compatibility-and-upgrade-notes)
11. [Release gate and change control](#11-release-gate-and-change-control)
12. [PI-01–PI-24 closure addendum (21 November 2026)](#12-pi-01pi-24-closure-addendum-21-november-2026)

## 1. What this release is

NovaMarket is a standalone, paper-only financial intelligence application inside the NovaControl runtime. This baseline delivers:

- market-data ingestion, provenance, freshness and quality controls;
- a deterministic NovaCoin wallet and append-only ledger;
- evidence-backed events, asset discovery and watchlists;
- probabilistic forecasts with calibration/evaluation;
- three-horizon portfolios and portfolio analytics;
- an independent, versioned risk engine;
- catalogued strategies and risk-gated proposals;
- realistic **simulated** paper execution with fees, slippage, fills and reconciliation;
- reproducible backtesting, non-executing shadow mode and governed adaptive learning;
- a 25-route React workspace and the `/market` API;
- activity/audit, notifications, health and lifecycle controls.

It is not a broker, exchange, investment adviser or portfolio-management service for real assets.

## 2. NM-01–NM-14 completion

| Phase | Scope | State |
|---|---|---|
| NM-01 | Architecture, Contracts & Integration | Accepted |
| NM-02 | Market Data Reliability | Accepted |
| NM-03 | NovaCoin Wallet & Deterministic Ledger | Accepted |
| NM-04 | Evidence-Backed Market Event Radar | Accepted |
| NM-05 | Autonomous Asset Discovery & Watchlists | Accepted |
| NM-06 | Measured Probabilistic Forecasting | Accepted |
| NM-07 | Autonomous Three-Horizon Portfolio Manager | Accepted |
| NM-08 | Independent Risk Engine | Accepted |
| NM-09 | Trading Strategies | Accepted |
| NM-10 | Realistic Paper Execution | Accepted — simulated only |
| NM-11 | Reproducible Backtesting | Accepted |
| NM-12 | Shadow Mode | Accepted — non-executing |
| NM-13 | Controlled Adaptive Learning | Accepted — no order gateway |
| NM-14 | Integrated Workspace & Final Acceptance | **Accepted — PASS (2026-11-09)** |

No NM-15 is introduced or implied by this release.

## 3. Final acceptance evidence

| Area | Result |
|---|---|
| Overall verdict | **PASS — internal acceptance scope; external certifications not claimed** |
| UI route loads | **25/25 PASS** |
| Recorded control matrix | **480 PASS, 0 PARTIAL, 0 BLOCKED** (`docs/NM14_UI_CONTROL_MATRIX.json`) |
| Fresh full-route sweep | 25 routes; 480 recorded results; no BLOCKED/PARTIAL row |
| Regression | **792 passed, 2 skipped, 938 subtests passed in 176.90 s (exit 0)** |
| Production build | **PASS** — 1,598 modules; chunk-size warning only |
| Types | `mypy src` clean; `mypy src --platform win32` clean (555 files) |
| API reference | `docs/API.md` in sync |
| API surface | 200 `/market` paths / 214 operations in the accepted running OpenAPI document |
| Audit data | 4,535 / 4,535 eligible instruments; 0 skipped, 0 failed, 0 duplicates, 0 synthetic bars |
| Accessibility | Internal automated review PASS only — 25 routes, 1,070 controls, 0 issue classes |
| Security | Internal review PASS only — 9/9 checks |

The acceptance matrix is the authoritative control record. Counts in earlier historical sections (including the superseded 247 PASS / 12 PARTIAL sweep) are not the final result.

### Final report

The authoritative record is [NM14_FINAL_ACCEPTANCE_REPORT.md](NM14_FINAL_ACCEPTANCE_REPORT.md). The earlier [NM14_ACCEPTANCE.md](NM14_ACCEPTANCE.md) PARTIAL record is retained for history and has been superseded.

## 4. Performance and soak

| Measurement | Result |
|---|---|
| 90-second soak | **p95 123.07 ms**, 0 errors |
| Memory | **162.86 MB → 162.52 MB**; growth **−0.34 MB**; slope **−0.27 MB/min** |
| Load test | **120/120 requests successful**; 4.44 req/s; median **1,859.32 ms**; p95 **2,360.51 ms**; max **2,546.89 ms** |
| Browser navigation | 25 routes; max heading **356.70 ms**; max settled **1,273.05 ms**; 0 errors |
| Full market scan | **p95 1,817.55 ms**; limit 15,000 ms |
| Paper execution latency | No order load in the accepted runtime status; latency sample count was 0 at check time |

These are measurements of the acceptance environment, not a capacity guarantee for every machine, dataset or network. A larger runtime database or a slower provider can change timings.

## 5. Build, types and documentation

### Frontend production build

- **1,598 modules** transformed.
- JavaScript: **566.64 kB / 152.21 kB gzip**.
- CSS: **91.25 kB / 16.80 kB gzip**.
- Vite emitted a chunk-size warning only; the build succeeded.
- Static output is `web/dist/`.

### Type checking

Both CI views passed cleanly:

```text
mypy src                          Success: no issues found in 555 source files
mypy src --platform win32         Success: no issues found in 555 source files
```

### API reference

`scripts/generate_api_reference.py --check` passed. The generated block remains the authoritative route inventory; `docs/API_GUIDE.md` explains conventions and representative flows without duplicating every route.

**Route-count note:** the NM-14 report's historical "372 NovaMarket routes" wording counts registered entries differently from OpenAPI path operations. The accepted running OpenAPI document has **200 `/market` paths / 214 operations**. This is a reporting/counting distinction, not missing functionality.

## 6. Market-data acceptance

The 9 November evidence used an **isolated audit database**, not the default runtime database:

- **4,535 of 4,535 eligible instruments** scanned; **0 skipped, 0 failed, 0 duplicates, 0 synthetic bars**.
- **667** official NSE futures contracts across **224** underlyings.
- **6,625** official NSE index-option contracts.
- **147** NIFTY option strikes in the reported snapshot.
- **PCR 1.6528** for the reported snapshot.
- **99 BSE instruments** lack bars because their symbols were absent from the downloaded BSE session.
- **151 recent listings** have only **3–29 bars**.
- Official derivatives snapshot trade date: **6 November 2026** (EOD, not live).
- Options IV is **model-derived with Black-Scholes**, not provider-reported IV.

The audit is a historical snapshot. Do not present it, or the screenshots, as current market data. The normal runtime may be empty, partial or older until the universe is synced and data imported.

## 7. Accessibility and security

### Accessibility

**Internal automated review only:** 25 routes, **1,070 controls**, 0 unlabeled controls, 0 heading/table/contrast/duplicate-ID/focus/overflow issue classes, 0 console/page errors; **10 `aria-sort`** attributes across **6** sortable tables; modal/keyboard checks passed.

This is **not** a WCAG conformance certification and not an independent audit.

### Security

**Internal review only:** **9/9 checks** — headers, foreign origin 403, foreign host 400, same-origin validation, configuration secret scan, static unsafe-HTML/live-path scans, token enforcement and rate limit.

This is **not** a penetration test, a security certification or a guarantee against all vulnerabilities. No external certification has been obtained.

## 8. Safety and execution boundary

The release is structurally paper-only:

- no broker client is implemented;
- no live-order execution path exists;
- no real-money trading is supported;
- `GET /market/execution/status`: `paper_only: true`, `live_trading: false`, `broker_connected: false`;
- `GET /market/shadow/status`: `real_money: false`, `execution_connected: false`, `nm13_activated: false`;
- `GET /market/learning/status`: `real_money: false`, `execution_connected: false`, `executes_trades: false`, `isolated: true`;
- `GET /market/backtests/status`: paper/live isolation with `real_money: false`;
- risk approval cannot be bypassed from the UI or API;
- a paper fill is simulated and must never be represented as a real market fill;
- market-data availability does not imply live trading capability.

No broker integration, real-money execution or credentials are part of this release.

## 9. Known limitations

1. **Paper-only:** no real order, broker balance, bank rail or withdrawal capability.
2. **Incomplete coverage:** 99 BSE names have no bars and 151 recent listings are short; the 4,535-instrument audit is isolated evidence, not default runtime content.
3. **EOD/delayed data:** public providers can be delayed, rate-limited or unavailable; official results are historical snapshots.
4. **Model IV:** Black-Scholes output is not provider-reported IV, and no live option-chain/provider-IV feed is implemented.
5. **Research uncertainty:** forecasts, backtests, stress tests and shadow projections can be wrong and are not investment advice.
6. **Internal reviews:** accessibility/security results are not external certifications or penetration tests.
7. **Build size:** the accepted JS chunk-size warning is informational; large bundles may affect slower clients.
8. **Route-count wording:** use the generated reference for the exact route inventory rather than a single ambiguous total.

## 10. Compatibility and upgrade notes

- Python **3.12+** is required; Node **18+** for Vite 6 (20/22 LTS recommended).
- Configuration `schema_version` **1** and contract **1.0.0** are enforced; incompatible versions are refused.
- SQLite is the only persistence engine. Back up `novamarket.sqlite3`, `novamarket_shadow.sqlite3`, `novamarket_learning.sqlite3` and `novamarket.json` while stopped.
- Do not copy `.ui_audit` acceptance data into the runtime directory or delete a production database to "reset" the app.
- API auth uses the shared `NOVACONTROL_API_TOKEN`; CORS uses exact origins. Do not weaken these to simplify an upgrade.
- No migration to a broker, live venue or external financial database is supported by this release.

## 11. Release gate and change control

A release based on this baseline must preserve:

- the NM-01–NM-14 domain behavior and public contracts;
- the independent risk gate;
- exact-decimal, append-only ledger behavior;
- simulated-only execution;
- non-executing shadow mode and learning;
- source/time provenance for market data;
- the 25-route UI/API synchronization and generated reference.

Reverification for a code change should include focused regression tests, both mypy views, the API reference check and a real browser check of affected routes. Do **not** rerun the entire NM-14 acceptance suite merely for documentation edits, and do not claim a new PASS from partial evidence.

The documentation milestone that accompanies this baseline changes documentation only. It does not add brokers, live execution, NM-15 functionality, or changes to NovaControl/NovaForge behavior.

## 12. PI-01–PI-24 closure addendum (21 November 2026)

This addendum records the final verification of the already accepted NM-01–NM-14
baseline plus the PI-01–PI-24 prediction-intelligence roadmap. It adds no phase
and no feature.

**Verified**

- Full NovaMarket suite with the declared `prediction` and `lstm` extras:
  **1907 passed, 0 failed, 0 errors, 2 skipped, 445 subtests in 736.50 s**
  (Python 3.13.0, pytest 9.1.1).
- `mypy src`: clean over 721 files; `docs/API.md` in sync at **933 routes**.
- All 14 SQLite stores: `integrity_check` ok, zero foreign-key violations.
- Paper chain end-to-end: fixture data → forecast → strategy proposal →
  independent risk decision → simulated fill → holdings/valuation → ledger →
  exit → realized P&L, with deterministic backtest replay.
- Every filled backtest order mapped 1:1 to an approved risk decision; rejected
  proposals could not be forwarded, and duplicate forwarding created no second
  order.

**Fixed**

- **O-1** — `/holdings` served a stored fill price while `/valuation` refreshed
  to the latest observable. `PortfolioEngine.holdings()` now refreshes positions
  through the same path; a regression test pins one price per position.
- Earlier cycle: LSTM unavailable-runtime crash, shutdown SQLite handle leak,
  stale route contract, and manual-review fallback targeting a closed book.

**Readiness**

| Area | Verdict |
|---|---|
| Software integration | PASS |
| Forecasting/strategy intelligence | PARTIAL (weak measured skill; 1m horizon added post-closure in §13) |
| Paper trading | PASS (controlled, simulated; no sustained live session) |
| Live market | BLOCKED (no live data or broker) |

**Still open:** the PI-23 portfolio-horizon to forecast-horizon mapping and the
AT-03 venue/instrument identifier mismatch recorded in the README remain open;
no live-market validation is claimed. The 20m → 5m → 1m hierarchy and 1m cycle
are implemented in the post-closure enhancement described in §13.

See `docs/FINAL_PI01_PI24_CLOSURE_AND_READINESS_REPORT.md` for the full evidence.

## 13. Post-PI-24 multi-timeframe enhancement (21 November 2026)

A post-closure enhancement adds the missing 20-minute context → 5-minute setup →
1-minute timing hierarchy, a genuine one-minute forecast cycle and cross-
timeframe conflict resolution. The layer is analysis-only: BUY/SELL candidates
gate new day-trading proposals before NM-08, while risk and NM-10 paper
execution remain mandatory. Swing and long-term strategy logic is unchanged.

New routes: `GET /market/multi-timeframe/status` and
`GET /market/multi-timeframe/analysis` (API reference at 935 routes). The
real-data chronological comparison covers two stored one-minute sessions and
two enhanced simulated trades; it does not establish improved accuracy or
profitability. See `docs/MULTITIMEFRAME_TRADING_INTELLIGENCE.md`.
