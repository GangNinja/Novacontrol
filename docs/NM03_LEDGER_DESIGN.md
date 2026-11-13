# NM-03 — NovaCoin Deterministic Ledger Design

**Scope:** the accounting model behind `novamarket/wallet/`. The architecture,
storage and API are in `docs/NM03_ARCHITECTURE.md` and
`docs/NM03_API_CONTRACTS.md`.

**Accounting unit:** NovaCoin (NC), **1 NC = ₹1 INR exactly**. Currency code is
always `INR`; NC is the unit name, not a second currency.

---

## 1. Accounting model

The ledger is **double-entry**: every money movement is one *transaction* that
posts two or more *entries*, and the entries of one transaction must sum to
zero. Each entry belongs to exactly one wallet.

| Direction | Signed amount | Effect on that wallet |
| --- | --- | --- |
| `credit` | `+amount` | balance **increases** |
| `debit` | `−amount` | balance **decreases** |

Consequences:

* A wallet's balance equals the sum of **its own** signed entries. A reader can
  always derive a balance from history, so a stored balance is a cache that can
  be proven wrong (and reconciliation does prove it).
* An account only "owns" its own leg. A deposit therefore shows **one** entry in
  the user's wallet (a credit of 100.00), not two — the debit lives in the
  issuance account. Tests assert entry counts per wallet for this reason.
* Nothing can appear from nowhere: the counter-leg always exists, so
  `SUM(signed_amount_paise) = 0` across the whole ledger.

## 2. The issuance account

`novamarket:issuance` (`owner = SYSTEM_OWNER`, `system = 1`, fixed wallet id)
is the reserved counter-party for money entering and leaving the simulated
economy:

| Operation | User wallet leg | Issuance leg |
| --- | --- | --- |
| deposit | `credit +amount` | `debit −amount` |
| withdrawal | `debit −amount` | `credit +amount` |
| transfer | sender `debit`, receiver `credit` | none |

Properties:

* It is excluded from the user-wallet listing (`system = 1` filtered out).
* Its balance is normally **negative** — it represents NC issued rather than
  held. The schema allows a negative balance only for `system = 1`
  (`CHECK (balance_paise >= 0 OR system = 1)`); a user wallet can never go
  negative.
* It cannot be deposited to, transferred to, or reversed, and its status is not
  API-managed. `test_the_issuance_account_is_reserved` and
  `test_the_issuance_wallet_status_is_not_api_managed` pin this.
* Because it takes every opposite leg, the whole ledger still sums to zero.

## 3. Schema

### 3.1 `wallet_transactions` — one row per movement

| Column | Type | Notes |
| --- | --- | --- |
| `transaction_id` | TEXT PK | `txn_…` |
| `transaction_type` | TEXT | `deposit`, `withdrawal`, `transfer`, `reversal`, `adjustment` |
| `status` | TEXT | `posted`, `reversed` — **derived** from a reversal referencing this row (see §9) |
| `wallet_id` | TEXT FK | the subject wallet (sender, for a transfer) |
| `counterparty_wallet_id` | TEXT FK NULL | receiver, for a transfer |
| `amount` / `amount_paise` | TEXT / INTEGER | display text + exact paise |
| `currency` | TEXT | `INR` only (`CHECK`) |
| `reference`, `description` | TEXT | free text, default `''` |
| `idempotency_key` | TEXT | `''` when unused; partial unique index when set |
| `reverses_transaction_id` | TEXT FK NULL | set on a reversal |
| `entry_count` | INTEGER | `>= 2` (`CHECK`) |
| `created_at` | TEXT | ISO-8601 UTC |
| `CHECK (amount_paise > 0)` | | a transaction is never zero or negative |
| `CHECK (status IN ('posted','reversed'))` | | closed vocabulary |

### 3.2 `ledger_entries` — the immutable record

| Column | Notes |
| --- | --- |
| `entry_id` TEXT PK | `ent_…` |
| `transaction_id` TEXT FK | which transaction posted it |
| `wallet_id` TEXT FK | whose balance it changes |
| `entry_type` | same vocabulary as the transaction type |
| `direction` | `debit` \| `credit` |
| `amount` / `amount_paise` | positive magnitude |
| `signed_amount` / `signed_amount_paise` | the directioned value |
| `balance_after` / `balance_after_paise` | the wallet's balance immediately after this entry |
| `currency` | `INR` |
| `sequence` | position within the transaction, unique per transaction |
| `description`, `reference`, `created_at` | audit context |

Constraints that make the entry self-consistent, enforced by SQLite:

```sql
CHECK (amount_paise > 0)
CHECK ((direction = 'debit'  AND signed_amount_paise = -amount_paise)
    OR (direction = 'credit' AND signed_amount_paise =  amount_paise))
CHECK (entry_type IN ('deposit','withdrawal','transfer','reversal','adjustment'))
UNIQUE (transaction_id, sequence)
```

A corrupted direction can therefore not be written even by buggy code, and two
legs can never share a sequence number.

### 3.3 Append-only enforcement

Four triggers abort the operation outright:

```sql
BEFORE UPDATE ON ledger_entries        -> 'ledger entries are append-only; post a reversal instead'
BEFORE DELETE ON ledger_entries        -> '... history is never deleted'
BEFORE UPDATE ON wallet_transactions   -> 'wallet transactions are append-only; ...'
BEFORE DELETE ON wallet_transactions   -> '... history is never deleted'
```

`RAISE(ABORT)` makes the statement fail inside the caller's transaction, so even
a `DELETE FROM ledger_entries` typed into a SQLite shell is refused. Pinned by
`test_ledger_entries_are_append_only_at_the_database_level`, which drives the
raw SQL, not the API.

`wallet_idempotency` and `wallet_meta` are **not** append-only: the first is
written once and read forever, the second is a version key.

## 4. Worked examples

All amounts in NC (= ₹, exactly).

**Deposit 100.00 into `wal_A`**

```
txn_1  deposit  wallet=wal_A          100.00   2 entries
  ent_1  wal_A         credit  +100.00  balance_after 100.00
  ent_2  issuance      debit   -100.00  balance_after -100.00
```

`wal_A` gains one visible entry; its balance is 100.00; the ledger sum is 0.

**Withdraw 25.00 from `wal_A`**

```
txn_2  withdrawal  wallet=wal_A       25.00    2 entries
  ent_3  wal_A         debit    -25.00  balance_after  75.00
  ent_4  issuance      credit   +25.00  balance_after -75.00
```

**Transfer 40.00 from `wal_A` to `wal_B`**

```
txn_3  transfer  wallet=wal_A  counterparty=wal_B   40.00   2 entries
  ent_5  wal_A  debit   -40.00  balance_after 35.00
  ent_6  wal_B  credit  +40.00  balance_after 40.00
```

`wal_A` = 35.00, `wal_B` = 40.00. Total across wallets is unchanged, and no
issuance leg exists because nothing entered or left the economy.

**Reverse `txn_3` with reason `"wrong recipient"`**

```
txn_4  reversal  reverses=txn_3   40.00   2 entries
  ent_7  wal_A  credit  +40.00  balance_after 75.00
  ent_8  wal_B  debit   -40.00  balance_after  0.00
```

`txn_3` reports `status = "reversed"` and
`reversed_by_transaction_id = "txn_4"`, yet its row was never modified: both
facts come from `txn_4`'s `reverses_transaction_id`. `wal_A` is back to 75.00;
`wal_B` is back to 0.00; ten entries exist in total and the sum is still 0.

## 5. Balance semantics

* `wallets.balance` (text) and `wallets.balance_paise` (integer) are the current
  balance. `available == total`; `reserved` is always `"0.00"` because NM-03
  holds no funds on anyone's behalf.
* `balance_after` on each entry records the wallet's balance at that instant, so
  a statement can be replayed without re-summing.
* Balances are written **only** inside the posting transaction, together with
  the entries that justify them. There is no setter method and no endpoint.
* Sequence numbers are per transaction (`1..n`), which is what makes
  `ux_ledger_entries_sequence` meaningful and keeps leg order stable.

## 6. Invariants and where each is enforced

| Invariant | Enforced by |
| --- | --- |
| One currency (`INR`) | `CHECK` on `wallets`, `wallet_transactions`, `ledger_entries` |
| Amounts positive | `CHECK (amount_paise > 0)` on both money tables; `parse_amount` in Python |
| Entry direction matches the sign | `CHECK` on `signed_amount_paise` |
| Entries of a transaction sum to zero | `WalletStore.post` refuses an unbalanced `PostingRequest` **before** writing; `reconcile()` re-derives it from stored rows |
| No negative user balance | `CHECK (balance_paise >= 0 OR system = 1)` + the in-transaction balance check |
| No double spend | `BEGIN IMMEDIATE` + a balance check inside the write transaction |
| History is immutable | Four append-only triggers |
| Stored balance equals the entries | `_verify_balance` before commit; `reconcile()` independently |
| One key never posts twice | `UNIQUE` index on the idempotency key + fingerprint comparison |
| A reversal reverses a real, unreversed transaction | `WalletEngine.reverse` validations |

Two independent implementations are deliberate: the store's `_verify_balance`
guards the write, and `reconcile()` re-derives everything from stored rows and
never trusts the store's own arithmetic. A bug in one is caught by the other.

## 7. Idempotency

```
wallet_idempotency(key TEXT PRIMARY KEY, transaction_id TEXT, fingerprint TEXT, created_at)
```

* The key is supplied by the caller (`idempotency_key`); an empty key means "no
  idempotency" and posts nothing into the table (the partial unique index skips
  `''`).
* The **fingerprint** is `request_fingerprint({...})` — a SHA-256 over the
  operation, wallet id(s), canonical amount text and reference. Same key + same
  fingerprint → the original `PostingResult` is returned with `replayed: true`,
  **no** new entries, **no** new events. Same key + different fingerprint →
  `IdempotencyConflictError` (HTTP 409), because silently replaying a different
  request would be a wrong answer.
* The lookup and the insert happen **inside** the same write transaction as the
  posting, so two concurrent duplicates cannot both miss the lookup: the second
  either sees the row or loses the `BEGIN IMMEDIATE` race and then sees it.
* The key is also looked up **before** wallet state is consulted
  (`WalletStore.replay`, called by the engine ahead of the membership/status
  gate), so a retry is answered with the original transaction after the wallet
  was frozen or closed. Nothing moves and no event is published.
* Persistence is the table, not a process dictionary — a restart replays
  correctly (`test_a_repeated_request_with_one_idempotency_key_credits_once`
  drives it over HTTP, `identical_idempotent_posting_returns_the_original` at
  the store).

## 8. Atomicity and write ordering

`WalletStore.post()` performs, inside one `BEGIN IMMEDIATE` … `COMMIT`:

1. idempotency lookup (replay or conflict short-circuit);
2. load the affected wallets (`SELECT … ` for update under the write lock);
3. validate status and sufficiency of every debit leg;
4. insert the `wallet_transactions` row;
5. insert every `ledger_entries` row with its `sequence` and `balance_after`;
6. update each wallet's balance (text + paise + `updated_at`);
7. insert the `wallet_idempotency` row;
8. `_verify_balance` for each affected wallet — recompute from the entries
   written in this transaction and compare with the balance just written;
9. `COMMIT`; on any exception, `ROLLBACK`.

Because validation happens *after* the write lock is taken, "check then write"
is not a race. Because events are published after `COMMIT` by the engine, no
listener can observe a movement that later rolls back. Because a failure raises
before `_after_commit`, a rolled-back transfer emits only
`wallet.transaction_failed`.

## 9. Reversals, corrections, adjustments

History is never edited; a correction is new history.

A reversal (`POST /market/wallet/reversals`, `WalletEngine.reverse`):

* requires a non-blank `reason` — an unauditable correction is refused;
* loads the original transaction and mirrors **every** leg (credit ↔ debit,
  same amount, same wallets) as a new `reversal` transaction with
  `reverses_transaction_id`;
* refuses to reverse a `reversal` (a reversal of a reversal is a new movement
  pretending to be an undo) and refuses a transaction that is already reversed
  (`test_a_reversal_is_exposed_and_keeps_the_original`);
* respects wallet status: a frozen or closed wallet cannot receive the mirror
  legs;
* is itself idempotent — the reversal carries its own key, and replaying it
  returns the same transaction;
* leaves the original row byte-identical. `status = "reversed"` and
  `reversed_by_transaction_id` are **derived at read time** by a correlated
  subquery over reversal rows, precisely because the append-only trigger forbids
  updating `status`.

`adjustment` exists in the vocabulary for a future credit/debit correction that
is not the mirror of an existing transaction; NM-03 exposes no route that posts
one, and no code path can create it implicitly.

## 10. Reconciliation

`reconcile(wallet_id=None)` recomputes independently and **reports**; it never
repairs, and it has no write path.

For one wallet:

1. `stored_balance` — the wallet row;
2. `ledger_balance` — `SUM(signed_amount_paise)` of that wallet's entries;
3. `difference = stored − ledger`; any non-zero difference is a
   `balance_mismatch`;
4. per transaction: `SUM(signed_amount_paise) != 0` →
   `unbalanced_transaction`; `COUNT(entries) != entry_count` →
   `entry_count_mismatch`; entries referencing a missing transaction →
   `orphan_transaction`; the same `(transaction_id, sequence)` twice →
   `duplicate_entry`;
5. a negative derived balance → `negative_balance`.

For the whole ledger (`wallet_id=None`) it additionally checks
`SUM(signed_amount_paise)` over **all** entries, which must be zero
(`ledger_not_zero_sum`), and refuses to run past
`MAX_RECONCILIATION_ENTRIES = 200_000` entries, reporting `ledger_too_large`
rather than silently sampling.

| Issue code | Meaning |
| --- | --- |
| `ledger_not_zero_sum` | the whole ledger does not sum to zero |
| `unbalanced_transaction` | one transaction's entries do not sum to zero |
| `entry_count_mismatch` | entries present ≠ `entry_count` recorded |
| `orphan_transaction` | an entry points at a transaction that does not exist |
| `duplicate_entry` | the same leg was written twice |
| `balance_mismatch` | stored balance ≠ ledger-derived balance |
| `negative_balance` | a wallet's derived balance is negative |
| `ledger_too_large` | the ledger exceeds the reconciliation cap |

The report carries `wallet_id`, `currency`, `stored_balance`, `ledger_balance`,
`difference`, `ok`, `issues[]` (each with `code`, `detail`, `wallet_id`,
`transaction_id`, `evidence`), `ledger_sum`, `wallets_checked` and the entry
count examined. `ok` is false when any issue exists, and **the stored balance is
left exactly as found** — the tamper tests assert both the detection *and* that
nothing was restored, because a reconciler that silently rewrites a balance
destroys the evidence of what went wrong.

Reconciliation is also reachable as the automatic in-transaction check (§8), so
every posting proves its own arithmetic before commit; the on-demand report is
the deeper, after-the-fact audit.

## 11. Precision and rounding policy

| Rule | Behaviour |
| --- | --- |
| Accepted input types | `Decimal`, integer, numeric `str`; anything else (notably `float`) is refused |
| Non-finite | `NaN`, `±inf` refused |
| Scale | at most **2** decimals; NC has no sub-paisa unit |
| `1.005`, `0.001`, `12.345` | **refused** with a message naming the paisa limit — never rounded, because rounding moves money the user did not ask to move |
| `0.00`, negatives | refused |
| Magnitude | `> MAX_AMOUNT` (1,000,000,000,000 NC) refused |
| Storage | decimal **text** for display + **integer paise** for arithmetic (`amount_paise`), so sums are exact and never accumulate float error |
| Formatting | `amount_text` renders a plain `-?\d+\.\d{2}` string with `ROUND_HALF_EVEN` applied only to already-representable values |
| Currency | always `INR`; `Money` carries the currency so a future multi-currency design cannot silently add rupees to NC |

1 NC = 1 INR is enforced by the value type's currency and stated once in the
contract (`exchange_rate: "1 NC = 1 INR"`); there is no conversion table to
drift.

## 12. Failure semantics

| Situation | Ledger state after the call | What the caller sees |
| --- | --- | --- |
| validation error (amount, owner, target) | unchanged — nothing written | 422 |
| insufficient funds | unchanged | 409 |
| wallet frozen/closed | unchanged | 409 |
| idempotent replay | unchanged — original returned | 200/201 with the original transaction |
| idempotency conflict | unchanged | 409 |
| post-write verification failure | rolled back | 500, `LedgerIntegrityError` |
| event publication failure | committed and correct | success; `last_event_error` recorded |
| reconciliation finds issues | unchanged, untouched | 200 with `ok: false` + issues |

The asymmetry is intentional: money movements are all-or-nothing, while the
event bus and the reconciler are reporting channels that may fail without
endangering correctness.

## 13. Rules for later phases

Do:

* fund a wallet with `deposit`, return funds with `withdraw`, move NC with
  `transfer`;
* pass a stable business id (order id, fill id, run id) as `idempotency_key` so
  a retry cannot double-charge;
* read balances from `wallets`/`wallet()` and history from `transactions()` /
  `entries()`;
* correct with `reverse(transaction_id, reason=...)` and a human-readable reason.

Don't:

* write `wallets.balance*` directly, or `INSERT` into `ledger_entries` /
  `wallet_transactions` outside `post()`;
* `UPDATE` or `DELETE` those tables — the triggers abort;
* treat a reconciliation failure as something to auto-fix;
* add a second money type, a third decimal place, or a float path.

## 14. Deliberately absent

No reserved balance/holds, no fees, no interest, no credit lines, no
multi-currency, no settlement, no external rails, no portfolio valuation, no
order/fill model. NM-03 is exactly the ledger the later market phases need and
nothing they have not asked for yet.
