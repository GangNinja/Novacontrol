"""Portfolio-domain errors mapped to HTTP by the API layer."""

from __future__ import annotations


class PortfolioError(ValueError):
    """Base class for portfolio failures."""


class PortfolioNotFound(PortfolioError):
    def __init__(self, portfolio_id: str) -> None:
        super().__init__(f"Portfolio {portfolio_id!r} was not found.")
        self.portfolio_id = portfolio_id


class PortfolioVentureNotFound(PortfolioError):
    def __init__(self, venture_id: str) -> None:
        super().__init__(f"Venture {venture_id!r} is not registered in this portfolio.")
        self.venture_id = venture_id


class PortfolioRecordNotFound(PortfolioError):
    def __init__(self, kind: str, record_id: str) -> None:
        super().__init__(f"{kind} {record_id!r} was not found in this portfolio.")
        self.kind = kind
        self.record_id = record_id


class PortfolioAuthorizationError(PortfolioError):
    """The actor lacks the portfolio role or venture permission."""


class PortfolioConflict(PortfolioError):
    """A duplicate, stale or conflicting portfolio operation."""


class PortfolioValidationError(PortfolioError):
    """Invalid portfolio input."""


class LifecycleGateError(PortfolioConflict):
    """The requested lifecycle state is not supported by Phase 35–38 evidence."""


class ApprovalRequired(PortfolioConflict):
    """A consequential portfolio action needs an explicit human approval."""


__all__ = [
    "ApprovalRequired",
    "LifecycleGateError",
    "PortfolioAuthorizationError",
    "PortfolioConflict",
    "PortfolioError",
    "PortfolioNotFound",
    "PortfolioRecordNotFound",
    "PortfolioValidationError",
    "PortfolioVentureNotFound",
]
