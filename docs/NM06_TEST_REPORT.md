# NM-06 — Test and Verification Report

Phase: **NM-06 — Measured Probabilistic Forecasting**
Environment: Windows, project `.venv` (Python 3.13), `PYTHONPATH=src`, run from
`novamarket/`.
Date of record: 2026-11-02, including the post-implementation audit pass.

Every result below is from a command executed in this working tree. No evidence
comes from live market data or a paid provider: all fixtures are generated
deterministically in the tests.

## Commands and results

| # | Command | Result |
| --- | --- | --- |
| 1 | `python -m pytest tests/test_novamarket_forecasting.py -q` | **68 passed, 35 subtests passed** (0.7 s) |
| 2 | `python -m pytest tests/test_novamarket.py tests/test_novamarket_api.py -q` | **107 passed, 104 subtests passed** (43.5 s) |
| 3 | `python -m pytest tests/ -q -k "novamarket" -rs` | **431 passed, 3 skipped, 188 subtests passed** (70.6 s) |
| 4 | `python -m mypy src` | **Success: no issues found in 507 source files** |
| 5 | `python -m mypy src --platform win32` | **Success: no issues found in 507 source files** |
| 6 | `python scripts/generate_api_reference.py --check` | **in sync** (`docs/API.md`, 433 routes) |

Skips (all pre-existing and environmental, none NM-06):

```
SKIPPED test_database.py:29                 optional `database` extra (sqlalchemy not installed)
SKIPPED test_novamarket_marketdata.py:1282  live provider verification requires NOVAMARKET_LIVE_TESTS=1
SKIPPED test_novamarket_marketdata.py:1303  live provider verification requires NOVAMARKET_LIVE_TESTS=1
```

## What each NM-06 test class pins

| Class (in `tests/test_novamarket_forecasting.py`) | Pinned behaviour |
| --- | --- |
| `HorizonTests` | the five horizons are the documented session counts; policy rejects incoherent thresholds |
| `RecordContractTests` | probabilities must sum to 1; point inside interval; target after creation; cutoff not after creation; the "not advice" flags |
| `ModelLifecycleTests` | illegal transitions raise (`RETIRED→ACTIVE`, `EXPERIMENTAL→ACTIVE`); legal paths return the pair |
| `FeatureTests` | reproducibility and digest equality; a bar after the cutoff cannot change features; insufficient history returns `None`; all 19 features finite; regime is a defined value |
| `EstimatorTests` | every model deterministic; distributional probabilities sum to 1; interval contains the point forecast; `zero_return` carries no distribution; **`historical_mean` predicts the mean**; **`drift` predicts the trailing drift**; insufficient history and unknown model raise; bootstrap changes with its input |
| `MetricTests` | MAE/RMSE/median error against hand-computed values; perfect-forecast Brier and log loss are 0; directional accuracy and confusion matrix; coverage and mean width; pinball asymmetry; explicit ±0.5% edges |
| `CalibrationTests` | insufficient sample is reported, not calibrated; the holdout is scored out of sample; the fitted map is monotone |
| `ValidationTests` | origins chronological and non-overlapping; insufficient history yields no scores; every model scored on the same origins; baseline comparison; `zero_return` has no probabilities |
| `StoreTests` | forecast round-trip; `UPDATE` and `DELETE` raise in SQLite; an outcome is recorded once; `pending_due` respects the target; **an illegal state move is refused and leaves the row unchanged**; selection and model state are per instrument × horizon with history |
| `EngineTests` | generate stores and publishes; target is a future weekday; insufficient history, stale bars, unknown instrument and unknown horizon are refused with distinct errors; a later bar cannot change a stored forecast; health degrades a model with bad recent outcomes; **unknown horizons on all four read paths raise `ForecastValidationError`**; **a blank `model_id` is refused**; the contract states its limits; a failed publication never loses the forecast |
| `EngineEvaluationTests` | a due forecast is evaluated and snapshotted; evaluation waits before the target and while no bar exists; unresolved after the grace period; an evaluated forecast is never rewritten; `performance.updated` is published |
| `EngineTrainingTests` | training persists scores, selection, model states, calibration and a training run; **the baseline is calibrated once, not twice**; **re-training at the same cutoff is idempotent**; **a suspended model cannot be promoted (409 state error)**; a forecast uses the selected model; insufficient history refuses training; promotion without measured evidence is refused |
| API tests (`tests/test_novamarket_api.py`) | status reports horizons and policy; unknown horizon is `422`; **unknown horizon on reads is `422`**; **calibration without `model_id` is `422`**; unknown instrument is `404`; empty reads are honest |

Route/registry pins: 61 market routes (11 new), 23 capabilities (4 new), 63
implemented events (11 new), and the future-event table no longer lists
`forecast.created`.

## Defects found and fixed

### During initial implementation

1. **Forecast engine was never closed.** Service shutdown closed market-data,
   radar, discovery and wallet but not forecasting, leaking the SQLite handle
   across disable/enable. Fixed in the shutdown path.
2. **`novamarket.forecast.performance.updated` was declared but never
   published.** Fixed in `evaluate_due` after the snapshot is saved; pinned by
   `test_a_performance_snapshot_is_published`.
3. **NM-06 was still pinned as a future phase** (capability word ban, route
   count 50, event count 52, `forecast.created` used as a future-event example).
   Updated to the implemented counts with a genuine NM-07 example.

### During the audit pass

4. **`historical_mean` and `drift` predicted the median, not their documented
   centre.** `_from_samples` always took `quantile(0.5)`, so a drift of +1.2%
   could be reported as a predicted return of +0.3% while its note claimed the
   drift. Fixed with an explicit `predicted=` argument and interval bounds
   expanded to contain it; pinned by two estimator tests.
5. **Training the baseline calibrated it twice.** When `random_walk` won
   selection, `for model_id in (selected, "random_walk")` wrote the same
   `forecast_calibration` primary key twice and raised `sqlite3.IntegrityError`
   — a `500` on every baseline-winning training run. Fixed by deduplicating
   (`dict.fromkeys`); pinned by a test that drives a baseline win.
6. **An unknown horizon on a read path was a `500`.** `forecast_list`,
   `forecast_models`, `forecast_performance` and `forecast_calibration`
   converted with a bare `Horizon(...)`, and the service converted again before
   the engine could. A blank `model_id` returned a `200 not_calibrated`, and a
   missing horizon silently defaulted to `next_session`. Fixed with one
   `_horizon_label` / `_required_horizon` in the engine and string pass-through
   in the service; now `422` with a named reason, pinned at both levels.
7. **The model lifecycle table was not enforced by the store.**
   `set_model_state` wrote any state it was given, so a `RETIRED` or `SUSPENDED`
   model could be promoted silently — the exact failure the state machine
   exists to prevent, and the reason `ForecastStateError`'s `409` mapping was
   unreachable. `set_model_state` now calls `ensure_model_transition` before
   writing; promotion of a suspended model is a `409`. Pinned at the store and
   engine level.
8. **Re-training at an identical cutoff collided on deterministic ids.** The
   training-run and calibration ids are derived from instrument, horizon and
   cutoff, and both tables used plain `INSERT`, so a repeat `train(at=…)` raised.
   `INSERT OR REPLACE` is correct here: the same id means the same inputs and
   the same record. Pinned by an idempotence test.

Five test expectations were also wrong during the build and audit (RMSE rows, a
zero log loss swallowed by `or 1.0`, reversed pinball asymmetry, "waiting"
asserted before the target was due, a Windows file handle), and were fixed in the
tests. No production assertion was weakened, no test skipped, and no error
swallowed to make this report green.

## Regression check

- The wider market suite (NM-01…NM-05) was re-run after the audit fixes:
  **431 passed, 3 skipped, 188 subtests**, including the 70 radar, 76+22
  discovery and 41 market-data tests.
- Both mypy views remained clean (507 files).
- `docs/API.md` remained in sync.

## What this evidence does and does not establish

**Establishes**

- The pipeline is implemented and exercised end to end on fixtures: bars →
  features → estimator → immutable record → outcome → performance → lifecycle.
- Leakage guards work: a bar after the cutoff cannot change a stored forecast,
  and calibration is fitted on the earlier portion only.
- Immutability is enforced by the database, and the lifecycle by the transition
  table, not by convention.
- API, events, permissions, capabilities and route registries agree with the
  implementation.

**Does not establish**

- Any accuracy, calibration or coverage figure for a real instrument. No live
  market data was used and no such figure is claimed.
- That a model will be accurate in the future; the lifecycle exists because that
  cannot be assumed.
- Live provider behaviour: two live-provider tests are skipped unless
  `NOVAMARKET_LIVE_TESTS=1`.
- Multi-instrument or 63-session training at scale; the suite exercises the
  default and one long horizon on fixtures.

## Remaining issues and unverified requirements

| Item | State |
| --- | --- |
| Automatic scheduling | **Not implemented.** Generation and evaluation happen when `POST /market/forecasts` and `POST /market/forecasts/evaluate` are called; there is no timer, so forecasts are only as regular as the caller. |
| UI surface | **Not implemented.** NM-06 is reachable through the API, events and status/contracts only; no Chat/JARVIS panel renders forecasts. |
| History depth | Capped at 200 bars by NM-02's `history_page_limit` (policy asks for 320). Documented in `NM06_ARCHITECTURE.md`; raises the evidence floor for the 63-session horizon. |
| Multi-instrument / cross-sectional models | **Not implemented.** One instrument and one horizon per forecast; no market-wide model. |
| Live-data validation | **Not performed.** No provider credentials were used; two tests remain gated by `NOVAMARKET_LIVE_TESTS=1`. |
| Calibration at long horizons | Exercised only by unit tests on fixtures; with 63-session windows a 200-bar history rarely reaches the 30-sample calibration floor, so the honest result there is `insufficient`. |
| Forecast UI i18n/accessibility | Not applicable — no UI exists yet. |

## Reproduce

```bash
cd novamarket
PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest tests/test_novamarket_forecasting.py -q
PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest tests/ -q -k "novamarket" -rs
PYTHONPATH=src ../.venv/Scripts/python.exe -m mypy src
PYTHONPATH=src ../.venv/Scripts/python.exe -m mypy src --platform win32
PYTHONPATH=src ../.venv/Scripts/python.exe scripts/generate_api_reference.py --check
```
