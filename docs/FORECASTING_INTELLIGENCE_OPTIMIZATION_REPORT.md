# Forecasting Intelligence Optimization Report

**Scope:** post-implementation optimization of NovaMarket's existing forecasting families.  
**Roadmap:** PI-01 to PI-24 are unchanged. This work does **not** create PI-25.  
**Date:** 2026-11-21  
**Evidence:** `tmp/audit/forecast_optimization_evidence.json`  
**Reproduction:** `PYTHONPATH=src ../.venv/Scripts/python.exe tmp/audit/forecast_optimization_audit.py`

---

## Executive summary

The reported scorecard was reproduced exactly. It was produced on `XNSE:ACCEPT`, whose bars have `provider='fixture'`: a synthetic acceptance series, not a market instrument. On that fixture every one of the 20 forecasts predicted `sideways`, which happened to match 12 of 20 outcomes (60%). The Brier score of `0.669111` for `random_walk` was slightly **worse** than the uniform three-class reference of `0.666667`; `bootstrap` was worse still at `0.681321`. The scorecard therefore described the majority-class base rate of a fixture, not forecasting skill.

On eight real daily instruments and 415 untouched test observations:

| Model | Test Brier | Skill vs uniform | Test MAE |
|---|---:|---:|---:|
| `bootstrap` | 0.6423 | +3.66% | 0.0214 |
| `random_walk` | 0.6442 | +3.38% | 0.0213 |
| `historical_mean` | 0.6442 | +3.38% | 0.0214 |
| `ewma` | 0.6800 | -1.99% | 0.0227 |
| `drift` | 0.6976 | -4.65% | 0.0227 |

That is a small, real improvement over an uninformed forecast for the baseline-family models, not a large one. Three-class directional accuracy was only ~36%; accuracy is a poor headline under class imbalance, and no claim of trading profitability is made.

Changes shipped:

1. **Selection now uses a chronological validation slice and reports a later untouched test slice.** Test metrics cannot select a model.
2. **A skill gate against the uniform Brier reference (`2/3`) is enforced.** If no model beats the baseline and uniform forecast, NovaMarket retains `random_walk` and marks the result `abstained`.
3. **Scorecards include `reference_brier` and `brier_skill_score`.** A 0.669 Brier can no longer look acceptable merely because it is near 0.667.
4. **Calibration is now genuinely computable and applicable for all three directions**, with instrument isolation, explicit `calibrated`/`uncalibrated` provenance, and a strict holdout-improvement gate. It is **report-only by default** because the untouched test showed only a weak, inconsistent gain.
5. **Empirical probabilities cannot be exactly 0 or 1**, preventing infinite log loss from a finite sample.
6. **Forecast history for NM-06 defaults to 720 bars instead of 320**, so a validation/test split and the 30-sample calibration floor can actually be reached on instruments with enough history.

Not shipped: equal-weight NM-06 ensemble, new features, deep-learning models, or any change to risk/execution authority. An experimental equal-weight combination did lower test Brier from 0.6423 to 0.6080 on the same 415 observations, but it remains an experiment: the result is not yet validated per asset/horizon through the production selection path, so no strategy consumes it.

---

## 1. Existing forecasting architecture

NovaMarket has several distinct forecasting layers. They must not be conflated.

### 1.1 PI-05 statistical point estimators

`src/novacontrol/novamarket/statistical/` implements eleven estimators:

`naive_last`, `random_walk`, `historical_return`, `seasonal_naive`, `sma`, `wma`, `ema`, `ses`, `holt`, `holt_winters`, `arima`.

`statistical/validation.py` performs a leakage-safe chronological train/validation/test evaluation. The model and its configuration are selected on validation using return MAE, but a challenger must clear a pre-registered 5% relative margin over `random_walk`; otherwise the baseline is retained, and a missing/ineligible baseline makes selection abstain. The test prefix is never used for selection. Metrics include price MAE/RMSE/MAPE/sMAPE, return error, directional accuracy, per-class precision/recall/F1, interval coverage and regime breakdowns. PI-05 estimators produce point forecasts and intervals, not calibrated class probabilities, so Brier/ECE are not applicable there.

### 1.2 NM-06 probabilistic forecasting

`src/novacontrol/novamarket/forecasting/` implements six models:

`zero_return` (point-only baseline), `random_walk`, `historical_mean`, `drift`, `ewma`, `bootstrap`.

The output is a three-class distribution (`up`/`down`/`sideways`, with a 0.5% neutral band), a point return, and a predictive interval. `validation.py` runs non-overlapping walk-forward origins. `calibration.py` fits isotonic maps. `store.py` persists immutable forecasts, model states, selections, training runs, calibration payloads and reliability transitions.

This is the layer behind the reported Brier score. It has no feature-driven classifier: the probability models use historical return samples or a normal approximation. The richer feature set is computed for context and diagnostics.

### 1.3 PI-06/PI-07 and PI-08

PI-06 gradient-boosting and PI-07 LSTM families are separate, optional forecast families. They were not modified or benchmarked here; the brief forbids adding deep models merely to add AI, and the local hardware/resource question does not justify that expansion.

PI-08 is the existing evidence-weighted ensemble. Its weights use validation/walk-forward MAE, direction accuracy, recency and regime evidence; it also applies validation-only conformal intervals. It does not calibrate directional class probabilities. Its weighting code already refuses models without usable evidence.

### 1.4 Consumers

The decision layer reads forecasts through the forecast API; the strategy layer may use a forecast as one analytical input. Risk independently checks forecast freshness, horizon, uncertainty and model state. A forecast never becomes an order instruction, and risk limits are not bypassed by confidence.

---

## 2. Root causes of weak forecasting performance

### 2.1 The reported scorecard came from a fixture

`data/novamarket.sqlite3` contains:

| Provider | Quality | Bars | Instruments |
|---|---|---:|---:|
| `yahoo` | `stale` | 30,674 | 125 |
| `yahoo` | `valid` | 181 | 148 |
| `yahoo` | `suspect` | 27 | 17 |
| `fixture` | `valid` | 1,091 | 1 |

Every `XNSE:ACCEPT` bar is `provider='fixture'`. The original training run `ftr_f93da9550fd0337a` used that instrument, 20 non-overlapping origins, `max_history_bars=320`, `min_training_bars=140`, horizon 3. Reproducing those exact inputs gives:

| Model | Brier | Direction | MAE | Calibration error |
|---|---:|---:|---:|---:|
| `random_walk` | 0.669111 | 0.60 | 0.024157 | 0.042498 |
| `historical_mean` | 0.669111 | 0.60 | 0.024931 | 0.042498 |
| `bootstrap` | 0.681321 | 0.60 | 0.024157 | 0.059375 |
| `drift` | 1.015706 | 0.40 | 0.032933 | 0.280979 |
| `ewma` | 0.924011 | 0.30 | 0.032933 | 0.329629 |

The `random_walk` confusion matrix is entirely in the `predicted=sideways` column: 2 actual up, 6 actual down, 12 actual sideways. The 60% is the fixture's sideways base rate. The uniform reference Brier is `2/3`; `random_walk` skill is `-0.37%`, `bootstrap` skill is `-2.20%`.

### 2.2 Missing baseline and skill reporting

The old scorecard compared 0.669 to 0.681 and to a "baseline" of 0.669, but not to the proper uninformed reference for a three-class distribution. In three classes, a uniform forecast has Brier `2/3`. Reporting only raw Brier made a negative-skill result look acceptable.

### 2.3 Calibration was stored but never applied

`ForecastEngine.train` fitted and persisted calibration reports, but `ForecastEngine.generate` used `output.p_up` directly. A stored report therefore changed no forecast. In addition:

- the calibrator was binary (`p_up` vs not-up) while the forecast is three-class;
- `ForecastStore.calibration(model_id, horizon)` did not filter by instrument, so a newer calibration for another instrument could be returned for the same model/horizon;
- no forecast exposed whether its probabilities were calibrated;
- a "calibrated" status could be recorded even when the holdout Brier did not improve.

The 20-observation scorecard had `status='insufficient'` because `calibration_min_samples=30`. That status was correct, but the scorecard did not label the published probabilities as uncalibrated.

### 2.4 No untouched test slice in NM-06 selection

`ForecastEngine.train` called `evaluate_models` over every available origin and then selected on the same scores. The reported metrics and the selection sample were the same observations. PI-05 already had a train/validation/test split; NM-06 did not.

### 2.5 Selection instability in PI-05

PI-05 selects the lowest validation return MAE. With 11–52 validation origins per real asset/horizon, small MAE differences choose complex models that often lose on the untouched test slice:

| Case | Selected on validation | Test MAE | Random-walk test MAE |
|---|---|---:|---:|
| ADANIENT 1d | `historical_return` | 43.71 | 43.14 |
| RELIANCE 3d | `ema` | 44.32 | 29.40 |
| RELIANCE 5d | `ema` | 43.88 | 39.72 |
| ADANIENT 5d | `ses` | 105.26 | 112.70 |
| ADANIENT 20m | `ses` | 4.649 | 4.868 |

The selector is leakage-safe. The margins between these validation errors were small, so the lowest-MAE rule promoted noise; the new relative-margin gate below removes the marginal promotions but does not create skill where the test slice disagrees. This is a selection-noise problem, not evidence that ARIMA/EMA are inherently better.

### 2.6 Data limitations

- Only two real daily instruments have more than ~250 bars; the rest have roughly 250.
- 30,674 of 31,974 real daily bars are marked `stale`. For historical evaluation this mostly means "old relative to retrieval", but it prevents treating the database as a live multi-year panel.
- No stored 5m, 15m, 30m or 1h bars exist. Intraday evaluation therefore uses the only real 1m series (`XBOM:ADANIENT`, 1,917 bars) and aggregates forecast steps from it.
- 20m and 1h horizons have only 19 and 6 test origins respectively; their metrics are indicative, not precise.
- 30-session and 90-session horizons cannot be evaluated meaningfully on ~250 real daily bars without overlapping targets.
- Daily and 1m timestamps have different bases (`03:45 UTC` bar-start for Yahoo daily, exchange-local minute bars). The pipeline does not reinterpret one interval as another, but session-boundary effects remain.

### 2.7 Class imbalance and probability construction

Most 1–5 session returns fall inside the 0.5% neutral band. A model that always predicts `sideways` can score 36–40% accuracy and a Brier near the base rate. Accuracy is not a sufficient metric. A proper Brier, its uniform reference, skill and calibration error are required together.

---

## 3. Individual model scorecards

### 3.1 NM-06 probabilistic models (real assets)

Eight real daily instruments (`XNSE:ACCEPT` excluded), horizons 1/3/5 sessions, 70/30 chronological split of walk-forward origins, 950 validation and 415 test observations. `zero_return` is point-only and has no Brier.

| Model | Val Brier | Val skill | Test Brier | Test skill | Test MAE | Test ECE | Forecast change |
|---|---:|---:|---:|---:|---:|---:|---:|
| `bootstrap` | 0.6621 | +0.68% | **0.6423** | **+3.66%** | 0.0214 | 0.1070 | 0.00134 |
| `random_walk` | 0.6625 | +0.62% | 0.6442 | +3.38% | 0.0213 | 0.0725 | 0.00023 |
| `historical_mean` | 0.6625 | +0.62% | 0.6442 | +3.38% | 0.0214 | 0.0725 | 0.00020 |
| `ewma` | 0.6827 | -2.40% | 0.6800 | -1.99% | 0.0227 | 0.1844 | 0.00362 |
| `drift` | 0.6792 | -1.88% | 0.6976 | -4.65% | 0.0227 | 0.1905 | 0.00362 |
| `zero_return` | — | — | — | — | **0.0212** | — | 0.00000 |

`reference_brier` is `0.666667` in every probabilistic row. Skill is `1 - Brier / 0.666667`.

Observations:

- `bootstrap` is the best probabilistic model on the untouched test slice, but its advantage over `random_walk` is 0.0019 Brier (0.3% relative), well inside sampling noise.
- `historical_mean` and `random_walk` are effectively identical on these short horizons.
- `drift` and `ewma` are actively worse than the uniform forecast; their calibration error is 18–19%.
- `zero_return` has the lowest MAE but is not a probabilistic model. It cannot be selected for a probabilistic forecast without inventing a distribution.
- The best model's test Brier varies by case from 0.52 to 0.74. Asset/horizon heterogeneity is larger than the average model differences.

### 3.2 The synthetic fixture, for comparison

The same audit on `XNSE:ACCEPT` alone (1,091 bars, 1,019 validation / 439 test observations) gives test Brier 0.2589–0.4234 and accuracy 64–85%. These numbers are not market evidence and are not used for any model decision. They explain why the original 20-origin number looked better than real-asset results.

### 3.3 PI-05 eleven point estimators (real assets)

Test observations are pooled over `XBOM:ADANIENT`, `XNSE:RELIANCE` for 1d/3d/5d and the real 1m `XBOM:ADANIENT` series for 1m/5m/20m/1h. Sample counts differ because a model that refuses a fit is skipped.

| Model | n | Price MAE | Price RMSE | sMAPE | Direction | 90% coverage | Mean latency |
|---|---:|---:|---:|---:|---:|---:|---:|
| `naive_last` | 643 | **11.380** | **15.387** | **0.005327** | 80.09% | — | 0.07 ms |
| `random_walk` | 643 | 11.380 | 15.387 | 0.005327 | 80.09% | 81.18% | 1.99 ms |
| `historical_return` | 643 | 11.500 | 15.550 | 0.005347 | 80.09% | 81.18% | 1.87 ms |
| `arima` | 643 | 11.484 | 15.544 | 0.005344 | 80.09% | 86.47% | 10.70 ms |
| `ses` | 643 | 13.584 | 17.760 | 0.006484 | 79.63% | 84.14% | 1.25 ms |
| `holt` | 643 | 14.153 | 18.360 | 0.006627 | 79.78% | 85.85% | 1.04 ms |
| `wma` | 643 | 18.441 | 23.377 | 0.008790 | 79.78% | 86.63% | 2.33 ms |
| `seasonal_naive` | 643 | 19.835 | 24.743 | 0.009331 | 80.40% | 84.76% | 1.04 ms |
| `ema` | 643 | 19.962 | 25.154 | 0.009486 | 79.78% | 86.16% | 2.22 ms |
| `sma` | 643 | 22.413 | 27.930 | 0.010580 | 79.94% | 86.31% | 1.28 ms |
| `holt_winters` | 5 | 46.686 | 48.664 | 0.036369 | 20.00% | 20.00% | refused on daily data |

`holt_winters` refuses the daily series because seasonal amplitude is not distinguishable from residual noise; the five observations are intraday cases where a fit was possible. It should not be selected automatically on these assets.

The 80% directional accuracy is not evidence of edge: the neutral class dominates. `naive_last` and `random_walk` have the best point error; ARIMA's nominal interval coverage is better but its MAE is not better. Adding complexity did not improve out-of-sample point accuracy.

### 3.4 PI-05 selection outcomes

Before the margin gate, the validation-selected model beat the random-walk test MAE in three of ten real asset/horizon cases, tied in four and lost in three. That is not a reliable selection advantage.

With the pre-registered 5% margin applied to the same validation scores, six of ten cases retained `random_walk` and four challengers cleared the hurdle. Only one case changed its choice: ADANIENT 1d moved from `historical_return` to `random_walk`, and its test MAE fell from 43.712 to 43.142. The other nine choices were unchanged, so the weighted selected test MAE moved from 11.763 to 11.717 (−0.39%). It remains above `random_walk`'s 11.380 on these cases because two EMA choices (RELIANCE 3d/5d) cleared the 5% validation margin and still lost on test. A validation margin filters marginal promotions; it cannot guarantee that a genuine validation advantage transfers.

The rule is pre-registered in code (`SELECTION_MARGIN_PCT = 5.0`) and recorded on every report with the baseline MAE, challenger MAE, required MAE and decision. It is not tuned against the test slice.

---

## 4. Feature engineering changes

No new features were added, because no feature demonstrated out-of-sample value in a model that can consume it.

The existing feature builder already computes:

- returns (`ret_1`, `ret_5`, `ret_21`),
- volatility (`vol_21`, `vol_63`, `atr_14_pct`, `range_20_pct`),
- moving-average relationships (`sma_20_gap`, `sma_60_gap`, `ema_12_26`),
- momentum (`momentum_10`, `trend_slope_20`),
- relative volume and drawdown,
- RSI and a regime classification.

Data-quality handling was audited, not expanded:

- duplicate timestamps are collapsed by `_usable_bars`;
- non-finite or impossible OHLC bars are discarded;
- partial bars and bars after the cutoff are excluded;
- invalid/rejected bars are excluded by the engine;
- stale bars are retained for historical training but cannot pass the live freshness gate.

The NM-06 probability models consume closes, not these features. PI-05 consumes closes and, where configured, bars for ARIMA/seasonality. Adding a feature-conditioned classifier would be a new model family and is not justified by the current evidence or permitted by "do not add arbitrary forecasting models." Feature value remains an experimental hypothesis, not a measured improvement.

No look-ahead leakage was found: every estimator receives `closes[:origin]`, targets are strictly later, and the selection/test split is chronological. The tests in `tests/test_novamarket_forecasting.py` and `tests/test_novamarket_statistical.py` assert these properties.

---

## 5. Model-selection improvements

### 5.1 NM-06: validation-only selection with a test slice

`ForecastEngine.train` now:

1. builds all walk-forward origins;
2. splits them 70/30 (`validation_fraction`);
3. evaluates models on the earlier validation origins;
4. selects and fits calibration on validation only;
5. evaluates the later origins once as a test slice and records `test_scores`/`test_origins`;
6. never feeds test scores into selection.

The split is by origin index, not random rows. Targets remain strictly after each origin. A short test slice is reported with its sample count rather than being silently merged into validation.

### 5.2 Skill gate and abstention

`ModelScore` now carries:

- `reference_brier = 2/3` for a uniform three-class forecast;
- `brier_skill_score = 1 - brier/reference_brier`.

Selection requires all of:

- the challenger beats `random_walk` on validation Brier;
- the challenger beats the uniform reference (`brier_skill_score > 0`).

If not, the selected model is `random_walk`, `abstained=true`, and the selection metrics record the reason. The forecast is still produced as an analytical baseline; it is not presented as a confident edge. On the original 20-observation fixture scorecard, the new gate would abstain because the baseline itself had negative skill.

Validation scorecards and training runs include the reference and skill. This makes a near-0.667 Brier visibly non-informative.

### 5.3 History window

`max_history_bars` increased from 320 to 720. This is not itself an accuracy claim. It makes more non-overlapping origins available and lets the 30-sample calibration floor be reached on longer series. Real instruments with ~250 bars are unaffected.

### 5.4 PI-05 selection

PI-05 now retains `random_walk` unless a challenger's validation return MAE is at least 5% lower. The margin is a fixed module constant, not a request parameter, so it cannot be selected after seeing test results. `_select_with_margin` is the single decision point; it records `selection_decision`, `challenger_model`, `baseline_validation_mae`, `challenger_validation_mae` and `required_challenger_mae` in the evaluation report. `selection_margin_pct` remains configurable only for explicit experiments through `evaluate_models`; the production service uses the default.

`random_walk` is evaluated as the comparison reference even when a caller requests a narrower model list, so the gate cannot be bypassed. When the baseline has too few finite validation observations or fails to produce them, selection raises `StatisticalValidationError` instead of promoting a model against an unknown reference. That is the abstain path: no forecast is selected and the service surfaces the failure rather than inventing a baseline.

---

## 6. Ensemble evaluation

The existing PI-08 ensemble already uses validation-only evidence weights and conformal intervals. It was inspected but not altered.

For NM-06, an experimental equal-weight combination of the five probabilistic members was evaluated on the same eight real instruments and 415 untouched test observations:

| Combination | Test Brier | Test samples |
|---|---:|---:|
| Equal-weight five-model average | **0.6080** | 415 |
| Best individual (`bootstrap`) | 0.6423 | 415 |
| `random_walk` | 0.6442 | 415 |
| Uniform reference | 0.6667 | 415 |

The equal-weight average reduces Brier by 0.0343 (5.3% relative) versus the best individual. This is a measured result, not a hypothesis. It is also not yet a shipped capability:

- the gain is concentrated in the 3- and 5-session cases;
- equal weighting duplicates the near-identical `random_walk`/`historical_mean` pair;
- the combination has no separate calibration or interval treatment;
- no production selection path or strategy consumer was changed to use it;
- the result has not been validated per asset through the full training/activation cycle.

The ensemble is therefore an experimental candidate for the next iteration. It was not adopted merely to increase model count. If adopted later, it should be a named combination with its own validation record and must not bypass risk controls.

---

## 7. Probability calibration results

### 7.1 What changed

`calibration.py` now supports:

- binary isotonic calibration (backward compatible);
- three-class one-vs-rest isotonic maps with renormalisation;
- three-class temperature scaling;
- a validation holdout that chooses between isotonic and temperature candidates;
- a strict acceptance rule: the holdout multiclass Brier must improve, not merely tie.

Acceptance is computed on the later portion of validation. The untouched test slice is never used to fit or choose a calibrator.

`ForecastRecord` now exposes `probability_status` (`calibrated`, `uncalibrated`, `not_applicable`) and `calibration_id`. Generated notes state whether calibration is report-only, rejected or insufficient. The store's calibration lookup is filtered by instrument; one instrument's calibrator can never be applied to another.

### 7.2 Real-data result

Across 24 asset/horizon cases:

- 20 had insufficient samples or no probabilistic model;
- 4 produced an accepted validation calibrator (all temperature scaling, 1-session);
- 20 were reported as `rejected`/`insufficient`, never as calibrated.

For the four accepted cases, the untouched test result was mixed:

| Case | Model | Validation Brier before → after | Test Brier raw → calibrated |
|---|---|---:|---:|
| `XBOM:ADANIENT` 1d | `bootstrap` | 0.6978 → 0.6782 | 0.6590 → 0.6597 |
| `XNSE:ADANIENT` 1d | `ewma` | 0.7488 → 0.7149 | 0.6525 → 0.6426 |
| `XNSE:ADANIPORTS` 1d | `ewma` | 0.6891 → 0.6681 | 0.6847 → 0.6632 |
| `XNSE:BAJAJ-AUTO` 1d | `random_walk` | 0.6983 → 0.6839 | 0.6583 → 0.6599 |

Sample-weighted test Brier for these four cases improved from **0.6635 to 0.6564** (1.1% relative), but one case regressed, the sample is only 135 observations, and all accepted cases are 1-session. That is not enough to justify applying calibration to every forecast.

**Decision:** calibration is computed, gated and reported, but `apply_calibration=False` by default. When disabled, generated probabilities remain raw and are explicitly labelled `uncalibrated` with a report-only note. This fixes the silent wiring defect without shipping a change that is not reliably better. An operator can enable application only after a larger validation panel supports it.

### 7.3 The original 20-observation problem

The original 20 samples were below the 30-sample floor. The correct status was `insufficient`, and the new output says so. No calibrator was invented for that sample. The apparent "0.669 vs 0.667" problem was not a calibration failure; it was a base-rate/fixture problem plus missing skill reporting.

---

## 8. Multi-horizon performance

| Horizon | Data | Test origins | Result |
|---|---|---:|---|
| 1 session | 8 real daily instruments | 269 | Best individual Brier 0.644–0.687 per case; selection unstable |
| 3 sessions | 8 real daily instruments | 90 | Best individual Brier 0.523–0.684 per case; selection unstable |
| 5 sessions | 8 real daily instruments | 56 | Best individual Brier 0.522–0.667 per case |
| 1 minute | 1m `XBOM:ADANIENT` | 384 | `naive_last` MAE 1.377; directional 100% is trivial under the neutral band |
| 5 minutes | 1m `XBOM:ADANIENT` | 76 | `naive_last` MAE 2.892; same caveat |
| 20 minutes | 1m `XBOM:ADANIENT` | 19 | `ses` MAE 4.649 vs random-walk 4.868; tiny sample |
| 1 hour | 1m `XBOM:ADANIENT` | 6 | `naive_last` MAE 7.992; too few origins |
| 30 sessions | real daily | insufficient | Not evaluated: ~250 bars cannot support non-overlapping 30-session test origins |
| 90 sessions | real daily | insufficient | Not evaluated: ~2–3 non-overlapping origins on the longest real series |

The 1-minute and 5-minute directional accuracies of 100% are class-imbalance artifacts, not evidence of a profitable intraday model. The 20-minute and 1-hour results are below any reasonable sample threshold and are reported only to show the execution path exists.

The NM-06 execution path supports daily horizons only (`next_session`, `few_sessions`, `one_week`, `one_month`, `one_quarter`). PI-05 supports 1m/5m/20m/1h through its own validation path. No horizon is claimed supported beyond what was actually run.

---

## 9. Out-of-sample comparisons

All comparisons below are chronological. Test observations were not used for fitting, selection or calibration.

### 9.1 Exact original scorecard vs new gate

| Model | Original Brier | Original skill | New decision |
|---|---:|---:|---|
| `random_walk` | 0.669111 | -0.37% | baseline; abstain |
| `historical_mean` | 0.669111 | -0.37% | not better |
| `bootstrap` | 0.681321 | -2.20% | not better |
| `drift` | 1.015706 | -52.36% | rejected |
| `ewma` | 0.924011 | -38.60% | rejected |

The new gate would not select `bootstrap` or any challenger on this sample. It retains the baseline and marks the result as having no demonstrated skill.

### 9.2 Real-asset vs baselines

- Best NM-06: `bootstrap` test Brier 0.6423, skill +3.66%; `random_walk` 0.6442, skill +3.38%.
- `drift` and `ewma` are worse than uniform on test.
- PI-05 point error: `naive_last`/`random_walk` 11.38 MAE; ARIMA 11.48; SMA 22.41.
- The equal-weight NM-06 experiment: 0.6080 Brier, 5.3% below the best individual, but experimental and not shipped.

### 9.3 What is not claimed

- No model is claimed profitable. Prediction accuracy and Brier skill are not strategy P&L.
- No transaction costs, slippage, survivorship correction or execution assumptions were added.
- No statistically significant superiority is claimed for `bootstrap` over `random_walk`.
- No claim is made that calibration improves live forecasts; it improved four validation-selected cases but is off by default.

---

## 10. Performance and resource measurements

Measured on the local Windows machine with the project virtual environment.

### 10.1 NM-06 inference latency

30 calls on 320 daily closes, horizon 3:

| Model | Mean | p95 | Max |
|---|---:|---:|---:|
| `zero_return` | 0.049 ms | 0.079 ms | 0.276 ms |
| `random_walk` | 0.052 ms | 0.053 ms | 0.067 ms |
| `ewma` | 0.078 ms | 0.081 ms | 0.091 ms |
| `historical_mean` | 0.095 ms | 0.098 ms | 0.109 ms |
| `drift` | 0.099 ms | 0.101 ms | 0.108 ms |
| `bootstrap` | 1.117 ms | 1.211 ms | 1.527 ms |

### 10.2 PI-05 inference latency

Five fits on 320 daily closes, one-step horizon:

| Model | Mean | Max |
|---|---:|---:|
| `naive_last` | 0.07 ms | 0.10 ms |
| `holt` | 1.04 ms | 1.39 ms |
| `seasonal_naive` | 1.04 ms | 2.26 ms |
| `ses` | 1.25 ms | 1.45 ms |
| `sma` | 1.28 ms | 1.70 ms |
| `historical_return` | 1.87 ms | 1.87 ms |
| `random_walk` | 1.99 ms | 2.22 ms |
| `ema` | 2.22 ms | 2.42 ms |
| `wma` | 2.33 ms | 3.35 ms |
| `arima` | 10.70 ms | 14.73 ms |

`holt_winters` refused the daily series and is absent from the latency table.

### 10.3 Audit cost

- Wall time: **51.9 seconds** for 123 real instruments loaded, 24 NM-06 cases and 10 PI-05 cases.
- Audit process working set observed at ~118 MB while running (Windows process measurement).
- No new dependency was added. The implementation uses the existing Python standard library and project code.
- Training cost is dominated by repeated bootstrap resampling and PI-05 ARIMA fits; no GPU is involved.

The local-first architecture is preserved. The changes add no service, container, model server or heavyweight package.

---

## 11. Regression test results

New regression tests cover:

- stored calibrator application when the policy enables it;
- default report-only calibration and explicit `uncalibrated` provenance;
- cross-instrument calibrator isolation;
- multiclass calibration improving holdout Brier;
- harmful calibrator rejection and non-`calibrated` status;
- uniform reference Brier and skill calculation;
- test-slice reporting without test-based selection;
- abstention to `random_walk` when no model has positive skill;
- empirical probabilities never exactly 0 or 1;
- a PI-05 challenger below the 5% validation margin leaves `random_walk` selected;
- a challenger exactly at and above the margin is selected;
- a missing or under-sampled `random_walk` baseline abstains;
- invalid PI-05 margins (negative, 100%, NaN) are rejected.

Targeted result:

```
tests/test_novamarket_forecasting.py
78 passed, 35 subtests passed

tests/test_novamarket_statistical.py
40 passed, 14 subtests passed
```

`mypy src`: **Success: no issues found in 724 source files**.  
`generate_api_reference.py --check`: **in sync (935 routes)**.

Complete NovaMarket regression suite, run on the final code:

```
1955 passed, 2 skipped, 883 warnings, 447 subtests passed in 759.45s
```

Recorded in `tmp/audit/full_suite_margin_final.log`. The 1,940-test pre-change baseline plus nine forecasting-optimization regressions and six PI-05 margin regressions accounts for the count. The forecasting changes do not touch risk, execution or strategy authority. No commit or push was performed.

---

## 12. Remaining limitations

1. **Fixture contamination in the historical record.** The original scorecard should not be cited as market evidence. The fixture remains useful as a deterministic acceptance test.
2. **Small real panel.** Two real daily instruments dominate; eight assets have only ~250 bars. Calibration and per-horizon conclusions are correspondingly weak.
3. **Stale flags.** Most real daily bars are `stale` relative to retrieval. Historical evaluation is possible, but live operation depends on fresh ingestion.
4. **No real 5m/15m/30m/1h history.** Intraday conclusions come from one 1m series; 20m/1h have too few origins.
5. **PI-05 margin is not a significance test.** The 5% gate removes marginal promotions, but a challenger can still clear it and lose out of sample (RELIANCE 3d/5d). Paired loss-difference confidence intervals are not implemented.
6. **No feature-conditioned model.** Existing features are diagnostics, not inputs to NM-06 probabilities. Their incremental value is untested.
7. **Calibration is report-only.** The code path works, but no broad out-of-sample gain is established.
8. **Ensemble is experimental.** The equal-weight result is encouraging but not part of production selection or risk consumption.
9. **No transaction-cost or P&L evaluation.** Nothing here establishes profitability.
10. **Only one random seed / deterministic bootstrap.** Bootstrap stability across seeds was not exhaustively measured.

---

## 13. Recommendations for further experimentation

1. **Ingest a longer, fresh real panel.** At least 2–3 years of daily bars for 20–30 instruments, with a documented provider and quality policy, before tuning selection margins or calibration.
2. **Evaluate whether 5% is the right margin on a larger panel.** The gate is now implemented and pre-registered; do not retune it against the current test cases. Compare a small set of pre-declared margins on a new, longer panel and retain the one with consistent out-of-sample behavior.
3. **Pre-register the ensemble.** If the equal-weight NM-06 combination is pursued, define its members, weighting, calibration and abstention rule before scoring test data. Report per-asset results, not only pooled Brier.
4. **Feature-value experiment.** Test one feature-conditioned probabilistic model against the return-only baselines with the same walk-forward split. Retain it only if Brier skill and calibration improve out of sample.
5. **Calibration sample expansion.** Re-evaluate temperature/isotonic calibration on the larger panel; keep it report-only until the accepted cases show consistent test improvement.
6. **Intraday data acquisition.** Obtain real 5m/15m/30m/1h bars with session-aware timestamps before claiming those horizons.
7. **Regime analysis.** With more data, test whether validation-selected models differ by volatility/trend regime; require minimum regime samples before use.
8. **Significance testing.** Add paired loss-difference confidence intervals (e.g., block bootstrap over origins) before promoting a challenger over the baseline.

---

## Appendix A — Code changes

| File | Change |
|---|---|
| `forecasting/models.py` | `ForecastPolicy`: 720-bar default, `validation_fraction`, `calibration_min_improvement`, `apply_calibration`; `ForecastRecord.probability_status`/`calibration_id` |
| `forecasting/estimators.py` | probability floor; no literal 0/1 |
| `forecasting/metrics.py` | `reference_brier`, `brier_skill_score`, `mean_forecast_change` |
| `forecasting/validation.py` | explicit origin slices; three-class calibration samples; reference/skill; stability |
| `forecasting/calibration.py` | multiclass isotonic + temperature; strict gate; calibrator decoding |
| `forecasting/engine.py` | validation/test split; skill gate and abstention; calibration application helper; provenance |
| `forecasting/store.py` | instrument-filtered calibration lookup |
| `tests/test_novamarket_forecasting.py` | 9 optimization regressions |

No PI-25, roadmap, risk, execution or strategy-authority file was changed.

## Appendix B — Evidence files

- `tmp/audit/forecast_optimization_audit.py` — reproducible audit.
- `tmp/audit/forecast_optimization_evidence.json` — exact scores, calibration cases, latency, fixture separation.
- `tmp/audit/forecast_optimization_audit.log` — run log.
- `tmp/audit/full_suite_final.log` — full regression log for the final code.

## Appendix C — Commands

```bash
cd novamarket
PYTHONPATH=src ../.venv/Scripts/python.exe tmp/audit/forecast_optimization_audit.py
PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest tests/test_novamarket_forecasting.py -q
PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest tests/test_novamarket_*.py -q
../.venv/Scripts/python.exe -m mypy src/novacontrol/novamarket/forecasting
```
