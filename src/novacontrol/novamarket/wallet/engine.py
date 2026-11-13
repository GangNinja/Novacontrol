"""NM-03 transaction engine: the only door money moves through.

Lifecycle of every request, in this order:

``REQUEST → VALIDATION → IDEMPOTENCY → BALANCE/OWNERSHIP → ATOMIC WRITE →
RECONCILIATION → SUCCESS/FAILURE``

The store owns steps 3-6 (its ``post`` validates inside one ``BEGIN IMMEDIATE``
transaction and refuses to commit unless each wallet's balance equals its ledger
sum). This engine owns the request-level rules — wallet status, ownership, amount
precision, self-transfers, reversal eligibility — and publishes events **after**
the commit, so a subscriber can never observe money that was rolled back.

Balance changes always arrive through a ledger-backed transaction: there is no
``set_balance`` here, and the API has no route that could reach one.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from novacontrol.core.events import EventType
from novacontrol.novamarket.contracts import (
    InsufficientFundsError,
    LedgerIntegrityError,
    WalletConflictError,
    WalletError,
    WalletNotFoundError,
    WalletNotActiveError,
    WalletValidationError,
)
from novacontrol.novamarket.wallet.models import (
    EntryDirection,
    EntryType,
    LedgerLeg,
    PostingRequest,
    PostingResult,
    ReconciliationReport,
    TransactionType,
    Wallet,
    WalletStatus,
    WalletTransaction,
    amount_text,
    parse_amount,
    request_fingerprint,
)
from novacontrol.novamarket.wallet.store import (
    DEFAULT_PAGE_LIMIT,
    WalletStore,
)

_LOGGER = logging.getLogger(__name__)


class EventPublisher(Protocol):
    """The publish seam; :class:`MarketEventPublisher` satisfies it."""

    async def publish(
        self,
        event_type: EventType | str,
        /,
        *,
        correlation_id: str = "",
        causation_id: str | None = None,
        **payload: Any,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class WalletOperationResult:
    """One committed money movement, with both sides of what it wrote."""

    transaction: WalletTransaction
    entries: tuple[Any, ...]
    wallets: Mapping[str, Wallet]
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "replayed": self.replayed,
            "transaction": self.transaction.to_dict(),
            "entries": [entry.to_dict() for entry in self.entries],
            "wallets": {
                wallet_id: wallet.to_dict()
                for wallet_id, wallet in sorted(self.wallets.items())
            },
        }


class WalletEngine:
    """Wallets, deposits, withdrawals, transfers, reversals and reconciliation."""

    def __init__(
        self,
        store: WalletStore,
        *,
        publisher: EventPublisher | None = None,
        clock: Any | None = None,
    ) -> None:
        self._store = store
        self._publisher = publisher
        self._clock = clock or (lambda: datetime.now(UTC))
        self._last_error = ""
        self._last_event_error = ""
        self._reconciliations = 0
        self._last_report: ReconciliationReport | None = None

    # -- access ---------------------------------------------------------------

    @property
    def store(self) -> WalletStore:
        return self._store

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def last_event_error(self) -> str:
        return self._last_event_error

    def set_publisher(self, publisher: EventPublisher | None) -> None:
        self._publisher = publisher

    # -- wallets --------------------------------------------------------------

    async def create_wallet(
        self,
        owner: str,
        *,
        label: str = "",
        wallet_id: str | None = None,
    ) -> dict[str, Any]:
        wallet = self._store.create_wallet(owner, label=label, wallet_id=wallet_id)
        await self._publish(
            EventType.NOVAMARKET_WALLET_CREATED,
            wallet_id=wallet.wallet_id,
            owner=wallet.owner,
        )
        return {"ok": True, "wallet": wallet.to_dict()}

    def wallet(self, wallet_id: str) -> dict[str, Any]:
        """One wallet with its balances and the ledger count behind them."""
        wallet = self._store.wallet(wallet_id)
        return {
            "ok": True,
            "wallet": wallet.to_dict(),
            "ledger_entries": self._store.count_entries(wallet_id=wallet.wallet_id),
            "transactions": self._store.count_transactions(wallet_id=wallet.wallet_id),
        }

    def wallets(
        self,
        *,
        owner: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        rows = self._store.wallets(owner=owner, limit=limit, offset=offset)
        return {
            "ok": True,
            "wallets": [wallet.to_dict() for wallet in rows],
            "total": self._store.count_wallets(),
            "limit": self._store.applied_limit(limit),
            "offset": offset,
            "currency": "INR",
        }

    async def set_wallet_status(
        self,
        wallet_id: str,
        status: WalletStatus | str,
        *,
        reason: str = "",
    ) -> dict[str, Any]:
        wallet = self._store.set_status(wallet_id, status, reason=reason)
        return {"ok": True, "wallet": wallet.to_dict(), "reason": reason}

    # -- money movements ------------------------------------------------------

    async def deposit(
        self,
        wallet_id: str,
        amount: object,
        *,
        idempotency_key: str = "",
        reference: str = "",
        description: str = "",
        owner: str | None = None,
    ) -> dict[str, Any]:
        """Credit a wallet from the simulated issuance account."""
        _LOGGER.info(
            "novamarket.wallet.request operation=deposit wallet=%s amount=%s key=%s",
            wallet_id,
            amount,
            idempotency_key or "-",
        )
        try:
            value = self._validated_amount(amount, field_name="deposit amount")
            fingerprint = request_fingerprint(
                {
                    "operation": "deposit",
                    "wallet_id": str(wallet_id),
                    "amount": amount_text(value),
                    "reference": reference,
                }
            )
            replay = self._store.replay(
                idempotency_key=idempotency_key, fingerprint=fingerprint
            )
            if replay is not None:
                return self._replayed_result(replay, owner=owner)
            wallet = self._operable_wallet(wallet_id, owner=owner, operation="deposit")
            result = self._store.post(
                PostingRequest(
                    transaction_type=TransactionType.DEPOSIT,
                    wallet_id=wallet.wallet_id,
                    amount=value,
                    legs=(
                        LedgerLeg(
                            self._store.system_wallet_id,
                            EntryDirection.DEBIT,
                            value,
                            EntryType.DEPOSIT,
                            description="issuance",
                        ),
                        LedgerLeg(
                            wallet.wallet_id,
                            EntryDirection.CREDIT,
                            value,
                            EntryType.DEPOSIT,
                            description=description or "deposit",
                        ),
                    ),
                    reference=reference,
                    description=description or "deposit",
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                )
            )
        except WalletError as exc:
            await self._failed("deposit", exc)
            raise
        await self._after_commit(result, deposit_event=True)
        return WalletOperationResult(
            transaction=result.transaction,
            entries=result.entries,
            wallets=result.wallets,
            replayed=result.replayed,
        ).to_dict()

    async def withdraw(
        self,
        wallet_id: str,
        amount: object,
        *,
        idempotency_key: str = "",
        reference: str = "",
        description: str = "",
        owner: str | None = None,
    ) -> dict[str, Any]:
        """Debit a wallet back to the simulated issuance account."""
        _LOGGER.info(
            "novamarket.wallet.request operation=withdraw wallet=%s amount=%s key=%s",
            wallet_id,
            amount,
            idempotency_key or "-",
        )
        try:
            value = self._validated_amount(amount, field_name="withdrawal amount")
            fingerprint = request_fingerprint(
                {
                    "operation": "withdraw",
                    "wallet_id": str(wallet_id),
                    "amount": amount_text(value),
                    "reference": reference,
                }
            )
            replay = self._store.replay(
                idempotency_key=idempotency_key, fingerprint=fingerprint
            )
            if replay is not None:
                return self._replayed_result(replay, owner=owner)
            wallet = self._operable_wallet(wallet_id, owner=owner, operation="withdraw")
            result = self._store.post(
                PostingRequest(
                    transaction_type=TransactionType.WITHDRAWAL,
                    wallet_id=wallet.wallet_id,
                    amount=value,
                    legs=(
                        LedgerLeg(
                            wallet.wallet_id,
                            EntryDirection.DEBIT,
                            value,
                            EntryType.WITHDRAWAL,
                            description=description or "withdrawal",
                        ),
                        LedgerLeg(
                            self._store.system_wallet_id,
                            EntryDirection.CREDIT,
                            value,
                            EntryType.WITHDRAWAL,
                            description="issuance",
                        ),
                    ),
                    reference=reference,
                    description=description or "withdrawal",
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                )
            )
        except WalletError as exc:
            await self._failed("withdraw", exc)
            raise
        await self._after_commit(result, deposit_event=False)
        return WalletOperationResult(
            transaction=result.transaction,
            entries=result.entries,
            wallets=result.wallets,
            replayed=result.replayed,
        ).to_dict()

    async def transfer(
        self,
        from_wallet_id: str,
        to_wallet_id: str,
        amount: object,
        *,
        idempotency_key: str = "",
        reference: str = "",
        description: str = "",
        owner: str | None = None,
    ) -> dict[str, Any]:
        """Move NC between two wallets in one balanced, atomic transaction."""
        _LOGGER.info(
            "novamarket.wallet.request operation=transfer from=%s to=%s amount=%s "
            "key=%s",
            from_wallet_id,
            to_wallet_id,
            amount,
            idempotency_key or "-",
        )
        try:
            value = self._validated_amount(amount, field_name="transfer amount")
            fingerprint = request_fingerprint(
                {
                    "operation": "transfer",
                    "from_wallet_id": str(from_wallet_id),
                    "to_wallet_id": str(to_wallet_id),
                    "amount": amount_text(value),
                    "reference": reference,
                }
            )
            replay = self._store.replay(
                idempotency_key=idempotency_key, fingerprint=fingerprint
            )
            if replay is not None:
                return self._replayed_result(replay, owner=owner)
            sender = self._operable_wallet(
                from_wallet_id, owner=owner, operation="transfer"
            )
            if str(from_wallet_id) == str(to_wallet_id):
                raise WalletValidationError(
                    "a transfer must name two different wallets; transferring to "
                    "the same wallet would post a debit and its own credit"
                )
            recipient = self._store.wallet(to_wallet_id)
            if recipient.system:
                raise WalletValidationError(
                    "the issuance account is not a transfer destination; use a "
                    "deposit instead"
                )
            if not recipient.active:
                raise WalletNotActiveError(
                    f"wallet {recipient.wallet_id} is {recipient.status.value}; money "
                    "may only move through an active wallet"
                )
            result = self._store.post(
                PostingRequest(
                    transaction_type=TransactionType.TRANSFER,
                    wallet_id=sender.wallet_id,
                    counterparty_wallet_id=recipient.wallet_id,
                    amount=value,
                    legs=(
                        LedgerLeg(
                            sender.wallet_id,
                            EntryDirection.DEBIT,
                            value,
                            EntryType.TRANSFER,
                            description=description or "transfer out",
                        ),
                        LedgerLeg(
                            recipient.wallet_id,
                            EntryDirection.CREDIT,
                            value,
                            EntryType.TRANSFER,
                            description=description or "transfer in",
                        ),
                    ),
                    reference=reference,
                    description=description or "transfer",
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                )
            )
        except WalletError as exc:
            await self._failed("transfer", exc)
            raise
        await self._publish(
            EventType.NOVAMARKET_WALLET_TRANSFER_COMPLETED,
            from_wallet_id=str(from_wallet_id),
            to_wallet_id=str(to_wallet_id),
            transaction_id=result.transaction.transaction_id,
            amount=amount_text(result.transaction.amount),
        )
        await self._after_commit(result, deposit_event=None)
        return WalletOperationResult(
            transaction=result.transaction,
            entries=result.entries,
            wallets=result.wallets,
            replayed=result.replayed,
        ).to_dict()

    async def reverse(
        self,
        transaction_id: str,
        *,
        reason: str,
        idempotency_key: str = "",
        owner: str | None = None,
    ) -> dict[str, Any]:
        """Undo one transaction with mirrored entries — never by editing it."""
        _LOGGER.info(
            "novamarket.wallet.request operation=reverse transaction=%s key=%s",
            transaction_id,
            idempotency_key or "-",
        )
        try:
            if not str(reason or "").strip():
                raise WalletValidationError(
                    "a reversal needs a reason; a correction without one is not "
                    "auditable"
                )
            original = self._store.transaction(transaction_id)
            fingerprint = request_fingerprint(
                {
                    "operation": "reverse",
                    "transaction_id": original.transaction_id,
                    "reason": reason,
                }
            )
            replay = self._store.replay(
                idempotency_key=idempotency_key, fingerprint=fingerprint
            )
            if replay is not None:
                return self._replayed_result(replay, owner=owner)
            if original.transaction_type is TransactionType.REVERSAL:
                raise WalletConflictError(
                    "a reversal cannot itself be reversed; post a new correction "
                    "transaction against the current state instead"
                )
            if original.reversed:
                raise WalletConflictError(
                    f"transaction {original.transaction_id} was already reversed by "
                    f"{original.reversed_by}"
                )
            wallet = self._store.wallet(original.wallet_id)
            if owner is not None and wallet.owner != str(owner).strip():
                raise WalletValidationError(
                    f"wallet {wallet.wallet_id} is owned by {wallet.owner!r}, not "
                    f"{str(owner).strip()!r}"
                )
            original_entries = self._store.entries(
                transaction_id=original.transaction_id, ascending=True
            )
            if not original_entries:
                raise LedgerIntegrityError(
                    f"transaction {original.transaction_id} has no ledger entries to "
                    "reverse"
                )
            legs = tuple(
                LedgerLeg(
                    entry.wallet_id,
                    (
                        EntryDirection.CREDIT
                        if entry.direction is EntryDirection.DEBIT
                        else EntryDirection.DEBIT
                    ),
                    entry.amount.amount,
                    EntryType.REVERSAL,
                    description=f"reversal of {original.transaction_id}: {reason}",
                    reference=original.reference,
                )
                for entry in original_entries
            )
            result = self._store.post(
                PostingRequest(
                    transaction_type=TransactionType.REVERSAL,
                    wallet_id=original.wallet_id,
                    counterparty_wallet_id=original.counterparty_wallet_id,
                    amount=original.amount.amount,
                    legs=legs,
                    reference=original.reference,
                    description=f"reversal of {original.transaction_id}: {reason}",
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    reverses_transaction_id=original.transaction_id,
                )
            )
        except WalletError as exc:
            await self._failed("reverse", exc)
            raise
        await self._after_commit(result, deposit_event=None)
        return WalletOperationResult(
            transaction=result.transaction,
            entries=result.entries,
            wallets=result.wallets,
            replayed=result.replayed,
        ).to_dict()

    # -- reads ----------------------------------------------------------------

    def transactions(
        self,
        *,
        wallet_id: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        ascending: bool = False,
    ) -> dict[str, Any]:
        rows = self._store.transactions(
            wallet_id=wallet_id, limit=limit, offset=offset, ascending=ascending
        )
        return {
            "ok": True,
            "transactions": [row.to_dict() for row in rows],
            "total": self._store.count_transactions(wallet_id=wallet_id),
            "limit": self._store.applied_limit(limit),
            "offset": offset,
            "currency": "INR",
        }

    def entries(
        self,
        *,
        wallet_id: str | None = None,
        transaction_id: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        ascending: bool = True,
    ) -> dict[str, Any]:
        rows = self._store.entries(
            wallet_id=wallet_id,
            transaction_id=transaction_id,
            limit=limit,
            offset=offset,
            ascending=ascending,
        )
        return {
            "ok": True,
            "entries": [row.to_dict() for row in rows],
            "total": self._store.count_entries(
                wallet_id=wallet_id, transaction_id=transaction_id
            ),
            "limit": self._store.applied_limit(limit),
            "offset": offset,
            "currency": "INR",
        }

    async def reconcile(self, wallet_id: str | None = None) -> dict[str, Any]:
        """Independently verify balances against the ledger; repair nothing."""
        report = self._store.reconcile(wallet_id)
        self._reconciliations += 1
        self._last_report = report
        _LOGGER.info(
            "novamarket.ledger.reconciled scope=%s wallets=%d entries=%d "
            "transactions=%d ledger_sum=%s ok=%s issues=%d",
            wallet_id or "all",
            report.wallets_checked,
            report.entries_checked,
            report.transactions_checked,
            amount_text(report.ledger_sum),
            report.ok,
            len(report.issues),
        )
        await self._publish(
            EventType.NOVAMARKET_LEDGER_RECONCILIATION_COMPLETED,
            wallets_checked=report.wallets_checked,
            ok=report.ok,
        )
        if not report.ok:
            for issue in report.issues:
                _LOGGER.error(
                    "novamarket.ledger.integrity_violation code=%s wallet=%s "
                    "transaction=%s detail=%s",
                    issue.code,
                    issue.wallet_id or "-",
                    issue.transaction_id or "-",
                    issue.detail,
                )
                await self._publish(
                    EventType.NOVAMARKET_LEDGER_INTEGRITY_VIOLATION,
                    wallet_id=issue.wallet_id,
                    code=issue.code,
                )
        return report.to_dict()

    def status(self) -> dict[str, Any]:
        summary = self._store.summary()
        return {
            "initialised": True,
            "repository": self._store.path,
            "path": self._store.path,
            "schema_version": self._store.schema_version,
            "summary": summary,
            "system_wallet_id": self._store.system_wallet_id,
            "nova_coin_per_inr": "1",
            "precision": "0.01",
            "currency": "INR",
            "reconciliations": self._reconciliations,
            "last_reconciliation": (
                self._last_report.to_dict() if self._last_report is not None else None
            ),
            "last_error": self._last_error,
            "last_event_error": self._last_event_error,
        }

    def close(self) -> None:
        self._store.close()

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _validated_amount(amount: object, *, field_name: str) -> Decimal:
        try:
            return parse_amount(amount, field_name=field_name)
        except (TypeError, ValueError) as exc:
            raise WalletValidationError(str(exc)) from exc

    def _operable_wallet(
        self, wallet_id: str, *, owner: str | None, operation: str
    ) -> Wallet:
        wallet = self._store.wallet(wallet_id)
        if wallet.system:
            raise WalletValidationError(
                "the issuance account is managed by the ledger; use a normal wallet"
            )
        if not wallet.active:
            raise WalletNotActiveError(
                f"wallet {wallet.wallet_id} is {wallet.status.value}; {operation} "
                "requires an active wallet"
            )
        if owner is not None and wallet.owner != str(owner).strip():
            raise WalletValidationError(
                f"wallet {wallet.wallet_id} is owned by {wallet.owner!r}, not "
                f"{str(owner).strip()!r}"
            )
        return wallet

    def _replayed_result(
        self, replay: PostingResult, *, owner: str | None = None
    ) -> dict[str, Any]:
        """Answer a retry with the original commit — no movement, no events."""
        if owner is not None:
            writable = [
                wallet for wallet in replay.wallets.values() if not wallet.system
            ]
            if writable and str(owner).strip() not in {w.owner for w in writable}:
                raise WalletValidationError(
                    "that idempotency key belongs to a transaction this owner "
                    "cannot see"
                )
        _LOGGER.info(
            "novamarket.wallet.replayed type=%s transaction=%s wallet=%s amount=%s",
            replay.transaction.transaction_type.value,
            replay.transaction.transaction_id,
            replay.transaction.wallet_id,
            amount_text(replay.transaction.amount),
        )
        return WalletOperationResult(
            transaction=replay.transaction,
            entries=replay.entries,
            wallets=replay.wallets,
            replayed=True,
        ).to_dict()

    async def _after_commit(
        self, result: PostingResult, *, deposit_event: bool | None
    ) -> None:
        """Publish what actually committed, and only after the commit."""
        transaction = result.transaction
        if result.replayed:
            # A replay changed nothing: re-publishing the events would tell
            # subscribers that the money moved twice.
            _LOGGER.info(
                "novamarket.wallet.replayed type=%s transaction=%s wallet=%s "
                "amount=%s",
                transaction.transaction_type.value,
                transaction.transaction_id,
                transaction.wallet_id,
                amount_text(transaction.amount),
            )
            return
        _LOGGER.info(
            "novamarket.wallet.committed type=%s transaction=%s wallet=%s "
            "counterparty=%s amount=%s entries=%d",
            transaction.transaction_type.value,
            transaction.transaction_id,
            transaction.wallet_id,
            transaction.counterparty_wallet_id or "-",
            amount_text(transaction.amount),
            len(result.entries),
        )
        for entry in result.entries:
            await self._publish(
                EventType.NOVAMARKET_LEDGER_ENTRY_CREATED,
                entry_id=entry.entry_id,
                transaction_id=entry.transaction_id,
                wallet_id=entry.wallet_id,
            )
        if deposit_event is True:
            await self._publish(
                EventType.NOVAMARKET_WALLET_DEPOSIT_COMPLETED,
                wallet_id=result.transaction.wallet_id,
                transaction_id=result.transaction.transaction_id,
                amount=amount_text(result.transaction.amount),
            )
        elif deposit_event is False:
            await self._publish(
                EventType.NOVAMARKET_WALLET_WITHDRAWAL_COMPLETED,
                wallet_id=result.transaction.wallet_id,
                transaction_id=result.transaction.transaction_id,
                amount=amount_text(result.transaction.amount),
            )
        for wallet_id, wallet in result.wallets.items():
            await self._publish(
                EventType.NOVAMARKET_WALLET_BALANCE_UPDATED,
                wallet_id=wallet_id,
                balance=amount_text(wallet.balance),
            )

    async def _failed(self, operation: str, exc: BaseException) -> None:
        self._last_error = f"{operation}: {type(exc).__name__}: {exc}"
        _LOGGER.warning(
            "novamarket.wallet.failed operation=%s error=%s reason=%s",
            operation,
            type(exc).__name__,
            exc,
        )
        await self._publish(
            EventType.NOVAMARKET_WALLET_TRANSACTION_FAILED,
            operation=operation,
            reason=str(exc),
        )

    async def _publish(self, event_type: EventType, **payload: Any) -> None:
        if self._publisher is None:
            return
        try:
            await self._publisher.publish(event_type, **payload)
        except Exception as exc:  # noqa: BLE001 - a committed transaction stands
            # Events describe a commit that already happened. A publisher failure
            # is recorded, never surfaced as a failed transaction.
            self._last_event_error = f"{type(exc).__name__}: {exc}"
            _LOGGER.warning(
                "NovaMarket wallet event %s could not be published: %s",
                event_type,
                exc,
            )


__all__ = ["EventPublisher", "WalletEngine", "WalletOperationResult"]
