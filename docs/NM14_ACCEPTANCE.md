# NovaMarket NM-14 — Integrated Workspace & Final Acceptance

**Phase:** `NM-14` (final phase of the NM-01–NM-14 roadmap)  
**Date:** 2026-11-04  
**Workspace:** `novamarket/`  
**Final acceptance status:** **PARTIAL — superseded by `NM14_FINAL_ACCEPTANCE_REPORT.md`**. The executed backend chain and 25-route render recorded here remain valid evidence; derivatives coverage and exhaustive control-level UI verification are not complete.

> The complete NM-01 → NM-14 chain ran in one process against an isolated
> acceptance store: 300 market sessions → forecast → portfolio → strategy →
> risk-approved proposal → filled paper order → backtest → shadow → evidence →
> experiment → promoted candidate. The chain exits 0. The full NovaMarket suite
> is green, and the live standalone workspace rendered all 25 routes without an
> error boundary or offline state.

---

## 1. Executive summary

NovaMarket NM-14 is implemented as an integrated subsystem, not a new financial
engine. The final phase added:

* a real, live-state operational workspace at `GET /market/workspace`;
* one read-only aggregation API, `GET /market/overview`;
* one event observer so activity and audit include wallet, risk, execution,
  shadow and learning events, not only lifecycle events;
* the shared local `novacontrol.audit` trail for NovaMarket events;
* discovery and strategy state in the main status surface;
* the NM-14 phase marker and a corrected contract description.

The full NovaMarket test group passes: **735 passed, 2 skipped, 352 subtests**.
Both mypy views are clean, `docs/API.md` is in sync, and the standalone
workspace rendered all 25 routes against the current backend with no error
boundary and no fabricated values.

The live market-data verification script also passed against real providers:
Yahoo returned a fresh AAPL quote, CoinGecko returned a fresh BTC quote, and NSE
returned 247 historical daily bars. The NSE instrument was correctly reported as
pre-open with no live quote.

## 2. Audit of NM-01 through NM-13

| Phase | Existing implementation | Integration state | Verification |
| --- | --- | --- | --- |
| NM-01 Architecture & contracts | service, lifecycle, permissions, events, storage boundaries | integrated in application/runtime/API | `test_novamarket.py` |
| NM-02 Market data | append-only repository, providers, freshness, quality, ingestion | integrated in service and live API | `test_novamarket_marketdata.py`; live script |
| NM-03 Wallet & ledger | wallets, double-entry ledger, reconciliation, reversals | integrated in portfolio and execution | `test_novamarket_wallet.py` |
| NM-04 Event radar | sources, extraction, verification, classification | integrated in discovery/portfolio/risk | `test_novamarket_radar.py` |
| NM-05 Discovery & watchlists | candidates, scoring, watchlist lifecycle, scheduler | integrated in portfolio/risk | `test_novamarket_discovery.py` |
| NM-06 Forecasting | estimators, calibration, model lifecycle | integrated in portfolio/risk | `test_novamarket_forecasting.py` |
| NM-07 Portfolios | three horizons, allocation, valuation, accounting | integrated with wallet and execution | `test_novamarket_portfolio.py` |
| NM-08 Risk | independent policy engine and decisions | authoritative gate for strategy/execution | `test_novamarket_risk.py` |
| NM-09 Strategies | catalog, signals, proposals, lifecycle | integrated with risk and execution | `test_novamarket_strategies.py` |
| NM-10 Paper execution | simulated orders, fills, fees, slippage, reconciliation | integrated with wallet, portfolio, risk, strategy | `test_novamarket_execution.py` |
| NM-11 Backtesting | point-in-time replay, fees, risk, reproducibility | isolated from paper and wallet | `test_novamarket_backtesting.py` |
| NM-12 Shadow mode | non-executing sessions, cycles, projections, outcomes | isolated by design and guards | `test_novamarket_shadow.py` |
| NM-13 Learning | evidence, experiments, approval, versions, rollback | consumes NM-10/11/12 evidence; no financial writes | `test_novamarket_learning.py` |
| NM-14 Workspace | overview, activity/audit, UI, phase closure | added here | `test_novamarket_api.py::NovaMarketWorkspaceApiTests` |

### Status classes at the start of NM-14

* **Implemented and verified:** NM-01 through NM-13, at their own phase scope.
* **Implemented but not integrated:** the durable financial audit boundary and
  the final workspace were the two material gaps.
* **Integrated but not adequately verified:** the status surface did not expose
  discovery and strategy state; no single overview represented all components.
* **Partially implemented:** operational activity existed only as scattered
  subsystem histories; the UI had no NovaMarket surface.
* **Missing:** `/market/workspace`, `/market/overview`, shared market audit rows.
* **Broken/regressed:** the contract still advertised NM-13 and stale
  “no portfolio/forecast/strategy/backtest” limitations.

## 3. Integration gaps identified

1. `IMPLEMENTED_PHASE` was still `NM-13`; the roadmap's final phase existed only
   as a declaration.
2. No NovaMarket operational workspace existed.
3. No single overview exposed the state of all thirteen phases.
4. The status surface omitted discovery and strategy state.
5. Market events were not written to the shared audit trail; only lifecycle
   activity from the service itself was visible in the service's local path.
6. Contract limitations still said portfolio, forecast, strategy, backtest and
   order execution did not ship, contradicting the implemented phases.
7. The existing route-consumer comments still said the workspace was deferred.
8. The market surface's route count and documentation were pinned to 203 routes;
   the new overview route needed all five registration places.
9. `/market/contracts` exposed strategy, execution and backtesting in status but
   omitted their contract documents, and had no NM-14 audit contract.
10. `/market/status` called several component status readers directly, so one
    failing subsystem store could take down the whole status/overview page.
11. The activity/audit observer was tied to a successful bus publication, so an
    operation was not auditable when event emission was disabled or no bus was
    attached.

## 4. Repairs completed

* Set `IMPLEMENTED_PHASE = "NM-14"` and updated phase tests.
* Added `GET /market/workspace`, a standalone page served by the existing
  FastAPI application, with content-derived asset versions and no-cache headers.
* Added `GET /market/overview`, composed from live subsystem state. It includes:
  status, summary, recent records, activity, audit summary and safety flags.
* Added discovery and strategy to `NovaMarketService.status()`.
* Added `attach_audit_logger()` and bound the application's ONE `AuditLogger` to
  NovaMarket after application construction.
* Added a single observer to `MarketEventPublisher`, so all subsystem events
  (wallet, risk, strategy, execution, backtest, shadow, learning) reach activity
  and audit without each engine growing a second observer.
* Added `/market/overview` to `ApiSurface`, `ROUTE_CONSUMERS`, tests and
  `docs/API.md`.
* Added the strategy, execution, backtesting and audit contracts to
  `/market/contracts`; the audit contract reports the real sink, locality and
  retention policy.
* Wrapped every component status read in one failure-isolating helper, so one
  unavailable store reports its own error instead of taking down `/market/status`
  or `/market/overview`.
* Made the NM-14 event observer independent of bus attachment and emission
  switches; committed operations remain auditable when announcements are off.
* Replaced stale contract limitations with the actual legacy-simulation and
  read-only-workspace boundaries.
* Added browser-realistic workspace sections for Overview, Market Intelligence,
  Market Events, Asset Discovery, Forecasts, Portfolios, Risk, Strategies, Paper
  Trading, Backtesting, Shadow Mode, Adaptive Learning, Wallet & Ledger,
  Activity & Audit, System Health and Settings.
* Kept the API token out of `localStorage`; it is held in the tab's memory only.

## 5. Complete subsystem integration map

```text
NovaControl application / EventDrivenRuntime
        │
        ├─ NovaMarketModule ── NovaMarketService
        │       ├─ lifecycle / permissions / capabilities / health
        │       ├─ NM-02 market data + providers + SQLite history
        │       ├─ NM-03 wallet + append-only deterministic ledger
        │       ├─ NM-04 radar ── NM-05 discovery ── NM-06 forecasts
        │       ├─ NM-07 three-horizon portfolios
        │       ├─ NM-08 independent risk (veto authority)
        │       ├─ NM-09 strategies ── NM-10 paper execution
        │       ├─ NM-11 isolated backtesting
        │       ├─ NM-12 non-executing shadow mode
        │       └─ NM-13 evidence-driven learning
        │
        ├─ ONE EventBus ── MarketEventPublisher ── activity + audit observer
        ├─ ONE PermissionManager and CapabilityRegistry
        ├─ ONE JsonStateStore / SQLite data directory
        ├─ ONE AuditLogger (Novacontrol.audit)
        └─ FastAPI /market/* ── standalone /market/workspace
```

The components remain separate domains. NM-14 adds observation, aggregation and
navigation; it does not add a second risk engine, portfolio manager, execution
engine, scheduler, event bus or database.

## 6. Financial accounting verification

Verified by `tests/test_novamarket_wallet.py` and the workspace integration test:

* 1 NovaCoin is 1 INR (`nova_coin_per_inr = "1"`).
* All reporting values are INR/NC at two-decimal, exact `Decimal` precision.
* Wallet creation opens at zero; deposits, withdrawals and transfers post
  balanced ledger entries; the issuance account offsets user balances.
* Reversals create mirrored entries and never edit the original.
* Reconciliation recomputes balances from ledger entries and does not repair by
  overwriting history.
* A 125.00 NC deposit produced `summary.wallet_balance_inr == "125.00"` in the
  integrated overview test.
* `NovaMarketRouteContractTests::test_no_live_execution_route_exists` and the
  safety flags (`real_money_trading=false`, `broker_connected=false`) pin the
  absence of a real-money path.
* Shadow and learning modules have no wallet/ledger gateway in their construction.

## 7. Market-data verification

`scripts/verify_marketdata_live.py` exited 0 on 2026-11-03:

| Instrument | Result |
| --- | --- |
| `XNAS:AAPL` | fresh valid quote, 330.54 USD, Yahoo, age 0.879s |
| `CRYPTO:BTC` | fresh valid quote, 77,759 USD, CoinGecko, age 53.365s |
| `XNSE:RELIANCE` | 247 daily bars; no live quote because NSE was pre-open |

The provider registry reported Yahoo and CoinGecko healthy, 3 instruments,
3 observations, 496 bars, and verified operations `quote`, `ohlcv`, `probe`.
This proves the live collection path, canonical currency normalization,
timestamps, freshness and provenance. It does not claim those prices remain
current after the verification timestamp.

The deterministic suite also covers missing values, duplicates, stale data,
provider fallback, calendars, OHLCV consistency and unavailable data. No
fabricated price replaces an unavailable observation.

## 8. Portfolio and risk verification

`tests/test_novamarket_portfolio.py` and `tests/test_novamarket_risk.py` cover:

* distinct `next_session`, `few_sessions`, `one_week`, `one_month` and
  `three_months` horizons and their policy profiles;
* allocation, proposal creation, funding, valuation, reconciliation and closure;
* risk assessment against the exact proposal and portfolio state;
* rejection and restriction outcomes, policy versions, alerts, stress results
  and overrides;
* stale or material-change proposals are refused by the existing decision and
  proposal coins/marks;
* execution records preserve the proposal and risk decision references.

NM-08 remains the only risk engine. Strategy and execution call its decision
path; there is no second risk table.

## 9. Paper-execution verification

`tests/test_novamarket_execution.py` covers the real wallet, portfolio,
execution and reconciliation stores with deterministic market/strategy/risk
seams:

* order submission from an approved proposal;
* partial and complete fills with fees and slippage;
* reservation, cancellation and remaining-quantity release;
* portfolio position updates only after valid execution events;
* ledger reconciliation and persisted execution audit records;
* idempotency and duplicate handling;
* no live broker or exchange path.

The workspace displays execution counts and recent orders from the same service
state; it does not model performance or fills client-side.

## 10. Backtesting verification

`tests/test_novamarket_backtesting.py` covers point-in-time bars, next-bar
execution, fees/slippage, risk decisions, walk-forward folds, sensitivity,
data-quality reporting, reproducibility verification and persistence. Backtests
use their own isolated database and never mutate paper orders, wallets,
portfolios or shadow sessions.

## 11. Shadow Mode verification

`tests/test_novamarket_shadow.py` covers the non-executing lifecycle: create,
start, pause, resume, stop, complete, cancel; observation cycles; decisions;
hypothetical projections; outcomes; analytics; errors; health; comparisons.
Guards and store construction connect no wallet, ledger, portfolio or execution
gateway. Shadow status explicitly reports `real_money=false`,
`execution_connected=false` and `nm13_activated=false`.

## 12. Adaptive-learning verification

`tests/test_novamarket_learning.py` and `NovaMarketLearningApiTests` cover
provenance, evidence eligibility, out-of-sample comparison, risk rejection,
manual-default approval, explicit automatic policy, shadow validation ordering,
versioning, promotion, rollback, post-promotion monitoring and audit history.
Learning has its own isolated SQLite file and no execution route. The earlier
NM-13 completeness gaps are now covered by the learning suite: per-instrument
attribution for NM-10/11 evidence, hard instrument/regime coverage gates,
enforced resource ceilings, and persisted `SHADOW_VALIDATION` rows. The bounded
search and descriptive regime labels are declared NM-13 scope, not defects.

## 13. Operational interface status

`GET /market/workspace` is a standalone NovaMarket page, deliberately separate
from the NovaControl root UI. It displays:

* lifecycle, health and phase from `/market/overview`;
* wallet balance, paper counts, risk counts, backtests, shadow sessions and
  learning readiness from the live status payload;
* every subsystem section, using only records the subsystem returned;
* activity and audit timelines;
* health, dependencies, capabilities, configuration and schema;
* explicit empty, not-open, loading, authentication-required, stale-data and
  error states;
* real Enable/Disable lifecycle controls and a Refresh action.

Browser verification on `http://127.0.0.1:8011/market/workspace` confirmed:

* page title and all 16 navigation sections rendered;
* badges showed `NM-14`, `RUNNING`, `HEALTH OK`;
* safety pills showed real-money trading disabled, broker none, zero live routes;
* activity showed lifecycle events and audit showed persisted completed rows;
* no console errors; CSS, JS and icon returned 200;
* asset version token was injected, not hard-coded;
* a direct reload on the `#wallet` hash preserved `NM-14 / RUNNING / HEALTH OK`
  and disabled Enable once the overview arrived. A first verification found the
  badges stuck at `LOADING` on a non-Overview hash; the fix renders the header
  chrome from every load/select path, and the reload was checked again.

The current interface is intentionally read-only for financial state; mutations
remain explicit API operations. It does not pretend that a form exists where it
does not.

## 14. Security and safety verification

* All management/market API routes are authenticated through the existing
  bearer-token dependency; the workspace asks for the token when enabled.
* The token is not stored in `localStorage`, cookies or logs by the workspace.
* No route exposes a secret value; configuration uses secret references.
* Real-money trading is absent from the permission model and the route set.
* Paper execution has no broker adapter; shadow and learning have no execution
  gateways.
* Market audit records use the existing local-only audit logger with redaction
  and retention; no second audit store was added.
* Finance mutations and lifecycle changes rely on service-level validation,
  exact decimal amounts and deterministic ledger constraints.

## 15. End-to-end scenario results

| Scenario | Result | Executed evidence |
| --- | --- | --- |
| A — Market intelligence | **Verified end to end** | 300 weekday sessions seeded at 10:00 UTC; 300 bars returned; forecast `few_sessions` generated with measured uncertainty |
| B — Portfolio creation | **Verified end to end** | wallet created and funded; portfolio created and funded from that wallet in the same chain |
| C — Strategy and risk | **Verified end to end** | strategy evaluated to a real `buy`; proposal assessed by NM-08 and `approved` |
| D — Paper execution | **Verified end to end** | approved proposal submitted to NM-10; paper order `filled` |
| E — Backtesting | **Verified end to end** | point-in-time replay `completed` with 10-session walk-forward slices |
| F — Shadow Mode | **Verified end to end** | shadow session created, cycled and `completed` without an execution gateway |
| G — Adaptive learning | **Verified end to end** | evidence `accepted`; experiment `completed`; candidate 4/10 `promoted` |
| NM-14 workspace | **Verified live** | all 25 routes rendered; zero error boundaries or offline states |

The chain is reproducible with:

```text
PYTHONPATH=src ../.venv/Scripts/python.exe scripts/verify_novamarket_chain.py
```

It exits 0. The promoted candidate's validation return was 0.01955 against a
0.01205 baseline, with walk-forward consistency 1.0; all four of ten evaluated
configurations were eligible. This is the executed payload, not a composed
example.

## 16. Automated test results

Command:

```text
PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest \
  tests/test_novamarket.py tests/test_novamarket_api.py \
  tests/test_novamarket_wallet.py tests/test_novamarket_marketdata.py \
  tests/test_novamarket_radar.py tests/test_novamarket_discovery.py \
  tests/test_novamarket_forecasting.py tests/test_novamarket_portfolio.py \
  tests/test_novamarket_risk.py tests/test_novamarket_strategies.py \
  tests/test_novamarket_execution.py tests/test_novamarket_backtesting.py \
  tests/test_novamarket_shadow.py tests/test_novamarket_learning.py -q
```

**735 passed, 2 skipped, 352 subtests passed** across three bounded groups
(327 + 254 + 136 tests; 89.24 s, 21.10 s and 16.16 s). The suite was split
because the runner caps a single command at 300 s; every test file ran.

New NM-14 tests:

* `NovaMarketWorkspaceApiTests::test_the_workspace_page_is_served_without_fabricated_values`
* `NovaMarketWorkspaceApiTests::test_overview_reflects_the_live_subsystem_and_wallet`
* `NovaMarketWorkspaceApiTests::test_status_and_overview_survive_one_component_read_failure`
* `NovaMarketWorkspaceApiTests::test_activity_and_audit_survive_disabled_event_emission`

`NovaMarketContractApiTests::test_contracts_describe_the_implemented_architecture`
was extended to pin the strategy, execution, backtesting, shadow, learning and
audit contracts.

Static checks:

| Check | Result |
| --- | --- |
| `mypy src` | 552 files, no issues |
| `mypy src --platform win32` | 552 files, no issues |
| `scripts/generate_api_reference.py --check` | `docs/API.md: in sync` (372 NovaMarket routes) |
| `node --check static/market-workspace.js` | clean |
| `git diff --check` | clean |
| `npm run build` (`novamarket/web`) | 1,594 modules; JS 459.88 kB (125.28 kB gzip), CSS 61.41 kB (12.23 kB gzip) |
| `scripts/verify_novamarket_chain.py` | exit 0; candidate promoted; evidence accepted |

## 17. Performance measurements

Measured locally on Windows, in-process services with a temporary data directory
(not a production benchmark):

| Measurement | Result |
| --- | --- |
| `NovaMarketService` construction + enable | 256.1 / 290.4 / 331.7 ms (median 290.4 ms) |
| `workspace_overview()` steady state | 2.4–3.2 ms; median 3.0 ms |
| First overview after opening components | 216–303 ms (lazy subsystem creation) |
| Wallet deposit | 1.4–2.1 ms; median 1.8 ms |
| HTTP `/market/workspace` | 6.0–6.7 ms |
| HTTP `/market/overview` | 6.1–15.1 ms |

The first overview is slower because it lazily opens the closed subsystems.
Steady-state reads are cheap; the page does not poll faster than every 30s and
pauses when the tab is hidden.

## 18. Scope boundaries (intentional, not defects)

* **Paper-only execution.** No broker adapter, live exchange route, bank rail or
  real-money path exists. This is the declared safety boundary of NovaMarket.
* **Read-only workspace.** The NM-14 page observes and navigates; financial
  mutations remain explicit API operations with their own tests.
* **Provider-dependent live quotes.** A quote is evidence at its observation
  timestamp, not a promise of a continuous feed. Unavailable quotes render as
  unavailable, never as a fabricated price.
* **Promotion/rollback spans two stores.** Compensation restores the previous
  configuration when a cross-store write fails; a failed restore is reported in
  `last_error`, never hidden.
* **Bounded learning search.** NM-13 performs bounded grid/random/walk-forward
  search and labels regimes as descriptive; it does not claim unrestricted
  model training or predictive regime labels.

The executed backend chain has no known accounting, risk or integration defect.
The remaining limitations are external data coverage and verification breadth,
recorded in `NM14_FINAL_ACCEPTANCE_REPORT.md`; they are not safety-boundary
claims.

## 19. Remaining defects

No known accounting or integration defect remains in the executed chain. The
suite is green, the acceptance chain exits 0, and no failing check was skipped,
weakened or reclassified. The control-level browser matrix is not complete; see
the final report §C and §I.

## 20. Final acceptance status

**PARTIAL — superseded by `NM14_FINAL_ACCEPTANCE_REPORT.md`.**

* NM-01 through NM-14 ran as one live chain: market data, forecast, wallet,
  portfolio, strategy, independent risk approval, paper fill, backtest, shadow,
  evidence, experiment approval and candidate promotion.
* The chain produced 300 bars, a real forecast, an approved proposal, a filled
  paper order, completed replay and shadow sessions, accepted evidence, a
  completed experiment and a promoted candidate.
* **735 passed, 2 skipped, 352 subtests**; both mypy views clean; API reference
  in sync (372 routes); production frontend build clean; 25/25 live routes with
  no error boundary.
* The declared safety boundaries are the paper-only/no-broker model and the
  read-only workspace mutation boundary. Data coverage and control-level
  verification gaps are listed in `NM14_FINAL_ACCEPTANCE_REPORT.md`.
