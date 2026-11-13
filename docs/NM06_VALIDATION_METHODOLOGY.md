# NM-06 — Validation Methodology

Validation answers one question: **would this model have been better than the
baseline on data it had not seen, at the time it would have been used?**

Everything below is implemented in `forecasting/validation.py` and
`forecasting/metrics.py`, and pinned by `ValidationTests` and `MetricTests`.

## Chronological walk-forward, never a random split

`walk_forward_origins(bar_count, min_training, step)` produces `(origin, target)`
pairs:

- `origin` is the index of the last bar visible to the model;
- `min_training` is the first origin, so nothing is scored before the model has
  a real training history;
- the model fits on `closes[:origin]` and predicts the return from
  `closes[origin]` to `closes[origin + horizon]`;
- the next origin is `origin + step`, with `step = horizon`.

Because the step equals the horizon, **targets never overlap**: the same move is
not counted twice and a lucky week cannot be scored five times. Origins are
strictly increasing, and every target stays inside the observed history.

There is no random shuffle, no k-fold split and no future bar in any training
window. A random split would let a model learn from next week to predict last
week; the validator cannot express that.

## What is scored

For each origin and each model:

| Recorded value | Meaning |
| --- | --- |
| predicted return | the model's point forecast |
| realised return | `close[origin + horizon] / close[origin] − 1` |
| `p_up`, `p_down`, `p_sideways` | the model's directional distribution |
| lower / upper | the model's 10th/90th percentile interval |

The realised return is classified with the same ±0.5% thresholds the
probabilities use, so accuracy and Brier are measured against one definition of
"up", "down" and "sideways".

## Metrics

| Metric | Definition | Reads |
| --- | --- | --- |
| MAE | mean absolute return error | point accuracy |
| RMSE | root mean squared return error | point accuracy, penalising large misses |
| median absolute error | median absolute return error | robustness to outliers |
| directional accuracy | share of origins where the most likely direction matched | usefulness, not calibration |
| Brier (multi-class) | mean squared distance from the one-hot outcome | probability quality |
| log loss | `−log P(realised direction)` | probability quality, harsher on confident mistakes |
| interval coverage | share of realised returns inside `[lower, upper]` | does the interval mean what it says |
| mean interval width | mean `upper − lower` | sharpness; a wide interval covers by being useless |
| pinball loss | quantile loss at each interval edge | quantile quality |
| calibration error (ECE) | bin-weighted `|accuracy − confidence|` | probability calibration |

MAE/RMSE are in return units. MAPE is deliberately absent: a return can be zero
or negative, where percentage error is undefined.

Coverage and width are reported together on purpose. A 100% coverage from a ±50%
interval is not a success, and the reliability rules read both.

## Selection rule

`best_model(result, minimum_samples=5)` returns the model with the lowest Brier
score among those with at least the minimum number of origins. `train()` selects
it **only if it beats `random_walk`**; otherwise the baseline is selected. This is
the guard against "the most complex model won by noise on 5 samples".

Models within 5% of the baseline Brier are labelled `VALIDATED`; models that
miss it are `EXPERIMENTAL`. Nothing is labelled `ACTIVE` without the comparison.

## What validation cannot prove

- It cannot prove future accuracy. It measures what happened on the history the
  instrument actually has.
- It cannot prove a model is safe to trade. NM-06 makes no trading decision.
- With fewer than `min_validation_samples` origins the result is not a validation
  at all: `minimum_samples` gates the selection, and `train()` refuses a history
  shorter than `min_training_bars` (140) outright.
- A single instrument's history is not a market-wide claim; each instrument and
  horizon carries its own selection and its own model states.

## Horizon-specific behaviour

The validator runs per horizon. The 63-session horizon has roughly four times
fewer non-overlapping windows than the 1-session horizon on the same history, so
its evidence is thinner by construction; the same `minimum_samples` gate applies,
and a long horizon that cannot clear it stays unselected rather than being
presented as validated.

## Reproducibility

`train()` records a training run with the policy, feature-set version, model
catalog version, sample count and `"seed": "deterministic"`. Re-running it on the
same bars and cutoff reproduces the same scores, the same selection and the same
calibration inputs. The bootstrap's seed is derived from its input closes, not
from process state.

## Verification in this build

- `ValidationTests` pins chronological ordering, non-overlap, per-model sample
  parity, the baseline comparison and the point baseline carrying no
  probabilities.
- `MetricTests` pins each metric against hand-computed values, including the
  directional edges and the pinball asymmetry.
- `EngineTrainingTests` pins that training stores scores, selection, model states,
  calibration reports and a training run, and that promotion without a measured
  Brier score is refused.

All of this runs on generated fixtures, not on live market data. No accuracy
figure in this document is a claim about any real instrument.
