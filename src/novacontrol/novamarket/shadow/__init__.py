"""NM-12 Shadow Mode.

Shadow Mode observes live market data and records what NovaMarket would have
done.  It has no execution gateway, no wallet and no ledger access.
"""

from novacontrol.novamarket.shadow.guards import (
    ShadowExecutionGuard,
    ShadowIsolationError,
)
from novacontrol.novamarket.shadow.models import (
    PortfolioSnapshot,
    ShadowComparison,
    ShadowDecision,
    ShadowDecisionStatus,
    ShadowErrorKind,
    ShadowOutcome,
    ShadowOutcomeStatus,
    ShadowProjection,
    ShadowProjectionStatus,
    ShadowSession,
    ShadowSessionStatus,
)
from novacontrol.novamarket.shadow.service import (
    ShadowError,
    ShadowNotFoundError,
    ShadowService,
    ShadowStateError,
    ShadowValidationError,
)
from novacontrol.novamarket.shadow.store import ShadowStore

__all__ = [
    "PortfolioSnapshot",
    "ShadowComparison",
    "ShadowDecision",
    "ShadowDecisionStatus",
    "ShadowError",
    "ShadowErrorKind",
    "ShadowExecutionGuard",
    "ShadowIsolationError",
    "ShadowNotFoundError",
    "ShadowOutcome",
    "ShadowOutcomeStatus",
    "ShadowProjection",
    "ShadowProjectionStatus",
    "ShadowService",
    "ShadowSession",
    "ShadowSessionStatus",
    "ShadowStateError",
    "ShadowStore",
    "ShadowValidationError",
]
