# NM-06 — Model Reliability and Lifecycle

A model that produces predictions but cannot demonstrate its reliability must
not be presented as validated. This document defines how NM-06 moves a model
between states, what evidence each move requires, and where the history is kept.

Implemented in `forecasting/models.py` (`ModelState`, `MODEL_TRANSITIONS`) and
`ForecastEngine.train/promote/check_health`; pinned by `ModelLifecycleTests`,
`EngineTrainingTests` and `EngineTests.test_check_health_degrades_a_model_with_bad_recent_outcomes`.

## States

| State | Meaning |
| --- | --- |
| `EXPERIMENTAL` | implemented and measured, but did not match the baseline |
| `VALIDATED` | walk-forward Brier within 5% of the `random_walk` baseline |
| `ACTIVE` | selected for this instrument × horizon, or promoted with measured evidence |
| `DEGRADED` | recently measured performance fell below its operating limits |
| `SUSPENDED` | too many outcomes never resolved; the model is not trusted |
| `RETIRED` | out of service permanently |

State lives on `forecast_models`; it is **never** written onto a forecast. A
forecast records the model and version that produced it, so retiring a model does
not rewrite history.

## Transitions

`ensure_model_transition(current, target)` is the only door. An illegal move
raises `ValueError` instead of overwriting a state.

| From | Allowed to |
| --- | --- |
| `EXPERIMENTAL` | `VALIDATED`, `DEGRADED`, `RETIRED` |
| `VALIDATED` | `ACTIVE`, `DEGRADED`, `RETIRED` |
| `ACTIVE` | `DEGRADED`, `SUSPENDED`, `RETIRED` |
| `DEGRADED` | `ACTIVE`, `SUSPENDED`, `RETIRED` |
| `SUSPENDED` | `EXPERIMENTAL`, `RETIRED` |
| `RETIRED` | — |

Two consequences are deliberate:

- `EXPERIMENTAL → ACTIVE` is illegal: a model cannot skip validation.
- `RETIRED` is terminal. Reviving one is a new model version, not a state edit.

## How training assigns a state

`train(instrument_id, horizon)` runs the chronological validator, then:

1. computes every model's walk-forward Brier;
2. takes the best model with at least `min_validation_samples` (5) origins;
3. selects it **only if** its Brier is strictly lower than `random_walk`'s;
4. labels the selected model `ACTIVE`, a model within 5% of the baseline
   `VALIDATED`, and everything else `EXPERIMENTAL`.

Each model record stores the measured metrics, the baseline Brier and a `reason`
string, so the state can be audited without re-running anything. A training run
with a history shorter than `min_training_bars` (140) is a typed data error; no
model is created and nothing is labelled.

## Promotion

`promote(model_id, instrument_id, horizon)` is for an explicit decision after a
training run:

- an unknown model is `ForecastNotFoundError`;
- a model already `ACTIVE` is returned unchanged (idempotent);
- a model with no measured `brier` score is refused with
  `ForecastValidationError` — "promote it and see" is not available;
- otherwise `EXPERIMENTAL` is first moved to `VALIDATED`, then to `ACTIVE`, and
  `novamarket.forecast.model.activated` is published.

## Degradation and suspension

`check_health()` examines every `ACTIVE` or `DEGRADED` model:

| Trigger | Action |
| --- | --- |
| ≥ 10 resolved outcomes and mean Brier > baseline + 0.05 | `DEGRADED` |
| ≥ 10 resolved outcomes and interval coverage < 0.55 | `DEGRADED` |
| ≥ 10 unresolved outcomes | `SUSPENDED` |

A `DEGRADED` model is not silently trusted again: it returns to `ACTIVE` only
through a state move (a fresh training run or an explicit promotion), and every
change is published (`model.degraded`, `model.suspended`) and appended to the
reliability history with its reason.

Suspension is checked first. A model that cannot be evaluated at all — because
targets never produced bars — is less reliable than one that was evaluated and
scored badly, and is treated that way.

Fewer than 10 resolved outcomes never triggers degradation: the sample is too
small to distinguish a bad model from noise, and the rule says so by doing
nothing.

## Unresolved outcomes

`evaluate_due` leaves a forecast **waiting** while no bar has closed at its
target. After `unresolved_grace_days` (10) it records an `unresolved` outcome
with the reason (`no completed bar at or after …`), publishes
`forecast.evaluation.pending`, and counts toward suspension. An unresolved
forecast is never scored as if the return were zero: that would make a data
outage look like an accurate call.

## Where reliability is visible

| Surface | Contents |
| --- | --- |
| `GET /market/forecasts/models` | per-model state, version, metrics, reason |
| `GET /market/forecasts/performance` | measured snapshots and the live aggregate |
| `GET /market/forecasts/status` | model counts and totals |
| `GET /market/status` | the live `forecasting` block |
| `forecast_reliability` table | append-only transitions with timestamps |
| `forecast.model.*` events | `trained`, `validated`, `activated`, `degraded`, `suspended` |

## What is never claimed

- No model is described as profitable, safe or better than the market. The only
  claim is a measured comparison with a stated baseline on historical,
  non-overlapping windows.
- A `VALIDATED` label means "within 5% of the baseline on the history this
  instrument has", not "expected to work next month".
- Reliability thresholds (0.05 Brier margin, 0.55 coverage floor, 10 outcomes)
  are policy values in `ForecastPolicy`, published in the contract, and can be
  changed only by changing that policy — which changes the feature digest and the
  methodology version stamped on every forecast.
- No degradation, suspension or calibration result in this build was produced by
  live market data. All verification used generated fixtures.
