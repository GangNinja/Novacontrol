# PI-08 — Ensemble Forecasting and Uncertainty Calibration

**Status:** **Scoped PASS for the PI-08 implementation, integration,
persistence, failure-isolation and verification scope after an independent
audit**, with the model-performance, data-coverage and unverified-runtime
limitations recorded below. The audit found and fixed defects that were not
captured by the first completion pass; this report now includes those results.
**Branch:** `feature/novamarket` (working tree only; nothing staged, committed,
tagged, pushed, merged or released).
**Date:** 2026-11-15.
**Evidence classification:** real stored RELIANCE daily bars for the ensemble
results; deterministic synthetic fixtures for aggregation, calibration,
abstention/fallback and API tests. Synthetic fixtures are not presented as
market performance.

## 1. Initial repository assessment

The phase started from the existing PI-01 through PI-07 implementation and read
their completion reports rather than assuming a phase was complete because
files existed.

| Prerequisite | Evidence | Status |
|---|---|---|
| PI-01 contracts, boundaries, lifecycle, prediction/model stores | `docs/PI01_COMPLETION_REPORT.md`; `prediction/models.py`; `prediction/ensemble.py`; `prediction/store.py` | PASS for PI-01 scope |
| PI-02 historical datasets and chronological splits | `docs/PI02_COMPLETION_REPORT.md`; `datasets/`; `build_training_dataset` | PASS, with recorded provider/data limitations |
| PI-03 market-data gateway and freshness | `docs/PI03_COMPLETION_REPORT.md`; `marketdata/gateway.py` | PASS for gateway scope |
| PI-04 features and market regimes | `docs/PI04_COMPLETION_REPORT.md`; `features/`; `pi04-feature-set-1.0` | PASS |
| PI-05 statistical baselines | `docs/PI05_COMPLETION_REPORT.md`; `statistical/` | PASS |
| PI-06 gradient boosting | `docs/PI06_COMPLETION_REPORT.md`; `gradient/` | PASS |
| PI-07 LSTM | `docs/PI07_COMPLETION_REPORT.md`; `lstm/` | PASS |

The inspected working tree was already dirty with PI-01–PI-08 work and
untracked generated files. Existing user changes were preserved. No full
repository suite was run; focused and affected-area verification was used as
the phase plan requires.

## 2. Existing functionality reused

| Existing functionality | Reuse in PI-08 |
|---|---|
| PI-01 `ForecastOutput`, `ModelMetadata`, `ModelPrediction`, `PredictionPolicy`, `EnsembleAssembler`, ID helpers, validity/probability semantics | Every constituent is parsed into the PI-01 contract; aggregation is delegated to the existing assembler |
| PI-01 `PredictionStore` model/forecast registry | Eligibility evidence, versioned configs, calibrations and evaluations |
| Existing `ForecastStore` | Ensemble forecast runs use `kind="ensemble"`; no parallel run store |
| PI-02 chronological training data and dataset versioning | Evidence harness trains only on an early prefix; test origins are later |
| PI-03 freshness/quality contracts | Point-in-time freshness validation; no stale contribution is silently used |
| PI-04 feature-set version and regime labels | Config/forecast metadata and regime-scoped evaluation |
| PI-05 statistical service and metrics | Eligible baseline constituents and same-origin comparison |
| PI-06 gradient service, artifacts, suitability status and metrics | XGBoost/LightGBM constituents; `baselines_preferred` is honored |
| PI-07 LSTM service, sequence artifacts and metrics | LSTM constituent when its registry evidence/artifact is eligible |
| Existing `/market/forecasts*` routes, `ApiSurface`, route consumers, `docs/API.md` | Ensemble engine routing, calibration/explain branches; no duplicate routes |
| Existing React Forecasts page | Ensemble engine option, contribution/uncertainty/abstention cards |

No duplicate prediction contract, model registry, feature pipeline, forecast
store, evaluation engine or HTTP endpoint family was created.

## 3. Constituent models integrated

The service discovers the model families through the existing registry and
family services:

- PI-05 statistical baselines: naive/last, random walk, historical return,
  seasonal naive, SMA, WMA, EMA, SES, Holt, Holt-Winters and ARIMA when their
  evidence is usable.
- PI-06 gradient boosting: XGBoost, LightGBM and their registered artifacts.
- PI-07 LSTM: sequential PyTorch models and their registered scaler/artifact.

Each contribution keeps its original model id and version. A family is not
required to participate in every horizon: a model whose evidence, status,
horizon support, artifact, scaler or point-in-time availability fails is
excluded with a reason. In the recorded evidence the 1d ensemble included an
LSTM plus statistical baselines; the 5d ensemble included LightGBM plus
statistical baselines. The 30d ensemble used the existing registered
statistical artifacts because fresh training was refused by the data limits.

## 4. Eligibility rules

Every candidate receives an explicit `EligibilityDecision`. Exclusion reasons
include:

- unavailable, suspended or retired state;
- `baselines_preferred`, `failed`, `unsupported`, `insufficient_data` or other
  existing unsuitable evaluation status;
- no validation/walk-forward evidence;
- fewer than the configured minimum validation samples;
- non-finite or negative error;
- unsupported horizon or instrument mismatch;
- reference-price, feature-set version or dataset version mismatch with the
  first surviving contributor (the assembler cannot blend different bases);
- missing gradient/LSTM artifact or LSTM scaler;
- a persistent gradient/LSTM artifact trained after the forecast cutoff;
- an actually stale contribution (the bar's age exceeds the horizon's maximum
  input interval), a future-observed bar, a missing/provider-unavailable
  observation, invalid values, or incompatible contract fields.

A statistical baseline is refitted by PI-05 from the supplied point-in-time
bars, so its registry `trained_at` is not treated as a persistent-artifact
leakage boundary. Stored ingestion freshness is interpreted relative to the
forecast cutoff: a historical bar observed at the cutoff is current for that
forecast, while a genuinely old bar remains stale and is excluded.

## 5. Weighting methodology

Weights are produced only from validation or walk-forward evidence:

1. Inverse relative MAE against the median eligible MAE.
2. A direction-accuracy factor where validation accuracy exists.
3. Recency decay with a configurable half-life.
4. A penalty for degraded model state.
5. Smoothing toward the previous registered configuration.
6. Bounded-simplex projection (`min_weight=0.05`, `max_weight=0.55`,
   normalized to one).

When `regime_aware` is enabled and the caller supplies a regime (or the
application resolves a current PI-04 regime), a model's per-regime validation
MAE and direction accuracy are used **only** when the model has at least
`min_regime_samples` observations in that regime. Otherwise the ordinary
validation/walk-forward evidence is used. The chosen regime is recorded on the
ensemble result, config row, forecast metadata and snapshot provenance; it is
not invented from a future-dated regime. No real RELIANCE origin in the
available evidence had a usable regime breakdown, so this path is covered by
deterministic tests rather than a real-data claim.

Test-set results are never used to select or reweight models. Every decision
list and the resulting weights are persisted with the config digest, so the
weighting is reproducible. The smoothing and caps prevent a small recent
sample from abruptly switching the whole ensemble.

## 6. Calibration methodology

`calibration.py` implements split-conformal residual quantiles fitted on
validation observations only. The final test observations are scored after the
quantile is fitted and are not fed back into weights or calibration. The
recorded calibration is marked `provisional` with `guarantee=False` while the
minimum sample requirement is not met. If no calibration is registered, the
assembly's weighted constituent interval is labelled
`weighted_constituent_validation_intervals`; it is not described as conformal.
Raw model confidence values are not averaged or treated as calibrated
probabilities. Direction probabilities are carried only when every contributor
supplies calibrated probabilities.

A registered calibration fitted at a different nominal level from the request
is **not applied**. The result stays uncalibrated, records the registered level
and explains the mismatch; an 80% quantile is never relabelled as a 90%
interval. The focused test `test_calibration_with_a_different_nominal_level_is_not_applied`
pins this behavior.

## 7. Model disagreement

`_disagreement_detail()` reports:

- weighted dispersion of normalized returns;
- standard deviation of constituent returns;
- direction vote distribution over up/down/neutral/uncertain;
- directional disagreement;
- the methodology string.

The PI-01 assembler applies the configured warning and abstention thresholds.
Disagreement is treated as uncertainty evidence, not as a predicted reversal.
The recorded 1d/5d/30d test ensembles had disagreement values of approximately
0.0041, 0.0213 and 0.0507 respectively, below the configured abstention
threshold.

## 8. Prediction-interval evaluation

Intervals are evaluated by horizon on the test origins. Coverage is the
fraction of actual outcomes inside the predicted interval; width is the mean
relative interval width. Recorded results:

| Horizon | Interval level | Test coverage | Mean width | Calibration status |
|---|---:|---:|---:|---|
| 1d | 0.80 | 0.80 | 0.0338 | provisional, 10 validation samples |
| 5d | 0.80 | 0.60 | 0.0558 | provisional, 10 validation samples |
| 30d | 0.80 | 0.00 | 0.0429 | provisional, 10 validation samples |

The 1d interval matched its nominal level; the 5d and 30d intervals were too
narrow on this small test set. These results are reported as failures of the
coverage claim, not hidden behind a nominal-level badge. Intervals do not
guarantee future market movement.

The stored real-data ensemble forecasts carry `probability_status=unavailable`
with `p_up`, `p_down` and `p_neutral` all `None`; constituent probability
outputs were not treated as calibrated and were not averaged into a direction
probability. The forecast contract's own `calibration_status` remains
`uncalibrated` because it describes probability calibration, while the interval
calibration used for the test forecasts is recorded separately as provisional.

## 9. Abstention and fallback evidence

Focused tests pin:

- no eligible model returns `abstained` with `forecast=None`;
- one eligible model is a transparent `fallback` with the original model
  identity, `fallback=True`, an `ensfb_` id and a reason;
- disagreement above the abstention threshold abstains rather than averaging;
- one failed constituent is isolated while other eligible constituents
  continue;
- a missing calibration is not invented;
- the real application dispatch rejects an unsupported engine/horizon conflict.

The real-data harness assembled all 30 origins for 1d, 5d and 30d, so no
real-data fallback or disagreement abstention was observed in that sample. The
abstention/fallback behavior is therefore verified with deterministic fixtures,
not claimed as a frequency observed in live markets.

## 10. Individual-model versus ensemble comparison

The same test origins and target representation were used for the family
comparisons. The ensemble did not outperform the best individual model:

| Horizon | Ensemble price MAE | Best recorded individual family | Best family MAE | Ensemble directional accuracy |
|---|---:|---|---:|---:|
| 1d | 14.73 | LightGBM | 12.27 | 0.05 |
| 5d | 36.15 | LightGBM | 30.22 | 0.25 |
| 30d | 94.05 | statistical baseline | 92.83 | 0.00 |

The audit fixed the evaluation harness's random-walk baseline, which had been
setting `predicted_price` to the **actual** price and therefore reported a
zero MAE. The corrected baseline MAEs are 13.70 (1d), 33.67 (5d) and 92.83
(30d). The ensemble still does not beat the best individual model; the 1d/5d
corrected random-walk baseline is also better than the ensemble on those
samples. This is reported as a negative result, not hidden.

Directional accuracy was low for every family in this sample; the market was
mostly neutral/downward and the classifiers had no demonstrated skill. The
ensemble is not claimed to be better merely because it combines models.

## 11. Horizon-specific results

| Horizon | Fresh training | Ensemble test samples | Price MAE | Directional accuracy | Interval coverage |
|---|---|---:|---:|---:|---:|
| 1d | statistical, gradient, LSTM | 20 | 14.73 | 0.05 | 0.80 |
| 5d | statistical, gradient, LSTM | 20 | 36.15 | 0.25 | 0.60 |
| 30d | refused (data limits); existing statistical artifacts used | 20 | 94.05 | 0.00 | 0.00 |
| 90d | not scored | 0 | — | — | — |

The 30d training refusals were: gradient/LSTM only 4 usable validation rows
(5 required), and statistical validation only 204 observations (240 required
for three validation origins plus a 30-bar target). The 90d horizon has no
leakage-safe target window plus validation/test origins in the available daily
history, so it is explicitly unsupported for evidence purposes even though the
code accepts the horizon and will abstain when no eligible constituent exists.

## 12. API and frontend integration evidence

The existing forecast routes accept `engine=ensemble` for generate, list, get,
models, performance, calibration, evaluate and explain. `test_api_forecast_routes_accept_the_ensemble_engine`
passes in the focused suite. The API reference check reports `docs/API.md: in
sync` with no new route family.

The React Forecasts page exposes the Ensemble engine and renders the main
forecast, participating model contribution/weights, disagreement, calibration
status, abstention/fallback reasons and the explain view. `npm run build`
completed successfully (TypeScript and Vite). The page was **not** visually
inspected in a browser during this pass; the build is compile-level evidence,
not a claim about a specific rendered screenshot.

The audit found that the Explain action was gated only to `gbf_` and `lstm_`
forecast ids in the row action and detail modal. It now accepts `ens_` and
`ensfb_` as well. The rebuilt frontend includes that change. The API explain
branch already recognized both ensemble prefixes.

## 13. Tests and actual results

### Focused PI-08 suite

`PYTHONPATH=src python -m pytest tests/test_novamarket_ensemble.py -q`

**28 passed**, including the point-in-time freshness test, the reference/
feature-set/dataset compatibility tests, the point-in-time future/missing-data
test, the calibration-level mismatch test and the regime-evidence test.

The final focused plus API rerun after the last point-in-time regime guard and
the validation-only calibration guard was **80 passed, 232 subtests passed**.

### Affected PI-01–PI-07 regression

`PYTHONPATH=src python -m pytest tests/test_novamarket_api.py tests/test_novamarket_prediction.py tests/test_novamarket_statistical.py tests/test_novamarket_gradient.py tests/test_novamarket_lstm.py tests/test_novamarket_forecasting.py tests/test_novamarket_features.py -q`

**261 passed, 281 subtests passed** (warnings are the existing FastAPI
`on_event` and Starlette/httpx deprecation warnings).

### Static and build checks

- `PYTHONPATH=src python -m mypy src` — no issues in 622 source files.
- `PYTHONPATH=src python -m mypy --platform win32 src` — no issues in 622 source files.
- `PYTHONPATH=src python -m compileall -q src` — passed.
- `PYTHONPATH=src python scripts/generate_api_reference.py --check` — in sync.
- `cd web && npm run build` — passed (Vite chunk-size and dynamic-import
  warnings only).

### Real evidence harness

`PYTHONPATH=src python tmp/pi08/run_real_evidence.py` completed with 256 real
RELIANCE daily bars from 2025-10-29 to 2026-11-11, 10 validation and 20 test
origins per horizon. Evidence is in `tmp/pi08/evidence.json`; the run log is
`tmp/pi08/audit_run5.log`. The rerun generates all validation forecasts first,
fits/registers the validation-only calibration, then generates the test block
against that registered calibration. All 30 origins per horizon assembled in
the rerun; the applied test calibration is recorded as `provisional` with 10
validation samples. The ensemble metrics did not improve after this correction
(they were already scored with the fitted calibration); the correction makes
the evidence path honest, not the market result better.

All recorded origins resolved to the PI-04 regime label `unclassified`, so the
real evidence has no usable regime breakdown; the regime-specific weighting
path is verified by deterministic tests, not by this sample.

## 14. Defects found and fixed

1. **Constituent forecasts were not persisted under the PI-01 lifecycle unique
   key.** PI-05/PI-06 use deterministic shared lifecycle ids, while
   `prediction_forecasts` has `UNIQUE(lifecycle_id, stage)`. Only the first
   constituent could persist, so assembly saw no eligible forecasts. Fixed by
   constructing an ephemeral validated `ForecastOutput` from the returned body
   when the constituent was not persisted; the returned body is still the
   source of the values.
2. **Public/flattened forecast names were not accepted by the contract
   builder.** Constituent bodies expose `reference_price_timestamp`,
   `predicted_direction`, `forecast_target_timestamp` and similar public names.
   Added an explicit public-to-contract name map; no field was invented.
3. **Historical point-in-time bars were treated as stale relative to wall
   clock.** This rejected almost every real historical constituent. Fixed by
   judging freshness relative to the cutoff and the horizon's maximum input
   interval; genuinely old bars still fail.
4. **The assembler received `valid=False` for point-in-time-valid stale-labelled
   bars**, so it reported "0 eligible". Fixed by deriving `ModelPrediction.valid`
   from the same point-in-time rule used for constituent validation.
5. **Persistent artifacts could leak future training.** Added `trained_at <=
   cutoff` filtering for XGBoost/LightGBM/LSTM artifacts; statistical baselines
   are exempt because PI-05 refits on the supplied bars.
6. **Default contributor cap excluded every eligible ML family in the real
   evidence** because ten statistical baselines occupied the top slots. The cap
   was raised to 12 so an eligible LSTM/LightGBM contribution can participate;
   weights remain evidence-ranked and bounded, and no family is forced in.
7. **Calibration/config cutoff filters were tightened** so a calibration or
   previous weight config created after the forecast cutoff cannot be used.

The post-completion audit found additional defects:

8. **The evaluation random-walk baseline used the actual outcome as its own
   prediction.** `predicted_price=row.actual_price` made the baseline MAE zero
   and flattered the ensemble comparison. It now predicts
   `actual_price / (1 + actual_return)` (the reference price), and a test pins
   the corrected baseline. Corrected baseline MAE is 13.70/33.67/92.83 for
   1d/5d/30d; the ensemble remains worse than the best individual model.
9. **A conformal calibration fitted for a different nominal level could be
   applied silently.** The service now checks the requested interval level
   against the registered level, keeps the constituent interval, and records
   the mismatch instead of relabelling an 80% interval as 90%.
10. **Reference price, feature-set version and dataset version were not all
    checked before blending.** Constituents with any of those mismatches are
    excluded with a recorded reason; three parameterized tests cover this.
11. **Future-observed bars and missing/provider-unavailable observations could
    pass the point-in-time check.** They are now rejected; the earlier
    fresh-at-cutoff rule remains for genuinely historical bars.
12. **Regime context was not recorded and `regime_aware` had no effect.**
    Regime is now accepted from the API/caller (or resolved from a current PI-04
    label whose timestamp is at or before the forecast cutoff), persisted in
    config/result/forecast/snapshot metadata, and used for weighting only when
    the model has enough per-regime validation samples.
13. **The ensemble calibration endpoint used `model_id` as an instrument
    fallback.** A missing `instrument_id` now returns an explicit `unavailable`
    response instead of looking up a calibration under a model name.
14. **The React Explain action was gated only to gradient/LSTM ids.** It now
    accepts `ens_` and `ensfb_`; the frontend was rebuilt.
15. **The real evidence harness generated test forecasts before registering
    the validation-only calibration.** It now fits and registers calibration at
    the end of the validation block, generates the test block against it, and
    records the applied calibration (`status=provisional`, 10 validation
    samples) in the last test ensemble.
16. **`fit_calibration` accepted observations without checking their
    validation flag.** A caller could have fitted on test rows despite the
    documented contract. It now refuses any non-validation observation, and a
    focused test pins the refusal.

## 15. Files and artifacts

Implementation:

- `src/novacontrol/novamarket/ensemble/models.py`
- `src/novacontrol/novamarket/ensemble/weighting.py`
- `src/novacontrol/novamarket/ensemble/calibration.py`
- `src/novacontrol/novamarket/ensemble/service.py`
- `src/novacontrol/novamarket/ensemble/__init__.py`
- `src/novacontrol/novamarket/prediction/store.py` (additive PI-08 tables/methods)
- `src/novacontrol/novamarket/service.py` (lazy service and dispatch)
- `src/novacontrol/api/app.py` (ensemble routing/errors/explain)
- `web/src/pages/ForecastModels.tsx` (Ensemble UI)

Verification and documentation:

- `tests/test_novamarket_ensemble.py`
- `tmp/pi08/run_real_evidence.py`
- `tmp/pi08/evidence.json`
- `docs/PREDICTION_INTELLIGENCE_ARCHITECTURE.md`
- `docs/PI08_COMPLETION_REPORT.md` (this report)

## 16. Remaining limitations and final acceptance verdict

### Remaining limitations

- The recorded real-data ensemble did not beat the best individual model on any
  tested horizon, nor the corrected random-walk baseline on 1d/5d, and 5d/30d
  interval coverage was below nominal.
- The 30d evidence used existing registered statistical artifacts because fresh
  training was refused by the available history; it is weaker than the fresh
  1d/5d evidence.
- The 90d horizon has no leakage-safe evidence in the available daily history.
- Disagreement abstention and fallback are verified with deterministic
  fixtures, not observed at frequency in the real sample.
- Every recorded RELIANCE origin resolved to the PI-04 label `unclassified`, so
  the regime-scoped weighting/evaluation path is implemented and tested but has
  no real regime breakdown in this report.
- Constituent calls are sequential and individually exception-isolated. There
  is no per-constituent timeout; a hung family service could still hold the
  request. This path was not exercised under a hung service.
- The frontend was compiled, not visually inspected.
- Model promotion, risk and execution authority remain outside PI-08 and
  unchanged.

### Final acceptance verdict

**Scoped PASS for the PI-08 implementation, integration, persistence,
failure-isolation, API/UI-wiring and verification scope after audit.** All
mandatory implementation criteria are satisfied; the measured ensemble
performance limitations, the unclassified regime sample, the missing
per-constituent timeout and the unviewed UI are recorded above. They prevent
any claim of superior market accuracy or of a fully production-bounded
inference path. PI-09 has not been started, and nothing has been staged,
committed, pushed, tagged, merged or released.
