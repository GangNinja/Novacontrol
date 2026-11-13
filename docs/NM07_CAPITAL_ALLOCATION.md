# NM-07 — Capital Allocation

## One wallet per portfolio

A portfolio is funded by moving NovaCoin out of an existing NM-03 wallet:

```
funding wallet ──transfer(pf-fund-<id>)──▶ pfw_<portfolio id>
```

The portfolio's cash lives in its own wallet, so every purchase, sale, valuation
and release is a ledger fact rather than a number in a table. The portfolio
record stores `funding_wallet_id` and `wallet_id`; neither can be changed after
creation.

## The market clearing wallet

Buys move cash from the portfolio wallet to one shared simulated wallet,
`pfw_market_clearing`. Sells move it back. This keeps the ledger honest without
inventing a counterparty:

- buying does not destroy NovaCoin, it parks it in the clearing account;
- selling does not create NovaCoin, it returns it from the clearing account;
- realized profit accumulates in the clearing account, which is exactly what a
  market counterparty's book would show.

Because every unit sits in exactly one wallet, total NC is conserved:

```
funding + Σ portfolio wallets + clearing = constant
```

`PortfolioEngine.reconcile()` checks the portfolio's own book against its
capital moves and realized history, and `GET /market/wallet/reconcile` covers
the whole ledger.

## No double allocation

Creation takes capital only once:

- the deterministic portfolio id makes repeated creation a replay;
- the transfer carries the idempotency key `pf-fund-<id>`, so even a retried
  request cannot move the money twice;
- the funding wallet is checked before a portfolio record exists, so an
  under-funded request leaves nothing behind;
- the opening allocation is proposed and applied once; a replay returns the
  stored portfolio rather than buying again.

Two portfolios may share a funding wallet; the second transfer simply fails if
the first consumed the balance. That refusal is the only correct outcome: a
wallet balance is never allowed to go negative.

## Sizing the commitment

Capital is explicit in the create request and recorded as `initial_capital`.
The engine does not infer a "suitable" amount from the wallet balance. A
portfolio can never spend more than its wallet holds, because every trade is a
ledger transfer and the cash reserve is checked before the first entry.

## Closing

`POST /market/portfolios/{id}/close` sells every open position at the latest
timestamped price, returns the proceeds to the portfolio wallet, transfers the
remaining cash back to the funding wallet and records the reason. Capital is
therefore released, not stranded. A close is idempotent: closing a closed
portfolio replays the stored record.

## What this is not

- No leverage, no margin, no borrowing.
- No fees or taxes in this phase (`fee_bps = 0`), so performance is gross.
- No currency conversion: 1 NC = ₹1 exactly, and every amount is `Decimal`.
- No real money and no broker: NM-10 owns execution.
