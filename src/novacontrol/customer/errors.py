"""Phase 37 customer-growth error types, mapped to HTTP by the API layer."""

from __future__ import annotations

from typing import Any


class CustomerError(Exception):
    """Base class for customer-growth refusals."""


class CustomerNotFound(CustomerError):
    def __init__(self, product_id: str) -> None:
        self.product_id = product_id
        super().__init__(f"Customer workspace not found for product: {product_id}")


class CustomerRecordNotFound(CustomerError):
    def __init__(self, kind: str, record_id: str) -> None:
        self.kind = kind
        self.record_id = record_id
        super().__init__(f"{kind} not found: {record_id}")


class CustomerConflict(CustomerError):
    """The request conflicts with lifecycle state, consent or stored records."""


class ProductNotLaunched(CustomerError):
    """Acquisition/sales promotion is refused until Phase 36 authorizes launch."""

    def __init__(self, product_id: str, state: str = "") -> None:
        self.product_id = product_id
        self.state = state
        super().__init__(
            f"Product {product_id} is not approved for launch "
            f"(state {state or 'unknown'}); customer acquisition cannot promote it."
        )


class ConsentRequired(CustomerError):
    def __init__(self, message: str, *, channel: str = "", recipient: str = "") -> None:
        self.channel = channel
        self.recipient = recipient
        super().__init__(message)


class CommunicationNotConfigured(CustomerError):
    """No outbound sender is configured; the record is retained honestly."""


class CustomerBudgetExceeded(CustomerError):
    """A bounded customer-growth budget or rate limit was reached."""


class CustomerValidationError(CustomerError, ValueError):
    """A payload is malformed."""


def error_payload(exc: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {"message": str(exc), "error": type(exc).__name__}
    for name in ("product_id", "kind", "record_id", "channel", "recipient", "state"):
        value = getattr(exc, name, "")
        if value:
            payload[name] = value
    return payload


__all__ = [
    "CommunicationNotConfigured",
    "ConsentRequired",
    "CustomerBudgetExceeded",
    "CustomerConflict",
    "CustomerError",
    "CustomerNotFound",
    "CustomerRecordNotFound",
    "CustomerValidationError",
    "ProductNotLaunched",
    "error_payload",
]
