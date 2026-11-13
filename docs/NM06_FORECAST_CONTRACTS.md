# NM-06 — Forecast Contracts

The wire and storage contract between the forecasting engine, the API and
anything that records a forecast. Field names here are the names on the wire;
`forecasting/models.py` is the single definition and `to_dict()` is the single
serialiser.

## `ForecastRecord`

| Field | Type | Meaning |
| --- | --- | --- |
| `forecast_id` | string | `fc_` + sha256(instrument, created_at, horizon, model_id)[:20] |
| `instrument_id` | string | e.g. `XNSE:ALPHA` |
| `created_at` | ISO-8601 UTC | when the forecast was made; equals the data cutoff |
| `data_cutoff` | ISO-8601 UTC | the newest instant any input may come from |
| `horizon` | enum | `next_session`, `few_sessions`, `one_week`, `one_month`, `three_months` |
| `horizon_sessions` | int | 1, 3, 5, 21 or 63 |
| `target_at` | ISO-8601 | the session date being forecast, after the cutoff, on a weekday |
| `model_id` | string | one of the six catalog ids |
| `model_version` | string | catalog version (`nm06-models-1.0`) |
| `feature_set_version` | string | `nm06-features-1.0` |
| `predicted_return` | float | decimal return, e.g. `0.012` = +1.2% |
| `predicted_price` | float | `last_close × (1 + predicted_return)`, context only |
| `p_up` / `p_down` / `p_sideways` | float | directional probabilities, sum to 1 ± 1e-6 |
| `lower_return` / `upper_return` | float | 10th/90th percentile of the predictive distribution |
| `lower_price` / `upper_price` | float | the same bounds in price terms |
| `uncertainty` | enum | `low`, `medium`, `high` from interval width |
| `uncertainty_width` | float | `upper_return − lower_return` |
| `data_quality` | string | `good`, or `partial` when some bars lacked volume |
| `regime` | enum | `trending_up`, `trending_down`, `sideways`, `high_volatility`, `low_volatility`, `uncertain` |
| `event_ids` | string[] | confirmed NM-04 events known at the cutoff (max 10) |
| `candidate_score` | float \| null | NM-05 score, provenance only |
| `methodology_version` | string | `nm06-forecast-1.0` |
| `digest` | string | sha256 of the methodology, features, model, probabilities, interval and policy |
| `notes` | string[] | model note and any documented adjustment |
| `is_price_prediction` | `false` | always; part of the contract |
| `is_investment_advice` | `false` | always |
| `guaranteed` | `false` | always |

`ForecastRecord` validates on construction: probabilities in `[0, 1]`, their sum
within 1e-6 of 1, the point forecast inside its interval, `target_at > created_at`,
`data_cutoff <= created_at`, `horizon_sessions >= 1`. An invalid record cannot be
stored.

## Directional probabilities

| Event | Condition |
| --- | --- |
| `up` | h-session return > `+directional_threshold_pct` (default 0.5%) |
| `down` | h-session return < `−directional_threshold_pct` |
| `sideways` | `|return| <= directional_threshold_pct` |

Tolerance: `1e-6`. The same thresholds classify realised returns during
evaluation, so accuracy and Brier measure one definition, not two.

## Prediction interval

- 10th and 90th percentile of the model's predictive distribution;
- guaranteed to contain the point forecast (`lower <= predicted <= upper`);
- **coverage is measured**, never assumed: `inside_interval` is recorded per
  outcome and aggregated into coverage;
- a confirmed high/critical NM-04 event widens the interval by
  `event_interval_widen` (1.25×) around the point forecast. This is a
  conservative, documented adjustment, not a validated accuracy claim, and the
  forecast says so in `notes`.

## `ForecastOutcome`

| Field | Meaning |
| --- | --- |
| `forecast_id`, `instrument_id` | the forecast being resolved |
| `status` | `evaluated` or `unresolved` |
| `evaluated_at` | when the outcome was recorded |
| `actual_return`, `actual_price` | realised values, `null` when unresolved |
| `error` | `predicted_return − actual_return` |
| `direction_correct` | whether the most likely direction matched |
| `brier` | multi-class Brier score of the three probabilities |
| `log_loss` | `−log P(realised direction)` |
| `inside_interval` | whether the realised return was inside the interval |
| `reason` | why an outcome is unresolved |

One outcome per forecast: a second insert is refused, so a forecast is scored
once and never rescored against a different bar.

## Immutability

`forecast_records` is append-only at the database level:

- `UPDATE` raises (`sqlite3.IntegrityError`);
- `DELETE` raises;
- outcomes, calibration, performance and reliability history are insert-only;
- model state is the only mutable thing, and it moves through
  `MODEL_TRANSITIONS`.

## HTTP endpoints

All are authenticated and `no-render`.

| Method | Path | Behaviour |
| --- | --- | --- |
| `POST` | `/market/forecasts` | body `instrument_id`, `horizon`; stores and returns the forecast |
| `GET` | `/market/forecasts` | query `instrument_id`, `horizon`, `status`, `limit`; newest first |
| `GET` | `/market/forecasts/status` | horizons, policy, model counts, totals |
| `GET` | `/market/forecasts/models` | query `instrument_id`, `horizon`; states and measured metrics |
| `GET` | `/market/forecasts/performance` | query `model_id`, `instrument_id`, `horizon`, `limit`; snapshots and the live aggregate |
| `GET` | `/market/forecasts/calibration` | query `model_id` (required) and `horizon`; latest report or `not_calibrated` |
| `POST` | `/market/forecasts/train` | body `instrument_id`, `horizon`; walk-forward comparison, selection, calibration |
| `POST` | `/market/forecasts/evaluate` | resolves every due forecast; returns evaluated/waiting/unresolved counts |
| `POST` | `/market/forecasts/models/{model_id}/promote` | promotes a model with measured evidence for its instrument and horizon |
| `GET` | `/market/forecasts/{forecast_id}` | one forecast, `404` when unknown |
| `GET` | `/market/forecasts/{forecast_id}/outcome` | the outcome, or a `pending` body before the target produces one |

### Errors

| Condition | Status |
| --- | --- |
| Unknown instrument or forecast | `404` (`ForecastNotFoundError`) |
| Unknown horizon, missing body field, unmeasured promotion | `422` (`ForecastValidationError`) |
| Too little history, stale bars, unusable market data | typed data error, refused |
| Lifecycle move the state machine refuses (e.g. suspended → active) | `409` (`ForecastStateError`) |

The API never returns a forecast for a request it could not satisfy. A
missing or unknown read parameter is a `422`, never a silently defaulted
read.

## Events

| Event | Payload fields |
| --- | --- |
| `novamarket.forecast.created` | `forecast_id`, `instrument_id`, `horizon`, `model_id` |
| `novamarket.forecast.completed` | the above plus `predicted_return`, `p_up` |
| `novamarket.forecast.evaluation.pending` | `forecast_id`, `instrument_id`, `reason` |
| `novamarket.forecast.evaluated` | `forecast_id`, `instrument_id`, `horizon`, `model_id`, `direction_correct`, `brier` |
| `novamarket.forecast.model.trained` | `model_id`, `instrument_id`, `horizon`, `state` |
| `novamarket.forecast.model.validated` | `instrument_id`, `horizon`, `selected_model`, `baseline_brier` |
| `novamarket.forecast.model.activated` | `model_id`, `instrument_id`, `horizon` |
| `novamarket.forecast.model.degraded` | `model_id`, `horizon`, `brier`, `coverage` |
| `novamarket.forecast.model.suspended` | `model_id`, `horizon`, `unresolved` |
| `novamarket.forecast.calibration.completed` | `model_id`, `horizon`, `status`, `samples` |
| `novamarket.forecast.performance.updated` | `model_id`, `horizon`, `samples` |

Payload keys are validated against `EVENT_PAYLOAD_FIELDS`; a payload that drops a
field is refused before publication. Publication happens **after** the row is
committed, and a publication failure never removes the stored forecast.

## Status and contract payloads

`GET /market/forecasts/status` returns `initialised`, `phase`, `schema_version`,
`policy`, `horizons`, `feature_set_version`, `model_catalog_version`, `models`,
`metrics`, `last_error`. When the subsystem is not operational it returns
`initialised: false` with the reason, never an empty success.

`GET /market/contracts` carries the forecast contract: implemented flag, horizons,
probability definition and tolerance, interval quantiles, the model catalog, the
policy, the data sources, the guarantees list and the event names.

## Permissions

| Operation | Capability | Risk | Granted |
| --- | --- | --- | --- |
| `READ_FORECASTS` | `market.forecast.read` | low | yes |
| `RUN_FORECASTS` | `market.forecast.run` | low | yes |
| `TRAIN_FORECAST_MODELS` | `market.forecast.train` | medium, reversible | yes |

`market.forecast.models` exposes measured model state. No forecast capability
places an order or moves money, and `EXECUTE_LIVE_TRADE` remains denied.
