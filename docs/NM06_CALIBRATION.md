# NM-06 — Probability Calibration

A probability is calibrated when events it gives probability *p* happen about
*p* of the time. NM-06 measures that on held-out, later data and refuses to call
anything calibrated when the sample cannot support the claim.

Implemented in `forecasting/calibration.py`; pinned by `CalibrationTests` and the
calibration half of `EngineTrainingTests`.

## What is calibrated — and what is not

- **Calibrated:** the directional probability of the realised direction. For each
  evaluated forecast the validator records `(raw_probability, outcome)` where
  `raw_probability` is the model's probability for the direction that actually
  occurred.
- **Not calibrated:** the return interval. The 10th/90th percentile interval is
  measured by **coverage** (see `NM06_FORECAST_CONTRACTS.md`), which is a
  different property. The calibration report says so in its notes rather than
  implying the interval was recalibrated.

## Method: isotonic regression (PAVA)

`isotonic_fit(samples)` sorts the samples by raw probability and pools adjacent
violations with the pool-adjacent-violators algorithm, producing a monotone
step map:

- `IsotonicCalibrator(knots_x, knots_y)` is the fitted map;
- `calibrate(p)` clamps to `[0, 1]`, returns the end value outside the knot
  range, and linearly interpolates between knots otherwise;
- the map is exported as `{"method": "isotonic-pava", "knots": [[x, y], ...]}` and
  stored with the report.

Isotonic regression is chosen because it assumes only monotonicity — a higher
raw probability must not map to a lower calibrated one — and needs no
distributional assumption. The alternative (a parametric sigmoid) would impose a
shape the data has not earned.

## Chronological isolation

`calibrate_probability(samples, min_samples, holdout_fraction)`:

1. orders the samples chronologically (the validator appends them in origin
   order);
2. splits at `int(n × (1 − holdout_fraction))` — with the default 0.4 holdout,
   the **earlier 60%** fits the calibrator and the **later 40%** scores it;
3. fits `isotonic_fit` on the earlier portion only;
4. scores Brier, log loss and expected calibration error **before** and **after**
   on the later portion;
5. returns the report, or `insufficient` with a reason.

Fitting and scoring on the same rows would report an improvement the model has
not earned. The split is chronological, not random, for the same reason the
validator is: a calibration learned from later data would be a leak.

## Statuses

| Status | Condition | Meaning |
| --- | --- | --- |
| `insufficient` | fewer than `calibration_min_samples` (30) samples | no calibration claim is made; the reason states how many were available |
| `insufficient` | either portion has fewer than 2 samples after the split | the split cannot support a fit |
| `calibrated` | both portions are large enough | fit on the earlier portion, scored on the later |

There is no "partially calibrated" state. A model whose probabilities were never
calibrated returns `not_calibrated` from the read endpoint, with the detail that
no calibration has been run for that model and horizon.

## Report shape

`CalibrationReport` (serialised by `to_dict()`):

| Field | Meaning |
| --- | --- |
| `status` | `calibrated` or `insufficient` |
| `samples` | total chronological samples |
| `holdout_samples` | samples scored out of sample |
| `brier_before` / `brier_after` | multi-class-free binary Brier on the holdout |
| `log_loss_before` / `log_loss_after` | binary log loss on the holdout |
| `calibration_error_before` / `calibration_error_after` | ECE on the holdout |
| `calibrator` | the fitted isotonic knots, or `null` |
| `reason` | why the status is `insufficient` |
| `notes` | the isolation note and the interval caveat |

`brier_after > brier_before` is a possible and reported outcome: an isotonic fit
on a small earlier portion can be worse out of sample. The report shows it rather
than hiding the comparison.

## When calibration runs

`train()` calibrates the **selected model** and the **`random_walk` baseline**,
and stores one report per model and horizon:

```
calibration_id = "fcal_" + sha256(instrument | horizon | model | cutoff)[:16]
```

Storage is append-only (`forecast_calibration`), and each run publishes
`novamarket.forecast.calibration.completed` with the status and sample count. The
training run records the status of each calibration it produced, so a later
reader can tell which model version was actually calibrated.

## What the numbers mean

- `brier_before` → `brier_after` shows whether calibration helped on data it did
  not see. It is not a claim that future probabilities will be calibrated.
- ECE before/after shows the reliability gap directly: predicted confidence
  versus observed frequency.
- A calibrated probability is still a probability. It does not become a
  prediction of direction, and it never becomes investment advice.

## Insufficiency is a result

Reported honestly and stored:

- too few forecasts evaluated → `insufficient`;
- a horizon whose non-overlapping windows are too few → `insufficient`;
- `train()` on a history shorter than `min_training_bars` → a typed data error,
  not an empty "calibrated" report.

The test suite pins the insufficiency path, the out-of-sample split and the
monotonicity of the fitted map. No live calibration result is claimed anywhere in
this build; all reports in the tests come from generated fixtures.
