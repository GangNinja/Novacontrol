# NM-11 Test Report

## Verdict

**PASS.** Every mandatory NM-11 requirement is implemented and covered by a
test. The subsystem reuses NM-02/04/06/07/08/09/10 rather than duplicating
them, replays strictly point in time, simulates all four order types with
partial fills, fees, slippage, liquidity, expiry, cancellation, halts and
calendar closures, uses `Decimal` throughout, never touches a production book,
persists immutable evidence (including comparisons and walk-forward runs), and
reproduces identical inputs byte-for-byte through a full replay verification.

## Requirement coverage

| Requirement | Implementation | Test evidence |
|---|---|---|
| Reuse NM-01…NM-10 | NM-02 bars/calendars, NM-04 events, NM-06 forecasts, NM-07 `Position`, NM-08 `evaluate_policy`, NM-09 evaluators, NM-10 pricing | `LeakageTests`, `RiskPolicyTests`, `AccountingTests`, `RealMarketDataIntegrationTests` |
| Strict point in time | `available_at` gate, event/forecast timestamps, next-bar execution | `test_an_order_never_fills_at_the_signal_close`, future event/forecast tests |
| Market/limit/stop/stop-limit | `_new_order` + `_execute_order` | `OrderSimulationTests` (four types, trigger/no-trigger) |
| Partial fills | Previous-bar participation cap, remainder pending | `test_partial_fills_keep_the_remainder_pending_until_expiry` |
| Fees, slippage, liquidity | NM-10 `execution_price` / `fee_breakdown` | `test_market_buy_fills_at_the_open`, `test_fees_and_realized_pnl_are_recorded_on_the_trade`, previous-bar liquidity test |
| Expiry | Bar-count TTL | `test_order_expiry_is_counted_in_bars_across_a_weekend` |
| Cancellation | Row state + in-flight flag | created-run cancel test, `test_a_concurrent_cancel_aborts_a_running_replay` |
| Halts and closures | `tradable` flag, NM-02 calendar weekends/holidays | halted-bar test, `test_a_bar_on_an_exchange_holiday_is_excluded`, missing-session assertion |
| Decimal, isolated book | `Decimal` money/quantity, NM-07 `Position`, no wallet/ledger writes | `AccountingTests`, contract `real_money: false` |
| Persist configuration/dataset/snapshots/orders/fills/decisions/metrics/warnings/comparisons | SQLite completed runs with bars; immutable comparison table | reopen/immutability tests, comparison persistence test |
| Identical inputs reproduce | Deterministic hash excluding run ids | two independent stores + `verify()` |
| No NM-12+ | No live execution or later phase | route contract excludes live trading |

## Defects found and fixed

| Defect | Effect | Fix |
|---|---|---|
| `result_model()` parsed a bare configuration as a result | `KeyError: 'config'` on new backtests; `/start` broken | Only completed rows with `repro_hash` are parsed |
| `config()` parsed a completed result as a configuration | compare/sensitivity failed on finished runs | Completed payloads read the nested config |
| Repro hash used as the `backtest_runs` primary key | forced replay hit `UNIQUE constraint failed` | Unique run id; `(backtest_id, repro_hash)` remains unique |
| Multi-instrument equity used one instrument's close for all | misstated equity | Each instrument marked at its last close; one row per timestamp |
| Average cost recomputed as `(basis + cost)/qty` | wrong cost basis when adding to an existing position | NM-07 `Position` update uses `(basis*old_qty + cost)/new_qty` |
| Order TTL counted calendar days | Friday one-bar orders expired over the weekend | TTL counted in observations |
| Drawdown/restricted gates applied to sells | de-risking exits could be blocked | Entry gates apply to buys only; sells recorded as approved exits |
| Bespoke risk limits duplicated NM-08 | two risk rule tables | One `evaluate_policy` call per entry; raw outcome stored |
| No exchange-session enforcement | holiday bars could trade | NM-02 calendar excludes closed dates; `closed_bars` reported |
| Comparisons were not persisted | persistence requirement unmet | Immutable comparison/sensitivity/walk-forward records + read route |
| `list()` returned bare payloads | created rows lacked id/status | Normalised records for all statuses |
| Validation outside the failure boundary | short datasets stuck at `created` | Failed rows recorded and retryable |
| No walk-forward evaluation | robustness gap | Sequential out-of-sample folds, persisted, with route |
| `Decimal` missing in `novamarket/service.py` | mypy failure | Import added |
| Cancelling a completed run reported success | false cancellation | Refused with a typed error |

## Focused NM-11 tests

```bash
cd novamarket
PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest tests/test_novamarket_backtesting.py -q
```

Result: **48 passed, 4 subtests passed**.

Classes: `ConfigurationTests`, `LeakageTests`, `OrderSimulationTests`,
`RiskPolicyTests`, `AccountingTests`, `BenchmarkRobustnessTests`,
`PersistenceReproducibilityTests`, `MetricsTests`, `BacktestingServiceTests`,
`RealMarketDataIntegrationTests`.

## Relevant integration tests

```bash
PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest \
  tests/test_novamarket_backtesting.py tests/test_novamarket_api.py \
  tests/test_novamarket.py tests/test_novamarket_wallet.py -q
```

Result: **252 passed, 242 subtests passed**.

| Suite | Result |
|---|---|
| `test_novamarket_backtesting.py` | 48 passed, 4 subtests |
| `test_novamarket_api.py` | 41 passed |
| `test_novamarket.py` + `test_novamarket_wallet.py` | 163 passed |

The API suite includes a real HTTP create → start → results → trades → verify →
list → comparisons → walk-forward → status round trip, plus a 404 for an
unknown id and a 422 for an incomplete run. The route contract pins **155**
market routes.

## Static checks

- `mypy src` — clean, 536 source files.
- `mypy src --platform win32` — clean, 536 source files.
- `python scripts/generate_api_reference.py --check` — `docs/API.md` in sync.
- `git diff --check` — clean.

## Scope limitations (not unmet criteria)

1. **Daily frequency only.** Intraday frequencies are rejected by configuration.
2. **Corporate actions require caller-adjusted bars.** No automatic adjustment
   is applied.
3. **Survivorship bias.** The universe is the caller-selected instrument list;
   delistings and historical membership are not reconstructed.
4. **No live provider history was fetched in the test harness.** The real NM-02
   `MarketDataStore` loading path is exercised with ingested fixture bars.
5. **No Monte Carlo/bootstrap resampling.** Walk-forward and parameter
   sensitivity are implemented; resampling is a later extension.
6. **Dependency lockfile hashes are not stored.** The reproducibility payload
   records software version, Python, platform, strategy version,
   execution-config version and risk-policy version.

No profitability claim is made. Synthetic fixtures test mechanism, not
historical performance.
