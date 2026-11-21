# NovaMarket Prediction Intelligence — Final Completion and Integration Report

**Phase:** PI-24 — Event-Aware Predictive Intelligence, Final Integration and Roadmap Closure  
**Branch:** `feature/novamarket`  
**Date:** 18 November 2026  
**Status:** **PASS (scoped implementation and integration)** — see §21 for the
verdict and the production/live-data limitations that remain unverified.  
**Roadmap:** the agreed PI-01 through PI-24 roadmap is **COMPLETE** for the
implemented scope. There is no PI-25.

No file was staged, committed, tagged, pushed, merged or published while this
report was written. The existing working-tree changes from PI-01 through PI-23
were preserved.

> **Commit status (21 November 2026):** the PI-01 through PI-24 working tree,
> including this report, is committed and pushed on `feature/novamarket`.
> README status, `.gitignore` scratch rules and `docs/API.md` (932 routes) were
> refreshed in that commit, together with the API reference at **933 routes**
> (the `/market/training/datasets` handler is now declared); the limitations
> above and in the README remain open.

---

## 1. PI-01 through PI-23 evidence reconciliation

The prior completion reports were read before implementation. All record a PASS
or scoped PASS for their stated scope. Their limitations are carried forward
rather than overwritten.

| Phase | Prior verdict | Reconciled evidence / remaining caveat |
|---|---|---|
| PI-01 | PASS (scope); PARTIAL for whole-repository regression | Prediction contracts, stores, lifecycle and gateway integration. Whole-repo run was not repeated; targeted PI-01 dependants passed in later phases. |
| PI-02 | PASS | Historical datasets, chronological splits and dataset versions. Provider coverage limitation remains. |
| PI-03 | PASS (scope) | Market-data gateway, freshness and quality states. Live-provider behaviour not re-run. |
| PI-04 | PASS | Feature engineering and regime detection; `news_events`/radar integration is the existing event path reused by PI-24. |
| PI-05 | PASS (scope) | Statistical baselines and horizon metrics. 90-day coverage limitation recorded. |
| PI-06 | Implemented, integrated, tested | Gradient-boosting training, feasibility and failure isolation. No new model was added in PI-24. |
| PI-07 | PASS (scope) | LSTM training/persistence and failure isolation. Performance limitations remain as reported. |
| PI-08 | Scoped PASS | Ensemble forecasting, calibration and disagreement. No second ensemble was created. |
| PI-09 | PASS (scope) | 20-minute scheduler, T−20/T−5/T−1 checkpoints and immutable lifecycle. Regression batch passed. |
| PI-10 | PASS (scoped) | Swing and long-term horizons; 2027 NSE holiday publication remains an external limitation. |
| PI-11 | PASS (scoped) | Evaluation, backtesting and chronological metrics; no historical profitability is claimed. |
| PI-12 | PASS (implementation scope) | Autonomy modes, operator authorization, kill switch, recovery-to-disabled, NM-08/NM-10 handoffs and duplicate-order prevention. |
| PI-13 | PASS | Isolated RL environment. No policy promotion path. |
| PI-14 | PASS | Reward calculation and policy evaluation; reproducibility and chronological evaluation. |
| PI-15 | PASS (software scope) | Isolated training and optimisation. Experimental policies remain advisory. |
| PI-16 | PASS (prior report) | Adaptive learning, regime suitability, drift and degradation monitoring; advisory only. |
| PI-17 | PASS | Eight existing specialist agents, read-only evidence and no execution authority. The event context now includes PI-24 evidence. |
| PI-18 | PASS (prior report) | Portfolio optimisation and capital allocation; advisory target weights. |
| PI-19 | PASS (scoped) | Financial research, source provenance and reported/derived/interpretation separation. |
| PI-20 | VERIFIED PASS with limitations | Prediction dashboard, SSE activity channel and explainability; live-data limitations recorded. |
| PI-21 | PASS | Portfolio management, position intelligence, opportunities, capital/cash, rebalancing and decision history; no direct financial mutation. |
| PI-22 | PASS | Advanced risk intelligence; NM-08 remains sole risk authority; event risk now reaches PI-22 through a read-only loader. |
| PI-23 | PASS | Forecast optimisation, calibration, uncertainty, disagreement, drift, degradation, reliability and fallback; no model promotion. |
| PI-24 | NEW — this report | Event intelligence plus final integration verification. |

No earlier phase was rebuilt. PI-24 adds one advisory package and read-only
handoffs.

---

## 2. PI-24 implementation summary

New package: `novamarket/src/novacontrol/novamarket/event_intelligence/`

- `models.py` — event groups, statuses, relevance classes, warning statuses,
  impact/outcome statuses, bounded configuration, source adapters and helpers.
- `store.py` — isolated append-only `ei_records` and `ei_config` SQLite store,
  with update/delete triggers, integrity checking and full-count pagination.
- `service.py` — normalization, synchronization, relevance, warnings, impact,
  point-in-time features, forecast context, outcomes, outcome study, timeline,
  agent evidence, portfolio context, analysis, overview, history, health,
  contract, navigation and configuration.
- `__init__.py` — public exports.

Integration touch points:

- `NovaMarketService` owns the lazy service, isolated store path, read-only
  source adapters and shutdown close.
- `risk_intelligence/service.py` accepts an optional `event_loader`; event
  warnings are appended to PI-22 alerts while NM-08 still decides.
- `multiagent/service.py` forwards `market.events`; the Market Research Agent
  reads verified and unverified events separately.
- `api/app.py`, `api/models.py`, `api/route_consumers.py` and `docs/API.md`
  expose and document the new routes.
- `web/src/pages/EventIntelligence.tsx`, `web/src/nav.ts` and `web/src/App.tsx`
  add the eight-view Event Intelligence workspace.

---

## 3. Event intelligence implementation

### 3.1 Ingestion, normalization and provenance

PI-24 reads the existing NM-04 radar through `NovaMarketService._ei_events`,
`_ei_event` and `_ei_event_history`. It does not add a feed, scraper or research
platform. Normalized events carry:

- event id, event key and immutable version;
- group (`corporate`, `economic`, `market`, `company_specific`, `unclassified`)
  and the original subcategory;
- title, description, category, status, radar status and verification;
- affected instruments, resolved company names, sectors, exchange and market;
- separate event date (`occurred_at`), publication, retrieval, discovery and
  update timestamps;
- source references from the event detail evidence, including publisher, URL,
  publication/retrieval times, reliability and retrieval status;
- verification reasons/conflicts, importance, confidence, mappings and related
  events;
- explicit `instruction_risk`, `is_price_prediction=false` and
  `is_trading_recommendation=false`.

The original radar row is never modified. `sync` writes one record per
event/version (`eieve_<event_id>_v<version>`) and is idempotent. Malformed rows
are returned in `skipped` with a reason.

Status mapping is evidence-based:

| NM-04 evidence | PI-24 status |
|---|---|
| `officially_confirmed`, `multi_source_corroborated` | `verified` |
| `single_source`, `unverified` | `unverified` |
| `conflicting_evidence` | `conflicted` |
| `retracted` / verified expiry | `expired` |
| radar `resolved` | `completed` |
| explicit radar `duplicate` / `irrelevant` | `duplicate` / `irrelevant` |
| unresolved extraction | `detected` |

### 3.2 Categories

NM-04's declared subcategories map into the required families:

- **Corporate:** earnings, guidance, management change, M&A, restructuring,
  buyback, dividend, stock split/bonus, fundraising/rights, listing/delisting.
- **Economic:** inflation, interest rate, central bank, GDP, employment, fiscal
  policy, budget, currency, sovereign debt, trade policy, geopolitics.
- **Market:** commodity prices, exchange announcement, trading halt, index
  change, liquidity, circuit breaker, infrastructure incident.
- **Company-specific:** product launch, regulatory investigation, litigation,
  bankruptcy, contract, partnership, sector regulation, demand, supply chain,
  technology, earnings trend, sector investment, competition.

No ordinary news is promoted to a major event by PI-24. Importance remains the
radar's declared importance; PI-24 does not invent a magnitude.

### 3.3 Relevance

Relevance classes are `direct`, `sector_related`, `market_wide`, `indirect`,
`not_relevant` and `insufficient_evidence`. The order is evidence-based:

1. A verified direct instrument mapping is `direct`.
2. A shared target/event sector is `sector_related`.
3. A declared economic/market family with at least moderate importance is
   `market_wide`.
4. An explicit indirect mapping is `indirect`.
5. A corporate event with no instrument/sector link is `not_relevant`.
6. Missing timestamps, a future publication, or no usable linkage is
   `insufficient_evidence`.

Regime and horizon context are attached as `market_regime` and
`horizon_effect`; they do not become a direction. `direction` is always
`not_estimated`. Sentiment is never mapped to up/down.

### 3.4 Impact, timeline and outcomes

Impact reports channels (fundamental expectations, post-announcement repricing,
capital structure, liquidity/execution, discount rate/macro, market-wide risk,
uncertainty) and observed historical outcomes when at least
`min_outcome_samples` exist. It reports `direction=not_estimated`,
`price_adjustment=none`, `calibrated_event_probability=null` and
`causation_established=false`.

Outcomes use stored OHLCV only:

- resolution is `5m`, `1h` or `1d` according to the horizon;
- bars must be valid/non-partial and observed at or before `as_of`;
- at least two pre-event bars and one post-event bar are required;
- post-event bars are bounded by the horizon (not an arbitrary 30-day window);
- the result records pre/post return, volatility change and, when a stored
  forecast exists at or before the event, the latest forecast comparison and
  prediction error;
- PI-11 evaluation reports are attached as historical evaluation context, not
  as proof that the event caused the outcome;
- `correlation_only=true` and `causation_established=false` are always present.

The outcome study groups immutable outcome records by horizon and marks a group
insufficient below `min_outcome_samples`. Timeline returns event versions,
publication/event dates, verification and transitions.

---

## 4. Event-aware feature and forecasting evidence

`features` produces `pi24-event-features-1.0`, including:

- `event_count_5d`, `event_count_20d`, verified/unverified counts;
- maximum importance/relevance, event frequency, sector count;
- `event_upcoming_count`, `event_proximity_days`, `event_age_days`;
- `event_time_since_publication_hours`, market-wide and uncertainty flags;
- earnings, guidance, dividend, corporate-action, regulatory, litigation,
  trading-halt, rate-decision, inflation and GDP flags;
- `event_historical_abs_return_mean` from observed outcomes only.

Each feature has `value`, `status` (`value`, `zero`, `missing`, `invalid`),
`version` and `source`. The snapshot has `point_in_time=true`,
`future_outcomes_included=false`, provenance event ids and `pi04_rows` in the
existing `news_events` shape. PI-04 already reads the NM-04 radar for its
`news_*` features; PI-24 does not add a second feature pipeline.

`forecast-context` is deliberately non-mutating:

```text
event_adjustment.applied       = false
price_adjustment               = none
direction                      = not_estimated
calibrated_event_probability   = null
abstention_recommended         = false
model_version_changed          = false
forecast_mutated               = false
```

Event evidence can raise warnings or suitability but cannot rewrite a forecast,
force abstention when evidence is insufficient or promote a model. PI-09/PI-10
forecast identity, target timestamps and immutable versions are untouched.

`forecast-evaluation` measures event-aware against non-event-aware forecast
error instead of asserting an improvement:

- the records come from PI-11's own collector
  (`PredictionEvaluationService.collect_records`), so they are the same stored,
  filtered and de-duplicated lifecycle/NM-06 records a PI-11 report scores — a
  second record pipeline was not created;
- a record is event-labelled when it declares event awareness
  (`metadata.event_aware`, a non-empty `metadata.event_ids`, or an event
  feature-set version) or when a stored event for that instrument was published
  inside the point-in-time window
  `[generated_at - lookback_days, generated_at]`;
- both cohorts are scored with PI-11's `return_metrics`, `price_metrics` and
  `direction_metrics`; the comparison reports return MAE per cohort, the delta,
  the relative change and a verdict — `event_aware_better`,
  `non_event_aware_better`, or `no_material_difference` at a 2% threshold, and
  `not_comparable` when a cohort has no scorable records;
- both cohorts must reach `min_evaluation_samples` (default 20). Otherwise the
  stored result is `status: insufficient_data`, `comparison: null`,
  `improvement_claimed: false`, with a reason naming each cohort's sample count.
  A verdict is never inferred from a short sample.

`causation_established` is always false and `correlation_only` always true: the
comparison measures stored forecast error, not event causality.

Local result: against the working-tree data the endpoint returns
`insufficient_data` (0 event-aware and 0 non-event-aware scorable records; two
records were rejected by PI-11's own filters), so **no improvement is claimed
for this data**. The measured-comparison path is exercised by deterministic
fixtures in `tests/test_novamarket_pi24.py::EventAwareEvaluationTests`
(measured improvement, explicit insufficient data, no material difference,
point-in-time proximity labelling, declared labelling and persistence).

---

## 5. Final architecture overview

The complete prediction path is:

```text
NM-02 market data / NM-04 events / PI-19 research
        │
        ├── PI-04 features and regimes
        ├── PI-05/06/07 statistical, gradient, LSTM
        ├── PI-08 ensemble and calibration
        ├── PI-09/10 intraday, swing, long-term lifecycle
        ├── PI-11 evaluation
        └── PI-24 event relevance, warnings and point-in-time features
                    │
                    ├── PI-17 agent evidence
                    ├── PI-22 risk context (NM-08 decides)
                    └── PI-23 selection/optimisation context
                              │
PI-18/21 portfolio ── PI-12 autonomy decisions ── NM-08 ── NM-10 paper execution
```

No duplicate prediction orchestrator, risk engine, execution engine, agent
coordinator or portfolio ledger was created.

---

## 6. Prediction pipeline integration

Verified against existing contracts and focused tests:

- historical datasets and point-in-time feature cutoffs;
- feature schema/version and regime detection;
- statistical, gradient and LSTM model contracts;
- ensemble/calibration/uncertainty and model disagreement;
- intraday, swing and long-term targets;
- model reliability, drift and degradation evidence;
- forecast evaluation and provenance.

`test_novamarket_features.py`, `test_novamarket_statistical.py` and
`test_novamarket_ensemble.py` passed: **96 tests, 14 subtests**.

---

## 7. Forecast lifecycle integration

PI-24 does not write the lifecycle. The existing scheduler and outlook suites
passed (`test_novamarket_scheduler.py`, `test_novamarket_outlook.py`: 42 tests).
The following contracts remain as previously verified:

- stable forecast-cycle IDs and immutable versions;
- T−20, T−5, T−1 and target evaluation checkpoints;
- separate generation, target and market-data timestamps;
- model and calibration version provenance;
- missed checkpoints remain missed and stale data remains stale;
- no future data is injected into a forecast;
- the 15-minute general market-data floor and approved intraday path are
  unchanged.

No PI-24 endpoint can write or delete a lifecycle record.

---

## 8. Portfolio integration

PI-18 and PI-21 suites passed:

- PI-18: **43 passed**;
- PI-21: **78 passed**.

The verified boundaries are:

- recommendations read current authoritative portfolio state;
- forecast uncertainty affects suitability;
- position actions are `maintain`, `reduce`, `exit`, `wait` or
  `review_required`;
- rebalancing compares current and target weights but remains advisory;
- concentration, cash, exposure and turnover limits are reported;
- there is no direct target-weight application, forced end-of-day liquidation
  or short-selling path.

PI-24 adds read-only event exposure through `portfolio_context`; it never
changes holdings, cash or target weights.

---

## 9. Risk integration

PI-22 suite passed: **41 passed**.

- NM-08 remains the sole authoritative risk engine.
- PI-22 records NM-08 decision references and never creates or overrides a risk
  decision.
- PI-24 warnings become PI-22 event alerts with `direction=not_estimated` and
  no probability claim.
- `risk_authority` is reported as `NM-08` by the PI-24 contract, warning,
  impact, portfolio and agent surfaces.
- PI-24 creates no risk decision and no order authority.

---

## 10. Autonomous trading integration

PI-12 suite passed: **29 passed**.

Verified controls retained:

- modes `disabled`, `monitoring`, `autonomous`, `paused`;
- server-side operator authorization;
- persistent cycle identity and one cycle at a time;
- idempotent requests and duplicate-order prevention;
- kill switch blocks new actions and does not claim submitted orders were
  cancelled;
- recovery always returns to `disabled`;
- outstanding-order reconciliation remains required;
- NM-08 assessment precedes any eligible paper decision;
- NM-10 remains the only paper-execution authority;
- no broker connection, live trading, real money, short selling or forced EOD
  liquidation.

PI-24 adds only event evidence to the decision context. It cannot submit,
cancel or modify an order.

---

## 11. RL and adaptive intelligence integration

PI-13, PI-14 and PI-15 suites passed: **104 tests, 3 subtests**. PI-16 passed:
**41 tests** in the cross-phase batch.

Confirmed:

- isolated training environments;
- reproducible reward calculation and chronological evaluation;
- policy performance and regime suitability monitoring;
- concept-drift and degradation detection;
- advisory policy recommendations only;
- no automatic policy promotion or activation;
- no RL bypass of PI-12, NM-08 or NM-10.

---

## 12. Dashboard and API integration

### 12.1 API

PI-24 adds 23 authenticated routes under `/market/event-intelligence/*`:

- reads: status, contracts, navigation, config, events, event detail, timeline,
  portfolio context, overview, history, health;
- operator writes/analyses: config update, relevance, impact, warnings,
  features, forecast context, outcomes, sync, ingest, analyze;
- read-only analyses: outcome study, agent evidence.

The route contract is synchronized in `ApiSurface`, `ROUTE_CONSUMERS` and
`docs/API.md` at **931 routes**. `tests/test_api_reference_sync.py` passed:
**6 tests, 931 subtests**. `tests/test_novamarket_api.py` passed:
**62 tests, 347 subtests**.

Security checks: unauthenticated reads return 401; evidence-writing actions
without the operator token return 403; no route contains an order, fill,
execution, broker or ledger path.

### 12.2 Frontend

`web/src/pages/EventIntelligence.tsx` is mounted at `#/eventintelligence` and
uses the existing API client, `useApi`, UI components and operator-token
pattern. It has eight views:

1. Overview — verified/unverified/conflicting counts, current warning, events.
2. Timeline — event date and publication time separately.
3. Relevance — direct/sector/market/indirect/insufficient assessment.
4. Impact — channels, historical samples, no direction or adjustment.
5. Warnings — horizon-aware status and event ids.
6. Features — versioned point-in-time values and statuses.
7. Outcomes — recorded outcome and reproducible study.
8. Agent evidence — verified/unverified separation and no execution authority.

The page does not calculate probabilities or synthesize prices. The frontend
build (`npm run build`, `tsc -b && vite build`) passed. The page was **compiled
but not visually inspected in a browser** in this pass.

---

## 13. Database consistency

- PI-24 uses its own SQLite database, separate from NM-04, NM-08, NM-10 and the
  portfolio ledger.
- `ei_records` is append-only: update and delete triggers abort.
- Record IDs are stable; repeated sync/relevance/warning/feature/outcome calls
  return the existing immutable record rather than duplicating it.
- `ei_config` is the only mutable PI-24 table.
- Existing forecast, model, portfolio and decision stores were not migrated or
  redesigned.
- Restart recovery cannot create a duplicate PI-24 record with the same stable
  id.

Focused immutability and integrity tests passed.

---

## 14. Performance measurements

Local synthetic benchmark on the development machine, in-memory PI-24 store,
6-event fixture (60 samples per operation):

| Operation | p50 | p95 | p99 |
|---|---:|---:|---:|
| Relevance | 0.25 ms | 0.53 ms | 0.58 ms |
| Features | 0.35 ms | 0.51 ms | 0.55 ms |
| Warnings | 0.20 ms | 0.23 ms | 0.24 ms |
| Impact | 0.08 ms | 0.10 ms | 0.13 ms |
| Agent evidence | 0.34 ms | 0.59 ms | 0.60 ms |

Synthetic 200-event normalization/sync: p50 **5.92 ms**, p95 **15.34 ms**;
200-event feature snapshot: **12.52 ms**.

These are internal deterministic measurements only. Provider publication delay,
live market-data latency, real event-feed throughput, network latency and
dashboard browser timing were not measured in this pass and must not be inferred
from these numbers.

---

## 15. End-to-end workflow evidence

| Workflow | Verification basis | Result |
|---|---|---|
| A — Market prediction | Features (35), statistical (33), ensemble (28) and scheduler/outlook suites | Passed for deterministic/stored-data paths; no live-provider run |
| B — Forecast lifecycle | Scheduler and outlook suites, prior PI-09/PI-10 evidence | Passed for checkpoint, timestamp and immutability contracts |
| C — Event-aware prediction | PI-24 suite (30) including point-in-time features, warnings, impact, outcomes, agent/risk handoffs and the event-aware evaluation comparison | Passed |
| D — Portfolio management | PI-18 (43) and PI-21 (78) suites | Passed for advisory portfolio state, positions, capital and rebalancing |
| E — Risk protection | PI-22 (41) plus PI-24 event-risk handoff tests | Passed; NM-08 authority preserved |
| F — Autonomous paper trading | PI-12 (29) plus prior AT/NM-10 evidence | Passed for modes, authorization, kill switch, handoff and no duplicate-order path; no live paper order submitted in this pass |
| G — Adaptive intelligence | PI-13 (40), PI-14 (45), PI-15 (19), PI-16 (41) | Passed; no automatic promotion |
| H — Recovery | PI-12 recovery tests and prior lifecycle evidence | Passed; recovery returns to disabled |

The application-level API tests open the real FastAPI app and verify PI-24
service initialization, authentication and operator rejection. PI-24 unit tests
drive the real service with deterministic adapters; they do not mock successful
forecast/risk/order responses.

---

## 16. Security and financial-safety verification

| Requirement | Evidence | Status |
|---|---|---|
| `paper_only=true` | `autonomy/models.py`, runtime/status payloads, PI-12 tests | Verified |
| `live_trading=false` | same | Verified |
| `broker_connected=false` | same; route contract forbids `broker` paths | Verified |
| NM-08 sole risk engine | PI-22 contract, PI-24 authority contract, PI-22 tests | Verified |
| NM-10 sole paper execution | PI-24/API contracts, PI-12 tests | Verified |
| Portfolio accounting authoritative | PI-18/PI-21 tests | Verified |
| No direct autonomous ledger mutation | PI-12/PI-21 tests | Verified |
| No direct autonomous position mutation | PI-21 tests | Verified |
| No broker integration | runtime flags and route allowlist | Verified |
| No real-money trading | `paper_only`, `live_trading=false` | Verified |
| No short-selling | PI-21 scope | Verified |
| No forced EOD liquidation | PI-21/AT-05 tests | Verified |
| No automatic target-weight rebalancing | PI-21 advisory rebalancing tests | Verified |
| No automatic RL policy promotion | PI-13–PI-16 tests | Verified |
| Operator authorization server-side | API 401/403 tests; no UI-only gate | Verified |
| Kill switch blocks new actions | PI-12 tests | Verified |
| Recovery returns to disabled | PI-12 tests | Verified |
| Protective exits remain operational | PI-12/AT-05 tests | Verified |

---

## 17. Actual test counts

| Check | Result |
|---|---|
| PI-24 focused suite `tests/test_novamarket_pi24.py` | **30 passed** |
| PI-17 + PI-22 + PI-23 | **107 passed** |
| PI-12 + PI-18 + PI-20 + PI-21 | **194 passed** |
| Scheduler + outlook + evaluation + PI-16 + PI-19 | **155 passed** |
| PI-13 + PI-14 + PI-15 | **104 passed, 3 subtests passed** |
| Features + statistical + ensemble | **96 passed, 14 subtests passed** |
| NovaMarket API (`test_novamarket_api.py`) | **62 passed, 347 subtests passed** |
| API reference sync | **6 passed, 931 subtests passed** |
| **Total focused verification** | **748 passed, 1,295 subtests passed** |
| Ruff | focused PI-24, event package and edited API/risk/multi-agent targets: **all checks passed** |
| mypy | PI-24 package: **4 files clean**; PI-24 plus service/risk/multi-agent/API targets: **16 files clean** |
| Frontend build | `npm run build` passed (`tsc -b && vite build`; existing chunk-size/dynamic-import warnings only) |

The full 2,254-test repository suite was intentionally not run. The figures
above are the focused and affected-area evidence actually executed.

### 17.1 Event-aware evaluation addendum

Added after the initial completion pass, when the missing comparison was
implemented:

| Check | Result |
|---|---|
| `tests/test_novamarket_pi24.py` (incl. 6 new `EventAwareEvaluationTests`) | **30 passed** |
| `tests/test_novamarket_evaluation.py` + `test_novamarket_pi24.py` + `test_novamarket_api.py` | **117 passed, 349 subtests passed** |
| mypy (`event_intelligence`, `evaluation`, `service.py`, `--platform win32`) | **10 files clean** |
| Live route smoke through `TestClient` | `POST /market/event-intelligence/forecast-evaluation` → 200 `insufficient_data`; unauthenticated → 403 |
| Route registration | route added to `ApiSurface`, `ROUTE_CONSUMERS` (`no-render`) and the exact `/market/` allowlist test |

`docs/API.md` could not be regenerated: `scripts/generate_api_reference.py`
already reports the file out of sync at HEAD and would drop 185 documented
routes, because it only emits `ApiSurface` entries while the market surface is
registered in `api/app.py`. That desync predates this change and was not
papered over; the new route is registered in the two registries the contract
tests read.

---

## 18. Test failures and skips

- One affected regression test initially failed:
  `NovaMarketRouteContractTests.test_no_live_execution_route_exists`. Its exact
  route allowlist had not been updated for the PI-23 and PI-24 analytical route
  families. The exact allowlist was updated; the contract test then passed
  (**3 passed, 339 subtests**), and the full API file passed
  (**62 passed, 347 subtests**).
- PI-24 development tests exposed three fixture/Windows issues: a slotted
  dataclass cannot be spread with `__dict__`, warning precedence across mixed
  event fixtures, and a SQLite file handle still open during temporary-directory
  cleanup. Tests were corrected; no production behavior was weakened.
- No test was skipped in the final focused runs. No test was marked xfail, no
  assertion was deleted to obtain a pass, and no type/lint suppression was added
  to the PI-24 package.
- The full repository suite was not run, so skips elsewhere in the repository
  are not claimed here.

---

## 19. Defects fixed during PI-24 verification

1. Event features always labelled `event_count_5d` as `value` even at zero; it
   now reports `zero`.
2. Normalized events had no exchange and no related-event ids; exchange is
   resolved from the radar instrument view and related ids are preserved.
3. A single malformed event could poison later warning status through the
   service-wide error list; warnings now use per-call source errors.
4. Portfolio context did not validate its horizon; an invalid horizon could
   escape as an internal error. It is now canonicalized.
5. Feature and impact outcome statistics could include outcomes observed after
   the feature cutoff. Both now filter by `observed_at <= as_of`.
6. Intraday event outcomes used daily bars and a 30-day post-event window. They
   now use 5-minute/hourly resolution and bound post-event observations to the
   horizon.
7. Event outcomes did not compare stored forecasts before/after the event; the
   immutable forecast comparison and prediction-error fields were added.
8. PI-17's Market Research Agent did not receive `market.events`; the multi-agent
   context now forwards it and the agent keeps verified/unverified evidence
   distinct.
9. PI-24 agent evidence lacked risk/portfolio/RL policy detail and ignored the
   portfolio id; the evidence package now carries them without authority.
10. The FastAPI error mapper referenced `NotOperationalError` without importing
    it in the nested scope; the mapper now imports it and returns 503.
11. PI-24 routes were missing from the exact API safety allowlist; the allowlist
    was corrected while keeping the no-order/no-broker assertion.

---

## 20. Remaining limitations

1. **Event coverage is source-bound.** PI-24 consumes the sources already
   configured in NM-04. No paid provider, exchange-grade real-time feed or new
   scraper was added. Provider publication delay is not measured.
2. **Duplicate/conflict handling is primarily NM-04's.** PI-24 preserves
   explicit duplicate/irrelevant radar states and conflicting verification, and
   NM-04 remains the deduplication/fingerprint authority. PI-24 does not run a
   second independent duplicate detector.
3. **No calibrated event probabilities.** The service deliberately returns
   `calibrated_event_probability=null` unless a separately validated event-impact
   model exists. This is a safety boundary, not a missing random number.
4. **Impact is correlational.** Event outcome studies compare observed price,
   return and volatility changes; they never claim the event caused the move.
5. **Outcome studies require stored bars and forecasts.** Local data may be
   insufficient, especially for 5-minute/hourly horizons; the honest result is
   `insufficient_data`.
6. **No live production workflow was driven.** Workflows A–H are verified by
   focused deterministic/integration tests and prior phase evidence, not by a
   live market session, live event feed or submitted paper order in this pass.
7. **Frontend visual inspection was not performed.** The Event Intelligence page
   compiled successfully; browser rendering, responsiveness and click-through
   were not visually checked.
8. **Performance is synthetic.** The measurements in §14 are local in-memory
   service timings, not provider, database-at-scale or dashboard timings.
9. **Full suite not run.** The focused and affected-area evidence is reported
   instead of an indiscriminate whole-repository claim.
10. **PI-01 through PI-23 were reconciled from their reports plus targeted
    regression batches**, not re-audited line by line in this final phase.
11. **The event-aware comparison has no sufficient local sample.** The
    comparison is implemented and returns `insufficient_data` against the
    working-tree store; no accuracy improvement is claimed. Only fixture
    evidence proves the measured-comparison path.

---

## 21. Final roadmap acceptance verdict

**PI-24 verdict: PASS for the implementation and integration scope.**

All required PI-24 acceptance items are met at the implementation/integration
level:

- PI-01 through PI-23 evidence was reconciled and no prerequisite blocking
  defect was found.
- Event ingestion, normalization, classification, verification, relevance,
  instrument/sector mapping, impact, timeline, forecast integration and outcome
  tracking are implemented over existing NM-04/PI-19 evidence.
- Point-in-time features, non-mutating forecast context, event warnings,
  horizon-aware outcomes and the event-aware versus non-event-aware error
  comparison work and are tested.
- PI-09/PI-10 lifecycle contracts, PI-11 evaluation, PI-17 agents, PI-21
  portfolio, PI-22 risk and PI-23 optimisation integration remain intact.
- NM-08 remains the sole risk authority; NM-10 remains the sole paper-execution
  authority; portfolio accounting remains authoritative.
- RL/adaptive policies remain isolated and cannot be automatically promoted.
- The dashboard, API and database surfaces are integrated; API reference is
  synchronized at 931 routes.
- Recovery and financial-safety controls remain in place.
- Focused tests pass, defects found during verification were fixed, and the
  remaining limitations are disclosed above.

**Production/live-data qualification: PARTIAL.** A live event feed, live market
session, live paper-order submission, browser visual walkthrough and full-suite
run were not performed. Those are the boundaries of this PASS; they are not
claimed as verified.

**Roadmap:** the agreed NovaMarket Prediction Intelligence roadmap (the original
15 phases, the 8 additional accepted phases, PI-23 and this PI-24 closure phase)
is **COMPLETE for the implemented scope**. No PI-25 or further expansion phase
is created.
