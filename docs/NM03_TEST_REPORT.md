# NM-03 — Acceptance & Test Report

**Phase:** NM-03 — NovaCoin Wallet & Deterministic Ledger
**Verdict:** **PASS** — every criterion below is demonstrated by a passing test
in this build. Nothing in this report is projected or assumed: each row cites the
test that produced the evidence, and the commands and counts in §2 are the actual
output of the final run.

- **Date:** 2026-11-02
- **Host:** Windows (the development machine), Python 3.13 venv at `../.venv`
- **Working directory:** `novamarket/` (the NovaMarket worktree)
- **Code under test:** `src/novacontrol/novamarket/wallet/` (models, store,
  engine), the service wiring, the 13 API routes, the event vocabulary and the
  permission/capability tables.

---

## 1. What NM-03 delivers

| Area | Delivered |
| --- | --- |
| Wallets | create, list, read, freeze/close, owner-scoped access, zero opening balance |
| Ledger | append-only double-entry entries with per-entry `balance_after`, exact integer paise |
| Movements | deposit, withdrawal, transfer, reversal — all atomic and balanced |
| Money | `Decimal` only, 2 decimals, 1 NC = ₹1, unrepresentable amounts refused |
| Idempotency | persisted key + request fingerprint, replay-safe, conflict-refusing |
| Concurrency | `BEGIN IMMEDIATE` + in-transaction balance checks, single writer lock |
| Reconciliation | independent recomputation, 8 issue codes, reports only, never repairs |
| Events | 9 typed events, published after commit only, none on rollback or replay |
| Logs | structured request / committed / replayed / failed / reconciled / integrity records |
| API | 13 authenticated routes, one error mapper, contract published under `/market/contracts` |
| Docs | `NM03_ARCHITECTURE.md`, `NM03_LEDGER_DESIGN.md`, `NM03_API_CONTRACTS.md`, this report, roadmap updated |

## 2. Commands and results

Run from `novamarket/` with `PYTHONPATH=src` so the worktree's package is
imported (the main repo also has an editable install).

| # | Command | Result |
| --- | --- | --- |
| 1 | `python -m pytest tests/test_novamarket_wallet.py -q` | **76 passed**, 22 subtests passed, exit 0 (6.27 s) |
| 2 | `python -m pytest tests/test_novamarket.py tests/test_novamarket_api.py tests/test_novamarket_marketdata.py -q` | **144 passed, 2 skipped**, 88 subtests passed, exit 0 (36.80 s) |
| 3 | `python -m pytest tests/test_web_api.py tests/test_optimization.py -q` | **150 passed**, 77 subtests passed, exit 0 (133.30 s) |
| 4 | `python -m mypy src/novacontrol/novamarket src/novacontrol/api` | **Success: no issues found in 34 source files**, exit 0 |
| 5 | `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync`, exit 0 (400 routes, 13 wallet routes present) |

`python` above is `../.venv/Scripts/python.exe`. Per the standing instruction the
full suite was **not** run; the NM-03 suite, the NM-01/NM-02 suites and the two
shared application suites that pin the route registry, event payloads and
permission grants were run, and all of them pass.

### 2.1 NM-03 suite composition

`tests/test_novamarket_wallet.py` (76 tests, 22 subtests):

| Class | Tests | Covers |
| --- | --- | --- |
| `PrecisionTests` | 8 | money rules: Decimal-only, floats/NaN/inf refused, 2-decimal scale, zero/negative, magnitude, 1 NC = ₹1, formatting |
| `WalletStoreTests` | 10 | wallet creation, ids, ownership, reserved issuance account, balance shape, status transitions |
| `LedgerPostingTests` | 9 | deposit/transfer/withdraw posting, balanced legs, insufficient funds, unbalanced request refused, frozen wallet, idempotency replay/conflict, append-only at the SQL level, concurrent withdrawals |
| `PersistenceTests` | 5 | reopen durability, schema version, newer schema refused, shared NM-02 database, data-directory path |
| `ReconciliationTests` | 5 | clean ledger, tampered balance, orphan transaction, duplicate entry, unbalanced transaction |
| `WalletEngineTests` | 17 | the asynchronous service paths and event timing |
| `PhaseContractTests` | 4 | implemented phase, event payload declarations, permission grants, reversal declared as a correction |
| `WalletApiTests` | 6 | full HTTP lifecycle, error statuses, idempotent HTTP request, reversal over HTTP, contract body, disabled subsystem |
| `WalletLoggingTests` | 5 | request/committed/replayed/failed/reconciled/integrity log records |
| `LedgerGateAndReplayTests` | 7 | retry answered before wallet-state gating, spent-deposit reversal refused, applied page limit reported, schema version in status, issuance readability, owner-scoped replay |

## 3. Acceptance criteria

Each criterion is stated as the requirement it satisfies and the test that
proves it in this build.

| # | Criterion | Evidence (all passing) |
| --- | --- | --- |
| 1 | **1 NC = ₹1 INR exactly** | `PrecisionTests::test_nova_coin_is_the_same_number_as_rupees`; `wallet_contract()["exchange_rate"] == "1 NC = 1 INR"`; `WalletApiTests::test_the_contract_reports_the_nm03_ledger` |
| 2 | **Decimal only, never binary floats** | `PrecisionTests::test_amounts_are_decimal_and_quantized_to_paise`, `::test_a_binary_float_is_refused`, `::test_non_finite_values_are_refused`; `CHECK` + integer paise columns in the schema |
| 3 | **Precision/rounding policy defined and documented** | `PrecisionTests::test_an_unrepresentable_amount_is_refused_not_rounded`; §11 of `NM03_LEDGER_DESIGN.md`; `wallet_contract()["precision"]` |
| 4 | **Append-only ledger** | `LedgerPostingTests::test_ledger_entries_are_append_only_at_the_database_level` drives raw `UPDATE`/`DELETE` and asserts the abort; four triggers in `store.py` |
| 5 | **Corrections only as reversal/adjustment entries** | `WalletEngineTests` reversal paths, `WalletApiTests::test_a_reversal_is_exposed_and_keeps_the_original`, `PhaseContractTests::test_a_reversal_is_declared_as_a_confirmed_correction` |
| 6 | **Atomic transfers** | `LedgerPostingTests::test_a_transfer_posts_one_debit_and_one_credit`, `::test_an_insufficient_transfer_changes_nothing`; one `BEGIN IMMEDIATE` per posting |
| 7 | **Balanced debit/credit entries** | `LedgerPostingTests::test_a_deposit_credits_the_wallet_and_balances_the_issuance_account`, `::test_an_unbalanced_posting_is_refused`; `ReconciliationTests::test_a_clean_ledger_reconciles_and_sums_to_zero` |
| 8 | **Deterministic balances** | `LedgerPostingTests` balance assertions + `_verify_balance` before commit; `ReconciliationTests::test_a_tampered_stored_balance_is_detected_and_preserved` proves the ledger is the authority |
| 9 | **No double spend** | `LedgerPostingTests::test_concurrent_withdrawals_never_overdraw_a_wallet` (threads, one winner, no negative balance); `::test_an_insufficient_transfer_changes_nothing` |
| 10 | **No negative balance** | `CHECK (balance_paise >= 0 OR system = 1)`; the concurrency test; `WalletStoreTests::test_a_frozen_wallet_refuses_money_and_a_zero_balance_wallet_closes` |
| 11 | **Persisted, not memory-only, idempotency** | `LedgerPostingTests::identical_idempotent_posting_returns_the_original`, `::reusing_a_key_for_a_different_amount_is_refused`, `WalletApiTests::test_a_repeated_request_with_one_idempotency_key_credits_once`; `wallet_idempotency` table survives `PersistenceTests`' reopen |
| 12 | **Concurrency-safe** | `test_concurrent_withdrawals_never_overdraw_a_wallet`; `busy_timeout` + WAL + `BEGIN IMMEDIATE` + one connection lock (§5 of the architecture doc) |
| 13 | **Reconciliation recomputes independently and only reports** | `ReconciliationTests` (5 tests, including three tamper scenarios that assert the report *and* that nothing was repaired) |
| 14 | **Events only after commit, none on rollback** | `WalletEngineTests` event-timing assertions; `_after_commit` runs after `store.post` returns; a failed movement publishes only `wallet.transaction_failed`; a replay publishes none |
| 15 | **Structured logs for requests, failures, reconciliations, integrity violations** | `WalletLoggingTests` (5 tests): `novamarket.wallet.request`, `.committed`, `.replayed`, `.failed`, `novamarket.ledger.reconciled`, `novamarket.ledger.integrity_violation` with key=value fields |
| 16 | **Reuse NM-01/NM-02 without breaking them** | Suites 2 and 3 above (144 passed / 2 skipped, 150 passed); the ledger shares NM-02's `novamarket.sqlite3` (`PersistenceTests::test_the_ledger_shares_the_market_data_database_without_clashing`); one event bus, one route registry, one permission layer |
| 17 | **Documentation delivered** | `docs/NM03_ARCHITECTURE.md`, `docs/NM03_LEDGER_DESIGN.md`, `docs/NM03_API_CONTRACTS.md`, `docs/NM03_TEST_REPORT.md`, `README.md` pointer, `docs/NOVAMARKET.md` roadmap/status |
| 18 | **Roadmap updated; nothing later started** | `docs/NOVAMARKET.md` marks NM-03 **Implemented** and appends an NM-03 readiness section; NM-04…NM-13 remain unstarted, `IMPLEMENTED_PHASE == "NM-03"` (`PhaseContractTests::the_build_reports_nm03_as_the_implemented_phase`) |
| 19 | **Out-of-scope untouched** | No event-detection, discovery, forecasting, portfolio, strategy, paper-execution, backtesting, shadow-mode, adaptive-learning or UI code exists in the diff; `market.trade.live` still refused; no real-money rail (contract `limits`) |

## 4. Defects found and fixed during verification

1. **Refused newer wallet schema leaked the SQLite handle** (`wallet/store.py`).
   `WalletStore.__init__` raised `WalletStoreError` while the connection stayed
   open, so on Windows the file remained locked for the rest of the run. The
   constructor now closes the connection before re-raising. Reproduced by
   `PersistenceTests::test_a_newer_schema_is_refused_rather_than_read`.
2. **Logging did not meet the requirement's breadth.** The engine logged only
   event-publication failures. Structured records for requests, commits,
   replays, failures, reconciliations and integrity violations were added, with
   five tests (`WalletLoggingTests`) that assert the records — and that a replay
   is *not* logged as a commit and a refusal is *not* logged as a commit.
3. **Permission-table ordering.** Adding `MarketOperation.CORRECT_LEDGER` broke
   the "exactly one policy per operation" invariant because the row order no
   longer matched the enum; the sensitive-config row was moved after the ledger
   row. `test_every_operation_has_exactly_one_policy` passes.
4. **Eight new tests asserted the wrong entry visibility.** They expected both
   ledger legs in the user's wallet history. The product is right — a wallet
   owns only its own leg, and the balance-sheet balancing lives in the issuance
   account — so the tests were corrected, not the ledger.

### 4.1 Second pass — adversarial audit

A second verification pass drove the engine directly on the paths where a green
suite can hide a wrong answer. **Three defects were found and fixed**, each with
a new test:

1. **An idempotent retry was refused once the wallet was frozen.** The engine
   validated wallet state before the store looked at the idempotency key, so
   retrying a committed deposit returned `409 WalletNotActiveError` instead of
   the original result — the documented "same key returns the original" was
   false after any freeze or close. `WalletStore.replay(key, fingerprint)` now
   answers the retry (returning the stored transaction and its wallet states)
   *before* the membership/status gate, for deposits, withdrawals, transfers and
   reversals; a replay by a different `owner` is still refused, and a replay
   publishes no event and moves nothing. Pinned by
   `test_a_replay_is_answered_after_the_wallet_is_frozen`,
   `test_a_replayed_reversal_returns_the_original_reversal` (which previously
   failed with "already reversed") and
   `test_a_replay_from_another_owner_is_refused`.
2. **The response lied about the page size.** `limit=9999` was clamped to 500 for
   the query but echoed back as `9999` in `limit`, so a client paging with the
   response value would skip rows. `transactions()`, `entries()` and `wallets()`
   now report the limit the store actually applied
   (`test_a_page_larger_than_the_cap_reports_the_applied_limit`).
3. **`/market/wallet/status` omitted the schema version** the architecture and
   API documents promised. `WalletStore.schema_version` (read from
   `wallet_meta`) is now part of the status payload
   (`test_status_reports_the_wallet_schema_version`).

Two documentation claims did not match the code and were corrected rather than
silently left standing: the issuance account **is** readable through
`GET /market/wallets/{wallet_id}` (reporting `system: true`) while never being
listed or manageable — the API document said `404`; and the whole-ledger
reconciliation report's `wallet_id` is `""` (now stated plainly). The behaviour
of a reversal whose funds have already left the wallet was confirmed correct
(`InsufficientFundsError`, nothing written) and pinned by
`test_reversing_a_deposit_whose_funds_left_is_refused`.

A related environment note, not a code defect: `generate_api_reference.py`
initially produced a reference with **no** wallet routes because it imported the
main repository's editable install rather than the worktree; running it with
`PYTHONPATH=src` regenerates all 400 routes including the 13 wallet routes, and
`--check` is clean.

## 5. Regressions

| Suite | Baseline before NM-03 work on it | Now |
| --- | --- | --- |
| `test_novamarket_marketdata.py` (NM-02) | 41 passed, 2 skipped | 41 passed, 2 skipped (inside suite 2) |
| `test_novamarket.py` + `test_novamarket_api.py` (NM-01) | 103 passed, 75 subtests | 103 passed, 75 subtests (inside suite 2) |
| `test_web_api.py` + `test_optimization.py` (shared app) | 150 passed, 77 subtests | 150 passed, 77 subtests |

Pins that had to move for NM-03, deliberately and visibly rather than by
loosening them: `IMPLEMENTED_PHASE` `NM-02` → `NM-03`; market route count 15 → 28
and the declared path set; granted-operation set (+3, test renamed
`test_only_reads_configuration_and_simulated_money_are_granted`); capability
count 8 → 12; implemented events 23 → 32; `_IMPLEMENTED_PAYLOADS` (+9 rows).

## 6. Limitations of this verification

- The **full suite was not run** (standing instruction). The suites above cover
  NM-03, its NM-01/NM-02 dependencies, the shared route/event/permission
  registries and the two application suites most likely to break.
- HTTP behaviour was exercised through FastAPI's `TestClient` against the real
  `create_app()` factory, not against a long-running `uvicorn` process; no live
  browser session was driven for NM-03 (it has no UI).
- The concurrency evidence is an in-process multi-threaded test against one
  SQLite file, not a multi-process load test or a soak test. There is no
  performance claim in this report.
- No real money, broker, exchange or payment rail is involved anywhere, so
  "financial correctness" here means ledger arithmetic and invariants, not
  settlement with a third party.
- Reconciliation's whole-ledger sweep is capped at 200,000 entries and reports
  `ledger_too_large` beyond that; the cap is not exercised at scale.
- The `adjustment` entry type is declared and constrained but no route posts
  one; it exists for a future confirmed correction. A reader must not assume a
  correction can be an adjustment today — only reversals are reachable.
- Replay-before-state is covered at engine level; the HTTP routes are thin
  delegates and are covered for their own contract, but there is no
  multi-process replay test.
- A wallet whose status is `closed` can still be set back to `active`; NM-03
  defines no transition table for reopening, and this pass did not invent one.

## 7. NM-04 readiness

NM-04 (Evidence-Backed Market Event Radar) can rely on:

- `WalletEngine` as the only write path (`deposit`/`withdraw`/`transfer`/
  `reverse`), with business ids usable as idempotency keys;
- wallet reads (`wallet`, `wallets`, `transactions`, `entries`) and
  `reconcile()` returning data rather than raising;
- the nine wallet/ledger events on the existing bus, plus
  `status()["wallet"]` and `contracts()["wallet"]` for observability;
- a database it shares with NM-02 without either subsystem owning the other's
  schema version.

What NM-04 must not do: write balances directly, edit or delete ledger rows, or
treat a reconciliation failure as auto-repairable. Those are pinned by database
triggers, not conventions.
