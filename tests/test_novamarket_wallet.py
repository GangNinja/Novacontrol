"""NM-03 verification: NovaCoin wallets and the deterministic ledger.

The suite drives the real SQLite repository and the real engine — no mock stands
in for the ledger. A recording publisher stands in for the event bus only so the
tests can assert *when* an event was published relative to the commit, which is
the one property a real bus would hide.
"""

from __future__ import annotations

import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable

from novacontrol.core.events import EVENT_PAYLOAD_FIELDS, EventType
from novacontrol.novamarket.contracts import (
    IMPLEMENTED_PHASE,
    IdempotencyConflictError,
    InsufficientFundsError,
    LedgerIntegrityError,
    WalletConflictError,
    WalletNotFoundError,
    WalletNotActiveError,
    WalletStoreError,
    WalletValidationError,
)
from novacontrol.novamarket.events import IMPLEMENTED_EVENT_NAMES
from novacontrol.novamarket.money import Currency, Money
from novacontrol.novamarket.permissions import MarketOperation, MarketPermissionModel
from novacontrol.novamarket.wallet import (
    EntryDirection,
    EntryType,
    LedgerLeg,
    PostingRequest,
    TransactionType,
    WALLET_SCHEMA_VERSION,
    WalletEngine,
    WalletStatus,
    WalletStore,
    amount_text,
    open_wallet_store,
    parse_amount,
)
from novacontrol.novamarket.wallet.models import SYSTEM_OWNER


class RecordingPublisher:
    """Records events and lets a test observe ledger state at publish time."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.on_publish: Callable[[], None] | None = None

    async def publish(
        self,
        event_type: EventType | str,
        /,
        *,
        correlation_id: str = "",
        causation_id: str | None = None,
        **payload: Any,
    ) -> None:
        self.events.append((str(event_type), dict(payload)))
        if self.on_publish is not None:
            self.on_publish()

    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def payloads(self, event_type: EventType | str) -> list[dict[str, Any]]:
        wanted = str(event_type)
        return [payload for name, payload in self.events if name == wanted]


def _deposit_request(
    store: WalletStore,
    wallet_id: str,
    amount: str,
    *,
    key: str = "",
    fingerprint: str = "fp",
) -> PostingRequest:
    value = Decimal(amount)
    system = store.system_wallet_id
    return PostingRequest(
        transaction_type=TransactionType.DEPOSIT,
        wallet_id=wallet_id,
        amount=value,
        legs=(
            LedgerLeg(system, EntryDirection.DEBIT, value, EntryType.DEPOSIT),
            LedgerLeg(wallet_id, EntryDirection.CREDIT, value, EntryType.DEPOSIT),
        ),
        idempotency_key=key,
        fingerprint=fingerprint,
    )


# ── precision -----------------------------------------------------------------


class PrecisionTests(unittest.TestCase):
    """NC carries two decimals and an unrepresentable amount is refused."""

    def test_amounts_are_decimal_and_quantized_to_paise(self) -> None:
        self.assertEqual(parse_amount("100"), Decimal("100.00"))
        self.assertEqual(parse_amount(Decimal("1.230")), Decimal("1.23"))
        self.assertEqual(parse_amount("0.01"), Decimal("0.01"))

    def test_a_binary_float_is_refused(self) -> None:
        with self.assertRaises(TypeError):
            parse_amount(0.1)
        with self.assertRaises(TypeError):
            parse_amount(True)

    def test_non_finite_values_are_refused(self) -> None:
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_amount(value)

    def test_an_unrepresentable_amount_is_refused_not_rounded(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_amount("1.005")
        self.assertIn("finer than one paisa", str(caught.exception))

    def test_zero_and_negative_amounts_are_refused(self) -> None:
        for value in ("0", "0.00", "-1.00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_amount(value)

    def test_absurd_magnitudes_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            parse_amount("1000000000001")

    def test_nova_coin_is_the_same_number_as_rupees(self) -> None:
        self.assertEqual(Money.nc("100").amount, Money.inr("100").amount)
        self.assertEqual(Money.nc("12.34").nova_coin, Decimal("12.34"))
        self.assertEqual(Money.nc("12.34").currency, Currency.INR)

    def test_amount_text_is_a_plain_decimal_string(self) -> None:
        self.assertEqual(amount_text(Decimal("5")), "5.00")
        self.assertEqual(amount_text(Money.nc("5.5")), "5.50")


# ── wallets -------------------------------------------------------------------


class WalletStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = WalletStore(":memory:")
        self.addCleanup(self.store.close)

    def test_a_new_wallet_has_a_stable_id_and_a_zero_balance(self) -> None:
        wallet = self.store.create_wallet("alice", label="Alice")
        self.assertTrue(wallet.wallet_id.startswith("wal_"))
        self.assertEqual(wallet.balance.amount, Decimal("0.00"))
        self.assertEqual(wallet.status, WalletStatus.ACTIVE)
        self.assertEqual(self.store.wallet(wallet.wallet_id).wallet_id, wallet.wallet_id)
        self.assertTrue(wallet.created_at.tzinfo is not None)

    def test_wallet_ids_are_unique_and_a_duplicate_is_refused(self) -> None:
        wallet = self.store.create_wallet("alice", wallet_id="wal_fixed")
        self.assertEqual(wallet.wallet_id, "wal_fixed")
        with self.assertRaises(WalletConflictError):
            self.store.create_wallet("bob", wallet_id="wal_fixed")

    def test_an_unknown_wallet_is_a_lookup_error(self) -> None:
        with self.assertRaises(WalletNotFoundError):
            self.store.wallet("wal_missing")

    def test_a_wallet_needs_an_owner(self) -> None:
        with self.assertRaises(WalletValidationError):
            self.store.create_wallet("   ")

    def test_the_issuance_account_is_reserved(self) -> None:
        with self.assertRaises(WalletValidationError):
            self.store.create_wallet(SYSTEM_OWNER)
        self.assertEqual(self.store.wallet(self.store.system_wallet_id).owner, SYSTEM_OWNER)

    def test_the_issuance_wallet_is_not_listed_as_a_user_wallet(self) -> None:
        self.store.create_wallet("alice")
        listed = self.store.wallets()
        self.assertEqual(len(listed), 1)
        self.assertFalse(any(wallet.system for wallet in listed))
        self.assertEqual(self.store.count_wallets(), 1)

    def test_balance_reports_available_equals_total_with_no_reserve(self) -> None:
        wallet = self.store.create_wallet("alice")
        payload = wallet.to_dict()["balance"]
        self.assertEqual(payload["available"], payload["total"])
        self.assertEqual(payload["reserved"], "0.00")
        self.assertEqual(payload["nova_coin"], payload["total"])

    def test_a_frozen_wallet_refuses_money_and_a_zero_balance_wallet_closes(self) -> None:
        wallet = self.store.create_wallet("alice")
        frozen = self.store.set_status(wallet.wallet_id, "frozen", reason="review")
        self.assertEqual(frozen.status, WalletStatus.FROZEN)
        closed = self.store.set_status(wallet.wallet_id, "closed", reason="done")
        self.assertEqual(closed.status, WalletStatus.CLOSED)

    def test_closing_a_wallet_that_still_holds_money_is_refused(self) -> None:
        wallet = self.store.create_wallet("alice")
        self.store.post(_deposit_request(self.store, wallet.wallet_id, "10.00"))
        with self.assertRaises(WalletConflictError):
            self.store.set_status(wallet.wallet_id, "closed")

    def test_the_issuance_wallet_status_is_not_api_managed(self) -> None:
        with self.assertRaises(WalletConflictError):
            self.store.set_status(self.store.system_wallet_id, "closed")


# ── posting -------------------------------------------------------------------


class LedgerPostingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = WalletStore(":memory:")
        self.addCleanup(self.store.close)
        self.alice = self.store.create_wallet("alice")
        self.bob = self.store.create_wallet("bob")

    def test_a_deposit_credits_the_wallet_and_balances_the_issuance_account(self) -> None:
        result = self.store.post(
            _deposit_request(self.store, self.alice.wallet_id, "250.00")
        )
        self.assertEqual(result.wallets[self.alice.wallet_id].balance.amount, Decimal("250.00"))
        self.assertEqual(len(result.entries), 2)
        self.assertEqual(
            sum(entry.signed_amount.amount for entry in result.entries), Decimal("0.00")
        )
        issuance = self.store.wallet(self.store.system_wallet_id)
        self.assertEqual(issuance.balance.amount, Decimal("-250.00"))

    def test_a_transfer_posts_one_debit_and_one_credit(self) -> None:
        self.store.post(_deposit_request(self.store, self.alice.wallet_id, "100.00"))
        result = self.store.post(
            PostingRequest(
                transaction_type=TransactionType.TRANSFER,
                wallet_id=self.alice.wallet_id,
                counterparty_wallet_id=self.bob.wallet_id,
                amount=Decimal("40.00"),
                legs=(
                    LedgerLeg(
                        self.alice.wallet_id,
                        EntryDirection.DEBIT,
                        Decimal("40.00"),
                        EntryType.TRANSFER,
                    ),
                    LedgerLeg(
                        self.bob.wallet_id,
                        EntryDirection.CREDIT,
                        Decimal("40.00"),
                        EntryType.TRANSFER,
                    ),
                ),
            )
        )
        self.assertEqual(result.wallets[self.alice.wallet_id].balance.amount, Decimal("60.00"))
        self.assertEqual(result.wallets[self.bob.wallet_id].balance.amount, Decimal("40.00"))
        debits = [e for e in result.entries if e.direction is EntryDirection.DEBIT]
        credits = [e for e in result.entries if e.direction is EntryDirection.CREDIT]
        self.assertEqual(len(debits), 1)
        self.assertEqual(len(credits), 1)
        self.assertEqual(debits[0].amount.amount, credits[0].amount.amount)
        self.assertEqual(debits[0].signed_amount.amount, Decimal("-40.00"))

    def test_an_insufficient_transfer_changes_nothing(self) -> None:
        self.store.post(_deposit_request(self.store, self.alice.wallet_id, "10.00"))
        before = self.store.summary()
        with self.assertRaises(InsufficientFundsError) as caught:
            self.store.post(
                PostingRequest(
                    transaction_type=TransactionType.TRANSFER,
                    wallet_id=self.alice.wallet_id,
                    counterparty_wallet_id=self.bob.wallet_id,
                    amount=Decimal("11.00"),
                    legs=(
                        LedgerLeg(
                            self.alice.wallet_id,
                            EntryDirection.DEBIT,
                            Decimal("11.00"),
                            EntryType.TRANSFER,
                        ),
                        LedgerLeg(
                            self.bob.wallet_id,
                            EntryDirection.CREDIT,
                            Decimal("11.00"),
                            EntryType.TRANSFER,
                        ),
                    ),
                )
            )
        self.assertIn("never allowed to go negative", str(caught.exception))
        after = self.store.summary()
        self.assertEqual(before["ledger_entries"], after["ledger_entries"])
        self.assertEqual(before["transactions"], after["transactions"])
        self.assertEqual(
            self.store.wallet(self.bob.wallet_id).balance.amount, Decimal("0.00")
        )

    def test_an_unbalanced_posting_is_refused(self) -> None:
        with self.assertRaises(LedgerIntegrityError):
            self.store.post(
                PostingRequest(
                    transaction_type=TransactionType.TRANSFER,
                    wallet_id=self.alice.wallet_id,
                    amount=Decimal("5.00"),
                    legs=(
                        LedgerLeg(
                            self.alice.wallet_id,
                            EntryDirection.DEBIT,
                            Decimal("5.00"),
                            EntryType.TRANSFER,
                        ),
                        LedgerLeg(
                            self.bob.wallet_id,
                            EntryDirection.CREDIT,
                            Decimal("4.00"),
                            EntryType.TRANSFER,
                        ),
                    ),
                )
            )
        self.assertEqual(self.store.summary()["ledger_entries"], 0)

    def test_a_frozen_wallet_refuses_a_posting(self) -> None:
        self.store.set_status(self.alice.wallet_id, "frozen")
        with self.assertRaises(WalletNotActiveError):
            self.store.post(_deposit_request(self.store, self.alice.wallet_id, "1.00"))

    def test_identical_idempotent_posting_returns_the_original(self) -> None:
        first = self.store.post(
            _deposit_request(self.store, self.alice.wallet_id, "20.00", key="k1")
        )
        second = self.store.post(
            _deposit_request(self.store, self.alice.wallet_id, "20.00", key="k1")
        )
        self.assertTrue(second.replayed)
        self.assertEqual(first.transaction.transaction_id, second.transaction.transaction_id)
        self.assertEqual(
            self.store.wallet(self.alice.wallet_id).balance.amount, Decimal("20.00")
        )
        self.assertEqual(self.store.summary()["transactions"], 1)

    def test_reusing_a_key_for_a_different_amount_is_refused(self) -> None:
        self.store.post(_deposit_request(self.store, self.alice.wallet_id, "20.00", key="k1"))
        with self.assertRaises(IdempotencyConflictError):
            self.store.post(
                _deposit_request(
                    self.store,
                    self.alice.wallet_id,
                    "21.00",
                    key="k1",
                    fingerprint="different",
                )
            )

    def test_ledger_entries_are_append_only_at_the_database_level(self) -> None:
        self.store.post(_deposit_request(self.store, self.alice.wallet_id, "5.00"))
        for statement in (
            "UPDATE ledger_entries SET amount_paise = 1",
            "DELETE FROM ledger_entries",
            "UPDATE wallet_transactions SET amount = '9.00'",
            "DELETE FROM wallet_transactions",
        ):
            with self.subTest(statement=statement), self.assertRaises(
                sqlite3.IntegrityError
            ):
                self.store._connection.execute(statement)

    def test_concurrent_withdrawals_never_overdraw_a_wallet(self) -> None:
        self.store.post(_deposit_request(self.store, self.alice.wallet_id, "100.00"))

        def withdraw() -> bool:
            try:
                self.store.post(
                    PostingRequest(
                        transaction_type=TransactionType.WITHDRAWAL,
                        wallet_id=self.alice.wallet_id,
                        amount=Decimal("15.00"),
                        legs=(
                            LedgerLeg(
                                self.alice.wallet_id,
                                EntryDirection.DEBIT,
                                Decimal("15.00"),
                                EntryType.WITHDRAWAL,
                            ),
                            LedgerLeg(
                                self.store.system_wallet_id,
                                EntryDirection.CREDIT,
                                Decimal("15.00"),
                                EntryType.WITHDRAWAL,
                            ),
                        ),
                    )
                )
                return True
            except InsufficientFundsError:
                return False

        with ThreadPoolExecutor(max_workers=10) as pool:
            outcomes = list(pool.map(lambda _: withdraw(), range(10)))

        self.assertEqual(sum(outcomes), 6)
        self.assertEqual(
            self.store.wallet(self.alice.wallet_id).balance.amount, Decimal("10.00")
        )
        self.assertTrue(self.store.reconcile().ok)


class PersistenceTests(unittest.TestCase):
    def test_wallets_and_history_survive_reopening_the_database(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "wallet.sqlite3"
            store = WalletStore(path)
            try:
                wallet = store.create_wallet("alice")
                store.post(_deposit_request(store, wallet.wallet_id, "42.50"))
            finally:
                store.close()

            reopened = WalletStore(path)
            try:
                self.assertEqual(
                    reopened.wallet(wallet.wallet_id).balance.amount, Decimal("42.50")
                )
                self.assertEqual(
                    reopened.count_transactions(wallet_id=wallet.wallet_id), 1
                )
                # One leg per wallet: the credit side. The issuance wallet holds
                # the debit side of a deposit.
                self.assertEqual(reopened.count_entries(wallet_id=wallet.wallet_id), 1)
            finally:
                reopened.close()

    def test_the_schema_version_lives_in_the_wallet_database(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "wallet.sqlite3"
            store = WalletStore(path)
            try:
                store.create_wallet("alice")
                version = store._connection.execute(
                    "SELECT value FROM wallet_meta WHERE key = 'schema_version'"
                ).fetchone()[0]
            finally:
                store.close()
            self.assertEqual(int(version), WALLET_SCHEMA_VERSION)

    def test_a_newer_schema_is_refused_rather_than_read(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "wallet.sqlite3"
            store = WalletStore(path)
            try:
                store.create_wallet("alice")
                store._connection.execute(
                    "UPDATE wallet_meta SET value = ? WHERE key = 'schema_version'",
                    (str(WALLET_SCHEMA_VERSION + 1),),
                )
            finally:
                store.close()
            with self.assertRaises(WalletStoreError):
                WalletStore(path)

    def test_the_ledger_shares_the_market_data_database_without_clashing(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "novamarket.sqlite3"
            market = sqlite3.connect(path)
            try:
                market.execute("CREATE TABLE market_observations (observation_id TEXT)")
                market.execute("PRAGMA user_version=1")
                market.commit()
            finally:
                market.close()

            store = WalletStore(path)
            try:
                wallet = store.create_wallet("alice")
                store.post(_deposit_request(store, wallet.wallet_id, "5.00"))
                self.assertEqual(
                    store.wallet(wallet.wallet_id).balance.amount, Decimal("5.00")
                )
                # NM-02's pragma is untouched: the ledger versions itself in
                # wallet_meta precisely so it cannot overwrite this.
                self.assertEqual(
                    store._connection.execute("PRAGMA user_version").fetchone()[0], 1
                )
            finally:
                store.close()

    def test_open_wallet_store_uses_the_data_directory(self) -> None:
        with TemporaryDirectory() as directory:
            store = open_wallet_store(directory)
            try:
                self.assertTrue(Path(directory, "novamarket.sqlite3").exists())
            finally:
                store.close()


# ── reconciliation ------------------------------------------------------------


class ReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = WalletStore(":memory:")
        self.addCleanup(self.store.close)
        self.alice = self.store.create_wallet("alice")
        self.store.post(_deposit_request(self.store, self.alice.wallet_id, "50.00"))

    def test_a_clean_ledger_reconciles_and_sums_to_zero(self) -> None:
        report = self.store.reconcile()
        self.assertTrue(report.ok)
        self.assertEqual(report.ledger_sum.amount, Decimal("0.00"))
        self.assertEqual(report.ledger_balance.amount, report.stored_balance.amount)
        self.assertEqual(report.issues, ())

    def test_a_tampered_stored_balance_is_detected_and_preserved(self) -> None:
        self.store._connection.execute(
            "UPDATE wallets SET balance = '999.00', balance_paise = 99900 "
            "WHERE wallet_id = ?",
            (self.alice.wallet_id,),
        )
        report = self.store.reconcile(self.alice.wallet_id)
        self.assertFalse(report.ok)
        codes = [issue.code for issue in report.issues]
        self.assertIn("balance_mismatch", codes)
        self.assertEqual(report.stored_balance.amount, Decimal("999.00"))
        self.assertEqual(report.ledger_balance.amount, Decimal("50.00"))
        self.assertEqual(report.difference.amount, Decimal("949.00"))
        # Nothing was repaired.
        self.assertEqual(
            self.store.wallet(self.alice.wallet_id).balance.amount, Decimal("999.00")
        )

    def test_an_orphan_transaction_is_reported(self) -> None:
        self.store._connection.execute(
            """
            INSERT INTO wallet_transactions (transaction_id, transaction_type,
                status, wallet_id, amount, amount_paise, currency, entry_count,
                created_at)
            VALUES ('txn_orphan', 'deposit', 'posted', ?, '1.00', 100, 'INR', 2,
                '2026-11-02T00:00:00+00:00')
            """,
            (self.alice.wallet_id,),
        )
        report = self.store.reconcile()
        codes = [issue.code for issue in report.issues]
        self.assertIn("orphan_transaction", codes)
        self.assertIn("entry_count_mismatch", codes)

    def test_a_duplicate_entry_is_reported(self) -> None:
        row = self.store._connection.execute(
            "SELECT * FROM ledger_entries WHERE wallet_id = ? AND direction = 'credit'",
            (self.alice.wallet_id,),
        ).fetchone()
        self.store._connection.execute(
            """
            INSERT INTO ledger_entries (entry_id, transaction_id, wallet_id,
                entry_type, direction, amount, amount_paise, signed_amount,
                signed_amount_paise, balance_after, balance_after_paise, currency,
                sequence, created_at)
            VALUES ('led_dup', ?, ?, 'deposit', 'credit', ?, ?, ?, ?, ?, ?, 'INR',
                ?, ?)
            """,
            (
                row["transaction_id"],
                row["wallet_id"],
                row["amount"],
                row["amount_paise"],
                row["signed_amount"],
                row["signed_amount_paise"],
                row["balance_after"],
                row["balance_after_paise"],
                99,
                row["created_at"],
            ),
        )
        report = self.store.reconcile()
        self.assertIn("duplicate_entry", [issue.code for issue in report.issues])

    def test_an_unbalanced_transaction_is_reported(self) -> None:
        self.store._connection.execute(
            """
            INSERT INTO wallet_transactions (transaction_id, transaction_type,
                status, wallet_id, amount, amount_paise, currency, entry_count,
                created_at)
            VALUES ('txn_bad', 'adjustment', 'posted', ?, '1.00', 100, 'INR', 2,
                '2026-11-02T00:00:00+00:00')
            """,
            (self.alice.wallet_id,),
        )
        for sequence in (0, 1):
            self.store._connection.execute(
                """
                INSERT INTO ledger_entries (entry_id, transaction_id, wallet_id,
                    entry_type, direction, amount, amount_paise, signed_amount,
                    signed_amount_paise, balance_after, balance_after_paise,
                    currency, sequence, created_at)
                VALUES (?, 'txn_bad', ?, 'adjustment', 'credit', '1.00', 100,
                    '1.00', 100, '1.00', 100, 'INR', ?, '2026-11-02T00:00:00+00:00')
                """,
                (f"led_bad_{sequence}", self.alice.wallet_id, sequence),
            )
        report = self.store.reconcile()
        codes = [issue.code for issue in report.issues]
        self.assertIn("unbalanced_transaction", codes)
        self.assertIn("ledger_not_zero_sum", codes)


# ── engine --------------------------------------------------------------------


class WalletEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = WalletStore(":memory:")
        self.addCleanup(self.store.close)
        self.publisher = RecordingPublisher()
        self.engine = WalletEngine(self.store, publisher=self.publisher)

    async def _wallet(self, owner: str = "alice") -> str:
        created = await self.engine.create_wallet(owner)
        return created["wallet"]["wallet_id"]

    async def test_deposit_and_withdraw_change_the_balance_by_ledger_entries(self) -> None:
        wallet_id = await self._wallet()
        deposited = await self.engine.deposit(wallet_id, "100.00")
        self.assertEqual(deposited["wallets"][wallet_id]["balance"]["total"], "100.00")
        withdrawn = await self.engine.withdraw(wallet_id, "30.00")
        self.assertEqual(withdrawn["wallets"][wallet_id]["balance"]["total"], "70.00")
        self.assertEqual(self.store.count_entries(wallet_id=wallet_id), 2)

    async def test_a_withdrawal_over_the_balance_is_refused_and_publishes_a_failure(self) -> None:
        wallet_id = await self._wallet()
        await self.engine.deposit(wallet_id, "10.00")
        with self.assertRaises(InsufficientFundsError):
            await self.engine.withdraw(wallet_id, "11.00")
        self.assertIn(
            str(EventType.NOVAMARKET_WALLET_TRANSACTION_FAILED), self.publisher.names()
        )
        self.assertEqual(
            self.engine.wallet(wallet_id)["wallet"]["balance"]["total"], "10.00"
        )

    async def test_a_transfer_moves_money_between_two_wallets(self) -> None:
        alice = await self._wallet("alice")
        bob = await self._wallet("bob")
        await self.engine.deposit(alice, "80.00")
        result = await self.engine.transfer(alice, bob, "25.50")
        self.assertEqual(result["wallets"][alice]["balance"]["total"], "54.50")
        self.assertEqual(result["wallets"][bob]["balance"]["total"], "25.50")
        payload = self.publisher.payloads(EventType.NOVAMARKET_WALLET_TRANSFER_COMPLETED)
        self.assertEqual(payload[0]["from_wallet_id"], alice)
        self.assertEqual(payload[0]["to_wallet_id"], bob)
        self.assertEqual(payload[0]["amount"], "25.50")

    async def test_a_self_transfer_is_refused(self) -> None:
        wallet_id = await self._wallet()
        await self.engine.deposit(wallet_id, "10.00")
        with self.assertRaises(WalletValidationError):
            await self.engine.transfer(wallet_id, wallet_id, "1.00")

    async def test_a_frozen_wallet_refuses_deposits(self) -> None:
        wallet_id = await self._wallet()
        await self.engine.set_wallet_status(wallet_id, "frozen", reason="review")
        with self.assertRaises(WalletNotActiveError):
            await self.engine.deposit(wallet_id, "1.00")

    async def test_an_ownership_mismatch_is_refused(self) -> None:
        wallet_id = await self._wallet("alice")
        with self.assertRaises(WalletValidationError):
            await self.engine.deposit(wallet_id, "1.00", owner="mallory")
        allowed = await self.engine.deposit(wallet_id, "1.00", owner="alice")
        self.assertEqual(allowed["wallets"][wallet_id]["balance"]["total"], "1.00")

    async def test_the_issuance_wallet_cannot_be_deposited_into(self) -> None:
        with self.assertRaises(WalletValidationError):
            await self.engine.deposit(self.store.system_wallet_id, "1.00")

    async def test_events_are_published_only_after_the_commit(self) -> None:
        wallet_id = await self._wallet()
        observed: list[int] = []
        self.publisher.on_publish = lambda: observed.append(
            self.store.summary()["ledger_entries"]
        )
        await self.engine.deposit(wallet_id, "5.00")
        self.assertTrue(observed)
        self.assertTrue(all(count > 0 for count in observed))

    async def test_an_idempotent_replay_publishes_no_second_money_event(self) -> None:
        wallet_id = await self._wallet()
        await self.engine.deposit(wallet_id, "5.00", idempotency_key="dep-1")
        before = len(self.publisher.events)
        replay = await self.engine.deposit(wallet_id, "5.00", idempotency_key="dep-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.publisher.events), before)
        self.assertEqual(
            self.store.wallet(wallet_id).balance.amount, Decimal("5.00")
        )

    async def test_a_reversal_restores_the_balance_with_mirrored_entries(self) -> None:
        alice = await self._wallet("alice")
        bob = await self._wallet("bob")
        await self.engine.deposit(alice, "40.00")
        transfer = await self.engine.transfer(alice, bob, "15.00")
        transaction_id = transfer["transaction"]["transaction_id"]

        reversal = await self.engine.reverse(transaction_id, reason="duplicate entry")
        self.assertEqual(reversal["wallets"][alice]["balance"]["total"], "40.00")
        self.assertEqual(reversal["wallets"][bob]["balance"]["total"], "0.00")
        original = self.store.transaction(transaction_id)
        self.assertTrue(original.reversed)
        self.assertEqual(reversal["transaction"]["reverses_transaction_id"], transaction_id)
        directions = {entry["direction"] for entry in reversal["entries"]}
        self.assertEqual(directions, {"debit", "credit"})
        self.assertTrue(self.store.reconcile().ok)

    async def test_a_reversal_needs_a_reason(self) -> None:
        wallet_id = await self._wallet()
        deposited = await self.engine.deposit(wallet_id, "5.00")
        with self.assertRaises(WalletValidationError):
            await self.engine.reverse(
                deposited["transaction"]["transaction_id"], reason="   "
            )

    async def test_a_transaction_cannot_be_reversed_twice(self) -> None:
        wallet_id = await self._wallet()
        deposited = await self.engine.deposit(wallet_id, "5.00")
        transaction_id = deposited["transaction"]["transaction_id"]
        await self.engine.reverse(transaction_id, reason="mistake")
        with self.assertRaises(WalletConflictError):
            await self.engine.reverse(transaction_id, reason="again")

    async def test_a_reversal_cannot_be_reversed(self) -> None:
        wallet_id = await self._wallet()
        deposited = await self.engine.deposit(wallet_id, "5.00")
        reversal = await self.engine.reverse(
            deposited["transaction"]["transaction_id"], reason="mistake"
        )
        with self.assertRaises(WalletConflictError):
            await self.engine.reverse(
                reversal["transaction"]["transaction_id"], reason="again"
            )

    async def test_a_reversal_that_would_overdraw_is_refused(self) -> None:
        alice = await self._wallet("alice")
        bob = await self._wallet("bob")
        await self.engine.deposit(alice, "100.00")
        transfer = await self.engine.transfer(alice, bob, "100.00")
        await self.engine.withdraw(bob, "100.00")
        with self.assertRaises(InsufficientFundsError):
            await self.engine.reverse(
                transfer["transaction"]["transaction_id"], reason="refund"
            )

    async def test_history_and_ledger_reads_are_paginated(self) -> None:
        wallet_id = await self._wallet()
        for index in range(3):
            await self.engine.deposit(wallet_id, "1.00", reference=f"r{index}")
        page = self.engine.transactions(wallet_id=wallet_id, limit=2)
        self.assertEqual(len(page["transactions"]), 2)
        self.assertEqual(page["total"], 3)
        entries = self.engine.entries(wallet_id=wallet_id, limit=2)
        self.assertEqual(len(entries["entries"]), 2)
        self.assertEqual(entries["total"], 3)
        with self.assertRaises(WalletValidationError):
            self.engine.entries(limit=0)

    async def test_reconciliation_publishes_its_result(self) -> None:
        wallet_id = await self._wallet()
        await self.engine.deposit(wallet_id, "5.00")
        report = await self.engine.reconcile()
        self.assertTrue(report["ok"])
        payload = self.publisher.payloads(
            EventType.NOVAMARKET_LEDGER_RECONCILIATION_COMPLETED
        )
        self.assertTrue(payload)
        self.assertTrue(payload[-1]["ok"])
        self.assertEqual(report["nova_coin"]["ledger_balance"], report["ledger_balance"])

    async def test_status_reports_the_ledger_totals(self) -> None:
        wallet_id = await self._wallet()
        await self.engine.deposit(wallet_id, "12.00")
        status = self.engine.status()
        self.assertTrue(status["initialised"])
        self.assertEqual(status["summary"]["user_balance"], "12.00")
        self.assertEqual(status["summary"]["ledger_sum"], "0.00")
        self.assertEqual(status["nova_coin_per_inr"], "1")


# ── phase contracts -----------------------------------------------------------


class PhaseContractTests(unittest.TestCase):
    def test_the_build_reports_nm14_as_the_implemented_phase(self) -> None:
        self.assertEqual(IMPLEMENTED_PHASE, "NM-14")

    def test_every_wallet_event_has_a_declared_payload(self) -> None:
        names = [
            "NOVAMARKET_WALLET_CREATED",
            "NOVAMARKET_WALLET_BALANCE_UPDATED",
            "NOVAMARKET_WALLET_DEPOSIT_COMPLETED",
            "NOVAMARKET_WALLET_WITHDRAWAL_COMPLETED",
            "NOVAMARKET_WALLET_TRANSFER_COMPLETED",
            "NOVAMARKET_WALLET_TRANSACTION_FAILED",
            "NOVAMARKET_LEDGER_ENTRY_CREATED",
            "NOVAMARKET_LEDGER_RECONCILIATION_COMPLETED",
            "NOVAMARKET_LEDGER_INTEGRITY_VIOLATION",
        ]
        for name in names:
            with self.subTest(event=name):
                event_type = getattr(EventType, name)
                self.assertIn(str(event_type), IMPLEMENTED_EVENT_NAMES)
                self.assertTrue(EVENT_PAYLOAD_FIELDS.get(event_type))
                self.assertTrue(str(event_type).startswith("novamarket."))

    def test_the_wallet_permissions_are_granted_and_live_execution_is_not(self) -> None:
        model = MarketPermissionModel()
        for operation in (
            MarketOperation.READ_WALLET,
            MarketOperation.MOVE_SIMULATED_FUNDS,
            MarketOperation.CORRECT_LEDGER,
        ):
            with self.subTest(operation=operation):
                self.assertTrue(model.authorize(operation).allowed)
        self.assertFalse(model.authorize(MarketOperation.EXECUTE_LIVE_TRADE).allowed)

    def test_a_reversal_is_declared_as_a_confirmed_correction(self) -> None:
        policy = MarketPermissionModel().policy(MarketOperation.CORRECT_LEDGER)
        self.assertTrue(policy.declaration.requires_confirmation)
        self.assertFalse(policy.declaration.reversible)


# ── HTTP ----------------------------------------------------------------------


def _test_client_app() -> Any:
    from fastapi.testclient import TestClient

    from novacontrol.api.app import create_app

    return TestClient(create_app())


class WalletApiTests(unittest.TestCase):
    """The HTTP surface drives the real service, engine and ledger."""

    def setUp(self) -> None:
        try:
            import httpx  # noqa: F401 - required by fastapi.testclient
            from fastapi.testclient import TestClient  # noqa: F401
        except Exception:  # pragma: no cover - environment without httpx
            self.skipTest("fastapi.testclient requires httpx")

        from unittest import mock

        from novacontrol.application import NovaControlApplication
        from novacontrol.browser.controller import NoopBrowserRunner
        from novacontrol.desktop.controller import NoopDesktopRunner

        self._tmp = TemporaryDirectory()
        self._patchers = [
            mock.patch(
                "novacontrol.api.app.NovaControlApplication",
                side_effect=self._isolated_application,
            ),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch(
                "novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner
            ),
        ]
        for patcher in self._patchers:
            patcher.start()
        self._client = _test_client_app()
        self._client.__enter__()

    def tearDown(self) -> None:
        self._client.__exit__(None, None, None)
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmp.cleanup()

    def _isolated_application(self, **kwargs: Any) -> Any:
        from novacontrol.application import NovaControlApplication

        kwargs["data_dir"] = self._tmp.name
        return NovaControlApplication(**kwargs)

    def _enable(self) -> None:
        response = self._client.post("/market/enable", json={})
        self.assertEqual(response.status_code, 200, response.text)

    def _create(self, owner: str = "alice") -> str:
        response = self._client.post("/market/wallets", json={"owner": owner})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["wallet"]["wallet_id"]

    def test_the_full_http_lifecycle_is_ledger_backed(self) -> None:
        self._enable()
        alice = self._create("alice")
        bob = self._create("bob")

        deposit = self._client.post(
            f"/market/wallets/{alice}/deposit", json={"amount": "500.00"}
        )
        self.assertEqual(deposit.status_code, 200, deposit.text)
        self.assertEqual(
            deposit.json()["wallets"][alice]["balance"]["total"], "500.00"
        )

        transfer = self._client.post(
            "/market/wallets/transfer",
            json={"from_wallet_id": alice, "to_wallet_id": bob, "amount": "125.25"},
        )
        self.assertEqual(transfer.status_code, 200, transfer.text)

        wallet = self._client.get(f"/market/wallets/{alice}")
        self.assertEqual(wallet.status_code, 200, wallet.text)
        self.assertEqual(wallet.json()["wallet"]["balance"]["total"], "374.75")
        self.assertGreaterEqual(wallet.json()["ledger_entries"], 2)

        history = self._client.get(f"/market/wallets/{alice}/transactions")
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["total"], 2)

        ledger = self._client.get(f"/market/wallets/{alice}/ledger")
        self.assertEqual(ledger.status_code, 200, ledger.text)
        self.assertTrue(
            all(entry["currency"] == "INR" for entry in ledger.json()["entries"])
        )

        reconcile = self._client.post("/market/wallet/reconcile")
        self.assertEqual(reconcile.status_code, 200, reconcile.text)
        self.assertTrue(reconcile.json()["ok"])
        self.assertEqual(reconcile.json()["ledger_sum"], "0.00")

        status = self._client.get("/market/wallet/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["summary"]["wallets"], 2)

    def test_invalid_amounts_and_unknown_wallets_have_distinct_statuses(self) -> None:
        self._enable()
        wallet_id = self._create()

        self.assertEqual(
            self._client.post(
                f"/market/wallets/{wallet_id}/deposit", json={"amount": "0"}
            ).status_code,
            422,
        )
        self.assertEqual(
            self._client.post(
                f"/market/wallets/{wallet_id}/deposit", json={"amount": "-5.00"}
            ).status_code,
            422,
        )
        self.assertEqual(
            self._client.post(
                f"/market/wallets/{wallet_id}/deposit", json={"amount": "1.005"}
            ).status_code,
            422,
        )
        self.assertEqual(
            self._client.get("/market/wallets/wal_missing").status_code, 404
        )
        self.assertEqual(
            self._client.post(
                f"/market/wallets/{wallet_id}/withdraw", json={"amount": "5.00"}
            ).status_code,
            409,
        )

    def test_a_repeated_request_with_one_idempotency_key_credits_once(self) -> None:
        self._enable()
        wallet_id = self._create()
        body = {"amount": "50.00", "idempotency_key": "http-1"}
        first = self._client.post(f"/market/wallets/{wallet_id}/deposit", json=body)
        second = self._client.post(f"/market/wallets/{wallet_id}/deposit", json=body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()["replayed"])
        wallet = self._client.get(f"/market/wallets/{wallet_id}").json()
        self.assertEqual(wallet["wallet"]["balance"]["total"], "50.00")
        self.assertEqual(wallet["transactions"], 1)

    def test_a_reversal_is_exposed_and_keeps_the_original(self) -> None:
        self._enable()
        wallet_id = self._create()
        deposit = self._client.post(
            f"/market/wallets/{wallet_id}/deposit", json={"amount": "20.00"}
        ).json()
        transaction_id = deposit["transaction"]["transaction_id"]

        reversal = self._client.post(
            "/market/wallet/reversals",
            json={"transaction_id": transaction_id, "reason": "entered twice"},
        )
        self.assertEqual(reversal.status_code, 200, reversal.text)
        wallet = self._client.get(f"/market/wallets/{wallet_id}").json()
        self.assertEqual(wallet["wallet"]["balance"]["total"], "0.00")
        history = self._client.get(f"/market/wallets/{wallet_id}/transactions").json()
        statuses = {row["transaction_id"]: row["status"] for row in history["transactions"]}
        self.assertEqual(statuses[transaction_id], "reversed")

        again = self._client.post(
            "/market/wallet/reversals",
            json={"transaction_id": transaction_id, "reason": "again"},
        )
        self.assertEqual(again.status_code, 409)

    def test_the_contract_reports_the_nm03_ledger(self) -> None:
        self._enable()
        body = self._client.get("/market/contracts").json()
        self.assertEqual(body["phase"], "NM-14")
        self.assertEqual(body["wallet"]["accounting_unit"], "NovaCoin")
        self.assertEqual(body["wallet"]["exchange_rate"], "1 NC = 1 INR")
        self.assertIn("ledger_entries", body["wallet"]["append_only"]["tables"])
        storage = {row["data"]: row for row in body["storage"]}
        self.assertTrue(storage["ledger_entries"]["append_only"])

    def test_the_ledger_is_not_available_while_disabled(self) -> None:
        self._enable()
        self._client.post("/market/disable", json={})
        response = self._client.get("/market/wallets")
        self.assertEqual(response.status_code, 409, response.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class WalletLoggingTests(unittest.IsolatedAsyncioTestCase):
    """The structured audit records: requests, failures, reconciliations."""

    LOGGER = "novacontrol.novamarket.wallet.engine"

    async def asyncSetUp(self) -> None:
        self.store = WalletStore(":memory:")
        self.addCleanup(self.store.close)
        self.engine = WalletEngine(self.store)

    async def _wallet(self, owner: str = "alice") -> str:
        body = await self.engine.create_wallet(owner)
        return str(body["wallet"]["wallet_id"])

    async def test_a_committed_deposit_logs_the_request_and_the_result(self) -> None:
        wallet_id = await self._wallet()
        with self.assertLogs(self.LOGGER, level="INFO") as captured:
            body = await self.engine.deposit(
                wallet_id, "10.00", idempotency_key="seed-1"
            )
        text = " ".join(captured.output)
        self.assertIn("novamarket.wallet.request operation=deposit", text)
        self.assertIn("novamarket.wallet.committed", text)
        self.assertIn(f"transaction={body['transaction']['transaction_id']}", text)
        self.assertIn("amount=10.00", text)
        self.assertIn("entries=2", text)

    async def test_a_refused_withdrawal_logs_the_failure_and_no_commit(self) -> None:
        wallet_id = await self._wallet()
        with self.assertLogs(self.LOGGER, level="INFO") as captured:
            with self.assertRaises(InsufficientFundsError):
                await self.engine.withdraw(wallet_id, "5.00")
        text = " ".join(captured.output)
        self.assertIn("novamarket.wallet.request operation=withdraw", text)
        self.assertIn("novamarket.wallet.failed operation=withdraw", text)
        self.assertIn("error=InsufficientFundsError", text)
        self.assertNotIn("novamarket.wallet.committed", text)

    async def test_an_idempotent_replay_is_logged_as_a_replay_not_a_commit(
        self,
    ) -> None:
        wallet_id = await self._wallet()
        first = await self.engine.deposit(wallet_id, "10.00", idempotency_key="k1")
        with self.assertLogs(self.LOGGER, level="INFO") as captured:
            second = await self.engine.deposit(wallet_id, "10.00", idempotency_key="k1")
        self.assertTrue(second["replayed"])
        self.assertEqual(
            first["transaction"]["transaction_id"],
            second["transaction"]["transaction_id"],
        )
        text = " ".join(captured.output)
        self.assertIn("novamarket.wallet.replayed", text)
        self.assertNotIn("novamarket.wallet.committed", text)
        self.assertEqual(self.engine.wallet(wallet_id)["wallet"]["balance"]["total"], "10.00")

    async def test_a_clean_reconciliation_is_logged_with_its_figures(self) -> None:
        wallet_id = await self._wallet()
        await self.engine.deposit(wallet_id, "10.00")
        with self.assertLogs(self.LOGGER, level="INFO") as captured:
            report = await self.engine.reconcile(wallet_id)
        self.assertTrue(report["ok"])
        text = " ".join(captured.output)
        self.assertIn("novamarket.ledger.reconciled scope=", text)
        self.assertIn("wallets=1", text)
        self.assertIn("ok=True", text)
        self.assertIn("ledger_sum=0.00", text)
        self.assertNotIn("integrity_violation", text)

    async def test_an_integrity_violation_is_logged_with_its_code(self) -> None:
        from datetime import UTC, datetime
        from unittest.mock import patch

        from novacontrol.novamarket.wallet.models import (
            ReconciliationIssue,
            ReconciliationReport,
            inr,
        )

        report = ReconciliationReport(
            wallet_id="wal_1",
            currency=Currency.INR,
            stored_balance=inr(Decimal("999.00")),
            ledger_balance=inr(Decimal("10.00")),
            difference=inr(Decimal("989.00")),
            ok=False,
            issues=(
                ReconciliationIssue(
                    code="balance_mismatch",
                    detail="stored 999.00 differs from the ledger 10.00",
                    wallet_id="wal_1",
                ),
            ),
            entries_checked=1,
            transactions_checked=1,
            wallets_checked=1,
            ledger_sum=inr(Decimal("0")),
            checked_at=datetime.now(UTC),
        )
        with patch.object(self.store, "reconcile", return_value=report):
            with self.assertLogs(self.LOGGER, level="INFO") as captured:
                body = await self.engine.reconcile()
        self.assertFalse(body["ok"])
        text = " ".join(captured.output)
        self.assertIn(
            "novamarket.ledger.integrity_violation code=balance_mismatch", text
        )
        self.assertIn("ok=False", text)


class LedgerGateAndReplayTests(unittest.IsolatedAsyncioTestCase):
    """A retry outranks wallet state; responses report what was applied."""

    async def asyncSetUp(self) -> None:
        self.store = WalletStore(":memory:")
        self.addCleanup(self.store.close)
        self.engine = WalletEngine(self.store)

    async def _wallet(self, owner: str = "alice") -> str:
        body = await self.engine.create_wallet(owner)
        return str(body["wallet"]["wallet_id"])

    async def test_a_replay_is_answered_after_the_wallet_is_frozen(self) -> None:
        wallet_id = await self._wallet()
        first = await self.engine.deposit(wallet_id, "10.00", idempotency_key="k1")
        await self.engine.set_wallet_status(wallet_id, "frozen", reason="review")
        again = await self.engine.deposit(wallet_id, "10.00", idempotency_key="k1")
        self.assertTrue(again["replayed"])
        self.assertEqual(
            first["transaction"]["transaction_id"],
            again["transaction"]["transaction_id"],
        )
        self.assertEqual(self.store.count_entries(wallet_id=wallet_id), 1)
        self.assertEqual(
            self.engine.wallet(wallet_id)["wallet"]["balance"]["total"], "10.00"
        )

    async def test_a_replayed_reversal_returns_the_original_reversal(self) -> None:
        wallet_id = await self._wallet()
        deposited = await self.engine.deposit(wallet_id, "10.00")
        original = deposited["transaction"]["transaction_id"]
        first = await self.engine.reverse(
            original, reason="duplicate", idempotency_key="rev-1"
        )
        again = await self.engine.reverse(
            original, reason="duplicate", idempotency_key="rev-1"
        )
        self.assertTrue(again["replayed"])
        self.assertEqual(
            first["transaction"]["transaction_id"],
            again["transaction"]["transaction_id"],
        )
        self.assertEqual(self.store.count_transactions(wallet_id=wallet_id), 2)
        self.assertEqual(
            self.engine.wallet(wallet_id)["wallet"]["balance"]["total"], "0.00"
        )

    async def test_reversing_a_deposit_whose_funds_left_is_refused(self) -> None:
        alice = await self._wallet("alice")
        bob = await self._wallet("bob")
        deposited = await self.engine.deposit(alice, "50.00")
        await self.engine.transfer(alice, bob, "50.00")
        before = self.store.count_transactions()
        with self.assertRaises(InsufficientFundsError):
            await self.engine.reverse(
                deposited["transaction"]["transaction_id"], reason="spent"
            )
        self.assertEqual(self.store.count_transactions(), before)
        self.assertFalse(
            self.store.transaction(deposited["transaction"]["transaction_id"]).reversed
        )
        self.assertEqual(
            self.engine.wallet(alice)["wallet"]["balance"]["total"], "0.00"
        )

    async def test_a_page_larger_than_the_cap_reports_the_applied_limit(self) -> None:
        wallet_id = await self._wallet()
        await self.engine.deposit(wallet_id, "10.00")
        body = self.engine.transactions(wallet_id=wallet_id, limit=9999)
        self.assertEqual(body["limit"], 500)
        self.assertLessEqual(len(body["transactions"]), 500)
        with self.assertRaises(WalletValidationError):
            self.engine.transactions(limit=0)

    async def test_status_reports_the_wallet_schema_version(self) -> None:
        self.assertEqual(self.engine.status()["schema_version"], WALLET_SCHEMA_VERSION)

    async def test_the_issuance_account_is_readable_but_never_listed(self) -> None:
        body = self.engine.wallet(self.store.system_wallet_id)
        self.assertTrue(body["wallet"]["system"])
        listed = {wallet["wallet_id"] for wallet in self.engine.wallets()["wallets"]}
        self.assertNotIn(self.store.system_wallet_id, listed)

    async def test_a_replay_from_another_owner_is_refused(self) -> None:
        wallet_id = await self._wallet("alice")
        await self.engine.deposit(
            wallet_id, "10.00", idempotency_key="k1", owner="alice"
        )
        with self.assertRaises(WalletValidationError):
            await self.engine.deposit(
                wallet_id, "10.00", idempotency_key="k1", owner="mallory"
            )
