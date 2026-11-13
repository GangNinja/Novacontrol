"""The durable NovaCoin wallet/ledger repository.

Why SQLite
----------
The ledger needs atomic multi-row writes, unique constraints that hold across
process restarts (idempotency), indexed range reads and a schema version. NM-02
already established SQLite as NovaMarket's durable engine for exactly those
reasons, so NM-03 adds tables in the same file and **no** second database engine.

Exact arithmetic, twice
-----------------------
Every amount is stored as ``Decimal``-derived TEXT for display **and** as an
integer number of paise for arithmetic. SQLite's numeric affinity is floating
point, so ``SUM()`` over decimal text would reintroduce the binary rounding the
ledger exists to avoid. Integer paise are exact, which makes three invariants
checkable in SQL rather than hoped for:

1. a wallet's stored balance equals ``SUM(signed_amount_paise)`` of its entries,
2. a transaction's entries sum to zero,
3. the whole ledger sums to zero (the issuance wallet carries the negative side).

Append-only, enforced by the database
-------------------------------------
``ledger_entries`` and ``wallet_transactions`` reject UPDATE and DELETE through
triggers. A correction is a new reversal transaction, so history cannot be
rewritten even by a bug in this package. ``wallets`` remains mutable for exactly
two things: its balance (written only together with the entries that justify it,
inside one transaction) and its status (freeze/close, which moves no money).

Concurrency
-----------
One connection guarded by a re-entrant lock, and every write runs inside
``BEGIN IMMEDIATE``. Validation happens *inside* that transaction, so two
withdrawals cannot both read the same balance and both succeed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Any

from novacontrol.novamarket.contracts import (
    IdempotencyConflictError,
    InsufficientFundsError,
    LedgerIntegrityError,
    WalletConflictError,
    WalletNotActiveError,
    WalletNotFoundError,
    WalletStoreError,
    WalletValidationError,
)
from novacontrol.novamarket.money import Currency, Money
from novacontrol.novamarket.wallet.models import (
    PAISE,
    SYSTEM_OWNER,
    EntryDirection,
    EntryType,
    LedgerEntry,
    PostingRequest,
    PostingResult,
    ReconciliationIssue,
    ReconciliationReport,
    TransactionType,
    Wallet,
    WalletStatus,
    WalletTransaction,
    amount_text,
    inr,
    new_id,
)

#: The durable schema this build writes. A file from a newer build is refused.
WALLET_SCHEMA_VERSION = 1

DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 500

#: A reconciliation refuses to read an unbounded ledger in one call.
MAX_RECONCILIATION_ENTRIES = 200_000


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _moment(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _paise(amount: Decimal | Money) -> int:
    """Exact integer paise for an amount already validated to 2 dp."""
    value = amount.amount if isinstance(amount, Money) else amount
    return int((value.quantize(PAISE) * 100).to_integral_value())


def _from_paise(paise: int) -> Money:
    return inr(Decimal(paise) / 100)


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS wallet_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wallets (
    wallet_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    system INTEGER NOT NULL DEFAULT 0,
    balance TEXT NOT NULL,
    balance_paise INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (currency = 'INR'),
    CHECK (status IN ('active', 'frozen', 'closed')),
    CHECK (system IN (0, 1)),
    CHECK (balance_paise >= 0 OR system = 1)
);

CREATE INDEX IF NOT EXISTS ix_wallets_owner ON wallets(owner);

CREATE TABLE IF NOT EXISTS wallet_transactions (
    transaction_id TEXT PRIMARY KEY,
    transaction_type TEXT NOT NULL,
    status TEXT NOT NULL,
    wallet_id TEXT NOT NULL REFERENCES wallets(wallet_id),
    counterparty_wallet_id TEXT REFERENCES wallets(wallet_id),
    amount TEXT NOT NULL,
    amount_paise INTEGER NOT NULL,
    currency TEXT NOT NULL,
    reference TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL DEFAULT '',
    reverses_transaction_id TEXT REFERENCES wallet_transactions(transaction_id),
    created_at TEXT NOT NULL,
    entry_count INTEGER NOT NULL,
    CHECK (currency = 'INR'),
    CHECK (transaction_type IN ('deposit', 'withdrawal', 'transfer', 'reversal', 'adjustment')),
    CHECK (status IN ('posted', 'reversed')),
    CHECK (amount_paise > 0),
    CHECK (entry_count >= 2)
);

CREATE INDEX IF NOT EXISTS ix_wallet_transactions_wallet
    ON wallet_transactions(wallet_id, created_at);
CREATE INDEX IF NOT EXISTS ix_wallet_transactions_counterparty
    ON wallet_transactions(counterparty_wallet_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_wallet_transactions_idempotency
    ON wallet_transactions(idempotency_key) WHERE idempotency_key <> '';
CREATE INDEX IF NOT EXISTS ix_wallet_transactions_reverses
    ON wallet_transactions(reverses_transaction_id);

CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES wallet_transactions(transaction_id),
    wallet_id TEXT NOT NULL REFERENCES wallets(wallet_id),
    entry_type TEXT NOT NULL,
    direction TEXT NOT NULL,
    amount TEXT NOT NULL,
    amount_paise INTEGER NOT NULL,
    signed_amount TEXT NOT NULL,
    signed_amount_paise INTEGER NOT NULL,
    balance_after TEXT NOT NULL,
    balance_after_paise INTEGER NOT NULL,
    currency TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    reference TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    CHECK (currency = 'INR'),
    CHECK (direction IN ('debit', 'credit')),
    CHECK (entry_type IN ('deposit', 'withdrawal', 'transfer', 'reversal', 'adjustment')),
    CHECK (amount_paise > 0),
    CHECK (
        (direction = 'debit' AND signed_amount_paise = -amount_paise)
        OR (direction = 'credit' AND signed_amount_paise = amount_paise)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_entries_sequence
    ON ledger_entries(transaction_id, sequence);
CREATE INDEX IF NOT EXISTS ix_ledger_entries_wallet
    ON ledger_entries(wallet_id, created_at);

CREATE TABLE IF NOT EXISTS wallet_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES wallet_transactions(transaction_id),
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trg_ledger_entries_no_update
BEFORE UPDATE ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger entries are append-only; post a reversal instead');
END;

CREATE TRIGGER IF NOT EXISTS trg_ledger_entries_no_delete
BEFORE DELETE ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger entries are append-only; history is never deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_wallet_transactions_no_update
BEFORE UPDATE ON wallet_transactions
BEGIN
    SELECT RAISE(ABORT, 'wallet transactions are append-only; post a reversal instead');
END;

CREATE TRIGGER IF NOT EXISTS trg_wallet_transactions_no_delete
BEFORE DELETE ON wallet_transactions
BEGIN
    SELECT RAISE(ABORT, 'wallet transactions are append-only; history is never deleted');
END;
"""


class WalletStore:
    """SQLite-backed wallets, transactions and the append-only ledger."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._lock = RLock()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._path = str(path)
        try:
            self._connection = sqlite3.connect(
                self._path, isolation_level=None, check_same_thread=False
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            if self._path != ":memory:":
                self._connection.execute("PRAGMA journal_mode = WAL")
                self._connection.execute("PRAGMA synchronous = FULL")
            self._ensure_schema()
        except sqlite3.Error as exc:  # pragma: no cover - environment failure
            self._connection.close()
            raise WalletStoreError(f"could not open the wallet ledger: {exc}") from exc
        except WalletStoreError:
            # A refused schema version is not a usable store: releasing the file
            # handle here keeps a later migration (or a retry) from meeting a
            # locked database on Windows.
            self._connection.close()
            raise
        self._system_wallet_id = self.ensure_system_wallet().wallet_id

    # -- lifecycle ------------------------------------------------------------

    @property
    def path(self) -> str:
        return self._path

    @property
    def schema_version(self) -> int:
        """The wallet schema version recorded in this database."""
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM wallet_meta WHERE key = 'schema_version'"
            ).fetchone()
        return int(row["value"]) if row is not None else WALLET_SCHEMA_VERSION

    @property
    def system_wallet_id(self) -> str:
        """The simulated issuance account every deposit/withdrawal balances to."""
        return self._system_wallet_id

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _ensure_schema(self) -> None:
        """Create the wallet tables, or verify the version of existing ones.

        The version lives in ``wallet_meta`` rather than ``PRAGMA user_version``
        because NM-02's market-data store owns that pragma in the SAME database
        file. Two subsystems sharing one pragma would overwrite each other's
        schema number and then read the wrong contract — the ledger keeps its own
        version key instead.
        """
        with self._lock:
            tables = {
                str(row[0])
                for row in self._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "wallet_meta" not in tables:
                self._connection.executescript(_SCHEMA_V1)
                self._connection.execute(
                    "INSERT OR REPLACE INTO wallet_meta (key, value) VALUES (?, ?)",
                    ("schema_version", str(WALLET_SCHEMA_VERSION)),
                )
                return
            row = self._connection.execute(
                "SELECT value FROM wallet_meta WHERE key = ?", ("schema_version",)
            ).fetchone()
            version = int(row["value"]) if row is not None else 0
            if version > WALLET_SCHEMA_VERSION:
                raise WalletStoreError(
                    f"wallet ledger schema is version {version} but this build "
                    f"speaks {WALLET_SCHEMA_VERSION}; upgrade NovaControl instead "
                    "of reading a newer ledger optimistically"
                )
            if version < WALLET_SCHEMA_VERSION:
                raise WalletStoreError(
                    f"wallet ledger schema {version} predates this build's "
                    f"{WALLET_SCHEMA_VERSION}; run the ledger migration instead of "
                    "reading it with the wrong contract"
                )

    def _begin(self) -> None:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise WalletStoreError(f"could not start a ledger transaction: {exc}") from exc

    # -- wallets --------------------------------------------------------------

    def ensure_system_wallet(self, currency: Currency = Currency.INR) -> Wallet:
        """Return the issuance wallet, creating it on first use."""
        with self._lock:
            row = self._connection.execute(
                "SELECT wallet_id FROM wallets WHERE system = 1 AND owner = ?",
                (SYSTEM_OWNER,),
            ).fetchone()
            if row is not None:
                return self.wallet(str(row["wallet_id"]))
            return self.create_wallet(
                SYSTEM_OWNER,
                label="NovaCoin issuance",
                currency=currency,
                system=True,
                wallet_id=f"wal_{SYSTEM_OWNER.split(':')[-1]}_inr",
            )

    def create_wallet(
        self,
        owner: str,
        *,
        label: str = "",
        currency: Currency = Currency.INR,
        wallet_id: str | None = None,
        system: bool = False,
    ) -> Wallet:
        """Create a wallet with a zero balance; never with an opening balance."""
        owner_text = str(owner or "").strip()
        if not owner_text:
            raise WalletValidationError("a wallet needs an owner reference")
        if owner_text == SYSTEM_OWNER and not system:
            raise WalletValidationError(
                f"{SYSTEM_OWNER!r} is reserved for the simulated issuance account"
            )
        if Currency(currency) is not Currency.INR:
            raise WalletValidationError(
                "NovaCoin wallets are INR wallets (1 NC = 1 INR)"
            )
        identifier = str(wallet_id or new_id("wal")).strip()
        now = self._clock()
        with self._lock:
            try:
                self._connection.execute(
                    """
                    INSERT INTO wallets (wallet_id, owner, label, currency, status,
                        system, balance, balance_paise, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        owner_text,
                        str(label or "").strip(),
                        Currency.INR.value,
                        WalletStatus.ACTIVE.value,
                        1 if system else 0,
                        "0.00",
                        0,
                        _iso(now),
                        _iso(now),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise WalletConflictError(
                    f"wallet {identifier!r} already exists"
                ) from exc
            except sqlite3.Error as exc:
                raise WalletStoreError(f"could not create the wallet: {exc}") from exc
        return self.wallet(identifier)

    def wallet(self, wallet_id: str) -> Wallet:
        with self._lock:
            return self._wallet_locked(wallet_id)

    def _wallet_locked(self, wallet_id: str) -> Wallet:
        row = self._connection.execute(
            "SELECT * FROM wallets WHERE wallet_id = ?", (str(wallet_id),)
        ).fetchone()
        if row is None:
            raise WalletNotFoundError(f"no wallet with id {wallet_id!r}")
        return _wallet_from_row(row)

    def applied_limit(self, limit: int) -> int:
        """The page size this store will actually apply for ``limit``."""
        return _page_limit(limit)

    def wallets(
        self,
        *,
        owner: str | None = None,
        include_system: bool = False,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> tuple[Wallet, ...]:
        clause = "" if include_system else " AND system = 0"
        params: list[Any] = []
        if owner is not None:
            clause += " AND owner = ?"
            params.append(str(owner).strip())
        params.extend([_page_limit(limit), _offset(offset)])
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM wallets WHERE 1 = 1{clause} "
                "ORDER BY created_at, wallet_id LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return tuple(_wallet_from_row(row) for row in rows)

    def count_wallets(self, *, include_system: bool = False) -> int:
        clause = "" if include_system else " WHERE system = 0"
        with self._lock:
            return int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM wallets{clause}"
                ).fetchone()[0]
            )

    def set_status(
        self, wallet_id: str, status: WalletStatus | str, *, reason: str = ""
    ) -> Wallet:
        """Freeze or close a wallet. Closing requires a zero balance."""
        wanted = WalletStatus(status)
        with self._lock:
            wallet = self._wallet_locked(wallet_id)
            if wallet.system:
                raise WalletConflictError(
                    "the issuance wallet's status is managed by the ledger, not the API"
                )
            if wanted is WalletStatus.CLOSED and wallet.balance.amount != 0:
                raise WalletConflictError(
                    f"wallet {wallet_id} still holds {amount_text(wallet.balance)} NC; "
                    "withdraw it before closing the wallet"
                )
            self._connection.execute(
                "UPDATE wallets SET status = ?, updated_at = ? WHERE wallet_id = ?",
                (wanted.value, _iso(self._clock()), wallet.wallet_id),
            )
            del reason  # the reason is published as an event, not stored here
            return self._wallet_locked(wallet.wallet_id)

    # -- posting --------------------------------------------------------------

    def post(self, request: PostingRequest) -> PostingResult:
        """Commit one balanced transaction atomically, or change nothing."""
        legs = tuple(request.legs)
        if not legs:
            raise LedgerIntegrityError("a transaction must post at least two entries")
        if len(legs) < 2:
            raise LedgerIntegrityError(
                "a transaction must post at least two entries so it balances"
            )
        total = sum((leg.signed() for leg in legs), Decimal("0"))
        if total != 0:
            raise LedgerIntegrityError(
                f"transaction legs sum to {total} instead of 0; refusing to post an "
                "unbalanced ledger transaction"
            )
        currency = Currency(request.currency)
        if currency is not Currency.INR:
            raise WalletValidationError("NovaCoin transactions are INR transactions")

        with self._lock:
            self._begin()
            try:
                replay = self._idempotent_replay(request)
                if replay is not None:
                    self._connection.execute("ROLLBACK")
                    return replay

                wallets: dict[str, Wallet] = {}
                for leg in legs:
                    if leg.wallet_id not in wallets:
                        wallets[leg.wallet_id] = self._wallet_locked(leg.wallet_id)

                debits: dict[str, Decimal] = {}
                for leg in legs:
                    if EntryDirection(leg.direction) is EntryDirection.DEBIT:
                        debits[leg.wallet_id] = (
                            debits.get(leg.wallet_id, Decimal("0")) + leg.amount
                        )

                for wallet_id, debit in debits.items():
                    wallet = wallets[wallet_id]
                    if wallet.system:
                        continue
                    if not wallet.active:
                        raise WalletNotActiveError(
                            f"wallet {wallet_id} is {wallet.status.value}; money may "
                            "only move through an active wallet"
                        )
                    if wallet.balance.amount - debit < 0:
                        raise InsufficientFundsError(
                            wallet_id,
                            amount_text(wallet.balance),
                            amount_text(debit),
                        )
                for leg in legs:
                    wallet = wallets[leg.wallet_id]
                    if not wallet.system and not wallet.active:
                        raise WalletNotActiveError(
                            f"wallet {wallet.wallet_id} is "
                            f"{wallet.status.value}; money may only move through an "
                            "active wallet"
                        )

                now = self._clock()
                transaction_id = new_id("txn")
                amount_paise = _paise(Decimal(request.amount))
                self._connection.execute(
                    """
                    INSERT INTO wallet_transactions (transaction_id,
                        transaction_type, status, wallet_id, counterparty_wallet_id,
                        amount, amount_paise, currency, reference, description,
                        idempotency_key, reverses_transaction_id, created_at,
                        entry_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        transaction_id,
                        TransactionType(request.transaction_type).value,
                        "posted",
                        request.wallet_id,
                        request.counterparty_wallet_id,
                        amount_text(Decimal(request.amount)),
                        amount_paise,
                        Currency.INR.value,
                        str(request.reference or "").strip(),
                        str(request.description or "").strip(),
                        str(request.idempotency_key or "").strip(),
                        request.reverses_transaction_id,
                        _iso(now),
                        len(legs),
                    ),
                )

                running: dict[str, int] = {
                    wallet_id: _paise(wallet.balance.amount)
                    for wallet_id, wallet in wallets.items()
                }
                for sequence, leg in enumerate(legs):
                    wallet = wallets[leg.wallet_id]
                    signed_paise = (
                        -_paise(leg.amount)
                        if EntryDirection(leg.direction) is EntryDirection.DEBIT
                        else _paise(leg.amount)
                    )
                    running[leg.wallet_id] += signed_paise
                    self._connection.execute(
                        """
                        INSERT INTO ledger_entries (entry_id, transaction_id,
                            wallet_id, entry_type, direction, amount, amount_paise,
                            signed_amount, signed_amount_paise, balance_after,
                            balance_after_paise, currency, sequence, description,
                            reference, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_id("led"),
                            transaction_id,
                            leg.wallet_id,
                            EntryType(leg.entry_type).value,
                            EntryDirection(leg.direction).value,
                            amount_text(leg.amount),
                            _paise(leg.amount),
                            amount_text(Decimal(signed_paise) / 100),
                            signed_paise,
                            amount_text(Decimal(running[leg.wallet_id]) / 100),
                            running[leg.wallet_id],
                            Currency.INR.value,
                            sequence,
                            str(leg.description or request.description or "").strip(),
                            str(leg.reference or request.reference or "").strip(),
                            _iso(now),
                        ),
                    )
                    del wallet  # balance is written once per wallet below

                for wallet_id, paise in running.items():
                    self._connection.execute(
                        "UPDATE wallets SET balance = ?, balance_paise = ?, "
                        "updated_at = ? WHERE wallet_id = ?",
                        (
                            amount_text(Decimal(paise) / 100),
                            paise,
                            _iso(now),
                            wallet_id,
                        ),
                    )

                if request.idempotency_key:
                    self._connection.execute(
                        """
                        INSERT INTO wallet_idempotency (idempotency_key,
                            transaction_id, fingerprint, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            str(request.idempotency_key).strip(),
                            transaction_id,
                            request.fingerprint,
                            _iso(now),
                        ),
                    )

                self._verify_balance(running)
                self._connection.execute("COMMIT")
            except BaseException:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover - already rolled back
                    pass
                raise

        wallet_after = {wallet_id: self.wallet(wallet_id) for wallet_id in running}
        return PostingResult(
            transaction=self.transaction(transaction_id),
            entries=self.entries(transaction_id=transaction_id, ascending=True),
            wallets=wallet_after,
            replayed=False,
        )

    def replay(
        self, *, idempotency_key: str, fingerprint: str
    ) -> PostingResult | None:
        """The earlier result for a repeated key, before any wallet-state gate.

        A retry has to be answerable even when the wallet has been frozen since
        the commit: the original transaction *is* the answer, and nothing moves.
        Looking the key up here — not inside ``post``'s state checks — is what
        keeps an idempotent retry from turning into a refusal.
        """
        key = str(idempotency_key or "").strip()
        if not key:
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT transaction_id, fingerprint FROM wallet_idempotency "
                "WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            if str(row["fingerprint"]) != fingerprint:
                raise IdempotencyConflictError(
                    f"idempotency key {key!r} was already used for a different "
                    "request; reusing a key for different money would hide a "
                    "second operation"
                )
            return self._replay_result(str(row["transaction_id"]))

    def _replay_result(self, transaction_id: str) -> PostingResult:
        entries = self.entries(transaction_id=transaction_id, ascending=True)
        wallets: dict[str, Wallet] = {}
        for entry in entries:
            wallets.setdefault(entry.wallet_id, self.wallet(entry.wallet_id))
        return PostingResult(
            transaction=self.transaction(transaction_id),
            entries=entries,
            wallets=wallets,
            replayed=True,
        )

    def transaction_for_key(self, idempotency_key: str) -> WalletTransaction | None:
        """Read the transaction a key already committed, without posting anything.

        Recovery needs to find a transfer that committed before its caller could
        persist the returned transaction id.  This is a read-only lookup; it must
        never make a missing operation appear by calling ``post``.
        """
        key = str(idempotency_key or "").strip()
        if not key:
            return None
        with self._lock:
            row = self._connection.execute(
                _TRANSACTION_SELECT + " WHERE t.idempotency_key = ?", (key,)
            ).fetchone()
        return _transaction_from_row(row) if row is not None else None

    def _idempotent_replay(self, request: PostingRequest) -> PostingResult | None:
        """The earlier result for a repeated idempotency key, or refuse the reuse."""
        return self.replay(
            idempotency_key=request.idempotency_key, fingerprint=request.fingerprint
        )

    def _verify_balance(self, expected: Mapping[str, int]) -> None:
        """Refuse to commit unless every balance equals its ledger sum."""
        for wallet_id, paise in expected.items():
            row = self._connection.execute(
                "SELECT COALESCE(SUM(signed_amount_paise), 0) FROM ledger_entries "
                "WHERE wallet_id = ?",
                (wallet_id,),
            ).fetchone()
            ledger_paise = int(row[0])
            if ledger_paise != paise:
                raise LedgerIntegrityError(
                    f"wallet {wallet_id} would hold {paise} paise but its entries sum "
                    f"to {ledger_paise}; refusing to commit an inconsistent balance"
                )

    # -- reads ----------------------------------------------------------------

    def transaction(self, transaction_id: str) -> WalletTransaction:
        with self._lock:
            row = self._connection.execute(
                _TRANSACTION_SELECT + " WHERE t.transaction_id = ?",
                (str(transaction_id),),
            ).fetchone()
        if row is None:
            raise WalletNotFoundError(
                f"no wallet transaction with id {transaction_id!r}"
            )
        return _transaction_from_row(row)

    def transactions(
        self,
        *,
        wallet_id: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        ascending: bool = False,
    ) -> tuple[WalletTransaction, ...]:
        clause = ""
        params: list[Any] = []
        if wallet_id is not None:
            clause = " WHERE t.wallet_id = ? OR t.counterparty_wallet_id = ?"
            params.extend([str(wallet_id), str(wallet_id)])
        order = "ASC" if ascending else "DESC"
        params.extend([_page_limit(limit), _offset(offset)])
        with self._lock:
            rows = self._connection.execute(
                f"{_TRANSACTION_SELECT}{clause} "
                f"ORDER BY t.created_at {order}, t.transaction_id {order} "
                "LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return tuple(_transaction_from_row(row) for row in rows)

    def count_transactions(self, *, wallet_id: str | None = None) -> int:
        clause = ""
        params: list[Any] = []
        if wallet_id is not None:
            clause = " WHERE wallet_id = ? OR counterparty_wallet_id = ?"
            params.extend([str(wallet_id), str(wallet_id)])
        with self._lock:
            return int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM wallet_transactions{clause}", params
                ).fetchone()[0]
            )

    def entries(
        self,
        *,
        wallet_id: str | None = None,
        transaction_id: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        ascending: bool = True,
    ) -> tuple[LedgerEntry, ...]:
        clause, params = _entry_filter(wallet_id=wallet_id, transaction_id=transaction_id)
        order = "ASC" if ascending else "DESC"
        params.extend([_page_limit(limit), _offset(offset)])
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM ledger_entries{clause} "
                f"ORDER BY created_at {order}, sequence {order} LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return tuple(_entry_from_row(row) for row in rows)

    def count_entries(
        self, *, wallet_id: str | None = None, transaction_id: str | None = None
    ) -> int:
        clause, params = _entry_filter(wallet_id=wallet_id, transaction_id=transaction_id)
        with self._lock:
            return int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM ledger_entries{clause}", params
                ).fetchone()[0]
            )

    def iter_entries(self, *, wallet_id: str | None = None) -> Iterator[LedgerEntry]:
        clause, params = _entry_filter(wallet_id=wallet_id, transaction_id=None)
        with self._lock:
            cursor = self._connection.execute(
                f"SELECT * FROM ledger_entries{clause} ORDER BY created_at, sequence",
                params,
            )
            rows = cursor.fetchall()
        for row in rows:
            yield _entry_from_row(row)

    def summary(self) -> dict[str, Any]:
        """Ledger totals in exact paise, for status and reconciliation."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM wallets WHERE system = 0) AS wallets,
                    (SELECT COUNT(*) FROM wallets WHERE system = 1) AS system_wallets,
                    (SELECT COUNT(*) FROM wallet_transactions) AS transactions,
                    (SELECT COUNT(*) FROM ledger_entries) AS entries,
                    (SELECT COALESCE(SUM(signed_amount_paise), 0) FROM ledger_entries)
                        AS ledger_paise,
                    -- Portfolio funding wallets are owned by the subsystem, not
                    -- by the person: counting them here reported the user's
                    -- balance as wallet + portfolio capital (₹50,000 instead of
                    -- ₹30,000 after a ₹20,000 book was funded).
                    (SELECT COALESCE(SUM(balance_paise), 0) FROM wallets
                        WHERE system = 0
                          AND owner NOT LIKE 'novamarket:%') AS user_paise
                """
            ).fetchone()
        return {
            "wallets": int(row["wallets"]),
            "system_wallets": int(row["system_wallets"]),
            "transactions": int(row["transactions"]),
            "ledger_entries": int(row["entries"]),
            "ledger_paise": int(row["ledger_paise"]),
            "ledger_sum": amount_text(Decimal(int(row["ledger_paise"])) / 100),
            "user_balance": amount_text(Decimal(int(row["user_paise"])) / 100),
            "currency": Currency.INR.value,
        }

    # -- reconciliation -------------------------------------------------------

    def reconcile(
        self,
        wallet_id: str | None = None,
        *,
        wallets_checked: int = 0,
    ) -> ReconciliationReport:
        """Independently recompute balances from the ledger and compare.

        Nothing is repaired here. Every problem becomes an issue with the evidence
        that shows it, and the original rows stay exactly as they were.
        """
        with self._lock:
            if wallet_id is None:
                wallets = self.wallets(include_system=True, limit=MAX_PAGE_LIMIT)
                if len(wallets) >= MAX_PAGE_LIMIT:
                    return self._bounded_report(len(wallets))
                targets = wallets
            else:
                targets = (self._wallet_locked(wallet_id),)
            issues: list[ReconciliationIssue] = []
            entries_checked = 0
            transactions_checked = 0

            global_row = self._connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(signed_amount_paise), 0) "
                "FROM ledger_entries"
            ).fetchone()
            entries_checked = int(global_row[0])
            ledger_paise = int(global_row[1])
            if entries_checked > MAX_RECONCILIATION_ENTRIES:
                return self._bounded_report(entries_checked)

            if ledger_paise != 0:
                issues.append(
                    ReconciliationIssue(
                        code="ledger_not_zero_sum",
                        detail=(
                            "the signed sum of every ledger entry is "
                            f"{amount_text(Decimal(ledger_paise) / 100)} NC instead of "
                            "0; every posted transaction must balance"
                        ),
                        evidence={"ledger_sum_paise": ledger_paise},
                    )
                )

            unbalanced = self._connection.execute(
                """
                SELECT transaction_id, SUM(signed_amount_paise) AS total
                FROM ledger_entries GROUP BY transaction_id HAVING total <> 0
                """
            ).fetchall()
            for row in unbalanced:
                issues.append(
                    ReconciliationIssue(
                        code="unbalanced_transaction",
                        detail=(
                            f"transaction {row['transaction_id']} has entries summing to "
                            f"{amount_text(Decimal(int(row['total'])) / 100)} NC, not 0"
                        ),
                        transaction_id=str(row["transaction_id"]),
                        evidence={"total_paise": int(row["total"])},
                    )
                )

            for transaction in self._all_transactions():
                transactions_checked += 1
                count_row = self._connection.execute(
                    "SELECT COUNT(*) FROM ledger_entries WHERE transaction_id = ?",
                    (transaction.transaction_id,),
                ).fetchone()
                actual = int(count_row[0])
                if actual != transaction.entry_count:
                    issues.append(
                        ReconciliationIssue(
                            code="entry_count_mismatch",
                            detail=(
                                f"transaction {transaction.transaction_id} records "
                                f"{transaction.entry_count} entries but has {actual}"
                            ),
                            transaction_id=transaction.transaction_id,
                            evidence={
                                "recorded": transaction.entry_count,
                                "actual": actual,
                            },
                        )
                    )
                if actual == 0:
                    issues.append(
                        ReconciliationIssue(
                            code="orphan_transaction",
                            detail=(
                                f"transaction {transaction.transaction_id} has no "
                                "ledger entries"
                            ),
                            transaction_id=transaction.transaction_id,
                        )
                    )

            duplicates = self._connection.execute(
                """
                SELECT transaction_id, wallet_id, direction, amount_paise, COUNT(*) AS n
                FROM ledger_entries
                GROUP BY transaction_id, wallet_id, direction, amount_paise
                HAVING n > 1
                """
            ).fetchall()
            for row in duplicates:
                issues.append(
                    ReconciliationIssue(
                        code="duplicate_entry",
                        detail=(
                            "identical entries exist for transaction "
                            f"{row['transaction_id']} on wallet {row['wallet_id']}"
                        ),
                        wallet_id=str(row["wallet_id"]),
                        transaction_id=str(row["transaction_id"]),
                        evidence={"count": int(row["n"])},
                    )
                )

            for wallet in targets:
                stored_paise = _paise(wallet.balance.amount)
                sum_row = self._connection.execute(
                    "SELECT COALESCE(SUM(signed_amount_paise), 0) FROM ledger_entries "
                    "WHERE wallet_id = ?",
                    (wallet.wallet_id,),
                ).fetchone()
                computed = int(sum_row[0])
                if computed != stored_paise:
                    issues.append(
                        ReconciliationIssue(
                            code="balance_mismatch",
                            detail=(
                                f"wallet {wallet.wallet_id} stores "
                                f"{amount_text(Decimal(stored_paise) / 100)} NC but its "
                                f"ledger entries sum to "
                                f"{amount_text(Decimal(computed) / 100)} NC"
                            ),
                            wallet_id=wallet.wallet_id,
                            evidence={
                                "stored_paise": stored_paise,
                                "ledger_paise": computed,
                            },
                        )
                    )
                if not wallet.system and stored_paise < 0:
                    issues.append(
                        ReconciliationIssue(
                            code="negative_balance",
                            detail=(
                                f"wallet {wallet.wallet_id} holds a negative balance"
                            ),
                            wallet_id=wallet.wallet_id,
                            evidence={"stored_paise": stored_paise},
                        )
                    )

            primary = targets[0] if len(targets) == 1 else None
            stored_total = sum(
                (_paise(wallet.balance.amount) for wallet in targets), 0
            )
            computed_total = 0
            for wallet in targets:
                computed_total += int(
                    self._connection.execute(
                        "SELECT COALESCE(SUM(signed_amount_paise), 0) FROM "
                        "ledger_entries WHERE wallet_id = ?",
                        (wallet.wallet_id,),
                    ).fetchone()[0]
                )
            ok = not issues
            now = self._clock()
            return ReconciliationReport(
                wallet_id=primary.wallet_id if primary is not None else "",
                currency=Currency.INR,
                stored_balance=_from_paise(stored_total),
                ledger_balance=_from_paise(computed_total),
                difference=_from_paise(stored_total - computed_total),
                ok=ok,
                issues=tuple(issues),
                entries_checked=entries_checked,
                transactions_checked=transactions_checked,
                wallets_checked=wallets_checked or len(targets),
                ledger_sum=_from_paise(ledger_paise),
                checked_at=now,
            )

    def _all_transactions(self) -> tuple[WalletTransaction, ...]:
        rows = self._connection.execute(_TRANSACTION_SELECT).fetchall()
        return tuple(_transaction_from_row(row) for row in rows)

    def _bounded_report(self, size: int) -> ReconciliationReport:
        return ReconciliationReport(
            wallet_id="",
            currency=Currency.INR,
            stored_balance=inr(Decimal("0")),
            ledger_balance=inr(Decimal("0")),
            difference=inr(Decimal("0")),
            ok=False,
            issues=(
                ReconciliationIssue(
                    code="ledger_too_large",
                    detail=(
                        f"the ledger holds {size} rows, above the "
                        f"{MAX_RECONCILIATION_ENTRIES} reconciliation bound; run a "
                        "bounded reconciliation per wallet instead"
                    ),
                ),
            ),
            entries_checked=0,
            transactions_checked=0,
            wallets_checked=0,
            ledger_sum=inr(Decimal("0")),
            checked_at=self._clock(),
        )


_TRANSACTION_SELECT = """
SELECT t.*, (
    SELECT r.transaction_id FROM wallet_transactions r
    WHERE r.reverses_transaction_id = t.transaction_id
    ORDER BY r.created_at LIMIT 1
) AS reversed_by
FROM wallet_transactions t
"""


def _entry_filter(
    *, wallet_id: str | None, transaction_id: str | None
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if wallet_id is not None:
        clauses.append("wallet_id = ?")
        params.append(str(wallet_id))
    if transaction_id is not None:
        clauses.append("transaction_id = ?")
        params.append(str(transaction_id))
    clause = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return clause, params


def _page_limit(limit: int) -> int:
    value = int(limit)
    if value < 1:
        raise WalletValidationError("limit must be at least 1")
    return min(value, MAX_PAGE_LIMIT)


def _offset(offset: int) -> int:
    value = int(offset)
    if value < 0:
        raise WalletValidationError("offset must not be negative")
    return value


def _wallet_from_row(row: sqlite3.Row) -> Wallet:
    return Wallet(
        wallet_id=str(row["wallet_id"]),
        owner=str(row["owner"]),
        currency=Currency(str(row["currency"])),
        status=WalletStatus(str(row["status"])),
        balance=inr(Decimal(str(row["balance"]))),
        label=str(row["label"]),
        system=bool(int(row["system"])),
        created_at=_moment(str(row["created_at"])),
        updated_at=_moment(str(row["updated_at"])),
    )


def _transaction_from_row(row: sqlite3.Row) -> WalletTransaction:
    reversed_by = row["reversed_by"] if "reversed_by" in row.keys() else None
    reverses = row["reverses_transaction_id"]
    return WalletTransaction(
        transaction_id=str(row["transaction_id"]),
        transaction_type=TransactionType(str(row["transaction_type"])),
        wallet_id=str(row["wallet_id"]),
        amount=inr(Decimal(str(row["amount"]))),
        created_at=_moment(str(row["created_at"])),
        counterparty_wallet_id=(
            str(row["counterparty_wallet_id"])
            if row["counterparty_wallet_id"]
            else None
        ),
        reference=str(row["reference"]),
        description=str(row["description"]),
        idempotency_key=str(row["idempotency_key"]),
        reverses_transaction_id=str(reverses) if reverses else None,
        entry_count=int(row["entry_count"]),
        reversed_by=str(reversed_by) if reversed_by else None,
    )


def _entry_from_row(row: sqlite3.Row) -> LedgerEntry:
    return LedgerEntry(
        entry_id=str(row["entry_id"]),
        transaction_id=str(row["transaction_id"]),
        wallet_id=str(row["wallet_id"]),
        entry_type=EntryType(str(row["entry_type"])),
        direction=EntryDirection(str(row["direction"])),
        amount=inr(Decimal(str(row["amount"]))),
        signed_amount=inr(Decimal(str(row["signed_amount"]))),
        balance_after=inr(Decimal(str(row["balance_after"]))),
        sequence=int(row["sequence"]),
        created_at=_moment(str(row["created_at"])),
        description=str(row["description"]),
        reference=str(row["reference"]),
    )


def open_wallet_store(
    data_dir: str | Path | None,
    *,
    filename: str = "novamarket.sqlite3",
    clock: Callable[[], datetime] | None = None,
    path: str | Path | None = None,
) -> WalletStore:
    """Open the ledger in the application's data directory (or in memory)."""
    if path is not None:
        return WalletStore(path, clock=clock)
    if data_dir is None:
        return WalletStore(":memory:", clock=clock)
    return WalletStore(Path(data_dir) / filename, clock=clock)


__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "MAX_RECONCILIATION_ENTRIES",
    "WALLET_SCHEMA_VERSION",
    "WalletStore",
    "open_wallet_store",
]
