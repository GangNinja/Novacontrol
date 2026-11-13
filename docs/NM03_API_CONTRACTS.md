# NM-03 — NovaCoin Wallet & Ledger API Contracts

All routes are authenticated (`require_auth`, the same dependency NM-01/NM-02
use), are declared in `novacontrol/api/models.py` (`ApiSurface`) and
`novacontrol/api/route_consumers.py`, and appear in the generated
`docs/API.md`. Every response is a `to_dict()` projection of a value type —
internal dataclasses are never returned raw.

Base path: `/market`. Money is a **decimal string** (never a JSON float), and
`nova_coin` is the same number as `currency: "INR"` at **1 NC = ₹1**.

---

## 1. Route index

| # | Method | Path | Purpose |
| --- | --- | --- | --- |
| 1 | GET | `/market/wallet/status` | ledger summary, schema version, last reconciliation |
| 2 | GET | `/market/wallets` | list wallets |
| 3 | POST | `/market/wallets` | create a wallet (zero balance) |
| 4 | POST | `/market/wallets/transfer` | transfer between wallets |
| 5 | GET | `/market/wallets/{wallet_id}` | one wallet + entry/transaction counts |
| 6 | GET | `/market/wallets/{wallet_id}/transactions` | transaction history |
| 7 | GET | `/market/wallets/{wallet_id}/ledger` | ledger entries |
| 8 | POST | `/market/wallets/{wallet_id}/deposit` | deposit from issuance |
| 9 | POST | `/market/wallets/{wallet_id}/withdraw` | withdraw to issuance |
| 10 | POST | `/market/wallets/{wallet_id}/status` | freeze / close |
| 11 | GET | `/market/wallets/{wallet_id}/reconcile` | reconcile one wallet |
| 12 | POST | `/market/wallet/reconcile` | reconcile every wallet |
| 13 | POST | `/market/wallet/reversals` | reverse a posted transaction |

## 2. Value shapes

### 2.1 Wallet

```json
{
  "wallet_id": "wal_7f3c…",
  "owner": "operator",
  "label": "research",
  "currency": "INR",
  "status": "active",
  "system": false,
  "balance": {
    "available": "125.50",
    "reserved": "0.00",
    "total": "125.50",
    "currency": "INR",
    "nova_coin": "125.50"
  },
  "created_at": "2026-11-02T09:14:03.113000+00:00",
  "updated_at": "2026-11-02T09:20:41.882000+00:00"
}
```

`available == total` and `reserved` is always `"0.00"`: NM-03 holds no funds on
anyone's behalf, so a reserve would be an unsupported claim.

### 2.2 Ledger entry (immutable)

```json
{
  "entry_id": "ent_1a2b…",
  "transaction_id": "txn_9c8d…",
  "wallet_id": "wal_7f3c…",
  "entry_type": "deposit",
  "direction": "credit",
  "amount": "100.00",
  "signed_amount": "100.00",
  "balance_after": "100.00",
  "sequence": 1,
  "currency": "INR",
  "nova_coin": "100.00",
  "description": "initial funding",
  "reference": "seed-1",
  "created_at": "2026-11-02T09:15:11.004000+00:00"
}
```

`direction` is `debit` (negative `signed_amount`) or `credit` (positive).
A debit that arrived as a positive number is impossible: the database `CHECK`
enforces the sign.

### 2.3 Transaction

```json
{
  "transaction_id": "txn_9c8d…",
  "transaction_type": "deposit",
  "status": "posted",
  "wallet_id": "wal_7f3c…",
  "counterparty_wallet_id": null,
  "amount": "100.00",
  "currency": "INR",
  "nova_coin": "100.00",
  "reference": "seed-1",
  "description": "initial funding",
  "idempotency_key": "seed-1",
  "reverses_transaction_id": null,
  "reversed_by_transaction_id": null,
  "entry_count": 2,
  "created_at": "2026-11-02T09:15:11.004000+00:00"
}
```

`status` and `reversed_by_transaction_id` are **derived** from reversal rows at
read time; the original row is never rewritten (append-only triggers forbid it).

### 2.4 Operation result (deposit / withdraw / transfer / reversal)

```json
{
  "ok": true,
  "replayed": false,
  "transaction": { …§2.3… },
  "entries": [ { …§2.2… }, { …§2.2… } ],
  "wallets": { "wal_7f3c…": { …§2.1… } }
}
```

`replayed: true` means an idempotency key matched the original request: the
original transaction and wallet states are returned, nothing new was posted, no
event was emitted, and the response is not a second movement. The key is checked
**before** wallet state, so a retry is answered with the original even if the
wallet has been frozen or closed since the commit (a replay from a different
`owner` is refused).

### 2.5 Reconciliation report

```json
{
  "wallet_id": "wal_7f3c…",
  "currency": "INR",
  "stored_balance": "125.50",
  "ledger_balance": "125.50",
  "difference": "0.00",
  "ok": true,
  "integrity_status": "ok",
  "issues": [],
  "entries_checked": 4,
  "transactions_checked": 3,
  "wallets_checked": 1,
  "ledger_sum": "0.00",
  "nova_coin": { "ledger_balance": "125.50", "difference": "0.00" },
  "checked_at": "2026-11-02T09:21:02.551000+00:00"
}
```

`wallet_id` is the reconciled wallet's id, or `""` for a whole-ledger
reconcile, where `wallets_checked` counts every wallet. `ok: false` comes with
`integrity_status: "violation"` and one or more issues:

```json
{ "code": "balance_mismatch",
  "detail": "stored balance 999.00 differs from the ledger 25.00",
  "wallet_id": "wal_7f3c…",
  "transaction_id": "",
  "evidence": { "stored": "999.00", "ledger": "25.00" } }
```

Issue codes: `ledger_not_zero_sum`, `unbalanced_transaction`,
`entry_count_mismatch`, `orphan_transaction`, `duplicate_entry`,
`balance_mismatch`, `negative_balance`, `ledger_too_large`.

---

## 3. Routes

### 3.1 `GET /market/wallet/status`

No parameters. Returns the engine's runtime status: `initialised`, repository
`path`, a `summary` of the ledger (wallet/entry/transaction counts, total NC
held), `system_wallet_id`, `nova_coin_per_inr: "1"`, `precision: "0.01"`,
`currency: "INR"`, the number of `reconciliations` run, and
`last_reconciliation` (a §2.5 report or `null`).

### 3.2 `GET /market/wallets`

| Query | Type | Default | Notes |
| --- | --- | --- | --- |
| `owner` | string | — | filter by owner |
| `limit` | int | 100 | 1…500 |
| `offset` | int | 0 | |

```json
{ "ok": true,
  "wallets": [ { …§2.1… } ],
  "total": 3,
  "limit": 100,
  "offset": 0,
  "currency": "INR" }
```

The issuance account is never listed (`system = true` is filtered out).

### 3.3 `POST /market/wallets`

```json
{ "owner": "operator", "label": "research", "wallet_id": null }
```

`owner` is required and must be non-blank; `wallet_id` is generated when
omitted. Returns `{ "ok": true, "wallet": { …§2.1… } }` with `balance.total =
"0.00"` — there is no way to open a wallet holding money.

Errors: `422` blank owner / malformed owner; `409` duplicate `wallet_id`.

### 3.4 `POST /market/wallets/transfer`

```json
{ "from_wallet_id": "wal_A",
  "to_wallet_id": "wal_B",
  "amount": "40.00",
  "idempotency_key": "trade-9",
  "reference": "order-1",
  "description": "settlement",
  "owner": "operator" }
```

`amount` accepts a decimal string or integer (never a float). Returns a §2.4
result whose `entries` are exactly two (sender debit, receiver credit) and whose
`wallets` maps both wallets' post-transfer state.

Errors: `404` unknown wallet; `409` insufficient funds / frozen or closed wallet;
`422` self-transfer, issuance as a party, unrepresentable precision, `owner`
mismatch, non-positive amount; `409` idempotency conflict.

### 3.5 `GET /market/wallets/{wallet_id}`

```json
{ "ok": true, "wallet": { …§2.1… }, "ledger_entries": 4, "transactions": 3 }
```

`404` for an unknown wallet. The issuance account is readable here and reports
`system: true`, so an audit can see the negative side of the NC it issued; it is
never listed by `GET /market/wallets` and its status cannot be changed through
the API.

### 3.6 `GET /market/wallets/{wallet_id}/transactions`

Query: `limit` (default 100, max 500), `offset` (0), `ascending` (false — newest
first). Returns `{ ok, transactions, total, limit, offset, currency }`. The list
includes transfers **received** by the wallet (matched on
`counterparty_wallet_id`), not only those it sent.

### 3.7 `GET /market/wallets/{wallet_id}/ledger`

Query: `limit` (100, max 500), `offset` (0). Returns
`{ ok, entries, total, limit, offset, currency }` in ascending ledger order by
default — a statement reads forward.

### 3.8 `POST /market/wallets/{wallet_id}/deposit`

```json
{ "amount": "100.00", "idempotency_key": "seed-1",
  "reference": "seed-1", "description": "initial funding",
  "owner": "operator" }
```

Credits the wallet from the issuance account. The wallet sees **one** entry (a
credit of 100.00); the issuance debit is not part of the caller's wallet history.

Errors: `404`, `409` (frozen/closed), `422` (amount), `409` idempotency conflict.

### 3.9 `POST /market/wallets/{wallet_id}/withdraw`

Same body. Debits the wallet back to issuance. `409` when funds are
insufficient; no entries are written in that case.

### 3.10 `POST /market/wallets/{wallet_id}/status`

```json
{ "status": "frozen", "reason": "manual review" }
```

`status` ∈ `active|frozen|closed`. Returns `{ ok, wallet, reason }`.

* `closed` requires a **zero** balance (`409` otherwise);
* the issuance account's status is refused (`409`, not API-managed);
* an unknown status value is `422`.

### 3.11 `GET /market/wallets/{wallet_id}/reconcile`

Recomputes that wallet's balance from its entries and returns a §2.5 report.
Always `200` — a detected problem is the answer, not an HTTP error. Nothing is
repaired.

### 3.12 `POST /market/wallet/reconcile`

Reconciles every wallet **and** the whole-ledger zero sum. Returns a §2.5 report
with `wallets_checked` set and one issue per problem found. Refuses (as an
issue, not a crash) past 200,000 entries.

### 3.13 `POST /market/wallet/reversals`

```json
{ "transaction_id": "txn_9c8d…",
  "reason": "wrong recipient",
  "idempotency_key": "rev-1",
  "owner": "operator" }
```

Posts mirrored entries as a new `reversal` transaction and returns a §2.4
result. `reason` is required — a blank reason is `422`. The original
transaction is untouched; it reports `status: "reversed"` and
`reversed_by_transaction_id` from then on.

Errors: `404` unknown transaction; `409` already reversed, reversed-by-a-
reversal, reversal of a reversal, wallet frozen/closed; `422` blank reason or
`owner` mismatch.

---

## 4. Error contract

All wallet routes map failures through one function (`_wallet_http_error`):

| Status | Conditions |
| --- | --- |
| `401` | missing/invalid token (shared auth dependency, unchanged) |
| `404` | unknown wallet or transaction |
| `409` | insufficient funds; frozen or closed wallet; duplicate `wallet_id`; double reversal; idempotency conflict; wallet subsystem not running |
| `422` | blank/invalid owner; amount not a positive 2-decimal decimal; float/NaN/inf; self-transfer; issuance as target; blank reversal reason; bad status value |
| `500` | post-write ledger verification failure (`LedgerIntegrityError`); unexpected error |

The error body is FastAPI's `{"detail": "<message>"}`. Messages are written for
a human ("amount 1.005 is finer than one paisa (0.01); NC carries 2 decimals")
and never include a stack trace or internal path.

**Availability:** the wallet routes are `409` while the NovaMarket subsystem is
disabled, matching NM-01's lifecycle gate. A test disables the subsystem
explicitly (`POST /market/disable`) before asserting it, because the application
enables NovaMarket at boot.

## 5. Contract publication

`GET /market/contracts` includes the NM-03 section verbatim from
`wallet_contract()`, so a client can discover the accounting rules instead of
hard-coding them:

```json
{ "implemented": true,
  "phase": "NM-03",
  "accounting_unit": "NovaCoin",
  "currency": "INR",
  "exchange_rate": "1 NC = 1 INR",
  "precision": { "decimals": 2, "smallest_unit": "0.01 NC",
                 "rounding": "ROUND_HALF_EVEN",
                 "unrepresentable_amounts": "refused, never silently rounded",
                 "binary_floats": "refused at construction" },
  "convention": { "credit": "increases the wallet balance",
                  "debit": "decreases the wallet balance",
                  "balance": "stored balance equals the sum of the wallet's signed entries",
                  "double_entry": "every transaction's entries sum to zero",
                  "issuance_account": "novamarket:issuance carries the negative side of deposits and withdrawals" },
  "append_only": { "tables": ["ledger_entries", "wallet_transactions"],
                   "enforced_by": "SQLite triggers that abort UPDATE and DELETE",
                   "corrections": "a reversal transaction with mirrored entries and a required reason" },
  "idempotency": { "storage": "wallet_idempotency table, primary key on the key",
                   "reuse": "same key + same fingerprint returns the original result",
                   "conflict": "same key + different fingerprint is refused" },
  "atomicity": "balance updates, entries, transaction and idempotency record commit in one BEGIN IMMEDIATE transaction",
  "reconciliation": "stored balance vs SUM(signed entries), per-transaction zero sum and whole-ledger zero sum",
  "tables": ["wallets", "wallet_transactions", "ledger_entries",
             "wallet_idempotency", "wallet_meta"],
  "limits": [ …three limitation strings… ] }
```

## 6. Events

Published after commit, never on rollback, and never on a replay:

| Event | Payload |
| --- | --- |
| `novamarket.wallet.created` | `wallet_id`, `owner` |
| `novamarket.wallet.balance_updated` | `wallet_id`, `balance` |
| `novamarket.wallet.deposit_completed` | `wallet_id`, `transaction_id`, `amount` |
| `novamarket.wallet.withdrawal_completed` | `wallet_id`, `transaction_id`, `amount` |
| `novamarket.wallet.transfer_completed` | `from_wallet_id`, `to_wallet_id`, `transaction_id`, `amount` |
| `novamarket.wallet.transaction_failed` | `operation`, `reason` |
| `novamarket.ledger.entry_created` | `entry_id`, `transaction_id`, `wallet_id` |
| `novamarket.ledger.reconciliation_completed` | `wallets_checked`, `ok` |
| `novamarket.ledger.integrity_violation` | `wallet_id`, `code` |

Payloads are validated by the bus against `EVENT_PAYLOAD_FIELDS`; a field the
vocabulary does not declare is refused rather than published silently.
