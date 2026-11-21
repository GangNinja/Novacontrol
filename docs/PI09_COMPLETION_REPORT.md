# PI-09 — 20-Minute Forecast and Confirmation Scheduler Completion Report

**Branch:** `feature/novamarket`
**Commit:** none created (the worktree is intentionally left dirty)
**Date:** 2026-11-15

## 1. Initial repository assessment

- Git branch confirmed as `feature/novamarket`; no files were staged, committed,
  pushed, tagged or published.
- Existing PI-01 through PI-08 implementation and their completion reports were
  present in the working tree before PI-09 began. PI-08 had a documented Scoped
  PASS from its focused evidence; no prerequisite was found to be failing.
- The relevant infrastructure was inspected before implementation:
  - PI-01: `prediction/models.py`, `prediction/store.py`,
    `prediction/service.py`, lifecycle/stage/evaluation contracts and immutable
    SQLite tables.
  - PI-02: dataset membership and target conventions used by the forecast
    services.
  - PI-03: `marketdata/gateway.py`, checkpoint/freshness/source-timestamp
    contracts and the 900-second general refresh floor.
  - PI-04: feature/regime services and the authoritative
    `marketdata/calendar.py`.
  - PI-05 through PI-07: constituent forecasting services and model metadata.
  - PI-08: `ensemble/service.py`, eligibility, weighting, calibration,
    disagreement, abstention and fallback behavior.
  - Existing event bus, durable SQLite store, runtime lifecycle and API
    registries.
- No second market-data gateway, prediction engine, model registry, scheduler
  or forecast database was created.

## 2. Existing functionality reused

| Existing authority | PI-09 use |
|---|---|
| PI-01 `PredictionIntelligenceService` / `PredictionStore` | Lifecycle and immutable forecast outcomes, evaluation, persistence |
| PI-01 forecast contracts and stage definitions | Cycle/version contracts and T−20/T−5/T−1/T semantics |
| PI-03 `MarketDataGateway.checkpoint()` | Every checkpoint observation, freshness and source timestamp |
| PI-04 `calendar_by_mic()` / `session_bounds()` | Target and session validation, exchange-local times |
| PI-08 `EnsembleForecastingService.generate()` | The only forecast generator used at checkpoints |
| Existing event bus | Lifecycle/forecast/confirmation/evaluation evidence events |
| Existing SQLite database and transaction lock | Durable cycle/checkpoint/outcome state and atomic claims |
| Existing API registry and React navigation | Lifecycle API and Forecasts page integration |

The scheduler is an additive coordinate layer. It has no order, portfolio,
ledger, fill, risk or execution code.

## 3. Scheduler architecture

`novacontrol/novamarket/scheduling/` contains:

- `models.py` — cycle/checkpoint/version/outcome contracts, horizon policies,
  checkpoint kinds and scheduler configuration.
- `schedule.py` — verified calendar target/session calculation and
  `ScheduleValidationError` fail-closed behavior.
- `service.py` — `ForecastScheduler`, the single durable lifecycle coordinator.

New prediction-owned tables are `prediction_forecast_cycles`,
`prediction_forecast_checkpoints` and `prediction_forecast_outcomes`. Existing
lifecycle, forecast, stage and evaluation tables remain the forecast record.
The scheduler is opened once by `NovaMarketService.start_runtime()` and stopped
by `stop_runtime()`. The API cannot create a second scheduler authority.

The scheduler claims due checkpoints with a conditional SQL `UPDATE`, owns them
with a lease, executes a bounded batch, persists the real execution timestamp
and retries with bounded backoff. Expired leases return to the due queue after
a restart or worker failure. An already persisted version is reused on
duplicate delivery. Crash recovery repairs a version whose immutable row was
persisted before its stage/lifecycle attachment; recovery never overwrites the
version and never leaves the cycle without its attached id when repair is
possible.

The service also runs a bounded automatic cycle planner from the scheduler's own
tick loop and from the existing runtime tick. It considers held instruments
first, then watchlist members, then the registered instrument universe;
deduplicates `MIC:SYMBOL` identities; skips instruments with a live pending
cycle; and caps planning at `SchedulerConfig.auto_schedule_limit` (20). The
planner makes no provider calls and creates no order. A 60-second throttle
prevents repeated planning work while leaving the next 20-minute target ready
promptly after the prior target. Per-instrument planning errors are returned in
`auto_plan.errors` and do not block the remaining instruments.

## 4. T−20 / T−5 / T−1 implementation

For a canonical target timestamp T, `ForecastCycle` records instrument,
exchange, horizon, target, session open/close, session rule and policy version.
The cycle id is deterministic from instrument, exchange, horizon and UTC target
and is also the PI-01 lifecycle id.

| Checkpoint | Behavior |
|---|---|
| Initial (T−20 for 20m/1h) | Fresh PI-03 observation, current PI-08 generation, immutable initial version |
| First confirmation (T−5) | New observation and generation, comparison with initial version, immutable new version |
| Final confirmation (T−1) | New observation and generation, comparison with both prior versions, immutable final version |
| Outcome (T) | Validated actual observation, independent evaluation of every preserved version, outcome rows |

Each version has a deterministic id from cycle, stage and checkpoint. Forecast
versions are never overwritten. Missing, stale, future-dated or non-usable
observations produce skipped/unconfirmed checkpoints; the prior versions are
preserved. An ensemble abstention is recorded as abstained with its reasons and
no forced direction.

## 5. Target-time evaluation

`_run_outcome` requires T to have been reached and a validated, sufficiently
fresh observation. It rejects future observations and observations older than
the bounded reconciliation window, records the actual observation timestamp
and delay, and calls the existing PI-01 evaluation path for every preserved
forecast. The scheduler stores one `ForecastOutcome` per version with:

- predicted and actual price/return;
- absolute, signed and percentage price error;
- return error, direction correctness, neutral/uncertain/abstained counts;
- interval containment and width;
- observation delay and model/ensemble provenance.

If the actual observation is unavailable, the checkpoint stays
`evaluation_pending` and retries later. It is never filled with an unrelated
later price silently. At outcome time the source timestamp must fall inside
the verified target exchange session; an observation taken outside that
session is rejected and left pending. Forecast-side observations are checked
for future timestamps, instrument identity, freshness, usable quality and
strictly newer source time than the prior confirmation.

## 6. Horizon-specific scheduling

`HORIZON_SCHEDULE_POLICIES` defines all nine established horizons:

| Horizon | Target rule | Initial | First | Final |
|---|---|---|---|---|
| 5m | session-aware duration | T−4m | T−2m | T−1m |
| 15m | session-aware duration | T−12m | T−5m | T−1m |
| 20m | session-aware duration | T−20m | T−5m | T−1m |
| 1h | session-aware duration | T−20m | T−5m | T−1m |
| 1d/3d/5d/30d/90d | verified target session close | target session open | T−60m | T−1m |

The shorter intraday leads avoid meaningless T−20 checkpoints for a five-minute
horizon. The 20-minute lifecycle uses the full required T−20/T−5/T−1 policy.
The long-horizon policy preserves the same version and outcome principles
without pretending an intraday timer is a daily session.

Calendar validation rejects unknown exchanges, uncovered years, weekends,
holidays and targets outside the verified session. The same instant expressed
in exchange-local time maps to one UTC target.

## 7. Market-session and calendar evidence

PI-09 uses the existing PI-04 calendars through `calendar_by_mic`. It does not
infer sessions from weekdays. Tests cover:

- unknown exchange fail-closed;
- Saturday target fail-closed;
- target after NSE close fail-closed;
- one-day policy using verified next-session open and close;
- exchange-local test target conversion to UTC.

Calendar coverage is the existing verified 2026 set. A target in an uncovered
year is refused rather than approximated.

## 8. Forecast versioning evidence

The focused lifecycle test produces initial, first-confirmation,
final-confirmation and three independently evaluated outcome rows in one cycle.
It asserts the three distinct version ids, one lifecycle id and one evaluated
cycle. A file-backed test proves that a version survives store close/reopen and
that a second write with the same version id is refused. The existing PI-01
SQLite UPDATE/DELETE triggers and `UNIQUE(lifecycle_id, stage)` remain the
immutability enforcement.

## 9. Data-freshness and recovery evidence

- Checkpoints request `accepted_freshness=(FRESH,)` from the PI-03 gateway and
  additionally reject a stale/missing observation, an instrument mismatch, a
  future source timestamp and an observation not newer than the prior
  checkpoint.
- A missing observation test records skipped and creates no forecast.
- A stale source-timestamp test records skipped and does not call PI-08.
- A late T−20 test records the actual execution time and `late=True`; it does
  not rewrite the scheduled T−20 time.
- A target-passed test records skipped checkpoints and an unconfirmed cycle
  rather than executing old checkpoints as on-time confirmations.
- An unavailable outcome observation stays `evaluation_pending`; a later valid
  tick reconciles it.
- A file-backed reopen test proves cycle, checkpoint and forecast state remain
  available after a process restart.
- Lease expiry returns an owned checkpoint to the due queue.

## 10. Idempotency and concurrency evidence

- `claim_checkpoint` is a conditional update; a second owner receives `None`.
- Duplicate ticks reuse the existing immutable version and do not create a
  duplicate forecast.
- Retries preserve the same cycle id and checkpoint id; the focused retry test
  checks both.
- Expired lease recovery is tested directly.
- The scheduler has one per-application instance and a per-tick lock; there is
  no second scheduler authority.

## 11. API and frontend integration evidence

Eight authenticated routes were added to the existing `/market/forecasts*`
family. The existing forecast detail and outcome routes now also resolve PI-09
`pfc_` versions from the PI-01 lifecycle store, so a scheduled version can be
read or queried for its pending/evaluated outcome without pretending it lives
in the separate PI-08 forecast-record table:

- `POST /market/forecasts/cycles`
- `GET /market/forecasts/cycles`
- `GET /market/forecasts/cycles/{cycle_id}`
- `GET /market/forecasts/scheduler`
- `POST /market/forecasts/scheduler/tick`
- `GET /market/forecasts/missed`
- `GET /market/forecasts/evaluations/pending`
- `GET /market/forecasts/accuracy`

The tick route is bounded to due checkpoints and cannot accept arbitrary
checkpoint ids or execution controls. Unknown cycles return 404, while
scheduling/validation failures return 422 rather than becoming 500 errors. The
route registry, consumers and generated `docs/API.md` are in sync at 639
routes. The actual FastAPI application integration test creates and lists a
cycle, proves replay idempotency and invalid-target rejection, reads scheduler
health with its automatic-plan surface, missed checkpoints, pending evaluations
and accuracy through the app.

The React Forecasts page adds a lifecycle card with the checkpoint timeline,
preserved version details, revisions and target outcomes, using the existing
page/navigation patterns. `npm run build` (TypeScript + Vite) passes. The UI was
not visually inspected in a browser during this implementation; the build and
API contract are the available frontend evidence.

## 12. Tests and actual results

Commands run from `novamarket/`:

```text
PYTHONPATH=src python -m pytest tests/test_novamarket_scheduler.py -q
22 passed

PYTHONPATH=src python -m pytest tests/test_novamarket_scheduler.py \
  tests/test_novamarket_prediction.py tests/test_novamarket_ensemble.py \
  tests/test_novamarket_gateway.py tests/test_novamarket_marketdata.py \
  tests/test_novamarket_datasets.py tests/test_novamarket_statistical.py \
  tests/test_novamarket_gradient.py tests/test_novamarket_lstm.py \
  tests/test_novamarket_features.py tests/test_novamarket_forecasting.py -q
347 passed, 2 skipped, 49 subtests passed

PYTHONPATH=src python -m pytest tests/test_novamarket_api.py -q
52 passed, 240 subtests passed

PYTHONPATH=src python -m mypy src
Success: no issues found in 626 source files

PYTHONPATH=src python -m mypy src --platform win32
Success: no issues found in 626 source files

PYTHONPATH=src python scripts/generate_api_reference.py --check
docs\API.md: in sync (639 routes)

cd web && npm run build
built successfully (TypeScript + Vite; existing chunk-size warning only)
```

The focused tests use a controlled clock and deterministic observations. They
do not claim live-market scheduler execution. No forecast-accuracy target was
imposed on the implementation.

## 13. Defects fixed

- Resolved the circular import between the scheduling package and the
  prediction store by keeping scheduler-service imports out of the package
  initializer.
- Fixed the checkpoint due query: `CheckpointState.PENDING` serializes as
  `evaluation_pending`, and the SQL filter must include that exact value.
- Added the scheduler lifecycle to the actual application start/stop path once.
- Corrected the market-route contract expectation for the eight new routes.
- Fixed the scheduler health read to tolerate a gateway without a `status()`
  method and to type-check its returned mapping.
- Fixed a Windows file-backed test cleanup issue by closing the store before
  temporary-directory removal.
- Added API accuracy filters for cycle, model, model version, ensemble, target
  date and regime so history retrieval is not limited to instrument/horizon.
- Normalized cycle identity to uppercase `MIC:SYMBOL`; replaying an explicit
  target, including a target that has already passed, returns the stable cycle
  and checkpoint ids instead of generating a second lifecycle.
- Persisted attached initial/first/final/outcome ids even when the cycle state
  label did not change; repaired ids and stage evidence after a crash between
  immutable version persistence and lifecycle attachment.
- Made final-confirmation exhaustion explicit: missing observations retry, and
  terminal failed/dead-letter/skipped final checkpoints mark the cycle and PI-01
  lifecycle `unconfirmed`. A late final failure cannot downgrade an evaluated
  cycle.
- Added verified-session checking for actual outcome observations, strict
  future-timestamp rejection and current gateway availability/quality checks.
- Fixed filtered accuracy history and missing-evaluation counts, including
  stage/cycle/model/ensemble/regime scope, and fixed retrieval of stored outcome
  rows whose decimal fields are serialized as strings.
- Expanded revision evidence with uncertainty, interval, membership, freshness
  and ensemble changes while keeping it explicitly separate from accuracy.
- Fixed the Forecasts UI to use the canonical public output keys
  `predicted_direction` and `forecast_uncertainty`, and added lifecycle status
  tones.
- Added bounded automatic 20-minute cycle planning with held/watchlist/universe
  priority, replay-safe creation and a scheduler-tick callback so the next cycle
  is not delayed until the unrelated 15-minute market refresh.
- Routed PI-09 `pfc_` forecast detail and outcome lookups to the PI-01 lifecycle
  store; previously the PI-08 record fallback returned a false 404.
- Mapped scheduling/lifecycle validation failures to 422 and unknown cycles to
  404 instead of leaking them as 500 responses.

## 14. Remaining limitations

- Exchange-calendar coverage is the existing verified 2026 set. Uncovered
  target years fail closed.
- Automatic planning defaults to the 20-minute horizon. Other horizons are
  fully schedulable through the cycle API but are not auto-planned by the
  bounded planning policy.
- The automatic planner reads local held/watchlist/universe context and never
  performs provider discovery; an unregistered or calendar-less instrument is
  reported as a planning error rather than silently scheduled.
- The installed free/delayed providers may not supply a fresh observation at
  every checkpoint. The scheduler records the checkpoint as skipped,
  unconfirmed or pending rather than fabricating a value.
- PI-08 constituents derive their native target from their own horizon and
  generation time. PI-09 records that native target in metadata and uses the
  cycle target for immutable comparison and outcome evaluation. This is a
  visible horizon/target semantic limitation, not a reason to relabel or
  overwrite a version.
- No live-market scheduler run is claimed. Verification is controlled-clock
  and deterministic-observation evidence plus actual FastAPI application
  integration.
- The React lifecycle card was built but not visually exercised in a browser
  during this implementation.
- Existing unrelated worktree changes were preserved; nothing was staged,
  committed, pushed, tagged or merged.

## 15. Final acceptance verdict

**PI-09 PASS for the implemented forecasting/lifecycle scope, with the
limitations above.** All required lifecycle stages are implemented over the
existing PI-01 through PI-08 authorities; versions are immutable; freshness,
session and calendar failures are honest; scheduling is durable, idempotent,
automatic and restart-recoverable; scheduled forecast detail/outcome lookup,
API and build integration pass; and no order, risk, portfolio, ledger or fill
authority was added. The phase scripts and all affected PI-01–PI-08 suites are
green, with the two existing optional skips. PI-10 was not started.
