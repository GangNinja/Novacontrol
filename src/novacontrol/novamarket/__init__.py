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
    PREDICTION_DOMAIN_BOUNDARIES,
    PREDICTION_ROADMAP,
    PREDICTION_ROADMAP_PHASES,
    PREDICTION_STORAGE_BOUNDARIES,
    ROADMAP,
    ROADMAP_PHASES,
    STORAGE_BOUNDARIES,
    validate_architecture,
    validate_prediction_architecture,
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
from novacontrol.novamarket.prediction import (
    PI_CONTRACT_VERSION,
    PI_PHASE,
    PredictionIntelligenceService,
)
from novacontrol.novamarket.marketdata.gateway import (
    AcquisitionPriority,
    GatewayObservation,
    GatewaySettings,
    MarketDataGateway,
)
from novacontrol.novamarket.features import (
    FEATURE_CONTRACT_VERSION,
    FEATURE_SET_VERSION,
    FeatureEngine,
    FeatureRequest,
    FeatureService,
    FeatureSet,
    RegimeDetector,
    RegimeResult,
)
from novacontrol.novamarket.statistical import (
    MODEL_IDS as STATISTICAL_MODEL_IDS,
    PI05_PHASE,
    STATISTICAL_CONTRACT_VERSION,
    ModelConfig as StatisticalModelConfig,
    StatisticalForecastingService,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "AcquisitionPriority",
    "CAPABILITIES",
    "ConfigurationError",
    "Currency",
    "DOMAIN_BOUNDARIES",
    "DependencyError",
    "FUTURE_EVENT_SPECS",
    "FEATURE_CONTRACT_VERSION",
    "FEATURE_SET_VERSION",
    "FeatureEngine",
    "FeatureRequest",
    "FeatureService",
    "FeatureSet",
    "HORIZON_PROFILES",
    "GatewayObservation",
    "GatewaySettings",
    "HorizonProfile",
    "IMPLEMENTED_EVENTS",
    "IMPLEMENTED_PHASE",
    "InvestmentHorizon",
    "LifecycleError",
    "LifecycleMachine",
    "LifecycleState",
    "MARKET_CONTRACT_VERSION",
    "MARKET_SCHEMA_VERSION",
    "PI_CONTRACT_VERSION",
    "PI_PHASE",
    "PREDICTION_DOMAIN_BOUNDARIES",
    "PREDICTION_ROADMAP",
    "PREDICTION_ROADMAP_PHASES",
    "PREDICTION_STORAGE_BOUNDARIES",
    "MarketCapability",
    "MarketDataProvider",
    "MarketDataGateway",
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
    "PredictionIntelligenceService",
    "RegimeDetector",
    "RegimeResult",
    "PI05_PHASE",
    "STATISTICAL_CONTRACT_VERSION",
    "STATISTICAL_MODEL_IDS",
    "StatisticalForecastingService",
    "StatisticalModelConfig",
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
    "validate_prediction_architecture",
]
