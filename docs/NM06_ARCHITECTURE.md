# NM-06 — Measured Probabilistic Forecasting: Architecture

NM-06 turns the market data NovaMarket already stores into **probabilistic
statements about future returns**, and then measures whether those statements
were any good. It is a read-only research subsystem: no order, no position, no
NovaCoin movement, no capital allocation.

Every claim in this document is implemented in
`src/novacontrol/novamarket/forecasting/` and pinned by
`tests/test_novamarket_forecasting.py`.

## Position in the pipeline

```
NM-02 completed daily bars ─┐
NM-04 confirmed events ─────┤   cutoff-safe features
NM-05 candidate score ──────┘            │
                                         ▼
                              deterministic estimators
                                         │
                    predicted return · P(up) · P(down) · P(sideways)
                    · 10th/90th percentile interval · regime · uncertainty
                                         │
                                         ▼
                       immutable ForecastRecord (SQLite, trigger-protected)
                                         │
                     target reached ─────┤
                                         ▼
                     ForecastOutcome (evaluated | unresolved)
                                         │
                                         ▼
               measured performance, calibration, model lifecycle
```

The subsystem **composes** the existing layers instead of duplicating them:

| Need | Reused component | Never re-implemented |
| --- | --- | --- |
| Price history | NM-02 `MarketDataEngine` / `MarketDataStore` | a second bar store |
| Session/exchange time zones | NM-05 `discovery.sessions.SESSION_TABLE` | a second calendar |
| Confirmed events | NM-04 `MarketEventRadar.instrument_events` | a second event pipeline |
| Candidate context | NM-05 discovery engine `candidate()` | a second scoring model |
| Events | the one `EventBus` + typed `EventType` vocabulary | a second bus |
| Permissions | `MarketPermissionModel` → shared `PermissionManager` | a second policy layer |
| Capabilities | `CapabilityRegistry` | a second registry |
| Persistence | the same SQLite file and state-store conventions | a second database |
| API | `/market/forecasts*` on the one application | a second service |

## Module map

| Module | Owns |
| --- | --- |
| `models.py` | `Horizon`, `HORIZON_SESSIONS`, `ForecastPolicy`, `ForecastRecord`, `ForecastOutcome`, `ModelRecord`, `ModelState`, transition table, id/digest helpers |
| `features.py` | `PriceBar`, `FeatureSet`, `compute_features`, `classify_regime` |
| `estimators.py` | `MODEL_IDS`, `MODEL_METADATA`, `forecast()` — the predictive distributions |
| `validation.py` | `walk_forward_origins`, `evaluate_models`, `best_model` |
| `calibration.py` | `CalibrationSample`, `isotonic_fit`, `IsotonicCalibrator`, `calibrate_probability` |
| `metrics.py` | MAE/RMSE/median error, directional accuracy, confusion, Brier, log loss, interval coverage/width, pinball loss, ECE |
| `store.py` | `ForecastStore` — immutable forecasts, outcomes, models, selections, training runs, performance, calibrations, reliability history |
| `engine.py` | `ForecastEngine` — `generate`, `evaluate_due`, `train`, `promote`, `check_health`, `status`, `contract`, `close` |

Each stage has exactly one owner. A change to a threshold is a
`ForecastPolicy` edit; a change to a model is an `estimators.py` edit; a change
to the reliability rule is a `check_health` edit. There is no second copy of any
of those decisions.

## Horizons

| Horizon | Trading sessions |
| --- | --- |
| `next_session` | 1 |
| `few_sessions` | 3 |
| `one_week` | 5 |
| `one_month` | 21 |
| `three_months` | 63 |

`target_at` skips weekends in the instrument's exchange time zone (from the
NM-05 session table, UTC for an unknown exchange). Sessions are counted, not
calendar days, so "one week" is five sessions and "one month" is twenty-one.

## One forecast, end to end

1. `generate(instrument_id, horizon)` resolves the horizon and takes a **cutoff**
   (the clock, or an explicit `at` for tests and reproducibility).
2. Instrument metadata comes from NM-02. An unknown instrument is
   `ForecastNotFoundError`.
3. Bars are read through `MarketDataEngine.bars(..., ascending=True)`, filtered to
   completed, valid, non-future bars. Partial and rejected bars are dropped.
4. Features are computed **only** from bars at or before the cutoff. Fewer than
   `min_history_bars` usable bars, or a newest bar older than
   `max_forecast_age_hours`, is `ForecastDataError` — no forecast is invented to
   fill the gap.
5. The selected model for this instrument and horizon is read from the store;
   with no selection the documented default `random_walk` is used.
6. The estimator returns a predictive distribution: predicted return, the three
   directional probabilities (summing to 1 ± 1e-6), and the 10th/90th percentile
   interval.
7. Confirmed NM-04 events published at or before the cutoff are recorded on the
   forecast. A high/critical confirmed event widens the interval by 1.25× — a
   conservative adjustment documented as such, **not** a validated accuracy
   claim. The NM-05 candidate score is recorded as provenance only and is never
   an estimator input.
8. A `ForecastRecord` is written with its cutoff, feature digest, model version,
   methodology version and payload digest, then `forecast.created` and
   `forecast.completed` are published. A publication failure never loses the
   stored forecast; it is reported in `status()["last_error"]`.

## Evaluation and reliability

`evaluate_due(at=...)` walks every forecast whose target has passed and:

- reads the completed bar at or after `target_at`;
- computes the realised return against the base bar at or before the cutoff;
- records direction, absolute error, multi-class Brier score, log loss and
  interval coverage in `ForecastOutcome`;
- writes a performance snapshot and publishes `forecast.evaluated` and
  `forecast.performance.updated`;
- leaves a forecast **waiting** when no bar has closed yet, and marks it
  **unresolved** only after `unresolved_grace_days` (10) — a missing bar is not a
  zero return.

An outcome is recorded once; a second record for the same forecast is refused by
the store. The forecast row itself is protected by SQLite triggers: `UPDATE` and
`DELETE` raise. Reliability therefore accumulates without ever rewriting
history.

`check_health()` degrades an `ACTIVE` model whose recent measured Brier exceeds
its baseline by `degrade_brier_margin` (0.05) or whose interval coverage falls
below `degrade_coverage_floor` (0.55), and suspends a model with
`suspend_unresolved` (10) unresolved outcomes. Reliability transitions are
stored as an append-only history (`forecast_reliability`).

## Storage

One SQLite file, opened by the service, with these tables:

| Table | Contents | Mutability |
| --- | --- | --- |
| `forecast_records` | the forecasts | append-only (triggers) |
| `forecast_outcomes` | one outcome per forecast | insert-once |
| `forecast_models` | model state and last measured metrics | upsert, state changes via the transition table |
| `forecast_selection` | selected model per instrument × horizon | replaced on training |
| `forecast_training_runs` | walk-forward runs, metrics, seeds, policy | append-only |
| `forecast_performance` | measured performance snapshots | append-only |
| `forecast_calibration` | calibration reports and isotonic knots | append-only |
| `forecast_reliability` | lifecycle transitions with reasons | append-only |

The store is the same infrastructure NM-02/03/04/05 use; NM-06 adds tables, not a
database.

## API surface

Eleven authenticated routes under `/market/forecasts`, all registered in
`api/app.py`, `ApiSurface` (`api/models.py`), `ROUTE_CONSUMERS`
(`no-render`) and `docs/API.md`:

```
POST /market/forecasts
GET  /market/forecasts
GET  /market/forecasts/status
GET  /market/forecasts/models
GET  /market/forecasts/performance
GET  /market/forecasts/calibration
POST /market/forecasts/train
POST /market/forecasts/evaluate
POST /market/forecasts/models/{model_id}/promote
GET  /market/forecasts/{forecast_id}
GET  /market/forecasts/{forecast_id}/outcome
```

`GET /market/status` reports the live `forecasting` block; `GET /market/contracts`
reports the forecast contract, models, policy and guarantees.

## Events

Eleven typed events, all declared in `core/events.py`, listed in
`IMPLEMENTED_EVENTS` and validated against `EVENT_PAYLOAD_FIELDS`:

`novamarket.forecast.created`, `.completed`, `.evaluation.pending`, `.evaluated`,
`.model.trained`, `.model.validated`, `.model.activated`, `.model.degraded`,
`.model.suspended`, `.calibration.completed`, `.performance.updated`.

Every payload names stored records or measured values; none contains a price
promise, a credential or a provider body.

## Permissions and capabilities

| Operation | Risk | Granted |
| --- | --- | --- |
| `READ_FORECASTS` | low | yes |
| `RUN_FORECASTS` | low | yes |
| `TRAIN_FORECAST_MODELS` | medium, reversible | yes |

Executing trades and managing a simulated portfolio remain denied. Four
capabilities are registered — `market.forecast.read`, `market.forecast.run`,
`market.forecast.train`, `market.forecast.models` — bringing the subsystem to 23.

## Failure behaviour

| Condition | Result |
| --- | --- |
| Unknown instrument | 404 (`ForecastNotFoundError`) |
| Unknown horizon, missing field, unmeasured promotion | 422 (`ForecastValidationError`) |
| Too little history, stale bars, unusable data | typed data error, refused rather than guessed |
| Event publication fails | forecast is stored; error recorded in status |
| No bar at the target yet | `waiting`, never a fabricated outcome |
| Promotion the lifecycle refuses | 409 (`ForecastStateError`) |
| Store closed / not initialised | `initialising`, with the reason |

## Known limits

- `ForecastPolicy.max_history_bars` is 320, but NM-02's `history_page_limit`
  (200) caps what the engine can read in one page, so a forecast currently sees
  at most 200 completed bars. That is above `min_training_bars` (140) but below
  the policy value; changing it means changing NM-02's page limit, not the
  forecasting policy alone.
- One instrument and one horizon per forecast. There is no cross-sectional or
  market-wide model, and no portfolio-level statement.
- No live market data was used to validate this build. Every figure in the test
  report comes from deterministic fixtures.
- The bootstrap is the slowest model; it is bounded by `bootstrap_paths` (400)
  and `max_bootstrap_bars` (400) rather than by a timing target.

## Non-goals

NM-06 does not: place or size orders (NM-10), allocate capital (NM-07), run
risk limits (NM-08), backtest strategies (NM-11), call a paid model provider, or
claim that a simple model beats the market. It produces calibrated,
reproducible, measured forecasts and reports honestly when the measurement is
insufficient.
