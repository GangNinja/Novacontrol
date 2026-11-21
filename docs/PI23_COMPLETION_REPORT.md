# PI-23 — Predictive Intelligence and Forecast Optimization: Completion Report

Date: 2026-11-18
Branch: `feature/novamarket`
Status: **PASS** (focused and affected-area verification; limitations below)

## 1. Scope and prerequisite assessment

PI-23 implements an advisory predictive-intelligence and forecast-optimization
layer over the existing PI-05 through PI-12, PI-16, PI-17 and PI-20
infrastructure. It does not add a statistical, tree, LSTM or ensemble model.
PI-08 remains the ensemble foundation, NM-08 remains the sole risk authority and
NM-10 remains the sole paper-execution authority. The worktree already contained
PI-22 and unrelated modifications; none were reverted.

The prerequisite reports PI-05 through PI-22 were inspected. PI-22 was already
marked PASS. No PI-24 work was started. Nothing was staged, committed, pushed,
tagged, merged or published.

## 2. Reused infrastructure

| Capability | Existing source |
|---|---|
| Statistical baselines and scorecards | PI-05 `forecast_runs` / `forecast_performance` |
| XGBoost/LightGBM models | PI-06 gradient service and registry |
| LSTM models | PI-07 LSTM service |
| Ensemble forecasts and calibration | PI-08 `ensemble_service()` |
| Lifecycle, scheduling and forecast freshness | PI-09 through PI-10 |
| Forecast evaluation reports | PI-11 `evaluation_service()` |
| Regime evidence | PI-04/PI-16 `latest_regime` |
| Model metadata | PI-01 `PredictionStore.models` |
| Dashboard patterns | PI-20 React components, `useApi`, navigation and toasts |

PI-23 owns only the isolated `fi_records` and `fi_config` tables in
`novamarket_forecast_intelligence.sqlite3`. It creates no second forecast store,
model registry, risk engine or execution engine.

## 3. Implemented package

`src/novacontrol/novamarket/forecast_intelligence/` contains:

- `models.py`: phases, versions, horizons, tasks, statuses, outcomes, authority
  contract, bounded `ForecastIntelligenceConfig`, read-only
  `ForecastIntelligenceSources`, `ModelEvidence` and typed errors
  (422/404/409/503).
- `metrics.py`: regression, classification, probability, interval, reliability
  bin, ECE, Platt/isotonic/temperature calibration, PSI, two-sample KS, drift,
  degradation and categorical reliability helpers.
- `store.py`: append-only `fi_records`, a single-row `fi_config` snapshot
  (configuration changes overwrite the snapshot and are not kept as history),
  immutability triggers, digest lookup, history, counts and integrity checks.
- `evidence.py`: read-only adapters over training runs, performance scorecards,
  forecasts, calibration, PI-11 evaluations, regimes and health. Callback
  failures become explicit evidence errors, never zero values.
- `service.py`: evaluation, selection, calibration, uncertainty, disagreement,
  multi-horizon consistency, drift, degradation, weights, reliability, fallback,
  registry, history, health, configuration and coordinated analysis.
- `__init__.py`: public package API.

`NovaMarketService` opens this layer lazily with its own path, status, contract,
configuration and analysis wrappers. Shutdown closes the store. No existing
forecast, risk or execution method was replaced.

## 4. Selection and leakage controls

- Selection candidates are ranked from **validation/walk-forward training
  evidence only**. Stored test scorecards are reported for monitoring and never
  affect the rank.
- A training report that declares `test_used_for_selection=true` has its
  validation evidence withheld; test results alone cannot make a model
  selectable.
- `as_of` cutoffs exclude later training runs and performance records.
- Forecasts are grouped by exact horizon and target; incompatible targets are
  excluded.
- Minimum sample thresholds and guardrails are applied before ranking.
- Calibration fits on the earliest chronological segment and is evaluated once
  on a later holdout. The holdout never chooses the calibration method.
- Outcomes are explicit: `selected`, `conditionally_selected`,
  `baseline_fallback`, `insufficient_evidence` and `no_eligible_model`.
- Every selection carries `automatic_promotion=false` and
  `test_used_for_selection=false`.

## 5. Uncertainty, disagreement and horizon consistency

Uncertainty separates empirical interval coverage/width from model, data, market
and regime limitations. Labels are documented heuristics, not calibrated
probabilities.

Disagreement uses only current, compatible forecasts clustered by target and
generation tolerance. Stale, expired, invalid and failed forecasts are excluded;
missing or insufficient evidence is reported as such.

Multi-horizon consistency preserves each horizon's own objective and target
ordering. Aligned and divergent pairs are reported separately; opposite
directions are not automatically called errors.

## 6. Calibration, drift, degradation and reliability

- Calibration supports Platt scaling, isotonic regression and temperature
  scaling, with a later-holdout before/after report. Calibration is never
  attached to a production forecast.
- Drift uses PSI and two-sample KS when explicit reference/current feature
  distributions are supplied. Otherwise it evaluates chronological residual
  behaviour. Unavailable input drift is named, persistence is required, and one
  window never deactivates a model.
- Degradation compares recent behaviour with a chronological baseline and
  reports an explicit health state.
- Reliability is categorical only (`high_reliability`,
  `moderate_reliability`, `low_reliability`, `unreliable`,
  `insufficient_data`). No numeric composite or probability is claimed.

## 7. Weights, fallback and abstention

Weight recommendations are bounded validation-rank changes capped at the
configured step. Every recommendation carries `applied=false`, and the response
carries `production_weights_mutated=false`; PI-08 weights are untouched.

Fallback recommends the preferred model, an eligible alternative, the existing
ensemble, an established baseline, abstention or `insufficient_data`. It never
creates a forecast, and abstention is a valid outcome.

## 8. Missing, stale and failed evidence

Missing inputs are explicit: `insufficient_data`, `stale_data`,
`no_eligible_model`, `unavailable`, `not_applicable` or evidence errors. A
missing scorecard is never interpreted as zero error. Source callback failures
are recorded on the status/result and do not crash an unrelated read. The
append-only store reports integrity on health.

## 9. API integration

There are 25 authenticated routes under `/market/forecast-intelligence/*`:

- reads: status, contracts, navigation, config, registry, history, overview,
  comparison, horizons, regimes and health;
- analytical writes: instruments, evaluate, select, calibration, uncertainty,
  disagreement, consistency, drift, degradation, weights, reliability, fallback
  and full analysis;
- configuration update.

Reads use `require_auth`; evaluation, selection, calibration, configuration and
full analysis use `require_autonomy_operator`. Every route is registered in
`api/app.py`, `ApiSurface` and `ROUTE_CONSUMERS` as `no-render`, and
`docs/API.md` is regenerated. The synchronized reference is **908 routes**.
`python scripts/generate_api_reference.py --check` reports in sync.

## 10. Frontend integration

The React **Forecast Optimization** workspace is
`web/src/pages/ForecastOptimization.tsx`, routed at `#/forecastoptimization`
under **Prediction Intelligence**. It has nine views: performance, comparison,
horizons, reliability, calibration, disagreement, drift, selection and fallback.
It shows authority, evidence counts, configuration limits, registry rows and
explicit missing-evidence guidance. It has no promotion, weight-application,
risk-decision or order control.

`npm run build` passes (`tsc -b && vite build`). The only warning is the
pre-existing chunk larger than 500 kB.

## 11. Verification

Focused tests: `PYTHONPATH=src python -m pytest tests/test_novamarket_pi23.py -q
-p no:warnings` → **31 passed**. Coverage includes:

- validation-only selection and test-scorecard exclusion;
- withheld validation when a report used test selection;
- explicit insufficient evidence and unknown-task rejection;
- chronological calibration holdout and insufficient probability pairs;
- PSI/KS drift without production deactivation;
- bounded advisory weights;
- categorical reliability without a numeric score;
- abstention without forecast fabrication;
- multi-horizon consistency;
- API surface, authentication, operator gate and actual application wiring;
- contract authorities and nine navigation views.

### 11.1 Defects found and fixed during the audit

- Evaluation persistence was deduplicated on a stable evidence fingerprint and
  now includes source-error tokens, so repeated evaluation of unchanged evidence
  updates one durable record instead of appending a near-duplicate. A repeated
  evaluation with a fixed clock now returns the same evaluation ID.
- Temperature calibration fitted on predicted class labels; it now fits on
  realised outcomes. Unknown calibration methods are rejected before the sample
  count is checked.
- Metric normalisation discarded legitimate `0.0` balanced accuracy, directional
  accuracy and F1 values; those zeros are now preserved.
- Forecast freshness could ignore a newer `evaluation_end`; the latest of the
  scorecard timestamp and evaluation end is now used, and attaching realised
  performance extends the evidence end.
- Compatible forecasts at the same resolution were treated as incompatible;
  exact-resolution forecasts are now accepted.
- Consistency and fallback could include expired, stale or invalid forecasts;
  both now operate on the same current-forecast filter. Disagreement uses the
  actual return range and a 0.50 two-way opposite-direction split as critical.
- Drift was reported as detected from one window; persistence is now required,
  and a single detected window is `drift_unconfirmed` with `detected=false`.
- History totals were truncated to the requested page length; totals now count
  the full filtered set. Batch analysis reports invalid instruments explicitly
  instead of silently dropping them.
- The API maps `NotOperationalError` to 503 and returns the corrected fallback
  authority shape. Frontend field mismatches were corrected (`counts.by_kind`,
  `participants`/`measurements`, selection horizon rows) and the full-analysis
  action and drift defaults were added.
- Unscoped registry queries collapsed rows when the same model ID existed for
  multiple instruments, and the unscoped training filter discarded every run
  because it compared each run horizon to an empty request horizon. Registry
  keys are now instrument/horizon/version-qualified and unscoped filtering only
  filters when a scope was requested. Registry-only rows remain visible per
  instrument.
- Uncertainty treated all-model evidence as current unless a specific model was
  named. It now aggregates stale models, reports `data_uncertainty=stale` when
  any considered evidence is stale, and reports unavailable rather than current
  when no evidence exists. Regime uncertainty aggregates the same way.
- PI-23 model and forecast reads no longer fail completely when the NM-06
  service is unavailable; the PI-01 registry and forecast contracts remain
  readable and the skipped source is recorded or logged.
- The API description claimed configuration change history that the store does
  not keep; it now describes the current bounded thresholds accurately.

Static checks: the PI-23 package and this test file pass focused `ruff check`
and `ruff format --check`, and `mypy src --platform win32` reports no errors in
708 source files. `tests/test_api_reference_sync.py` and `tests/test_api.py`
pass (**9 passed, 908 subtests passed**) with `docs/API.md` regenerated from the
local `src` tree. The frontend TypeScript/Vite build passes. A temporary
application smoke run exercised 17 representative PI-23 routes without a 5xx
response, including empty-evidence reads and analytical writes. Existing repository-wide
`ruff check` findings outside the PI-23 package were not changed; no broad
cleanup was performed. The full repository test suite was not rerun.

## 12. Limitations, remaining issues and unverified requirements

- The work uses deterministic synthetic evidence in tests; no live provider or
  production model artifact was evaluated.
- Reliability and uncertainty labels are documented heuristics, not calibrated
  probabilities.
- Feature-level drift requires explicit distributions; otherwise only
  chronological residual behaviour is available, and that limitation is shown.
- The Forecast Optimization page was built and type-checked but was **not
  visually inspected in a browser** in this audit.
- Configuration updates are a single-row snapshot; there is no configuration
  change history.
- Degradation is computed from PI-11 per-observation evaluation records. When
  only aggregate scorecards exist it returns `insufficient_data` rather than
  inferring degradation from unlike snapshots.
- The global registry lists one row per model, instrument and horizon rather
  than a version history.
- No deployment, multi-user concurrency or load test was performed.
- No automatic model promotion, production weight update, risk decision, order
  or financial mutation path exists by design.

No required PI-23 capability was found unimplemented during this audit. The
items above are limitations of the available evidence and of the focused
verification, not hidden passes: live-data behaviour, configuration history,
scorecard-only degradation and browser presentation remain unverified or
intentionally constrained.

## 13. Final verdict

**PASS.** PI-23 is implemented in the actual NovaMarket application, reuses the
existing forecasting, evaluation and registry stack, preserves NM-08 and NM-10
authority, prevents test-set and future-outcome leakage into selection, reports
missing evidence honestly, exposes the required API and dashboard views, and
provides bounded advisory optimization without financial mutation. The audit
found and fixed the defects above; 31 phase tests, the focused API contract
tests, static analysis and the frontend build pass. PI-24 has not been started.
