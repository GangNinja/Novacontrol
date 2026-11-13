"""NM-03 canonical wallet and ledger models.

The accounting model, stated once
---------------------------------
NovaCoin is an internal accounting unit: **1 NC = ₹1**, exactly. NM-03 therefore
keeps every amount in INR and reports the NC value as the same number; there is no
rate to look up and nothing to convert. A foreign-currency amount is refused at
the door instead of being treated as INR.

Debits and credits
------------------
Every ledger entry belongs to exactly one wallet and carries a *direction*:

* ``credit`` increases that wallet's balance (``signed_amount`` is positive),
* ``debit`` decreases it (``signed_amount`` is negative).

Each posted transaction writes entries whose signed amounts sum to **zero**, so
the ledger is double-entry:

* **deposit** — user wallet credit / issuance wallet debit,
* **withdrawal** — user wallet debit / issuance wallet credit,
* **transfer** — sender debit / recipient credit,
* **reversal** — the mirror of the reversed transaction's legs.

The issuance wallet (owner ``novamarket:issuance``) is the simulated system's own
account. Its balance is negative by exactly the amount of NC in circulation, which
is what makes the whole ledger sum to zero; it is never listed as a user wallet and
no API can deposit into or withdraw from it directly.

Precision policy
----------------
Amounts are :class:`decimal.Decimal` and must be representable in paise — two
decimal places with ``ROUND_HALF_EVEN`` as the ledger's one rounding rule. An
amount that would *lose* value when quantized (``1.005``) is refused rather than
silently rounded, because silently rounding a ledger amount changes what the
ledger says happened. Zero and negative amounts are refused for money movements.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from novacontrol.novamarket.money import Currency, Money

#: The smallest representable amount: one paisa / 0.01 NC.
PAISE = Decimal("0.01")

#: How many decimal places NC carries. Mirrors :data:`PAISE`.
NC_PRECISION = 2

#: A ceiling that exists to catch a typo rather than to model a limit: one lakh
#: crore NC (₹1e12). A simulated wallet has no business holding more.
MAX_AMOUNT = Decimal("1000000000000")

#: The owner identifier of the simulated issuance account. Reserved: an operator
#: wallet may not use it.
SYSTEM_OWNER = "novamarket:issuance"


class WalletStatus(StrEnum):
    """A wallet's operational state; money only moves while ``ACTIVE``."""

    ACTIVE = "active"
    FROZEN = "frozen"
    CLOSED = "closed"


class EntryDirection(StrEnum):
    """Which side of the ledger an entry is on."""

    DEBIT = "debit"
    CREDIT = "credit"


class EntryType(StrEnum):
    """Why an entry exists. Reversals are their own type, never an edit."""

    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    TRANSFER = "transfer"
    REVERSAL = "reversal"
    ADJUSTMENT = "adjustment"


class TransactionType(StrEnum):
    """The operation a transaction records."""

    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    TRANSFER = "transfer"
    REVERSAL = "reversal"
    ADJUSTMENT = "adjustment"


class TransactionStatus(StrEnum):
    """A posted transaction is final; ``REVERSED`` is derived, never written.

    The store is append-only, so a reversal does not mutate the original row: the
    original stays ``POSTED`` forever and the read model reports ``reversed`` plus
    the id of the transaction that reversed it.
    """

    POSTED = "posted"
    REVERSED = "reversed"


def _coerce_decimal(value: object, *, field_name: str) -> Decimal:
    """A finite Decimal from Decimal/int/decimal-string; floats are refused."""
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be a decimal value, not bool")
    if isinstance(value, float):
        raise TypeError(
            f"{field_name} must not be a binary float; use Decimal, int or a "
            "decimal string so ledger arithmetic stays deterministic"
        )
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field_name} must not be empty")
        try:
            amount = Decimal(text)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{field_name} is not a decimal value: {value!r}") from exc
    else:
        raise TypeError(
            f"{field_name} must be Decimal, int or a decimal string, not "
            f"{type(value).__name__}"
        )
    if not amount.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal, got {amount}")
    return amount


def parse_amount(
    value: object,
    *,
    field_name: str = "amount",
    allow_zero: bool = False,
) -> Decimal:
    """Validate one money amount against the NC precision policy.

    Refuses floats, NaN/infinity, empty strings, amounts finer than one paisa
    (unless the extra digits are trailing zeros), non-positive amounts, and
    absurd magnitudes. It never rounds: an amount that cannot be represented is
    rejected so the caller — not the ledger — decides what to do.
    """
    amount = _coerce_decimal(value, field_name=field_name)
    quantized = amount.quantize(PAISE, rounding=ROUND_HALF_EVEN)
    if quantized != amount:
        raise ValueError(
            f"{field_name} {amount} is finer than one paisa ({PAISE}); NC carries "
            f"{NC_PRECISION} decimal places and an unrepresentable amount is "
            "refused rather than silently rounded"
        )
    if amount < 0:
        raise ValueError(f"{field_name} must not be negative, got {amount}")
    if amount == 0 and not allow_zero:
        raise ValueError(f"{field_name} must be greater than zero")
    if amount > MAX_AMOUNT:
        raise ValueError(
            f"{field_name} {amount} exceeds the supported maximum {MAX_AMOUNT}"
        )
    return quantized


def amount_text(amount: Decimal | Money) -> str:
    """The canonical decimal string stored in SQLite (never a float)."""
    value = amount.amount if isinstance(amount, Money) else amount
    return format(value.quantize(PAISE, rounding=ROUND_HALF_EVEN), "f")


def inr(amount: Decimal) -> Money:
    """A Money value in INR; NC is the same number by definition."""
    return Money.inr(amount)


def new_id(prefix: str) -> str:
    """A stable, unique identifier with a readable prefix."""
    return f"{prefix}_{uuid.uuid4().hex}"


def request_fingerprint(payload: Mapping[str, Any]) -> str:
    """A deterministic hash of a financial request.

    Two submissions of one idempotency key are only the same request when their
    fingerprints match; a different payload under a reused key is a conflict, not
    a replay.
    """
    canonical = json.dumps(
        {key: str(value) for key, value in sorted(payload.items())},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Wallet:
    """One NovaCoin wallet.

    ``balance`` is the stored representation the ledger must always agree with;
    reconciliation reads the entries and compares them to it. There is no reserved
    balance in NM-03: nothing holds funds yet (that arrives with paper execution),
    so the total is the available balance rather than a second number that would
    only look meaningful.
    """

    wallet_id: str
    owner: str
    currency: Currency
    status: WalletStatus
    balance: Money
    label: str = ""
    system: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        wallet_id = str(self.wallet_id or "").strip()
        owner = str(self.owner or "").strip()
        if not wallet_id:
            raise ValueError("a wallet needs an id")
        if not owner:
            raise ValueError("a wallet needs an owner")
        if Currency(self.currency) is not Currency.INR:
            raise ValueError(
                "NovaCoin wallets are INR wallets (1 NC = 1 INR); a "
                f"{Currency(self.currency).value} wallet would need a timestamped "
                "exchange rate this phase does not have"
            )
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("wallet timestamps must be timezone-aware")
        object.__setattr__(self, "wallet_id", wallet_id)
        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "label", str(self.label or "").strip())
        object.__setattr__(self, "currency", Currency.INR)

    @property
    def available_balance(self) -> Money:
        """What may be withdrawn or transferred right now."""
        return self.balance

    @property
    def total_balance(self) -> Money:
        """Available + reserved; NM-03 holds no reserves, so it equals available."""
        return self.balance

    @property
    def active(self) -> bool:
        return self.status is WalletStatus.ACTIVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "wallet_id": self.wallet_id,
            "owner": self.owner,
            "label": self.label,
            "currency": self.currency.value,
            "status": self.status.value,
            "system": self.system,
            "balance": {
                "available": amount_text(self.available_balance),
                "reserved": "0.00",
                "total": amount_text(self.total_balance),
                "currency": self.currency.value,
                "nova_coin": amount_text(self.total_balance),
            },
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One immutable side of a posted transaction."""

    entry_id: str
    transaction_id: str
    wallet_id: str
    entry_type: EntryType
    direction: EntryDirection
    amount: Money
    signed_amount: Money
    balance_after: Money
    sequence: int
    created_at: datetime
    description: str = ""
    reference: str = ""

    def __post_init__(self) -> None:
        direction = EntryDirection(self.direction)
        expected_negative = direction is EntryDirection.DEBIT
        if (self.signed_amount.amount < 0) is not expected_negative:
            raise ValueError(
                f"a {direction.value} entry must carry a "
                f"{'negative' if expected_negative else 'positive'} signed amount"
            )
        if abs(self.signed_amount.amount) != self.amount.amount:
            raise ValueError("signed amount must mirror the entry amount")

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "transaction_id": self.transaction_id,
            "wallet_id": self.wallet_id,
            "entry_type": self.entry_type.value,
            "direction": self.direction.value,
            "amount": amount_text(self.amount),
            "signed_amount": amount_text(self.signed_amount),
            "balance_after": amount_text(self.balance_after),
            "sequence": self.sequence,
            "currency": Currency.INR.value,
            "nova_coin": amount_text(self.amount),
            "description": self.description,
            "reference": self.reference,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class WalletTransaction:
    """One posted financial operation, with the entries it produced."""

    transaction_id: str
    transaction_type: TransactionType
    wallet_id: str
    amount: Money
    created_at: datetime
    counterparty_wallet_id: str | None = None
    reference: str = ""
    description: str = ""
    idempotency_key: str = ""
    reverses_transaction_id: str | None = None
    entry_count: int = 0
    reversed_by: str | None = None

    def __post_init__(self) -> None:
        if Currency.INR.value != self.amount.currency.value:
            raise ValueError("a transaction amount must be INR (NovaCoin)")

    @property
    def reversed(self) -> bool:
        return self.reversed_by is not None

    @property
    def status(self) -> TransactionStatus:
        return (
            TransactionStatus.REVERSED if self.reversed else TransactionStatus.POSTED
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "transaction_type": self.transaction_type.value,
            "status": self.status.value,
            "wallet_id": self.wallet_id,
            "counterparty_wallet_id": self.counterparty_wallet_id,
            "amount": amount_text(self.amount),
            "currency": Currency.INR.value,
            "nova_coin": amount_text(self.amount),
            "reference": self.reference,
            "description": self.description,
            "idempotency_key": self.idempotency_key,
            "reverses_transaction_id": self.reverses_transaction_id,
            "reversed_by_transaction_id": self.reversed_by,
            "entry_count": self.entry_count,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ReconciliationIssue:
    """One detected integrity problem, with the evidence that shows it."""

    code: str
    detail: str
    wallet_id: str = ""
    transaction_id: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            "wallet_id": self.wallet_id,
            "transaction_id": self.transaction_id,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """The independent verdict: ledger-derived balance vs the stored balance.

    ``ok`` is false when any issue was found; nothing is repaired. ``ledger_sum``
    is the signed sum of every entry considered, which must be zero across the
    whole ledger because every posted transaction balances.
    """

    wallet_id: str
    currency: Currency
    stored_balance: Money
    ledger_balance: Money
    difference: Money
    ok: bool
    issues: tuple[ReconciliationIssue, ...]
    entries_checked: int
    transactions_checked: int
    wallets_checked: int
    ledger_sum: Money
    checked_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "wallet_id": self.wallet_id,
            "currency": self.currency.value,
            "stored_balance": amount_text(self.stored_balance),
            "ledger_balance": amount_text(self.ledger_balance),
            "difference": amount_text(self.difference),
            "ok": self.ok,
            "integrity_status": "ok" if self.ok else "violation",
            "issues": [issue.to_dict() for issue in self.issues],
            "entries_checked": self.entries_checked,
            "transactions_checked": self.transactions_checked,
            "wallets_checked": self.wallets_checked,
            "ledger_sum": amount_text(self.ledger_sum),
            "nova_coin": {
                "ledger_balance": amount_text(self.ledger_balance),
                "difference": amount_text(self.difference),
            },
            "checked_at": self.checked_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class LedgerLeg:
    """One side of a transaction about to be posted."""

    wallet_id: str
    direction: EntryDirection
    amount: Decimal
    entry_type: EntryType
    description: str = ""
    reference: str = ""

    def signed(self) -> Decimal:
        return (
            -self.amount
            if EntryDirection(self.direction) is EntryDirection.DEBIT
            else self.amount
        )


@dataclass(frozen=True, slots=True)
class PostingRequest:
    """A validated, balanced set of legs the store may commit atomically."""

    transaction_type: TransactionType
    wallet_id: str
    amount: Decimal
    legs: Sequence[LedgerLeg]
    currency: Currency = Currency.INR
    counterparty_wallet_id: str | None = None
    reference: str = ""
    description: str = ""
    idempotency_key: str = ""
    fingerprint: str = ""
    reverses_transaction_id: str | None = None


@dataclass(frozen=True, slots=True)
class PostingResult:
    """What a commit produced (or the replay of an earlier identical commit)."""

    transaction: WalletTransaction
    entries: tuple[LedgerEntry, ...]
    wallets: Mapping[str, Wallet]
    replayed: bool = False


__all__ = [
    "EntryDirection",
    "EntryType",
    "LedgerEntry",
    "LedgerLeg",
    "MAX_AMOUNT",
    "NC_PRECISION",
    "PAISE",
    "PostingRequest",
    "PostingResult",
    "ReconciliationIssue",
    "ReconciliationReport",
    "SYSTEM_OWNER",
    "TransactionStatus",
    "TransactionType",
    "Wallet",
    "WalletStatus",
    "WalletTransaction",
    "amount_text",
    "inr",
    "new_id",
    "parse_amount",
    "request_fingerprint",
]
