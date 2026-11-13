"""NovaMarket — the financial-intelligence subsystem of NovaControl (NM-01).

NM-01 ships the foundation, not the market features: a versioned contract
surface, an append-only timestamped price contract, deterministic NovaCoin/INR
money, three distinguishable investment horizons, an operation permission model
that grants no real-money execution, a validated lifecycle, typed events, and a
runtime module NovaControl can enable, disable, configure and monitor.

The next phase (NM-02, Market Data Reliability) implements
:class:`novacontrol.novamarket.money.MarketDataProvider` and a durable
append-only store behind these contracts. Nothing in NM-01 fetches a price,
creates a portfolio or sends an order.
"""

from novacontrol.novamarket.architecture import (
    DOMAIN_BOUNDARIES,
    ROADMAP,
    ROADMAP_PHASES,
    STORAGE_BOUNDARIES,
    validate_architecture,
)
from novacontrol.novamarket.config import NovaMarketConfig, ResourceLimits, SecretReference
from novacontrol.novamarket.contracts import (
    ALLOWED_TRANSITIONS,
    IMPLEMENTED_PHASE,
    MARKET_CONTRACT_VERSION,
    MARKET_SCHEMA_VERSION,
    ConfigurationError,
    DependencyError,
    LifecycleError,
    LifecycleMachine,
    LifecycleState,
    NotOperationalError,
    NovaMarketError,
    NovaMarketInitializationError,
    SUBSYSTEM_NAME,
)
from novacontrol.novamarket.events import (
    FUTURE_EVENT_SPECS,
    IMPLEMENTED_EVENTS,
    MarketEventPublisher,
    validate_market_event,
)
from novacontrol.novamarket.horizons import (
    HORIZON_PROFILES,
    HorizonProfile,
    InvestmentHorizon,
    profile_for,
)
from novacontrol.novamarket.module import NovaMarketModule
from novacontrol.novamarket.money import (
    Currency,
    MarketDataProvider,
    MarketQuote,
    Money,
    NOVA_COIN_INR_RATE,
    PriceHistory,
)
from novacontrol.novamarket.permissions import (
    MarketOperation,
    MarketPermissionDecision,
    MarketPermissionModel,
)
from novacontrol.novamarket.service import (
    CAPABILITIES,
    MarketCapability,
    MarketHealth,
    NovaMarketService,
    STATE_NAMESPACE,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "CAPABILITIES",
    "ConfigurationError",
    "Currency",
    "DOMAIN_BOUNDARIES",
    "DependencyError",
    "FUTURE_EVENT_SPECS",
    "HORIZON_PROFILES",
    "HorizonProfile",
    "IMPLEMENTED_EVENTS",
    "IMPLEMENTED_PHASE",
    "InvestmentHorizon",
    "LifecycleError",
    "LifecycleMachine",
    "LifecycleState",
    "MARKET_CONTRACT_VERSION",
    "MARKET_SCHEMA_VERSION",
    "MarketCapability",
    "MarketDataProvider",
    "MarketEventPublisher",
    "MarketHealth",
    "MarketOperation",
    "MarketPermissionDecision",
    "MarketPermissionModel",
    "MarketQuote",
    "Money",
    "NOVA_COIN_INR_RATE",
    "NovaMarketConfig",
    "NovaMarketError",
    "NovaMarketInitializationError",
    "NovaMarketModule",
    "NovaMarketService",
    "NotOperationalError",
    "PriceHistory",
    "ROADMAP",
    "ROADMAP_PHASES",
    "ResourceLimits",
    "STATE_NAMESPACE",
    "STORAGE_BOUNDARIES",
    "SUBSYSTEM_NAME",
    "SecretReference",
    "profile_for",
    "validate_architecture",
    "validate_market_event",
]
