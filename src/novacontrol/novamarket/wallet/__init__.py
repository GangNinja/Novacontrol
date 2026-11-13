"""NM-03 — NovaCoin wallet and deterministic ledger.

The package is deliberately small and layered:

``models``   canonical wallets, transactions, ledger entries, the NC precision
             policy and the debit/credit convention (1 NC = 1 INR);
``store``    the durable, append-only SQLite repository with exact integer-paise
             arithmetic, atomic postings, persisted idempotency and reconciliation;
``engine``   request validation, ownership and status rules, reversal policy and
             the events published *after* a commit.

Nothing here reaches for a market price, a portfolio, an order or a forecast: later
phases consume these contracts instead of extending this package's scope.
"""

from novacontrol.novamarket.wallet.engine import WalletEngine, WalletOperationResult
from novacontrol.novamarket.wallet.models import (
    SYSTEM_OWNER,
    EntryDirection,
    EntryType,
    LedgerEntry,
    LedgerLeg,
    PostingRequest,
    PostingResult,
    ReconciliationIssue,
    ReconciliationReport,
    TransactionStatus,
    TransactionType,
    Wallet,
    WalletStatus,
    WalletTransaction,
    amount_text,
    parse_amount,
)
from novacontrol.novamarket.wallet.store import (
    WALLET_SCHEMA_VERSION,
    WalletStore,
    open_wallet_store,
)

__all__ = [
    "SYSTEM_OWNER",
    "EntryDirection",
    "EntryType",
    "LedgerEntry",
    "LedgerLeg",
    "PostingRequest",
    "PostingResult",
    "ReconciliationIssue",
    "ReconciliationReport",
    "TransactionStatus",
    "TransactionType",
    "WALLET_SCHEMA_VERSION",
    "Wallet",
    "WalletEngine",
    "WalletOperationResult",
    "WalletStatus",
    "WalletStore",
    "WalletTransaction",
    "amount_text",
    "open_wallet_store",
    "parse_amount",
]
