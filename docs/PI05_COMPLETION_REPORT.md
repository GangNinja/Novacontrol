# PI-05 Completion Report — Statistical Baseline Forecasting Models

**Phase:** NovaMarket Prediction Intelligence PI-05
**Branch:** `feature/novamarket`
**Date:** 2026-11-14
**Contract:** `pi05.statistical-forecast.1.0`
**Model version:** `pi05-stat-1.0`
**Methodology:** `pi05-statistical-methodology-1.0`
**Verdict:** **PASS for the PI-05 scope, with the limitations in §13.**
PI-06 was not started.

This is the single consolidated PI-05 report. It reports measured results, not
targets. Synthetic unit fixtures and real project-history runs are labelled
separately; no accuracy claim is made from synthetic data.

---

## 1. Initial repository assessment

The mandatory inspection was completed before implementation:

- Git branch was `feature/novamarket`; existing working-tree changes were left
  untouched. Nothing was staged, committed, pushed, tagged, merged or deployed.
- Prerequisite reports were read:
  - PI-01: PASS for its stated scope; whole-repository regression reported
    PARTIAL by the prior phase.
  - PI-02: PASS with reported provider/data limitations.
  - PI-03: PASS for the gateway scope, with live-provider coverage limitations.
  - PI-04: PASS for the feature/regime scope.
- The prediction contracts (`ForecastOutput`, horizons, model metadata,
  `PredictionStore`), PI-02 dataset engine/store, PI-03 gateway, PI-04 feature
  and regime engine, NM-06 forecast engine/store, existing evaluation tables,
  API routes and architecture contracts were inspected.
- The existing NM-06 forecasting machinery and PI-01 persistence were reused.
  No duplicate prediction, dataset, feature, risk or execution engine was
  created. No new database was created.

The only dependency constraint discovered was material: `statsmodels`, pandas,
scipy and pmdarima were absent; only the standard library plus numpy was
available. Adding a new statistical dependency was not requested and would have
changed deployment requirements, so PI-05 ships a bounded pure-stdlib ARIMA.

## 2. Existing functionality reused

| Concern | Existing authority reused |
|---|---|
| Forecast contract | PI-01 `ForecastOutput`, `ModelMetadata`, `ModelState`, `ForecastHorizon`, `HORIZON_SPECS`, `PredictionStore` |
| Forecast result persistence | `PredictionStore.prediction_forecasts` (existing immutable table) |
| Model metadata | `PredictionStore.prediction_models` and `register_model` |
| Evaluation history | Existing `ForecastStore` training-run and performance rows; the shared `forecast_selection` row remains NM-06-owned |
| Market observations | NM-02 `MarketDataEngine`/`MarketDataStore`, or explicit validated bars |
| Dataset input | PI-02 `HistoricalDatasetService.metadata/bars`, with the dataset version and training-usability gate |
| Regime/feature input | PI-04 `FeatureService.engine.generate`, including `feature_set.version` and `regime.primary` |
| API surface | Existing `/market/forecasts*` routes, extended with `engine=statistical`; the route count stayed at 223 |
| Risk / execution | NM-08 and NM-10 remain the sole authorities; PI-05 has neither path |

## 3. Models implemented

All IDs below are canonical (`statistical/models.py`); aliases normalise to them.

1. **Naive/last-value** — `naive_last`: latest completed close; explicitly no
   interval support.
2. **Random-walk baseline** — `random_walk`: latest close plus empirical
   historical level-change interval; no exploitable trend claim.
3. **Historical-return baseline** — `historical_return`: mean historical log
   return projected to the target; empirical return interval.
4. **Seasonal-naive baseline** — `seasonal_naive`: equivalent prior seasonal
   phase; refuses when fewer than two cycles exist.
5. **Simple moving average** — `sma`: the recent level is the explicit
   next-price forecast (not merely a technical indicator).
6. **Weighted moving average** — `wma`: linearly weighted recent level.
7. **Exponential moving average** — `ema`: EMA level forecast.
8. **Simple exponential smoothing** — `ses`: level smoothing, configurable alpha.
9. **Holt linear trend** — `holt`: level + trend, optional damped trend.
10. **Holt-Winters** — `holt_winters`: additive seasonal smoothing, refused on
    weak/indistinguishable seasonality.
11. **ARIMA** — `arima`: bounded ARIMA(p,d,q), ADF-style stationarity
    assessment, optional configured/auto differencing, conditional least
    squares for AR/ARMA terms, AR companion-radius stability rejection,
    Ljung-Box residual diagnostics and normal-residual intervals.

Direction classification is a first-class output for every model:
`up`, `down`, `neutral` (inside the configurable neutral band) and `uncertain`
(supported interval straddles zero). Probabilities are never fabricated.

## 4. Model configurations

`ModelConfig` records: window (2–500), alpha/beta/gamma, seasonal period,
`seasonal`, ARIMA order (each 0–3; p+q bounded), `auto_differencing`,
`damped_trend`, interval level (default 0.80), neutral band (default 0.5%),
minimum observations (default 30), maximum ARIMA order, low-seasonality override
and adjusted-price preference. Every fit stores the configuration digest.

Bounded candidate grids used by evaluation:

- moving averages: windows 5, 10, 20, 50 and the caller's window;
- SES: the caller's alpha plus 0.2, 0.5 and 0.8;
- Holt: the caller's alpha/beta pair plus three standard pairs;
- Holt-Winters: configured/5/20-bar seasons, `seasonal=true`;
- ARIMA: the caller's configured order plus `(0,1,0)`, `(1,1,0)`,
  `(0,1,1)`, `(1,1,1)`, `(2,1,1)` and `(1,1,2)`, with candidates outside a
  user-supplied `max_arima_order` skipped rather than failing the whole grid.

A caller-supplied configuration is included in the grid rather than silently
discarded. The grid remains bounded and is evaluated on validation data only.

Model selection uses validation return MAE, with directional accuracy as a
tie-break. A challenger must beat the `random_walk` validation MAE by the
pre-registered relative margin fixed in `statistical/validation.py`
(`SELECTION_MARGIN_PCT = 5.0`); otherwise `random_walk` is retained. If the
baseline was not in the caller's model list, it is evaluated as the comparison
reference rather than skipped. If the baseline is missing or lacks enough
finite validation observations, selection
abstains rather than comparing against an unknown reference. It does not choose
the lowest training error, and test data is never consulted for selection. No
model is automatically promoted.

## 5. Supported forecast horizons

The existing PI-01 forecast horizons and horizon specifications are used:

| Family | Horizons | Finest accepted input | Default input |
|---|---|---|---|
| Intraday | 5m, 15m, 20m, 1h | 1m, 5m, 5m, 15m | the finest accepted interval |
| Swing | 1d, 3d, 5d | daily | daily |
| Long-term | 30d, 90d | daily | daily |

`horizon_steps` converts the horizon into model steps only when the data
resolution is permitted. Daily data cannot produce a 5m/15m/20m/1h claim, and
daily/session horizons require daily observations: intraday bars are not
silently reinterpreted as trading days. The target time is computed through the
exchange calendar when the instrument has a verified calendar; intraday
horizons walk actual sessions and daily horizons land on the correct session
close. Without a calendar, the result is explicitly a calendar fallback, not an
exchange-session claim. Insufficient history or an unsupported resolution
returns an unavailable/error result; it is not filled.

## 6. Evaluation datasets and date ranges

Real historical observations were read from the project's existing market-data
snapshot `data/novamarket.sqlite3` without modifying it. This is a raw market-data
snapshot, not a newly fabricated PI-02 dataset version; the PI-02 path is
implemented and covered by the service integration test. The snapshot version
key below is the first 16 hex characters of a SHA-256 over the ordered
`(observed_at, close)` rows. The dataset was therefore identifiable and
reproducible but its PI-02 `quality` status was not re-certified for this run;
that is an explicit limitation.

| Instrument | Input | Snapshot version | Bars | Train range | Validation range | Test range |
|---|---|---|---|---|---|---|
| `XNSE:RELIANCE` | 1d | `25dba7b299dfa6a0` | 256 | 2025-10-29 → 2026-06-12 (153) | 2026-06-15 → 2026-08-25 (51) | 2026-08-26 → 2026-11-11 (52) |
| `XBOM:ADANIENT` | 1m | `419fc793b1b0d97b` | 1,104 | 2026-11-05 05:20 → 2026-11-09 07:54 (662) | 2026-11-09 07:55 → 2026-11-11 06:03 (221) | 2026-11-11 06:04 → 2026-11-12 04:18 (221) |

The intraday split is shared by the 5m, 15m, 20m and 1h evaluations; the
horizon determines target steps (5, 3, 4 and 4 on 1m/1m/5m/15m respectively,
with the 1m source used for the ADANIENT experiment). The 30d run used the same
RELIANCE snapshot with a shorter training prefix; the 90d run was correctly
refused because 256 daily observations are insufficient for its chronological
split (the service reported a minimum of 540: 180 minimum history plus three
90-bar validation origins and one 90-bar test origin).

## 7. Actual model performance metrics

These are exploratory out-of-sample measurements on one instrument per family,
not a claim of general or future accuracy. All numbers are test-window metrics,
except the selected model/configuration which was chosen on validation.

### 7.1 `XNSE:RELIANCE`, 1-day horizon, selected validation model: `arima`

Selected ARIMA configuration `(p,d,q)=(2,1,1)`.

| Metric | Validation (38 origins) | Test (46 origins) |
|---|---:|---:|
| Price MAE | 10.30790125 | 16.94423262 |
| Price RMSE | 13.62717424 | 26.33600193 |
| Price MAPE | 0.00788878 | 0.01369983 |
| Price sMAPE | 0.00790527 | 0.01381368 |
| Return MAE | 0.00791592 | 0.01393282 |
| Return RMSE | 0.01050850 | 0.02217072 |
| Mean forecast error | 0.00098477 | 0.00073210 |
| Forecast bias | −0.00098477 | −0.00073210 |
| Directional accuracy | 0.42105263 | 0.21739130 |
| Interval coverage (80%) | 0.92105263 | 0.82608696 |
| Mean interval width | 0.03762518 | 0.03855774 |

The ARIMA model was selected on validation (return MAE 0.007916) but did **not**
beat the random-walk baseline on the held-out test window. This is reported as
the measured result, not hidden or re-selected using test data.

### 7.2 `XBOM:ADANIENT`, intraday selected test results

| Horizon | Selected model | Test origins | Price MAE | Price RMSE | Directional accuracy | Interval coverage | Mean width |
|---|---|---:|---:|---:|---:|---:|---:|
| 5m | `naive_last` | 44 | 4.43976940 | 6.33182915 | 0.97727273 | — (point baseline) | — |
| 15m | `naive_last` | 14 | 6.69641113 | 10.49791616 | 0.85714286 | — (point baseline) | — |
| 20m | `sma` | 11 | 9.03250289 | 11.36039844 | 0.81818182 | 1.00000000 | 0.02053239 |
| 1h | `sma` | 3 | 13.00083822 | 14.25701330 | 0.66666667 | 1.00000000 | 0.03519394 |

The 5m/15m high directional accuracy is dominated by `neutral` actual outcomes
under the 0.5% band; it must not be read as a tradable signal. The 1h result has
only three test origins and is statistically weak. Interval coverage from 11
and 3 samples is likewise descriptive only.

### 7.3 Swing and long-term (`XNSE:RELIANCE`)

| Horizon | Selected model | Test origins | Price MAE | Price RMSE | Directional accuracy | Interval coverage | Mean width |
|---|---|---:|---:|---:|---:|---:|---:|
| 3d | `arima` | 16 | 29.22154805 | 44.32304873 | 0.12500000 | 0.87500000 | 0.06677048 |
| 5d | `sma` | 10 | 47.45449646 | 62.29819873 | 0.00000000 | 0.90000000 | 0.15550621 |
| 30d | `naive_last` | 1 | 109.40002441 | 109.40002441 | 0.00000000 | — (point baseline) | — |
| 90d | — | — | unavailable: 256 observations; at least 540 required | — | — | — | — |

The 5d validation choice (`sma`) underperformed the random-walk baseline on the
test window; the 30d result has one test observation and is not evidence of
skill. The 90d limitation is a data-coverage limitation, not a model pass.

## 8. Baseline comparisons

Test price MAE, with the random-walk baseline shown for direct comparison:

| Instrument/horizon | Naive last | Random walk | Historical return | SMA | SES | ARIMA (selected/other) |
|---|---:|---:|---:|---:|---:|---:|
| RELIANCE 1d | 15.80000423 | 15.80000423 | 15.78933311 | 43.48067463 | 16.07567869 | 16.94423262 (selected) |
| ADANIENT 5m | 4.43976940 | 4.43976940 | 4.44214287 | 5.70566628 | 4.64253397 | 4.51411865 |
| ADANIENT 15m | 6.69641113 | 6.69641113 | 6.56177460 | 7.08000837 | 6.69435375 | 6.62897963 |
| ADANIENT 20m | 8.71362305 | 8.71362305 | 8.58600664 | 9.03250289 | 7.81415977 | 8.43960826 |
| ADANIENT 1h | 12.61661784 | 12.61661784 | 11.20101212 | 13.00083822 | 11.19589437 | 10.91945499 |
| RELIANCE 3d | 29.39999569 | 29.39999569 | 28.88368112 | 47.32440868 | 36.96253836 | 29.22154805 |
| RELIANCE 5d | 39.71999512 | 39.71999512 | 37.84690795 | 47.45449646 | 38.27998346 | 37.79734237 |
| RELIANCE 30d | 109.40002441 | 109.40002441 | 132.82531997 | 99.20002441 | 109.66328075 | 143.19750196 |

Full configurations, failures and every metric are in the generated
evaluation artifacts `tmp/pi05_real_reliance_1d.json`,
`tmp/pi05_real_adanient_intraday.json` and
`tmp/pi05_real_reliance_swing_long.json`; the post-audit 1d rerun is
`tmp/pi05_audit_reliance_1d_current.json`. No single combined accuracy number is
reported; instruments and horizons are kept separate.

The important negative result is explicit: the validation-selected model does
not always outperform the naive/random-walk baseline on test. That is exactly
what a baseline phase must expose.

## 9. Prediction-interval results

Interval methods are recorded per model: empirical residual/return intervals for
naive/random-walk/return/seasonal/moving-average/exponential models, and
normal-residual intervals for ARIMA. Point-only `naive_last` reports
`interval_supported=false` and `uncertainty=unknown`; it never presents a
zero-width interval as certainty.

Real-run coverage (80% nominal) for the selected models that support intervals:

| Instrument/horizon | Samples | Coverage | Mean width |
|---|---:|---:|---:|
| RELIANCE 1d (ARIMA) | 46 | 0.82608696 | 0.03855774 |
| ADANIENT 20m (SMA) | 11 | 1.00000000 | 0.02053239 |
| ADANIENT 1h (SMA) | 3 | 1.00000000 | 0.03519394 |
| RELIANCE 3d (ARIMA) | 16 | 0.87500000 | 0.06677048 |
| RELIANCE 5d (SMA) | 10 | 0.90000000 | 0.15550621 |

The random-walk 1d baseline covered 0.75 on its 52 test rows; the 5m baseline
covered 0.70454545 on 44 rows. Coverage above/below nominal on these small
windows is descriptive; it is not a calibration certificate. The interval is a
statistical estimate, not a guarantee that the market price will be inside it.
No probability or arbitrary confidence score is attached.

## 10. Integration evidence

- **PI-01:** every generated result is constructed as a validated
  `ForecastOutput` (instrument, exchange, reference/predicted price, return,
  target, generated/received/observed timestamps, horizon, model/version,
  dataset/feature versions, interval, validity, probability status). It is
  persisted in `prediction_forecasts`; model metadata in `prediction_models`;
  reopen from the same SQLite file was verified. Evaluation persists its
  selected model/config in the PI-05 training run, and a later `generate` with
  no explicit model/config reuses that selection. PI-05 never writes NM-06's
  shared `forecast_selection` row.
- **PI-02:** `StatisticalForecastingService` consumes `HistoricalDatasetService`
  metadata and version membership when `dataset_id` is supplied, refuses a
  dataset marked not usable for training, filters observations to the cutoff,
  and records the exact dataset/version. Direct validated bars remain a supported
  input. The real historical runs used the raw project market-data snapshot
  described in §6; they did not re-certify a PI-02 dataset version.
- **PI-03:** quality/freshness provenance is preserved from observations into the
  forecast contract; no gateway or provider is duplicated.
- **PI-04:** the service consumes `FeatureService.engine.generate` for the
  feature-set version and regime label, preserves the instrument currency when
  adapting bars, and records the feature-set version in forecast/model
  metadata. The dedicated PI-04-adapter integration test exercises both the
  generated feature-set version and regime lookup, including the selection
  isolation and selected-config follow-up path. The real historical evaluation
  runs did not attach a live FeatureService, so their regime labels are honestly
  reported as `unclassified` rather than claimed as regime evidence.
- **Actual application:** `NovaMarketService.statistical_service()` composes the
  existing PI-01/NM-06 stores. The real FastAPI application test created an
  HTTP campaign through `POST /market/forecasts` (`engine=statistical`), listed
  it through `GET /market/forecasts?engine=statistical`, read it through the
  existing `GET /market/forecasts/{id}`, inspected models/suitability and
  calibration, and saw `status.statistical` with NM-08/NM-10 declared.
- **Authorities:** no order, fill, position, ledger entry, risk decision or
  execution path is created. The status explicitly reports
  `risk_authority: NM-08` and `execution_authority: NM-10`.

## 11. Tests and actual results

Focused PI-05 suite: `tests/test_novamarket_statistical.py` — **33 passed, 14
subtests passed** after the audit. It covers all model families, ARIMA
fit/forecast/diagnostics, stationarity, insufficient history, all horizon
conversions/refusals, calendar target, price/return/direction, intervals, metrics,
chronological selection, leakage, regimes, persistence/reopen, dataset input,
future-bar filtering, failure isolation, unavailable model metadata, invalid
config/cutoff HTTP 422s, seasonal interval scaling, bounded ARIMA candidates,
stale/invalid quality provenance, and real FastAPI integration including
PI-05/NM-06 selection isolation.

Affected regression (forecasting + prediction + features + datasets + gateway +
NovaMarket core + NovaMarket API):

```text
308 passed, 319 subtests passed in 175.48s
```

The focused suite and affected regression were run separately (33 + 308 = 341
tests). Both are green at the current tree: the affected regression was run
after the final source change, and the focused suite was rerun after adding its
final quality-provenance test.

Hygiene/contracts:

- `python -m mypy src --platform win32`: **Success: no issues found in 605 source files**
- `python -m mypy src`: **Success: no issues found in 605 source files**
- architecture validators: both returned `()`
- `scripts/generate_api_reference.py --check`: `docs/API.md: in sync`
- route count remains **223**; no duplicate route was added
- current-code real audit: `tmp/pi05_audit_reliance_1d_current.json` reran the
  RELIANCE daily comparison against the current implementation. It selected the
  same ARIMA `(2,1,1)` configuration, with 51 validation/52 test split bars and
  38/46 evaluable ARIMA origins, and reproduced test return MAE `0.01393282`
  and interval coverage `0.82608696`.

The whole-repository suite was not run in this phase; the affected NovaMarket
suite above was the regression boundary. No failure was hidden or converted to a
skip. The intraday and swing/long real artifacts were generated before this
audit and were not rerun; the current-code rerun covers the 1d path whose
model-selection behavior was affected most directly.

## 12. Defects fixed during verification

1. ARIMA treated every configured `d` as automatic when `d=0`; replaced with an
   explicit `auto_differencing` option and made configured differencing
   authoritative.
2. The initial chronological split left only one validation origin for an
   80-bar minimum history; the split now reserves the required validation
   origins and test window without using future data.
3. PI-04 expects `OhlcvBar` with an interval, while PI-05's read model is a
   `PriceBar`; added a point-in-time adapter (fixes `AttributeError: PriceBar has
   no attribute interval`).
4. PI-04's feature-set version field is `version`, not
   `feature_set_version`; corrected the integration boundary.
5. `_target_at` passed a calendar timezone string to `astimezone`; it now uses
   the exchange timezone explicitly and validates the close timestamp.
6. Invalid model/horizon inputs could bypass failure counters or return 500;
   they now produce typed PI-05 validation errors, HTTP 422 at the API boundary,
   and an explicit `unavailable` model record where a fit failed.
7. Dataset inspection accepted any version; a version explicitly marked not
   usable for training is now refused rather than silently used.
8. Observation quality/freshness was lost when converting bars; it is now
   carried into the forecast contract (stale data remains marked stale).
9. Point baselines could have looked like zero-width intervals; `naive_last`
   explicitly reports no interval support.
10. Root forecast reads needed to distinguish PI-05 `sft_` records from NM-06
    records; the existing routes now dispatch without a second endpoint.
11. The API accepted `config`/`models`/split ratios but did not forward them;
    they are now validated (`ModelConfig.from_dict`) and forwarded to the
    existing PI-05 service, including the JSON `arima_order` list form.
12. A forecast id omitted dataset/version/payload identity, so two different
    data vintages at the same timestamp could overwrite one row; the id now
    includes those identities.
13. The generate response reported `persisted=True` before checking the store;
    it now reports the actual insert/existing-row outcome and an unchanged
    observation window reuses a bounded fit cache rather than refitting.
14. `ModelConfig` validated `max_arima_order` before checking p+q and converted
    numeric interval/neutral-band fields before comparing them; malformed API
    config values and malformed `as_of` timestamps now return 422 instead of a
    server error.
15. `forecast_model` accepts the same JSON configuration form as the API, and
    `naive_last` is usable with a minimal two-close history without weakening a
    smoothing model's history guard.
16. The candidate grid now includes the caller's SES/Holt/ARIMA configuration
    and filters ARIMA orders against `max_arima_order`; a narrow bound no longer
    aborts the complete comparison.
17. `seasonal_naive` interval records/uses horizon scaling (`sqrt(steps)`) rather
    than incorrectly treating a multi-step target as one step.
18. Market-history reads pass the request cutoff to the source before taking the
    newest bounded window, and paged/list bar containers are adapted uniformly;
    dataset metadata without a usable version fails explicitly.
19. PI-04 adaptation preserves the instrument currency, evaluation records the
    actual feature-set version, model metadata retains prior validation/test
    evidence across ordinary generation, and uncertainty detail reports the
    residual sample count rather than the total observation count.
20. PI-05 no longer overwrites NM-06's shared `forecast_selection` row. Evaluation
    persists model/config evidence in the PI-05 training run, `generate` reuses
    it, and statistical performance reads filter out non-PI-05 snapshots.
21. Direction-band messages now use the normalized numeric band, and reference
    prices are validated before return projection.
22. Observations whose normalized quality is invalid/unavailable, or whose
    freshness is missing/provider-unavailable, are excluded before fitting;
    stale observations remain usable and are marked stale in the result.

## 13. Remaining limitations

- **No calibrated probability model.** Probabilities/confidence are withheld;
  `p_*` are `None`, status is `unavailable`, calibration is `not_applicable`.
  Direction is a deterministic neutral-band/interval rule, not a probability.
- **ARIMA is bounded, not full statsmodels.** No SARIMA, exact ML covariance,
  parameter-uncertainty propagation or seasonal ARIMA. The implementation does
  include stationarity/differencing, AR/MA least squares, stability rejection,
  residual diagnostics and normal-residual intervals. It fails gracefully on
  insufficient/unstable fits.
- **Real-history scope is narrow.** The evidence covers RELIANCE daily and
  ADANIENT 1-minute data from the local snapshot. 90d is unavailable due to
  insufficient observations; 30d has one test origin; the 1h run has three.
  Intraday directional accuracy is dominated by neutral classifications.
- **Regime evidence is limited.** The dedicated PI-04-adapter integration test
  proves the feature-set/regime boundary, but the real historical runs did not
  attach a live FeatureService, so their regime labels were `unclassified`. The
  separate real regime smoke check did use PI-04 and produced labels
  (`strong_down`, `weak_down`, `weak_up`, `strong_up`, `uncertain`), but those
  counts are small/uneven and are not treated as model-skill evidence. Real
  regime sample counts need a larger validated dataset before model-by-regime
  claims.
- **No interval guarantee.** Empirical/normal-residual intervals exclude
  parameter, data-quality, event and regime uncertainty; coverage on small
  windows is descriptive.
- **No automatic promotion, execution or risk authority.** Selection is stored
  as evidence in the PI-05 training run; NM-08 and NM-10 remain sole authorities.
  A caller can explicitly override the selected model/config.
- **Real evidence is not a PI-02-certified dataset.** The RELIANCE daily run was
  rerun against current code, but the intraday and swing/long artifacts predate
  this audit and were not rerun. The snapshot is raw and stale-labelled; the
  results are exploratory, not a production accuracy claim.
- **No full-repository regression.** The affected NovaMarket suites and both
  mypy views pass; unrelated suites were not executed.

## 14. Final acceptance verdict

**PASS for PI-05 scope.**

All required model families are implemented and exercised; multi-horizon
compatibility, PI-01 contract construction/persistence, chronological
walk-forward selection, test isolation, regime-aware evaluation, interval
reporting, model metadata/versioning, failure isolation, existing application
integration and the existing forecast routes are verified. Selection is isolated
from NM-06's shared slot, and a statistical follow-up reuses the selected
model/config. The focused and affected tests pass, both mypy views are clean,
architecture validation and API reference checks pass, the current-code RELIANCE
daily rerun reproduced the reported selected configuration and metrics, and no
known PI-05 blocking defect remains.

The PASS is qualified by the limitations above: no calibrated probability, a
bounded non-seasonal ARIMA, narrow real-history evidence (90d unavailable),
small-sample regime/interval results, and intraday/swing/long real artifacts not
rerun after the audit. These are reported as limitations, not silently converted
into accuracy claims. **PI-06 must not begin on this report without the user's
explicit phase authorization.**
