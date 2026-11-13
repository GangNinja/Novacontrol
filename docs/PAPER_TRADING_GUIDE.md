# Paper Trading Guide

This guide explains the complete simulated trading lifecycle in the accepted NM-01–NM-14 baseline. It is the practical companion to [User Guide](USER_GUIDE.md) and [Architecture](ARCHITECTURE.md).

> **This is not brokerage.** NovaMarket has no broker client, no live-order route and no real-money trading. `GET /market/execution/status` reports `paper_only: true`, `live_trading: false` and `broker_connected: false`. A paper order is simulated. A paper fill is not an exchange fill.

## Contents

1. [Paper trading in one minute](#1-paper-trading-in-one-minute)
2. [NovaCoin and the wallet](#2-novacoin-and-the-wallet)
3. [Portfolio funding](#3-portfolio-funding)
4. [From strategy to proposal](#4-from-strategy-to-proposal)
5. [Risk evaluation and approval](#5-risk-evaluation-and-approval)
6. [Submitting a paper order](#6-submitting-a-paper-order)
7. [Order states](#7-order-states)
8. [Fills, partial fills and expiry](#8-fills-partial-fills-and-expiry)
9. [Pricing, fees and slippage](#9-pricing-fees-and-slippage)
10. [Positions and ledger effects](#10-positions-and-ledger-effects)
11. [Cancellation, rejection and reconciliation](#11-cancellation-rejection-and-reconciliation)
12. [Backtesting](#12-backtesting)
13. [Shadow mode](#13-shadow-mode)
14. [Evidence and adaptive learning](#14-evidence-and-adaptive-learning)
15. [Market data versus simulated execution](#15-market-data-versus-simulated-execution)
16. [Model-derived IV and provider-reported IV](#16-model-derived-iv-and-provider-reported-iv)
17. [Verification checklist](#17-verification-checklist)

## 1. Paper trading in one minute

```text
Wallet deposit (simulated NC)
        ↓
Funded portfolio (explicit ledger movement)
        ↓
Strategy signal → risk-gated proposal → approval
        ↓
Paper order (simulated) → partial/full fill → fees/slippage
        ↓
Positions + wallet ledger + reconciliation
```

There is no arrow to a broker. The only execution implementation in this build is the paper engine.

## 2. NovaCoin and the wallet

NovaCoin (NC) is the application's simulated accounting unit. It is not a cryptocurrency and cannot be bought, sold, deposited or withdrawn outside NovaMarket. **1 NC = ₹1** for paper accounting. The wallet, portfolio cash and paper fees use the same unit and ledger authority.

Rules:

- A wallet opens at **zero**.
- A **deposit** is a simulated ledger posting from the issuance/offset account; it is not a bank transfer.
- A **withdrawal** moves simulated NC back to the issuance account, subject to available balance.
- A **transfer** between wallets is one atomic, balanced transaction. NC is conserved.
- Wallet amounts use exact decimals, at most 2 decimal places for operations. Floats are not accepted as ledger authority.
- Every mutation should carry an idempotency key. Retrying the same request with the same key must not duplicate the posting.
- Entries are append-only. A correction is a reversal with mirrored entries, never a deletion or an edit.
- Closing a wallet requires a zero balance; a wallet can also be frozen where supported.
- Reconciliation recomputes balances from entries and reports integrity. It does not repair evidence.

Use the UI's **NovaCoin Wallet → Deposit/Withdraw** or the API:

```powershell
$deposit = @{
  amount = "100000.00"
  reference = "initial-paper-capital"
  description = "Simulated deposit"
  idempotency_key = "deposit-2026-11-09-001"
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri `
  "http://127.0.0.1:8011/market/wallets/<WALLET_ID>/deposit" `
  -ContentType "application/json" -Body $deposit
```

Do not interpret a successful deposit response as a real payment.

## 3. Portfolio funding

A portfolio is a **book** with a horizon:

| UI | Stored value |
|---|---|
| Short-Term Portfolio | `short_term` |
| Medium-Term Portfolio | `medium_term` |
| Long-Term Portfolio | `long_term` |

Creation and funding are one validated operation:

```json
{
  "horizon": "short_term",
  "funding_wallet_id": "<WALLET_ID>",
  "capital": "50000.00",
  "name": "Short-term paper book"
}
```

The backend checks the horizon, decimal amount and wallet funds, then posts the ledger movement and records the portfolio. **Capital is never implied**: an unfunded or insufficiently funded book cannot silently borrow money.

Portfolio statuses: `initializing`, `active`, `under_review`, `rebalancing`, `paused`, `suspended`, `closed`. Pausing/resuming does not delete holdings or history. Closing liquidates according to the implemented policy and releases capital through the ledger.

### Portfolio rebalance proposals

A portfolio **rebalance proposal** is different from a strategy proposal. It is a versioned set of target intents with status `validated`, `applied`, `rejected`, `expired`, `superseded` or `failed`.

- **Propose rebalance** validates the proposal against the current book.
- **Review** shows what would change; **Keep unapplied** changes nothing.
- **Apply proposal** applies a validated intent.
- A proposal is not a broker order and does not connect to execution automatically.

## 4. From strategy to proposal

A strategy signal is not an order. The implemented path is:

1. a strategy from the catalogue is created/configured;
2. **Run strategies** evaluates active strategies over stored data for a funded portfolio;
3. actionable signals become **strategy proposals**;
4. each proposal is sent to the independent risk engine;
5. a proposal can be used by paper execution only after a current approval and while unexpired.

Proposal statuses: `created`, `pending_risk`, `approved`, `rejected`, `restricted`, `expired`, `cancelled`, `forwarded_to_execution`.

A proposal contains the instrument, side, quantity/intent, portfolio, strategy, risk decision/expiry and evidence. Read those fields; do not infer approval from a green UI badge or from the word "proposal".

A **portfolio rebalance proposal** is not a strategy proposal and should not be submitted as one.

## 5. Risk evaluation and approval

Risk is independent. It receives the intent, portfolio state, market snapshot and a **versioned policy**, then returns:

| Outcome | Meaning for paper trading |
|---|---|
| `approved` | Eligible to proceed if unexpired and still current |
| `restricted` | Only the permitted/reduced scope may proceed; read the conditions |
| `rejected` | Do not submit; the violated rule is explained |
| `requires_review` | Not executable until the stated review is resolved |
| `insufficient_data` | Cannot decide from stale/missing evidence; do not guess |

The paper executor accepts **only an already-approved proposal with a live risk decision**. This is checked at submission, not merely remembered from when the proposal was created. An approval is bound to the proposal it assessed; it is not a permanent permission to trade any instrument.

The UI's risk page can show combined exposure, decisions, alerts, policies and hypothetical stress results. Alerts can be acknowledged/resolved, but that does not approve a trades. Risk **never** places, amends or cancels an order.

If risk says `insufficient_data`, fix the data/freshness or wait; do not bypass the gate or hand-edit a decision row.

## 6. Submitting a paper order

Use **Paper Trading → Submit from proposal**.

Inputs:

- an approved, unexpired **proposal**;
- optional quantity override;
- order type: `market`, `limit`, `stop`, `stop_limit`;
- limit price for `limit`/`stop_limit`;
- trigger price for `stop`/`stop_limit`;
- optional expiry (the backend defaults to the proposal/TTL where allowed).

The form refuses submission until required fields are available. The API accepts the same shape:

```json
{
  "proposal_id": "<PROPOSAL_ID>",
  "quantity": "10",
  "order_type": "limit",
  "limit_price": "2450.50",
  "trigger_price": null,
  "expires_at": "2026-11-10T09:15:00Z",
  "idempotency_key": "paper-order-2026-11-09-001"
}
```

`execute` defaults to true for the API's paper submission path. It means "simulate now under the paper engine", not "send live".

What the backend does:

1. validates proposal, quantity, order type and conditional prices;
2. re-checks risk approval/eligibility;
3. creates a paper order and reservation;
4. prices the simulation from available evidence;
5. applies slippage and fee components;
6. records fill(s), positions and ledger cash movements;
7. stores reconciliation evidence.
8. never opens a broker connection.

The UI labels the result as simulated. If the order is rejected, read the structured reason; it may be a risk, quantity, reservation, data-freshness or liquidity issue.

## 7. Order states

The accepted lifecycle is:

| Status | Meaning |
|---|---|
| `created` | Order record exists; not yet validated/accepted |
| `pending_validation` | Awaiting validation/reservation checks |
| `accepted` | Passed initial checks; may become working |
| `open` | Working; can be processed, partially filled or cancelled |
| `partially_filled` | Some quantity filled; remainder may remain working |
| `filled` | All quantity filled; terminal success |
| `cancel_pending` | Cancellation recorded; completion may still be settling |
| `cancelled` | Remaining quantity cancelled; completed fills preserved |
| `rejected` | Refused; no fill for the refused portion |
| `expired` | Expiry reached; no further fill |
| `failed` | Processing failed; inspect evidence/status |

Order types and states are reported by `GET /market/execution/status` and `/market/execution/contracts`. Transitions are audited; do not rewrite statuses in the database.

## 8. Fills, partial fills and expiry

- A **fill** is a simulated execution record with quantity, price, slippage, fee breakdown, net cash and timestamp.
- A market order can fill immediately if fresh data and liquidity are available; otherwise it can be rejected/left unfilled rather than given an invented price.
- A limit/stop can wait as `open` or be partially filled. **Process** asks the engine to re-evaluate an eligible order against fresh data.
- A partial fill updates positions and ledger only for the filled quantity. The unfilled reservation remains until filled, cancelled or expired.
- **Cancel** preserves completed fills, releases the unfilled reservation and records a reason.
- Expiry prevents further fills. The proposal/order expiry is part of the evidence; do not extend it by editing a row.
- A fill price is the paper engine's simulated price after its explicit assumptions. It is not a quote you could necessarily execute at.

### Order status vs proposal status

Do not conflate them. A proposal can be `approved` while the order is `rejected` for a data/liquidity/reservation reason; a proposal can expire while a previously created order has its own state. Read both records.

## 9. Pricing, fees and slippage

The execution configuration is versioned (`nm10-exec-v1` in the accepted baseline) and recorded on orders/fills.

### Liquidity and price assumptions

| Component | Default |
|---|---:|
| Participation rate | 10% of reference-session volume |
| Spread | 8 bps synthetic |
| Impact coefficient | 12 bps square-root |
| Volatility weight | 0.35 |
| Maximum slippage | 150 bps |
| Stale-quote limit | 300 s |
| Require fresh data | true |
| Bar fallback | false |
| Default order TTL | 86,400 s |

The synthetic spread is an approximation because NovaMarket has no consolidated book. It is not a fabricated bid/ask claim.

### Illustrative fee schedule

| Component | Default |
|---|---:|
| Brokerage | 3 bps |
| Exchange | 0.297 bps |
| SEBI | 0.01 bps |
| Stamp duty | 0.015 bps |
| GST | 18% |
| Minimum total fee | 0 |
| Effective date metadata | 2026-01-01 |

The fee disclaimer states these are illustrative Indian cash-equity simulation assumptions and must be verified against current tariffs. Fees are not a broker quote. The actual fill response is authoritative for the breakdown applied to that fill.

### Why your fill differs from the last price

The paper engine can apply spread, impact, volatility and slippage, subject to its cap and available quantity. A partial fill can occur before the full quantity. Check `slippage_amount`, `total_fees`, `net_cash` and the execution config version.

## 10. Positions and ledger effects

For a simulated buy, the intended accounting is:

- wallet/portfolio cash decreases by gross notional + fees;
- position quantity/cost increases;
- a fill and ledger evidence are written;
- reservation is consumed/released appropriately.

For a simulated sell:

- quantity decreases and cash increases by gross proceeds − fees;
- realised P&L is recorded under the implemented cost policy;
- fees/slippage are recorded.

The exact sign conventions and fee incidence are those returned by the backend. Do not recalculate authority in the browser, a spreadsheet, or a second service. Read the fill and ledger entries together.

**Ledger rule:** every wallet balance is a projection of immutable entries. A paper order's cash effect must reconcile with the ledger. If it does not, the reconciliation record is evidence of a problem, not something to hide.

## 11. Cancellation, rejection and reconciliation

### Cancel

Use **Cancel** on an open/partially filled order. The request carries a reason where exposed. Completed fills remain; unfilled quantity is cancelled; reservation is released. A terminal order cannot be "uncancelled"; create a new order from a valid proposal if appropriate.

### Rejection

A rejection can come from:

- missing/expired/incorrect proposal approval;
- invalid quantity/price/expiry;
- insufficient wallet/portfolio funds;
- unavailable/stale quote or insufficient liquidity;
- a risk restriction;
- an operational/lifecycle error.

Read the order's structured rejection/evidence. Do not retry blindly with the same idempotency key unless it is the same intended operation.

### Reconciliation

**Paper Trading → Reconciliation → Reconcile now**, or `POST /market/execution/reconcile`.

It compares simulated order effects with wallet ledger entries and reports differences. It does **not** repair, delete or silently adjust evidence. A discrepancy blocks a claim of correct accounting until understood. Also use wallet reconciliation for the whole ledger; the two checks are related but not interchangeable.

## 12. Backtesting

A backtest is not a paper trade. It:

- replays stored historical bars point-in-time;
- uses the strategy configuration and risk policy snapshot stored on the run;
- applies explicit fees/slippage;
- produces metrics, trades, orders, decisions, signals, data-quality findings and a reproducibility hash;
- supports sensitivity/walk-forward as explicit variants;
- never creates a paper order, wallet entry or portfolio mutation.

Create from **Backtesting → New backtest** (name, strategy, instruments, historical window, policy), then **Create and start**, or use `POST /market/backtests`.

Read total return, max drawdown, Sharpe, trades, equity curve, assumptions and data quality. A positive historical result is not a promise; a missing/insufficient dataset is a limitation, not a zero return.

## 13. Shadow mode

Shadow mode answers: "what would the strategy have decided?" It does **not** execute.

- Sessions have a portfolio baseline, instruments and at least one strategy.
- Lifecycle: `created`, `running`, `paused`, `stopped`, `completed`, `cancelled`, `failed`.
- It records decisions, projections, hypothetical outcomes (MFE/MAE), comparisons, errors and health.
- `GET /market/shadow/status` reports `real_money: false`, `execution_connected: false` and `nm13_activated: false`.
- No wallet, ledger, portfolio or capital reservation is touched.

Do not add shadow projections to portfolio value or treat them as paper fills. Small samples are explicitly handled; a shadow result is not proof of profitability.

## 14. Evidence and adaptive learning

Learning consumes evidence from backtest/paper/shadow, runs bounded experiments and produces candidates. A candidate must pass evaluation, risk compatibility, shadow validation where required, and governance approval before promotion. Promotion creates an immutable version; rollback is recorded.

Learning status guarantees:

- `real_money: false`;
- `execution_connected: false`;
- `executes_trades: false`;
- `isolated: true`.

Learning cannot execute an order, mutate a wallet/ledger/portfolio or bypass risk. It is not an NM-15 feature and does not add live trading.

## 15. Market data versus simulated execution

| Question | Answer |
|---|---|
| Where does a price come from? | A stored provider/file observation or bar with source and timestamp |
| Is a paper price guaranteed executable? | No; it is a simulation assumption with spread/impact/slippage |
| Does a market quote create an order? | No |
| Does paper execution contact an exchange? | No |
| Can a paper fill be a real fill? | No |
| Does a broker account exist? | No |
| Is there a live-order path? | No |

A market-data provider can be unavailable or delayed. That does not enable live trading; it should produce a stale/unavailable state, rejection or `insufficient_data`, never a guessed fill.

## 16. Model-derived IV and provider-reported IV

For the official F&O snapshot, NovaMarket calculates **implied volatility with Black-Scholes** from option prices/settlement data. This is a model value.

- It is **not** an exchange/provider-reported IV.
- It depends on the model, rate/dividend assumptions and the chosen option row.
- It must not be compared as if it were a vendor IV without checking methodology.
- No provider-reported IV feed is implemented in the accepted baseline.
- The snapshot is EOD for its trade date, not a live option chain.

When you see IV, ask: model or provider? date/time? underlying/strike/expiry? If those are not shown, do not treat it as current.

## 17. Verification checklist

Before considering a paper trade complete:

- [ ] Wallet deposit/transfer is in the ledger, not just in the UI.
- [ ] Portfolio funding matches the wallet movement.
- [ ] Proposal is approved and unexpired; risk decision cites its policy version.
- [ ] Order is labelled paper; no broker is connected.
- [ ] Order type/quantity/prices match the intent.
- [ ] Fill price includes the disclosed simulation assumptions; fees/slippage match the fill response.
- [ ] Position/cash effects reconcile with immutable ledger entries.
- [ ] A partial fill's unfilled quantity/reservation is understood.
- [ ] Cancellation/expiry leaves completed fills intact.
- [ ] Reconciliation difference is zero or explained.
- [ ] Backtest/shadow values are kept out of current portfolio value.
- [ ] No screenshot contains a token, password, personal data or real financial account.

If any item is false, stop and diagnose. Do not bypass risk, alter the ledger directly or present a simulated result as a real market fill.

For setup and error resolution, see [Installation](INSTALLATION.md) and [Troubleshooting](TROUBLESHOOTING.md).
