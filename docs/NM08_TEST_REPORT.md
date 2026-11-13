# NM-08 — Test Report

## Commands and results

All commands run from `novamarket/` with the project virtual environment.

| Command | Result |
| --- | --- |
| `PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest tests/test_novamarket_risk.py -q` | **48 passed** |
| `PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest tests/test_novamarket.py tests/test_novamarket_api.py -q` | **126 passed, 159 subtests** |
| `PYTHONPATH=src ../.venv/Scripts/python.exe -m mypy src` | **clean, 520 files** |
| `PYTHONPATH=src ../.venv/Scripts/python.exe -m mypy src --platform win32` | **clean, 520 files** |
| `PYTHONPATH=src ../.venv/Scripts/python.exe scripts/generate_api_reference.py --check` | **in sync, 469 routes** |
| NM-07 portfolio suite (during NM-07 audit) | 47 passed |
| market regression `-k novamarket` (during NM-07 audit) | 478 passed, 2 skipped, 204 subtests |

## Coverage

- Numerical metrics: drawdown, volatility, VaR, CVaR (including the empirical tail),
  concentration, beta, correlation, Sharpe, Sortino and insufficient-data handling.
- Policies: approval, position/cash/restricted-instrument/capital rejection,
  stale-data rejection, volatility restriction, drawdown review, verified-event
  review, and hard-breach precedence over missing metrics.
- Engine: decision creation, idempotent replay, action-parameter binding (different
  notionals cannot share one decision), approval application, stale-state and
  changed-policy refusal, rejection gate, restricted full-size refusal, insufficient
  data, typed errors, capital allocation against available cash, paper orders,
  aggregate three-portfolio exposure, hypothetical stress (including volatility
  projection), alert lifecycle and same-code dedupe after acknowledgement, policy
  versioning under identical timestamps, portfolio/position reports, contract, and
  API read surfaces.
- Registry pins: 97 market routes, 91 events, 30 capabilities, five risk operations.

## Defects found and fixed in this audit

- Decision ids omitted `side`/`notional`, so a second, larger proposal could replay
  an earlier approval for the same instrument and state.
- Capital allocation/reservation never checked available cash and could approve any
  amount, including one larger than the portfolio's cash.
- `paper_order` was documented but absent from the action vocabulary and projection.
- A `RESTRICTED` decision could be applied at full size once an override existed.
- `apply_decision` did not reject an approval made under a superseded policy version.
- CVaR averaged every return at the VaR threshold, over-weighting the tail when
  returns tied; it now averages the same empirical tail observations as VaR.
- Hard breaches were outranked by missing metrics; the documented hard-first order
  is now enforced.
- Volatility normalised by invested weight, ignoring cash; it now uses book weights.
- A missing forecast provider produced no forecast failure, and forecasts were not
  filtered by the portfolio's horizon.
- Verified high-importance events were only warnings; they now require review.
- Acknowledged alerts were not deduplicated; active alerts now cover acknowledged.
- Policy versions created at the same timestamp could be read out of order.
- Position reports returned `approved` with unmeasured volatility/liquidity.
- Volatility/liquidity stress scenarios reported no scenario metric; they now do.
- Explanations hid violations behind an insufficient-data message.
- Malformed policy values and `ttl_hours` could surface as server errors.

## Remaining issues and unverified requirements

- NM-01…NM-06 phase suites were not re-run after NM-08; their shared market/API
  surface is covered by the 126-test pin run and the earlier 478-test market
  regression.
- Beta and correlation require a benchmark return series; none exists in this
  build, so they are reported as insufficient rather than fabricated.
- No live bid-ask spread exists in the data contract, so liquidity uses traded value.
- Stress results are linear hypothetical estimates, not calibrated forecasts.
- Restricted overrides are recorded for audit but deliberately cannot authorize a
  full-size application; an adjusted proposal must be reassessed.
- No authenticated administrative identity provider exists; the deployment must
  place policy/override routes behind its authenticated boundary.

## Acceptance

All mandatory acceptance criteria are met: metrics validated, versioned policies,
position/portfolio/aggregate risk, forecast and freshness checks, bound pre-action
validation, rejected and restricted proposals blocked, insufficient data never
approved, stress with explicit assumptions, persistent alerts, explainable
decisions, auditable governance, intact earlier functionality, passing tests and
accurate documentation.
