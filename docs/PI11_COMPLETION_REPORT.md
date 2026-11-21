# PI-11 — Prediction Evaluation and Backtesting Completion Report

**Phase:** PI-11  
**Branch:** `feature/novamarket`  
**Date:** 2026-11-16  
**Status:** **PASS (scoped)** — implementation, integration and deterministic
verification are complete. No evaluated historical outcomes exist in the local
application data, so this report makes **no historical accuracy or profitability
claim**.

## 1. Initial repository assessment

- The checked-out branch is `feature/novamarket`. The worktree already contained
  the PI-01–PI-10 user changes, including untracked new packages and reports.
  Nothing was staged, committed, pushed, tagged or merged while completing PI-11.
- PI-01 through PI-10 completion reports were read. They record PASS or scoped
  PASS, and PI-10's only stated limitation is the unpublished 2027 NSE holiday
  calendar. No prerequisite dependency failure was found.
- Existing prediction, dataset, feature, statistical, gradient-boosting, LSTM,
  ensemble, lifecycle, outlook, strategy, risk, paper-execution, portfolio,
  accounting and NM-11 backtesting code was inspected before adding PI-11.
- The local SQLite stores were inspected to determine whether real evaluation
  evidence was available:
  - `data/novamarket.sqlite3` contains six original NM-06 forecast records and
    33,561 market bars, but **zero evaluated forecast outcomes** and no PI-09
    operational forecast outcomes.
  - The six NM-06 targets are later than the latest available bars for their
    instrument, and the forecasts have no stored outcome. PI-11 therefore does
    not invent an accuracy result for them.
  - No `prediction_forecasts` or `prediction_forecast_outcomes` rows exist in
    the currently inspected application data.
- Existing test-only databases and runtime fixtures were not treated as market
  evidence.

## 2. Existing functionality reused

| Existing capability | Reused for | Change |
|---|---|---|
| PI-01 `ForecastOutput`, prediction store, lifecycle contracts | Immutable forecast identity, target timestamps, probability and calibration provenance | Read-only |
| PI-02 datasets and market bars | Target observation resolution where a stored outcome is absent; existing corporate-action adjusted data | Read-only |
| PI-03 market-data gateway | Bounded target-bar lookup for original NM-06 forecasts | Read-only |
| PI-04 feature/regime engine | Stored regime labels on evaluation records | Read-only |
| PI-05 statistical models | Stored out-of-sample scorecards in `forecast_performance` | Read-only |
| PI-06 XGBoost/LightGBM | Stored test scorecards and model versions | Read-only |
| PI-07 LSTM | Stored test scorecards and model versions | Read-only |
| PI-08 ensemble calibration | Calibrated lifecycle probabilities, interval widths, uncertainty | Read-only |
| PI-09 lifecycle scheduler and outcomes | T−20/T−5/T−1 checkpoint evidence and original operational forecasts | Read-only |
| PI-10 swing/long horizons | Horizon contracts and stored context metadata | Read-only |
| NM-08 risk engine | Backtest risk-policy snapshot and decisions | Existing interface |
| NM-10 paper execution | Fees, spread, slippage, liquidity and order behaviour | Existing interface |
| NM-11 backtesting | Isolated replay store, strategy catalog, execution engine, metrics, benchmark, reproducibility | Extended read-only forecast loader only |
| Portfolio/accounting services | Existing simulation valuation semantics | No mutation |

No second forecasting engine, risk engine, execution engine, portfolio service,
ledger or accounting authority was created. PI-11 has no order, fill, ledger or
portfolio handle.

## 3. Forecast evaluation implementation

New package `src/novacontrol/novamarket/evaluation/`:

- `models.py` defines `EvaluationConfig`, `EvaluationRecord`, record origins,
  deterministic configuration hashing, report hashing and leakage-timestamp
  validation.
- `metrics.py` implements deterministic price, return, direction, probability,
  interval, stability, lifecycle, regime, horizon, model and walk-forward
  metrics.
- `store.py` implements an append-only SQLite report store in
  `novamarket_evaluation.sqlite3` with immutability triggers.
- `service.py` reads existing stores, resolves missing NM-06 target
  observations, persists reports, verifies hashes and reproduces results.

Supported sources:

- `lifecycle`: PI-09 `ForecastOutcome` records joined to their immutable
  `ForecastOutput` contracts. These are original operational forecasts and are
  evaluated verbatim.
- `nm06`: original NM-06 forecast records with stored evaluated outcomes. If an
  original forecast has no stored outcome, PI-11 may use the first completed bar
  at or after its recorded target, within the evaluation `as_of` cutoff. It
  records the actual observation timestamp, delay, and resolution method. The
  forecast is never regenerated.
- `records`: explicitly supplied research or test evidence. Every record must
  declare `historical_operational`, `research_reconstruction` or
  `synthetic_fixture`. Reconstruction requires model-training and information
  cutoffs that do not follow generation.

PI-05/06/07/08 training scorecards are read from the existing
`forecast_performance` and ensemble-evaluation tables into a separate
`model_evidence` block. They are aggregate out-of-sample evidence, not
per-target forecast records, and are not mixed into price metrics.

## 4. Forecast accuracy results

**Actual historical results are unavailable in the inspected local data.** There
are no evaluated PI-09 outcomes and no evaluated NM-06 outcomes. The six stored
NM-06 forecasts have targets beyond the available bar history. The system
returns an honest `insufficient_data` result rather than a zero-error result.

Deterministic fixture verification (not market evidence):

| Check | Expected | Observed |
|---|---:|---:|
| Price MAE | 10.00 | 10.00 |
| Price RMSE | 10.00 | 10.00 |
| Price MAPE | 0.10000000 | 0.10000000 |
| Price sMAPE | 0.10025063 | 0.10025063 |
| Price bias | 0.00 | 0.00 |
| Empty-evidence status | insufficient_data | insufficient_data |

The empty-evidence test asserts that no accuracy claim is created when no
records match. A resolved bar after the evaluation cutoff is rejected rather
than silently used.

## 5. Direction and return evaluation

Direction evaluation reports accuracy, per-class precision/recall/F1, confusion
matrix, bullish accuracy, bearish accuracy, neutral accuracy, false-bullish and
false-bearish rates, uncertain count/rate, abstained count/rate, and
non-directional rate. Neutral outcomes use the configured threshold.

The two explicit policies are:

- `exclude_uncertain`: uncertain and abstained predictions are excluded from
  the accuracy denominator.
- `count_uncertain_incorrect`: both classes are counted as incorrect and remain
  in the denominator.

They are reported separately; they are never silently treated as correct or
incorrect. Under `neutral_policy=exclude`, neutral actuals are removed from both
numerator and denominator even when uncertain predictions are counted as
incorrect. Stored uncertainty labels and ensemble disagreement are summarized
separately without turning disagreement into a probability or residual variance. A fixture with two resolved records and one uncertain prediction
produces accuracy 1.0 under the exclusion policy; the same fixture produces
2/3 under the counting policy. A standalone abstention has no resolved accuracy
and abstention rate 1.0. With neutral actuals excluded, one correct call over
two non-neutral actuals reports 0.5, not 1/3.

Return evaluation reports expected versus actual return error, MAE, RMSE, mean
error, bias, over-estimation rate, under-estimation rate, downside/upside mean
error and error distribution. Price errors and portfolio returns are never
combined.

## 6. Calibration and uncertainty results

Probability metrics are calculated only for records explicitly marked
`calibrated`. Arbitrary uncalibrated NM-06 scores are withheld. The service no
longer labels a stored NM-06 probability as calibrated merely because values
exist.

Deterministic fixture verification:

| Check | Expected | Observed |
|---|---:|---:|
| Multi-class Brier (up 0.7, down 0.2, neutral 0.1; actual up) | 0.14 | 0.14 |
| Log loss | positive | positive |
| ECE | non-negative | non-negative |
| Uncalibrated record | unavailable | unavailable |
| 80% interval coverage, 1 of 2 inside | 0.5 | 0.5 |
| Coverage gap | -0.3 | -0.3 |

Interval evaluation reports nominal coverage, actual coverage, gap, mean and
median width, misses below/above, and a sample-size sufficiency flag. Nominal
confidence is never assumed to equal actual coverage. Calibration by regime and
horizon is available from the stored bins and slices.

## 7. Forecast lifecycle evaluation

Lifecycle evaluation groups T−20, T−5 and T−1 by stage and pairs them by cycle.
It reports, for each pair:

- sample count and paired observations;
- left/right MAE and mean absolute-error change;
- right-better/right-worse/tied counts;
- direction accuracy on each checkpoint;
- direction-change count and rate;
- confirmation-unavailable count;
- a caveat that later confirmation is not assumed to be better.

A deterministic two-cycle fixture produced one improvement and one worsening
between initial and final confirmation, with two paired cycles. Data freshness
is also grouped and reported against price, direction and interval metrics. The
service reports lifecycle execution/regeneration provenance through the stored
record timestamps, and each PI-09 checkpoint retains its own generated time.

## 8. Model comparison

Per-record model comparison groups by identical horizon, model/ensemble and
version. Ranking uses MAE then RMSE, within the same horizon only. A baseline is
selected only from models with a baseline-like identifier (`naive`, `baseline`,
`statistical`, `sma`, `arima`) and sufficient samples.

Deterministic fixture: a naive model with MAE 5 ranked ahead of an LSTM with MAE
15. The report exposes `beats_baseline` and `automatic_promotion=false`.
Complexity is not a ranking input.

Stored PI-05/06/07/08 scorecards are shown separately and ranked only within a
matching horizon. They are aggregate training/evaluation evidence and may cover
different instruments or windows; the report says so explicitly.

## 9. Market-regime analysis

Regime slices use the stored PI-04 label on each record. Each slice reports
sample size, `sufficient`, `reliable`, MAE, RMSE, direction accuracy, return
bias, interval coverage and abstention rate. A slice below the configured
minimum reports `insufficient` and the caveat “no regime conclusion is drawn.”

The one-record high-volatility fixture correctly fails the sufficiency gate. No
regime weight, model promotion or trading policy is derived from these results.

## 10. Historical trading simulation implementation

PI-11 does not add a replay engine. It reuses the NM-11 engine through the
existing `forecast_assisted` strategy:

- `NovaMarketService._backtest_forecasts` reads NM-06 rows and PI-09 contracts
  at the replay decision time.
- PI-09 contracts are adapted to the NM-06 input shape:
  `5m/15m/20m/1h/1d → next_session`, `3d → few_sessions`, `5d → one_week`,
  `30d → one_month`, `90d → three_months`.
- The adapter preserves generation time, data cutoff, target, predicted return,
  probability, interval, uncertainty, model/ensemble version and validity.
- A contract generated after the decision timestamp is excluded.
- A contract whose data cutoff follows the decision timestamp is excluded by the
  existing NM-11 loader.
- Failed, suspended and degraded forecasts are excluded by the existing loader.
- The replay invokes the existing NM-08 policy and NM-10 pricing/fee engine.
- Signals, orders, fills, positions, cash and equity are recorded in the
  isolated NM-11 store, never in live or paper books.

A focused adapter test confirms a contract generated at the decision time is
available, a future contract is excluded, the `5d → one_week` mapping is
preserved, validated state becomes `active`, and source provenance remains
`PI-09`.

## 11. Execution and cost assumptions

The replay reuses NM-10's `ExecutionConfig`. Its current illustrative defaults
are:

| Assumption | Default |
|---|---:|
| Brokerage | 3 bps |
| Exchange charge | 0.297 bps |
| SEBI charge | 0.01 bps |
| Stamp duty | 0.015 bps |
| GST | 18% |
| Synthetic spread | 8 bps |
| Market-impact coefficient | 12 bps |
| Maximum slippage | 150 bps |
| Participation rate | 10% of reference volume |

The PI-11 UI adds explicit brokerage, spread and impact fields to the replay
form and sends them through the existing `execution_config` API. Fee schedules
are described as illustrative simulation assumptions, not broker quotes. Daily
OHLCV data cannot establish a real bid/ask, queue position, intraday impact or
partial-fill sequence. The existing engine does not fill a signal at the signal
close and already has tests for next-bar fills, limit/stop conditions, partial
fills, expiry, liquidity and insufficient cash.

## 12. Portfolio simulation results

**No actual portfolio simulation result is reported.** The inspected stores do
not contain a complete forecast-outcome series or a completed real-data
forecast replay. Fabricating one would violate the phase rules.

The reused NM-11 engine tracks starting virtual capital (default ₹1,00,000),
cash, open/closed positions, portfolio value, realized/unrealized P&L, costs,
exposure, concentration and the equity curve. Its existing tests verify cash and
position reconciliation, multi-instrument valuation, fees, realized P&L and
immutability. No live paper portfolio, actual user capital or production ledger
is touched.

## 13. Risk-adjusted performance results

NM-11 already computes total return, annualized return where observations allow,
daily/cumulative returns, net returns, maximum drawdown, drawdown duration,
volatility, downside deviation, Sharpe, Sortino, Calmar, trade count, win rate,
average win/loss, profit factor, holding period, fees and slippage. Tests verify
drawdown/return calculations and that insufficient observations are reported
honestly rather than annualized.

No numeric risk-adjusted result is claimed for PI-11 because no suitable
real-data replay exists in the local stores.

## 14. Benchmark comparisons

The existing NM-11 result includes a buy-and-hold benchmark when a benchmark
instrument is supplied. Benchmark and strategy use the same replay window and
adjusted historical bars from the existing dataset layer. Dividend/split
handling depends on the verified adjusted series; it is not reimplemented by
PI-11.

The PI-11 replay form accepts an optional canonical benchmark and reports the
existing benchmark block. No benchmark-relative return is claimed without a
completed replay.

## 15. Leakage-prevention evidence

Implemented safeguards:

- A target must follow forecast generation.
- Actual observations may not precede the target.
- Reference data may not follow generation (5-minute generation tolerance).
- Model-training and information cutoffs may not follow generation.
- A reconstructed record must declare both cutoffs.
- Records generated after the evaluation `as_of` are excluded.
- Target bars after the evaluation `as_of` are never used.
- Forecast replay excludes contracts generated after the decision.
- The existing replay loader excludes rows whose data cutoff follows the
  decision.
- Walk-forward folds use only preceding records, purge preceding actual
  observations that had not ended before the test window began, and use a fixed
  configuration. The purge uses `actual_observed_at`, not forecast generation
  time; a delayed target observation therefore cannot enter the training side
  of the next fold.
- No test-set tuning, automatic promotion or model-weight change occurs.
- A leakage audit reports rejected records, look-ahead violations, delayed
  observations and missing versions.

Evidence: the real-store test reads an actual PI-09 outcome/forecast contract
without regeneration; the target-resolution test rejects a bar after the cutoff;
the adapter test rejects a future forecast; the lifecycle test pairs identical
cycle targets; the reconstruction test fails without both cutoffs.

## 16. Reproducibility evidence

Every evaluation persists:

- evaluation ID and name;
- full configuration and SHA-256 configuration hash;
- source scope and source counts;
- record origins and data digest;
- all metrics, slices and leakage audit;
- report hash;
- creation timestamp;
- record sample and, for explicit records, the input records needed to
  reproduce.

The store rejects updates and deletes via triggers. `verify` recomputes the
report hash. `reproduce` reruns the same configuration and compares the data
digest and report hash. The explicit-record test verifies persistence,
immutability and successful reproduction. Reproduction is a read-only
verification: it does not append a second report or change the persisted count.
The original explicit payload is retained (including filtered or rejected
rows), so a reproduction replays the same audit. A missing match produces
`insufficient_data`, not a synthetic zero-error report. The status route reads
persisted counts with a short-lived store, so a restarted process does not
report zero before the first list request and a disabled market does not keep
the report SQLite file open.

Existing NM-11 replay configuration hashing, seed, immutable completed runs and
`verify` remain unchanged and are covered by its 48 tests.

## 17. API and frontend integration

Seven authenticated routes were added:

- `GET /market/evaluations/status`
- `GET /market/evaluations/contracts`
- `GET /market/evaluations`
- `POST /market/evaluations`
- `GET /market/evaluations/{evaluation_id}`
- `GET /market/evaluations/{evaluation_id}/verify`
- `POST /market/evaluations/{evaluation_id}/reproduce`

They are registered in `ApiSurface`, `ROUTE_CONSUMERS`, the API route contract
tests and generated `docs/API.md`.

Current route surface:

| Measure | Count |
|---|---:|
| Total API routes | 651 |
| `/market` route decorators | 279 |
| Market-route contract entries | 244 |

Frontend:

- `web/src/pages/PredictionEvaluation.tsx` is wired into `App.tsx` and `nav.ts`
  as **Prediction Evaluation** under Intelligence.
- It supports a source/date/instrument/horizon/threshold/confidence/sample
  configuration, persisted evaluation selection, price/return/direction/
  calibration/interval/lifecycle/regime/stored-scorecard views, and a separate
  forecast-assisted replay with capital, benchmark, brokerage, spread and impact.
- The replay form uses the selected evaluation's persisted instruments and first
  horizon instead of silently replaying an empty universe, requires an NM-08
  policy, passes an accepted `evaluation_id` provenance field, and reads the
  equity curve from the backtest detail fallback when the results endpoint has
  no curve array. It also displays the replay's risk-adjusted metrics and the
  available benchmark block; unavailable benchmark data is labelled as such.
- The page clearly separates forecast accuracy from simulated trading.
- `npm run build` passed after the final UI integration.

The UI was not visually inspected in a browser. The build verifies TypeScript
and bundling, not rendered appearance or interaction.

## 18. Tests and actual results

Focused PI-11 tests:

| Test group | Result |
|---|---:|
| `tests/test_novamarket_evaluation.py` | **22 passed** |
| `PredictionEvaluationApiTests` | **4 passed** |
| Route contract + `/market` decorator pin + strategy/backtesting integration | **118 passed, 248 subtests passed** |

Affected regression:

```text
tests/test_novamarket_evaluation.py
tests/test_novamarket_api.py
tests/test_novamarket.py
tests/test_novamarket_scheduler.py
tests/test_novamarket_forecasting.py
tests/test_novamarket_statistical.py
tests/test_novamarket_gradient.py
tests/test_novamarket_lstm.py
tests/test_novamarket_ensemble.py
tests/test_novamarket_prediction.py
tests/test_novamarket_features.py
tests/test_novamarket_strategies.py
tests/test_novamarket_backtesting.py
tests/test_novamarket_outlook.py
```

Earlier affected regression (before the final audit fixes): **529 passed, 358
subtests passed** in 158.69 seconds. Final audit revalidation of the full API
and NovaMarket service files: **143 passed, 305 subtests passed** in 90.56
seconds. The focused PI-11 evaluation/API/route/strategy/backtesting run passed
**118 tests and 248 subtests**. The evaluation suite itself passed **22 tests**.
Only existing FastAPI/Starlette deprecation warnings were emitted.

Other checks:

- `mypy` on the evaluation package, `NovaMarketService` and API modules:
  **success, no issues found**.
- `python scripts/generate_api_reference.py --check`: **docs/API.md in sync**.
- `npm run build`: **pass**; existing dynamic-import and chunk-size warnings
  remain unrelated to PI-11.
- Actual local-data inspection (read-only URI connection): six NM-06 forecasts,
  zero forecast outcomes, zero PI-09 operational forecasts/outcomes. The six
  targets are 2026-11-09; the latest `XNSE:ACCEPT` bar is 2026-11-04. Therefore no
  historical accuracy, calibration, model ranking, trading performance or
  benchmark result is claimed.

Deterministic tests explicitly use synthetic fixtures and a real empty SQLite
schema; neither is labelled as historical evidence.

## 19. Defects found and fixed

1. **Lifecycle source could miss outcomes without a cycle index.** The original
   instrument-filtered query depended on a cycle row. The service now loads the
   bounded outcome set once and filters against the immutable forecast, so an
   outcome is not hidden merely because its cycle index is unavailable.
2. **NM-06 probabilities were incorrectly treated as calibrated.** Stored NM-06
   probabilities are raw estimator outputs. The service now preserves their
   actual status and probability metrics withhold them unless explicitly
   calibrated.
3. **Uncertain and abstained predictions were conflated.** Direction metrics now
   count them separately, report both rates, and apply the documented policy to
   both non-directional classes.
4. **Missing NM-06 outcomes could not be evaluated from historical bars.**
   Added the bounded first-observed-bar-at-or-after-target resolver, with
   timestamp/delay preservation and a hard cutoff rejection.
5. **A missing outcome could be mistaken for zero error.** Empty evidence is
   persisted as `insufficient_data` with an explicit limitation.
6. **Future target observations could leak into a resolved evaluation.** A bar
   after `as_of` is rejected; the cutoff test verifies this.
7. **Model scorecards were at risk of being mixed into per-target metrics.**
   They now live in a separate `model_evidence` block.
8. **The evaluation page was not reachable from navigation.** Added the nav item
   and App route.
9. **Mypy issues in the new metrics/service code.** Fixed optional narrowing,
   annotation shadowing, and redundant casts without weakening checks.
10. **Neutral exclusion did not apply to the counting policy denominator.** With
    `neutral_policy=exclude` and `direction_policy=count_uncertain_incorrect`,
    neutral actuals stayed in the denominator through `len(rows)`. Both policies
    now use the same eligible non-neutral population.
11. **Walk-forward purge used the wrong timestamp.** It compared `target_at` with
    the test forecast's generation time, so a target observed during the test
    window could remain in the training side. It now compares the actual
    observation time with the first test target and is pinned by a delayed-bar
    fixture.
12. **Reproduction mutated the append-only store.** `reproduce` called the normal
    evaluate path and inserted a duplicate report. It now evaluates with
    `persist=False`; the API test and service test assert the count is unchanged.
13. **Explicit records could inherit an operational origin.** Missing provenance
    defaulted to `historical_operational`, contradicting the report and allowing
    caller evidence to be mislabelled. Explicit rows are now rejected without a
    declared origin, and the low-level defaults are `synthetic_fixture` rather
    than operational.
14. **The UI replay payload could never validate.** It sent an `evaluation_id`
    strategy parameter that the catalog did not declare, so the API rejected the
    configuration as unknown. The field is now declared as inert provenance and
    the exact UI payload is exercised by a test.
15. **The UI replay could target an empty universe or the wrong horizon.** An
    empty persisted instrument list was truthy in JavaScript, bypassing the
    instrument fallback; the replay also read the live form horizon instead of
    the selected evaluation's horizon. Both now derive from the stored report
    with an explicit fallback.
16. **The replay equity curve was never queried.** The page read only
    `/results`, which has no `equity` array, and the detail endpoint that carries
    the curve was fetched but unused. The page now falls back to the backtest
    detail, and its Refresh action refreshes the selected replay endpoints too.
17. **The status route reported persisted counts only after a list request.** It
    now reads the report store through a short-lived connection and closes it, so
    a restarted process reports real counts without leaking the SQLite handle on
    a disabled market.
18. **Empty `sources` silently became both defaults.** `from_mapping` replaced an
    explicitly empty source list with the default pair before validation. Only a
    missing/`None` source list now defaults; `[]` is rejected with HTTP 422.
19. **String source filters did not attach the right store.** A comma-separated
    `sources: "lifecycle"` was iterated character by character when deciding
    which lazy store to attach. The request now normalizes strings before the
    attachment decision.

No unresolved phase-blocking defect was found after these fixes.

## 20. Remaining limitations

- **No evaluated historical evidence in the local application data.** The six
  original NM-06 forecasts have no evaluated outcomes and their targets are later
  than the available bars. No actual accuracy or trading result is presented.
- **No operational PI-09 history in the inspected data.** Lifecycle evaluation is
  verified with a real PI-09 table/schema test but not with historical cycles.
- **Aggregate model scorecards are not per-target records.** PI-05/06/07/08
  training scorecards may cover different instruments or windows and are
  reported separately.
- **Daily bars cannot prove intraday execution.** Spread, impact, slippage and
  fills are simulation assumptions, not exchange-grade evidence.
- **Universe membership and survivorship correction remain dependent on the
  source dataset.** PI-11 does not reconstruct historical index membership.
- **Regime conclusions require adequate samples.** The system refuses to draw a
  conclusion below the configured threshold.
- **PI-09 automatic scheduling remains 20-minute only.** Five PI-10 horizons are
  available through the existing per-instrument cycle API; PI-11 did not change
  the auto-planner.
- **2027 NSE holidays remain unpublished.** A 90d target crossing into 2027 is
  still refused rather than approximated.
- **UI not visually inspected.** TypeScript/build passed; rendered layout,
  navigation and interaction were not reviewed in a browser. The replay payload,
  instrument/horizon derivation and curve fallback are code/build verified, not
  visually verified.
- **No Git operations were performed.** Changes remain uncommitted and unstaged.

## 21. Final acceptance verdict

**PASS (scoped)** for the PI-11 implementation, integration, reproducibility,
leakage safeguards, API and frontend wiring. The final audit found and fixed
denominator, walk-forward purge, provenance, reproduction, replay-payload,
equity-curve and status-count defects; the evaluation and replay layers remain
separated, use existing authorities, and refuse to fabricate evidence.

The empirical portion is intentionally limited: the local data has no evaluated
historic outcomes, so this report does not claim accuracy, calibration,
risk-adjusted returns or benchmark outperformance. The deterministic tests
verify the formulas and failure behaviour, and the actual empty-store behaviour
is reported honestly. Renderer appearance and browser interaction remain
unverified. PI-12 was not started.
