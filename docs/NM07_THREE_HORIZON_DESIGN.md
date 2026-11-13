# NM-07 — Three-Horizon Design

One engine runs three independent policies. They share the candidate universe,
the forecast engine and the ledger, but nothing else: each horizon has its own
wallet, its own limits, its own gate order and its own review cadence.

## Policies

| | Short-term | Medium-term | Long-term |
| --- | --- | --- | --- |
| Forecast horizons | `next_session`, `few_sessions` | `one_week`, `one_month` | `three_months`, `one_month` |
| Weighting | discovery score | risk budget | inverse volatility |
| Max positions | 6 | 10 | 12 |
| Max position weight | 25% | 18% | 15% |
| Min position weight | 5% | 4% | 3% |
| Max sector weight | 40% | 30% | 35% |
| Minimum cash | 10% | 8% | 5% |
| Minimum discovery score | 55 | 50 | 45 |
| Maximum forecast age | 12h | 72h | 168h |
| Maximum uncertainty | medium | high | high |
| Accepted model states | active, validated | active, validated, degraded | active, validated, degraded |
| Drift threshold | 2% | 4% | 6% |
| Rebalance cooldown | 6h | 24h | 72h |
| Maximum turnover | 50% | 35% | 20% |
| Event-pressure exclusion | yes | no | no |
| Review interval | 6h | 24h | 72h |
| Minimum observations for ratios | 20 | 20 | 20 |

These values live in `HORIZON_POLICIES` and are returned by
`GET /market/portfolios/horizons`, `GET /portfolio/status` and every portfolio's
`policy` block. A change is a one-row edit.

## Why the horizons differ

The short horizon trades on attention: discovery scores move first, forecasts
are only trusted for hours, and a confirmed high-importance event is a reason
not to enter. The medium horizon budgets risk across a week or a month, accepts
degraded models and a wider uncertainty band, and tolerates more drift before
acting. The long horizon sizes by inverse volatility, waits days for a forecast,
turns over at most 20% at a time, and is allowed to sit in cash for a long time
when evidence is thin.

The policies are not three copies of one strategy with different numbers: the
weighting function, the gates and the evidence windows all differ.

## Selection gate order

1. Discovery score below the policy minimum → rejected.
2. Data quality `insufficient` or `invalid` → rejected.
3. No forecast for any policy horizon, unreadable timestamp, future timestamp,
   or a forecast older than the policy limit → rejected.
4. Model has no measured state for `(instrument, model, horizon)` → rejected.
5. Model state excluded by the policy (e.g. `suspended`, `experimental`, or
   `degraded` for the short horizon) → rejected.
6. Uncertainty above the policy limit → rejected.
7. Short horizon only: a confirmed, high-importance event is open → rejected.

Every rejection carries the human-readable reason that produced it; the reasons
are stored in the decision record, not just logged.

## Ranking and weighting

Candidates that pass are ranked by a blended score (discovery score, forecast
direction and confidence) and capped at `max_positions`. `allocate()` then turns
the ranking into target weights using the horizon's weighting method:

- **score** — the blended score, normalised.
- **risk_budget** — score scaled by forecast confidence and interval width.
- **inverse_volatility** — the inverse of the forecast interval width, so wider
  uncertainty receives less capital.

Predicted return alone never sets a weight; it is one input among confidence,
uncertainty, discovery evidence and event pressure.

## Calendar

`review_interval_hours` is the intended cadence and `cooldown_hours` is the hard
minimum between applied rebalances. `POST /market/portfolios/{id}/rebalance`
returns `skipped: "cooldown"` rather than forcing an action. There is no
background scheduler in this phase; an operator or a later supervisor calls the
endpoint on the stated cadence.
