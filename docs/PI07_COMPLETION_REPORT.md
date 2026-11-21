# PI-07 — LSTM Deep Learning Forecasting Models

**Status:** **PASS for the PI-07 implementation, integration, persistence,
failure-isolation and verification scope**, with the model-performance
limitations recorded below.
**Branch:** `feature/novamarket` (working tree only; nothing staged, committed,
tagged, pushed, merged or released).
**Date:** 2026-11-15.
**Evidence classification:** real historical bars stored in the local
NovaMarket database for the model results; deterministic synthetic fixtures for
the tests. Synthetic results are not presented as market performance.

## 1. Initial repository assessment

The phase started from the existing PI-01–PI-06 implementation. No
prerequisite was treated as complete merely because files existed; each
completion report was read and its scope checked.

| Prerequisite | Evidence | Status |
|---|---|---|
| PI-01 contracts, boundaries and lifecycle | `docs/PI01_COMPLETION_REPORT.md`; `prediction/`; forecast/model stores | PASS for PI-01 scope |
| PI-02 historical datasets and chronological training | `docs/PI02_COMPLETION_REPORT.md`; `datasets/`; `build_training_dataset` | PASS, with the reported provider/data limitations |
| PI-03 market-data gateway and freshness | `docs/PI03_COMPLETION_REPORT.md`; `marketdata/gateway.py` | PASS for gateway scope |
| PI-04 features and regimes | `docs/PI04_COMPLETION_REPORT.md`; `features/`; `pi04-feature-set-1.0` | PASS |
| PI-05 statistical baselines | `docs/PI05_COMPLETION_REPORT.md`; `statistical/`; baseline metrics | PASS |
| PI-06 gradient boosting | `docs/PI06_COMPLETION_REPORT.md`; `gradient/`; feasibility/training matrix | PASS |

Environment inspected before choosing a framework:

- Python 3.13.
- PyTorch **2.14.0+cpu**, CUDA false, MPS false; 14 host threads.
- TensorFlow absent; no second deep-learning framework was added.
- Existing SQLite stores, prediction/model registry, forecast stores, API and
  React Forecasts page were reused.
- No full-repository suite was run; focused and affected-area verification was
  used, as required by the phase plan.

The repository already had uncommitted PI-01–PI-06 work and generated
untracked files. Those were preserved. PI-07 only adds/changes the files listed
in section 15.

## 2. Existing functionality reused

| Existing functionality | Reuse in PI-07 |
|---|---|
| PI-01 `PredictionStore.prediction_models`, `prediction_forecasts`, `ForecastOutput` contract, IDs, lifecycle and validity semantics | LSTM model metadata, forecasts, explain payloads, API responses |
| PI-02 `build_training_dataset`, chronological splits, purge gap, target construction | Every LSTM evaluation uses the existing matrix; no second dataset path |
| PI-03 freshness/quality | Bars supplied to the service retain existing data-quality/freshness evidence; validity is derived from it |
| PI-04 `FeatureEngine`, `FeatureSchema`, feature-set version, regime groups, point-in-time context | Sequence features and inference windows |
| PI-05 baseline formulas, metrics and evaluation | Same-origin last-value/random-walk/SMA/SES/ARIMA comparison |
| PI-06 `GradientMatrix`, `build_training_matrix`, schema/stats, metrics helpers, suitability, walk-forward folds, family-result shape, `GradientArtifactStore` patterns | Sequence source, PI-06 comparison on the exact same matrix, shared metric semantics |
| Existing `/market/forecasts*` routes, `ApiSurface`, route consumers, `docs/API.md` generation | LSTM engine routing and explain branch; no duplicate routes |
| Existing React Forecasts page | LSTM engine/status/training/forecast/explanation presentation |
| Existing failure isolation and status composition | LSTM absent/failed state never blocks other families |

No duplicate dataset, feature, prediction, evaluation or model-registry system
was created. PI-06 comparison models are fitted for evidence on the same
`GradientMatrix` but deliberately not registered: PI-06 remains the owner of
its registry records.

## 3. LSTM architecture implemented

New additive package: `src/novacontrol/novamarket/lstm/`.

| File | Implementation |
|---|---|
| `models.py` | Bounded `LSTMConfig`; horizon sequence profiles; `SequenceScaler`; `FeatureStats`/schema reuse; `LSTMArtifact`, `LSTMResult`, `LSTMEvaluationReport`, `LSTMForecast`, `LSTMLoaded`; version derivation; explicit errors. |
| `sequence.py` | `SequenceSet`; chronological `build_sequences`; train-only `fit_scaler`; `scaled_sequence`; `build_inference_sequence` through PI-04. |
| `network.py` | Lazily imported CPU PyTorch network: LSTM encoder, LayerNorm, regression head, three-class direction head; capability/hardware reporting. |
| `training.py` | Deterministic seed/thread setup; bounded candidate fitting; early stopping; best-state restore; Huber/MSE; class weights; validation-residual intervals; regression/classifier metrics; feature importance; walk-forward folds; resource measurements. |
| `artifacts.py` | Atomic native state-dict writes; SHA-256 and byte-size verification; compatibility/load guards. |
| `service.py` | Lazy evaluate/train, PI-05 baseline scoring, PI-06 same-matrix comparison, suitability, model registration, inference, forecast read surfaces, explanation, status/contract and failure isolation. |
| `__init__.py` | Public PI-07 surface. |

Network shape: shared LSTM encoder with hidden size 32 and one layer by default;
dropout 0.10; LayerNorm; a scalar return head; a three-class (`down`, `neutral`,
`up`) head. Each feature contributes two input channels — the scaled value and
its explicit observed/missing mask — so missing and observed-zero values remain
distinct inside the network. The network is deliberately small and CPU-first.
The candidate set is bounded to base, shallower/slower and
deeper/more-regularised variants; it is not an unrestricted
neural-architecture search.

The persisted model version prefix is `pi07-lstm-1.1` and the methodology
version is `pi07-lstm-methodology-v2`; the bump records the explicit mask
input architecture change and prevents an older 1.0 state dict from being
reinterpreted under the new input contract.

Integration points:

- `NovaMarketService`: lazy `_lstm_service`, LSTM status/contract, forecast
  generate/list/get/train/evaluate/performance/models/calibration/explain
  routing, and shutdown.
- `api/app.py`: `engine=lstm` (alias `sequence`) on the existing
  forecast routes; LSTM branch on the existing explanation route.
- `web/src/pages/ForecastModels.tsx`: LSTM engine option, experimental badge,
  backend status, training/generation routing, sequence/version/uncertainty
  display and LSTM explanation.
- `architecture.py`: PI-06 and PI-07 roadmap titles aligned with the
  implemented gradient-boosting and LSTM model families.
- `pyproject.toml`: optional `lstm = ["torch>=2.2,<3", "numpy>=1.26"]` extra;
  PyTorch is not a mandatory dependency for the rest of NovaMarket.

## 4. Sequence preparation and leakage controls

A training sequence contains `sequence_length` consecutive PI-04 feature rows
ending at the decision timestamp. The target is attached to the same decision
row:

```text
target_return = future_close / decision_close - 1
```

Construction rules enforced by code and tests:

- Rows must be strictly chronological; duplicate decision timestamps and
  non-monotonic observations are refused.
- Target rows are restricted to the train, validation and test index blocks
  returned by PI-02. A window may use earlier observations as historical input,
  but its target row remains inside its split; no later-split target enters it.
- The target observation must be after the decision time; target alignment is
  checked before a window can be trained on.
- No future row is used in the input; the next target is not part of the
  input sequence. Inference filters the entire caller-supplied series to
  observations at or before `as_of`, including the reference close.
- Insufficient post-window rows are reported, never padded with synthetic
  observations.
- Feature order is the persisted `FeatureSchema`; schema digest mismatch is a
  hard refusal at inference.
- Sequence values carry a separate missing mask. The network receives scaled
  values and the mask as two concatenated input channels; a missing feature is
  imputed with the training median for the value channel while the mask remains
  zero, so an observed zero is distinguishable from a missing value.

Horizon defaults (overridable):

| Horizon family | Default sequence length |
|---|---|
| 5m, 15m, 20m, 1h with 1m resolution | 30 |
| Intraday with coarser resolution | 20 |
| 1d, 3d, 5d | 30 |
| 30d, 90d | 15 |

The service also retains PI-02/PI-06 target-step and resolution checks; a
daily-only series cannot establish a 5-minute forecast claim.

## 5. Training configuration and overfitting controls

Default `LSTMConfig` used for the production evidence:

| Setting | Value |
|---|---|
| Hidden size | 32 |
| LSTM layers | 1 |
| Dropout | 0.10 |
| Input window (`window_bars` for PI-04 context) | 60 |
| Sequence length | Horizon profile (30/20/15) |
| Learning rate | 0.001 |
| Batch size | 32 |
| Optimizer | Adam |
| Regression loss | Huber |
| Classification loss weight | 0.35 |
| Weight decay | 1e-4 |
| Gradient clip | 1.0 |
| Max epochs | 40 |
| Early-stopping patience | 6 |
| Checkpoint restore | Best validation state |
| Candidate count | 3 |
| Random seed | 42 |
| Training threads | 2 |
| Max samples | 5,000 |
| Neutral band | max(0.5%, 10 bps round-trip cost) |
| Interval | 80% validation residual, uncalibrated |

Training protocol:

1. Build the existing PI-02 matrix and reject invalid rows/targets.
2. Fit the scaler on training sequences only.
3. Fit each bounded candidate on training sequences, selecting by validation
   loss/price error; restore the best validation epoch.
4. Keep the selected candidate's best validation checkpoint; do not refit it
   on validation. This keeps validation loss/interval calibration independent
   of the final weights and avoids leaking validation into the reported test
   uncertainty.
5. Score test once; never feed the test result back into candidate choice, band,
   interval or hyperparameters.
6. Persist the artifact and complete provenance; a failure does not overwrite
   the previous version.

Controls: early stopping, dropout, weight decay, gradient clipping, bounded
epochs/candidates, validation-loss monitoring, best-state restoration and
comparison of training/validation behavior. Continued decreasing training loss
alone never selects a model.

## 6. Dataset and feature versions

The real evidence used the existing local database copied for evidence:

- Dataset record: `market-data` with the stored version produced by the
  existing PI-02 builder for each horizon.
- Feature set: `pi04-feature-set-1.0`.
- Feature count: 84 for daily sequences; 59 for the ADANIENT 1-minute session
  (availability differs by session/context; missingness is explicit).
- Source data: RELIANCE daily (256 bars) and ADANIENT 1-minute (250 bars).
- The live `data/novamarket.sqlite3` was not modified; the harness copies it to
  `tmp/pi07/workspace/novamarket.sqlite3`.

Observed split blocks (before sequence reduction):

| Instrument | Horizon | Train | Validation | Test |
|---|---|---:|---:|---:|
| RELIANCE | 1d | 116 | 38 | 40 |
| RELIANCE | 3d | 114 | 36 | 39 |
| RELIANCE | 5d | 113 | 36 | 39 |
| RELIANCE | 30d | 85 | 13 | 36 |
| RELIANCE | 90d | — | — | — (52 total rows after purge; 24 training) |
| ADANIENT | 1d | 113 | 37 | 38 |
| ADANIENT | 5m | 109 | 33 | 38 |
| ADANIENT | 15m | 97 | 23 | 38 |
| ADANIENT | 20m | 89 | 17 | 37 |
| ADANIENT | 1h | — | — | — (54 total rows after purge; 25 training) |

## 7. Supported horizons

Target generation and model configuration support all established PI-01
horizons: `5m`, `15m`, `20m`, `1h`, `1d`, `3d`, `5d`, `30d`, `90d`.

Support is conditional on:

- resolution compatible with the horizon (1m bars for 5m/15m/20m/1h; daily
  bars for daily/weekly/long-term horizons),
- enough post-purge observations for the sequence window and target,
- usable train, validation and test rows,
- a persisted model for the requested instrument/horizon/version at inference.

Where those conditions fail, the exact limitation is returned. It is not
converted into a forecast.

## 8. Actual training and validation results

Real historical bars; default bounded configuration; one held-out test block
per case; selection used validation only.

| Instrument | Horizon | Status | Validation price MAE | Train/Val/Test sequences | Test price MAE | Best PI-05 baseline MAE | Directional accuracy | Classifier accuracy | 80% interval coverage | Train s | Infer ms |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| RELIANCE | 1d | provisional | 10.27 | 87/38/40 | 17.88 | 16.45 | 0.050 | 0.250 | 0.500 | 6.87 | 0.039 |
| RELIANCE | 3d | provisional | 12.67 | 85/36/39 | 41.55 | 29.82 | 0.282 | 0.308 | 0.128 | 4.00 | 0.120 |
| RELIANCE | 5d | provisional | 17.57 | 84/36/39 | 43.63 | 36.54 | 0.128 | 0.205 | 0.385 | 2.04 | 0.077 |
| RELIANCE | 30d | provisional | 25.50 | 71/13/36 | 65.68 | 72.54 | 0.333 | 0.250 | 0.056 | 1.98 | 0.038 |
| RELIANCE | 90d | refused | — | — | — | — | — | — | — | — | — |
| ADANIENT | 1d | provisional | 39.32 | 84/37/38 | 55.63 | 51.15 | 0.158 | 0.289 | 0.526 | 3.27 | 0.099 |
| ADANIENT | 5m | provisional | 1.77 | 80/33/38 | 2.24 | 1.64 | 1.000 | 1.000 | 0.421 | 3.20 | 0.089 |
| ADANIENT | 15m | provisional | 2.24 | 68/23/38 | 4.68 | 3.03 | 1.000 | 1.000 | 0.289 | 3.32 | 0.083 |
| ADANIENT | 20m | provisional | 2.75 | 60/17/37 | 4.36 | 3.26 | 1.000 | 0.946 | 0.514 | 1.17 | 0.083 |
| ADANIENT | 1h | refused | — | — | — | — | — | — | — | — | — |

Interpretation:

- **The LSTM was not eligible for preference in any case.** Seven of eight
trained cases had a higher test price MAE than the best PI-05 baseline. The
30d case had lower test MAE (65.68 vs 72.54) but its 5.6% interval coverage was
far below the nominal level, so it remained `provisional`; a single small test
block is not evidence of stable superiority.
- **No LSTM reached `lstm_suitable`.** All eight trained runs were
`provisional` because residual-interval coverage was below the binomial lower
bound of the nominal 80% interval. The suitability rule is not an arbitrary
accuracy target.
- **Intraday directional accuracy of 1.000 is not skill.** The single ADANIENT
session is short and monotonic; the classifier predicted `neutral` for almost
every test row. Classifier accuracy is reported, not celebrated.
- Classifier probabilities were withheld (`probability_status=unavailable`);
`calibration_status=not_applicable`. The intervals are validation-residual
intervals, not calibrated probabilities or guarantees.
- The 30d case had only 13 validation sequences (71 training sequences) and
5.6% test coverage; it is far too small for a reliable long-term claim.
- RELIANCE 90d and ADANIENT 1h were refused because the chronological
target-window purge left 24 and 25 training rows respectively, below the
30-row minimum, with no usable validation block. This is the required honest
limitation, not a fabricated forecast.

## 9. Test-set results and walk-forward results

The test block is reported once after validation selection. Test metrics
include MAE, RMSE, MAPE, sMAPE, bias, price/return errors, direction
accuracy/precision/recall/F1, confusion matrices and interval coverage. The
complete per-case metrics are in `tmp/pi07/evidence.json`.

Walk-forward uses three contiguous held-out blocks, refitting the selected
architecture on expanding history and purging target-overlapping rows. Test
price MAE by fold:

| Instrument / horizon | Fold 1 | Fold 2 | Fold 3 | Variation |
|---|---:|---:|---:|---|
| RELIANCE 1d | 15.05 | 18.98 | 22.53 | material |
| RELIANCE 3d | 35.69 | 28.03 | 44.95 | material |
| RELIANCE 5d | 31.88 | 36.35 | 51.02 | material |
| RELIANCE 30d | 49.56 | 53.33 | 78.29 | material |
| ADANIENT 1d | 64.06 | 48.18 | 62.94 | material |
| ADANIENT 5m | 1.219 | 3.057 | 4.031 | material |
| ADANIENT 15m | 2.048 | 7.732 | 4.984 | material |
| ADANIENT 20m | 1.220 | 6.727 | 5.368 | material |

Fold variation is large relative to the small sample sizes. It does not support
a claim of stable out-of-sample superiority; fold metrics are retained as
evidence, not used to promote or retune the model.

## 10. Comparison against PI-05 and PI-06

### PI-05 statistical baselines

PI-05 models were scored at the exact same test origins. The best baseline MAE
is shown in section 8. LSTM lost to that baseline in **7 of 8** trained cases:

- Daily/swing: 17.88 vs 16.45 (1d), 41.55 vs 29.82 (3d), 43.63 vs 36.54 (5d),
  55.63 vs 51.15 (ADANIENT 1d).
- Long-term: 65.68 vs 72.54 (30d) — lower MAE, but not eligible because of
  5.6% interval coverage and a small test block.
- Intraday: 2.24 vs 1.64 (5m), 4.68 vs 3.03 (15m), 4.36 vs 3.26 (20m).

### PI-06 gradient boosting

PI-06 XGBoost and LightGBM were trained on the **same `GradientMatrix`** and
scored on the same test origins. They are not registered as LSTM models and no
PI-06 registry was duplicated.

| Case | LSTM MAE | XGBoost MAE | LightGBM MAE | LSTM better than both? |
|---|---:|---:|---:|---|
| RELIANCE 1d | 17.88 | 22.17 | 20.75 | yes |
| RELIANCE 3d | 41.55 | 41.93 | 39.21 | no (beats XGBoost only) |
| RELIANCE 5d | 43.63 | 43.72 | 43.10 | no (beats XGBoost only) |
| RELIANCE 30d | 65.68 | 95.76 | 96.29 | yes |
| ADANIENT 1d | 55.63 | 58.15 | 58.25 | yes |
| ADANIENT 5m | 2.24 | 3.59 | 2.93 | yes |
| ADANIENT 15m | 4.68 | 3.73 | 4.31 | no |
| ADANIENT 20m | 4.36 | 4.21 | 3.58 | no |

LSTM beat both gradient boosters in 4 of 8 cases, beat XGBoost only in 2, and
lost to both in 2. It also lost to the PI-05 baseline in 7 of 8; the 30d case
had a lower MAE but failed the uncertainty/suitability gate. The measured
conclusion is therefore **no demonstrated out-of-sample advantage over the
simpler statistical baseline**, despite local wins over PI-06. This is an
honest negative/neutral result, not a hidden failure: a simpler model remains
preferable when it is better, more stable and less resource-intensive.

Comparability notes:

- All three families consumed the same stored bars, the same chronological
  splits/targets and the same test origins.
- LSTM applies a sequence window, so its effective training sample count is
  smaller than the tree/statistical models' row count. This is an inherent
  difference in the model class, not hidden data leakage.
- LSTM used 84 daily features (59 intraday); PI-06 used the same matrix feature
  schema. Feature counts differ only where the stored context differs.
- No calibrated probability comparison is claimed; all families' probability
  fields are withheld where uncalibrated.

## 11. Resource and latency measurements

Recorded on the development machine, CPU-only:

| Metric | Measured range |
|---|---|
| Training duration per successful case | 1.17–6.87 s |
| Inference latency (service) | 0.038–0.120 ms |
| Peak process memory during training | 9.3–94.9 MB |
| Native state-dict size | 51.4–371.2 KB |
| Epochs observed | 7–33, within each case's bounded maximum |
| Best epoch | 1–27 |
| Early-stopped cases | 8 of 8 trained |
| CPU threads used | 2 |
| GPU required | no |
| CUDA/MPS used | no |

The measured workload is bounded to 5,000 samples, three candidates, 40 epochs
and a small network. Training is an explicit service call; ordinary inference
is separate and never retrains. These are development-machine measurements,
not guarantees for other hardware or datasets.

## 12. API and frontend integration evidence

- `test_novamarket_lstm.py::TestHTTPIntegration` trains, generates, fetches,
  explains, lists models/performance and reads status through the real FastAPI
  app; all responses were 200 and the LSTM branch was asserted.
- `/market/forecasts` accepts `engine=lstm`; `/market/forecasts/train` returns
  the LSTM evaluation report with `test_used_for_selection: false`;
  `/market/forecasts/{id}/explain` returns sequence-model importance.
- `docs/API.md` was regenerated from the shared route registry: **631 routes**,
  in sync (`generate_api_reference.py --check`).
- `web/src/pages/ForecastModels.tsx` contains the explicit LSTM engine,
  experimental/provisional badge, PyTorch backend availability, sequence
  length, version, uncertainty/validity and evaluation display. The React build
  (`tsc -b && vite build`) passed: 1,600 modules, 621.39 kB JS (165.17 kB
  gzip).
- A focused static frontend contract test asserts the LSTM engine option,
  sequence evidence and PI-07 label are present.

The page was **not clicked through in a browser** and no screenshot was taken.
Frontend verification is the TypeScript/Vite build plus source contract test,
not a claim that every visual interaction was manually exercised.

## 13. Tests and actual results

### Focused LSTM suite

`PYTHONPATH=src python -m pytest tests/test_novamarket_lstm.py -q`

**23 passed**, 13 warnings, in 34.13 s. Coverage includes:

- config bounds, horizon sequence profiles and candidate set;
- chronological windows, ordering and target alignment;
- refusal of reordered timestamps and short sequences;
- train-only scaler fitting, missing-vs-zero preservation, explicit network
  mask channel, metadata round-trip and inverse target transform;
- regression/classification, early stopping, checkpoint metadata, artifact
  persistence/reload/checksum failure;
- multi-horizon targets, forecast contract/probability withholding/validity;
- walk-forward and comparison evidence;
- daily bars refusing a 5m claim;
- caller-supplied post-cutoff rows excluded from the inference series and
  reference close;
- training-run/report ID equality and model-metadata provenance;
- model selection rejecting a different target span for the same horizon;
- failed training restoring the previous `tracemalloc` state;
- one training failure isolated while comparisons and baselines survive;
- artifact-store checksum rejection without bad-version reuse;
- actual `NovaMarketService` integration through `forecast_train(engine="lstm")`
  and `forecast_generate`;
- HTTP train/generate/get/explain/models/performance/status;
- frontend LSTM surface contract.

No test asserts an accuracy target; market performance is evidence, not a
green-test assertion.

### Affected PI-01–PI-06 regression

`PYTHONPATH=src python -m pytest tests/test_novamarket_prediction.py
tests/test_novamarket_marketdata.py tests/test_novamarket_datasets.py
tests/test_novamarket_features.py tests/test_novamarket_statistical.py
tests/test_novamarket_forecasting.py tests/test_novamarket_gradient.py
tests/test_novamarket_api.py -q`

**308 passed, 2 skipped, 281 subtests passed**, 355 warnings (deprecation
warnings only), in 126.90 s. The skips are existing environment-gated cases.

### Static and build checks

| Check | Result |
|---|---|
| `PYTHONPATH=src python -m mypy src` | Success, 617 source files |
| `PYTHONPATH=src python -m mypy --platform win32 src` | Success, 617 source files |
| `python -m compileall -q src/novacontrol/novamarket/lstm` | passed |
| `python scripts/generate_api_reference.py --check` | in sync (631 routes) |
| `cd web && npm run build` | passed (warnings only: dynamic-import and chunk-size notices) |

### Real evidence harness

`PYTHONPATH=src python tmp/pi07/run_real_evidence.py` completed 10 cases and
wrote `tmp/pi07/evidence.json`. The harness copies the local database and uses
the real `NovaMarketService`; it is evidence generation, not a second
application.

## 14. Defects found and fixed

| Defect | Effect | Fix |
|---|---|---|
| `SequenceSet` had no `__len__` | Real smoke failed before training with `TypeError: object of type 'SequenceSet' has no len()` | Added explicit length semantics and test coverage |
| Registered LSTM metadata omitted scaler and config | A trained artifact could not be reloaded; real run reported “no trained LSTM model” | Persist/restore scaler, config, schema, feature stats and importance with `LSTMResult.registry_metrics()` |
| `feature_stats` metadata indentation | Feature statistics could be lost/misplaced in registry metadata | Corrected the payload structure |
| Network collapsed missing and observed-zero values | Masking by multiplication made an observed zero indistinguishable from an imputed missing value | Feed scaled values and the explicit mask as two network input channels; bumped methodology/model version to v2/1.1 and regenerated evidence |
| Caller-supplied post-cutoff bars could affect inference | The feature builder filtered each prefix, but the reference close was taken from the end of the unfiltered series, so a future row could set the reference price | Filter the entire series to observations at or before `as_of` before feature construction and reference-price selection; added a regression test |
| Training run and registered model carried different report IDs | Provenance could not be joined across `forecast_training_runs` and `prediction_models`; a model's evidence report looked unrelated to its run | Generate one report ID before registration and pass it through the report; added an equality test |
| Inference could load a model for a different target span | Model lookup filtered only by horizon, so a 5m/target-1 model could be chosen for a 15m/target-3 request when the profile matched | Restrict lookup to the exact `target_steps` and register that field with every model; added a mismatch regression test |
| Training memory tracing survived a failed run | `tracemalloc` stayed enabled after an exception, distorting later measurements and global tracing state | Scope tracing in a wrapper with `finally` cleanup; verified with a deliberately invalid matrix |
| Non-finite validation loss could be accepted | A NaN/inf validation metric could break early stopping and artifact selection silently | Raise `LSTMTrainingError` before recording the epoch |
| Candidate failures disappeared from the final result | A failed candidate was recorded by training but the service replaced `LSTMResult.failures` with its own list, so partial failure isolation was not auditable | Extend the final result/report failure list with `fitted.failures`, then refresh it after suitability and registration |
| Non-finite scaler target mean could load | A corrupt/hand-edited scaler could pass construction and only fail during inverse transformation | Reject a non-finite target mean at scaler construction/load |
| LSTM mypy override names collided with inherited statistical/gradient names | Type checking failed on the additive service | Renamed the LSTM model/artifact accessors and used a distinct `_lstm_artifacts` store |
| Generated config keys were fed back into `from_dict` | Config round-trip rejected its own generated fields | Filter generated documentation fields before constructor validation |
| Test assumptions used wrong enum/contract key names | Focused tests failed collection/assertions | Corrected tests to the existing `ForecastHorizon.MINUTES_5` and `forecast_validity_conditions` contract (no production behavior weakened) |
| Route descriptions did not name the LSTM engine | API docs understated the existing route | Updated `ApiSurface` descriptions and regenerated `docs/API.md` |

No failure was “fixed” by deleting a test, weakening an assertion or silently
substituting another model's output.

## 15. Files and artifacts

Implementation:

- `src/novacontrol/novamarket/lstm/{__init__,models,sequence,network,training,artifacts,service}.py`
- `src/novacontrol/novamarket/service.py`
- `src/novacontrol/novamarket/architecture.py` (implemented-family roadmap titles)
- `src/novacontrol/api/app.py`
- `src/novacontrol/api/models.py`
- `pyproject.toml`
- `tests/test_novamarket_lstm.py`
- `web/src/pages/ForecastModels.tsx`
- `docs/API.md`
- `docs/ARCHITECTURE.md`
- `docs/PREDICTION_INTELLIGENCE_ARCHITECTURE.md`
- `docs/PI07_COMPLETION_REPORT.md` (this file)

Evidence (not production model registration):

- `tmp/pi07/run_real_evidence.py`
- `tmp/pi07/evidence.json`
- `tmp/pi07/workspace/` (copied database and local artifacts)
- `tmp/pi07/one/` (timing smoke; not evidence)

The application persistence path is `data/novamarket/lstm_models`; model
metadata/scaler/schema/metrics live in the existing `prediction_models` table,
and evaluation evidence in `forecast_training_runs`. No model was promoted.

## 16. Remaining limitations and final acceptance verdict

### Remaining limitations

- **Insufficient historical coverage:** RELIANCE 90d and ADANIENT 1h cannot be
  trained from the stored data after the required purge; they are refused with
  exact reasons.
- **No demonstrated LSTM advantage:** seven of eight cases lost to the best
  PI-05 baseline. The 30d case had lower test MAE but failed interval coverage
  and remains ineligible. LSTM beat both PI-06 boosters in 4 of 8 cases; do not
  present this model as superior.
- **Interval under-coverage:** test coverage 5.6%–52.6% against an 80% nominal
  level; the intervals are not calibrated guarantees.
- **Small sample / one intraday session:** daily evidence is limited to
  RELIANCE and ADANIENT; the intraday session is one day and produces
  neutral-heavy, non-skill direction results.
- **Probabilities withheld:** no calibrated probability, no arbitrary
  confidence percentage and no claim of guaranteed future behavior.
- **No fundamentals/news coverage in evidence:** the optional point-in-time
  context path exists through PI-04 but was not exercised with supplied
  fundamental/news rows; no skill is claimed for it.
- **No regime-specific model training:** regime slices are diagnostics only;
  sample sizes are reported and insufficient slices are not scored.
- **No ensemble:** PI-08 has not started; LSTM forecasts are independent and
  are never blended or relabelled.
- **UI was built and source-contract tested, not clicked through:** visual
  behavior is not claimed beyond the build and the HTTP/source integration.
- **No full repository suite:** only focused LSTM tests and affected
  PI-01–PI-06/API tests were run, per the phase’s focused-testing rule.

### Final acceptance verdict

**PASS for PI-07 implementation, integration, persistence, versioning,
failure isolation, API/UI wiring and verification.** The LSTM subsystem
correctly consumes PI-02/PI-04 data, uses PI-01 contracts, builds chronological
leakage-safe sequences, supports configurable multi-feature/multi-horizon
training, persists preprocessing with the artifact, exposes honest uncertainty
and abstention, and compares against PI-05/PI-06 on the same test origins.

The **model-performance conclusion is negative/neutral**: the LSTM did not
establish a reliable advantage over the best statistical baseline on the
available historical data. One 30d case had lower test MAE but failed interval
coverage, so no LSTM was eligible; seven of eight cases lost to the baseline.
That is a recorded evidence result, not a phase-blocking defect, and it is why
no LSTM model was promoted and the UI marks the family experimental. PI-08 has
not been started.

No Git staging, commit, push, tag, merge or release was performed. Only the
working tree contains the changes; the user retains control of those
operations.

## 17. Post-completion verification pass (2026-11-15)

A second pass re-checked PI-07 against its objectives after the initial report.
The focused suite, affected dependency/integration tests, both mypy platforms,
compileall, API-reference generation and the web build were rerun; the real
historical-data harness was regenerated (10 cases, 8 trained, 2 refused).

| Check | Result |
|---|---|
| Focused LSTM suite | **23 passed**, 13 warnings, 34.13 s |
| Affected PI-01–PI-06/API tests | **308 passed, 2 skipped, 281 subtests passed**, 126.90 s |
| `mypy src` / `mypy --platform win32 src` | success, 617 files each |
| API reference | in sync, 631 routes |
| Web build | passed (chunk-size warning only) |
| Real evidence | 10 evaluated, 8 trained, 2 refused; report metrics unchanged |

The pass fixed: a training-run/model-metadata report-ID mismatch; inference
model lookup that could select a model trained for a different target span;
`tracemalloc` state left active after a failed training; acceptance of a
non-finite validation loss; loss of isolated candidate/suitability/registration
failures from the final result and report; acceptance of a non-finite scaler
target mean on model load; conflicting LSTM `engine`/`model_id` requests; and
an inaccurate split-boundary claim in the sequence documentation. Regression
tests now pin the report-ID equality, target-span rejection, post-cutoff row
exclusion, failed-training tracing cleanup, HTTP conflict rejection and the
application/API path.

**Remaining unverified requirements:** browser click-through and visual
screenshots were not performed; the full repository suite was not run; RELIANCE
90d and ADANIENT 1h could not be trained because the stored history is below
the configured minimum; fundamentals/news context was available through PI-04
but not exercised with supplied rows. No model was promoted, and no calibrated
probability, risk or execution claim is made. The implementation verdict
remains **PASS for the stated PI-07 scope**, with the negative model-performance
and interval-coverage findings recorded above.
