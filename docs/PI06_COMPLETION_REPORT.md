# PI-06 Completion Report — XGBoost and LightGBM Forecasting

**Branch:** `feature/novamarket` (nested NovaMarket repository)
**Status:** Implemented, integrated, tested and evaluated.
**Evidence class:** real historical bars stored in NovaMarket, plus deterministic synthetic unit fixtures for repeatable tests.
**Date:** 14 November 2026

This is the single consolidated PI-06 report. It does not replace the PI-05
report and does not start PI-07.

---

## 1. Initial repository assessment

The working tree was inspected before coding. PI-01 through PI-05 were already
implemented on `feature/novamarket` and their completion reports were verified as
PASS for their scopes. No prerequisite dependency failure was found.

| Prerequisite | Evidence inspected | Result |
|---|---|---|
| PI-01 prediction contracts | `PI01_COMPLETION_REPORT.md`, `prediction/` contracts, `PredictionStore`, forecast API | PASS |
| PI-02 historical datasets | `PI02_COMPLETION_REPORT.md`, `datasets/`, dataset versions/splits | PASS |
| PI-03 market-data gateway | `PI03_COMPLETION_REPORT.md`, `marketdata/gateway.py`, bar ingestion/quality | PASS |
| PI-04 features and regimes | `PI04_COMPLETION_REPORT.md`, `features/`, feature-set schema and regime detection | PASS |
| PI-05 statistical baselines | `PI05_COMPLETION_REPORT.md`, `statistical/`, forecast routes, baseline evaluation | PASS |

The root checkout is on `main` and was not the target. All work was done in the
nested `novamarket/` repository on `feature/novamarket`. Nothing was staged,
committed, pushed, tagged or merged.

## 2. Existing functionality reused

PI-06 deliberately composes the existing architecture:

- **PI-01 contracts** — forecast identity, horizons, direction, validity,
  timestamps, dataset/feature versions, model metadata and lifecycle.
- **PI-02 datasets** — historical bars and dataset versions; the evidence run
  uses the same stored SQLite bars, not generated prices.
- **PI-03 market-data gateway/store** — bar ordering, quality, multi-instrument
  coverage and the existing quote/bar access path.
- **PI-04 features** — the validated feature engine (`pi04-feature-set-1.0`),
  market-regime labels and feature profiles.
- **PI-05 baselines** — the same last-value/random-walk, SMA, SES and ARIMA
  implementations, scored at the exact same test origins.
- **Persistence** — `PredictionStore.prediction_forecasts` for forecasts,
  `prediction_models` for model metadata, `ForecastStore.forecast_training_runs`
  for training/evaluation evidence, and native artifacts under the store's
  data directory (`data/novamarket/gradient_models` in the application).
- **API/UI conventions** — the existing `/market/forecasts*` routes and React
  page, with the existing authorization and renderer conventions.

No second dataset engine, feature engine, prediction registry, risk authority or
execution engine was created.

## 3. Models implemented

A new additive package, `novacontrol.novamarket.gradient`, contains:

| Module | Responsibility |
|---|---|
| `models.py` | Configuration, feature schema, forecast contract, family result, evaluation report and explanation models. |
| `training.py` | Chronological matrix, target and label generation, native fits, bounded selection, metrics, regime slices, walk-forward folds and local contributions. |
| `artifacts.py` | Native XGBoost/LightGBM JSON persistence, SHA-256 checksums, dependency versions and feature-schema metadata. |
| `service.py` | Lazy orchestration, dataset/feature acquisition, evaluation, baseline comparison, persistence, inference, explanation and suitability. |

Models:

- **XGBoost** — regressor for forward simple return, classifier for
  `down|neutral|up`, native JSON serialization.
- **LightGBM** — same regression/classification contracts, same preprocessing,
  same feature schema and same forecast contract.
- **Horizon independence** — one model per instrument/horizon. A model trained
  for `1d` is not used for `5d`; a missing horizon model is refused rather than
  silently substituted.
- **Price and return** — the regressor predicts
  `future_close / decision_close - 1`; predicted price is derived from the
  reference close. Return and price are both reported, plus a bounded
  extrapolation check.
- **Direction** — the classifier and regression-derived direction use a
  configurable neutral band (0.5% default, at least the 10 bps round-trip cost
  and the horizon band). A supported interval crossing zero yields `uncertain`.

## 4. Training configurations

The default configuration is deliberately bounded and reproducible:

| Parameter | XGBoost default | LightGBM default |
|---|---|---|
| Estimators | 160 | 160 |
| Learning rate | 0.05 | 0.05 |
| Depth | 4 | 4 |
| Leaves | — | 15 |
| Subsample / bagging | 0.8 | 0.8 |
| Column / feature fraction | 0.8 | 0.8 |
| Minimum child | weight 2.0 | samples 20 |
| L2 regularization | 1.0 | 1.0 |
| Early stopping | 20 rounds | 20 rounds |
| Seed / threads | 42 / 2 | 42 / 2 |
| GPU | none | none |

The candidate grid contains three bounded configurations (base, a
shallower/slower variant and a deeper/more regularised variant). It is not an
unrestricted hyperparameter search. Validation **price** MAE selects the
candidate, with directional accuracy as a tie-break; the selected configuration
is refit on train + validation. Test data is never used for selection, and the
report records `test_used_for_selection: false`.

Inference abstains when the interval is wider than the configurable
`max_interval_width` (50% default), in addition to missing-feature, staleness,
out-of-distribution and unsupported-horizon guards. This is an uncertainty
guard, not an accuracy threshold.

The exact configuration digest, dataset version, feature-set version, target
definition, horizon, target-step count, split ranges, sample counts, dependency
versions, random seed, metrics and artifact checksums are persisted with every
model. The version digest includes the target-step count and instrument, and
lookup rejects a mismatched target-step count.

## 5. Supported horizons

| Horizon | Target steps | Status in this environment |
|---|---|---|
| `5m` | 5 one-minute steps | Trained/evaluated on ADANIENT's stored 1-minute session. |
| `15m` | 15 one-minute steps | Trained/evaluated on the same session. |
| `20m` | 20 one-minute steps | Trained/evaluated on the same session. |
| `1h` | 60 one-minute / 4 fifteen-minute steps | Refused for ADANIENT: 25 usable training rows, 30 required. |
| `1d` | 1 daily step | Trained/evaluated on RELIANCE and ADANIENT. |
| `3d` | 3 daily steps | Trained/evaluated on RELIANCE. |
| `5d` | 5 daily steps | Trained/evaluated on RELIANCE. |
| `30d` | 30 daily steps | Trained/evaluated on RELIANCE. |
| `90d` | 90 daily steps | Refused for RELIANCE: 24 usable training rows, 30 required. |

The target generator itself supports all nine horizons and is covered by
deterministic fixtures. A horizon is only claimed when the observed input
resolution and coverage support it. Daily bars cannot support a 5-minute claim;
the service infers spacing from the supplied bars and rejects too-coarse data.

## 6. Dataset and feature versions

- Dataset: `market-data` (the existing NovaMarket SQLite historical bars).
- Feature set: `pi04-feature-set-1.0`.
- Swing/long-term feature schema used by the evidence models: 84 PI-04 features.
- Intraday evidence used one ADANIENT 1-minute session (250 bars), aggregated
  to the requested horizon; it is not a multi-session intraday sample.
- Dhan/RELIANCE data: 256 daily bars (2025-10-29 through 2026-11-11 as stored).
- ADANIENT data: 250 daily bars and 250 one-minute bars from one session.

The evidence is real historical data stored in NovaMarket, but it is a small
sample. It is not presented as a production backtest.

## 7. Actual evaluation results

Test block metrics below are from the single chronological holdout. Baselines
were scored at the exact same origins. Lower MAE is better.

| Instrument | Horizon | Model | Test MAE | Directional accuracy | Best PI-05 baseline (MAE) |
|---|---|---|---:|---:|---|
| XNSE:RELIANCE | 1d | LightGBM (selected) | 20.749 | 0.300 | naive/last-value 16.445 |
| XNSE:RELIANCE | 1d | XGBoost | 22.170 | 0.275 | naive/last-value 16.445 |
| XNSE:RELIANCE | 3d | LightGBM | 39.212 | 0.333 | ARIMA 29.816 |
| XNSE:RELIANCE | 3d | XGBoost (selected) | 41.931 | 0.333 | ARIMA 29.816 |
| XNSE:RELIANCE | 5d | LightGBM | 43.098 | 0.103 | naive/last-value 36.536 |
| XNSE:RELIANCE | 5d | XGBoost (selected) | 43.717 | 0.231 | naive/last-value 36.536 |
| XNSE:RELIANCE | 30d | LightGBM (selected) | 96.290 | 0.194 | ARIMA 72.537 |
| XNSE:RELIANCE | 30d | XGBoost | 95.757 | 0.194 | ARIMA 72.537 |
| XNSE:ADANIENT | 1d | LightGBM | 58.252 | 0.211 | naive/last-value 51.147 |
| XNSE:ADANIENT | 1d | XGBoost (selected) | 58.151 | 0.079 | naive/last-value 51.147 |
| XNSE:ADANIENT | 5m | LightGBM | 2.929 | 1.000* | ARIMA 1.641 |
| XNSE:ADANIENT | 5m | XGBoost (selected) | 3.595 | 1.000* | ARIMA 1.641 |
| XNSE:ADANIENT | 15m | LightGBM (selected) | 4.311 | 1.000* | ARIMA 3.025 |
| XNSE:ADANIENT | 15m | XGBoost | 3.729 | 1.000* | ARIMA 3.025 |
| XNSE:ADANIENT | 20m | LightGBM (selected) | 3.582 | 1.000* | ARIMA 3.263 |
| XNSE:ADANIENT | 20m | XGBoost | 4.213 | 1.000* | ARIMA 3.263 |

\* The intraday directional accuracy of 1.0 is an artifact of one short,
monotonic session with a neutral-heavy test set. It is **not** evidence of
forecasting skill.

Classification evidence was trained independently. Test accuracy was 0.325
(LightGBM) and 0.350 (XGBoost) for RELIANCE 1d, 0.333/0.333 for 3d,
0.333/0.410 for 5d, 0.278/0.278 for 30d, 0.342/0.368 for ADANIENT 1d and 1.0
for both intraday families on the single neutral monotonic session. The
three-class confusion matrices are persisted. Class probabilities are withheld
because they are not calibrated.

Walk-forward folds refit the already-selected configuration on expanding
windows (three contiguous tail blocks). Rows whose target window crossed a
fold boundary were purged. Per-fold test MAE varied materially:

| Instrument/horizon | LightGBM fold MAEs | XGBoost fold MAEs |
|---|---|---|
| RELIANCE 1d | 14.405 / 16.047 / 18.890 | 14.234 / 16.183 / 19.235 |
| RELIANCE 3d | 20.278 / 24.299 / 44.205 | 20.183 / 24.418 / 44.060 |
| RELIANCE 5d | 24.692 / 24.142 / 60.929 | 27.292 / 24.406 / 60.387 |
| RELIANCE 30d | 84.348 / 67.069 / 86.665 | 81.413 / 80.286 / 94.576 |
| ADANIENT 1d | 58.590 / 54.167 / 47.626 | 59.225 / 54.581 / 44.666 |
| ADANIENT 5m | 1.314 / 2.312 / 3.154 | 1.398 / 3.080 / 3.155 |
| ADANIENT 15m | 1.970 / 6.373 / 4.914 | 2.013 / 5.400 / 3.244 |
| ADANIENT 20m | 1.337 / 5.078 / 4.371 | 1.646 / 4.340 / 4.094 |

Walk-forward means are not directly comparable to the single-holdout baseline
numbers above because the folds use different windows; they are included to
show stability, not to claim a win.

## 8. Comparison against PI-05 baselines

The overall preference was `statistical_baseline` for **every** evaluated
instrument/horizon pair. Every trained family was marked `provisional` rather
than `baselines_preferred`: its 80% validation-residual interval covered only
0%–53.8% of the test outcomes, below the nominal level's binomial lower bound.
That interval undercoverage is itself a material limitation, not a success.
The best baseline was:

- RELIANCE 1d: naive/last-value (16.445).
- RELIANCE 3d: ARIMA (29.816).
- RELIANCE 5d: naive/last-value (36.536).
- RELIANCE 30d: ARIMA (72.537).
- ADANIENT 1d: naive/last-value (51.147).
- ADANIENT 5m/15m/20m: ARIMA (1.641 / 3.025 / 3.263).

No gradient-boosting model beat the best baseline on the test block. The service
therefore reports baselines as preferable and does **not** promote the gradient
model. This is an honest negative result for this small evidence set, not a
claim that the implementation failed to run.

The two refused horizons are also honest results:

- RELIANCE 90d: `only 24 usable training rows; 30 are required`.
- ADANIENT 1h: `only 25 usable training rows; 30 are required`.

## 9. Explainability evidence

The real generation path produced a LightGBM forecast:

- Forecast id: `gbf_9706a8c821d66699456c`
- Model version: `pi06-gb-1.0-lightgbm-ac267470d1287fc8`
- Reference/predicted price: 1335.4 → 1336.774
- Predicted return: 0.00102871
- Direction: neutral
- 80% validation-residual range: 1324.551 … 1357.377
- Validity: valid; `p_up`/`p_down`: null

The on-demand explanation:

- Method: `lightgbm_pred_contrib_tree_shap`
- Base value: −0.00056617; prediction: 0.00102871
- Top local contributions: `ema_12` +0.0018960, `sma_cross_20_50` −0.0016439,
  `relative_volume_20` −0.0014586, `atr_pct` +0.0013114.
- Top global gain features: `relative_volume_20` (0.02003; 7.31% normalized),
  `distance_ema_50`, `high_low_range_pct`, `atr_pct`, `sma_cross_20_50`.

A local attribution explains the model's use of those inputs for that forecast.
It is not a causal claim, not a confidence score and not a profitability proof.

## 10. Prediction API and UI integration

The existing forecast surface was extended, not duplicated:

- Generate: `POST /market/forecasts` with `engine=gradient` (or
  `engine=xgboost` / `engine=lightgbm`).
- List/get: `GET /market/forecasts` and `GET /market/forecasts/{forecast_id}`
  with the same engine selector.
- Models: `GET /market/forecasts/models` with engine, instrument and horizon
  filters; metadata includes family, version, config digest, dataset/feature
  versions, metrics, artifact paths, checksums and status.
- Performance: `GET /market/forecasts/performance`.
- Train/evaluate: `POST /market/forecasts/train` and
  `POST /market/forecasts/evaluate`.
- Calibration: `POST /market/forecasts/{forecast_id}/calibrate` responds
  `not_applicable`; no probability is claimed.
- Explanation: `GET /market/forecasts/{forecast_id}/explain` is the one new
  route. It is declared in `ApiSurface`, `ROUTE_CONSUMERS`, the route-contract
  test and `docs/API.md`.

The React Forecasts page (`web/src/pages/ForecastModels.tsx`, re-exported by
`Intelligence.tsx`) filters by engine/family/horizon, starts bounded training,
shows the evaluation report, model version, test metrics, uncertainty, validity
and unsupported-horizon reasons, and requests a local explanation on demand.
It visually distinguishes statistical, XGBoost and LightGBM results and does not
present any as production-ready.

## 11. Test results

Focused PI-06 suite:

```
PYTHONPATH=src python -m pytest tests/test_novamarket_gradient.py -q
25 passed
```

Coverage includes XGBoost/LightGBM regression and classification, all nine
target definitions, feature schema/missing values, leakage-safe splits,
walk-forward folds, bounded selection, importance, local explanation,
uncertainty/validity, persistence/reload/versioning, suitability, failure
isolation, unsupported horizons, API generation/explanation and the
no-authority assertion.

Other checks:

- `PYTHONPATH=src python -m mypy src` and the Windows-platform view — clean
  (610 source files).
- Focused affected regression
  (`gradient` + `statistical` + `features` + `datasets` + `api`) — **168
  passed, 246 subtests passed**.
- `PYTHONPATH=src python -m pytest tests/test_novamarket_api.py::NovaMarketRouteContractTests -q`
  — passed with 224 market routes and the explanation path.
- `PYTHONPATH=src python scripts/generate_api_reference.py --check` — 631
  routes in sync.
- `npm run build` in `web/` — TypeScript and Vite build passed; only the
  pre-existing chunk-size/dynamic-import warnings.
- Real evidence: `tmp/pi06/run_real_evidence.py` wrote
  `tmp/pi06/real_evidence.json` against a copy of the stored database.

## 12. Defects fixed during the phase

- Corrected LightGBM objective mapping for regression and multiclass
  classification.
- Fixed `GradientConfig.from_dict` dropping computed target/direction fields.
- Fixed a NumPy array `.index()` call in regime slicing.
- Removed a classifier validation leak: validation classification evidence now
  comes from the candidate model fitted on the training prefix, not the refit
  model that saw validation labels.
- Fixed missing baseline `series` scope.
- Prevented a failed family's status being overwritten by its suitability
  placeholder.
- Applied the model-version prefix (`pi06-gb-1.0-<family>-<hash>`) instead of
  leaving it unused.
- Fixed direct-bar API calls falsely assuming a horizon default resolution;
  spacing is now inferred from the supplied bars and too-coarse series are
  refused.
- Fixed mypy issues in the training/service/artifact contracts, including the
  `NDArray` alias, optional split values, metrics index types, report naming,
  optional timestamps and feature-value casts.
- Scoped model lookup to the requested instrument; a model trained on another
  instrument can no longer be selected merely because it is newest.
- Aligned candidate selection with the reported metric: validation **price**
  MAE, with directional accuracy as the tie-break.
- Removed TreeSHAP computation from ordinary inference; local contributions are
  now computed only by the on-demand explanation route.
- Replaced the hardcoded 0.35 directional-accuracy rule with a comparison to
  the price-winning PI-05 baseline's direction on the same test origins (not
  the maximum direction from a different baseline), and added a binomial
  coverage check for the validation-residual interval.
- Fixed early-stopped booster iteration fallback so disabling early stopping
  uses all configured trees rather than a single tree.
- Purged training/validation rows whose target window crosses a walk-forward
  fold's test start, and recorded the purge count and target-end timestamps;
  the earlier index-only split could leak a multi-day label into test.
- Normalized engine matching case-insensitively and rejected conflicting
  engine/model-family requests; an explicit `engine=xgboost|lightgbm` now forces
  that family instead of silently using the run-selected family, and
  engine-filtered list/model/performance reads are family-scoped.
- Added target-step and resolution identity to the model version metadata and
  refuse a model trained for a different target-step count.
- Added optional fundamentals/news inputs to inference so models trained with
  those validated features can be served, and enforced the persisted feature
  profile at inference.
- Validated artifact metadata version/family and made report leakage/selection
  flags serialize their actual values instead of hardcoded claims.
- Mapped provisional/insufficient suitability to the matching family status and
  degraded model state, and rejected non-finite or non-positive target prices.

## 13. Remaining limitations

- **Small real sample.** 256 RELIANCE daily bars and one ADANIENT 1-minute
  session are not enough to establish production skill. Walk-forward variation
  is large.
- **No baseline win.** Every evaluated pair preferred the PI-05 baseline. This
  must not be reported as an accuracy success.
- **Refused horizons.** RELIANCE 90d and ADANIENT 1h lack enough usable rows.
- **Intraday evidence.** 5m/15m/20m results come from one monotonic session;
  directional accuracy of 1.0 is not credible skill.
- **No fundamentals/news in evidence.** The API accepts optional validated
  fundamentals/news rows and delegates publication-time checks to PI-04, but
  the stored evidence here did not supply them. They remain an unexercised
  coverage limitation, not a claimed accuracy improvement.
- **Uncalibrated uncertainty.** The interval is validation-residual based, with
  no coverage guarantee; on the real test blocks its coverage was 0%–53.8%
  against a nominal 80%, so all trained families were marked provisional.
  Classifier probabilities are withheld.
- **No regime-specific models.** Regimes are reported as performance slices only
  where sample counts suffice; separate regime models were not trained.
- **No ensemble-disagreement abstention.** One independent family is served per
  request, so there is no ensemble vote to compare. The service does not
  fabricate a disagreement score; disagreement-based abstention belongs to a
  future ensemble, while uncertainty-width, missing-feature, OOD, staleness and
  unsupported-horizon guards are implemented.
- **No promotion.** Learning/promotion remains in the existing NovaMarket
  architecture. No model gains risk or execution authority.
- **No GPU / distributed training.** This is deliberate for the existing
  hardware and tree-based models.

## 14. Unverified requirements

- **UI interaction was not visually exercised.** The React/TypeScript build
  passes, but no browser click-through of the Forecasts page was performed in
  this audit. API routes and component compilation are verified; visual layout,
  loading/error states and responsive behavior remain unverified.
- **No successful 90d or ADANIENT 1h model.** The real database has only 24 and
  25 usable training rows respectively, so these horizons are verified as
  explicit refusals, not as successful forecasts.
- **Fundamentals/news inference path is unexercised.** The optional API inputs
  and PI-04 delegation are implemented, but no stored evidence supplied those
  rows, so their effect on accuracy is unknown.
- **Classifier probabilities are intentionally unavailable.** Classification
  metrics are reported, but no calibrated probability is claimed; calibration
  against a proper scoring rule remains future work.
- **No multi-family ensemble or disagreement signal exists.** Disagreement-based
  abstention is therefore unverified/out of scope for this phase.
- **Real evidence is small and non-production.** It is real stored history, not
  synthetic fixtures, but it cannot establish production skill.

## 15. Final acceptance verdict

**PI-06 PASS for implementation, integration, explainability, persistence,
failure isolation and test coverage.**

The phase is complete in the required sense: XGBoost and LightGBM consume the
PI-02/PI-04 inputs, comply with PI-01 contracts, are compared at the exact PI-05
test origins, persist model identity/artifacts/versions, expose the existing
API/UI surface with bounded training and on-demand explanation, isolate
failures, and add no financial or execution authority.

The real historical evidence does **not** show that either gradient model is
preferable to the statistical baselines on this database. The service reports
that honestly: overall preference is `statistical_baseline` for every evaluated
pair, and every trained family is `provisional` because its residual interval
under-covered the test block. It refuses unsupported horizons and does not
promote a model. That negative result is part of the PI-06 verdict, not a defect
to be hidden or tuned away.

PI-07 may begin only after this report and the focused/regression checks are
accepted; no commit, push, tag or merge was performed.
