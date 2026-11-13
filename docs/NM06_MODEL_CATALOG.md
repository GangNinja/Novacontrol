# NM-06 — Model Catalog

Six models are implemented in `forecasting/estimators.py`. All are deterministic
given the same closes, horizon and policy, and all are deliberately transparent:
with a few hundred daily bars a complex model would add tuning surface rather
than demonstrated skill, and the phase's rule is that a model is only called
better when walk-forward validation says so.

`MODEL_IDS` and `MODEL_METADATA` are the single registry. The catalog version is
`nm06-models-1.0` and is stored on every forecast and model record.

| `model_id` | Kind | Prediction | Interval |
| --- | --- | --- | --- |
| `zero_return` | baseline | exactly `0.0` | none (point baseline) |
| `random_walk` | baseline | median of empirical h-session returns | 10th/90th percentile of those returns |
| `historical_mean` | statistical | mean of non-overlapping h-session returns | 10th/90th percentile of the same-mean residuals |
| `drift` | statistical | trailing 21-session mean return × h | 10th/90th percentile of drift-shifted h-session returns |
| `ewma` | statistical | trailing 21-session mean × h | normal, σ = EWMA volatility × √h |
| `bootstrap` | statistical | median of a seeded bootstrap of h-session sums | 10th/90th percentile of the bootstrap |

## `zero_return`

The point baseline. It predicts no change and carries **no probabilities and no
interval**: `p_up`, `p_down`, `p_sideways` and the interval bounds are `None`.
It exists so that every other model has a floor to beat, and the metrics layer
records it with `brier = None` rather than inventing numbers for a model that
makes no probabilistic claim. `generate()` refuses it because a forecast requires
an interval.

## `random_walk`

Persistence. The point forecast is the median of the observed, **non-overlapping**
h-session returns, and the interval is their 10th/90th percentile.

This is the default model and the validation baseline. Its h-session returns are
sampled every `h` bars (`closes[i] / closes[i-h] − 1`), so overlapping windows
never inflate the sample or leak the same move twice.

## `historical_mean`

The mean of the same non-overlapping h-session returns, with the empirical
distribution of deviations around that mean. It answers "what if the average
history repeats", and its interval reflects how wrong that average has been.

## `drift`

The trailing 21-session mean daily return extrapolated over the horizon, with the
h-session returns re-centred on that drift. It is the short-momentum alternative
to `historical_mean` and is the model most sensitive to a regime change — which
is why it must beat the baseline rather than be assumed better.

## `ewma`

A normal predictive distribution: mean = trailing 21-session mean × h,
σ = exponentially weighted volatility (span 21) × √h. Probabilities come from the
normal CDF at the ±`directional_threshold_pct` edges, so `P(up)`, `P(down)` and
`P(sideways)` sum to 1 by construction. This is the only parametric model in the
catalog; it exists to represent a smooth, thin-tailed alternative to the empirical
models.

## `bootstrap`

A seeded bootstrap of daily returns compounded over the horizon. The seed is
`sha256` of the last 80 closes, so identical history gives an identical forecast
and a changed history changes the sample. It uses `bootstrap_paths` (400) paths
over at most `max_bootstrap_bars` (400) daily returns. It captures fat tails and
path compounding that a normal model cannot, at the cost of being the slowest
model in the catalog.

## Probabilities

All distributional models report the same three probabilities, defined once in
the contract:

- `up`: the h-session return is greater than `+0.5%`;
- `down`: the h-session return is less than `−0.5%`;
- `sideways`: the absolute return is at most `0.5%`.

`ForecastRecord` refuses a record whose three probabilities do not sum to
1 ± 1e-6, so no model can ship an incoherent distribution. Serialised values are
rounded to six decimals, which is inside the stated tolerance.

## Selection

`train()` walks the models through the chronological validator and picks the
**lowest walk-forward Brier score**, but only if it is strictly better than
`random_walk`. With no better model the baseline stays selected. A model is:

- `ACTIVE` when selected;
- `VALIDATED` when its Brier is within 5% of the baseline;
- `EXPERIMENTAL` otherwise.

No model is described as accurate, profitable or superior without those measured
numbers, and no number in this document is a performance claim.

## Determinism

| Source of variation | Handling |
| --- | --- |
| Random numbers | only `bootstrap`, seeded from the input closes |
| Iteration order | bars sorted by timestamp; models iterated in registry order |
| Wall clock | passed in as `cutoff`; no estimator reads a clock |
| Floating-point drift | all arithmetic is plain IEEE-754 double; comparisons use explicit tolerances |

## Adding a model

1. Add the id to `MODEL_IDS` (and `PROBABILISTIC_MODELS` if it has a
   distribution) and a `(kind, description)` row to `MODEL_METADATA`.
2. Implement the branch in `forecast()` returning a `ForecastOutput` whose
   interval contains its point forecast.
3. Nothing else: training, scoring, calibration, selection, storage and API
   expose it automatically, and the catalog tests run it with the rest.
