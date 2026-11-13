# NM-12 Shadow Mode — Safety Boundary

Shadow Mode is strictly non-executing. This is enforced by architecture, not by
a label on a response.

## What shadow code may touch

- Its own SQLite database.
- NM-02 reads (`latest`, `bars`).
- NM-07 portfolio `detail()` (read-only baseline capture).
- NM-09 pure evaluators.
- NM-08 pure policy evaluation.
- NM-10 pure pricing/fee functions.
- NM-11 completed backtest results (read-only comparison).
- The application event bus (typed events only).

It has no import of `ExecutionEngine`, `ExecutionStore`, the wallet engine, the
ledger, portfolio mutation methods or a broker adapter. There is no route that
places an order, fills an order, reserves capital, transfers NovaCoin or changes
a paper position.

## Explicit execution guard

`ShadowExecutionGuard` exposes the execution-shaped verbs a caller might reach
for — `submit_order`, `execute`, `place_order`, `process_order`,
`reserve_capital`, `debit`, `credit` — and every one raises
`ShadowIsolationError` before touching any state.

Every projection is also checked by `assert_projection`:

- `simulated` must be true;
- `execution_order_id` must be empty;
- the label must be `hypothetical order projection`.

The API serializes `execution_order_id: null`, `execution_permitted: false` and
`is_execution: false` on every projection.

## Why a shadow session cannot become an execution session

1. Session configuration has no execution, broker or wallet fields.
2. The proposal path calls pure NM-10 pricing only; it never constructs an
   NM-10 order.
3. The guard is the only execution-shaped surface the service holds, and it is
   deny-only.
4. No application wiring passes an execution gateway into `ShadowService`.
5. NM-13 adaptive learning is not constructed, scheduled or activated by a
   shadow session.

## Test evidence

`tests/test_novamarket_shadow.py` covers:

- every guard method raising;
- a projection with an execution id being refused;
- no execution engine attribute on the service;
- no portfolio/baseline mutation after cycles;
- projections carrying no execution id;
- wallet and portfolio status unchanged across the HTTP lifecycle test;
- a real NM-02-store integration run producing only shadow records.

The API contract test separately asserts no `/market/trade/live` or `broker`
route exists.
