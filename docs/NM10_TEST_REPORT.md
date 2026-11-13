# NM-10 Test Report

## Commands and results

The focused suite after the NM-10 audit and fixes:

```
PYTHONPATH=src python -m pytest tests/test_novamarket_execution.py -q
21 passed
```

The relevant NovaMarket regressions were run in three groups:

```
PYTHONPATH=src python -m pytest tests/test_novamarket.py tests/test_novamarket_api.py \
  tests/test_novamarket_wallet.py tests/test_novamarket_execution.py -q
223 passed, 218 subtests passed

PYTHONPATH=src python -m pytest tests/test_novamarket_marketdata.py \
  tests/test_novamarket_radar.py tests/test_novamarket_discovery.py -q
161 passed, 2 skipped, 7 subtests passed

PYTHONPATH=src python -m pytest tests/test_novamarket_portfolio.py \
  tests/test_novamarket_risk.py tests/test_novamarket_strategies.py \
  tests/test_novamarket_forecasting.py -q
203 passed, 35 subtests passed
```

Total relevant NovaMarket result: **587 passed, 2 skipped, 260 subtests**.

Static checks:

```
PYTHONPATH=src python -m mypy src
Success: no issues found in 533 source files

PYTHONPATH=src python -m mypy src --platform win32
Success: no issues found in 533 source files

PYTHONPATH=src python scripts/generate_api_reference.py --check
docs/API.md: in sync (507 routes)

git diff --check
clean
```

## Audit findings fixed

The audit found and fixed seven defects:

1. **Risk revalidation used the proposal notional, not the actual order size.**
   The engine now computes the requested quantity first and revalidates NM-08
   with the actual reference-price notional.
2. **Original NM-08 restrictions were not independently checked.** A decision
   carrying restrictions the execution model cannot enforce is now rejected
   before an order opens.
3. **Unusable/suspect market observations could pass freshness checks.**
   `snapshot_for` now refuses an unusable latest reconciliation and
   `invalid`/`suspect` quality, while preserving the explicit, disabled-by-default
   bar fallback.
4. **Fill/order/reservation persistence was split across independent commits.**
   `ExecutionStore.record_fill` now writes the immutable fill, order update,
   transition and reservation consumption/release in one SQLite transaction.
   Wallet and portfolio writes remain before that execution-owned commit.
5. **Reconciliation rounded quantities through money formatting.** Fractional
   quantities are now compared with the quantity precision, and malformed
   position-effect evidence is reported as `invalid_position_effect` instead of
   aborting the reconciliation.
6. **An accepted-but-not-yet-open order could expire illegally.** The lifecycle
   now permits `ACCEPTED -> EXPIRED`; a regression test drives that path.
7. **NM-09 still refused every forward as "NM-10 not implemented".** NM-09 now
   injects a lazy NM-10 handoff, records `execution_order_id`/`execution_status`
   on the proposal, marks only successful handoffs `forwarded_to_execution`, and
   publishes `novamarket.strategy.proposal.forwarded`.

## Coverage

The 21-test NM-10 suite covers approved-proposal submission, duplicate replay,
rejected proposals, stale data, unusable/suspect market data, market/limit/stop/
stop-limit orders, partial and multiple fills, expiry including accepted orders,
actual-notional risk revalidation, risk restrictions, cancellation with
reservation release, SELL reductions, ledger-backed settlement, immutable fills,
deterministic fee/slippage records, and reconciliation passing/overfill detection.

The NM-09 suite additionally covers a successful handoff to the injected NM-10
execution boundary, and the application injects a lazy paper-execution service so
the strategy engine does not own orders or settlement.

The existing market suites pin the route registry, event payloads, capabilities,
permission matrix, API surface/consumer agreement, wallet/ledger integrity and
NM-01…NM-09 contracts.

## Remaining issues and unverified requirements

- **Cross-store atomicity is still not guaranteed.** The execution-owned group is
  atomic, but an NM-03 ledger write or NM-07 position write can succeed before the
  execution commit fails. Reconciliation can expose that state but does not
  repair it. A durable coordinator or compensating transaction is not implemented.
- The market-data, strategy and risk seams are deterministic fakes in the focused
  execution suite. The wallet, portfolio, execution and reconciliation stores are
  real. A full strategy-engine → real NM-08 policy → execution HTTP workflow is not
  covered end to end in this report.
- `pfw_market_clearing` must be funded by prior paper buys before a SELL can settle;
  a directly seeded NM-07 position without clearing liquidity is refused.
- There is no autonomous order-expiry worker; expiry is evaluated on `process`.
- The default fee schedule is illustrative, not verified against a live broker.
- No browser-level UI test exists because NM-10 has no dedicated panel.
- The full repository suite was not run; only the relevant NovaMarket groups.

## Verdict

Functional paper-execution acceptance and all relevant regression groups pass.
The phase remains **PASS WITH LIMITATIONS**, not a claim of distributed
cross-store atomicity, live-market execution, or exact current fee accuracy.
