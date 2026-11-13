# NM-08 — Stress Testing

Stress testing estimates how a portfolio would behave under explicitly hypothetical
shocks. Results are **not forecasts** and must never be presented as expected
outcomes.

## Scenarios

The registry covers broad-market decline, sector decline, volatility spike,
liquidity deterioration, interest-rate shock, currency shock, commodity shock,
geopolitical disruption and company-specific adverse events. Each scenario declares
its kind, a shock size and the targets it applies to. Custom scenarios can be
supplied by the caller; they are labelled hypothetical in the same way.

## Method

1. Capture the current valued holdings and cash.
2. Apply the scenario shock to the affected instruments and/or sectors.
3. Recompute position values, total value and portfolio weights.
4. Report the value change, percentage impact, position-level impact, resulting
   drawdown proxy, liquidity note and capital impact.

The calculation is linear in the shock and assumes no trading during the shock. It
does not model correlations, feedback loops or second-order liquidity effects.

## Output

Every result carries `hypothetical: true`, `is_forecast: false`, the scenario
definition, portfolio id, timestamp and the assumptions. Stress runs are persisted
and exposed through `POST /market/risk/stress` and `GET /market/risk/stress`.

## Assumptions and limitations

- Shocks are declared percentages, not calibrated historical events.
- Sector shocks apply uniformly to every holding in that sector.
- Cash is assumed unchanged unless the scenario explicitly includes a capital shock.
- No historical stress window is replayed: NM-02's stored history is used for
  volatility and drawdown, not for scenario calibration.
- Results are diagnostic inputs to review; they do not approve or reject an action
  on their own.
