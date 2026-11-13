# NovaMarket NM-03 — NovaCoin Wallet & Deterministic Ledger Architecture

**Status:** implemented and verified (see `docs/NM03_TEST_REPORT.md` for the
evidence, `docs/NM03_LEDGER_DESIGN.md` for the accounting rules and
`docs/NM03_API_CONTRACTS.md` for the wire contract).
**Phase:** `NM-03` (`IMPLEMENTED_PHASE`), contract version `1.0.0` unchanged.
**Accounting unit:** NovaCoin (NC). **1 NC = ₹1 INR**, exactly, everywhere.

NM-03 adds an internal, simulated financial subsystem: wallets, an append-only
double-entry ledger, atomic and idempotent money movements, and an independent
reconciliation. It introduces **no** real-money rail, no bank, broker or
cryptocurrency integration, and no market feature (event detection, discovery,
forecasting, portfolios, strategies, execution, backtesting all remain later
phases).

---

## 1. What NM-01 and NM-02 already provided (and what NM-03 reuses)

NM-03 was built **into** the existing subsystem rather than beside it. Nothing
below was duplicated:

| Established by | Used by NM-03 for |
| --- | --- |
| NM-01 lifecycle (`initialize → enable → disable → shutdown`) | Wallets only exist while the subsystem is `running`; disable closes the ledger cleanly |
| NM-01 event bus + typed vocabulary (`core/events.py`) | Every wallet/ledger event; payload validation through `EVENT_PAYLOAD_FIELDS` |
| NM-01 `ApiSurface` + `ROUTE_CONSUMERS` + generated `docs/API.md` | The 13 wallet routes are declared in the same two registries, so the reference cannot drift |
| NM-01 contracts endpoint (`GET /market/contracts`) | `wallet_contract()` publishes the NM-03 contract as data |
| NM-01 `PermissionManager` + `MarketPermissionModel` | `market.wallet.read`, `market.wallet.move`, `market.ledger.correct` |
| NM-01 capability registry | Four new capability rows (`market.wallet.read`, `market.wallet.move`, `market.ledger.reconcile`, `market.ledger.correct`) |
| NM-02 durable store (`novamarket.sqlite3`, SQLite) | **The same database file and the same engine** — no second database |
| NM-02 append-only discipline | The same enforcement style: SQLite triggers that abort UPDATE/DELETE |

`open_wallet_store(data_dir, path=…)` opens `novamarket.sqlite3` in the
subsystem's data directory, creating it the way NM-02 does. A caller may pass an
explicit `wallet_path` (tests do) but the production path is the NM-02 file.

## 2. Component map

```
src/novacontrol/novamarket/wallet/
├── models.py    value types + money rules (no I/O)
│   ├── WalletStatus, EntryDirection, EntryType, TransactionType, TransactionStatus
│   ├── Wallet, LedgerEntry, WalletTransaction, ReconciliationIssue, ReconciliationReport
│   ├── LedgerLeg, PostingRequest, PostingResult
│   └── parse_amount, amount_text, inr, request_fingerprint, PAISE, MAX_AMOUNT, SYSTEM_OWNER
├── store.py     the durable repository (SQLite, one connection, one lock)
│   ├── schema DDL + append-only triggers + WALLET_SCHEMA_VERSION
│   ├── wallets: create_wallet, wallet, wallets, set_status, ensure_system_wallet
│   ├── post(PostingRequest) -> PostingResult   (one BEGIN IMMEDIATE)
│   ├── reads: transactions, entries, counts, summary, transaction
│   └── reconcile(wallet_id=None) -> ReconciliationReport
└── engine.py    the service layer (validation, ownership, idempotency, events)
    ├── WalletEngine: create_wallet, set_wallet_status, deposit, withdraw,
    │                 transfer, reverse, transactions, entries, reconcile, status
    └── EventPublisher protocol  +  WalletOperationResult
```

Wiring:

* `novamarket/service.py` owns the engine's lifetime (`_ensure_wallet`), exposes
  `wallet_status()`, delegates the twelve operations, and answers
  `wallet_contract()`. `status()["wallet"]` and `contracts()["wallet"]` surface
  it; the subsystem's own `applications` list gains the wallet tables.
* `novacontrol/api/app.py` holds the 13 handlers and one nested error mapper
  (`_wallet_http_error`) that names every status code in one place.
* `novacontrol/api/models.py` declares the 13 `ApiRoute` rows and
  `api/route_consumers.py` maps each to `no-render` (no web panel owns them; the
  API is the contract), and `scripts/generate_api_reference.py` regenerates
  `docs/API.md` from those two registries.

The layering is strict and one-directional:

```
API handler → NovaMarketService → WalletEngine → WalletStore → SQLite
                                     │
                                     └── EventPublisher (the NM-01 bus)
```

Nothing above the store writes a balance. There is no "set balance" method
anywhere: `wallets.balance`/`balance_paise` are written only inside the same
transaction that writes the entries justifying them.

## 3. Storage architecture

### 3.1 One database, two schema owners

`novamarket.sqlite3` already belongs to NM-02, which versions itself with
`PRAGMA user_version`. NM-03 therefore does **not** touch that pragma; it keeps
its own version key in a `wallet_meta` table (`schema_version = 1`).

Consequences, all pinned by tests:

* A database created by NM-02 alone is upgraded in place the first time the
  wallet store opens it — the wallet tables are additive, and NM-02's
  `user_version` is still `1` afterwards
  (`test_the_ledger_shares_the_market_data_database_without_clashing`).
* A wallet database whose `schema_version` is *newer* than this build is
  refused with `WalletStoreError` instead of being read optimistically, and the
  connection is released so a later migration is not blocked.
* Nothing in NM-02's tables is altered.

### 3.2 Tables

| Table | Purpose | Append-only |
| --- | --- | --- |
| `wallet_meta` | schema version key | no (metadata) |
| `wallets` | one row per wallet: owner, currency, status, `system` flag, balance (text) + `balance_paise` (integer), timestamps | balance + status only, always with its entries |
| `wallet_transactions` | one row per money movement: type, status, wallet, counterparty, amount, reference, description, idempotency key, `reverses_transaction_id`, `entry_count` | **yes** (triggers) |
| `ledger_entries` | the immutable entries: direction, amount, signed amount, `balance_after`, sequence | **yes** (triggers) |
| `wallet_idempotency` | one row per idempotency key → transaction + request fingerprint | no (written once) |

Indexes: owner lookup, per-wallet history (ascending/descending), counterparty
lookup, idempotency uniqueness (partial unique index on the non-empty key),
reversal lookup, and a unique `(transaction_id, sequence)` so two legs of one
transaction can never collide.

Integrity is enforced at three levels, deliberately: `CHECK` constraints in the
DDL (currency, enums, positive amounts, `signed_amount_paise` = ±`amount_paise`,
`entry_count >= 2`), foreign keys with `PRAGMA foreign_keys = ON`, and the
store's own post-write verification inside the transaction.

## 4. Request lifecycle

The phase's lifecycle diagram maps onto real code, not documentation:

| Stage | Where it happens |
| --- | --- |
| **REQUEST** | API handler → `NovaMarketService.wallet_*` → `WalletEngine.deposit/withdraw/transfer/reverse` |
| **VALIDATION** | `parse_amount` (type, finiteness, precision, magnitude, positivity) and argument checks in the engine |
| **IDEMPOTENCY CHECK** | `store.post` looks up `wallet_idempotency` **inside** the write transaction; same key + same fingerprint returns the original `PostingResult(replayed=True)` |
| **BALANCE / OWNERSHIP CHECK** | `_operable_wallet` (exists → owner match when an owner is asserted → status is `active`), then the balance check inside the transaction |
| **ATOMIC LEDGER WRITE** | `store.post`: new transaction row → entries → wallet balance updates → idempotency row → commit |
| **BALANCE RECONCILIATION** | `store._verify_balance` recomputes the affected wallets from the entries it just wrote, before commit; `reconcile()` is the independent, on-demand version |
| **SUCCESS / FAILURE** | Commit → events; any failure → rollback → `wallet.transaction_failed` and a typed exception |

## 5. Concurrency model

* One connection per store, guarded by a re-entrant lock, with
  `check_same_thread=False` (the FastAPI app is async and the engine calls the
  store through `asyncio.to_thread`).
* Every write begins `BEGIN IMMEDIATE`, so SQLite takes the write lock **before**
  any balance is read. Validation therefore happens inside the write
  transaction: two concurrent withdrawals cannot both observe the same balance
  and both succeed.
* `busy_timeout = 5000`, WAL journal, `synchronous = FULL` — durability in
  exchange for throughput, which is the correct trade for a ledger.
* `test_concurrent_withdrawals_never_overdraw_a_wallet` drives N threads on one
  store and asserts exactly one succeeds and the balance never goes negative.

There is no in-memory balance cache anywhere, so a second process (a test, a
CLI, a future service) sees the same ledger through the same constraints.

## 6. Money and precision

* Every amount is a `Decimal` wrapped in the NM-01 `Money` value type with
  currency `INR`; **no float ever reaches the store** — `parse_amount` refuses
  `float`, `NaN`, `inf`, zero, negatives and magnitudes above `MAX_AMOUNT`
  (1,000,000,000,000 NC).
* NC carries two decimals. `PAISE = 0.01`; an amount that cannot be represented
  in paise (for example `1.005`) is **refused**, never rounded.
* Each amount is stored twice: decimal text for display and exact integer paise
  for arithmetic, so `SUM()` in SQL is exact and the invariants are checkable in
  the database rather than merely asserted in Python.
* Rounding never happens silently: `ROUND_HALF_EVEN` is used only when a value
  is already representable (formatting), and the refused-precision path is
  pinned by tests.

Full rules and worked examples: `docs/NM03_LEDGER_DESIGN.md`.

## 7. Account model

* A wallet has `wallet_id` (stable, `wal_…` by default, caller-supplied ids
  allowed), `owner`, `label`, `currency = INR`, `status`
  (`active|frozen|closed`), timestamps, and a balance triple
  (`available`, `reserved`, `total`).
* **No reserved balance is implemented.** NM-03 holds no funds on anyone's
  behalf, so `available == total` and `reserved` is reported as `"0.00"`. The
  field exists because the report shape needs it; introducing a non-zero
  reserve would be an accounting claim with nothing behind it.
* One wallet is reserved: the **issuance account**
  (`owner = novamarket:issuance`, `system = 1`). It carries the negative side of
  deposits and withdrawals so the whole ledger sums to zero. It is not listed as
  a user wallet, cannot be targeted by a deposit, transfer or reversal, and its
  status is not API-managed.
* Ownership: when a request asserts `owner`, it must match the wallet's owner;
  a mismatch is a validation error. Wallet status gates money — `frozen`
  refuses movements, `closed` refuses everything and requires a zero balance
  first.
* Wallets open at zero. There is no API that opens a wallet with money: the only
  way in is a ledger-backed deposit.

## 8. Events

Nine events were promoted into `EventType`/`EVENT_PAYLOAD_FIELDS` **before**
any was published, and into `IMPLEMENTED_EVENTS`:

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

Rules:

* **Published after commit only.** `_after_commit` runs after `store.post`
  returns, i.e. after `COMMIT`; a failure inside `post` raises before any event,
  and a `WalletError` path publishes only `wallet.transaction_failed`.
* **A replay publishes nothing new.** An idempotent replay returns the original
  result with `replayed: true` and emits no money event — a duplicate request is
  not a second movement.
* **Publishing never breaks a transaction.** `_publish` records
  `last_event_error` and continues; the money has already moved and the response
  is the truth.
* Reconciliation publishes the completion event, plus one
  `integrity_violation` event per detected issue.

## 9. API surface

13 routes, all under `/market/`, all authenticated by the same
`require_auth` dependency NM-01/NM-02 use:

| Method & path | Purpose |
| --- | --- |
| `GET /market/wallet/status` | ledger summary, schema version, last reconciliation |
| `GET /market/wallets` | list wallets (`owner`, `limit`, `offset`) |
| `POST /market/wallets` | create a wallet (always zero balance) |
| `POST /market/wallets/transfer` | atomic transfer between two wallets |
| `GET /market/wallets/{wallet_id}` | one wallet + entry/transaction counts |
| `GET /market/wallets/{wallet_id}/transactions` | paged transaction history |
| `GET /market/wallets/{wallet_id}/ledger` | paged immutable entries |
| `POST /market/wallets/{wallet_id}/deposit` | deposit from issuance |
| `POST /market/wallets/{wallet_id}/withdraw` | withdraw back to issuance |
| `POST /market/wallets/{wallet_id}/status` | freeze / close |
| `GET /market/wallets/{wallet_id}/reconcile` | reconcile one wallet |
| `POST /market/wallet/reconcile` | reconcile every wallet |
| `POST /market/wallet/reversals` | reverse a posted transaction |

One error mapper maps exception → status: not found `404`; insufficient funds,
conflict, inactive wallet, not-operational `409`; validation `422`; ledger
integrity `500`; other wallet errors `400`. Internal models are never returned
raw — every response is a `to_dict()` projection (see
`docs/NM03_API_CONTRACTS.md`).

## 10. Permissions and safety boundaries

`MarketPermissionModel` gained three operations, all **granted** in this build
because they move simulated NC only:

| Operation | Risk | Confirmation | Reversible |
| --- | --- | --- | --- |
| `market.wallet.read` | LOW | no | — |
| `market.wallet.move` | MEDIUM, `filesystem:write` | no | yes (by reversal) |
| `market.ledger.correct` | HIGH, `filesystem:write` | yes | no (posts new entries) |

`market.trade.live` stays refused, and `market.trade.paper`,
`market.portfolio.simulate` and `market.config.sensitive.read` stay un-granted —
NM-03 implemented none of them.

Safety properties, each with a test:

* no direct balance mutation endpoint exists;
* every movement is ledger-backed and balanced;
* `wallets`/`ledger_entries`/`wallet_transactions` cannot be edited or deleted
  (triggers), so history cannot be rewritten even by a bug;
* a correction is a new reversal transaction with a required reason; a reversal
  of a reversal is refused;
* a frozen or closed wallet refuses money, and closing a funded wallet is
  refused;
* the issuance account cannot be used as a deposit/transfer/reversal target.

## 11. Interfaces for later phases (NM-04 … NM-14)

The wallet is usable without knowing the store exists:

```python
engine = nova.novamarket.wallet            # WalletEngine | None
engine.deposit(wallet_id, "500.00", idempotency_key="order-123")
engine.withdraw(wallet_id, "25.00")
engine.transfer(seller, buyer, "120.50", idempotency_key="trade-9")
engine.transactions(wallet_id=..., limit=50)
engine.entries(wallet_id=..., transaction_id=...)
engine.reconcile(wallet_id)                # never repairs
```

Designed-for reuse:

* **NM-10 paper execution** can fund a portfolio wallet with a deposit and post
  each fill as a transfer, using the idempotency key as the order id — a retried
  order cannot double-charge.
* **NM-07/08/09** read balances (`available == total`) and history; nothing needs
  a write path of its own.
* **NM-12/13** can reconcile before and after a run and compare reports, because
  `reconcile()` returns data (`ok`, issues, differences) rather than raising.

Deliberate absences (so later phases don't inherit a premature design): no
portfolio, order, position, fee or valuation concept; no multi-currency; no
interest or credit; no reserved balance; no wallet-level limits or quotas.

## 12. Failure modes

| Failure | Behaviour |
| --- | --- |
| Subsystem disabled | `409`: the ledger is not open; nothing is created |
| Unknown wallet | `WalletNotFoundError` → `404` |
| `owner` mismatch | `WalletValidationError` → `422` |
| frozen / closed wallet | `WalletNotActiveError` → `409` |
| insufficient funds | `InsufficientFundsError` → `409`, no entries written |
| unrepresentable precision, float, NaN, ≤ 0, > max | `WalletValidationError` → `422` |
| self-transfer, issuance as target | `WalletValidationError` → `422` |
| idempotency key reused with a different payload | `IdempotencyConflictError` → `409` |
| replay of a committed movement against a frozen/closed wallet | the original result is returned (`replayed: true`); nothing is written and no event is published |
| replay by a different `owner` | `WalletValidationError` → `422` |
| reversal without a reason / of a reversal / of an already reversed txn | `409` |
| post-write balance verification fails | `LedgerIntegrityError` → `500`, transaction rolled back |
| reconciliation finds issues | report returned (`ok: false`), **nothing repaired**, `integrity_violation` events published |
| newer wallet schema | `WalletStoreError` at open; the store refuses to read it |

## 13. Known limitations

* No real-money rail of any kind; deposits and withdrawals are simulated moves
  against the issuance account.
* No reserved balance, no holds, no fees, no interest, no credit.
* The ledger is single-file SQLite: correct and durable for one host, not a
  distributed ledger. Concurrency safety is per-file and per-process.
* A reversal marks nothing on the original row (history is append-only); the
  "reversed" state is derived from the reversal transaction, so a reader must
  ask the store rather than read one row.
* Reconciliation is on demand (plus the per-write verification); there is no
  background integrity sweep.
* No UI: NM-03 is a headless subsystem by scope.
* No NovaMarket workspace integration (NM-14).

## 14. Verification

`docs/NM03_TEST_REPORT.md` records the commands, counts, and the per-criterion
evidence for the 19 acceptance criteria. `tests/test_novamarket_wallet.py` is the
NM-03 suite (76 tests, 22 subtests); NM-01/NM-02 suites and the shared
application suites were re-run as regressions.
