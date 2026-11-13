# NM-07 — Test Report

Verification was run on 2026-11-02 from `novamarket/` with
`PYTHONPATH=src ../.venv/Scripts/python.exe`. No live market data, broker
session or real money was used.

## Results

| Check | Command | Result |
| --- | --- | --- |
| NM-07 suite | `pytest tests/test_novamarket_portfolio.py -q` | **47 passed** |
| NM-01 surface | `pytest tests/test_novamarket.py -q` | 87 passed, 54 subtests |
| NM-01…NM-07 API | `pytest tests/test_novamarket_api.py -q` | 39 passed, 86 subtests |
| NM-02 market data | `pytest tests/test_novamarket_marketdata.py -q` | 41 passed, 2 skipped |
| NM-03 wallet | `pytest tests/test_novamarket_wallet.py -q` | 76 passed, 22 subtests |
| NM-04 radar | `pytest tests/test_novamarket_radar.py -q` | 70 passed, 7 subtests |
| NM-05 discovery | `pytest tests/test_novamarket_discovery.py -q` | 50 passed |
| NM-06 forecasting | `pytest tests/test_novamarket_forecasting.py -q` | 68 passed, 35 subtests |
| Whole market subsystem | the eight files above | **478 passed, 2 skipped, 204 subtests** |
| Types (Linux view) | `mypy src` | success, 514 files |
| Types (Windows view) | `mypy src --platform win32` | success, 514 files |
| API reference | `generate_api_reference.py --check` | in sync, 450 routes |

The two skips are the existing optional-dependency skips in market data; they
are not NM-07 tests.

## Coverage

`tests/test_novamarket_portfolio.py` drives the real `PortfolioEngine`, the real
`WalletEngine` and the real stores. Only the market-data, discovery, forecast
and radar seams are replaced, matching how the application wires them.

| Area | Tests |
| --- | --- |
| Creation, idempotency, conflicting capital, refuse-before-record | 7 |
| Opening allocation, caps, cash reserve, investable fraction, sector metadata | 5 |
| Selection gates: stale, degraded, missing state, low score, event pressure | 6 |
| Valuation, unrealized P&L, staleness, append-only snapshots, gated ratios | 4 |
| Proposal, apply, cooldown, expiry, replay, failed proposal, no-candidate retention | 6 |
| Lifecycle: pause/resume, illegal transition, close and capital release | 4 |
| Capital conservation, portfolio reconcile, ledger reconcile | 3 |
| Decisions, position history, restart over the same store | 3 |
| Contract, status, horizon independence | 3 |
| HTTP: status, list, 404s, 422 validation, funded end-to-end creation | 6 |

## Defects found and fixed during verification

1. `portfolio.models` did not export the `utcnow` clock the engine imports.
2. `_publish` took a positional parameter named `name`, colliding with the
   `name` payload of `portfolio.created`.
3. Stored proposal intents kept `side` as a string, so replay raised
   `AttributeError` and the opening apply wrote no positions — the first
   end-to-end run produced a funded portfolio with zero holdings.
4. Re-proposing an already-applied book re-inserted the same deterministic
   proposal id and hit the primary-key constraint; proposals now replay.
5. Creation funded and activated a portfolio but never deployed the capital;
   funding now triggers the opening proposal and apply.
6. Stale positions were counted in the valuation but not flagged in holding
   rows; the flag is now computed from the observation timestamp.
7. A refused funding could leave an `initializing` record behind; the funding
   balance is now checked before the record is created.
8. Route-count pins were updated to the measured 78 market routes (61 + 17).

### Second audit pass

9. New positions hardcoded `sector="unknown"`, so the sector cap and sector
   concentration never saw the real sector that discovery and market metadata
   already carried. Positions now record the sector and asset class used for
   sizing (falling back to market metadata for a held name that is no longer a
   candidate), and a test pins it.
10. Re-creating the same horizon/name with a different capital silently replayed
    the original amount. It is now refused with the stored capital named.
11. A rebalance that failed partway through left the proposal `validated`, so a
    retry would replay the ledger but double-count the position writes. Failed
    applies now mark the proposal `FAILED`, and `apply` refuses it.
12. `_price` accepted a selected observation even when the market-data
    reconciliation reported it unusable; it now refuses it and falls back to the
    latest bar.
13. `_candidate_rows` mutated the discovery engine's own dictionaries while
    adding metadata; it now copies each row first.
14. The turnover metric is explicitly labelled cumulative traded notional over
    mean value via `turnover_basis`, instead of implying a period ratio.

## Honest limits of this verification

- Successful creation through HTTP is now covered end to end (wallet → deposit →
  portfolio). Rebalance and apply through HTTP are still covered at the engine
  level; the HTTP tests cover their status codes, not a full funded round trip.
- Position sector/asset class is recorded from the discovery row or market
  instrument metadata. When neither exposes it, the position says `unknown`
  and no concentration score is invented.
- A partially applied proposal is not simulated by forcing a mid-apply ledger
  failure; the `FAILED` gate is tested directly by marking a proposal failed and
  proving `apply` refuses it.
- Performance ratios are exercised only in their gated state; there is no
  historical series in this phase to produce a real Sharpe ratio.
- No fees or taxes exist, so returns are gross.
- Everything is simulated: no order, broker, network or real money is involved.
