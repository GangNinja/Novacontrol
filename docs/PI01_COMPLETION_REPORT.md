# PI-01 Completion Report

**Programme:** NovaMarket Prediction Intelligence (PI-01–PI-16)  
**Phase:** PI-01 — Architecture, Contracts & Existing-System Integration  
**Report date:** 2026-11-13  
**Implementation branch:** `feature/novamarket`  
**Status:** **PASS for the PI-01 scope; PARTIAL for whole-repository regression** (see §8)

## 1. Executive summary

PI-01 is implemented as a real, importable prediction layer inside the existing
NovaMarket package. It adds versioned data/forecast/lifecycle/ensemble/learning
contracts, a read-only market-data gateway, an immutable T−20/T−5/T−1 lifecycle,
a model-eligibility and abstention boundary, SQLite persistence in the existing
NovaMarket database file, typed lifecycle events, and read-only decision
evidence.

The implementation does **not** create a second data authority, does **not**
place orders or fills, and does **not** mutate portfolios, the ledger, risk
policy or execution records. NM-08 remains the sole risk authority and NM-10
remains the sole paper-execution authority. NovaMarket remains paper-only.

No HTTP route was added. Prediction is exposed through `/market/status`,
`/market/contracts`, and the existing `NovaMarketService` service methods. This
keeps the pinned API surface unchanged while making the new layer reachable.

## 2. Scope, constraints and non-goals

### Delivered in PI-01

- Seven capability domains and the additive PI-01–PI-16 roadmap.
- Versioned contracts for requests, market snapshots, feature definitions and
  vectors, fundamentals, news/event evidence, historical datasets, model
  metadata and predictions, forecast responses, ensembles, lifecycle stages,
  evaluations, decision evidence and the learning boundary.
- Intraday 5m/15m/20m/1h, swing 1d/3d/5d and long-term 30d/90d horizon
  specifications with enforced resolution compatibility.
- Formal T−20 initial, T−5 first confirmation, T−1 final confirmation and T
  evaluation semantics, with immutable persisted versions.
- Missing/stale/conflicting data handling: snapshots become unusable and the
  confirmation is recorded as skipped/invalid; stale observations are never
  relabelled fresh.
- Ensemble eligibility, weighting, disagreement detection, calibrated-
  probability withholding and abstention.
- Read-only `DecisionForecastEvidence` with `authorizes_action=false`.
- Persistence foundations in the existing NovaMarket SQLite file.
- Typed prediction lifecycle events and a lazy service composition root.
- Canonical architecture documentation and this report.

### Explicit non-goals for PI-01

- Model training, statistical baselines, gradient boosting or sequence models.
- A production lifecycle scheduler or background daemon.
- Reinforcement-learning algorithms, training runs or automatic promotion.
- New HTTP routes or a complete prediction UI.
- Paid data providers, broker integration, live trading, direct orders/fills,
  portfolio or ledger mutation, risk-policy changes, or AT-07.
- Replacing or reordering the canonical NM-01–NM-14 roadmap.

## 3. Delivered implementation

| Area | Artifact | Behaviour |
|---|---|---|
| Contracts | `src/novacontrol/novamarket/prediction/models.py` | Versioned dataclasses, enums, horizon specs, policy validation, learning boundary |
| Data boundary | `prediction/data.py` | Read-only gateway over `MarketDataEngine`; source/receipt timestamps, freshness, conflicts, history sufficiency |
| Ensemble | `prediction/ensemble.py` | Eligibility filtering, weights, disagreement, uncertainty, calibrated probabilities, abstention |
| Lifecycle | `prediction/lifecycle.py` | T−20/T−5/T−1 scheduling, change detection, confirmation helpers |
| Persistence | `prediction/store.py` | Nine prediction tables, schema version, immutable triggers, deterministic JSON payloads |
| Orchestration | `prediction/service.py` | Create/confirm/evaluate, model registry, decision evidence, status/contracts |
| Integration | `novamarket/service.py` | Lazy prediction service on the existing market engine; status/contracts; shutdown |
| Permissions | `novamarket/permissions.py` | Four prediction operations/capabilities with `PI-01` policy phase |
| Events | `novamarket/events.py`, `core/events.py` | Eight typed prediction event types and payload contracts |
| Architecture | `novamarket/architecture.py` | Seven domains, sixteen-phase roadmap, ten storage boundaries, validators |
| Documentation | `docs/PREDICTION_INTELLIGENCE_ARCHITECTURE.md`, `docs/ARCHITECTURE.md` | Canonical PI-01 reference and integrated architecture section |
| Tests | `tests/test_novamarket_prediction.py` and updated pinned suites | 24 PI tests plus updated permission/event/capability counts |

## 4. Contracts and data semantics

`PredictionResponse` is the public response name and is the same immutable shape
as `ForecastOutput`. The main contracts are:

- `PredictionRequest`
- `MarketDataSnapshot`
- `FeatureDefinition`
- `FeatureVector`
- `FundamentalSnapshot`
- `NewsEventEvidence`
- `HistoricalMarketDataset`
- `PredictionEvidenceBundle`
- `ModelMetadata`
- `ModelPrediction`
- `ForecastOutput` / `PredictionResponse`
- `EnsembleResult`
- `ForecastLifecycle`
- `LifecycleStageRecord`
- `ForecastEvaluation`
- `DecisionForecastEvidence`
- `PredictionPolicy`, `AcquisitionPolicy`, `ResourcePolicy`
- `learning_boundary_contract()`

Every forecast carries its contract version, model/ensemble identity, member
versions, data snapshot id, feature-set/dataset version, generation timestamp,
underlying data timestamps, resolution, freshness, quality, validity, target and
an explicit `is_forecast=true` / `is_market_observation=false` marker. A forecast
also declares `authorizes_action=false`, `risk_authority=NM-08` and
`execution_authority=NM-10`.

A missing version is represented as `"unspecified"` or `"mixed"`, not as a
fabricated identifier. Probabilities are present only when every participating
model declares calibrated output and supplies all three directional values.

## 5. Lifecycle and ensemble behaviour

For a target `T`:

| Stage | Scheduled point | Persisted result |
|---|---|---|
| Initial | T−20 | Initial forecast and stage record, validity `unconfirmed` |
| First confirmation | T−5 | New immutable forecast version, changed fields recorded |
| Final confirmation | T−1 | New immutable forecast, validity `confirmed` when usable |
| Evaluation | T | One evaluation per preserved forecast |

The initial forecast is never overwritten. The initial window now closes before
the T−5 point; a lifecycle cannot be started after that point. A repeated
confirmation returns the already-recorded stage rather than creating a second
version. Missing or stale data at T−5 records `skipped`; at T−1 it records
`invalid` and the lifecycle becomes `unconfirmed`. No synthetic bar, price or
freshness value is produced.

The ensemble:

1. Rejects unusable or incompatible snapshots.
2. Excludes invalid, unregistered, unsupported, duplicate, future-generated or
   wrong-snapshot model contributions with an explicit reason.
3. Applies metadata weights and halves degraded models.
4. Enforces the participant limit and minimum model count.
5. Computes weighted return, interval and disagreement.
6. Abstains above the abstention threshold.
7. Withholds probabilities unless every participant is calibrated.
8. Emits an immutable `ForecastOutput`, or an `EnsembleResult` with no output.

## 6. Existing-system integration and authority boundaries

- `PredictionDataGateway` calls only `get_instrument`, `latest`, `bars` and
  `provider_health` on the existing `MarketDataEngine`. It has no provider,
  network, cache-mutation or refresh-policy code.
- `PredictionIntelligenceService` is opened lazily by `NovaMarketService` using
  the same database path as the market-data store and the existing event
  publisher/clock.
- `/market/status` includes prediction status; `/market/contracts` includes the
  prediction roadmap, domains, storage boundaries, data contracts and safety
  declarations.
- Four capabilities were added: `market.prediction.read`,
  `market.prediction.run`, `market.prediction.models` and
  `market.prediction.evidence`.
- Eight typed events cover lifecycle creation, model registration, abstention,
  forecast generation, confirmation, skipped confirmation, evaluation and
  decision evidence readiness.
- `DecisionForecastEvidence` is persisted append-only and always read-only. It
  does not call NM-08 or NM-10.

No route, order, fill, position, ledger or risk-policy path was added.

## 7. Persistence, performance and resources

Prediction tables live in the existing NovaMarket SQLite file:

`prediction_requests`, `prediction_lifecycles`, `prediction_forecasts`,
`prediction_stages`, `prediction_models`, `prediction_snapshots`,
`prediction_ensembles`, `prediction_evaluations`, `prediction_evidence`.

`prediction_meta.schema_version` is owned by the prediction layer. Forecast,
stage, evaluation and evidence rows are protected by SQLite UPDATE/DELETE
triggers. Model metadata is versioned and state-updatable. Requests, forecasts
and snapshots are insert-only. The prediction layer creates only its own tables;
it does not migrate unrelated financial tables.

`ResourcePolicy` declares lazy model loading, a two-model load bound, a 10-second
inference timeout, 30-second CPU and 512 MB memory budgets, prediction-owned
cache, failure isolation, no background scheduler and no distributed workers.
These are resource boundaries for later phases; PI-01 does not claim measured
latency or throughput for models it does not run.

The low-latency path is explicit in `AcquisitionPolicy`: 900-second general
refresh floor, 30-second intraday minimum interval, 20 requests/minute, at most
20 discovery instruments, at most two concurrent fetches, reserved priority
slots, request timeout/retry/failover limits, and visible provider health,
latency, conflicts and rate-limit state. It is a priority/caching boundary, not
a second provider authority and not a bypass of fair rotation.

## 8. Verification evidence

Environment: Windows, repository virtual environment, branch
`feature/novamarket`, working tree intentionally uncommitted.

### Commands and results

| Check | Command | Result |
|---|---|---|
| Focused PI suite | `cd novamarket && ../.venv/Scripts/python.exe -m pytest tests/test_novamarket_prediction.py -q` | **24 passed** |
| Affected regression | `pytest tests/test_novamarket_prediction.py tests/test_novamarket.py tests/test_novamarket_marketdata.py tests/test_novamarket_api.py tests/test_novamarket_forecasting.py -q` | **279 passed, 2 skipped, 319 subtests passed** in 118.80s |
| Focused mypy | `mypy src/novacontrol/novamarket/prediction src/novacontrol/novamarket/service.py src/novacontrol/novamarket/architecture.py --show-error-codes --no-error-summary` | **exit 0, no errors** |
| Service smoke | initialize a `NovaMarketService` with a temporary data directory, read status/contracts, shut down | `lifecycle=ready`; prediction `PI-01` initialised; 16 phases, 7 domains, 10 storage rows; capabilities 51; events 176; both validators returned `()` |
| API surface | Included in the affected regression | Route set remained the pinned **223**; capability count **51**; event count **176** |
| Ruff | `ruff` is not installed in the project virtual environment | **Not run** |

The two skips are the existing `LiveProviderTests` in `test_novamarket_marketdata.py`;
they require the explicit `NOVAMARKET_LIVE_TESTS=1` opt-in for real provider
calls and are not PI-01 failures.

### Additional audit fixes

The PI-01 verification pass added and re-ran tests for these correctness fixes:

- A snapshot supplied directly to `create_lifecycle` or `confirm` is now
  revalidated for instrument, exchange, stage age, freshness, quality, conflict
  and resolution, so a caller cannot bypass the gateway's stale-data rules.
- Lifecycle creation is idempotent even when a retry arrives after T−5; the
  existing lifecycle is returned instead of failing the retry.
- Evaluation is idempotent for the recorded actual price and rejects a later
  evaluation with a different actual outcome.
- The ensemble excludes a model whose family differs from its registered
  metadata and a model output generated at or after the forecast target.
- The ensemble abstains rather than constructing a forecast with a non-positive
  implied price.
- Model registration removes stale horizon rows when a version no longer
  supports a horizon.
- Acquisition validation now enforces the 30-second intraday floor, the
  20/minute request limit, the two-concurrent-fetch limit and fair rotation.
- `low_latency_contract()` reflects the service's configured acquisition policy
  instead of always reporting defaults.
- `ResourcePolicy` rejects disabled failure isolation and any background
  scheduler in PI-01.
- Forecast and decision-evidence payloads now carry the paper-only, no-live-
  trading and no-broker flags explicitly; the canonical
  `is_price_prediction=false` marker is preserved.
- Timezone-naive clocks and `at` values are rejected instead of being silently
  converted through the host timezone.

### Full-suite limitation

A complete `novamarket/tests` run was attempted in the background. It was still
at approximately 5% after several minutes and exceeded the tool's 295-second
per-command limit; it was stopped rather than allowed to run indefinitely. No
whole-tree pass is claimed. The affected regression suite above is the
verification basis for this report.

## 9. Acceptance criteria

| Criterion | Evidence | Verdict |
|---|---|---|
| Seven capability domains | `PREDICTION_DOMAIN_BOUNDARIES`, validator, smoke output | PASS |
| Additive PI-01–PI-16 roadmap | `PREDICTION_ROADMAP`, 16 unique phases | PASS |
| Versioned foundational contracts | `prediction/models.py`; PI contract tests | PASS |
| Intraday/swing/long-term horizons and resolution compatibility | `HORIZON_SPECS`, `accepts_resolution`, tests | PASS |
| Formal T−20/T−5/T−1/T lifecycle | `lifecycle.py`, service, lifecycle tests | PASS |
| Missing/stale data is never fabricated or reused as fresh | gateway tests, skipped/invalid lifecycle test | PASS |
| Ensemble eligibility, disagreement and abstention | ensemble tests and service path | PASS |
| Calibrated probabilities only | contract and ensemble tests | PASS |
| Read-only decision evidence; no execution authority | `DecisionForecastEvidence`, authority assertions | PASS |
| Persistence in the existing SQLite file with immutable versions | store schema, triggers, lifecycle tests | PASS |
| Existing-system integration, no duplicate data authority | `PredictionDataGateway`, `NovaMarketService` integration tests and smoke | PASS |
| Low-latency and resource boundaries explicit | `AcquisitionPolicy`, `ResourcePolicy`, contracts/status | PASS (boundary; no runtime scheduler) |
| API/frontend integration strategy | status/contracts exposure; documented future UI contract; no new routes | PASS for PI-01 scope |
| Canonical architecture documentation updated | both architecture documents | PASS |
| Focused tests and affected regression | 24 PI tests; 279 tests + 319 subtests passing (2 live-provider skips) | PASS for affected set |
| Whole-repository regression | full tree not completed within tool limit | PARTIAL / not claimed |
| No orders, fills, portfolio/ledger mutation, live trading, risk-policy change or AT-07 | code inspection, contracts, safety declarations, tests | PASS |
| No Git staging, commit, push, tag or merge | no Git commands that mutate history were run | PASS |

## 10. Limitations, deferred work and final verdict

### Known limitations

1. PI-01 does not train or run a forecasting model. Model outputs are supplied
   through `ModelPrediction`; later phases implement the model families.
2. There is no lifecycle scheduler daemon. The service provides the formal
   create/confirm/evaluate operations, and a later phase owns scheduling.
3. There is no prediction HTTP route or UI. The status/contracts payloads and
   service methods are the integration boundary until a later phase updates the
   route registry and generated API reference together.
4. `ResourcePolicy` and `AcquisitionPolicy` are enforced declarations; no
   measured model latency, memory or provider throughput is claimed.
5. The learning boundary is advisory only. No RL environment, reward
   implementation, training run or promotion exists.
6. The full NovaMarket test tree was not completed; the affected regression is
   green, but whole-repository regression remains unverified.
7. Ruff was unavailable in the environment, so lint was not run.

### Unverified requirements and remaining issues

| Item | Status |
|---|---|
| Real Yahoo/CoinGecko provider reachability | **Unverified**; the two `LiveProviderTests` require `NOVAMARKET_LIVE_TESTS=1` and were skipped |
| Real provider latency, rate-limit and fair-rotation behaviour | **Not exercised against a live provider**; the bounded policy and contracts are tested, not the external services |
| Model training, inference and measured resource budgets | **Deferred** to PI-04 onward; PI-01 supplies the model-output and resource contracts only |
| Lifecycle scheduler daemon | **Deferred** to PI-03; PI-01 supplies the create/confirm/evaluate runtime operations |
| Reinforcement-learning environment and policy evaluation | **Deferred** to PI-13/PI-14; only the advisory boundary contract exists |
| Dedicated prediction HTTP routes and frontend rendering | **Deferred**; the pinned route set is unchanged and status/contracts are the current integration surface |
| Whole-repository regression | **Unverified**; affected NovaMarket dependency/integration suites pass, but the complete tree was not completed |
| Lint | **Unverified**; Ruff is not installed in the environment |

None of these is represented as a completed PI-01 capability in the code or in
this report. The deferred items are explicitly outside the PI-01 scope.

### Final verdict

**PI-01 is complete for its defined architecture, contracts, lifecycle,
persistence and existing-system integration scope.** The implementation is real
and tested, not documentation-only or placeholder code. The affected regression
suite passes, focused typing is clean, and the service initialises and exposes
the prediction contracts from the existing NovaMarket runtime.

The correct qualification is: **PASS (PI-01 scope), PARTIAL (whole-repository
regression not completed).** PI-02 has not been started. No Git staging,
commit, push, tag or merge was performed.
