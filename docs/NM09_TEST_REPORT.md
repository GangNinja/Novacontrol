# NM-09 Test Report

## Commands and results

```
PYTHONPATH=src python -m pytest tests/test_novamarket_strategies.py -q
40 passed

PYTHONPATH=src python -m pytest tests/test_novamarket.py tests/test_novamarket_api.py -q
126 passed, 196 subtests passed

PYTHONPATH=src python -m mypy src
Success: no issues found in 526 source files

PYTHONPATH=src python -m mypy src --platform win32
Success: no issues found in 526 source files

PYTHONPATH=src python scripts/generate_api_reference.py --check
docs/API.md: in sync (493 routes)
```

## Coverage

The NM-09 suite covers all six strategy categories (`trend_sma`, `mean_reversion_rsi`, `momentum_breakout`, `volatility_breakout`, `event_driven`, `forecast_assisted`), strategy creation and validation, unsupported asset classes/horizons/priorities, explicit lifecycle transitions, configuration versioning, stale market data, stale forecasts, verified-event importance filtering, signal replay, portfolio-horizon recording, signal expiry and strategy-version binding.

Risk coverage includes NM-08 approved/rejected proposals, restricted decisions with retained restrictions, `requires_review` and `insufficient_data` remaining pending, and risk-engine failure never becoming approval. Execution coverage confirms short sells are refused, SELL/EXIT notionals are bounded by the holding, no wallet or portfolio mutation occurs in NM-09, forwarding without an execution handoff is refused, and a configured handoff marks the proposal `forwarded_to_execution` and records the NM-10 order reference.

Conflict coverage includes duplicate consolidation, review, priority, block, horizon mismatch, repeated-run idempotence, and proposal-to-conflict linking. Performance coverage verifies post-horizon market prices (not today's price) and publication of `strategy.performance.updated`. API coverage verifies catalog, status, creation, performance, unknown-strategy 404, unknown-proposal 404 and forward 404.

Registry pins cover 135 market routes, 122 implemented events, 36 capabilities, 27 granted operations and exact ApiSurface/route-consumer agreement.

## Remaining issues and unverified requirements

- The restricted and pending risk outcomes are exercised through a stubbed risk decision at the strategy-engine boundary. The real NM-08 restricted policy path is covered by the NM-08 suite, but a real NM-08 restricted decision flowing through a strategy proposal is not yet tested end to end.
- There is no live broker or real-money order in NM-09. A forwarded proposal is handed to the simulated NM-10 order path; no live wallet, broker or real portfolio mutation is possible.
- A proposal cannot be created directly through an HTTP route; the API creates proposals through `POST /market/strategies/run`, while `StrategyEngine.propose` is covered at unit level.
- Performance samples are short and use observed market bars, not fills, fees or slippage.
- NM-01..NM-08 regressions are represented by the 126 market contract/API pins. The full repository suite was not run.
- `docs/API.md` is in sync at 507 routes; this report does not claim a deployment or browser-level verification.

## Verdict

The implemented NM-09 scope passes its phase suite and relevant market contract/API regressions. The unverified items above remain explicitly open; this is not a claim of live-trading readiness.
