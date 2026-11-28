"""NovaMarketService — the subsystem's validated lifecycle and public surface.

This is the object NovaControl manages. It composes NovaMarket's own contracts
with NovaControl's existing services rather than replacing any of them:

* the **persisted configuration** rides the application's ``JsonStateStore``
  under the ``novamarket`` namespace;
* **lifecycle announcements** ride the application's one ``EventBus`` with the
  typed ``novamarket.*`` vocabulary;
* **permissions** are declared into the application's one ``PermissionManager``
  (and withdrawn at disable/shutdown — exactly what this subsystem declared);
* **capabilities** are registered into the application's one
  ``CapabilityRegistry`` and withdrawn the same way;
* **health** is a report this object computes from its own records, which the
  application's diagnostics roster and status surface read.

NM-02 adds the market-data ENGINE behind that surface: opening the append-only
store, building the installed provider adapters, starting and stopping the
single ingestion worker, and serving validated reads. The engine owns fetching,
normalisation, freshness and quality; this service owns the lifecycle that
starts it and the record that says whether it actually worked. Nothing here
creates a portfolio, forecasts or sends an order.

Failure isolation is deliberate. ``auto_start`` (what the runtime module calls)
never raises: NovaMarket failing to initialize must not stop NovaControl from
starting, and the status surface then explains the failure. The explicit
``enable`` path *does* raise, because an operator who asked for it deserves the
reason.
"""

from __future__ import annotations

import inspect
import logging
import asyncio
import os
import time
from collections.abc import Callable, Mapping, Sequence
from threading import RLock
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:  # pragma: no cover - typing only
    from novacontrol.novamarket.portfolio import PortfolioEngine
    from novacontrol.novamarket.risk import RiskEngine
    from novacontrol.novamarket.shadow import ShadowService
    from novacontrol.novamarket.backtesting import BacktestingService
    from novacontrol.novamarket.evaluation import PredictionEvaluationService
    from novacontrol.novamarket.learning import LearningService

from novacontrol.core.diagnostics import HealthState
from novacontrol.core.events import Event, EventBus, EventType
from novacontrol.core.interfaces import Capability as RuntimeCapability
from novacontrol.intelligence.intent import (
    Capability,
    CapabilityAvailability,
    CapabilitySource,
)
from novacontrol.intelligence.intent import RiskLevel as CapabilityRiskLevel
from novacontrol.novamarket.architecture import (
    DOMAIN_BOUNDARIES,
    PREDICTION_DOMAIN_BOUNDARIES,
    PREDICTION_ROADMAP,
    PREDICTION_STORAGE_BOUNDARIES,
    ROADMAP,
    STORAGE_BOUNDARIES,
    validate_architecture,
)
from novacontrol.novamarket.config import NovaMarketConfig
from novacontrol.novamarket.features.definitions import FEATURE_SPECS, profile_features
from novacontrol.novamarket.filings import FILING_SOURCE_ID, FILING_SOURCE_NAME
from novacontrol.novamarket.features.models import (
    FEATURE_CONTRACT_VERSION,
    FEATURE_SET_VERSION,
    REGIME_METHOD_VERSION,
    FeatureFamily,
    FeatureProfileName,
    FeatureRequest,
    FeatureValidationError,
    RegimeLabel,
    RegimeResult,
)
from novacontrol.novamarket.features.service import FeatureService
from novacontrol.novamarket.features.store import FeatureStore, FeatureStoreError
from novacontrol.novamarket.autonomy import (
    AutonomyMode,
    AutonomyRuntime,
    closed_status,
)
from novacontrol.novamarket.autonomy.models import MonitoringConfig
from novacontrol.novamarket.autonomy_observability import build_reliability_report
from novacontrol.novamarket.autonomy.monitoring import (
    AutonomyMonitoring,
    MonitoringRefused,
    closed_monitoring_status,
)
from novacontrol.novamarket.autonomy.session import evaluate_session
from novacontrol.novamarket.autonomy.models import (
    CALENDAR_STATUS,
    HORIZON_MAPPING,
    LIVE_TRADING,
    MODE_TRANSITIONS,
    PAPER_ONLY,
    RELEASE_GATE,
    TRADING_IMPLEMENTED,
)
from novacontrol.novamarket.contracts import (
    StrategyValidationError,
    ConfigurationError,
    DependencyError,
    DiscoveryValidationError,
    EventValidationError,
    IMPLEMENTED_PHASE,
    LIFECYCLE_HISTORY_LIMIT,
    LifecycleError,
    LifecycleMachine,
    LifecycleState,
    MARKET_CONTRACT_VERSION,
    MARKET_SCHEMA_VERSION,
    MarketDataError,
    NotOperationalError,
    NovaMarketError,
    NovaMarketInitializationError,
    SUBSYSTEM_NAME,
)
from novacontrol.novamarket.events import MarketEventPublisher
from novacontrol.novamarket.horizons import (
    horizon_profiles,
    validate_horizon_profiles,
)
from novacontrol.novamarket.marketdata.engine import MarketDataEngine, ProviderProbe
from novacontrol.novamarket.marketdata.gateway import (
    AcquisitionPriority,
    GatewayObservation,
    GatewaySettings,
    MarketDataGateway,
)
from novacontrol.novamarket.marketdata.models import DataInterval, Instrument
from novacontrol.novamarket.marketdata.providers.base import (
    EnvironmentSecretResolver,
    HttpTransport,
    SecretResolver,
    UrllibTransport,
    resolve_credential,
)
from novacontrol.novamarket.marketdata.providers.registry import (
    ProviderRegistry,
    build_adapters,
)
from novacontrol.novamarket.marketdata.store import MarketDataStore
from novacontrol.novamarket.marketdata.screener import MarketBoard
from novacontrol.novamarket.marketdata.session_schedule import plan_market_refresh
from novacontrol.novamarket.marketdata.universe import build_indian_universe
from novacontrol.novamarket.workbench import MarketWorkbench
from novacontrol.novamarket.notifications import (
    NotificationCenter,
    market_session_snapshot,
)
from novacontrol.novamarket.permissions import (
    MarketOperation,
    MarketPermissionModel,
)
from novacontrol.novamarket.portfolio.models import money_text
from novacontrol.novamarket.wallet.engine import WalletEngine
from novacontrol.novamarket.wallet.store import WalletStore, open_wallet_store
from novacontrol.novamarket.discovery import (
    AssetDiscoveryEngine,
    DiscoveryStore,
    WatchlistKind as DiscoveryWatchlistKind,
)

from novacontrol.novamarket.prediction import (
    PI_CONTRACT_VERSION,
    PI_PHASE,
    PredictionIntelligenceService,
)
from novacontrol.novamarket.datasets import (
    DATASET_CONTRACT_VERSION,
    PI02_PHASE,
    DatasetBuildRequest,
    HistoricalDatasetService,
)
from novacontrol.novamarket.forecasting import (
    ForecastEngine,
    ForecastPolicy,
    ForecastStore,
    Horizon,
)
from novacontrol.novamarket.statistical import StatisticalForecastingService
from novacontrol.novamarket.gradient import (
    GradientForecastingService,
    backend_available as _gradient_backend_available,
    backend_version as _gradient_backend_version,
)
from novacontrol.novamarket.gradient.models import GradientFamily, GradientValidationError
from novacontrol.novamarket.ensemble import EnsembleForecastingService
from novacontrol.novamarket.ensemble.models import (
    ENSEMBLE_CONTRACT_VERSION,
    ENSEMBLE_MODEL_VERSION,
    ENSEMBLE_PHASE,
    EnsembleValidationError,
)
from novacontrol.novamarket.scheduling.models import SchedulerConfig
from novacontrol.novamarket.scheduling.service import ForecastScheduler
from novacontrol.novamarket.outlook import (
    HORIZON_OUTLOOK_PROFILES,
    HORIZON_PROFILE_VERSION,
    OUTLOOK_CONTRACT_VERSION,
    PI10_HORIZONS,
    HorizonForecastService,
    outlook_profile,
    validate_outlook_profiles,
)
from novacontrol.novamarket.lstm import (
    LSTMForecastingService,
    LSTMValidationError,
    backend_available as _lstm_backend_available,
    backend_version as _lstm_backend_version,
)
from novacontrol.novamarket.radar import (
    EventCategory as RadarEventCategory,
    EventFeedClient as RadarFeedClient,
    EventQuery as RadarEventQuery,
    EventStore as RadarEventStore,
    Importance as RadarImportance,
    InstrumentView as RadarInstrumentView,
    MarketEventRadar,
    SourceCatalog as RadarSourceCatalog,
    VerificationState as RadarVerificationState,
)
from novacontrol.reliability.permissions import PermissionManager

#: The state-store namespace this subsystem owns. One namespace, so disabling or
#: resetting NovaMarket can never touch another subsystem's snapshots.
STATE_NAMESPACE = "novamarket"

#: The logger. Its level follows the configuration, so observability is a
#: configuration choice rather than a constant compiled into the package.
_LOGGER = logging.getLogger("novacontrol.novamarket")


class StateStore(Protocol):
    """The small subset of :class:`JsonStateStore` NovaMarket needs."""

    def read(self, name: str) -> dict[str, Any]: ...

    def write(self, name: str, payload: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class MarketDependency:
    """One integration NovaMarket needs, and whether it is present."""

    name: str
    required: bool
    available: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "required": self.required,
            "available": self.available,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class MarketHealth:
    """NovaMarket's own health, in the application's one vocabulary."""

    state: HealthState
    reasons: tuple[str, ...]
    checked_at: datetime

    @property
    def healthy(self) -> bool:
        return self.state is HealthState.OK

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "healthy": self.healthy,
            "reasons": list(self.reasons),
            "checked_at": self.checked_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class MarketCapability:
    """A capability this build actually implements.

    ``requires_provider`` keeps the table honest about collection, and
    ``verified_operation`` keeps it honest about what has been PROVEN: a data
    capability is available only after the store records a successful
    provider operation of that name. Installing an adapter, or naming one in
    configuration, is a reference, not a verified capability.
    """

    capability_id: str
    name: str
    description: str
    operation: MarketOperation
    requires_provider: bool = False
    verified_operation: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "name": self.name,
            "description": self.description,
            "operation": self.operation.value,
            "requires_provider": self.requires_provider,
            "verified_operation": self.verified_operation,
        }


#: What NM-01 implements. Everything else the roadmap names stays a boundary,
#: not a capability: a capability table that advertises forecasting, trading or
#: backtesting before those phases exist would be a false claim.
CAPABILITIES: tuple[MarketCapability, ...] = (
    MarketCapability(
        capability_id="market.status",
        name="NovaMarket status",
        description="Report lifecycle, health, dependencies and configuration.",
        operation=MarketOperation.READ_STATUS,
    ),
    MarketCapability(
        capability_id="market.configure",
        name="NovaMarket configuration",
        description="Validate, apply and persist NovaMarket configuration.",
        operation=MarketOperation.MANAGE_CONFIG,
    ),
    MarketCapability(
        capability_id="market.contracts",
        name="NovaMarket contracts",
        description="Publish the versioned contracts and architectural boundaries.",
        operation=MarketOperation.READ_STATUS,
    ),
    MarketCapability(
        capability_id="market.read",
        name="Read market data",
        description="Read timestamped market quotes from a configured provider.",
        operation=MarketOperation.READ_MARKET,
        requires_provider=True,
        verified_operation="quote",
    ),
    MarketCapability(
        capability_id="market.data.history",
        name="Read market history",
        description="Read paginated price observations and OHLCV bars.",
        operation=MarketOperation.READ_MARKET,
        requires_provider=True,
        verified_operation="ohlcv",
    ),
    MarketCapability(
        capability_id="market.data.ingest",
        name="Collect market data",
        description="Run a bounded collection cycle and background ingestion.",
        operation=MarketOperation.READ_MARKET,
        requires_provider=True,
        verified_operation="quote",
    ),
    MarketCapability(
        capability_id="market.data.providers",
        name="Market-data providers",
        description="Report installed adapters, declared limits and measured health.",
        operation=MarketOperation.READ_STATUS,
    ),
    MarketCapability(
        capability_id="market.instruments.manage",
        name="Manage instruments",
        description="Register canonical instrument identity and provider symbols.",
        operation=MarketOperation.MANAGE_CONFIG,
    ),
    # NM-03: wallets and the deterministic ledger.
    MarketCapability(
        capability_id="market.wallet.read",
        name="Read NovaCoin wallets",
        description="Read wallet balances, transactions and ledger entries.",
        operation=MarketOperation.READ_WALLET,
    ),
    MarketCapability(
        capability_id="market.wallet.move",
        name="Move simulated NovaCoin",
        description="Deposit, withdraw and transfer NC between wallets.",
        operation=MarketOperation.MOVE_SIMULATED_FUNDS,
    ),
    MarketCapability(
        capability_id="market.ledger.reconcile",
        name="Reconcile the ledger",
        description="Recompute balances from ledger entries and report integrity.",
        operation=MarketOperation.READ_WALLET,
    ),
    MarketCapability(
        capability_id="market.ledger.correct",
        name="Reverse a transaction",
        description="Post compensating entries against a posted transaction.",
        operation=MarketOperation.CORRECT_LEDGER,
    ),
    # NM-04: evidence-backed market events.
    MarketCapability(
        capability_id="market.events.read",
        name="Read market events",
        description=("Search stored market events, their evidence, mappings and history."),
        operation=MarketOperation.READ_EVENTS,
    ),
    MarketCapability(
        capability_id="market.events.refresh",
        name="Refresh event sources",
        description=("Fetch a configured event feed and store the events it reports."),
        operation=MarketOperation.REFRESH_EVENTS,
    ),
    MarketCapability(
        capability_id="market.events.sources",
        name="Event source health",
        description="Report configured feeds and the outcome of their last fetch.",
        operation=MarketOperation.READ_EVENTS,
    ),
    # NM-05: autonomous discovery and watchlists.
    MarketCapability(
        capability_id="market.discovery.candidates",
        name="Read discovery candidates",
        description=("Read scored candidates, their factor breakdown and evaluation history."),
        operation=MarketOperation.DISCOVER_ASSETS,
    ),
    MarketCapability(
        capability_id="market.discovery.run",
        name="Run asset discovery",
        description="Run a bounded discovery cycle and reevaluate watchlist members.",
        operation=MarketOperation.DISCOVER_ASSETS,
    ),
    MarketCapability(
        capability_id="market.watchlist.read",
        name="Read watchlists",
        description="Read watchlists, members, rankings and membership history.",
        operation=MarketOperation.READ_WATCHLISTS,
    ),
    MarketCapability(
        capability_id="market.watchlist.manage",
        name="Manage watchlists",
        description="Create watchlists and add or remove instrument memberships.",
        operation=MarketOperation.MANAGE_WATCHLISTS,
    ),
    # NM-06: probabilistic forecasting.
    MarketCapability(
        capability_id="market.forecast.read",
        name="Read forecasts",
        description="Read forecasts, outcomes, models, calibration and performance.",
        operation=MarketOperation.READ_FORECASTS,
    ),
    MarketCapability(
        capability_id="market.forecast.run",
        name="Run forecasting",
        description="Generate and evaluate probabilistic forecasts from stored data.",
        operation=MarketOperation.RUN_FORECASTS,
    ),
    MarketCapability(
        capability_id="market.forecast.train",
        name="Train forecast models",
        description="Run walk-forward model comparison and calibration.",
        operation=MarketOperation.TRAIN_FORECAST_MODELS,
    ),
    MarketCapability(
        capability_id="market.forecast.models",
        name="Read forecast models",
        description="Read model versions, states, metrics and reliability history.",
        operation=MarketOperation.READ_FORECASTS,
    ),
    MarketCapability(
        capability_id="market.portfolio.read",
        name="Portfolio records",
        description="Read portfolios, holdings, valuations and decisions.",
        operation=MarketOperation.READ_PORTFOLIOS,
    ),
    MarketCapability(
        capability_id="market.portfolio.manage",
        name="Portfolio lifecycle",
        description="Create, pause, resume and close simulated portfolios.",
        operation=MarketOperation.MANAGE_SIMULATED_PORTFOLIO,
    ),
    MarketCapability(
        capability_id="market.portfolio.rebalance",
        name="Portfolio rebalancing",
        description="Propose and apply a validated simulated rebalance.",
        operation=MarketOperation.MANAGE_SIMULATED_PORTFOLIO,
    ),
    MarketCapability(
        capability_id="market.risk.assess",
        name="Independent risk assessment",
        description="Assess a proposed action against versioned risk policies.",
        operation=MarketOperation.ASSESS_RISK,
    ),
    MarketCapability(
        capability_id="market.risk.read",
        name="Risk decisions and metrics",
        description="Read decisions, portfolio risk, exposure, alerts and history.",
        operation=MarketOperation.READ_RISK,
    ),
    MarketCapability(
        capability_id="market.risk.policy",
        name="Versioned risk policies",
        description="Read and create audited risk-policy versions.",
        operation=MarketOperation.MANAGE_RISK_POLICY,
    ),
    MarketCapability(
        capability_id="market.risk.governance",
        name="Risk overrides and alert lifecycle",
        description="Record time-bounded overrides and acknowledge or resolve alerts.",
        operation=MarketOperation.OVERRIDE_RISK,
    ),
    MarketCapability(
        capability_id="market.strategy.read",
        name="Strategy records and evidence",
        description="Read definitions, signals, proposals, conflicts and performance.",
        operation=MarketOperation.READ_STRATEGY,
    ),
    MarketCapability(
        capability_id="market.strategy.manage",
        name="Strategy lifecycle and configuration",
        description="Create, version, activate, pause, degrade, suspend and retire strategies.",
        operation=MarketOperation.MANAGE_STRATEGY,
    ),
    MarketCapability(
        capability_id="market.strategy.evaluate",
        name="Signal and proposal generation",
        description="Evaluate strategies and submit proposals to the independent risk engine.",
        operation=MarketOperation.RUN_STRATEGIES,
    ),
    MarketCapability(
        capability_id="market.execution.submit",
        name="Paper order execution",
        description="Execute approved proposals as simulated orders with fees, slippage and partial fills.",
        operation=MarketOperation.EXECUTE_PAPER_TRADE,
    ),
    MarketCapability(
        capability_id="market.execution.read",
        name="Paper order records",
        description="Read simulated orders, fills, reservations and execution status.",
        operation=MarketOperation.READ_EXECUTION,
    ),
    MarketCapability(
        capability_id="market.execution.reconcile",
        name="Paper execution reconciliation",
        description="Independently verify orders, fills, ledger references and portfolio positions.",
        operation=MarketOperation.RECONCILE_EXECUTION,
    ),
    MarketCapability(
        capability_id="market.backtest.read",
        name="Read backtests",
        description="Read persisted historical simulations, metrics and evidence.",
        operation=MarketOperation.READ_BACKTESTS,
    ),
    MarketCapability(
        capability_id="market.backtest.run",
        name="Run reproducible backtests",
        description="Replay historical bars with risk, execution, fees and accounting.",
        operation=MarketOperation.RUN_BACKTESTS,
    ),
    MarketCapability(
        capability_id="market.backtest.compare",
        name="Compare and verify backtests",
        description="Compare completed runs and replay their persisted evidence.",
        operation=MarketOperation.COMPARE_BACKTESTS,
    ),
    MarketCapability(
        capability_id="market.shadow.read",
        name="Read Shadow Mode",
        description="Read sessions, decisions, hypothetical projections and outcomes.",
        operation=MarketOperation.READ_SHADOW,
    ),
    MarketCapability(
        capability_id="market.shadow.manage",
        name="Manage shadow sessions",
        description="Create, start, pause, resume, stop, complete and cancel shadow sessions.",
        operation=MarketOperation.MANAGE_SHADOW,
    ),
    MarketCapability(
        capability_id="market.shadow.run",
        name="Run shadow cycles",
        description="Observe market data and record hypothetical decisions and projections.",
        operation=MarketOperation.RUN_SHADOW,
    ),
    MarketCapability(
        capability_id="market.shadow.compare",
        name="Compare shadow evidence",
        description="Compare shadow decisions with NM-08, NM-09, NM-10 and NM-11 references.",
        operation=MarketOperation.COMPARE_SHADOW,
    ),
    MarketCapability(
        capability_id="market.learning.read",
        name="Read learning evidence",
        description="Read evidence, experiments, candidates, alerts and versions.",
        operation=MarketOperation.READ_LEARNING,
    ),
    MarketCapability(
        capability_id="market.learning.run",
        name="Run learning experiments",
        description="Generate and evaluate bounded candidate configurations against a baseline.",
        operation=MarketOperation.RUN_LEARNING,
    ),
    MarketCapability(
        capability_id="market.learning.manage",
        name="Manage learning records",
        description="Record evidence, monitor strategies and cancel experiments.",
        operation=MarketOperation.MANAGE_LEARNING,
    ),
    MarketCapability(
        capability_id="market.learning.approve",
        name="Approve and promote learning",
        description="Approve, reject, promote and roll back strategy versions.",
        operation=MarketOperation.APPROVE_LEARNING,
    ),
    # PI-01: prediction intelligence contracts and immutable evidence.
    MarketCapability(
        capability_id="market.prediction.read",
        name="Read prediction intelligence",
        description=(
            "Read forecast lifecycles, immutable confirmation stages, "
            "evaluations and model metadata."
        ),
        operation=MarketOperation.READ_PREDICTIONS,
    ),
    MarketCapability(
        capability_id="market.prediction.run",
        name="Generate and confirm forecasts",
        description=(
            "Assemble eligible model outputs, preserve T-20/T-5/T-1 evidence "
            "and evaluate actual outcomes; never place an order."
        ),
        operation=MarketOperation.RUN_PREDICTIONS,
    ),
    MarketCapability(
        capability_id="market.prediction.models",
        name="Prediction model registry",
        description=(
            "Register versioned model metadata and eligibility without automatic promotion."
        ),
        operation=MarketOperation.MANAGE_PREDICTION_MODELS,
    ),
    MarketCapability(
        capability_id="market.prediction.evidence",
        name="Prediction decision evidence",
        description=(
            "Provide read-only forecast evidence to the existing decision "
            "pipeline with authorizes_action=false."
        ),
        operation=MarketOperation.READ_DECISION_EVIDENCE,
    ),
    # PI-02: versioned historical datasets and leakage-safe training sets.
    MarketCapability(
        capability_id="market.dataset.read",
        name="Read historical datasets",
        description=(
            "Read dataset versions, exact membership, coverage, quality and "
            "classified missing observations."
        ),
        operation=MarketOperation.READ_DATASETS,
    ),
    MarketCapability(
        capability_id="market.dataset.build",
        name="Build historical datasets",
        description=(
            "Acquire bounded history through the existing approved providers, "
            "validate it and persist an immutable version."
        ),
        operation=MarketOperation.BUILD_DATASETS,
    ),
    MarketCapability(
        capability_id="market.dataset.manage",
        name="Manage corporate actions and annotations",
        description=(
            "Record sourced corporate actions and market facts used for gap "
            "classification; never rewrites an observation."
        ),
        operation=MarketOperation.MANAGE_DATASETS,
    ),
    # PI-03: the central low-latency gateway.
    MarketCapability(
        capability_id="market.gateway.read",
        name="Read through the market-data gateway",
        description=(
            "Read current observations, provenance, freshness, checkpoint "
            "answers, provider contracts and latency evidence."
        ),
        operation=MarketOperation.READ_MARKET,
        requires_provider=True,
    ),
    MarketCapability(
        capability_id="market.gateway.intraday",
        name="Run the intraday acquisition path",
        description=(
            "Run a bounded priority/fair-rotation cycle through the existing "
            "providers; never bypasses provider limits or the general floor."
        ),
        operation=MarketOperation.READ_MARKET,
        requires_provider=True,
    ),
    MarketCapability(
        capability_id="market.gateway.manage",
        name="Manage gateway subscriptions",
        description=(
            "Subscribe instruments and assign legitimate application "
            "priorities without changing provider limits."
        ),
        operation=MarketOperation.MANAGE_CONFIG,
    ),
    # PI-04: feature engineering and regime detection.
    MarketCapability(
        capability_id="market.feature.read",
        name="Read feature sets and regimes",
        description=(
            "Read versioned feature definitions, point-in-time feature sets, "
            "market regimes and their quality and provenance evidence."
        ),
        operation=MarketOperation.READ_FEATURES,
    ),
    MarketCapability(
        capability_id="market.feature.generate",
        name="Generate features and detect regimes",
        description=(
            "Derive price, indicator, structure, multi-timeframe, context, "
            "fundamental and event features from validated observations and "
            "persist one immutable feature set with its regime."
        ),
        operation=MarketOperation.GENERATE_FEATURES,
    ),
    MarketCapability(
        capability_id="market.feature.manage",
        name="Manage feature preprocessing and diagnostics",
        description=(
            "Fit versioned preprocessing on training rows and record feature "
            "selection diagnostics without automatically deleting features."
        ),
        operation=MarketOperation.MANAGE_FEATURES,
    ),
)


def _radar_moment(value: str | None, field_name: str) -> Any:
    """Parse an ISO query bound; a timezone is required, never assumed."""
    if value in (None, ""):
        return None
    from datetime import datetime as _datetime

    try:
        parsed = _datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise EventValidationError(
            f"{field_name} must be an ISO-8601 timestamp, got {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise EventValidationError(
            f"{field_name} must carry a timezone (for example 2026-11-02T00:00:00Z)"
        )
    return parsed


def _filings_disabled() -> bool:
    """Whether the process opted out of the free filing reader.

    Tests and offline machines set ``NOVACONTROL_DISABLE_FILINGS=1`` so a
    research run never reaches the network; the adapter is the only outbound
    call in the research context.
    """
    value = str(os.environ.get("NOVACONTROL_DISABLE_FILINGS") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


class NovaMarketService:
    """The one object NovaControl uses to manage NovaMarket."""

    def __init__(
        self,
        *,
        store: StateStore | None = None,
        config: NovaMarketConfig | None = None,
        event_bus: EventBus | None = None,
        permissions: PermissionManager | None = None,
        capabilities: Any | None = None,
        validators: Sequence[Callable[[], Any]] = (),
        clock: Callable[[], datetime] | None = None,
        namespace: str = STATE_NAMESPACE,
        data_dir: str | Path | None = None,
        market_data_path: str | Path | None = None,
        wallet_path: str | Path | None = None,
        transport: HttpTransport | None = None,
        secret_resolver: SecretResolver | None = None,
    ) -> None:
        self._store = store
        self._namespace = namespace
        self._clock = clock or (lambda: datetime.now(UTC))
        # The market-data repository is a SQLite file in the application's one
        # data directory, matching the existing memory store; without a data
        # directory it is in-memory so the subsystem still works in tests.
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._market_data_path = Path(market_data_path) if market_data_path is not None else None
        self._transport = transport
        self._secret_resolver: SecretResolver = secret_resolver or EnvironmentSecretResolver()
        self._market_store: MarketDataStore | None = None
        self._market_engine: MarketDataEngine | None = None
        # PI-03: the controlled gateway over the same engine; it owns no store.
        self._gateway: MarketDataGateway | None = None
        self._gateway_error = ""
        # NM-03's ledger: the same data directory (and by default the same
        # SQLite file) as the market-data repository, with its own version key.
        self._wallet_path = Path(wallet_path) if wallet_path is not None else None
        self._wallet_store: WalletStore | None = None
        self._wallet_engine: WalletEngine | None = None
        self._wallet_error = ""
        # NM-04: the event radar. Same database file, its own schema key.
        self._radar_store: RadarEventStore | None = None
        self._radar_engine: MarketEventRadar | None = None
        self._radar_error = ""
        # NM-05: asset discovery and watchlists. Same database file, its own
        # schema key.
        self._discovery_store: DiscoveryStore | None = None
        self._discovery_engine: AssetDiscoveryEngine | None = None
        self._discovery_error = ""
        # NM-06: probabilistic forecasting.
        self._forecast_store: ForecastStore | None = None
        self._forecast_engine: ForecastEngine | None = None
        self._forecast_error = ""
        # PI-05: additive statistical baselines over the same stores.
        self._statistical_service: StatisticalForecastingService | None = None
        self._statistical_error = ""
        # PI-06: gradient models over the same PI-01/PI-02/PI-04/PI-05 stores.
        self._gradient_service: GradientForecastingService | None = None
        self._gradient_error = ""
        # PI-07: LSTM sequence models over the same stores and feature engine.
        self._lstm_service: LSTMForecastingService | None = None
        self._lstm_error = ""
        # PI-08: the evidence-weighted ensemble over PI-05/06/07. It composes
        # their public generate() surfaces and never retrains a constituent.
        self._ensemble_service: EnsembleForecastingService | None = None
        self._ensemble_error = ""
        self._outlook_service: HorizonForecastService | None = None
        # PI-09: one durable lifecycle scheduler over the existing gateway,
        # PI-08 ensemble and PI-01 stores.  It has no risk or execution path.
        self._forecast_scheduler: ForecastScheduler | None = None
        self._forecast_scheduler_error = ""
        self._forecast_auto_plan: dict[str, Any] = {}
        self._forecast_auto_plan_at: datetime | None = None
        # PI-01: prediction intelligence contracts and immutable evidence. It
        # reads the existing NM-02 engine and never becomes an execution path.
        self._prediction_service: PredictionIntelligenceService | None = None
        self._prediction_error = ""
        # PI-02: historical dataset engine. It reuses the same market-data
        # engine/store and the same SQLite file; it is not a second collector.
        self._dataset_service: HistoricalDatasetService | None = None
        self._dataset_error = ""
        # PI-04: feature engineering and regime detection. It composes the
        # existing market engine/store, datasets, radar and prediction
        # contracts; it is not a second collector or indicator authority.
        self._feature_service: FeatureService | None = None
        self._feature_error = ""
        self._portfolio_store: Any | None = None
        self._portfolio_engine: "PortfolioEngine | None" = None
        self._portfolio_error = ""
        # NM-08: the independent risk layer.
        self._risk_store: Any | None = None
        self._risk_engine: "RiskEngine | None" = None
        self._risk_error = ""
        self._strategy_store: Any | None = None
        self._strategy_engine: "StrategyEngine | None" = None  # type: ignore[name-defined]
        self._strategy_error = ""
        # Post-PI-24: 20m context / 5m setup / 1m timing analysis. Evidence only;
        # it gates day-trading entries before NM-08 and never executes.
        self._timeframe_analyzer: Any | None = None
        self._timeframe_error = ""
        self._execution_engine: Any | None = None
        self._execution_store: Any | None = None
        self._execution_error = ""
        # AT-01: the durable autonomy shell. This object owns a mode and a
        # lease only; it has no strategy, risk, execution or portfolio path.
        self._autonomy_runtime: AutonomyRuntime | None = None
        self._autonomy_monitoring: AutonomyMonitoring | None = None
        self._decision_pipeline: Any | None = None
        self._decision_error = ""
        self._pi12_decision_source_cache: Any | None = None
        self._prediction_outcome_last: dict[str, Any] = {}
        self._prediction_outcome_error = ""
        # AT-04: controlled paper-order forwarding through the one NM-10 engine.
        self._autonomy_execution: Any | None = None
        self._autonomy_execution_error = ""
        # AT-05: stop-loss supervision over existing positions and NM-08/NM-10.
        self._autonomy_exits: Any | None = None
        self._autonomy_exits_error = ""
        self._autonomy_recovery: dict[str, Any] = {}
        self._autonomy_error = ""
        # A restart can find the previous process's unexpired lease still in
        # the store.  It is not stolen, but auto-start retries until that lease
        # must have expired so an unclean restart never stays disabled forever.
        self._autonomy_auto_start_retry_until: datetime | None = None
        # NM-11: reproducible historical backtesting. It owns an isolated
        # database and never shares the paper-execution or wallet stores.
        self._backtest_service: Any | None = None
        self._backtest_error = ""
        # PI-13: isolated reinforcement-learning research. It owns an isolated
        # SQLite file and has no NM-07/NM-08/NM-10/autonomy dependency.
        self._rl_service: Any | None = None
        self._rl_error = ""
        self._rl_forecast_cache: dict[str, list[Any]] = {}
        # PI-14: reward and policy evaluation. Additive and read-only with
        # respect to PI-13; it owns tables in the same isolated RL database and
        # has no production portfolio, risk, execution or autonomy handle.
        self._reward_service: Any | None = None
        self._reward_error = ""
        # PI-15: isolated experimental RL training. It has no production
        # portfolio, risk, execution, autonomy or broker handle.
        self._training_service: Any | None = None
        self._training_error = ""
        # PI-16: adaptive intelligence is an advisory research layer over the
        # PI-04/PI-14/PI-15 contracts. It owns adp_* tables and has no
        # production portfolio, risk, execution, autonomy or broker handle.
        self._adaptive_service: Any | None = None
        self._adaptive_error = ""
        # PI-17: coordinated financial-intelligence agents. It owns ma_* tables
        # in an isolated research file and consumes read-only evidence only.
        self._multiagent_service: Any | None = None
        self._multiagent_error = ""
        # PI-18: read-only portfolio optimization and capital allocation. It
        # owns po_* tables in an isolated research file and has no portfolio,
        # risk, execution, autonomy or broker handle.
        self._optimization_service: Any | None = None
        self._optimization_error = ""
        # PI-19: advanced market intelligence and financial research. It owns
        # pr_* tables in its own SQLite file and is strictly advisory; it has no
        # portfolio, ledger, risk-approval, execution or autonomy handle.
        self._research_service: Any | None = None
        self._research_error = ""
        # PI-19 filing source: one free, keyless adapter reads statements and
        # company facts; it is lazily built so import and boot stay offline.
        self._filings_reader: Any | None = None
        # PI-20: read-only prediction-intelligence dashboard composition. It
        # owns only presentation preferences and has no order, fill, position,
        # ledger, risk-approval, autonomy or broker handle.
        self._dashboard_service: Any | None = None
        self._dashboard_error = ""
        # PI-21: advanced autonomous portfolio management is an advisory
        # composition layer. It owns pm_* research tables only and can reach a
        # financial system exclusively by asking the existing PI-12/AT-04
        # executor to continue a persisted, NM-08-approved AT-03 proposal.
        self._portfolio_management_service: Any | None = None
        self._portfolio_management_error = ""
        # PI-21 scheduled reviews ride this runtime's tick, so the last refusal
        # or failure belongs to the tick rather than to a second scheduler.
        self._portfolio_management_scheduler_error = ""
        # PI-22: advanced risk intelligence is advisory and owns ri_* tables
        # only.  It reads authoritative portfolio/valuation state and NM-08
        # assessments; it never creates a risk decision or submits an order.
        self._risk_intelligence_service: Any | None = None
        self._risk_intelligence_error = ""
        # PI-23: forecast optimization is an advisory composition over PI-05
        # through PI-11. It owns fi_* evidence only; it cannot promote a model,
        # apply ensemble weights, create a risk decision or submit an order.
        self._forecast_intelligence_service: Any | None = None
        self._forecast_intelligence_error = ""
        # PI-24: event-aware intelligence is an advisory composition over NM-04
        # event evidence and PI-19 research. It owns ei_* records only and has
        # no feed, forecast-mutation, risk or execution authority.
        self._event_intelligence_service: Any | None = None
        self._event_intelligence_error = ""
        # PI-11: forecast evaluation is a read-only evidence layer over the
        # existing forecast stores. It owns a separate append-only report file
        # and has no order, fill, portfolio or ledger path.
        self._evaluation_service: Any | None = None
        self._evaluation_error = ""
        # NM-12: non-executing shadow mode. It owns a separate database and has
        # no execution, wallet, ledger or portfolio gateway.
        self._shadow_service: "ShadowService | None" = None
        self._shadow_error = ""
        # NM-13: controlled adaptive learning. It owns a separate database,
        # reads the other subsystems' records and has no execution path.
        self._learning_service: "LearningService | None" = None
        self._learning_error = ""
        # NM-14: operational activity and the shared local audit trail. The
        # audit logger is attached by the application after it constructs its
        # ONE audit logger; a service used standalone still exposes activity.
        self._audit_logger: Any | None = None
        self._activity: list[dict[str, Any]] = []
        self._activity_counter = 0
        self._last_audit_error = ""
        self._unknown_providers: tuple[str, ...] = ()
        self._market_ingestion_error = ""
        # Read-through board cache. A workbench snapshot is deterministic over
        # stored bars, and on this machine it costs ~1.75 s; a refresh or a tab
        # switch must not re-run the whole analysis. TTL is short enough that a
        # newly ingested bar is visible on the next desk refresh.
        self._workbench_cache: dict[tuple[str, str, int], tuple[float, dict[str, Any]]] = {}
        self._workbench_cache_ttl = 3.0
        # Market Overview is the same deterministic board with different
        # filters. Under concurrent reads, all eight requests otherwise rebuild
        # and normalize all 4,811 rows at once; the measured p95 rose to ~11.5 s.
        # One TTL entry per exact query, guarded by one lock, gives concurrent
        # callers the same answer without a thundering herd. The TTL matches the
        # workbench cache, so a newly ingested bar is visible within 3 seconds.
        self._screener_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
        self._screener_cache_ttl = 3.0
        self._screener_cache_lock = RLock()
        self._permission_model = MarketPermissionModel()
        self._permissions = permissions
        self._capability_registry = capabilities
        self._validators = tuple(validators)
        for validator in self._validators:
            if not callable(validator):
                raise TypeError("validators must be callables")

        self._config_error = ""
        self._config = self._load_config(store, config, namespace)
        self._publisher = MarketEventPublisher(event_bus, emit_enabled=self._config.emit_events)
        # Every component receives this same publisher. The observer is what
        # makes wallet, risk, execution and learning events visible to the
        # workspace/audit without adding a second event bus.
        self._publisher.set_observer(self._observe_market_event)
        # Operator notifications and the continuous monitor. The SMTP
        # credential is read from the environment; only the recipient and the
        # thresholds are persisted in NovaMarket's own configuration.
        self._notification_center = NotificationCenter(
            self,
            store=self._store,
            namespace=f"{self._namespace}.notifications",
            clock=self._clock,
        )
        self._runtime_task: asyncio.Task[Any] | None = None
        # The autonomy lease is short by design. Renew it independently of the
        # 15-minute market refresh so a closed market cannot silently expire a
        # running operator-owned runtime.
        self._autonomy_heartbeat_task: asyncio.Task[Any] | None = None
        self._runtime_error = ""
        self._runtime_last_tick: datetime | None = None
        self._runtime_last_event_refresh: datetime | None = None
        self._runtime_last_position_refresh: datetime | None = None
        self._runtime_interval_seconds = 60
        self._lifecycle = LifecycleMachine(clock=self._clock)
        self._architecture_problems = (
            *validate_architecture(),
            *validate_horizon_profiles(),
        )

        self._registered_capabilities: list[str] = []
        self._declared_permissions: tuple[str, ...] = ()
        self._subscriptions: dict[str, Callable[[Event], Any]] = {}
        self._configuration_handler = self._handle_configuration_request

        self._last_error = ""
        self._last_event_error = ""
        self._last_persist_error = ""
        self._initialized_at: datetime | None = None
        self._started_at: datetime | None = None
        self._health_state = HealthState.SKIPPED
        self._health_reasons: tuple[str, ...] = ("subsystem constructed; not initialised",)
        self._counters: dict[str, int] = {
            "initializations": 0,
            "enables": 0,
            "disables": 0,
            "reinitializations": 0,
            "configuration_updates": 0,
            "errors": 0,
            "event_failures": 0,
            "persist_failures": 0,
        }
        self._apply_log_level()
        if self._config_error:
            _LOGGER.warning("NovaMarket stored configuration is invalid: %s", self._config_error)

    # -- construction ---------------------------------------------------------

    def _load_config(
        self,
        store: StateStore | None,
        config: NovaMarketConfig | None,
        namespace: str,
    ) -> NovaMarketConfig:
        """The persisted snapshot, a caller's config, or validated defaults.

        A stored snapshot that does not validate keeps the defaults for display
        but sets ``_config_error``. Initialisation then refuses to reach READY,
        so a corrupt file is an honest ERROR rather than a silent factory reset.
        """
        if isinstance(config, NovaMarketConfig):
            return config
        try:
            payload = store.read(namespace) if store is not None else {}
        except Exception as exc:  # noqa: BLE001 - construction must not fail the app
            # The application builds this service while it is starting. A store
            # that cannot answer is a failed mandatory check, not a reason for
            # NovaControl to fail to boot; initialisation will report it as ERROR.
            self._config_error = (
                f"stored configuration could not be read: {type(exc).__name__}: {exc}"
            )
            return NovaMarketConfig()
        raw = payload.get("config") if isinstance(payload, Mapping) else None
        if raw is None:
            return NovaMarketConfig()
        try:
            return NovaMarketConfig.from_mapping(raw)
        except (ConfigurationError, TypeError, ValueError) as exc:
            self._config_error = f"stored configuration is invalid: {exc}"
            return NovaMarketConfig()

    @property
    def name(self) -> str:
        return SUBSYSTEM_NAME

    @property
    def state(self) -> LifecycleState:
        return self._lifecycle.state

    @property
    def config(self) -> NovaMarketConfig:
        return self._config

    @property
    def config_error(self) -> str:
        return self._config_error

    @property
    def permission_model(self) -> MarketPermissionModel:
        return self._permission_model

    @property
    def event_publisher(self) -> MarketEventPublisher:
        return self._publisher

    # -- integration seams ----------------------------------------------------

    def attach_event_bus(self, event_bus: EventBus | None) -> None:
        """Point the publisher at the application's one bus (idempotent)."""
        self._publisher.attach(event_bus)
        self._publisher.emit_enabled = self._config.emit_events

    # -- dashboard refresh announcements --------------------------------------
    # PI-17, PI-18, PI-19 and PI-14..PI-16 operations are synchronous engines
    # called from threads, so they publish nothing themselves: the async seam
    # that awaited the operation announces the committed result on the app's
    # one EventBus. One announcement per completed operation is what lets an
    # open dashboard view refetch without a polling loop.

    async def announce_agent_cycle(self, result: Mapping[str, Any]) -> None:
        """Announce a freshly completed PI-17 decision cycle."""
        cycle = dict(result.get("cycle") or {})
        if not cycle.get("cycle_id") or result.get("idempotent_reuse"):
            return
        decision = dict(result.get("decision") or {})
        await self._publish(
            EventType.NOVAMARKET_AGENT_CYCLE_COMPLETED,
            cycle_id=str(cycle.get("cycle_id") or ""),
            instrument_id=str(cycle.get("instrument_id") or ""),
            horizon=str(cycle.get("horizon") or ""),
            status=str(cycle.get("status") or ""),
            action=str(decision.get("action") or ""),
        )

    async def announce_research_run(self, result: Mapping[str, Any]) -> None:
        """Announce a PI-19 research run that reached a terminal status."""
        run_id = str(result.get("run_id") or "")
        status = str(result.get("status") or "")
        if not run_id or status in {"", "pending", "running"}:
            return
        await self._publish(
            EventType.NOVAMARKET_RESEARCH_RUN_COMPLETED,
            run_id=run_id,
            instrument_id=str(result.get("instrument_id") or ""),
            status=status,
            stance=str((result.get("thesis") or {}).get("stance") or ""),
        )

    async def announce_optimization_run(self, result: Mapping[str, Any]) -> None:
        """Announce a PI-18 run that produced an allocation recommendation."""
        run = dict(result.get("run") or {})
        run_id = str(run.get("run_id") or "")
        status = str(run.get("status") or "")
        if not run_id or status not in {"completed", "completed_with_warnings"}:
            return
        rebalancing = dict(run.get("rebalancing") or {})
        intents = rebalancing.get("intents")
        await self._publish(
            EventType.NOVAMARKET_OPTIMIZATION_RECOMMENDATION_CREATED,
            run_id=run_id,
            portfolio_id=str(run.get("portfolio_id") or ""),
            status=status,
            rebalance_action=str(rebalancing.get("action") or ""),
            recommendation_count=len(intents) if isinstance(intents, Sequence) else 0,
        )

    async def announce_policy_degradation(self, result: Mapping[str, Any]) -> None:
        """Announce a newly recorded PI-16 policy-degradation alert."""
        degradation = dict(result.get("degradation") or {})
        status = str(degradation.get("status") or "")
        if not degradation.get("policy_id") or not result.get("inserted"):
            return
        if status not in {"temporary_underperformance", "persistent_degradation"}:
            return
        await self._publish(
            EventType.NOVAMARKET_POLICY_DEGRADED,
            policy_id=str(degradation.get("policy_id") or ""),
            status=status,
            trigger_count=int(degradation.get("trigger_count") or 0),
            retraining_recommended=status == "persistent_degradation",
        )

    def capabilities(self) -> tuple[RuntimeCapability, ...]:
        """The runtime module's declared capabilities (the core contract)."""
        return tuple(
            RuntimeCapability(
                name=item.capability_id,
                description=item.description,
                permissions=(item.operation.value,),
            )
            for item in CAPABILITIES
        )

    def capability_records(self) -> tuple[dict[str, Any], ...]:
        """Every NM-01 capability with live availability and its reason."""
        records: list[dict[str, Any]] = []
        for item in CAPABILITIES:
            available, reason = self._capability_availability(item)
            decision = self._permission_model.authorize(item.operation)
            records.append(
                {
                    **item.to_dict(),
                    "phase": IMPLEMENTED_PHASE,
                    "available": available,
                    "reason": reason,
                    "granted": decision.allowed,
                    "risk": decision.risk.value,
                }
            )
        return tuple(records)

    # -- evidence-backed market events (NM-04) --------------------------------

    @property
    def radar(self) -> MarketEventRadar | None:
        """The event radar, or ``None`` before initialisation / after shutdown."""
        return self._radar_engine

    def _autonomy_path(self) -> Any:
        """The existing NovaMarket SQLite file; autonomy has its own schema key."""

        if self._market_data_path is not None:
            return self._market_data_path
        if self._data_dir is not None:
            return self._data_dir / "novamarket.sqlite3"
        return ":memory:"

    def _ensure_autonomy(self) -> AutonomyRuntime:
        """Lazily open the autonomy shell; it never auto-resumes a mode."""

        if self._autonomy_runtime is None or self._autonomy_runtime.closed:
            try:
                self._autonomy_runtime = AutonomyRuntime(self._autonomy_path(), clock=self._clock)
                self._autonomy_error = ""
            except Exception as exc:  # noqa: BLE001 - status must explain, not crash
                self._autonomy_error = f"{type(exc).__name__}: {exc}"
                raise
        return self._autonomy_runtime

    def _ensure_monitoring(self) -> AutonomyMonitoring:
        """Lazily open the AT-02 monitoring runner; it never trades."""

        runtime = self._ensure_autonomy()
        if self._autonomy_monitoring is None or self._autonomy_monitoring.closed:
            self._autonomy_monitoring = AutonomyMonitoring(
                runtime,
                discovery=lambda: self._discovery_engine,
                clock=self._clock,
                instrument_lookup=self._autonomy_instrument_lookup,
                universe=lambda: self._config.universe,
            )
        return self._autonomy_monitoring

    def _autonomy_instrument_lookup(self, instrument_id: str) -> dict[str, Any] | None:
        """Validate a discovered instrument against NM-02 when it is open."""

        engine = self._market_engine
        if engine is None:
            return None
        return dict(engine.get_instrument(instrument_id).to_dict())

    def autonomy_status(
        self,
        *,
        operator_authorized: bool = False,
        operator_configured: bool | None = None,
    ) -> dict[str, Any]:
        """Server-authoritative AT-01 status; no order or financial claim."""

        configured = (
            operator_configured
            if operator_configured is not None
            else bool(os.getenv("NOVACONTROL_AUTONOMY_OPERATOR_TOKEN"))
        )
        runtime = self._autonomy_runtime
        if runtime is None:
            # A subsystem that never started must not open a store just to
            # answer a status read; report the honest closed shell instead.
            if self._lifecycle.state is not LifecycleState.RUNNING:
                return closed_status(
                    operator_authorized=operator_authorized,
                    operator_configured=configured,
                )
            runtime = self._ensure_autonomy()
        status = runtime.status(
            operator_authorized=operator_authorized,
            operator_configured=configured,
        )
        retry_until = self._autonomy_auto_start_retry_until
        status["auto_start_retry"] = {
            "active": retry_until is not None,
            "retry_until": retry_until.isoformat() if retry_until else None,
            "reason": (
                "a live foreign lease blocks application auto-start; "
                "retrying until that lease expires (it is never stolen)"
                if retry_until is not None
                else ""
            ),
        }
        return status

    def autonomy_start(
        self,
        mode: AutonomyMode | str,
        *,
        reason: str = "",
        acknowledge_paper_only: bool = False,
        actor: str = "system",
    ) -> dict[str, Any]:
        started = self._ensure_autonomy().start(
            mode,
            reason=reason,
            acknowledge_paper_only=acknowledge_paper_only,
            actor=actor,
        )
        self._clear_autonomy_auto_start_retry()
        return started

    def autonomy_pause(
        self, *, reason: str = "", actor: str = "system"
    ) -> dict[str, Any]:
        return self._ensure_autonomy().pause(reason=reason, actor=actor)

    def autonomy_resume(
        self,
        mode: AutonomyMode | str | None = None,
        *,
        reason: str = "",
        acknowledge_paper_only: bool = False,
        actor: str = "system",
    ) -> dict[str, Any]:
        return self._ensure_autonomy().resume(
            mode,
            reason=reason,
            acknowledge_paper_only=acknowledge_paper_only,
            actor=actor,
        )

    def autonomy_stop(
        self, *, reason: str = "", actor: str = "system"
    ) -> dict[str, Any]:
        """Operator/emergency stop: disable the mode and the auto-start opt-in."""

        stopped = self._ensure_autonomy().stop(
            reason=reason, disable_autostart=True, actor=actor
        )
        self._clear_autonomy_auto_start_retry()
        return stopped

    async def autonomy_recover(self, *, reason: str = "operator recovery") -> dict[str, Any]:
        """Reconcile durable autonomy work without resuming autonomous mode.

        Recovery is a read/reconcile operation, never a replay.  It closes the
        runtime lease, interrupts abandoned cycles, marks abandoned decision
        runs failed, reconciles NM-10 order/fill records and pending exit
        decisions.  The resulting mode is always ``disabled``; an operator must
        explicitly start or resume afterwards.
        """

        runtime = self._ensure_autonomy()
        self._clear_autonomy_auto_start_retry()
        runtime_status = runtime.recover()
        decision_pipeline = self._ensure_decision()
        interrupted_runs = int(decision_pipeline.recover() or 0)
        execution_result = await self._ensure_autonomy_execution().recover()
        exit_result = await self._ensure_autonomy_exits().recover()
        unresolved = list(execution_result.get("unresolved") or ())
        unresolved.extend(exit_result.get("unresolved") or ())
        result = {
            "ok": True,
            "status": "completed",
            "phase": "AT-06",
            "reason": reason,
            "recovered_at": datetime.now(UTC).isoformat(),
            "runtime": runtime_status,
            "mode": runtime_status.get("mode", "disabled"),
            "interrupted_decision_runs": interrupted_runs,
            "execution": execution_result,
            "exits": exit_result,
            "recovered": int(execution_result.get("recovered") or 0)
            + int(exit_result.get("recovered") or 0),
            "reconciled": int(execution_result.get("reconciled") or 0)
            + int(exit_result.get("reconciled") or 0),
            "unresolved": unresolved,
            "unresolved_count": len(unresolved),
            "auto_resumed": False,
            "paper_only": True,
            "live_trading": False,
            "broker_connected": False,
        }
        self._autonomy_recovery = result
        return result

    def autonomy_reliability(self) -> dict[str, Any]:
        """One read-only AT-06 view composed from the authoritative statuses."""

        runtime = self.autonomy_status()
        monitoring = self.autonomy_monitoring_status()
        decisions = self.autonomy_decision_status()
        execution = self.autonomy_execution_status()
        exits = self.autonomy_exits_status()
        learning = (
            self.autonomy_learning_evidence(limit=500)
            if self._lifecycle.state is LifecycleState.RUNNING
            else {"evidence": [], "total": 0}
        )
        return build_reliability_report(
            runtime=runtime,
            monitoring=monitoring,
            decisions=decisions,
            execution=execution,
            exits=exits,
            learning=learning,
            recovery=self._autonomy_recovery,
        )

    def autonomy_contract(self) -> dict[str, Any]:
        """Integrated paper-runtime metadata; no user or financial record."""

        from novacontrol.novamarket.autonomy.models import (
            AUTOMATIC_POLICY_PROMOTION,
            BROKER_CONNECTED,
            EXIT_SUPERVISION_IMPLEMENTED,
            LEARNING_PROMOTION_IMPLEMENTED,
        )

        return {
            "milestone": "AT-06",
            "status": "paper-only autonomous runtime (AT-01 through AT-06)",
            "trading_implemented": TRADING_IMPLEMENTED,
            "paper_only": PAPER_ONLY,
            "live_trading": LIVE_TRADING,
            "broker_connected": BROKER_CONNECTED,
            "orders_possible": TRADING_IMPLEMENTED,
            "live_orders_possible": False,
            "release_gate": RELEASE_GATE,
            "modes": [mode.value for mode in AutonomyMode],
            "runtime_states": [
                "stopped",
                "starting",
                "running",
                "recovering",
                "degraded",
                "stopping",
                "failed",
            ],
            "mode_transitions": {
                mode.value: sorted(item.value for item in targets)
                for mode, targets in MODE_TRANSITIONS.items()
            },
            "one_cycle_at_a_time": True,
            "paused_exit_policy": "protective",
            "exit_supervision_implemented": EXIT_SUPERVISION_IMPLEMENTED,
            "learning_promotion_implemented": LEARNING_PROMOTION_IMPLEMENTED,
            "automatic_policy_promotion": AUTOMATIC_POLICY_PROMOTION,
            "horizon_mapping": dict(HORIZON_MAPPING),
            "calendar": dict(CALENDAR_STATUS),
            "storage": {
                "schema": "autonomy_meta",
                "version": 4,
                "tables": [
                    "autonomy_config",
                    "autonomy_runtime",
                    "autonomy_cycles",
                    "autonomy_monitoring_config",
                    "autonomy_monitoring_state",
                    "autonomy_monitoring_cycles",
                    "autonomy_monitoring_candidates",
                ],
            },
            "routes": [
                {"method": "GET", "path": "/market/autonomy/contracts"},
                {"method": "GET", "path": "/market/autonomy/status"},
                {"method": "GET", "path": "/market/autonomy/health"},
                {"method": "POST", "path": "/market/autonomy/start"},
                {"method": "POST", "path": "/market/autonomy/pause"},
                {"method": "POST", "path": "/market/autonomy/resume"},
                {"method": "POST", "path": "/market/autonomy/stop"},
            ],
            "authorization": {
                "ordinary_auth_sufficient": False,
                "header": "X-Autonomy-Operator-Token",
                "environment": "NOVACONTROL_AUTONOMY_OPERATOR_TOKEN",
                "server_side_enforced": True,
            },
            "limitations": [
                "Paper only: no live trading, broker connection or real-money execution exists.",
                "Every entry and exit still passes NM-08 risk approval and NM-10 execution.",
                "Automatic policy promotion is disabled; learning remains advisory.",
                "Market sessions, calendar coverage and data freshness fail closed.",
                "An operator stop disables auto-start; graceful shutdown preserves it.",
            ],
        }

    # -- AT-02 monitoring and session reads ---------------------------------

    def autonomy_monitoring_contract(self) -> dict[str, Any]:
        """Static AT-02 capabilities; no trading claim is possible."""

        return self._ensure_monitoring().contract()

    def autonomy_session_status(self, *, exchange: str = "") -> dict[str, Any]:
        """Evaluate the verified session for an exchange without opening a store."""

        mic = str(exchange or "").strip().upper()
        if not mic:
            monitoring = self._autonomy_monitoring
            mic = monitoring.config().exchange if monitoring is not None else "XNSE"
        return evaluate_session(mic, self._clock()).to_dict()

    def autonomy_monitoring_status(self) -> dict[str, Any]:
        """Server-authoritative AT-02 status; a closed service reads closed."""

        monitoring = self._autonomy_monitoring
        if monitoring is not None:
            return monitoring.status()
        if self._lifecycle.state is not LifecycleState.RUNNING:
            return closed_monitoring_status()
        return self._ensure_monitoring().status()

    def autonomy_monitoring_cycles(self, *, limit: int = 20) -> dict[str, Any]:
        monitoring = self._autonomy_monitoring
        if monitoring is None:
            if self._lifecycle.state is not LifecycleState.RUNNING:
                return {"ok": True, "cycles": [], "total": 0}
            monitoring = self._ensure_monitoring()
        return monitoring.cycles(limit=limit)

    def autonomy_monitoring_cycle(self, cycle_id: str) -> dict[str, Any]:
        monitoring = self._autonomy_monitoring
        if monitoring is None:
            if self._lifecycle.state is not LifecycleState.RUNNING:
                raise MonitoringRefused("CYCLE_NOT_FOUND", f"unknown monitoring cycle {cycle_id!r}")
            monitoring = self._ensure_monitoring()
        return monitoring.cycle(cycle_id)

    async def autonomy_monitoring_run(self, *, trigger: str = "manual") -> dict[str, Any]:
        """Run one authorized monitoring cycle through the same runner."""

        return await self._ensure_monitoring().run_cycle(trigger=trigger)

    def autonomy_monitoring_configure(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist validated monitoring configuration; no unsafe value is accepted."""

        monitoring = self._ensure_monitoring()
        current = monitoring.config()
        config = MonitoringConfig(
            enabled=bool(payload.get("enabled", current.enabled)),
            exchange=str(payload.get("exchange", current.exchange)),
            interval_seconds=int(payload.get("interval_seconds", current.interval_seconds)),
            max_instruments_per_cycle=int(
                payload.get(
                    "max_instruments_per_cycle",
                    current.max_instruments_per_cycle,
                )
            ),
            retry_max=int(payload.get("retry_max", current.retry_max)),
            cycle_timeout_seconds=int(
                payload.get("cycle_timeout_seconds", current.cycle_timeout_seconds)
            ),
        )
        saved = monitoring.save_config(config)
        return {"ok": True, **saved.to_dict()}

    # -- PI-12 prediction -> autonomous decision integration -----------------

    def _pi12_forecast_source(self) -> Any:
        """Read-only PI-08/PI-10/PI-11 source used by AT-03.

        It does not generate a forecast.  If the required PI-08 source cannot
        open or be read, the returned source raises on every read so AT-03
        fails closed instead of falling back to a strategy-only decision.
        """

        existing = getattr(self, "_pi12_decision_source_cache", None)
        if existing is not None:
            return existing
        from novacontrol.novamarket.decision.forecast import ForecastDecisionSource

        unavailable = ""
        try:
            ensemble = self.ensemble_service()
        except Exception as exc:  # noqa: BLE001 - required evidence fails closed
            ensemble = None
            unavailable = (
                f"PI-12 forecast source unavailable: {type(exc).__name__}: {exc}"
            )
        outlook = None
        evaluations = None
        try:
            outlook = self.outlook_service()
        except Exception:  # noqa: BLE001 - PI-10 context is optional
            outlook = None
        try:
            evaluations = self.evaluation_service()
        except Exception:  # noqa: BLE001 - PI-11 evidence is optional
            evaluations = None
        source = ForecastDecisionSource(ensemble, outlook, evaluations, clock=self._clock)
        self._pi12_decision_source_cache = source
        self._decision_error = unavailable
        return source

    def _pi12_feature_source(self) -> Any | None:
        try:
            return self.feature_service()
        except Exception:  # noqa: BLE001 - regime context is optional
            return None

    def _pi12_open_orders(self, portfolio_id: str, instrument_id: str) -> list[dict[str, Any]]:
        """Read-only NM-10 order awareness over the existing execution store.

        Opening the existing engine is read-only, but failing to open it must
        fail closed: an unknown open-order state cannot justify a new entry.
        """

        engine = self._execution_engine
        if engine is None:
            engine = self._ensure_execution()
        store = getattr(engine, "store", None)
        if store is None:
            raise RuntimeError("NM-10 order store is unavailable; open-order state is unknown")
        open_statuses = {
            "created",
            "pending_validation",
            "accepted",
            "open",
            "partially_filled",
            "cancel_pending",
        }
        rows: list[dict[str, Any]] = []
        for order in store.orders(portfolio_id=portfolio_id, limit=200):
            status = getattr(getattr(order, "status", None), "value", "")
            if (
                str(getattr(order, "instrument_id", "")) == instrument_id
                and status in open_statuses
            ):
                rows.append(order.to_dict())
        return rows

    def _pi12_risk_evidence(self, instrument_id: str) -> dict[str, Any]:
        """Map stored PI forecasts onto NM-08's existing forecast labels."""

        from novacontrol.novamarket.decision.forecast import risk_evidence_from_forecasts

        source = self._pi12_forecast_source()
        rows = source.forecasts(instrument_id) if source is not None else []
        return risk_evidence_from_forecasts(instrument_id, rows)

    # -- AT-03 decision and risk pipeline ------------------------------------

    def _ensure_decision(self) -> Any:
        """Open the AT-03 pipeline over the already accepted engines.

        It composes NM-09 evaluate() and NM-08 assess(); it never installs an
        NM-09 forwarder and never touches NM-10.  A restart marks an abandoned
        AT-03 run failed instead of resuming it.
        """

        from novacontrol.novamarket.decision import AutonomyDecisionPipeline
        from novacontrol.novamarket.decision.advisory import (
            MultiAgentAdvisoryEvidence,
        )
        from novacontrol.novamarket.decision.forecast_optimization import (
            ForecastOptimizationEvidence,
        )

        if self._decision_pipeline is not None and not self._decision_pipeline.closed:
            return self._decision_pipeline
        try:
            pipeline = AutonomyDecisionPipeline(
                self._ensure_autonomy(),
                self._strategy_service(),
                self._risk_service(),
                self._portfolio_service(),
                market_data=self._market_engine,
                clock=self._clock,
                forecasts=self._pi12_forecast_source(),
                features=self._pi12_feature_source(),
                radar=self._radar_engine,
                evaluations=self._evaluation_service,
                open_orders=self._pi12_open_orders,
                advisory_evidence=MultiAgentAdvisoryEvidence(self._ensure_multiagent),
                forecast_optimization_evidence=ForecastOptimizationEvidence(
                    self.forecast_intelligence_service
                ),
            )
            pipeline.recover()
        except Exception as exc:  # noqa: BLE001 - status must explain, not crash
            self._decision_error = f"{type(exc).__name__}: {exc}"
            raise
        self._decision_pipeline = pipeline
        self._decision_error = ""
        return pipeline

    def _decision_or_closed(self) -> Any | None:
        if self._decision_pipeline is not None and not self._decision_pipeline.closed:
            return self._decision_pipeline
        if self._lifecycle.state is not LifecycleState.RUNNING:
            return None
        return self._ensure_decision()

    def autonomy_decision_contract(self) -> dict[str, Any]:
        pipeline = self._decision_or_closed()
        if pipeline is None:
            return {
                "implemented": True,
                "available": False,
                "phase": "AT-03",
                "milestone": "AT-03",
                "version": "at03-decision-1.0",
                "detail": "the NovaMarket runtime is not enabled",
                "paper_only": True,
                "live_trading": False,
                "broker_connected": False,
                "execution_implemented": False,
                "orders_possible": False,
                "risk_engine": "NM-08",
                "strategy_engine": "NM-09",
                "risk_authority": "NM-08 is the sole risk authority",
                "proposal_statuses": [
                    "created",
                    "validation_pending",
                    "invalid",
                    "risk_pending",
                    "risk_approved",
                    "risk_rejected",
                    "risk_restricted",
                    "risk_review",
                    "expired",
                    "cancelled",
                ],
            }
        return cast(dict[str, Any], pipeline.contract())

    def autonomy_decision_status(self) -> dict[str, Any]:
        pipeline = self._decision_or_closed()
        if pipeline is None:
            return {
                "ok": True,
                "initialised": False,
                "phase": "AT-03",
                "implemented": True,
                "paper_only": True,
                "live_trading": False,
                "orders_possible": False,
                "detail": "the NovaMarket runtime is not enabled",
                "last_error": self._decision_error,
            }
        return cast(dict[str, Any], pipeline.status())

    def autonomy_prediction_decisions(
        self,
        *,
        run_id: str = "",
        portfolio_id: str = "",
        instrument_id: str = "",
        decision_type: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        pipeline = self._decision_or_closed()
        if pipeline is None:
            return {
                "ok": True,
                "decisions": [],
                "items": [],
                "total": 0,
                "limit": limit,
                "offset": offset,
                "has_more": False,
                "detail": "the NovaMarket runtime is not enabled",
            }
        return cast(
            dict[str, Any],
            pipeline.prediction_decisions(
                run_id=run_id,
                portfolio_id=portfolio_id,
                instrument_id=instrument_id,
                decision_type=decision_type,
                limit=limit,
                offset=offset,
            ),
        )

    def autonomy_prediction_decision(self, decision_id: str) -> dict[str, Any]:
        pipeline = self._decision_or_closed()
        if pipeline is None:
            from novacontrol.novamarket.decision import DecisionNotFound

            raise DecisionNotFound(f"unknown prediction decision {decision_id!r}")
        return cast(dict[str, Any], pipeline.prediction_decision_detail(decision_id))

    def autonomy_prediction_outcomes(self, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        pipeline = self._decision_or_closed()
        if pipeline is None:
            from novacontrol.novamarket.decision.forecast_metrics import (
                forecast_metrics,
            )

            return {
                "ok": True,
                "outcomes": [],
                "items": [],
                "total": 0,
                "limit": limit,
                "offset": offset,
                "has_more": False,
                "metrics": forecast_metrics(()),
            }
        body = cast(
            dict[str, Any],
            pipeline.prediction_outcomes(limit=limit, offset=offset),
        )
        from novacontrol.novamarket.decision.forecast_metrics import forecast_metrics

        all_rows = cast(
            dict[str, Any], pipeline.prediction_outcomes(limit=500, offset=0)
        )
        body["metrics"] = forecast_metrics(all_rows.get("items") or ())
        return body

    def track_prediction_outcomes(self, *, limit: int = 50) -> dict[str, Any]:
        """Measure due outcomes from committed records; it never mutates a book."""

        from novacontrol.novamarket.decision.outcomes import ForecastOutcomeTracker

        pipeline = self._decision_or_closed()
        if pipeline is None:
            return {
                "ok": True,
                "measured_count": 0,
                "pending_count": 0,
                "detail": "the NovaMarket runtime is not enabled",
            }
        tracker = ForecastOutcomeTracker(
            pipeline.store,
            self._market_engine,
            # Open the existing NM-10 engine lazily so an outcome measured
            # before this process submitted an order can still see its durable
            # order/fill records.  A failure is reported in the outcome; it can
            # never be mistaken for a fill.
            execution_provider=lambda: self._execution_engine or self._ensure_execution(),
            clock=self._clock,
        )
        result = tracker.track(limit=limit)
        try:
            from novacontrol.novamarket.decision.forecast_metrics import (
                forecast_metrics,
            )

            rows = pipeline.prediction_outcomes(limit=500, offset=0)
            result["metrics"] = forecast_metrics(rows.get("items") or ())
        except Exception as exc:  # noqa: BLE001 - tracking succeeded; metrics are separate
            result["metrics_error"] = f"{type(exc).__name__}: {exc}"
        self._prediction_outcome_last = result
        self._prediction_outcome_error = ""
        return result

    def autonomy_forecast_policy(self) -> dict[str, Any]:
        pipeline = self._decision_or_closed()
        if pipeline is None:
            from novacontrol.novamarket.decision.forecast import ForecastDecisionConfig

            config = ForecastDecisionConfig()
            return {
                "ok": True,
                "policy": config.to_dict(),
                "digest": config.digest(),
                "source": "default",
                "detail": "the NovaMarket runtime is not enabled",
            }
        return cast(dict[str, Any], pipeline.forecast_policy())

    def autonomy_forecast_policy_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        pipeline = self._decision_or_closed()
        if pipeline is None:
            from novacontrol.novamarket.decision import DecisionNotFound

            raise DecisionNotFound("the NovaMarket runtime is not enabled")
        return cast(dict[str, Any], pipeline.set_forecast_policy(payload))

    def autonomy_decision_runs(self, *, limit: int = 50) -> dict[str, Any]:
        pipeline = self._decision_or_closed()
        if pipeline is None:
            return {"ok": True, "runs": [], "total": 0}
        return cast(dict[str, Any], pipeline.runs(limit=limit))

    def autonomy_decision_run_detail(self, run_id: str) -> dict[str, Any]:
        pipeline = self._decision_or_closed()
        if pipeline is None:
            from novacontrol.novamarket.decision import DecisionNotFound

            raise DecisionNotFound(f"unknown decision run {run_id!r}")
        return cast(dict[str, Any], pipeline.run_detail(run_id))

    def autonomy_decision_signals(
        self,
        *,
        run_id: str = "",
        strategy_id: str = "",
        portfolio_id: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        pipeline = self._decision_or_closed()
        if pipeline is None:
            return {"ok": True, "signals": [], "total": 0, "orders_possible": False}
        return cast(
            dict[str, Any],
            pipeline.signals(
                run_id=run_id,
                strategy_id=strategy_id,
                portfolio_id=portfolio_id,
                limit=limit,
            ),
        )

    def autonomy_decision_proposals(
        self,
        *,
        run_id: str = "",
        portfolio_id: str = "",
        instrument_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        pipeline = self._decision_or_closed()
        if pipeline is None:
            return {
                "ok": True,
                "proposals": [],
                "total": 0,
                "execution_allowed": False,
                "orders_possible": False,
            }
        return cast(
            dict[str, Any],
            pipeline.proposals(
                run_id=run_id,
                portfolio_id=portfolio_id,
                instrument_id=instrument_id,
                status=status,
                limit=limit,
            ),
        )

    def autonomy_decision_proposal(self, proposal_id: str) -> dict[str, Any]:
        pipeline = self._decision_or_closed()
        if pipeline is None:
            from novacontrol.novamarket.decision import DecisionNotFound

            raise DecisionNotFound(f"unknown decision proposal {proposal_id!r}")
        return cast(dict[str, Any], pipeline.proposal_detail(proposal_id))

    async def autonomy_decision_run(
        self,
        *,
        monitoring_cycle_id: str = "",
        trigger: str = "manual",
        actor: str = "operator",
    ) -> dict[str, Any]:
        pipeline = self._decision_or_closed()
        if pipeline is None:
            from novacontrol.novamarket.decision import DecisionStateError

            raise DecisionStateError(
                "RUNTIME_DISABLED",
                "the NovaMarket runtime is not enabled; start monitoring first",
            )
        try:
            result = await pipeline.run_cycle(
                monitoring_cycle_id=monitoring_cycle_id,
                trigger=trigger,
                actor=actor,
            )
        except Exception as exc:  # noqa: BLE001 - preserve the decision error
            self._decision_error = f"{type(exc).__name__}: {exc}"
            raise
        self._decision_error = ""
        return cast(dict[str, Any], result)

    # -- AT-04 autonomous paper execution ------------------------------------

    def _ensure_autonomy_execution(self) -> Any:
        """Open AT-04 over the existing decision pipeline and NM-10 engine."""

        from novacontrol.novamarket.autonomy_execution import AutonomyPaperExecution

        if self._autonomy_execution is not None and not self._autonomy_execution.closed:
            return self._autonomy_execution
        try:
            pipeline = AutonomyPaperExecution(
                self._ensure_autonomy(),
                self._ensure_decision(),
                self._execution_service(),
                self._portfolio_service(),
                market_data=self._market_engine,
                clock=self._clock,
                publisher=self._execution_publish,
            )
        except Exception as exc:  # noqa: BLE001 - status must explain, not crash
            self._autonomy_execution_error = f"{type(exc).__name__}: {exc}"
            raise
        self._autonomy_execution = pipeline
        self._autonomy_execution_error = ""
        return pipeline

    def _autonomy_execution_or_closed(self) -> Any | None:
        if self._autonomy_execution is not None and not self._autonomy_execution.closed:
            return self._autonomy_execution
        if self._lifecycle.state is not LifecycleState.RUNNING:
            return None
        return self._ensure_autonomy_execution()

    # -- AT-05 protective exit supervision -----------------------------------
    def _exit_portfolio_view(self, portfolio_id: str = "", limit: int = 200) -> Any:
        engine = self._portfolio_service()
        if portfolio_id:
            return engine.detail(portfolio_id)
        return engine.portfolios(limit=max(1, min(int(limit), 200)))

    def _exit_learning_record(self, evidence: Mapping[str, Any]) -> Any:
        entry = evidence.get("entry") or {}
        exit_row = evidence.get("exit") or {}
        realized = evidence.get("realized") or {}
        return self._learning().record_evidence(
            {
                "kind": "paper",
                "strategy_id": entry.get("strategy_id") or "at05_protective_exit",
                "strategy_version": entry.get("strategy_version") or "unknown",
                "source_ref": evidence.get("decision_id"),
                "observations": 1,
                "instruments": [evidence.get("instrument_id", "")],
                "regimes": ["unknown"],
                "window_start": entry.get("opened_at") or None,
                "window_end": evidence.get("created_at") or None,
                "metrics": {
                    "evidence_complete": evidence.get("status") == "complete",
                    "exit_fill_count": len(exit_row.get("fills") or ()),
                    "holding_duration_seconds": evidence.get("holding_duration_seconds", 0),
                    "return_pct": realized.get("portfolio_return_pct"),
                },
                "out_of_sample": True,
                "metadata": {"at05": evidence},
                "notes": "AT-05 advisory exit evidence; never auto-promoted",
            }
        )

    def _ensure_autonomy_exits(self) -> Any:
        from novacontrol.novamarket.autonomy_exits import AutonomyExitSupervisor

        if self._autonomy_exits is not None:
            return self._autonomy_exits
        try:
            pipeline = AutonomyExitSupervisor(
                runtime=self._ensure_autonomy(),
                portfolios=self._exit_portfolio_view,
                market=self._market_data_engine(),
                risk=self._risk_service(),
                execution=self._execution_service(),
                decisions=self._ensure_decision(),
                learning_recorder=self._exit_learning_record,
                clock=self._clock,
                stale_after_seconds=int(self._execution_service().config.stale_after_seconds),
            )
        except Exception as exc:
            self._autonomy_exits_error = f"{type(exc).__name__}: {exc}"
            raise
        self._autonomy_exits = pipeline
        self._autonomy_exits_error = ""
        return pipeline

    def autonomy_exits_contract(self) -> dict[str, Any]:
        pipeline = self._autonomy_exits
        if pipeline is None:
            return {
                "implemented": True,
                "available": False,
                "phase": "AT-05",
                "paper_only": True,
                "live_trading": False,
                "broker_connected": False,
                "supported_exit_conditions": ["stop_loss", "target_price", "trailing_stop"],
                "unsupported_exit_conditions": [],
                "supported_directions": ["long"],
                "detail": "AT-05 supervision opens with the NovaMarket runtime",
            }
        return cast(dict[str, Any], pipeline.contract())

    def autonomy_exits_status(self) -> dict[str, Any]:
        pipeline = self._autonomy_exits
        if pipeline is None:
            return {
                "ok": True,
                "initialised": False,
                "phase": "AT-05",
                "paper_only": True,
                "live_trading": False,
                "last_error": self._autonomy_exits_error,
            }
        body = cast(dict[str, Any], pipeline.status())
        body["supervision"] = pipeline.supervision_view()
        return body

    async def run_autonomy_exits(self, *, trigger: str = "operator") -> dict[str, Any]:
        return cast(dict[str, Any], await self._ensure_autonomy_exits().run_cycle(trigger=trigger))

    def autonomy_exit_decisions(
        self, *, portfolio_id: str = "", limit: int = 100
    ) -> dict[str, Any]:
        if self._autonomy_exits is not None:
            return cast(
                dict[str, Any],
                self._autonomy_exits.decisions(portfolio_id=portfolio_id, limit=limit),
            )
        rows = self._execution_service().store.exit_decisions(
            portfolio_id=portfolio_id, limit=limit
        )
        return {"ok": True, "phase": "AT-05", "decisions": list(rows), "total": len(rows)}

    def autonomy_exit_decision(self, decision_id: str) -> dict[str, Any]:
        if self._autonomy_exits is not None:
            return cast(dict[str, Any], self._autonomy_exits.decision(decision_id))
        row = self._execution_service().store.exit_decision(decision_id=decision_id)
        if row is None:
            from novacontrol.novamarket.autonomy_exits import ExitNotFound

            raise ExitNotFound(f"unknown exit decision {decision_id!r}")
        return {"ok": True, "decision": row}

    async def reconcile_autonomy_exit(self, decision_id: str) -> dict[str, Any]:
        return cast(
            dict[str, Any], await self._ensure_autonomy_exits().reconcile_decision(decision_id)
        )

    def autonomy_exit_events(self, decision_id: str, *, limit: int = 200) -> dict[str, Any]:
        if self._autonomy_exits is not None:
            return cast(dict[str, Any], self._autonomy_exits.events(decision_id, limit=limit))
        row = self._execution_service().store.exit_decision(decision_id=decision_id)
        if row is None:
            from novacontrol.novamarket.autonomy_exits import ExitNotFound

            raise ExitNotFound(f"unknown exit decision {decision_id!r}")
        events = self._execution_service().store.exit_events(decision_id, limit=limit)
        return {
            "ok": True,
            "decision_id": decision_id,
            "events": list(events),
            "total": len(events),
        }

    def autonomy_exit_positions(self, *, limit: int = 200) -> dict[str, Any]:
        rows = self._execution_service().store.exit_states(limit=limit)
        return {
            "ok": True,
            "phase": "AT-05",
            "positions": list(rows),
            "total": len(rows),
            "paper_only": True,
            "live_trading": False,
        }

    def autonomy_exit_configurations(self, *, limit: int = 200) -> dict[str, Any]:
        rows = self._execution_service().store.exit_configurations(limit=limit)
        return {
            "ok": True,
            "phase": "AT-05",
            "configurations": list(rows),
            "total": len(rows),
            "paper_only": True,
            "live_trading": False,
        }

    def set_autonomy_exit_configuration(
        self,
        portfolio_id: str,
        instrument_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        from novacontrol.novamarket.autonomy_exits import (
            InvalidExitConfiguration,
            build_exit_configuration,
        )

        detail = self._exit_portfolio_view(portfolio_id=portfolio_id)
        holdings = (detail.get("holdings") or ()) if isinstance(detail, Mapping) else ()
        holding = next(
            (row for row in holdings if str(row.get("instrument_id") or "") == instrument_id),
            None,
        )
        if holding is None:
            raise InvalidExitConfiguration(
                "an exit configuration requires an open position in the portfolio"
            )
        if Decimal(str(holding.get("quantity") or "0")) <= 0:
            raise InvalidExitConfiguration("the authoritative position has no quantity")
        store = self._execution_service().store
        existing = store.exit_configuration(portfolio_id, instrument_id) or {}
        target = (
            payload["target_price"] if "target_price" in payload else existing.get("target_price")
        )
        if "trailing_stop_percent" in payload:
            trailing_percent = payload["trailing_stop_percent"]
        elif "trailing_stop_fraction" in payload:
            trailing_percent = None
        else:
            trailing_percent = existing.get("trailing_stop_percent")
        body = build_exit_configuration(
            portfolio_id,
            instrument_id,
            position_id=str(holding.get("position_id") or holding.get("id") or ""),
            target_price=target,
            trailing_stop_fraction=(
                payload.get("trailing_stop_fraction")
                if "trailing_stop_fraction" in payload
                else None
            ),
            trailing_stop_percent=trailing_percent,
            average_cost=holding.get("average_cost"),
            existing=existing,
        )
        stored = store.save_exit_configuration(body)
        return {
            "ok": True,
            "phase": "AT-05",
            "configuration": stored,
            "paper_only": True,
            "live_trading": False,
        }

    def delete_autonomy_exit_configuration(
        self, portfolio_id: str, instrument_id: str
    ) -> dict[str, Any]:
        removed = self._execution_service().store.delete_exit_configuration(
            portfolio_id, instrument_id
        )
        return {
            "ok": True,
            "removed": removed,
            "portfolio_id": portfolio_id,
            "instrument_id": instrument_id,
            "paper_only": True,
            "live_trading": False,
        }

    def _attach_rl_feedback(
        self, body: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        from novacontrol.novamarket.learning.paper_feedback import (
            chronological_splits,
        )

        experiences: list[Mapping[str, Any]] = []
        for row in rows:
            feedback = row.get("rl_feedback")
            if isinstance(feedback, Mapping) and feedback.get("status") == "verified":
                experiences.append(feedback)
        splits = chronological_splits(experiences)
        result = dict(body)
        result["rl_feedback"] = {
            **splits,
            "verified_experiences": splits["total"],
            "automatic_policy_promotion": False,
            "training_performed": False,
            "policy_modified": False,
        }
        return result

    def autonomy_learning_evidence(
        self, *, decision_id: str = "", limit: int = 100
    ) -> dict[str, Any]:
        if self._autonomy_exits is not None:
            body = cast(
                dict[str, Any], self._autonomy_exits.learning(decision_id=decision_id, limit=limit)
            )
            return self._attach_rl_feedback(body, body.get("evidence") or ())
        rows = self._execution_service().store.learning_evidence(
            decision_id=decision_id, limit=limit
        )
        return self._attach_rl_feedback(
            {
                "ok": True,
                "evidence": list(rows),
                "total": len(rows),
                "advisory_only": True,
                "auto_promoted": False,
            },
            rows,
        )

    def autonomy_execution_contract(self) -> dict[str, Any]:
        pipeline = self._autonomy_execution_or_closed()
        if pipeline is None:
            return {
                "implemented": True,
                "available": False,
                "phase": "AT-04",
                "milestone": "AT-04",
                "version": "at04-paper-execution-1.0",
                "detail": "the NovaMarket runtime is not enabled",
                "paper_only": True,
                "live_trading": False,
                "broker_connected": False,
                "execution_authority": "NM-10",
                "risk_authority": "NM-08",
                "execution_implemented": True,
                "orders_possible": True,
                "live_orders_possible": False,
            }
        return cast(dict[str, Any], pipeline.contract())

    def autonomy_execution_status(self) -> dict[str, Any]:
        pipeline = self._autonomy_execution_or_closed()
        if pipeline is None:
            return {
                "ok": True,
                "initialised": False,
                "phase": "AT-04",
                "implemented": True,
                "paper_only": True,
                "live_trading": False,
                "broker_connected": False,
                "orders_possible": True,
                "live_orders_possible": False,
                "detail": "the NovaMarket runtime is not enabled",
                "last_error": self._autonomy_execution_error,
            }
        return cast(dict[str, Any], pipeline.status())

    def autonomy_executions(
        self,
        *,
        status: str = "",
        portfolio_id: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        pipeline = self._autonomy_execution_or_closed()
        if pipeline is None:
            return {"ok": True, "executions": [], "total": 0, "counts": {}}
        return cast(
            dict[str, Any],
            pipeline.executions(status=status, portfolio_id=portfolio_id, limit=limit),
        )

    def autonomy_execution(self, execution_id: str) -> dict[str, Any]:
        pipeline = self._autonomy_execution_or_closed()
        if pipeline is None:
            from novacontrol.novamarket.autonomy_execution import ExecutionNotFound

            raise ExecutionNotFound(f"unknown autonomous paper execution {execution_id!r}")
        return cast(dict[str, Any], pipeline.execution(execution_id))

    def autonomy_execution_events(self, execution_id: str, *, limit: int = 200) -> dict[str, Any]:
        pipeline = self._autonomy_execution_or_closed()
        if pipeline is None:
            from novacontrol.novamarket.autonomy_execution import ExecutionNotFound

            raise ExecutionNotFound(f"unknown autonomous paper execution {execution_id!r}")
        return cast(dict[str, Any], pipeline.events(execution_id, limit=limit))

    def autonomy_execution_failures(self, *, limit: int = 100) -> dict[str, Any]:
        pipeline = self._autonomy_execution_or_closed()
        if pipeline is None:
            return {"ok": True, "failures": [], "total": 0}
        return cast(dict[str, Any], pipeline.failures(limit=limit))

    async def autonomy_execution_run(
        self,
        *,
        trigger: str = "manual",
        actor: str = "autonomy-operator",
        proposal_id: str = "",
    ) -> dict[str, Any]:
        pipeline = self._autonomy_execution_or_closed()
        if pipeline is None:
            from novacontrol.novamarket.autonomy_execution import AutonomyExecutionError

            raise AutonomyExecutionError(
                "RUNTIME_NOT_AUTONOMOUS",
                "the NovaMarket runtime is not enabled; start autonomy first",
            )
        try:
            result = await pipeline.run_cycle(trigger=trigger, actor=actor, proposal_id=proposal_id)
        except Exception as exc:  # noqa: BLE001 - preserve the execution error
            self._autonomy_execution_error = f"{type(exc).__name__}: {exc}"
            raise
        self._autonomy_execution_error = ""
        return cast(dict[str, Any], result)

    async def autonomy_execution_reconcile(self, execution_id: str) -> dict[str, Any]:
        pipeline = self._autonomy_execution_or_closed()
        if pipeline is None:
            from novacontrol.novamarket.autonomy_execution import ExecutionNotFound

            raise ExecutionNotFound(f"unknown autonomous paper execution {execution_id!r}")
        return cast(dict[str, Any], await pipeline.reconcile_execution(execution_id))

    def _radar_path(self) -> Any:
        """The ledger's SQLite file when it is open, else the subsystem's."""
        if self._wallet_store is not None:
            return self._wallet_store.path
        if self._wallet_path is not None:
            return self._wallet_path
        if self._data_dir is not None:
            return self._data_dir / "novamarket.sqlite3"
        return ":memory:"

    def _shadow_path(self) -> Any:
        """NM-12's own database file, never the market/wallet/ledger file."""
        if self._data_dir is not None:
            return self._data_dir / "novamarket_shadow.sqlite3"
        if self._market_data_path is not None:
            return Path(self._market_data_path).with_name("novamarket_shadow.sqlite3")
        return ":memory:"

    def _dashboard_path(self) -> Any:
        """PI-20 keeps only dashboard preference presets in its own file."""
        if self._data_dir is not None:
            return self._data_dir / "novamarket_dashboard.sqlite3"
        if self._market_data_path is not None:
            return Path(self._market_data_path).with_name("novamarket_dashboard.sqlite3")
        return ":memory:"

    def _portfolio_management_path(self) -> Any:
        """PI-21 keeps only management research records in its own file."""
        if self._data_dir is not None:
            return self._data_dir / "novamarket_portfolio_management.sqlite3"
        if self._market_data_path is not None:
            return Path(self._market_data_path).with_name("novamarket_portfolio_management.sqlite3")
        return ":memory:"

    def _risk_intelligence_path(self) -> Any:
        """PI-22 keeps analytical risk records separate from accounting data."""
        if self._data_dir is not None:
            return self._data_dir / "novamarket_risk_intelligence.sqlite3"
        if self._market_data_path is not None:
            return Path(self._market_data_path).with_name(
                "novamarket_risk_intelligence.sqlite3"
            )
        return ":memory:"

    def _forecast_intelligence_path(self) -> Any:
        """PI-23 keeps advisory optimization evidence separately."""
        if self._data_dir is not None:
            return self._data_dir / "novamarket_forecast_intelligence.sqlite3"
        if self._market_data_path is not None:
            return Path(self._market_data_path).with_name(
                "novamarket_forecast_intelligence.sqlite3"
            )
        return ":memory:"

    def _event_intelligence_path(self) -> Any:
        """PI-24 keeps event analyses separate from radar and accounting data."""
        if self._data_dir is not None:
            return self._data_dir / "novamarket_event_intelligence.sqlite3"
        if self._market_data_path is not None:
            return Path(self._market_data_path).with_name(
                "novamarket_event_intelligence.sqlite3"
            )
        return ":memory:"

    def _evaluation_path(self) -> Any:
        """PI-11 reports have their own append-only file."""
        if self._data_dir is not None:
            return self._data_dir / "novamarket_evaluation.sqlite3"
        if self._market_data_path is not None:
            return Path(self._market_data_path).with_name("novamarket_evaluation.sqlite3")
        return ":memory:"

    def _learning_path(self) -> Any:
        """NM-13's own database file, never the market/wallet/shadow file."""
        if self._data_dir is not None:
            return self._data_dir / "novamarket_learning.sqlite3"
        if self._market_data_path is not None:
            return Path(self._market_data_path).with_name("novamarket_learning.sqlite3")
        return ":memory:"

    def _radar_enum(self, enum_type: Any, value: str, field_name: str) -> Any:
        """Coerce a query string to its enum; an unknown value is a 422, not a 500."""
        try:
            return enum_type(value)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in enum_type)
            raise EventValidationError(f"{field_name} must be one of: {allowed}") from exc

    def _radar_instruments(self) -> tuple[RadarInstrumentView, ...]:
        """NM-02's canonical instruments, adapted; empty when data is closed."""
        try:
            engine = self._market_data_engine()
            page = engine.list_instruments(limit=500)
        except Exception as exc:  # noqa: BLE001 - mapping is optional, never fatal
            _LOGGER.warning("NovaMarket event radar could not read instruments: %s", exc)
            return ()
        views: list[RadarInstrumentView] = []
        for instrument in page.items:
            try:
                views.append(RadarInstrumentView.from_payload(instrument.to_dict()))
            except ValueError:
                continue
        return tuple(views)

    def _ensure_radar(self) -> MarketEventRadar:
        """Open the event store and build the radar ONCE per run."""
        if self._radar_engine is not None:
            return self._radar_engine
        try:
            store = RadarEventStore(self._radar_path(), clock=self._clock)
            engine = MarketEventRadar(
                store,
                catalog=RadarSourceCatalog(),
                client=RadarFeedClient(transport=self._transport),
                instruments=self._radar_instruments,
                publisher=self._publisher,
                clock=self._clock,
            )
        except Exception as exc:
            self._radar_error = f"{type(exc).__name__}: {exc}"
            raise
        self._radar_store = store
        self._radar_engine = engine
        self._radar_error = ""
        return engine

    def _radar_service(self) -> MarketEventRadar:
        engine = self._radar_engine
        if engine is not None:
            return engine
        if self._lifecycle.state in (
            LifecycleState.READY,
            LifecycleState.RUNNING,
            LifecycleState.DEGRADED,
        ):
            return self._ensure_radar()
        raise NotOperationalError(
            "NovaMarket event radar is not available while the subsystem is "
            f"{self._lifecycle.state.value}; enable NovaMarket first"
        )

    def radar_status(self) -> dict[str, Any]:
        """Event totals, source health and the last error, or why it is closed."""
        if self._radar_engine is None and self._lifecycle.state in (
            LifecycleState.READY,
            LifecycleState.RUNNING,
            LifecycleState.DEGRADED,
        ):
            try:
                self._ensure_radar()
            except Exception as exc:  # noqa: BLE001 - status reports, never raises
                return {
                    "initialised": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                    "phase": "NM-04",
                }
        engine = self._radar_engine
        if engine is None:
            return {
                "initialised": False,
                "detail": (
                    "the event radar opens when NovaMarket is operational "
                    f"(currently {self._lifecycle.state.value})"
                ),
                "phase": "NM-04",
                "error": self._radar_error,
            }
        try:
            return engine.status()
        except Exception as exc:  # noqa: BLE001 - a read must not crash status
            return {
                "initialised": False,
                "detail": f"{type(exc).__name__}: {exc}",
                "phase": "NM-04",
            }

    def radar_search(
        self,
        *,
        category: str | None = None,
        subcategory: str = "",
        verification: str | None = None,
        importance: str | None = None,
        instrument_id: str = "",
        sector: str = "",
        market: str = "",
        source_id: str = "",
        text: str = "",
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        query = RadarEventQuery(
            category=self._radar_enum(RadarEventCategory, category, "category")
            if category
            else None,
            subcategory=subcategory or "",
            verification=self._radar_enum(RadarVerificationState, verification, "verification")
            if verification
            else None,
            importance=self._radar_enum(RadarImportance, importance, "importance")
            if importance
            else None,
            instrument_id=instrument_id or "",
            sector=sector or "",
            market=market or "",
            source_id=source_id or "",
            text=text or "",
            since=_radar_moment(since, "since"),
            until=_radar_moment(until, "until"),
            limit=limit,
            offset=offset,
        )
        return self._radar_service().search(query)

    def radar_event(self, event_id: str, *, version: int | None = None) -> dict[str, Any]:
        return self._radar_service().event(event_id, version=version)

    def radar_evidence(self, event_id: str) -> dict[str, Any]:
        return self._radar_service().evidence(event_id)

    def radar_history(self, event_id: str) -> dict[str, Any]:
        return self._radar_service().history(event_id)

    def radar_instrument_events(self, instrument_id: str, *, limit: int = 50) -> dict[str, Any]:
        return self._radar_service().instrument_events(instrument_id, limit=limit)

    def radar_sources(self) -> dict[str, Any]:
        engine = self._radar_service()
        rows = list(engine.sources())
        return {
            "ok": True,
            "sources": rows,
            "total": len(rows),
            "enabled": sum(1 for row in rows if row["enabled"]),
        }

    def radar_set_source_enabled(self, source_id: str, enabled: bool) -> dict[str, Any]:
        body = self._radar_service().set_source_enabled(source_id, bool(enabled))
        body["ok"] = True
        return body

    async def radar_refresh(self, source_id: str | None = None) -> dict[str, Any]:
        body = await self._radar_service().refresh(source_id or None)
        body["ok"] = True
        return body

    async def radar_expire(self) -> dict[str, Any]:
        body = await self._radar_service().expire_due()
        body["ok"] = True
        return body

    def radar_contract(self) -> dict[str, Any]:
        if self._radar_engine is None:
            return {
                "implemented": False,
                "phase": "NM-04",
                "detail": "the event radar is not open in this run",
            }
        return self._radar_engine.contract()

    # -- wallets and the deterministic ledger (NM-03) -------------------------

    @property
    def wallet(self) -> WalletEngine | None:
        """The ledger engine, or ``None`` before initialisation / after shutdown."""
        return self._wallet_engine

    def _wallet_service(self) -> WalletEngine:
        """The ledger engine, opened lazily once the subsystem is operational."""
        engine = self._wallet_engine
        if engine is not None:
            return engine
        if self._lifecycle.state in (
            LifecycleState.READY,
            LifecycleState.RUNNING,
            LifecycleState.DEGRADED,
        ):
            return self._ensure_wallet()
        raise NotOperationalError(
            "NovaMarket wallets are not available while the subsystem is "
            f"{self._lifecycle.state.value}; enable NovaMarket first"
        )

    def _ensure_wallet(self) -> WalletEngine:
        """Open the ledger repository and build the engine ONCE per run."""
        if self._wallet_engine is not None:
            return self._wallet_engine
        try:
            store = open_wallet_store(self._data_dir, clock=self._clock, path=self._wallet_path)
            engine = WalletEngine(store, publisher=self._publisher, clock=self._clock)
        except Exception as exc:
            self._wallet_error = f"{type(exc).__name__}: {exc}"
            raise
        self._wallet_store = store
        self._wallet_engine = engine
        self._wallet_error = ""
        return engine

    # -- NM-07: three-horizon portfolio management ---------------------------

    def _ensure_portfolio(self) -> "PortfolioEngine":
        from novacontrol.novamarket.portfolio import PortfolioEngine, PortfolioStore

        if self._portfolio_engine is not None:
            return self._portfolio_engine
        try:
            store = PortfolioStore(self._radar_path(), clock=self._clock)
            engine = PortfolioEngine(
                store,
                self._wallet_service(),
                market_data=self._market_engine,
                discovery=self._discovery_engine,
                forecasts=self._forecast_engine,
                radar=self._radar_engine,
                publisher=self._portfolio_publish,
                clock=self._clock,
            )
        except Exception as exc:
            self._portfolio_error = f"{type(exc).__name__}: {exc}"
            raise
        self._portfolio_store = store
        self._portfolio_engine = engine
        self._portfolio_error = ""
        return engine

    async def _portfolio_publish(self, event_type: str, payload: dict[str, Any]) -> None:
        await self._publish(EventType(str(event_type)), **payload)

    def _portfolio_service(self) -> "PortfolioEngine":
        if self._portfolio_engine is None:
            if self._lifecycle.state not in (
                LifecycleState.READY,
                LifecycleState.RUNNING,
                LifecycleState.DEGRADED,
            ):
                raise NotOperationalError(
                    "portfolio management is not initialised; enable NovaMarket first"
                )
            self._ensure_portfolio()
        engine = self._portfolio_engine
        if engine is None:
            raise NotOperationalError("portfolio management did not initialise")
        engine.set_market_data(self._market_engine)
        engine.set_discovery(self._discovery_engine)
        engine.set_forecasts(self._forecast_engine)
        engine.set_radar(self._radar_engine)
        return engine

    def portfolio_status(self) -> dict[str, Any]:
        if self._portfolio_engine is None and self._lifecycle.state not in (
            LifecycleState.READY,
            LifecycleState.RUNNING,
            LifecycleState.DEGRADED,
        ):
            return {
                "initialised": False,
                "detail": (
                    "portfolio management opens when NovaMarket is operational "
                    f"(currently {self._lifecycle.state.value})"
                ),
                "phase": "NM-07",
                "error": self._portfolio_error,
            }
        try:
            return self._portfolio_service().status()
        except Exception as exc:  # noqa: BLE001 - a read must not crash status
            return {
                "initialised": False,
                "detail": f"{type(exc).__name__}: {exc}",
                "phase": "NM-07",
            }

    def portfolio_horizons(self) -> dict[str, Any]:
        from novacontrol.novamarket.portfolio import policy_for
        from novacontrol.novamarket.portfolio.models import PortfolioHorizon

        return {
            "ok": True,
            "horizons": {
                horizon.value: policy_for(horizon).to_dict() for horizon in PortfolioHorizon
            },
        }

    async def portfolio_create(
        self,
        horizon: str,
        *,
        name: str = "",
        funding_wallet_id: str,
        capital: object,
    ) -> dict[str, Any]:
        return await self._portfolio_service().create(
            horizon,
            name=name,
            funding_wallet_id=funding_wallet_id,
            capital=capital,
        )

    def portfolios_list(
        self,
        *,
        horizon: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._portfolio_service().portfolios(horizon=horizon, status=status, limit=limit)

    def portfolio_detail(self, portfolio_id: str) -> dict[str, Any]:
        return self._portfolio_service().detail(portfolio_id)

    def portfolio_holdings(self, portfolio_id: str) -> dict[str, Any]:
        return self._portfolio_service().holdings(portfolio_id)

    async def portfolio_valuation(self, portfolio_id: str) -> dict[str, Any]:
        return await self._portfolio_service().valuate(portfolio_id)

    def portfolio_valuations(
        self, portfolio_id: str, *, limit: int = 200, latest: bool = False
    ) -> dict[str, Any]:
        return self._portfolio_service().valuations(portfolio_id, limit=limit, latest=latest)

    def portfolio_performance(self, portfolio_id: str) -> dict[str, Any]:
        return self._portfolio_service().performance(portfolio_id)

    def portfolio_decisions(self, portfolio_id: str, *, limit: int = 100) -> dict[str, Any]:
        return self._portfolio_service().decisions(portfolio_id, limit=limit)

    def portfolio_proposals(
        self,
        portfolio_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._portfolio_service().proposals(portfolio_id, status=status, limit=limit)

    def portfolio_lifecycle(self, portfolio_id: str) -> dict[str, Any]:
        return self._portfolio_service().lifecycle(portfolio_id)

    def portfolio_constraints(self, portfolio_id: str) -> list[dict[str, Any]]:
        return self._portfolio_service().constraints(portfolio_id)

    def portfolio_reconcile(self, portfolio_id: str) -> dict[str, Any]:
        return self._portfolio_service().reconcile(portfolio_id)

    async def portfolio_rebalance(
        self, portfolio_id: str, *, force: bool = False
    ) -> dict[str, Any]:
        return await self._portfolio_service().propose(portfolio_id, force=force)

    async def portfolio_apply(self, proposal_id: str) -> dict[str, Any]:
        return await self._portfolio_service().apply(proposal_id)

    async def portfolio_set_status(
        self, portfolio_id: str, action: str, *, reason: str = ""
    ) -> dict[str, Any]:
        return await self._portfolio_service().set_status(portfolio_id, action, reason=reason)

    async def portfolio_close(self, portfolio_id: str, *, reason: str = "") -> dict[str, Any]:
        return await self._portfolio_service().close(portfolio_id, reason=reason)

    def portfolio_contract(self) -> dict[str, Any]:
        if self._portfolio_engine is None:
            return {
                "implemented": False,
                "phase": "NM-07",
                "detail": "portfolio management is not initialised",
            }
        return self._portfolio_engine.contract()

    # -- NM-08: independent risk management ----------------------------------

    def _ensure_risk(self) -> "RiskEngine":
        from novacontrol.novamarket.risk import RiskEngine, RiskStore

        if self._risk_engine is not None:
            return self._risk_engine
        try:
            store = RiskStore(self._radar_path(), clock=self._clock)
            engine = RiskEngine(
                store,
                self._portfolio_service(),
                market_data=self._market_engine,
                forecasts=self._forecast_engine,
                radar=self._radar_engine,
                discovery=self._discovery_engine,
                wallets=self._wallet_service(),
                publisher=self._risk_publish,
                clock=self._clock,
            )
        except Exception as exc:
            self._risk_error = f"{type(exc).__name__}: {exc}"
            raise
        self._risk_store = store
        self._risk_engine = engine
        self._risk_error = ""
        return engine

    async def _risk_publish(self, event_type: str, payload: dict[str, Any]) -> None:
        await self._publish(EventType(str(event_type)), **payload)

    def _risk_service(self) -> "RiskEngine":
        if self._risk_engine is None:
            if self._lifecycle.state not in (
                LifecycleState.READY,
                LifecycleState.RUNNING,
                LifecycleState.DEGRADED,
            ):
                raise NotOperationalError(
                    "risk management is not initialised; enable NovaMarket first"
                )
            self._ensure_risk()
        engine = self._risk_engine
        if engine is None:
            raise NotOperationalError("risk management did not initialise")
        from novacontrol.novamarket.decision.forecast import ForecastRiskAdapter

        engine.set_market_data(self._market_engine)
        # NM-08 keeps its own policy and forecast checks.  The adapter only
        # exposes stored PI forecasts under the NM-06 horizon labels already
        # used by that policy; it does not assess or approve anything here.
        engine.set_forecasts(
            ForecastRiskAdapter(
                self._forecast_engine,
                self._pi12_risk_evidence,
                clock=self._clock,
            )
        )
        engine.set_radar(self._radar_engine)
        engine.set_discovery(self._discovery_engine)
        engine.set_wallets(self._wallet_service())
        return engine

    def risk_status(self) -> dict[str, Any]:
        if self._risk_engine is None and self._lifecycle.state not in (
            LifecycleState.READY,
            LifecycleState.RUNNING,
            LifecycleState.DEGRADED,
        ):
            return {
                "initialised": False,
                "phase": "NM-08",
                "detail": (
                    "risk management opens when NovaMarket is operational "
                    f"(currently {self._lifecycle.state.value})"
                ),
                "last_error": self._risk_error,
            }
        return self._risk_service().status()

    def risk_contract(self) -> dict[str, Any]:
        if self._risk_engine is None:
            return {
                "implemented": False,
                "phase": "NM-08",
                "detail": "risk management is not initialised",
            }
        return self._risk_engine.contract()

    async def risk_assess(
        self,
        *,
        action: str = "apply_proposal",
        portfolio_id: str = "",
        proposal_id: str = "",
        instrument_id: str = "",
        side: str = "",
        notional: object | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        return await self._risk_service().assess(
            action,
            portfolio_id=portfolio_id,
            proposal_id=proposal_id,
            instrument_id=instrument_id,
            side=side,
            notional=notional,
            actor=actor,
        )

    def risk_decision(self, decision_id: str) -> dict[str, Any]:
        return self._risk_service().decision(decision_id)

    def risk_decisions(
        self, *, portfolio_id: str = "", outcome: str = "", limit: int = 100
    ) -> dict[str, Any]:
        return self._risk_service().decisions(
            portfolio_id=portfolio_id, outcome=outcome, limit=limit
        )

    async def risk_apply_decision(self, decision_id: str) -> dict[str, Any]:
        return await self._risk_service().apply_decision(decision_id)

    def risk_portfolio(self, portfolio_id: str) -> dict[str, Any]:
        return self._risk_service().portfolio_risk(portfolio_id)

    def risk_position(self, portfolio_id: str, instrument_id: str) -> dict[str, Any]:
        return self._risk_service().position_risk(portfolio_id, instrument_id)

    def risk_exposure(self) -> dict[str, Any]:
        return self._risk_service().exposure()

    def risk_policies(self) -> dict[str, Any]:
        return self._risk_service().policies()

    async def risk_update_policy(
        self, policy_id: str, changes: dict[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        return await self._risk_service().update_policy(policy_id, changes, actor=actor)

    def risk_alerts(self, *, status: str = "", limit: int = 100) -> dict[str, Any]:
        return self._risk_service().alerts(status=status, limit=limit)

    async def risk_acknowledge_alert(self, alert_id: str, *, actor: str = "") -> dict[str, Any]:
        return await self._risk_service().acknowledge_alert(alert_id, actor=actor)

    async def risk_resolve_alert(self, alert_id: str, *, actor: str = "") -> dict[str, Any]:
        return await self._risk_service().resolve_alert(alert_id, actor=actor)

    async def risk_stress(
        self, portfolio_id: str, scenario: str = "market_decline", *, actor: str = ""
    ) -> dict[str, Any]:
        return await self._risk_service().stress(portfolio_id, scenario, actor=actor)

    def risk_stress_results(self, portfolio_id: str = "", limit: int = 50) -> dict[str, Any]:
        return self._risk_service().stress_results(portfolio_id, limit)

    def risk_overrides(self, decision_id: str = "", limit: int = 100) -> dict[str, Any]:
        return self._risk_service().overrides(decision_id=decision_id, limit=limit)

    async def risk_record_override(
        self,
        decision_id: str,
        *,
        actor: str,
        reason: str,
        ttl_hours: float = 24,
    ) -> dict[str, Any]:
        return await self._risk_service().record_override(
            decision_id, actor=actor, reason=reason, ttl_hours=ttl_hours
        )

    # -- NM-09: trading strategies -------------------------------------------
    def _ensure_strategy(self) -> "StrategyEngine":  # type: ignore[name-defined]
        from novacontrol.novamarket.strategies import StrategyEngine, StrategyStore

        if self._strategy_engine is not None:
            return self._strategy_engine
        try:
            store = StrategyStore(self._radar_path(), clock=self._clock)
            engine = StrategyEngine(
                store,
                self._portfolio_service(),
                self._risk_service(),
                market_data=self._market_engine,
                forecasts=self._forecast_engine,
                radar=self._radar_engine,
                discovery=self._discovery_engine,
                publisher=self._strategy_publish,
                forwarder=self._forward_strategy_to_execution,
                clock=self._clock,
            )
        except Exception as exc:
            self._strategy_error = f"{type(exc).__name__}: {exc}"
            raise
        self._strategy_store = store
        self._strategy_engine = engine
        self._strategy_error = ""
        return engine

    async def _strategy_publish(self, event_type: str, payload: dict[str, Any]) -> None:
        await self._publish(EventType(str(event_type)), **payload)

    def _strategy_service(self) -> "StrategyEngine":  # type: ignore[name-defined]
        engine = self._strategy_engine
        if engine is None:
            if self._lifecycle.state not in (
                LifecycleState.READY,
                LifecycleState.RUNNING,
                LifecycleState.DEGRADED,
            ):
                raise NotOperationalError(
                    "strategy management is not initialised; enable NovaMarket first"
                )
            engine = self._ensure_strategy()
        engine.set_market_data(self._market_engine)
        engine.set_forecasts(self._forecast_engine)
        engine.set_timeframe_analyzer(self._timeframe_service())
        engine.set_radar(self._radar_engine)
        engine.set_discovery(self._discovery_engine)
        engine.set_risk(self._risk_service())
        engine.set_forwarder(self._forward_strategy_to_execution)
        return engine

    def _timeframe_service(self) -> Any:
        """The one multi-timeframe analyzer, built over the shared store."""
        from novacontrol.novamarket.multitimeframe import MultiTimeframeAnalyzer

        if self._timeframe_analyzer is None:
            try:
                self._timeframe_analyzer = MultiTimeframeAnalyzer(
                    self._ensure_market_data(), clock=self._clock
                )
            except Exception as exc:
                self._timeframe_error = f"{type(exc).__name__}: {exc}"
                raise
        return self._timeframe_analyzer

    def multi_timeframe_status(self) -> dict[str, Any]:
        try:
            return dict(self._timeframe_service().status())
        except Exception as exc:  # noqa: BLE001 - status must remain readable
            return {
                "ok": False,
                "phase": "post-PI-24 enhancement",
                "error": self._timeframe_error or f"{type(exc).__name__}: {exc}",
            }

    def multi_timeframe_analysis(
        self,
        portfolio_id: str,
        *,
        instrument_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Analyse the portfolio's instruments on 20m/5m/1m and return evidence."""
        detail = self._portfolio_service().detail(portfolio_id)
        horizon = str(detail["portfolio"]["horizon"])
        ids = [str(item) for item in (instrument_ids or ()) if str(item).strip()]
        if not ids:
            ids = [
                str(row.get("instrument_id"))
                for row in detail.get("holdings", ()) or ()
                if row.get("instrument_id")
            ]
        if not ids:
            raise ValueError(
                "a multi-timeframe analysis needs at least one instrument id"
            )
        analyzer = self._timeframe_service()
        decisions = []
        errors: list[dict[str, str]] = []
        for instrument_id in list(dict.fromkeys(ids))[:20]:
            try:
                decisions.append(analyzer.analyse(instrument_id).to_dict())
            except Exception as exc:  # noqa: BLE001 - one instrument cannot block the rest
                errors.append(
                    {
                        "instrument_id": instrument_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        return {
            "ok": True,
            "portfolio_id": portfolio_id,
            "portfolio_horizon": horizon,
            "applied": horizon == "short_term",
            "applies_to": "short_term/day_trading entries only",
            "decisions": decisions,
            "errors": errors,
            "config": analyzer.config.to_dict(),
        }

    async def _forward_strategy_to_execution(
        self, proposal_id: str, actor: str = ""
    ) -> dict[str, Any]:
        """Lazy NM-09 -> NM-10 handoff without a circular constructor dependency."""
        return await self._execution_service().submit(proposal_id, actor=actor)  # type: ignore[no-any-return]

    def strategy_status(self) -> dict[str, Any]:
        if self._strategy_engine is None and self._lifecycle.state not in (
            LifecycleState.READY,
            LifecycleState.RUNNING,
            LifecycleState.DEGRADED,
        ):
            return {
                "initialised": False,
                "phase": "NM-09",
                "detail": (
                    "strategy management opens when NovaMarket is operational "
                    f"(currently {self._lifecycle.state.value})"
                ),
                "last_error": self._strategy_error,
            }
        return self._strategy_service().status()  # type: ignore[no-any-return]

    def strategy_contract(self) -> dict[str, Any]:
        if self._strategy_engine is None:
            return {
                "implemented": False,
                "phase": "NM-09",
                "detail": "strategy management is not initialised",
            }
        return self._strategy_engine.contract()  # type: ignore[no-any-return]

    # -- NM-10: realistic paper execution ------------------------------------
    def _ensure_execution(self) -> Any:
        from novacontrol.novamarket.execution import ExecutionEngine, ExecutionStore

        if self._execution_engine is not None:
            return self._execution_engine
        try:
            store = ExecutionStore(self._radar_path(), clock=self._clock)
            engine = ExecutionEngine(
                store,
                wallets=self._wallet_service(),
                portfolios=self._portfolio_service(),
                risk=self._risk_service(),
                strategies=self._strategy_service(),
                market_data=self._market_engine,
                publisher=self._execution_publish,
                clock=self._clock,
                proposal_loader=self._execution_proposal_view,
            )
        except Exception as exc:
            self._execution_error = f"{type(exc).__name__}: {exc}"
            raise
        self._execution_store = store
        self._execution_engine = engine
        self._execution_error = ""
        return engine

    def _execution_proposal_view(self, proposal_id: str) -> dict[str, Any] | None:
        """Read an AT-03 proposal for NM-10 without replacing strategy storage."""

        try:
            exits = self._autonomy_exits
            if exits is not None:
                proposal = exits.exit_proposal(proposal_id)
                if proposal is not None:
                    return cast(dict[str, Any], proposal)
            pipeline = self._decision_pipeline
            if pipeline is None or pipeline.closed:
                pipeline = self._ensure_decision()
            return cast(dict[str, Any] | None, pipeline.execution_proposal(proposal_id))
        except Exception:  # noqa: BLE001 - NM-10 falls back to its strategy source
            return None

    async def _execution_publish(self, event_type: str, payload: Mapping[str, Any]) -> None:
        # NM-10 owns the registered order/execution event vocabulary.  AT-04
        # also emits journal lifecycle names that are not in that enum; the
        # journal remains authoritative and an unregistered name must not turn
        # a successful paper execution into a logged publisher failure.
        try:
            registered = EventType(str(event_type))
        except ValueError:
            return
        await self._publish(registered, **dict(payload))

    def _execution_service(self) -> Any:
        engine = self._execution_engine
        if engine is None:
            if self._lifecycle.state not in (
                LifecycleState.READY,
                LifecycleState.RUNNING,
                LifecycleState.DEGRADED,
            ):
                raise NotOperationalError(
                    "paper execution is not initialised; enable NovaMarket first"
                )
            engine = self._ensure_execution()
        engine.set_market_data(self._market_engine)
        return engine

    def execution_status(self) -> dict[str, Any]:
        if self._execution_engine is None:
            return {
                "ok": True,
                "phase": "NM-10",
                "initialised": False,
                "paper_only": True,
                "live_trading": False,
                "broker_connected": False,
                "last_error": self._execution_error,
                "detail": "paper execution opens when NovaMarket is operational",
            }
        result: dict[str, Any] = self._execution_service().status()
        return result

    def execution_contract(self) -> dict[str, Any]:
        if self._execution_engine is None:
            return {
                "ok": True,
                "phase": "NM-10",
                "implemented": False,
                "paper_only": True,
                "live_trading": False,
                "detail": "paper execution is not initialised",
            }
        result: dict[str, Any] = self._execution_engine.contract()
        return result

    async def submit_paper_order(self, proposal_id: str, **kwargs: Any) -> dict[str, Any]:
        return await self._execution_service().submit(proposal_id, **kwargs)  # type: ignore[no-any-return]

    def paper_orders(
        self, *, status: str = "", portfolio_id: str = "", limit: int = 100
    ) -> dict[str, Any]:
        return self._execution_service().orders(  # type: ignore[no-any-return]
            status=status, portfolio_id=portfolio_id, limit=limit
        )

    def paper_order(self, order_id: str) -> dict[str, Any]:
        return self._execution_service().order(order_id)  # type: ignore[no-any-return]

    def paper_fills(self, order_id: str = "") -> dict[str, Any]:
        return self._execution_service().fills(order_id)  # type: ignore[no-any-return]

    async def process_paper_order(self, order_id: str) -> dict[str, Any]:
        return await self._execution_service().process(order_id)  # type: ignore[no-any-return]

    async def cancel_paper_order(
        self, order_id: str, *, reason: str = "", actor: str = ""
    ) -> dict[str, Any]:
        return await self._execution_service().cancel(  # type: ignore[no-any-return]
            order_id, reason=reason, actor=actor
        )

    def execution_fees(self) -> dict[str, Any]:
        return self._execution_service().fees()  # type: ignore[no-any-return]

    def execution_slippage(self) -> dict[str, Any]:
        return self._execution_service().slippage()  # type: ignore[no-any-return]

    async def reconcile_paper_execution(self, order_id: str = "") -> dict[str, Any]:
        return await self._execution_service().reconcile(order_id)  # type: ignore[no-any-return]

    def paper_reconciliations(self, limit: int = 50) -> dict[str, Any]:
        return self._execution_service().reconciliations(limit)  # type: ignore[no-any-return]

    # -- NM-11: reproducible historical backtesting --------------------------

    def _ensure_backtesting(self) -> "BacktestingService":
        from novacontrol.novamarket.backtesting import BacktestingService

        if self._backtest_service is not None:
            return cast("BacktestingService", self._backtest_service)
        try:
            service = BacktestingService(
                self._radar_path(),
                market_data=self._market_data_engine(),
                strategy_loader=self._backtest_strategy,
                risk_policy_loader=self._backtest_risk_policy,
                event_loader=self._backtest_events,
                forecast_loader=self._backtest_forecasts,
                publisher=self._backtest_publish,
                clock=self._clock,
            )
        except Exception as exc:
            self._backtest_error = f"{type(exc).__name__}: {exc}"
            raise
        self._backtest_service = service
        self._backtest_error = ""
        return service

    def _backtesting(self) -> "BacktestingService":
        service = self._backtest_service
        if service is not None:
            return cast("BacktestingService", service)
        if self._lifecycle.state not in (
            LifecycleState.READY,
            LifecycleState.RUNNING,
            LifecycleState.DEGRADED,
        ):
            raise NotOperationalError(
                "backtesting is not available while NovaMarket is "
                f"{self._lifecycle.state.value}; enable NovaMarket first"
            )
        return self._ensure_backtesting()

    def _backtest_strategy(self, strategy_id: str) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], self._strategy_service().strategy(strategy_id))

    def _backtest_risk_policy(self, version: str) -> Mapping[str, Any]:
        body = self._risk_service().policies()
        rows = list(body.get("policies") or ())
        if not rows:
            raise NotOperationalError("no NM-08 risk policy is available")
        selected = next(
            (
                row
                for row in rows
                if version
                and (str(row.get("policy_id")) == version or str(row.get("version")) == version)
            ),
            rows[0],
        )
        payload = dict(selected)
        payload["max_gross_exposure"] = payload.get(
            "max_invested_fraction", payload.get("max_gross_exposure")
        )
        drawdown = payload.get("max_drawdown_pct", payload.get("max_drawdown"))
        if drawdown is not None:
            value = Decimal(str(drawdown))
            payload["max_drawdown"] = value / Decimal("100") if value > 1 else value
        return payload

    def _backtest_events(self, instrument_id: str, at: datetime) -> Sequence[Mapping[str, Any]]:
        if self._radar_engine is None:
            return ()
        body = self._radar_engine.search(
            RadarEventQuery(instrument_id=instrument_id, until=at, limit=100)
        )
        return tuple(dict(row) for row in body.get("events") or ())

    def _backtest_forecasts(self, instrument_id: str, at: datetime) -> Sequence[Mapping[str, Any]]:
        """Point-in-time forecasts from the existing NM-06 and PI-01 stores.

        NM-06 rows keep their own shape. PI-09 forecast contracts are adapted
        to the same read-only strategy input, with their horizon names mapped
        to the NM-06 labels understood by the existing ``forecast_assisted``
        catalog entry. The adaptor never regenerates a forecast and never
        exposes an execution path.
        """
        rows: list[dict[str, Any]] = []
        if self._forecast_engine is not None:
            body = self._forecast_engine.forecasts(instrument_id=instrument_id, limit=500)
            rows.extend(dict(row) for row in body.get("forecasts") or ())
        if self._prediction_service is not None:
            horizon_map = {
                "5m": "next_session",
                "15m": "next_session",
                "20m": "next_session",
                "1h": "next_session",
                "1d": "next_session",
                "3d": "few_sessions",
                "5d": "one_week",
                "30d": "one_month",
                "90d": "three_months",
            }
            try:
                contracts = self._prediction_service.store.list_forecasts(
                    instrument_id=instrument_id, limit=500
                )
            except Exception as exc:  # noqa: BLE001 - one source cannot break replay
                self._backtest_error = f"PI-09 forecast read: {type(exc).__name__}: {exc}"
                contracts = []
            for contract in contracts:
                if contract.generated_at > at:
                    continue
                metadata = dict(contract.metadata or {})
                validity = contract.validity.value
                state = str(metadata.get("model_state") or "")
                if not state:
                    state = (
                        "validated"
                        if validity in {"valid", "confirmed"}
                        and contract.probability_status.value == "calibrated"
                        else "experimental"
                    )
                status = (
                    "active"
                    if validity in {"valid", "confirmed"}
                    else "failed"
                    if validity in {"invalid", "expired"}
                    else "degraded"
                )
                rows.append(
                    {
                        "forecast_id": contract.forecast_id,
                        "instrument_id": contract.instrument_id,
                        "created_at": contract.generated_at.isoformat(),
                        "data_cutoff": contract.data_observed_at.isoformat(),
                        "horizon": horizon_map.get(
                            contract.forecast_horizon.value,
                            contract.forecast_horizon.value,
                        ),
                        "target_at": contract.target_at.isoformat(),
                        "predicted_return": contract.predicted_return,
                        "predicted_price": format(contract.predicted_price, "f"),
                        "probability_up": contract.p_up,
                        "lower_return": contract.lower_return,
                        "upper_return": contract.upper_return,
                        "uncertainty": contract.uncertainty.value,
                        "model_id": contract.model_id,
                        "model_version": contract.model_version,
                        "ensemble_id": contract.ensemble_id,
                        "model_state": state,
                        "status": status,
                        "source": "PI-09",
                    }
                )
        return tuple(rows)

    async def _backtest_publish(self, event_type: str, payload: dict[str, Any]) -> None:
        await self._publish(EventType(str(event_type)), **payload)

    def backtest_status(self) -> dict[str, Any]:
        if self._backtest_service is None:
            return {
                "ok": True,
                "phase": "NM-11",
                "initialised": False,
                "simulated": True,
                "real_money": False,
                "last_error": self._backtest_error,
                "detail": "backtesting opens when NovaMarket is operational",
            }
        return cast(dict[str, Any], self._backtest_service.status())

    async def backtest_create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._backtesting().create(payload)

    async def backtest_validate(
        self, payload: Mapping[str, Any], *, load_data: bool = False
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._backtesting().validate, payload, load_data=load_data)

    async def backtest_start(self, backtest_id: str) -> dict[str, Any]:
        return await self._backtesting().start(backtest_id)

    async def backtest_cancel(self, backtest_id: str) -> dict[str, Any]:
        return await self._backtesting().cancel(backtest_id)

    def backtest_list(self, limit: int = 50) -> dict[str, Any]:
        return self._backtesting().list(limit)

    def backtest(self, backtest_id: str, *, include_bars: bool = False) -> dict[str, Any]:
        return self._backtesting().get(backtest_id, include_bars=include_bars)

    def backtest_results(self, backtest_id: str) -> dict[str, Any]:
        return self._backtesting().results(backtest_id)

    def backtest_metrics(self, backtest_id: str) -> dict[str, Any]:
        return self._backtesting().metrics(backtest_id)

    def backtest_trades(self, backtest_id: str) -> dict[str, Any]:
        return self._backtesting().trades(backtest_id)

    def backtest_orders(self, backtest_id: str) -> dict[str, Any]:
        return self._backtesting().orders(backtest_id)

    def backtest_decisions(self, backtest_id: str) -> dict[str, Any]:
        return self._backtesting().decisions(backtest_id)

    def backtest_signals(self, backtest_id: str) -> dict[str, Any]:
        return self._backtesting().signals(backtest_id)

    def backtest_data_quality(self, backtest_id: str) -> dict[str, Any]:
        return self._backtesting().data_quality(backtest_id)

    def backtest_compare(self, backtest_ids: Sequence[str]) -> dict[str, Any]:
        return self._backtesting().compare(backtest_ids)

    def backtest_comparisons(self, limit: int = 50) -> dict[str, Any]:
        return self._backtesting().comparisons(limit)

    async def backtest_verify(self, backtest_id: str) -> dict[str, Any]:
        return await self._backtesting().verify(backtest_id)

    async def backtest_sensitivity(
        self, base_id: str, variants: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        return await self._backtesting().sensitivity(base_id, variants)

    async def backtest_walk_forward(
        self, backtest_id: str, train_bars: object, test_bars: object
    ) -> dict[str, Any]:
        return await self._backtesting().walk_forward(backtest_id, train_bars, test_bars)

    def backtest_contract(self) -> dict[str, Any]:
        if self._backtest_service is None:
            return {
                "ok": True,
                "phase": "NM-11",
                "implemented": False,
                "simulated": True,
                "real_money": False,
                "detail": "backtesting is not initialised in this run",
            }
        return cast(dict[str, Any], self._backtest_service.contract())

    # -- PI-13: isolated reinforcement-learning research ---------------------
    def _rl_path(self) -> Path:
        if self._data_dir is not None:
            return self._data_dir / "novamarket_rl.sqlite3"
        if self._market_data_path is not None:
            return Path(self._market_data_path).with_name("novamarket_rl.sqlite3")
        return Path(":memory:")

    def _ensure_rl(self) -> Any:
        from novacontrol.novamarket.rl import RLService

        if self._rl_service is not None:
            return self._rl_service
        try:
            self._rl_service = RLService(
                self._rl_path(),
                bar_loader=self._rl_bars,
                feature_loader=self._rl_feature_snapshot,
                forecast_loader=self._rl_forecast_snapshot,
                publisher=self._rl_publish,
                clock=self._clock,
            )
        except Exception as exc:  # noqa: BLE001 - status explains the refusal
            self._rl_error = f"{type(exc).__name__}: {exc}"
            raise
        self._rl_error = ""
        return self._rl_service

    def _rl_bars(self, config: Any) -> list[Any]:
        """Load exact PI-02 dataset membership, never current stored bars."""
        from novacontrol.novamarket.backtesting.core import HistoricalBar

        service = self.dataset_service()
        rows: list[Any] = []
        offset = 0
        total = 1
        target = min(20_000, int(config.max_steps) + 2)
        while offset < total and len(rows) < target:
            page = service.bars(
                config.dataset_id,
                config.dataset_version,
                instrument_id=config.instrument_id,
                start=config.start,
                end=config.end,
                limit=1000,
                offset=offset,
                ascending=True,
            )
            items = list(page.get("items") or ())
            total = int(page.get("total") or len(items))
            if not items:
                break
            for item in items:
                if len(rows) >= target:
                    break
                if item.get("missing"):
                    continue
                quality = str(item.get("quality") or "").lower()
                if bool(item.get("partial", False)) or quality in {"invalid", "suspect"}:
                    continue
                observed_raw = item.get("observed_at")
                if not observed_raw:
                    continue
                observed = datetime.fromisoformat(str(observed_raw).replace("Z", "+00:00"))
                retrieved = item.get("retrieved_at") or observed_raw
                rows.append(
                    HistoricalBar(
                        instrument_id=config.instrument_id,
                        observed_at=observed,
                        available_at=observed,
                        retrieved_at=datetime.fromisoformat(str(retrieved).replace("Z", "+00:00")),
                        open=item.get("open"),
                        high=item.get("high"),
                        low=item.get("low"),
                        close=item.get("close"),
                        volume=Decimal(str(item.get("volume") or 0)),
                        source=str(item.get("provider") or "nm02"),
                        adjusted=bool(item.get("adjusted", False)),
                    )
                )
            offset += len(items)
        return rows

    def _rl_feature_snapshot(
        self, config: Any, at: datetime, history: Sequence[Any]
    ) -> Mapping[str, Any] | None:
        """Point-in-time PI-04 features without persisting a feature set."""
        from novacontrol.novamarket.marketdata.models import DataInterval, OhlcvBar

        if len(history) < 30:
            return None
        request = FeatureRequest(
            instrument_id=config.instrument_id,
            as_of=at,
            resolution="1d",
            profile=config.feature_profile,
            include_context=False,
            include_fundamentals=False,
            include_news=False,
            dataset_id=config.dataset_id,
            dataset_version=config.dataset_version,
            max_bars=min(500, len(history)),
        )
        bars = []
        for item in history[-request.max_bars :]:
            bars.append(
                OhlcvBar.create(
                    instrument_id=config.instrument_id,
                    provider=str(item.source or "nm02"),
                    interval=DataInterval.DAY_1,
                    open=item.open,
                    high=item.high,
                    low=item.low,
                    close=item.close,
                    currency="INR",
                    observed_at=item.observed_at,
                    # Dataset membership is the point-in-time provenance.  The
                    # export's ingestion timestamp is later than the replay
                    # cutoff and would otherwise mark every historical bar as
                    # not yet retrieved, yielding empty features.
                    retrieved_at=item.observed_at,
                    source_timezone="Asia/Kolkata",
                    volume=item.volume,
                )
            )
        result = self.feature_service().generate_sync(request, bars={request.resolution: bars})
        return cast(Mapping[str, Any], result.feature_set.to_dict())

    def _rl_forecast_snapshot(self, config: Any, at: datetime) -> Mapping[str, Any] | None:
        """Latest PI-09 forecast for this dataset generated at or before ``at``.

        Forecast contracts are stored per instrument, not per dataset, so the
        rows are filtered to the environment's ``dataset_version``: a forecast
        built on another dataset is not evidence for these observations and
        must never be selected here.
        """
        key = f"{config.instrument_id}|{config.dataset_version}"
        if key not in self._rl_forecast_cache:
            try:
                rows = [
                    row
                    for row in self.prediction_service().store.list_forecasts(
                        instrument_id=config.instrument_id, limit=500
                    )
                    if str(getattr(row, "dataset_version", "") or "")
                    == str(config.dataset_version)
                ]
            except Exception:  # noqa: BLE001 - absence is an explicit missing feature
                rows = []
            rows.sort(key=lambda item: item.generated_at)
            self._rl_forecast_cache[key] = rows
        eligible = [row for row in self._rl_forecast_cache[key] if row.generated_at <= at]
        if not eligible:
            return None
        return cast(Mapping[str, Any], eligible[-1].to_dict())

    async def _rl_publish(self, name: str, payload: Mapping[str, Any]) -> None:
        try:
            event_type = EventType(name)
        except ValueError:
            return
        await self._publish(event_type, **dict(payload))

    def rl_status(self) -> dict[str, Any]:
        if self._rl_service is None:
            return {
                "phase": "PI-13",
                "implemented": True,
                "initialised": False,
                "simulated": True,
                "advisory_only": True,
                "production_authority": False,
                "last_error": self._rl_error,
                "detail": "the RL research environment opens on first use",
            }
        return cast(dict[str, Any], self._rl_service.status())

    def rl_contract(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_rl().contract())

    def rl_health(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_rl().health())

    def rl_create_environment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_rl().create_environment(payload))

    def rl_environments(self, limit: int = 50) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_rl().environments(limit))

    def rl_environment(self, environment_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_rl().environment(environment_id))

    def rl_start_episode(
        self, environment_id: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_rl().start_episode(environment_id, payload))

    def rl_episodes(
        self,
        *,
        environment_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_rl().episodes(environment_id=environment_id, limit=limit, offset=offset),
        )

    def rl_episode(self, episode_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_rl().episode(episode_id))

    def rl_step(self, episode_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_rl().step(episode_id, payload))

    def rl_reset_episode(self, episode_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_rl().reset_episode(episode_id))

    def rl_experiences(
        self, episode_id: str, *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_rl().experiences(episode_id, limit=limit, offset=offset),
        )

    def rl_transitions(
        self, episode_id: str, *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_rl().transitions(episode_id, limit=limit, offset=offset),
        )

    def rl_cancel(self, episode_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_rl().cancel(episode_id))

    # -- PI-14: reward calculation and policy evaluation ---------------------
    def _ensure_reward(self) -> Any:
        from novacontrol.novamarket.rewards import RewardService

        if self._reward_service is not None:
            return self._reward_service
        try:
            self._reward_service = RewardService(
                self._rl_path(),
                transition_reader=self._rl_reward_transitions,
                bar_loader=self._rl_bars,
                feature_loader=self._rl_feature_snapshot,
                forecast_loader=self._rl_forecast_snapshot,
                clock=self._clock,
            )
        except Exception as exc:  # noqa: BLE001 - status explains the refusal
            self._reward_error = f"{type(exc).__name__}: {exc}"
            raise
        self._reward_error = ""
        return self._reward_service

    def _rl_reward_transitions(self, episode_id: str) -> Sequence[Mapping[str, Any]]:
        """Read stored PI-13 transitions in sequence order, bounded by pages."""
        service = self._ensure_rl()
        items: list[Mapping[str, Any]] = []
        offset = 0
        total = 1
        while offset < total and len(items) < 5_000:
            page = service.experiences(episode_id, limit=1_000, offset=offset)
            rows = list(page.get("transitions") or page.get("experiences") or ())
            total = int(page.get("total") or len(rows))
            if not rows:
                break
            items.extend(row for row in rows if isinstance(row, Mapping))
            offset += len(rows)
        return items

    def reward_status(self) -> dict[str, Any]:
        if self._reward_service is None:
            return {
                "phase": "PI-14",
                "implemented": True,
                "initialised": False,
                "simulated": True,
                "experimental": True,
                "advisory_only": True,
                "production_authority": False,
                "last_error": self._reward_error,
                "detail": "the isolated reward layer opens on first use",
            }
        return cast(dict[str, Any], self._reward_service.status())

    def reward_contract(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_reward().contract())

    def reward_health(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_reward().health())

    def reward_create_config(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_reward().create_config(payload))

    def reward_configs(self, limit: int = 50) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_reward().configs(limit))

    def reward_config(self, config_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_reward().config(config_id))

    def reward_calculate_episode(self, episode_id: str, *, config_id: str = "") -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_reward().calculate_episode(episode_id, config_id=config_id),
        )

    def reward_episode_rewards(self, episode_id: str, *, limit: int = 200) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_reward().episode_rewards(episode_id, limit=limit))

    def reward_result(self, reward_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_reward().reward(reward_id))

    def reward_evaluate(
        self,
        payload: Mapping[str, Any],
        *,
        policy_names: Sequence[str] | None = None,
        request_id: str = "",
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_reward().evaluate(
                payload, policy_names=policy_names, request_id=request_id
            ),
        )

    def reward_cancel_evaluation(self, request_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_reward().request_cancel(request_id))

    def reward_evaluations(self, limit: int = 50) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_reward().evaluations(limit))

    def reward_evaluation(self, evaluation_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_reward().evaluation(evaluation_id))

    def reward_comparisons(self, limit: int = 50) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_reward().comparisons(limit))

    def reward_baselines(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_reward().baselines())

    def reward_stability(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_reward().stability(payload))

    def reward_walk_forward(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_reward().walk_forward(payload))

    # -- PI-15: experimental RL training -------------------------------------
    def _ensure_training(self) -> Any:
        from novacontrol.novamarket.agent_training import TrainingManager

        if self._training_service is not None:
            return self._training_service
        try:
            self._training_service = TrainingManager(
                self._rl_path(),
                bar_loader=self._rl_bars,
                feature_loader=self._rl_feature_snapshot,
                forecast_loader=self._rl_forecast_snapshot,
                reward_service=self._ensure_reward(),
                clock=self._clock,
            )
        except Exception as exc:  # noqa: BLE001 - status explains the refusal
            self._training_error = f"{type(exc).__name__}: {exc}"
            raise
        self._training_error = ""
        return self._training_service

    def training_status(self) -> dict[str, Any]:
        if self._training_service is None:
            return {
                "phase": "PI-15",
                "implemented": True,
                "initialised": False,
                "simulated": True,
                "experimental": True,
                "advisory_only": True,
                "production_authority": False,
                "activation_available": False,
                "last_error": self._training_error,
                "detail": "the isolated training layer opens on first use",
            }
        return cast(dict[str, Any], self._training_service.status())

    def training_contract(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().contract())

    def training_health(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().health())

    def training_create_config(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().create_config(payload))

    def training_configs(self, limit: int = 50) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().configs(limit))

    def training_config(self, config_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().config(config_id))

    def training_validate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().validate_config(payload))

    def training_start(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().start(payload))

    def training_runs(self, limit: int = 50, *, status: str = "") -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().runs(limit, status=status))

    def training_run(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().run(run_id))

    def training_metrics(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().metrics(run_id))

    def training_pause(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().pause(run_id))

    def training_resume(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().resume(run_id))

    def training_cancel(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().cancel(run_id))

    def training_retry(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().retry(run_id))

    def training_checkpoints(self, run_id: str = "", limit: int = 100) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().checkpoints(run_id, limit))

    def training_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().checkpoint(checkpoint_id))

    def training_resume_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().resume_checkpoint(checkpoint_id))

    def training_request_checkpoint(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().request_checkpoint(run_id))

    def training_policies(self, limit: int = 50, *, status: str = "") -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().policies(limit, status=status))

    def training_policy(self, policy_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().policy(policy_id))

    def training_archive_policy(self, policy_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().archive_policy(policy_id))

    def training_validate_policy(self, policy_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().validate_policy(policy_id))

    def training_test_policy(self, policy_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().test_policy(policy_id))

    def training_compare(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().compare_policies(payload))

    def training_optimize(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().optimize(payload))

    def training_search(self, search_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().search(search_id))

    def training_evaluations(self, policy_id: str = "", limit: int = 50) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_training().evaluations(policy_id, limit))

    # -- PI-16: adaptive learning and market-regime intelligence -------------
    def _ensure_adaptive(self) -> Any:
        from novacontrol.novamarket.adaptive import AdaptiveIntelligenceService

        if self._adaptive_service is not None:
            return self._adaptive_service
        try:
            self._adaptive_service = AdaptiveIntelligenceService(
                self._rl_path(),
                feature_service=self.feature_service(),
                training_service=self._ensure_training(),
                reward_service=self._ensure_reward(),
                clock=self._clock,
            )
        except Exception as exc:  # noqa: BLE001 - status explains the refusal
            self._adaptive_error = f"{type(exc).__name__}: {exc}"
            raise
        self._adaptive_error = ""
        return self._adaptive_service

    def adaptive_status(self) -> dict[str, Any]:
        if self._adaptive_service is None:
            return {
                "phase": "PI-16",
                "implemented": True,
                "initialised": False,
                "advisory_only": True,
                "production_authority": False,
                "activation_available": False,
                "last_error": self._adaptive_error,
                "detail": "adaptive intelligence opens on first use",
            }
        return cast(dict[str, Any], self._adaptive_service.status())

    def adaptive_contract(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_adaptive().contract())

    def adaptive_health(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_adaptive().health())

    def adaptive_dashboard(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_adaptive().dashboard())

    def adaptive_create_config(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_adaptive().create_config(payload))

    def adaptive_configs(self, limit: int = 50) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_adaptive().configs(limit))

    def adaptive_config(self, config_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_adaptive().config_record(config_id))

    def adaptive_regime_analysis(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_adaptive().regime_analysis(payload))

    def adaptive_latest_regime(self, instrument_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_adaptive().latest_regime(instrument_id))

    def adaptive_annotate_regimes(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_adaptive().annotate_regimes(payload))

    def adaptive_annotations(
        self, instrument_id: str = "", limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_adaptive().annotations(instrument_id, limit=limit, offset=offset),
        )

    def adaptive_regime_experiences(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_adaptive().regime_experiences(payload))

    def adaptive_record_regime_evaluation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_adaptive().record_regime_evaluation(payload))

    def adaptive_regime_performance(
        self,
        policy_id: str = "",
        *,
        regime: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_adaptive().regime_performance(
                policy_id, regime=regime, limit=limit, offset=offset
            ),
        )

    def adaptive_detect_change(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_adaptive().detect_market_change(payload))

    def adaptive_changes(
        self, instrument_id: str = "", limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_adaptive().changes(instrument_id, limit=limit, offset=offset),
        )

    def adaptive_detect_drift(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_adaptive().detect_drift(payload))

    def adaptive_drift(
        self, subject: str = "", limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any], self._ensure_adaptive().drift(subject, limit=limit, offset=offset)
        )

    def adaptive_monitor_degradation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_adaptive().monitor_degradation(payload))

    def adaptive_degradation(
        self, policy_id: str = "", limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_adaptive().degradation(policy_id, limit=limit, offset=offset),
        )

    def adaptive_assess_suitability(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_adaptive().assess_suitability(payload))

    def adaptive_suitability(
        self, policy_id: str = "", limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_adaptive().suitability(policy_id, limit=limit, offset=offset),
        )

    def adaptive_recommend(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_adaptive().recommend(payload))

    def adaptive_recommendations(
        self,
        policy_id: str = "",
        *,
        status: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_adaptive().recommendations(
                policy_id, status=status, limit=limit, offset=offset
            ),
        )

    def adaptive_acknowledge_recommendation(
        self, recommendation_id: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_adaptive().acknowledge_recommendation(recommendation_id, payload),
        )

    def adaptive_retraining_recommendation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_adaptive().retraining_recommendation(payload))

    def adaptive_retraining(
        self, policy_id: str = "", limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_adaptive().retraining(policy_id, limit=limit, offset=offset),
        )

    def adaptive_create_reassessment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_adaptive().create_reassessment(payload))

    def adaptive_run_reassessment(self, reassessment_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_adaptive().run_reassessment(reassessment_id))

    def adaptive_reassessments(
        self, policy_id: str = "", limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_adaptive().reassessments(policy_id, limit=limit, offset=offset),
        )

    def adaptive_reassessment(self, reassessment_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_adaptive().reassessment(reassessment_id))

    # -- PI-17: multi-agent financial intelligence ---------------------------
    def _multiagent_path(self) -> Path:
        return self._rl_path().with_name("novamarket_multiagent.sqlite3")

    def _ensure_multiagent(self) -> Any:
        from novacontrol.novamarket.multiagent import (
            MultiAgentIntelligenceService,
        )

        if self._multiagent_service is not None:
            return self._multiagent_service
        try:
            self._multiagent_service = MultiAgentIntelligenceService(
                self._multiagent_path(),
                context_loader=self._multiagent_context,
                clock=self._clock,
            )
        except Exception as exc:  # noqa: BLE001 - status explains the refusal
            self._multiagent_error = f"{type(exc).__name__}: {exc}"
            raise
        self._multiagent_error = ""
        return self._multiagent_service

    def _multiagent_context(self, request: Any) -> Mapping[str, Any]:
        """Assemble read-only PI-04 through PI-16 evidence for one cycle.

        Every read is isolated: a missing source reduces the evidence package
        rather than failing the whole application.  No risk assessment is
        approved, no portfolio is mutated and no execution service is touched.
        """
        from novacontrol.novamarket.multiagent.models import canonical_horizon

        context: dict[str, Any] = {}
        instrument_id = str(request.instrument_id)
        horizon = canonical_horizon(str(request.horizon))

        def safe_read(label: str, reader: Any) -> Any:
            try:
                return reader()
            except Exception as exc:  # noqa: BLE001 - optional evidence source
                context.setdefault("source_notes", []).append(
                    f"{label}: {type(exc).__name__}: {exc}"
                )
                return None

        feature_set = safe_read("features", lambda: self.latest_feature_set(instrument_id))
        if isinstance(feature_set, Mapping):
            context["features"] = dict(feature_set)
            market: dict[str, Any] = {
                "instrument": {
                    "instrument_id": instrument_id,
                    "exchange": str(feature_set.get("exchange") or request.exchange),
                },
                "as_of": feature_set.get("as_of"),
                "data_version": str(feature_set.get("version") or ""),
                "source_reference": str(feature_set.get("feature_set_id") or ""),
            }
            context["market"] = market
        regime = safe_read("regime", lambda: self.latest_regime(instrument_id))
        if isinstance(regime, Mapping):
            context["regime"] = dict(regime)
        news = safe_read("news", lambda: self._outlook_news(instrument_id))
        if news:
            context["news"] = [dict(row) for row in news]
        event_evidence = safe_read(
            "events",
            lambda: self.event_intelligence_agent_context(instrument_id, horizon),
        )
        if isinstance(event_evidence, Mapping) and event_evidence.get("ok"):
            context["events"] = dict(event_evidence)
        forecasts = safe_read(
            "forecasts",
            lambda: self._multiagent_forecasts(instrument_id, horizon, request.as_of),
        )
        if forecasts:
            context["forecasts"] = forecasts
        portfolio_id = str(getattr(request, "portfolio_id", "") or "")
        if portfolio_id:
            detail = safe_read("portfolio", lambda: self.portfolio_detail(portfolio_id))
            if isinstance(detail, Mapping):
                portfolio_context = dict(detail)
                valuation = portfolio_context.get("valuation")
                if isinstance(valuation, Mapping) and valuation.get("as_of"):
                    portfolio_context["as_of"] = valuation.get("as_of")
                elif isinstance(portfolio_context.get("portfolio"), Mapping):
                    portfolio_context["as_of"] = portfolio_context["portfolio"].get("updated_at")
                optimization = safe_read(
                    "optimization",
                    lambda: self.optimization_latest(portfolio_id, horizon=horizon),
                )
                if isinstance(optimization, Mapping):
                    portfolio_context["optimization"] = {
                        "run_id": optimization.get("run_id"),
                        "status": optimization.get("status"),
                        "horizon": optimization.get("horizon"),
                        "target_weights": optimization.get("target_weights", {}),
                        "cash_reserve": optimization.get("cash_reserve"),
                        "created_at": optimization.get("created_at"),
                        "advisory_only": True,
                        "production_authority": False,
                    }
                context["portfolio"] = portfolio_context
            risk = safe_read("risk", lambda: self.risk_portfolio(portfolio_id))
            if isinstance(risk, Mapping):
                risk_context = dict(risk)
                risk_context["assessed_at"] = self._clock().isoformat()
                risk_context["source"] = "NM-08 read-only portfolio risk report"
                context["risk"] = risk_context
        rl = safe_read("rl", lambda: self.training_policies(20))
        if isinstance(rl, Mapping):
            policies = rl.get("policies") or rl.get("items") or []
            if policies:
                context["rl"] = {"policies": [dict(row) for row in policies]}
        suitability = safe_read(
            "adaptive_suitability", lambda: self.adaptive_suitability(limit=100)
        )
        adaptive: dict[str, Any] = {}
        if isinstance(suitability, Mapping):
            adaptive["suitability"] = list(suitability.get("items") or [])
        latest_adaptive = safe_read(
            "adaptive_regime", lambda: self.adaptive_latest_regime(instrument_id)
        )
        if isinstance(latest_adaptive, Mapping):
            adaptive["latest_regime"] = dict(latest_adaptive)
        if adaptive:
            context["adaptive"] = adaptive
        return context

    def _multiagent_forecasts(
        self, instrument_id: str, horizon: str, as_of: Any
    ) -> list[dict[str, Any]]:
        """Read PI-05/PI-08 forecasts for the declared horizon only.

        NovaMarket exposes short intraday horizons through the same lifecycle
        labels used by PI-09.  The mapping is explicit and the source label is
        retained so the agent never silently mixes horizons.
        """
        horizon_label = {
            "5m": "5m",
            "15m": "15m",
            "20m": "20m",
            "60m": "1h",
            "1d": "1d",
            "3d": "3d",
            "5d": "5d",
            "30d": "30d",
            "90d": "90d",
        }[horizon]
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for engine, source in (
            ("statistical", "PI-05 statistical forecast"),
            ("ensemble", "PI-08 ensemble forecast"),
        ):
            try:
                result = self.forecast_list(
                    instrument_id=instrument_id,
                    horizon=horizon_label,
                    engine=engine,
                    limit=30,
                )
            except Exception:  # noqa: BLE001 - one forecast source may be absent
                continue
            for raw in result.get("forecasts") or result.get("items") or []:
                if not isinstance(raw, Mapping):
                    continue
                row = dict(raw)
                forecast_id = str(row.get("forecast_id") or "")
                if forecast_id and forecast_id in seen:
                    continue
                seen.add(forecast_id)
                target_at = row.get("target_at")
                row["source"] = source
                row["source_horizon"] = horizon_label
                row["horizon"] = horizon
                row["expected_return"] = row.get("expected_return", row.get("predicted_return"))
                row["direction"] = row.get("direction") or row.get("predicted_direction")
                row["uncertainty"] = row.get("uncertainty") or row.get("forecast_uncertainty")
                row["validity"] = row.get("validity") or row.get("forecast_validity")
                row["generated_at"] = (
                    row.get("generated_at")
                    or row.get("created_at")
                    or row.get("forecast_generation_timestamp")
                )
                row["available_at"] = (
                    row.get("available_at")
                    or row.get("created_at")
                    or row.get("forecast_generation_timestamp")
                )
                if not row.get("direction"):
                    up = row.get("p_up")
                    down = row.get("p_down")
                    sideways = row.get("p_sideways")
                    if (
                        isinstance(up, (int, float))
                        and isinstance(down, (int, float))
                        and isinstance(sideways, (int, float))
                    ):
                        row["direction"] = (
                            "up"
                            if float(up) > max(float(down), float(sideways))
                            else "down"
                            if float(down) > max(float(up), float(sideways))
                            else "sideways"
                        )
                if not row.get("validity"):
                    try:
                        row["validity"] = (
                            "expired" if target_at and str(target_at) <= str(as_of) else "valid"
                        )
                    except Exception:  # noqa: BLE001
                        row["validity"] = "valid"
                rows.append(row)
        return rows

    def multiagent_status(self) -> dict[str, Any]:
        if self._multiagent_service is None:
            return {
                "phase": "PI-17",
                "implemented": True,
                "initialised": False,
                "advisory_only": True,
                "production_authority": False,
                "order_submission_available": False,
                "risk_authority": "NM-08",
                "execution_authority": "NM-10",
                "last_error": self._multiagent_error,
                "detail": "multi-agent intelligence opens on first use",
            }
        return cast(dict[str, Any], self._multiagent_service.status())

    def multiagent_contract(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_multiagent().contract())

    def multiagent_health(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_multiagent().health())

    def multiagent_agents(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_multiagent().agents())

    def multiagent_agent(self, agent_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_multiagent().agent(agent_id))

    def multiagent_update_config(self, agent_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_multiagent().update_config(agent_id, payload),
        )

    def multiagent_create_cycle(
        self, payload: Mapping[str, Any], *, idempotency_key: str = ""
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_multiagent().create_cycle(payload, idempotency_key=idempotency_key),
        )

    def multiagent_run_cycle(self, cycle_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_multiagent().run_cycle(cycle_id))

    def multiagent_cancel_cycle(self, cycle_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_multiagent().cancel_cycle(cycle_id))

    def multiagent_cycle(self, cycle_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_multiagent().cycle(cycle_id))

    def multiagent_cycles(
        self,
        *,
        instrument_id: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_multiagent().cycles(
                instrument_id=instrument_id,
                status=status,
                limit=limit,
                offset=offset,
            ),
        )

    def multiagent_runs(self, cycle_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_multiagent().runs(cycle_id))

    def multiagent_evidence(self, cycle_id: str, *, agent_id: str = "") -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_multiagent().evidence(cycle_id, agent_id=agent_id),
        )

    def multiagent_conflicts(self, cycle_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_multiagent().conflicts(cycle_id))

    def multiagent_decision(self, cycle_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_multiagent().decision(cycle_id))

    def multiagent_history(
        self,
        *,
        instrument_id: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_multiagent().history(
                instrument_id=instrument_id, limit=limit, offset=offset
            ),
        )

    def multiagent_performance(self, agent_id: str = "") -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_multiagent().performance(agent_id),
        )

    def multiagent_handoff(self, cycle_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_multiagent().handoff(cycle_id))

    # -- PI-19: advanced market intelligence and financial research ----------
    def _research_path(self) -> Path:
        return self._rl_path().with_name("novamarket_research.sqlite3")

    def _filings_adapter(self) -> Any:
        """Build the one free filing reader on first use (no boot-time network)."""
        if self._filings_reader is not None:
            return self._filings_reader
        from novacontrol.novamarket.filings import YahooFundamentalsAdapter

        cache_dir: Path | None = None
        try:
            cache_dir = self._research_path().parent / "cache" / "filings"
        except Exception:  # noqa: BLE001 - an in-memory store simply has no cache
            cache_dir = None
        self._filings_reader = YahooFundamentalsAdapter(
            cache_dir=cache_dir,
            clock=self._clock,
        )
        return self._filings_reader

    def _ensure_research(self) -> Any:
        from novacontrol.novamarket.research import ResearchService

        if self._research_service is not None:
            return self._research_service
        try:
            self._research_service = ResearchService(
                str(self._research_path()),
                context_loader=self._research_context,
                clock=self._clock,
                run_timeout_seconds=45.0,
            )
        except Exception as exc:  # noqa: BLE001 - status explains the refusal
            self._research_error = f"{type(exc).__name__}: {exc}"
            raise
        self._research_error = ""
        return self._research_service

    def _research_context(self, request: Any) -> Mapping[str, Any]:
        """Assemble read-only NM-02/PI-04/PI-08/NM-04/NM-07 research context.

        No paid financial-statement provider is bundled.  Statements arrive
        only through an explicit caller/adaptor context, and a missing filing
        remains missing rather than being reconstructed by an LLM.  Every read
        below is isolated: a failed source reduces the evidence package and is
        reported in ``source_notes``.  No order, risk approval or portfolio
        mutation is available from this method.
        """
        context: dict[str, Any] = {}
        notes: list[str] = []

        def safe_read(label: str, reader: Any) -> Any:
            try:
                return reader()
            except Exception as exc:  # noqa: BLE001 - optional evidence source
                notes.append(f"{label}: {type(exc).__name__}: {exc}")
                return None

        instrument_id = str(request.instrument_id)
        detail = safe_read(
            "instrument",
            lambda: self._workbench().instrument(instrument_id, interval="1d"),
        )
        sector_name = ""
        as_of = request.as_of
        instrument: dict[str, Any] = {}
        if isinstance(detail, Mapping):
            raw_instrument = detail.get("instrument")
            instrument = dict(raw_instrument) if isinstance(raw_instrument, Mapping) else {}
            sector_name = str(detail.get("sector") or instrument.get("sector") or "")
            metadata = instrument.get("metadata")
            industry = (
                str(metadata.get("industry") or metadata.get("sector_industry") or "")
                if isinstance(metadata, Mapping)
                else ""
            )
            context["instrument"] = {
                "instrument_id": instrument_id,
                "name": str(instrument.get("name") or request.company_name or ""),
                "symbol": str(instrument.get("symbol") or instrument_id.split(":", 1)[-1]),
                "exchange": str(instrument.get("exchange") or request.exchange or ""),
                "sector": sector_name,
                "industry": industry or sector_name,
                "asset_class": instrument.get("asset_class"),
            }
            if detail.get("analysis_available") and detail.get("last_price") is not None:
                context["price"] = {
                    "value": detail.get("last_price"),
                    "currency": detail.get("currency") or "INR",
                    "as_of": detail.get("as_of") or request.as_of.isoformat(),
                    "source": f"NM-02 stored bar ({detail.get('provider') or 'unknown provider'})",
                    "price_basis": detail.get("price_basis"),
                }
                as_of = str(detail.get("as_of") or request.as_of.isoformat())
            if detail.get("analysis_available") is False:
                notes.append(
                    f"instrument series: {detail.get('reason') or 'no usable stored bars'}"
                )
        features = safe_read("features", lambda: self.latest_feature_set(instrument_id))
        if isinstance(features, Mapping):
            context["features"] = dict(features)
            metadata = features.get("metadata")
            if isinstance(metadata, Mapping):
                context.setdefault("instrument", {})
                context["instrument"].setdefault("sector", metadata.get("sector"))
                context["instrument"].setdefault("industry", metadata.get("industry"))
        regime = safe_read("regime", lambda: self.latest_regime(instrument_id))
        if isinstance(regime, Mapping):
            context["regime"] = dict(regime)
        forecasts = safe_read(
            "forecasts",
            lambda: self._multiagent_forecasts(instrument_id, str(request.horizon), request.as_of),
        )
        if forecasts:
            context["forecasts"] = list(forecasts)
        news_rows = safe_read("news", lambda: self._outlook_news(instrument_id))
        news: list[dict[str, Any]] = []
        for row in news_rows or ():
            if not isinstance(row, Mapping):
                continue
            headline = str(row.get("title") or row.get("headline") or "").strip()
            if not headline:
                continue
            news.append(
                {
                    "headline": headline,
                    "summary": row.get("summary") or "",
                    "url": row.get("url") or row.get("source_url") or "",
                    "source_name": row.get("source_name")
                    or row.get("source")
                    or "NovaMarket event radar",
                    "source_id": row.get("source_id") or row.get("source") or "nm04:radar",
                    "published_at": row.get("published_at")
                    or row.get("observed_at")
                    or row.get("event_time"),
                    "retrieved_at": row.get("retrieved_at") or as_of,
                    "source_tier": row.get("source_tier") or "unknown",
                    "verification": row.get("verification") or "unverified",
                    "category": row.get("category") or "company",
                    "instrument_ids": [instrument_id],
                    "claims": [],
                }
            )
        if news:
            context["news"] = news
        else:
            notes.append("news: no source-attributed point-in-time radar item")
        def aware_iso(value: Any, fallback: str) -> str:
            """Normalize a reference date for research's timezone-aware contract.

            The packaged sector reference carries a calendar date ("2026-11-05");
            research rejects a naive timestamp, so a date is anchored at UTC
            midnight instead of being dropped.
            """
            candidate = str(value or "").strip()
            if not candidate:
                return fallback
            try:
                parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            except ValueError:
                return fallback
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.isoformat()

        as_of_iso = as_of.isoformat() if isinstance(as_of, datetime) else str(as_of)
        if sector_name:
            snapshot = safe_read(
                "sector",
                lambda: self._workbench_snapshot("market", interval="1d", limit=80),
            )
            sector_row: Mapping[str, Any] | None = None
            if isinstance(snapshot, Mapping):
                for candidate in snapshot.get("sector_flow") or ():
                    if (
                        isinstance(candidate, Mapping)
                        and str(candidate.get("sector") or "").lower() == sector_name.lower()
                    ):
                        sector_row = candidate
                        break
            if sector_row is not None:
                sector_as_of = aware_iso(sector_row.get("sector_as_of"), as_of_iso)
                context["sector"] = {
                    "sector": sector_name,
                    "as_of": sector_as_of,
                    "published_at": sector_as_of,
                    "source_name": sector_row.get("sector_source")
                    or "NovaMarket workbench sector flow",
                    "source_id": "nm02:sector_flow",
                    "source_tier": "independent",
                    "performance": (
                        float(sector_row["average_change_pct"]) / 100.0
                        if isinstance(sector_row.get("average_change_pct"), (int, float))
                        else None
                    ),
                    "relative_strength": (
                        float(sector_row["relative_strength_pct"]) / 100.0
                        if isinstance(sector_row.get("relative_strength_pct"), (int, float))
                        else None
                    ),
                    "verification": "partially_verified",
                    "warnings": [
                        "sector performance is measured over the analysed stored-bar universe",
                    ],
                }
            else:
                context["sector"] = {
                    "sector": sector_name,
                    "as_of": as_of_iso,
                    "published_at": as_of_iso,
                    "source_name": "NM-02 instrument metadata",
                    "verification": "partially_verified",
                    "warnings": ["no sector performance series was available"],
                }
                notes.append("sector: classification only; no performance snapshot")
        filings = (
            None
            if _filings_disabled()
            else safe_read(
                "filings",
                lambda: self._filings_adapter().load(
                    {**instrument, "instrument_id": instrument_id},
                    instrument_id=instrument_id,
                    as_of=request.as_of,
                ),
            )
        )
        if filings is not None:
            if filings.statements:
                context["statements"] = [dict(item) for item in filings.statements]
                context["statement_source_id"] = FILING_SOURCE_ID
                context["statement_source_name"] = FILING_SOURCE_NAME
            if filings.evidence:
                supplied = [
                    item
                    for item in (context.get("evidence") or ())
                    if isinstance(item, Mapping)
                ]
                context["evidence"] = [
                    *supplied,
                    *(dict(item) for item in filings.evidence),
                ]
            notes.extend(str(item) for item in filings.notes)
        portfolio_id = str(request.portfolio_id or "")
        if portfolio_id:
            portfolio = safe_read("portfolio", lambda: self.portfolio_detail(portfolio_id))
            if isinstance(portfolio, Mapping):
                nested_portfolio = portfolio.get("portfolio")
                portfolio_as_of = portfolio.get("as_of")
                if portfolio_as_of is None and isinstance(nested_portfolio, Mapping):
                    portfolio_as_of = nested_portfolio.get("updated_at")
                context["portfolio"] = {
                    "portfolio_id": portfolio_id,
                    "exposure": portfolio.get("valuation")
                    if isinstance(portfolio.get("valuation"), Mapping)
                    else None,
                    "as_of": portfolio_as_of,
                    "source": "NM-07 read-only portfolio snapshot",
                    "can_mutate": False,
                }
        context["source_notes"] = notes
        return context

    def research_status(self) -> dict[str, Any]:
        if self._research_service is None:
            return {
                "phase": "PI-19",
                "implemented": True,
                "initialised": False,
                "advisory_only": True,
                "production_authority": False,
                "order_submission_available": False,
                "risk_authority": "NM-08",
                "execution_authority": "NM-10",
                "last_error": self._research_error,
                "detail": "financial research opens on first use",
            }
        return cast(dict[str, Any], self._research_service.status())

    def research_contract(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_research().contract())

    def research_health(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_research().health())

    def research_create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_research().create(payload))

    def research_run(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_research().run(run_id))

    def research_cancel(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_research().cancel(run_id))

    def research_refresh(
        self, run_id: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        created = self._ensure_research().refresh(run_id, payload or {})
        return cast(dict[str, Any], self._ensure_research().run(str(created["run_id"])))

    def research_detail(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_research().detail(run_id))

    def research_section(self, run_id: str, section: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_research().section(run_id, section))

    def research_evidence(self, run_id: str) -> dict[str, Any]:
        service = self._ensure_research()
        return {"run_id": run_id, "evidence": service.evidence(run_id)}

    def research_claims(self, run_id: str) -> dict[str, Any]:
        service = self._ensure_research()
        return {"run_id": run_id, "claims": service.claims(run_id)}

    def research_features(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_research().features(run_id))

    def research_handoff(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_research().handoff(run_id))

    def research_history(
        self,
        *,
        instrument_id: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return {
            "runs": self._ensure_research().history(
                instrument_id=instrument_id,
                status=status,
                limit=limit,
                offset=offset,
            ),
            "advisory_only": True,
            "financial_mutation": False,
        }

    def research_latest(self, instrument_id: str, *, horizon: str = "") -> dict[str, Any] | None:
        return cast(
            "dict[str, Any] | None",
            self._ensure_research().latest(instrument_id, horizon=horizon),
        )

    def research_monitor(
        self,
        instrument_id: str,
        *,
        horizon: str = "",
        min_interval_hours: float = 6.0,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_research().monitor(
                instrument_id, horizon=horizon, min_interval_hours=min_interval_hours
            ),
        )

    # -- PI-18: portfolio optimization and capital allocation ----------------
    def _optimization_path(self) -> Path:
        return self._rl_path().with_name("novamarket_optimization.sqlite3")

    def _ensure_optimization(self) -> Any:
        from novacontrol.novamarket.optimization import OptimizationService

        if self._optimization_service is not None:
            return self._optimization_service
        try:
            self._optimization_service = OptimizationService(
                self._optimization_path(),
                context_loader=self._optimization_context,
                clock=self._clock,
            )
        except Exception as exc:  # noqa: BLE001 - status explains the refusal
            self._optimization_error = f"{type(exc).__name__}: {exc}"
            raise
        self._optimization_error = ""
        return self._optimization_service

    def _optimization_context(self, request: Any) -> Mapping[str, Any]:
        """Assemble read-only NM-07/NM-02/PI-04/PI-05..PI-10 evidence.

        Missing optional evidence reduces the eligible universe; it never makes
        the optimizer write a price, mutate a portfolio or bypass NM-08/NM-10.
        """
        from novacontrol.novamarket.optimization.models import HORIZONS

        context: dict[str, Any] = {}
        notes: list[str] = []
        portfolio_id = str(request.portfolio_id)

        def safe_read(label: str, reader: Any) -> Any:
            try:
                return reader()
            except Exception as exc:  # noqa: BLE001 - optional evidence source
                notes.append(f"{label}: {type(exc).__name__}: {exc}")
                return None

        detail = safe_read("portfolio", lambda: self.portfolio_detail(portfolio_id))
        if isinstance(detail, Mapping):
            context["portfolio"] = dict(detail)
        risk = safe_read("risk", lambda: self.risk_portfolio(portfolio_id))
        if isinstance(risk, Mapping):
            context["risk"] = dict(risk)
        proposals = safe_read(
            "pending_orders",
            lambda: self.portfolio_proposals(portfolio_id, status="validated", limit=50),
        )
        if isinstance(proposals, Mapping):
            rows = proposals.get("proposals") or proposals.get("items") or []
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)):
                context["pending_orders"] = [dict(row) for row in rows if isinstance(row, Mapping)]

        interval = str(HORIZONS[str(request.horizon)]["interval"])
        context["interval"] = interval
        bars: dict[str, Any] = {}
        forecasts: dict[str, Any] = {}
        features: dict[str, Any] = {}
        regimes: dict[str, Any] = {}
        instruments: dict[str, Any] = {}
        for instrument_id in request.instrument_ids:
            # The bars page is capped below the 320 requested, so asking for the
            # oldest rows silently withheld the recent ones. Read newest-first
            # and reverse into chronological order for the optimiser.
            page = safe_read(
                f"bars:{instrument_id}",
                lambda item=instrument_id: self.market_bars(
                    item, interval=interval, limit=320, ascending=False
                ),
            )
            if isinstance(page, Mapping):
                rows = list(page.get("items") or page.get("bars") or [])
                rows.reverse()
                if rows:
                    bars[instrument_id] = {"bars": rows}
            forecast_rows = safe_read(
                f"forecasts:{instrument_id}",
                lambda item=instrument_id: self._multiagent_forecasts(
                    item, str(request.horizon), request.as_of
                ),
            )
            if forecast_rows:
                forecasts[instrument_id] = list(forecast_rows)
            feature = safe_read(
                f"features:{instrument_id}",
                lambda item=instrument_id: self.latest_feature_set(item),
            )
            if isinstance(feature, Mapping):
                features[instrument_id] = dict(feature)
                exchange = str(feature.get("exchange") or "")
                instruments.setdefault(
                    instrument_id,
                    {
                        "instrument_id": instrument_id,
                        "exchange": exchange or instrument_id.split(":", 1)[0],
                        "asset_class": str(feature.get("asset_class") or "equity"),
                        "sector": str(feature.get("sector") or "unknown"),
                        "trading_status": "active",
                    },
                )
            regime = safe_read(
                f"regime:{instrument_id}",
                lambda item=instrument_id: self.latest_regime(item),
            )
            if isinstance(regime, Mapping):
                regimes[instrument_id] = dict(regime)
                instruments.setdefault(
                    instrument_id,
                    {
                        "instrument_id": instrument_id,
                        "exchange": instrument_id.split(":", 1)[0],
                        "asset_class": "equity",
                        "sector": str(regime.get("sector") or "unknown"),
                        "trading_status": "active",
                    },
                )
        benchmark_id = str(getattr(request, "benchmark_instrument_id", "") or "").strip()
        if benchmark_id:
            # The market benchmark is a read-only PI-02 series: it is never added
            # to the eligible universe and cannot receive an allocation.
            benchmark_page = safe_read(
                f"benchmark_bars:{benchmark_id}",
                lambda: self.market_bars(
                    benchmark_id, interval=interval, limit=320, ascending=False
                ),
            )
            if isinstance(benchmark_page, Mapping):
                benchmark_rows = list(
                    benchmark_page.get("items") or benchmark_page.get("bars") or []
                )
                benchmark_rows.reverse()
                context["benchmark"] = {"instrument_id": benchmark_id, "bars": benchmark_rows}
        for instrument_id in bars:
            # Bars alone are sufficient for price history; PI-04 metadata is
            # optional evidence and must not make an otherwise valid instrument
            # ineligible.
            instruments.setdefault(
                instrument_id,
                {
                    "instrument_id": instrument_id,
                    "exchange": instrument_id.split(":", 1)[0],
                    "asset_class": "equity",
                    "sector": "unknown",
                    "trading_status": "active",
                },
            )
        context["bars"] = bars
        context["forecasts"] = forecasts
        context["features"] = features
        context["regimes"] = regimes
        context["instruments"] = instruments
        context["source_notes"] = notes
        return context

    def optimization_status(self) -> dict[str, Any]:
        if self._optimization_service is None:
            return {
                "phase": "PI-18",
                "implemented": True,
                "initialised": False,
                "advisory_only": True,
                "production_authority": False,
                "order_submission_available": False,
                "risk_authority": "NM-08",
                "execution_authority": "NM-10",
                "last_error": self._optimization_error,
                "detail": "portfolio optimization opens on first use",
            }
        return cast(dict[str, Any], self._optimization_service.status())

    def optimization_contract(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_optimization().contract())

    def optimization_health(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_optimization().health())

    def optimization_create_config(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_optimization().create_config(payload))

    def optimization_configs(
        self, *, portfolio_id: str = "", limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_optimization().configs(
                portfolio_id=portfolio_id, limit=limit, offset=offset
            ),
        )

    def optimization_config(self, config_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_optimization().config(config_id))

    def optimization_validate(
        self, payload: Mapping[str, Any], *, as_of: datetime | None = None
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_optimization().validate_inputs(payload, as_of=as_of),
        )

    def optimization_run(
        self, payload: Mapping[str, Any], *, idempotency_key: str = ""
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_optimization().run(payload, idempotency_key=idempotency_key),
        )

    def optimization_runs(
        self,
        *,
        portfolio_id: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_optimization().runs(
                portfolio_id=portfolio_id, status=status, limit=limit, offset=offset
            ),
        )

    def optimization_run_record(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_optimization().run_record(run_id))

    def optimization_allocations(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_optimization().allocations(run_id))

    def optimization_diversification(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_optimization().diversification(run_id))

    def optimization_risk(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_optimization().risk(run_id))

    def optimization_liquidity(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_optimization().liquidity(run_id))

    def optimization_rebalancing(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_optimization().rebalancing(run_id))

    def optimization_scenarios(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_optimization().scenarios(run_id))

    def optimization_run_scenarios(self, run_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_optimization().run_scenarios(run_id, payload),
        )

    def optimization_backtest(self, run_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_optimization().backtest(run_id, payload))

    def optimization_backtests(
        self, *, run_id: str = "", limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_optimization().backtests(run_id=run_id, limit=limit, offset=offset),
        )

    def optimization_backtest_record(self, backtest_id: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._ensure_optimization().backtest_record(backtest_id),
        )

    def optimization_handoff(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_optimization().handoff(run_id))

    def optimization_cancel(self, run_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._ensure_optimization().cancel(run_id))

    def optimization_latest(self, portfolio_id: str, *, horizon: str = "") -> dict[str, Any] | None:
        return cast(
            "dict[str, Any] | None",
            self._ensure_optimization().latest(portfolio_id, horizon=horizon),
        )

    # -- PI-20: prediction intelligence dashboard ---------------------------

    def dashboard_service(self) -> Any:
        """Open the read-only dashboard composition over existing services."""
        if self._dashboard_service is not None:
            return self._dashboard_service
        from novacontrol.novamarket.dashboard import PredictionDashboardService

        try:
            self._dashboard_service = PredictionDashboardService(
                self, store_path=self._dashboard_path(), clock=self._clock
            )
        except Exception as exc:  # noqa: BLE001 - dashboard status explains it
            self._dashboard_error = f"{type(exc).__name__}: {exc}"
            raise
        self._dashboard_error = ""
        return self._dashboard_service

    def dashboard_status(self) -> dict[str, Any]:
        try:
            status = cast(dict[str, Any], self.dashboard_service().status())
        except Exception as exc:  # noqa: BLE001 - status reports the failure
            return {
                "ok": False,
                "phase": "PI-20",
                "implemented": True,
                "initialised": False,
                "read_only": True,
                "error": self._dashboard_error or f"{type(exc).__name__}: {exc}",
            }
        status["error"] = self._dashboard_error
        return status

    def dashboard_contract(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.dashboard_service().contract())

    def dashboard_navigation(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.dashboard_service().navigation())

    def dashboard_overview(self, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return cast(dict[str, Any], self.dashboard_service().overview(filters))

    def dashboard_instrument(self, instrument_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.dashboard_service().instrument(instrument_id))

    def dashboard_forecasts(self, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return cast(dict[str, Any], self.dashboard_service().forecasts(filters))

    def dashboard_compare(self, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return cast(dict[str, Any], self.dashboard_service().compare(filters))

    def dashboard_horizons(self, instrument_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.dashboard_service().horizons(instrument_id))

    def dashboard_lifecycle(
        self, *, cycle_id: str = "", instrument_id: str = "", limit: int = 20
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.dashboard_service().lifecycle(
                cycle_id=cycle_id, instrument_id=instrument_id, limit=limit
            ),
        )

    def dashboard_performance(
        self,
        *,
        evaluation_id: str = "",
        instrument_id: str = "",
        horizon: str = "",
        evaluation_start: str = "",
        evaluation_end: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.dashboard_service().performance(
                evaluation_id=evaluation_id,
                instrument_id=instrument_id,
                horizon=horizon,
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
                limit=limit,
            ),
        )

    def dashboard_regimes(self, *, instrument_id: str = "", limit: int = 100) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.dashboard_service().regimes(instrument_id=instrument_id, limit=limit),
        )

    def dashboard_decisions(
        self,
        *,
        instrument_id: str = "",
        status: str = "",
        agent_id: str = "",
        limit: int = 30,
        offset: int = 0,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.dashboard_service().decisions(
                instrument_id=instrument_id,
                status=status,
                agent_id=agent_id,
                limit=limit,
                offset=offset,
            ),
        )

    def dashboard_decision(self, cycle_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.dashboard_service().decision(cycle_id))

    def dashboard_explain(self, subject_id: str, *, kind: str = "") -> dict[str, Any]:
        return cast(dict[str, Any], self.dashboard_service().explain(subject_id, kind=kind))

    def dashboard_attribution(self, forecast_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.dashboard_service().attribution(forecast_id))

    def dashboard_evidence(self, subject_id: str, *, kind: str = "") -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.dashboard_service().evidence(subject_id, kind=kind),
        )

    def dashboard_research(
        self,
        *,
        instrument_id: str = "",
        run_id: str = "",
        research_status: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.dashboard_service().research(
                instrument_id=instrument_id,
                run_id=run_id,
                research_status=research_status,
                limit=limit,
            ),
        )

    def dashboard_portfolio(self, *, portfolio_id: str = "", limit: int = 20) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.dashboard_service().portfolio(portfolio_id=portfolio_id, limit=limit),
        )

    def dashboard_policies(self, *, policy_id: str = "", limit: int = 30) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.dashboard_service().policies(policy_id=policy_id, limit=limit),
        )

    def dashboard_replay(self, cycle_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.dashboard_service().replay(cycle_id))

    def dashboard_filters(self, *, name: str = "default") -> dict[str, Any]:
        return cast(dict[str, Any], self.dashboard_service().filters(name=name))

    def dashboard_save_filters(
        self, payload: Mapping[str, Any], *, name: str = "default"
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.dashboard_service().save_filters(payload, name=name),
        )

    # -- PI-21: advanced autonomous portfolio management ---------------------

    def portfolio_management_service(self) -> Any:
        """Open the PI-21 advisory coordinator over existing authorities."""
        if self._portfolio_management_service is not None:
            return self._portfolio_management_service
        from novacontrol.novamarket.portfolio_management import (
            ManagementSources,
            PortfolioManagementService,
        )

        try:
            self._portfolio_management_service = PortfolioManagementService(
                self._portfolio_management_sources(ManagementSources),
                store_path=self._portfolio_management_path(),
                clock=self._clock,
            )
        except Exception as exc:  # noqa: BLE001 - status explains the failure
            self._portfolio_management_error = f"{type(exc).__name__}: {exc}"
            raise
        self._portfolio_management_error = ""
        return self._portfolio_management_service

    def _portfolio_management_sources(self, source_type: Any) -> Any:
        """The complete PI-21 evidence surface, mapped onto existing services."""

        return source_type(
            portfolio_list=self._pm_portfolio_list,
            portfolio_detail=self._pm_portfolio_detail,
            pending_orders=self._pm_pending_orders,
            reservations=self._pm_reservations,
            reserved_cash=self._pm_reserved_cash,
            fills=self._pm_fills,
            risk_portfolio=self._pm_risk_portfolio,
            exit_configurations=self._pm_exit_configurations,
            forecast_views=self._pm_forecast_views,
            instrument_evidence=self._pm_instrument_evidence,
            agent_decisions=self._pm_agent_decisions,
            optimization_latest=self._pm_optimization_latest,
            training_policies=self._pm_training_policies,
            at03_proposals=self._pm_at03_proposals,
            runtime_status=self._pm_runtime_status,
            execute_proposal=self._pm_execute_proposal,
            reconcile_execution=self._pm_reconcile_execution,
            recover_autonomy=self._pm_recover_autonomy,
            valuation_history=self._pm_valuation_history,
        )

    def _pm_portfolio_list(self, limit: int) -> dict[str, Any]:
        return self.portfolios_list(limit=max(1, min(int(limit), 100)))

    def _pm_portfolio_detail(self, portfolio_id: str) -> dict[str, Any]:
        return self.portfolio_detail(portfolio_id)

    def _pm_pending_orders(self, portfolio_id: str) -> dict[str, Any]:
        return self.paper_orders(portfolio_id=portfolio_id, limit=200)

    def _pm_reservations(self, portfolio_id: str) -> dict[str, Any]:
        store = self._execution_service().store
        rows = [row.to_dict() for row in store.reservations(portfolio_id=portfolio_id)]
        return {"ok": True, "reservations": rows, "total": len(rows)}

    def _pm_reserved_cash(self, portfolio_id: str) -> str:
        return str(self._execution_service().store.reserved_total(portfolio_id, "cash"))

    def _pm_fills(self, portfolio_id: str) -> dict[str, Any]:
        del portfolio_id
        store = self._execution_service().store
        rows = [row.to_dict() for row in store.fills()]
        return {"ok": True, "fills": rows[:500], "total": len(rows)}

    def _pm_risk_portfolio(self, portfolio_id: str) -> dict[str, Any]:
        return self.risk_portfolio(portfolio_id)

    def _pm_exit_configurations(self, limit: int) -> list[dict[str, Any]]:
        body = self.autonomy_exit_configurations(limit=max(1, min(int(limit), 500)))
        return [dict(row) for row in body.get("configurations") or () if isinstance(row, Mapping)]

    def _pm_forecast_views(self, instrument_id: str | None, horizon: str) -> dict[str, Any]:
        dashboard_horizon = "1h" if horizon == "60m" else horizon
        return self.dashboard_forecasts(
            {
                "instrument_id": instrument_id or "",
                "horizon": dashboard_horizon,
                "limit": 200,
            }
        )

    def _pm_instrument_evidence(self, instrument_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {"instrument": {"instrument_id": instrument_id}}
        engine = self._market_data_engine()
        try:
            metadata = engine.get_instrument(instrument_id).to_dict()
        except Exception:  # noqa: BLE001 - the evidence reader reports missing metadata
            metadata = {}
        instrument: dict[str, Any] = {"instrument_id": instrument_id}
        for key in ("symbol", "name", "exchange", "mic", "asset_class", "currency"):
            if metadata.get(key) not in (None, ""):
                instrument[key] = metadata[key]
        instrument["sector"] = (
            (metadata.get("metadata") or {}).get("sector")
            if isinstance(metadata.get("metadata"), Mapping)
            else ""
        )
        try:
            reconciled = engine.latest(instrument_id).to_dict()
        except Exception as exc:  # noqa: BLE001 - missing price is reported, not zero
            reconciled = {"instrument_id": instrument_id, "error": str(exc)}
        selected = reconciled.get("selected") or {}
        if isinstance(selected, Mapping):
            instrument["price"] = selected.get("price")
            instrument["price_at"] = selected.get("observed_at")
            instrument["price_freshness"] = selected.get("freshness")
            instrument["price_quality"] = selected.get("quality")
        body["instrument"] = instrument
        body["quote"] = reconciled
        try:
            body["regime"] = self.latest_regime(instrument_id) or {}
        except Exception as exc:  # noqa: BLE001
            body["regime"] = {"status": "unavailable", "error": str(exc)}
        try:
            body["research"] = self.research_latest(instrument_id) or {}
        except Exception as exc:  # noqa: BLE001
            body["research"] = {"status": "unavailable", "error": str(exc)}
        try:
            bars = self.market_bars(instrument_id, interval="1d", limit=60)
            body["chart"] = {"rows": bars.get("bars") or bars.get("items") or []}
        except Exception as exc:  # noqa: BLE001
            body["chart"] = {"rows": [], "error": str(exc)}
        try:
            body["events"] = self.radar_instrument_events(instrument_id, limit=20)
        except Exception as exc:  # noqa: BLE001
            body["events"] = {"events": [], "error": str(exc)}
        chart = dict(body["chart"]) if isinstance(body["chart"], Mapping) else {}
        event_body: dict[str, Any] = (
            dict(body["events"]) if isinstance(body.get("events"), Mapping) else {}
        )
        chart["news_events"] = event_body.get("events") or event_body.get("items") or []
        body["chart"] = chart
        return body

    def _pm_agent_decisions(self, instrument_id: str, limit: int) -> dict[str, Any]:
        return self.multiagent_history(
            instrument_id=instrument_id, limit=max(1, min(int(limit), 100))
        )

    def _pm_optimization_latest(self, portfolio_id: str, horizon: str) -> dict[str, Any] | None:
        return self.optimization_latest(portfolio_id, horizon=horizon)

    def _pm_training_policies(self, limit: int) -> dict[str, Any]:
        return self.training_policies(max(1, min(int(limit), 100)))

    def _pm_at03_proposals(self, portfolio_id: str) -> list[dict[str, Any]]:
        body = self.autonomy_decision_proposals(
            portfolio_id=portfolio_id, status="risk_approved", limit=100
        )
        enriched: list[dict[str, Any]] = []
        for row in body.get("proposals") or ():
            if not isinstance(row, Mapping):
                continue
            proposal = dict(row)
            try:
                detail = self.autonomy_decision_proposal(str(proposal.get("proposal_id") or ""))
            except Exception:  # noqa: BLE001 - proposal remains as stored
                detail = {}
            risk = detail.get("risk") if isinstance(detail, Mapping) else None
            if isinstance(risk, Mapping):
                proposal["risk_outcome"] = risk.get("outcome")
                proposal["risk_decision_id"] = risk.get("risk_decision_id")
                proposal["restrictions"] = risk.get("restrictions") or []
            enriched.append(proposal)
        return enriched

    def _pm_runtime_status(self) -> dict[str, Any]:
        return self.autonomy_status()

    async def _pm_execute_proposal(self, proposal_id: str, actor: str) -> dict[str, Any]:
        return await self.autonomy_execution_run(
            trigger="pi21_portfolio_handoff", actor=actor, proposal_id=proposal_id
        )

    async def _pm_reconcile_execution(self, execution_id: str) -> dict[str, Any]:
        return await self.autonomy_execution_reconcile(execution_id)

    async def _pm_recover_autonomy(self, reason: str) -> dict[str, Any]:
        return await self.autonomy_recover(reason=reason)

    def _pm_valuation_history(self, portfolio_id: str, limit: int) -> dict[str, Any]:
        return self.portfolio_valuations(portfolio_id, limit=max(1, min(int(limit), 500)))

    def portfolio_management_status(
        self,
        *,
        operator_authorized: bool = False,
        operator_configured: bool | None = None,
    ) -> dict[str, Any]:
        try:
            status = cast(
                dict[str, Any],
                self.portfolio_management_service().status(
                    operator_authorized=operator_authorized,
                    operator_configured=operator_configured,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - status reports the failure
            return {
                "ok": False,
                "phase": "PI-21",
                "implemented": True,
                "initialised": False,
                "advisory_only": True,
                "error": self._portfolio_management_error or f"{type(exc).__name__}: {exc}",
                "direct_financial_mutation": False,
                "direct_order_submission": False,
            }
        status["error"] = self._portfolio_management_error
        status["scheduler_error"] = self._portfolio_management_scheduler_error
        return status

    def portfolio_management_contract(self) -> dict[str, Any]:
        return cast(
            dict[str, Any], self.portfolio_management_service().contract()
        )

    def portfolio_management_navigation(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.portfolio_management_service().navigation())

    def portfolio_management_config(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.portfolio_management_service().config())

    def portfolio_management_update_config(
        self, payload: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.portfolio_management_service().update_config(payload, actor=actor),
        )

    async def portfolio_management_run(
        self,
        *,
        portfolio_id: str = "",
        horizon: str = "",
        trigger: str = "manual",
        request_id: str = "",
        actor: str = "operator",
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.portfolio_management_service().run_cycle(
                portfolio_id=portfolio_id,
                horizon=horizon,
                trigger=trigger,
                request_id=request_id,
                actor=actor,
            ),
        )

    async def portfolio_management_handoff(
        self, cycle_id: str, *, actor: str = "operator", max_changes: int = 10
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.portfolio_management_service().handoff_cycle(
                cycle_id, actor=actor, max_changes=max_changes
            ),
        )

    async def portfolio_management_reconcile(
        self, cycle_id: str, *, actor: str = "operator"
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.portfolio_management_service().reconcile_cycle(cycle_id, actor=actor),
        )

    async def portfolio_management_recover(
        self, *, reason: str = "PI-21 recovery"
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.portfolio_management_service().recover(reason=reason),
        )

    def portfolio_management_cycles(
        self,
        *,
        portfolio_id: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.portfolio_management_service().cycles(
                portfolio_id=portfolio_id,
                status=status,
                limit=limit,
                offset=offset,
            ),
        )

    def portfolio_management_cycle(self, cycle_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.portfolio_management_service().cycle(cycle_id))

    def portfolio_management_snapshot(self, portfolio_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.portfolio_management_service().snapshot(portfolio_id))

    def portfolio_management_positions(self, portfolio_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.portfolio_management_service().positions(portfolio_id))

    def portfolio_management_opportunities(self, portfolio_id: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.portfolio_management_service().opportunities(portfolio_id),
        )

    def portfolio_management_decisions(
        self, portfolio_id: str, *, limit: int = 100
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.portfolio_management_service().decisions(portfolio_id, limit=limit),
        )

    def portfolio_management_capital(self, portfolio_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.portfolio_management_service().capital(portfolio_id))

    def portfolio_management_cash(self, portfolio_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.portfolio_management_service().cash(portfolio_id))

    def portfolio_management_exposure(self, portfolio_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.portfolio_management_service().exposure(portfolio_id))

    def portfolio_management_rebalance(self, portfolio_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.portfolio_management_service().rebalance(portfolio_id))

    def portfolio_management_analytics(self, portfolio_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.portfolio_management_service().analytics(portfolio_id))

    def portfolio_management_events(self, cycle_id: str, *, limit: int = 200) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.portfolio_management_service().events(cycle_id, limit=limit),
        )

    # -- PI-22: advanced risk intelligence and portfolio protection ----------

    def risk_intelligence_service(self) -> Any:
        """Open the PI-22 advisory layer over existing financial authorities."""
        if self._risk_intelligence_service is not None:
            return self._risk_intelligence_service
        from novacontrol.novamarket.risk_intelligence import (
            RiskIntelligenceService,
            RiskIntelligenceSources,
        )

        try:
            self._risk_intelligence_service = RiskIntelligenceService(
                self._risk_intelligence_sources(RiskIntelligenceSources),
                store_path=self._risk_intelligence_path(),
                clock=self._clock,
                event_loader=self._event_intelligence_risk_context,
            )
        except Exception as exc:  # noqa: BLE001 - status explains the failure
            self._risk_intelligence_error = f"{type(exc).__name__}: {exc}"
            raise
        self._risk_intelligence_error = ""
        return self._risk_intelligence_service

    def _risk_intelligence_sources(self, source_type: Any) -> Any:
        return source_type(
            portfolio_list=self._pm_portfolio_list,
            portfolio_detail=self._pm_portfolio_detail,
            valuation_history=self._ri_valuation_history,
            bars=self._ri_bars,
            forecast_views=self._pm_forecast_views,
            instrument_evidence=self._pm_instrument_evidence,
            agent_decisions=self._pm_agent_decisions,
            optimization_latest=self._pm_optimization_latest,
            risk_portfolio=self._pm_risk_portfolio,
            risk_position=self.risk_position,
            runtime_status=self._pm_runtime_status,
            pending_orders=self._pm_pending_orders,
            reserved_cash=self._pm_reserved_cash,
            management_snapshot=self._pm_portfolio_detail,
        )

    def _ri_bars(self, instrument_id: str, limit: int) -> list[dict[str, Any]]:
        body = self.market_bars(
            instrument_id,
            interval="1d",
            limit=max(1, min(int(limit), 1000)),
            ascending=True,
        )
        rows = body.get("items") or body.get("bars") or []
        return [dict(row) for row in rows if isinstance(row, Mapping)]

    def _ri_valuation_history(self, portfolio_id: str, limit: int) -> dict[str, Any]:
        """Read the most recent valuations, not the oldest slice.

        PI-22 drawdown must reflect the current book even when a portfolio has
        accumulated more than the read limit. The authoritative NM-07 method
        remains the source; this callback only selects the latest window.
        """
        return self.portfolio_valuations(
            portfolio_id, limit=max(1, min(int(limit), 500)), latest=True
        )

    def risk_intelligence_status(
        self,
        *,
        operator_authorized: bool = False,
        operator_configured: bool | None = None,
    ) -> dict[str, Any]:
        del operator_configured
        try:
            status = cast(
                dict[str, Any],
                self.risk_intelligence_service().status(
                    operator_authorized=operator_authorized
                ),
            )
        except Exception as exc:  # noqa: BLE001 - status reports the failure
            return {
                "ok": False,
                "phase": "PI-22",
                "implemented": True,
                "initialised": False,
                "advisory_only": True,
                "error": self._risk_intelligence_error or f"{type(exc).__name__}: {exc}",
                "direct_financial_mutation": False,
                "direct_order_submission": False,
            }
        status["error"] = self._risk_intelligence_error
        return status

    def risk_intelligence_contract(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.risk_intelligence_service().contract())

    def risk_intelligence_navigation(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.risk_intelligence_service().navigation())

    def risk_intelligence_config(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.risk_intelligence_service().config())

    def risk_intelligence_update_config(
        self, payload: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.risk_intelligence_service().update_config(payload, actor=actor),
        )

    async def risk_intelligence_analyze(
        self,
        *,
        portfolio_id: str = "",
        horizon: str = "5d",
        trigger: str = "manual",
        request_id: str = "",
        actor: str = "operator",
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.risk_intelligence_service().analyze(
                portfolio_id,
                horizon=horizon,
                trigger=trigger,
                request_id=request_id,
                actor=actor,
            ),
        )

    def risk_intelligence_overview(self, portfolio_id: str) -> dict[str, Any]:
        return cast(
            dict[str, Any], self.risk_intelligence_service().overview(portfolio_id)
        )

    def risk_intelligence_section(self, portfolio_id: str, section: str) -> dict[str, Any]:
        body = self.risk_intelligence_overview(portfolio_id)
        if section == "positions":
            rows = body.get("positions") or []
            return {
                "ok": body.get("ok", False),
                "portfolio_id": portfolio_id,
                "assessment_id": body.get("assessment_id"),
                "positions": rows,
                "total": len(rows),
                "advisory_only": True,
            }
        value = body.get(section)
        return {
            "ok": body.get("ok", False),
            "portfolio_id": portfolio_id,
            "assessment_id": body.get("assessment_id"),
            section: value if value is not None else {},
            "advisory_only": True,
        }

    def risk_intelligence_history(
        self, portfolio_id: str = "", *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.risk_intelligence_service().history(
                portfolio_id, limit=limit, offset=offset
            ),
        )

    def risk_intelligence_trend(self, portfolio_id: str) -> dict[str, Any]:
        return cast(
            dict[str, Any], self.risk_intelligence_service().trend(portfolio_id)
        )

    def risk_intelligence_alerts(
        self, *, portfolio_id: str = "", status: str = "", limit: int = 100
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.risk_intelligence_service().alerts(
                portfolio_id=portfolio_id, status=status, limit=limit
            ),
        )

    async def risk_intelligence_update_alert(
        self, alert_id: str, *, action: str, actor: str = ""
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.risk_intelligence_service().update_alert(
                alert_id, action=action, actor=actor
            ),
        )

    def risk_intelligence_recommendations(
        self, *, portfolio_id: str = "", limit: int = 100
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.risk_intelligence_service().recommendations(
                portfolio_id=portfolio_id, limit=limit
            ),
        )

    def risk_intelligence_scenarios(
        self, portfolio_id: str = "", *, limit: int = 100
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.risk_intelligence_service().scenarios(portfolio_id, limit=limit),
        )

    def risk_intelligence_create_scenario(
        self, payload: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.risk_intelligence_service().create_scenario(payload, actor=actor),
        )

    async def risk_intelligence_run_scenario(
        self,
        portfolio_id: str,
        *,
        scenario_id: str = "",
        config_payload: Mapping[str, Any] | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.risk_intelligence_service().run_scenario(
                portfolio_id,
                scenario_id=scenario_id,
                config_payload=config_payload,
                actor=actor,
            ),
        )

    def risk_intelligence_scenario_runs(
        self, portfolio_id: str = "", *, limit: int = 50
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.risk_intelligence_service().scenario_runs(portfolio_id, limit=limit),
        )

    def risk_intelligence_stress_results(
        self, portfolio_id: str = "", *, limit: int = 50
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.risk_intelligence_service().stress_results(portfolio_id, limit=limit),
        )

    async def risk_intelligence_run_stress(
        self,
        portfolio_id: str,
        *,
        mode: str = "hypothetical",
        scenario_id: str = "",
        shock_pct: float | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.risk_intelligence_service().run_stress(
                portfolio_id,
                mode=mode,
                scenario_id=scenario_id,
                shock_pct=shock_pct,
                actor=actor,
            ),
        )

    async def risk_intelligence_recover(self, *, reason: str = "") -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.risk_intelligence_service().recover(
                reason=reason or "PI-22 operator recovery"
            ),
        )

    # -- PI-23: predictive intelligence and forecast optimization ------------

    def forecast_intelligence_service(self) -> Any:
        """Open the PI-23 advisory layer over existing forecast infrastructure."""
        if self._forecast_intelligence_service is not None:
            return self._forecast_intelligence_service
        from novacontrol.novamarket.forecast_intelligence import (
            ForecastIntelligenceService,
            ForecastIntelligenceSources,
        )

        try:
            self._forecast_intelligence_service = ForecastIntelligenceService(
                self._forecast_intelligence_sources(ForecastIntelligenceSources),
                store_path=self._forecast_intelligence_path(),
                clock=self._clock,
            )
        except Exception as exc:  # noqa: BLE001 - status reports the failure
            self._forecast_intelligence_error = f"{type(exc).__name__}: {exc}"
            raise
        self._forecast_intelligence_error = ""
        return self._forecast_intelligence_service

    def _forecast_intelligence_sources(self, source_type: Any) -> Any:
        return source_type(
            models=self._fi_models,
            training_runs=self._fi_training_runs,
            performance=self._fi_performance,
            forecasts=self._fi_forecasts,
            calibration=self._fi_calibration,
            evaluations=self._fi_evaluations,
            evaluation=self._fi_evaluation,
            regime=self._fi_regime,
            ensemble_models=self._fi_ensemble_models,
            ensemble_calibration=self._fi_ensemble_calibration,
            health=self._fi_health,
        )

    def _fi_models(
        self, instrument_id: str | None = None, horizon: str | None = None
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            body = self._forecast_service().models(
                instrument_id=instrument_id, horizon=horizon
            )
            rows.extend(
                dict(row) for row in body.get("models", ()) if isinstance(row, Mapping)
            )
        except Exception as exc:  # noqa: BLE001 - PI-01 registry may still be readable
            _LOGGER.debug("PI-23 skipped the NM-06 model registry: %s", exc)
        try:
            for item in self.prediction_service().store.models(horizon):
                rows.append(item.to_dict())
        except Exception:  # noqa: BLE001 - PI-01 registry is optional evidence
            pass
        return rows

    def _fi_ensemble_models(
        self, instrument_id: str | None = None, horizon: str | None = None
    ) -> list[dict[str, Any]]:
        try:
            body = self.ensemble_service().models(
                instrument_id=instrument_id, horizon=horizon
            )
            return [dict(row) for row in body.get("models", ()) if isinstance(row, Mapping)]
        except Exception:  # noqa: BLE001 - absent PI-08 is explicit
            return []

    def _fi_training_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        body = self._forecast_service().training_runs(limit=limit)
        return [dict(row) for row in body.get("runs", ()) if isinstance(row, Mapping)]

    def _fi_performance(
        self,
        *,
        model_id: str | None = None,
        instrument_id: str | None = None,
        horizon: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self._forecast_service()
        if self._forecast_store is None:
            return []
        return self._forecast_store.performance(
            model_id=model_id,
            instrument_id=instrument_id,
            horizon=horizon,
            limit=limit,
        )

    def _fi_forecasts(
        self,
        *,
        instrument_id: str | None = None,
        horizon: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            self._forecast_service()
        except Exception as exc:  # noqa: BLE001 - PI-01 contracts are independent
            _LOGGER.debug("PI-23 skipped the NM-06 forecast store: %s", exc)
        if self._forecast_store is not None:
            rows.extend(
                self._forecast_store.forecasts(
                    instrument_id=instrument_id, horizon=horizon, limit=limit
                )
            )
        try:
            rows.extend(
                item.to_dict()
                for item in self.prediction_service().store.list_forecasts(
                    instrument_id=instrument_id, horizon=horizon, limit=limit
                )
            )
        except Exception as exc:  # noqa: BLE001 - PI-01 contracts are optional
            _LOGGER.debug("PI-23 skipped PI-01 forecast contracts: %s", exc)
        return rows

    def _fi_calibration(
        self, model_id: str, horizon: str, *, instrument_id: str = ""
    ) -> dict[str, Any]:
        if model_id == "ensemble" and instrument_id:
            return self._fi_ensemble_calibration(instrument_id, horizon)
        engine = ""
        if model_id in {"xgboost", "lightgbm"}:
            engine = model_id
        elif model_id == "lstm":
            engine = "lstm"
        elif model_id in {
            "naive",
            "random_walk",
            "moving_average",
            "exponential_smoothing",
            "arima",
        }:
            engine = "statistical"
        return self.forecast_calibration(
            model_id, horizon, engine=engine, instrument_id=instrument_id
        )

    def _fi_ensemble_calibration(self, instrument_id: str, horizon: str) -> dict[str, Any]:
        try:
            return self.ensemble_service().calibration(instrument_id, horizon)
        except Exception:  # noqa: BLE001
            return {"status": "unavailable"}

    def _fi_evaluations(self, *, limit: int = 8) -> list[dict[str, Any]]:
        body = self.evaluation_service().list(limit=limit)
        return [dict(row) for row in body.get("evaluations", ()) if isinstance(row, Mapping)]

    def _fi_evaluation(self, evaluation_id: str) -> dict[str, Any]:
        return self.evaluation_service().get(evaluation_id)

    def _fi_regime(self, instrument_id: str) -> dict[str, Any]:
        body = self.latest_regime(instrument_id)
        return dict(body or {})

    def _fi_health(self) -> dict[str, Any]:
        return self.forecast_status()

    def forecast_intelligence_status(self) -> dict[str, Any]:
        try:
            body = dict(self.forecast_intelligence_service().status())
        except Exception as exc:  # noqa: BLE001 - a read must not crash status
            return {
                "ok": True,
                "phase": "PI-23",
                "implemented": True,
                "initialised": False,
                "error": self._forecast_intelligence_error or f"{type(exc).__name__}: {exc}",
                "authority": {
                    "risk_authority": "NM-08",
                    "execution_authority": "NM-10",
                    "automatic_promotion": False,
                    "production_weight_mutation": False,
                    "submits_orders": False,
                },
            }
        body["initialised"] = True
        body["error"] = self._forecast_intelligence_error
        return body

    def forecast_intelligence_contract(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.forecast_intelligence_service().contract())

    def forecast_intelligence_navigation(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.forecast_intelligence_service().navigation())

    def forecast_intelligence_config(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.forecast_intelligence_service().config_view())

    def forecast_intelligence_update_config(
        self, payload: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.forecast_intelligence_service().update_config(payload, actor=actor),
        )

    def forecast_intelligence_evaluate(
        self, payload: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.forecast_intelligence_service().evaluate(payload, actor=actor),
        )

    def forecast_intelligence_select(
        self, payload: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.forecast_intelligence_service().select(payload, actor=actor),
        )

    def forecast_intelligence_calibrate(
        self, payload: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.forecast_intelligence_service().calibrate(payload, actor=actor),
        )

    def forecast_intelligence_uncertainty(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any], self.forecast_intelligence_service().uncertainty(payload)
        )

    def forecast_intelligence_disagreement(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any], self.forecast_intelligence_service().disagreement(payload)
        )

    def forecast_intelligence_consistency(self, instrument_id: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.forecast_intelligence_service().consistency(instrument_id),
        )

    def forecast_intelligence_drift(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self.forecast_intelligence_service().drift(payload))

    def forecast_intelligence_degradation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any], self.forecast_intelligence_service().degradation(payload)
        )

    def forecast_intelligence_weights(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self.forecast_intelligence_service().weights(payload))

    def forecast_intelligence_reliability(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any], self.forecast_intelligence_service().reliability(payload)
        )

    def forecast_intelligence_fallback(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self.forecast_intelligence_service().fallback(payload))

    def forecast_intelligence_analyze(
        self, payload: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        body = payload
        if actor:
            body = {**dict(payload), "actor": actor}
        return cast(
            dict[str, Any], self.forecast_intelligence_service().analyze(body)
        )

    def forecast_intelligence_overview(
        self, *, instrument_id: str = "", horizon: str = "", limit: int = 100
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.forecast_intelligence_service().performance_overview(
                instrument_id=instrument_id, horizon=horizon, limit=limit
            ),
        )

    def forecast_intelligence_comparison(
        self, instrument_id: str, horizon: str, task: str = "return_regression"
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.forecast_intelligence_service().comparison(instrument_id, horizon, task),
        )

    def forecast_intelligence_horizons(
        self, instrument_id: str, task: str = "return_regression"
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.forecast_intelligence_service().horizons(instrument_id, task),
        )

    def forecast_intelligence_instruments(
        self, instrument_ids: Sequence[str], horizon: str, task: str = "return_regression"
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.forecast_intelligence_service().instruments(
                instrument_ids, horizon, task
            ),
        )

    def forecast_intelligence_regimes(self, instrument_id: str, horizon: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.forecast_intelligence_service().regimes(instrument_id, horizon),
        )

    def forecast_intelligence_registry(
        self, *, instrument_id: str = "", horizon: str = ""
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.forecast_intelligence_service().registry(instrument_id, horizon),
        )

    def forecast_intelligence_history(
        self,
        *,
        instrument_id: str = "",
        horizon: str = "",
        kind: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.forecast_intelligence_service().history(
                instrument_id=instrument_id, horizon=horizon, kind=kind, limit=limit
            ),
        )

    def forecast_intelligence_health(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.forecast_intelligence_service().health())

    # -- PI-24: event-aware predictive intelligence ---------------------------

    def event_intelligence_service(self) -> Any:
        """Open the PI-24 advisory layer over NM-04 and PI-19 evidence."""
        if self._event_intelligence_service is not None:
            return self._event_intelligence_service
        from novacontrol.novamarket.event_intelligence import (
            EventIntelligenceService,
            EventIntelligenceSources,
        )

        try:
            self._event_intelligence_service = EventIntelligenceService(
                self._event_intelligence_sources(EventIntelligenceSources),
                store_path=self._event_intelligence_path(),
                clock=self._clock,
            )
        except Exception as exc:  # noqa: BLE001 - status reports the failure
            self._event_intelligence_error = f"{type(exc).__name__}: {exc}"
            raise
        self._event_intelligence_error = ""
        return self._event_intelligence_service

    def _event_intelligence_sources(self, source_type: Any) -> Any:
        return source_type(
            events=self._ei_events,
            event=self._ei_event,
            event_history=self._ei_event_history,
            instrument=self._ei_instrument,
            regime=self._fi_regime,
            bars=self._ei_bars,
            forecasts=self._fi_forecasts,
            evaluations=self._fi_evaluations,
            evaluation_records=self._ei_evaluation_records,
            portfolio=self._ei_portfolio,
            agents=self._ei_agents,
        )

    def _ei_events(
        self, *, instrument_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        body = self.radar_search(
            instrument_id=instrument_id or "", limit=max(1, min(int(limit), 200))
        )
        return [
            dict(row) for row in body.get("events", ()) if isinstance(row, Mapping)
        ]

    def _ei_event(self, event_id: str, version: int | None = None) -> dict[str, Any]:
        return self.radar_event(event_id, version=version)

    def _ei_event_history(self, event_id: str) -> dict[str, Any]:
        return self.radar_history(event_id)

    def _ei_evaluation_records(
        self, *, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """PI-11 collected records for a configuration, read-only.

        The event-intelligence layer re-scores these records with PI-11's own
        metric functions; nothing here writes an evaluation report.
        """

        return self.evaluation_service().collect_records(payload or {})

    def _ei_instrument(self, instrument_id: str) -> dict[str, Any]:
        for item in self._radar_instruments():
            if item.instrument_id != instrument_id:
                continue
            return {
                "instrument_id": item.instrument_id,
                "symbol": item.symbol,
                "name": item.name,
                "asset_class": item.asset_class,
                "exchange": item.exchange,
                "market": item.market,
                "sectors": list(item.sectors),
                "aliases": list(item.aliases),
            }
        return {}

    def _ei_bars(
        self,
        *,
        instrument_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        resolution: str = "1d",
        limit: int = 100,
        ascending: bool = True,
    ) -> list[dict[str, Any]]:
        body = self.market_bars(
            instrument_id,
            interval=resolution,
            start=start,
            end=end,
            limit=max(1, min(int(limit), 500)),
            ascending=ascending,
        )
        return [dict(row) for row in body.get("items", ()) if isinstance(row, Mapping)]

    def _ei_portfolio(self, portfolio_id: str) -> dict[str, Any]:
        return self.portfolio_detail(portfolio_id)

    def _ei_agents(
        self, *, instrument_id: str, horizon: str, portfolio_id: str = ""
    ) -> Mapping[str, Any]:
        return self.event_intelligence_agent_context(
            instrument_id, horizon, portfolio_id=portfolio_id
        )

    def event_intelligence_status(self) -> dict[str, Any]:
        try:
            body = dict(self.event_intelligence_service().status())
        except Exception as exc:  # noqa: BLE001 - a read must not crash status
            return {
                "ok": True,
                "phase": "PI-24",
                "implemented": True,
                "initialised": False,
                "error": self._event_intelligence_error
                or f"{type(exc).__name__}: {exc}",
                "authority": {
                    "risk_authority": "NM-08",
                    "execution_authority": "NM-10",
                    "financial_mutation": False,
                    "submits_orders": False,
                    "changes_forecast": False,
                },
            }
        body["initialised"] = True
        body["error"] = self._event_intelligence_error
        return body

    def event_intelligence_contract(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.event_intelligence_service().contract())

    def event_intelligence_navigation(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.event_intelligence_service().navigation())

    def event_intelligence_config(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.event_intelligence_service().config_view())

    def event_intelligence_update_config(
        self, payload: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.event_intelligence_service().update_config(payload, actor=actor),
        )

    def event_intelligence_sync(
        self, *, instrument_id: str = "", limit: int | None = None
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.event_intelligence_service().sync(
                instrument_id=instrument_id, limit=limit
            ),
        )

    async def event_intelligence_ingest(
        self, source_id: str = ""
    ) -> dict[str, Any]:
        from novacontrol.novamarket.event_intelligence import (
            EventIntelligenceValidationError,
        )

        refresh = await self.radar_refresh(source_id or None)
        if not refresh.get("ok", True):
            raise EventIntelligenceValidationError(
                "the event radar refresh did not complete; no event intelligence was ingested"
            )
        body = {
            "ok": True,
            "phase": "PI-24",
            "refresh": refresh,
            "intelligence": self.event_intelligence_sync(),
            "new_feed_added": False,
            "sources": ["NM-04 event radar"],
            "financial_mutation": False,
        }
        return body

    def event_intelligence_events(
        self,
        *,
        instrument_id: str = "",
        status: str = "",
        group: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.event_intelligence_service().events(
                instrument_id=instrument_id,
                status=status,
                group=group,
                limit=limit,
                offset=offset,
            ),
        )

    def event_intelligence_event(
        self, event_id: str, *, version: int | None = None
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.event_intelligence_service().event(event_id, version=version),
        )

    def event_intelligence_relevance(
        self, payload: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.event_intelligence_service().assess_relevance(payload, actor=actor),
        )

    def event_intelligence_impact(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any], self.event_intelligence_service().impact(payload)
        )

    def event_intelligence_warnings(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any], self.event_intelligence_service().warnings(payload)
        )

    def event_intelligence_features(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any], self.event_intelligence_service().features(payload)
        )

    def event_intelligence_forecasts(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any], self.event_intelligence_service().forecasts(payload)
        )

    def event_intelligence_outcomes(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any], self.event_intelligence_service().outcomes(payload)
        )

    def event_intelligence_outcome_study(
        self, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any], self.event_intelligence_service().outcome_study(payload)
        )

    def event_intelligence_forecast_evaluation(
        self, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.event_intelligence_service().forecast_evaluation(payload),
        )

    def event_intelligence_timeline(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any], self.event_intelligence_service().timeline(payload)
        )

    def event_intelligence_agent_context(
        self, instrument_id: str, horizon: str, *, portfolio_id: str = ""
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.event_intelligence_service().agent_evidence(
                {
                    "instrument_id": instrument_id,
                    "horizon": horizon,
                    "portfolio_id": portfolio_id,
                }
            ),
        )

    def event_intelligence_portfolio_context(
        self, portfolio_id: str, *, horizon: str = "5d"
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.event_intelligence_service().portfolio_context(
                portfolio_id, horizon=horizon
            ),
        )

    def event_intelligence_analyze(
        self, payload: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any], self.event_intelligence_service().analyze(payload)
        )

    def event_intelligence_overview(
        self, *, instrument_id: str = "", horizon: str = "5d"
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.event_intelligence_service().overview(
                instrument_id=instrument_id, horizon=horizon
            ),
        )

    def event_intelligence_history(
        self,
        *,
        kind: str = "",
        event_id: str = "",
        instrument_id: str = "",
        horizon: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.event_intelligence_service().history(
                kind=kind,
                event_id=event_id,
                instrument_id=instrument_id,
                horizon=horizon,
                limit=limit,
                offset=offset,
            ),
        )

    def event_intelligence_health(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.event_intelligence_service().health())

    def _event_intelligence_risk_context(
        self, portfolio_id: str, horizon: str
    ) -> Mapping[str, Any]:
        """Read-only PI-24 evidence for PI-22; no risk decision is created."""
        return self.event_intelligence_portfolio_context(portfolio_id, horizon=horizon)

    # -- PI-11: forecast evaluation and forecast-assisted replay -------------

    def evaluation_service(self) -> "PredictionEvaluationService":
        """Open the read-only evaluation layer over existing forecast stores."""

        from novacontrol.novamarket.evaluation import PredictionEvaluationService

        if self._evaluation_service is not None:
            return cast("PredictionEvaluationService", self._evaluation_service)
        prediction_store = (
            self._prediction_service.store if self._prediction_service is not None else None
        )
        forecast_store = self._forecast_store
        if forecast_store is None and self._forecast_engine is not None:
            forecast_store = self._forecast_engine._store  # read-only adapter
        try:
            service = PredictionEvaluationService(
                self._evaluation_path(),
                prediction_store=prediction_store,
                forecast_store=forecast_store,
                market_data=self._market_engine,
                clock=self._clock,
            )
        except Exception as exc:
            self._evaluation_error = f"{type(exc).__name__}: {exc}"
            raise
        self._evaluation_service = service
        return service

    def evaluation_status(self) -> dict[str, Any]:
        if self._evaluation_service is not None:
            return cast(dict[str, Any], self._evaluation_service.status())
        # Read the persisted counts with a short-lived store. Opening the full
        # evaluation service here used to leave its SQLite file open on a
        # disabled market, because shutdown returns before closing lazy stores.
        from novacontrol.novamarket.evaluation import PredictionEvaluationService
        from novacontrol.novamarket.evaluation.models import (
            PI11_PHASE,
            REPORT_CONTRACT_VERSION,
        )
        from novacontrol.novamarket.evaluation.store import EvaluationStore

        evaluation_path = self._evaluation_path()
        try:
            if str(evaluation_path) == ":memory:" or not Path(evaluation_path).exists():
                # A status poll must not create the append-only report file.
                counts = {"total": 0, "completed": 0, "degraded": 0}
            else:
                counts_store = EvaluationStore(evaluation_path)
                try:
                    counts = counts_store.counts()
                finally:
                    counts_store.close()
        except Exception as exc:  # noqa: BLE001 - status must remain readable
            self._evaluation_error = f"{type(exc).__name__}: {exc}"
            return {
                "ok": True,
                "phase": "PI-11",
                "initialised": False,
                "last_error": self._evaluation_error,
                "detail": "forecast evaluation opens with the existing forecast stores",
                "authority": PredictionEvaluationService._authority(),
            }
        return {
            "ok": True,
            "phase": PI11_PHASE,
            "contract_version": REPORT_CONTRACT_VERSION,
            "initialised": True,
            "forecast_stores": {
                "lifecycle": self._prediction_service is not None,
                "nm06": self._forecast_store is not None or self._forecast_engine is not None,
                "target_bar_resolution": self._market_engine is not None,
            },
            "evaluations": counts,
            "last_error": self._evaluation_error,
            "authority": PredictionEvaluationService._authority(),
        }

    def evaluation_contract(self) -> dict[str, Any]:
        try:
            return self.evaluation_service().contract()
        except Exception as exc:  # noqa: BLE001 - contract remains readable
            return {
                "ok": False,
                "phase": "PI-11",
                "initialised": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def evaluation_create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        service = self.evaluation_service()
        raw_sources = payload.get("sources") or ("lifecycle", "nm06")
        if isinstance(raw_sources, str):
            sources = {item.strip().lower() for item in raw_sources.split(",") if item.strip()}
        else:
            sources = {str(item).strip().lower() for item in raw_sources}
        if "lifecycle" in sources and self._prediction_service is None:
            if self._lifecycle.state in (
                LifecycleState.READY,
                LifecycleState.RUNNING,
                LifecycleState.DEGRADED,
            ):
                try:
                    service.attach_stores(prediction_store=self.prediction_service().store)
                except Exception as exc:  # noqa: BLE001 - explicit records still work
                    self._evaluation_error = f"prediction store: {type(exc).__name__}: {exc}"
        if "nm06" in sources and self._forecast_store is None:
            if self._forecast_engine is not None:
                service.attach_stores(forecast_store=self._forecast_engine._store)
            elif self._lifecycle.state in (
                LifecycleState.READY,
                LifecycleState.RUNNING,
                LifecycleState.DEGRADED,
            ):
                try:
                    service.attach_stores(
                        forecast_store=self._forecast_service()._store,
                        market_data=self._market_engine,
                    )
                except Exception as exc:  # noqa: BLE001 - lifecycle source still works
                    self._evaluation_error = f"forecast store: {type(exc).__name__}: {exc}"
        return await asyncio.to_thread(service.evaluate, payload)

    def evaluation_list(self, limit: int = 50) -> dict[str, Any]:
        return self.evaluation_service().list(limit)

    def evaluation(self, evaluation_id: str) -> dict[str, Any]:
        return self.evaluation_service().get(evaluation_id)

    async def evaluation_verify(self, evaluation_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.evaluation_service().verify, evaluation_id)

    async def evaluation_reproduce(self, evaluation_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.evaluation_service().reproduce, evaluation_id)

    # -- NM-12: non-executing shadow mode ------------------------------------

    def _ensure_shadow(self) -> "ShadowService":
        from novacontrol.novamarket.execution.config import ExecutionConfig
        from novacontrol.novamarket.shadow import ShadowService

        if self._shadow_service is not None:
            return self._shadow_service
        try:
            service = ShadowService(
                self._shadow_path(),
                market_data=self._market_data_engine(),
                strategy_loader=self._shadow_strategy,
                portfolio_loader=self._shadow_portfolio,
                risk_policy_loader=self._backtest_risk_policy,
                backtest_loader=self._shadow_backtest,
                publisher=self._shadow_publish,
                clock=self._clock,
                execution_config=ExecutionConfig(),
            )
        except Exception as exc:
            self._shadow_error = f"{type(exc).__name__}: {exc}"
            raise
        self._shadow_service = service
        self._shadow_error = ""
        return service

    def _shadowing(self) -> "ShadowService":
        service = self._shadow_service
        if service is not None:
            return service
        if self._lifecycle.state not in (
            LifecycleState.READY,
            LifecycleState.RUNNING,
            LifecycleState.DEGRADED,
        ):
            raise NotOperationalError(
                "shadow mode is not available while NovaMarket is "
                f"{self._lifecycle.state.value}; enable NovaMarket first"
            )
        return self._ensure_shadow()

    def _shadow_strategy(self, strategy_id: str) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], self._strategy_service().strategy(strategy_id))

    def _shadow_portfolio(self, portfolio_id: str) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], self._portfolio_service().detail(portfolio_id))

    def _shadow_backtest(self, backtest_id: str) -> Mapping[str, Any]:
        service = self._backtesting()
        body = dict(service.results(backtest_id))
        body["trades"] = list(service.trades(backtest_id).get("trades") or ())
        config = dict(body.get("config") or {})
        body["instruments"] = list(config.get("instruments") or ())
        return cast(Mapping[str, Any], body)

    async def _shadow_publish(self, event_type: str, payload: dict[str, Any]) -> None:
        await self._publish(EventType(str(event_type)), **payload)

    def shadow_status(self) -> dict[str, Any]:
        if self._shadow_service is None:
            return {
                "ok": True,
                "phase": "NM-12",
                "initialised": False,
                "simulated": True,
                "real_money": False,
                "execution_connected": False,
                "last_error": self._shadow_error,
                "detail": "shadow mode opens when NovaMarket is operational",
            }
        return self._shadow_service.status()

    def shadow_contract(self) -> dict[str, Any]:
        if self._shadow_service is None:
            return {
                "ok": True,
                "phase": "NM-12",
                "implemented": False,
                "simulated": True,
                "real_money": False,
                "execution_connected": False,
                "detail": "shadow mode is not initialised in this run",
            }
        return self._shadow_service.contract()

    async def shadow_create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._shadowing().create(payload)

    async def shadow_start(self, session_id: str) -> dict[str, Any]:
        return await self._shadowing().start(session_id)

    async def shadow_pause(self, session_id: str) -> dict[str, Any]:
        return await self._shadowing().pause(session_id)

    async def shadow_resume(self, session_id: str) -> dict[str, Any]:
        return await self._shadowing().resume(session_id)

    async def shadow_stop(self, session_id: str) -> dict[str, Any]:
        return await self._shadowing().stop(session_id)

    async def shadow_complete(self, session_id: str) -> dict[str, Any]:
        return await self._shadowing().complete(session_id)

    async def shadow_cancel(self, session_id: str) -> dict[str, Any]:
        return await self._shadowing().cancel(session_id)

    def shadow_session(self, session_id: str) -> dict[str, Any]:
        return self._shadowing().get(session_id)

    def shadow_sessions(self, limit: int = 50) -> dict[str, Any]:
        return self._shadowing().list(limit=limit)

    def shadow_history(self, session_id: str) -> dict[str, Any]:
        return self._shadowing().history(session_id)

    async def shadow_cycle(
        self, session_id: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        body = dict(payload or {})
        return await self._shadowing().run_cycle(session_id, observations=body.get("observations"))

    async def shadow_track_outcomes(
        self, session_id: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        body = dict(payload or {})
        return await self._shadowing().track_outcomes(
            session_id, observations=body.get("observations")
        )

    def shadow_decisions(
        self,
        session_id: str,
        *,
        limit: int = 200,
        instrument_id: str = "",
        strategy_id: str = "",
        signal_type: str = "",
        status: str = "",
    ) -> dict[str, Any]:
        return self._shadowing().decisions(
            session_id,
            limit=limit,
            instrument_id=instrument_id,
            strategy_id=strategy_id,
            signal_type=signal_type,
            status=status,
        )

    def shadow_projections(self, session_id: str, limit: int = 200) -> dict[str, Any]:
        return self._shadowing().projections(session_id, limit=limit)

    def shadow_outcomes(self, session_id: str, limit: int = 500) -> dict[str, Any]:
        return self._shadowing().outcomes(session_id, limit=limit)

    def shadow_analytics(self, session_id: str) -> dict[str, Any]:
        return self._shadowing().analytics(session_id)

    def shadow_errors(self, session_id: str, limit: int = 100) -> dict[str, Any]:
        return self._shadowing().errors(session_id, limit=limit)

    def shadow_health(self, session_id: str) -> dict[str, Any]:
        return self._shadowing().health(session_id)

    async def shadow_compare(
        self, session_id: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._shadowing().compare(session_id, payload)

    def shadow_comparisons(self, session_id: str, limit: int = 50) -> dict[str, Any]:
        return self._shadowing().comparisons(session_id, limit=limit)

    # -- NM-13: controlled adaptive learning --------------------------------
    def _ensure_learning(self) -> "LearningService":
        from novacontrol.novamarket.learning import LearningService

        if self._learning_service is not None:
            return self._learning_service
        try:
            shadow: Any | None
            try:
                shadow = self._shadow_service or self._ensure_shadow()
            except Exception:  # noqa: BLE001 - learning still works without validation
                shadow = None
            service = LearningService(
                self._learning_path(),
                market_data=self._market_data_engine(),
                strategy_loader=self._shadow_strategy,
                risk_policy_loader=self._backtest_risk_policy,
                backtest_loader=self._shadow_backtest,
                paper_loader=self._learning_paper,
                shadow_loader=self._learning_shadow,
                shadow_service=shadow,
                strategy_applier=self._learning_apply,
                publisher=self._learning_publish,
                clock=self._clock,
                # A full validation replay can exceed the library's 30-second
                # default on this host; keep the guard bounded but large enough
                # for a real experiment to finish.
                max_cpu_seconds=180.0,
            )
        except Exception as exc:  # noqa: BLE001
            self._learning_error = f"{type(exc).__name__}: {exc}"
            raise
        self._learning_service = service
        self._learning_error = ""
        return service

    def _learning(self) -> "LearningService":
        service = self._learning_service
        if service is not None:
            return service
        if self._lifecycle.state not in (
            LifecycleState.READY,
            LifecycleState.RUNNING,
            LifecycleState.DEGRADED,
        ):
            raise NotOperationalError(
                "learning mode is not available while NovaMarket is "
                f"{self._lifecycle.state.value}; enable NovaMarket first"
            )
        return self._ensure_learning()

    async def _learning_publish(self, event_type: str, payload: dict[str, Any]) -> None:
        await self._publish(EventType(str(event_type)), **payload)

    def _learning_apply(self, strategy_id: str, config: Mapping[str, Any]) -> Mapping[str, Any]:
        """The one seam by which an approved learning version becomes active."""
        return cast(
            Mapping[str, Any],
            self._strategy_service().update_config(
                strategy_id,
                dict(config),
                actor="nm-13",
                reason="approved learning promotion",
            ),
        )

    def _learning_paper(self, strategy_id: str) -> Mapping[str, Any]:
        """NM-10 paper records for one strategy, with their simulated origin."""
        engine = self._execution_service()
        rows = list(engine.orders(limit=500).get("orders") or ())
        selected = [row for row in rows if str(row.get("strategy_id") or "") == strategy_id]
        fills: list[dict[str, Any]] = []
        for row in selected:
            fills.extend(engine.fills(str(row.get("order_id"))).get("fills") or ())
        stamps = [str(row.get("created_at")) for row in selected if row.get("created_at")]
        return {
            "strategy_id": strategy_id,
            "orders": selected,
            "fills": fills,
            "window_start": min(stamps) if stamps else None,
            "window_end": max(stamps) if stamps else None,
            "simulated": True,
            "real_money": False,
        }

    def _learning_shadow(self, session_id: str) -> Mapping[str, Any]:
        """NM-12 analytics for one session, kept hypothetical."""
        shadow = self._shadowing()
        session = shadow.get(session_id)["session"]
        analytics = shadow.analytics(session_id)
        decisions = list(shadow.decisions(session_id, limit=1).get("decisions") or ())
        first = decisions[0] if decisions else {}
        frozen = (session.get("metadata") or {}).get("frozen_strategies") or {}
        record: Mapping[str, Any] = (
            next(iter(frozen.values()), {}) if isinstance(frozen, Mapping) else {}
        )
        return {
            "session_id": session_id,
            "strategy_id": first.get("strategy_id") or record.get("strategy_id"),
            "strategy_version": first.get("strategy_version") or record.get("version"),
            "catalog_id": record.get("catalog_id"),
            "config": record.get("config") or {},
            "started_at": session.get("started_at"),
            "completed_at": session.get("completed_at"),
            "analytics": analytics,
            "hypothetical": True,
        }

    def learning_status(self) -> dict[str, Any]:
        if self._learning_service is None:
            return {
                "ok": True,
                "phase": "NM-13",
                "initialised": False,
                "execution_connected": False,
                "executes_trades": False,
                "last_error": self._learning_error,
                "detail": "learning opens when NovaMarket is operational",
            }
        return self._learning_service.status()

    def learning_contract(self) -> dict[str, Any]:
        if self._learning_service is None:
            return {
                "ok": True,
                "phase": "NM-13",
                "implemented": False,
                "execution_connected": False,
                "executes_trades": False,
                "detail": "learning is not initialised in this run",
            }
        return self._learning_service.contract()

    def learning_dashboard(self) -> dict[str, Any]:
        return self._learning().dashboard()

    def learning_health(self) -> dict[str, Any]:
        return self._learning().health()

    def learning_record_evidence(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._learning().record_evidence(payload)

    def learning_evidence(
        self,
        *,
        strategy_id: str = "",
        kind: str = "",
        status: str = "",
        limit: int = 200,
    ) -> dict[str, Any]:
        return self._learning().evidence_rows(
            strategy_id=strategy_id, kind=kind, status=status, limit=limit
        )

    def learning_evidence_record(self, evidence_id: str) -> dict[str, Any]:
        return self._learning().evidence(evidence_id)

    def learning_monitor(
        self, strategy_id: str, *, kind: str = "", window_days: int = 30
    ) -> dict[str, Any]:
        return self._learning().monitor(strategy_id, kind=kind, window_days=window_days)

    def learning_alerts(
        self, *, strategy_id: str = "", status: str = "", limit: int = 100
    ) -> dict[str, Any]:
        return self._learning().alerts(strategy_id=strategy_id, status=status, limit=limit)

    def learning_acknowledge_alert(self, alert_id: str, actor: str = "") -> dict[str, Any]:
        return self._learning().acknowledge_alert(alert_id, actor)

    def learning_create_experiment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._learning().create_experiment(payload)

    async def learning_start_experiment(self, experiment_id: str) -> dict[str, Any]:
        return await self._learning().start_experiment(experiment_id)

    async def learning_run_experiment(self, experiment_id: str) -> dict[str, Any]:
        return await self._learning().run_experiment(experiment_id)

    async def learning_cancel_experiment(
        self, experiment_id: str, reason: str = ""
    ) -> dict[str, Any]:
        return await self._learning().cancel_experiment(experiment_id, reason)

    def learning_experiment(self, experiment_id: str) -> dict[str, Any]:
        return self._learning().experiment(experiment_id)

    def learning_experiments(self, limit: int = 50) -> dict[str, Any]:
        return self._learning().experiments(limit=limit)

    def learning_candidates(
        self,
        *,
        experiment_id: str = "",
        strategy_id: str = "",
        limit: int = 200,
    ) -> dict[str, Any]:
        return self._learning().candidates(
            experiment_id=experiment_id,
            strategy_id=strategy_id,
            limit=limit,
        )

    def learning_candidate(self, candidate_id: str) -> dict[str, Any]:
        return self._learning().candidate(candidate_id)

    async def learning_evaluate_candidate(self, candidate_id: str) -> dict[str, Any]:
        return await self._learning().evaluate_candidate(candidate_id)

    def learning_approve_candidate(
        self, candidate_id: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._learning().approve_candidate(candidate_id, payload)

    def learning_reject_candidate(
        self, candidate_id: str, reason: str = "", actor: str = ""
    ) -> dict[str, Any]:
        return self._learning().reject_candidate(candidate_id, reason=reason, actor=actor)

    async def learning_shadow_validate(
        self, candidate_id: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._learning().shadow_validate_candidate(candidate_id, payload)

    def learning_promote_candidate(
        self, candidate_id: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._learning().promote_candidate(candidate_id, payload)

    def learning_rollback(
        self, strategy_id: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._learning().rollback(strategy_id, payload)

    def learning_versions(self, strategy_id: str) -> dict[str, Any]:
        return self._learning().versions(strategy_id)

    def learning_active_version(self, strategy_id: str) -> dict[str, Any]:
        return self._learning().active_version(strategy_id)

    def learning_rollbacks(self, strategy_id: str = "", limit: int = 50) -> dict[str, Any]:
        return self._learning().rollbacks(strategy_id=strategy_id, limit=limit)

    def strategy_catalog(self) -> dict[str, Any]:
        return self._strategy_service().catalog()  # type: ignore[no-any-return]

    def strategies(
        self, *, status: str = "", category: str = "", limit: int = 100
    ) -> dict[str, Any]:
        return self._strategy_service().strategies(status=status, category=category, limit=limit)  # type: ignore[no-any-return]

    def strategy(self, strategy_id: str) -> dict[str, Any]:
        return self._strategy_service().strategy(strategy_id)  # type: ignore[no-any-return]

    def strategy_versions(self, strategy_id: str) -> dict[str, Any]:
        return self._strategy_service().versions(strategy_id)  # type: ignore[no-any-return]

    async def create_strategy(self, catalog_id: str, **kwargs: Any) -> dict[str, Any]:
        return await self._strategy_service().create(catalog_id, **kwargs)  # type: ignore[no-any-return]

    async def update_strategy_config(
        self, strategy_id: str, changes: dict[str, Any], *, actor: str = "", reason: str = ""
    ) -> dict[str, Any]:
        return self._strategy_service().update_config(  # type: ignore[no-any-return]
            strategy_id, changes, actor=actor, reason=reason
        )

    async def strategy_transition(
        self, strategy_id: str, action: str, *, actor: str = "", reason: str = ""
    ) -> dict[str, Any]:
        service = self._strategy_service()
        handler = {
            "activate": service.activate,
            "pause": service.pause,
            "suspend": service.suspend,
            "retire": service.retire,
            "degrade": service.degrade,
        }.get(action)
        if handler is None:
            raise StrategyValidationError(f"unknown strategy action {action!r}")
        return await handler(strategy_id, actor=actor, reason=reason)  # type: ignore[no-any-return]

    async def evaluate_strategy(
        self,
        strategy_id: str,
        portfolio_id: str,
        *,
        instrument_ids: list[str] | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        return await self._strategy_service().evaluate(  # type: ignore[no-any-return]
            strategy_id, portfolio_id, instrument_ids=instrument_ids, actor=actor
        )

    async def propose_strategy_signal(
        self, signal_id: str, *, notional: object | None = None, actor: str = ""
    ) -> dict[str, Any]:
        return await self._strategy_service().propose(  # type: ignore[no-any-return]
            signal_id, notional=notional, actor=actor
        )

    async def run_strategies(
        self,
        portfolio_id: str,
        *,
        strategy_ids: list[str] | None = None,
        instrument_ids: list[str] | None = None,
        conflict_policy: str = "review",
        actor: str = "",
    ) -> dict[str, Any]:
        return await self._strategy_service().run(  # type: ignore[no-any-return]
            portfolio_id,
            strategy_ids=strategy_ids,
            instrument_ids=instrument_ids,
            conflict_policy=conflict_policy,
            actor=actor,
        )

    async def forward_strategy_proposal(self, proposal_id: str) -> dict[str, Any]:
        return await self._strategy_service().forward(proposal_id)  # type: ignore[no-any-return]

    def strategy_signals(
        self,
        *,
        strategy_id: str = "",
        portfolio_id: str = "",
        signal_type: str = "",
        limit: int = 200,
    ) -> dict[str, Any]:
        return self._strategy_service().signals(  # type: ignore[no-any-return]
            strategy_id=strategy_id, portfolio_id=portfolio_id, signal_type=signal_type, limit=limit
        )

    def strategy_signal(self, signal_id: str) -> dict[str, Any]:
        return self._strategy_service().signal(signal_id)  # type: ignore[no-any-return]

    def strategy_proposals(
        self, *, strategy_id: str = "", portfolio_id: str = "", status: str = "", limit: int = 100
    ) -> dict[str, Any]:
        return self._strategy_service().proposals(  # type: ignore[no-any-return]
            strategy_id=strategy_id, portfolio_id=portfolio_id, status=status, limit=limit
        )

    def strategy_proposal(self, proposal_id: str) -> dict[str, Any]:
        return self._strategy_service().proposal(proposal_id)  # type: ignore[no-any-return]

    def strategy_conflicts(self, limit: int = 100) -> dict[str, Any]:
        return self._strategy_service().conflicts(limit)  # type: ignore[no-any-return]

    async def strategy_performance(self, strategy_id: str) -> dict[str, Any]:
        return dict(await self._strategy_service().performance(strategy_id))

    def strategy_lifecycle(self, strategy_id: str) -> dict[str, Any]:
        return self._strategy_service().lifecycle(strategy_id)  # type: ignore[no-any-return]

    def wallet_status(self) -> dict[str, Any]:
        """Ledger totals and the last reconciliation, or why it is not open."""
        if self._wallet_engine is None and self._lifecycle.state in (
            LifecycleState.READY,
            LifecycleState.RUNNING,
            LifecycleState.DEGRADED,
        ):
            try:
                self._ensure_wallet()
            except Exception as exc:  # noqa: BLE001 - status reports, never raises
                return {
                    "initialised": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                    "nova_coin_per_inr": "1",
                    "currency": "INR",
                }
        engine = self._wallet_engine
        if engine is None:
            return {
                "initialised": False,
                "detail": (
                    "the ledger opens when NovaMarket is operational "
                    f"(currently {self._lifecycle.state.value})"
                ),
                "nova_coin_per_inr": "1",
                "currency": "INR",
                "error": self._wallet_error,
            }
        try:
            return engine.status()
        except Exception as exc:  # noqa: BLE001 - a read must not crash status
            _LOGGER.warning("NovaMarket could not read wallet status: %s", exc)
            return {"initialised": False, "detail": str(exc)}

    async def create_wallet(
        self, owner: str, *, label: str = "", wallet_id: str | None = None
    ) -> dict[str, Any]:
        """Create one wallet; the caller cannot choose an opening balance."""
        return await self._wallet_service().create_wallet(owner, label=label, wallet_id=wallet_id)

    def wallet_detail(self, wallet_id: str) -> dict[str, Any]:
        return self._wallet_service().wallet(wallet_id)

    def wallets_list(
        self,
        *,
        owner: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self._wallet_service().wallets(owner=owner, limit=limit, offset=offset)

    async def wallet_deposit(
        self,
        wallet_id: str,
        amount: object,
        *,
        idempotency_key: str = "",
        reference: str = "",
        description: str = "",
        owner: str | None = None,
    ) -> dict[str, Any]:
        return await self._wallet_service().deposit(
            wallet_id,
            amount,
            idempotency_key=idempotency_key,
            reference=reference,
            description=description,
            owner=owner,
        )

    async def wallet_withdraw(
        self,
        wallet_id: str,
        amount: object,
        *,
        idempotency_key: str = "",
        reference: str = "",
        description: str = "",
        owner: str | None = None,
    ) -> dict[str, Any]:
        return await self._wallet_service().withdraw(
            wallet_id,
            amount,
            idempotency_key=idempotency_key,
            reference=reference,
            description=description,
            owner=owner,
        )

    async def wallet_transfer(
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
        return await self._wallet_service().transfer(
            from_wallet_id,
            to_wallet_id,
            amount,
            idempotency_key=idempotency_key,
            reference=reference,
            description=description,
            owner=owner,
        )

    async def wallet_reverse(
        self,
        transaction_id: str,
        *,
        reason: str,
        idempotency_key: str = "",
        owner: str | None = None,
    ) -> dict[str, Any]:
        return await self._wallet_service().reverse(
            transaction_id,
            reason=reason,
            idempotency_key=idempotency_key,
            owner=owner,
        )

    async def wallet_set_status(
        self, wallet_id: str, status: str, *, reason: str = ""
    ) -> dict[str, Any]:
        return await self._wallet_service().set_wallet_status(wallet_id, status, reason=reason)

    def wallet_transactions(
        self,
        *,
        wallet_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        ascending: bool = False,
    ) -> dict[str, Any]:
        return self._wallet_service().transactions(
            wallet_id=wallet_id, limit=limit, offset=offset, ascending=ascending
        )

    def wallet_entries(
        self,
        *,
        wallet_id: str | None = None,
        transaction_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        ascending: bool = True,
    ) -> dict[str, Any]:
        return self._wallet_service().entries(
            wallet_id=wallet_id,
            transaction_id=transaction_id,
            limit=limit,
            offset=offset,
            ascending=ascending,
        )

    async def wallet_reconcile(self, wallet_id: str | None = None) -> dict[str, Any]:
        """Independently verify balances against the ledger; repair nothing."""
        return await self._wallet_service().reconcile(wallet_id)

    def wallet_contract(self) -> dict[str, Any]:
        """The NM-03 contract as data, for ``/market/contracts``."""
        return {
            "implemented": self._wallet_engine is not None,
            "phase": "NM-03",
            "accounting_unit": "NovaCoin",
            "currency": "INR",
            "exchange_rate": "1 NC = 1 INR",
            "precision": {
                "decimals": 2,
                "smallest_unit": "0.01 NC",
                "rounding": "ROUND_HALF_EVEN",
                "unrepresentable_amounts": "refused, never silently rounded",
                "binary_floats": "refused at construction",
            },
            "convention": {
                "credit": "increases the wallet balance (positive signed amount)",
                "debit": "decreases the wallet balance (negative signed amount)",
                "balance": ("stored balance equals the sum of the wallet's signed entries"),
                "double_entry": "every transaction's entries sum to zero",
                "issuance_account": (
                    "novamarket:issuance carries the negative side of deposits and withdrawals"
                ),
            },
            "append_only": {
                "tables": ["ledger_entries", "wallet_transactions"],
                "enforced_by": "SQLite triggers that abort UPDATE and DELETE",
                "corrections": (
                    "a reversal transaction with mirrored entries and a required reason"
                ),
            },
            "idempotency": {
                "storage": "wallet_idempotency table, primary key on the key",
                "reuse": "same key + same fingerprint returns the original result",
                "conflict": "same key + different fingerprint is refused",
            },
            "atomicity": (
                "balance updates, entries, transaction and idempotency record "
                "commit in one BEGIN IMMEDIATE transaction"
            ),
            "reconciliation": (
                "stored balance vs SUM(signed entries), per-transaction zero sum "
                "and whole-ledger zero sum"
            ),
            "tables": [
                "wallets",
                "wallet_transactions",
                "ledger_entries",
                "wallet_idempotency",
                "wallet_meta",
            ],
            "limits": [
                "No reserved balance: NM-03 holds no funds, so total equals available.",
                "No bank, broker or crypto rail: deposits and withdrawals touch only the issuance account.",
                "No live trading, portfolio or order route exists.",
            ],
        }

    # -- market data (NM-02) --------------------------------------------------

    @property
    def market_data(self) -> MarketDataEngine | None:
        """The engine, or ``None`` before initialisation / after shutdown."""
        return self._market_engine

    def _market_data_engine(self) -> MarketDataEngine:
        engine = self._market_engine
        if engine is None:
            raise NotOperationalError(
                "NovaMarket market data is not initialised; enable NovaMarket first"
            )
        return engine

    def _verified_operations(self) -> frozenset[str]:
        engine = self._market_engine
        if engine is None:
            return frozenset()
        try:
            return frozenset(
                operation
                for health in engine.store.all_provider_health()
                for operation in health.verified_operations
            )
        except Exception as exc:  # noqa: BLE001 - a read must not crash status
            _LOGGER.warning("NovaMarket could not read verified provider operations: %s", exc)
            return frozenset()

    def _installed_adapters(self) -> tuple[Any, ...]:
        credentials = self._config.provider_credentials
        alpha_ref = credentials.get("alpha-vantage") or self._config.credentials_ref
        return build_adapters(
            self._transport or UrllibTransport(),
            credentials_ref=alpha_ref,
            secret_resolver=self._secret_resolver,
            clock=self._clock,
        )

    def _configured_provider_names(self, registry: ProviderRegistry) -> tuple[str, ...]:
        """Configured priority order, or the credential-ready installed set.

        A configured name this build does not install is reported rather than
        silently dropped; it does not become an enabled provider.
        """
        configured = tuple(self._config.data_providers)
        installed = set(registry.names)
        self._unknown_providers = tuple(name for name in configured if name not in installed)
        if configured:
            return tuple(name for name in configured if name in installed)
        # Nothing configured: enable the installed adapters that can actually
        # work on this machine. An adapter needing a credential is enabled only
        # when its reference resolves — installing it is not the same as having
        # a key, and a provider that must fail every call is not enabled.
        rows = {str(row["provider"]): row for row in registry.capability_matrix()}
        ready: list[str] = []
        for name in registry.names:
            row = rows.get(name, {})
            if row.get("requires_credentials"):
                reference = str(row.get("credential_reference") or "")
                if not resolve_credential(self._secret_resolver, reference):
                    continue
            ready.append(name)
        return tuple(ready)

    def _ensure_market_data(self) -> MarketDataEngine:
        """Open the repository and build the engine ONCE per operational run."""
        if self._market_engine is not None:
            return self._market_engine
        path = self._market_data_path or (
            self._data_dir / "novamarket.sqlite3"
            if self._data_dir is not None
            else Path(":memory:")
        )
        store = MarketDataStore(path, clock=self._clock)
        try:
            registry = ProviderRegistry(self._installed_adapters())
            names = self._configured_provider_names(registry)
            engine = MarketDataEngine(
                store=store,
                registry=registry,
                enabled_providers=names,
                universe=self._config.universe,
                ingestion=self._config.ingestion,
                freshness=self._config.freshness,
                quality=self._config.quality,
                cache=self._config.cache,
                clock=self._clock,
                event_sink=self._on_market_event,
                history_page_limit=self._config.history_page_limit,
            )
        except Exception:
            store.close()
            raise
        self._market_store = store
        self._market_engine = engine
        self._market_ingestion_error = ""
        return engine

    # -- PI-03 low-latency gateway -------------------------------------------

    def gateway(self) -> MarketDataGateway:
        """Open the PI-03 gateway lazily over the existing NM-02 engine.

        The gateway owns no provider, store, scheduler or validation engine. It
        is the controlled policy/distribution layer for new consumers.
        """
        if self._gateway is not None:
            return self._gateway
        engine = self._ensure_market_data()
        try:
            self._gateway = MarketDataGateway(
                engine,
                settings=self._config.gateway,
                clock=self._clock,
            )
        except Exception as exc:
            self._gateway_error = f"{type(exc).__name__}: {exc}"
            raise
        self._gateway_error = ""
        return self._gateway

    def gateway_service(self) -> MarketDataGateway:
        return self.gateway()

    def gateway_status(self) -> dict[str, Any]:
        if self._gateway is None:
            return {
                "phase": "PI-03",
                "implemented": True,
                "initialised": False,
                "enabled": self._config.gateway.enabled,
                "settings": self._config.gateway.to_dict(),
                "error": self._gateway_error,
                "detail": "the gateway opens with the market-data engine",
            }
        return self._gateway.status()

    def gateway_contract(self) -> dict[str, Any]:
        if self._gateway is None:
            return {
                "phase": "PI-03",
                "settings": self._config.gateway.to_dict() or GatewaySettings.schema(),
                "general_refresh_floor_seconds": (
                    self._config.gateway.general_refresh_floor_seconds
                ),
                "detail": "runtime gateway opens with market data",
            }
        return self._gateway.contract()

    def gateway_subscribe(
        self,
        instrument_id: str,
        *,
        priority: AcquisitionPriority | str = AcquisitionPriority.GENERAL,
        reason: str = "",
    ) -> dict[str, Any]:
        return self.gateway().subscribe(instrument_id, priority=priority, reason=reason)

    def gateway_unsubscribe(self, instrument_id: str) -> bool:
        return self.gateway().unsubscribe(instrument_id)

    def gateway_prioritize(
        self,
        instrument_id: str,
        priority: AcquisitionPriority | str,
        *,
        reason: str = "",
    ) -> dict[str, Any]:
        return self.gateway().prioritize(instrument_id, priority, reason=reason)

    def gateway_current(self, instrument_id: str, *, provider: str | None = None) -> dict[str, Any]:
        return self.gateway().current(instrument_id, provider=provider).to_dict()

    async def gateway_checkpoint(
        self,
        instrument_id: str,
        *,
        previous_observed_at: datetime | None = None,
        refresh: bool = False,
        provider: str | None = None,
        accepted_freshness: Sequence[Any] | None = None,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "previous_observed_at": previous_observed_at,
            "refresh": refresh,
            "provider": provider,
        }
        if accepted_freshness is not None:
            options["accepted_freshness"] = accepted_freshness
        return await self.gateway().checkpoint(instrument_id, **options)

    async def gateway_refresh(
        self,
        instrument_ids: Sequence[str],
        *,
        provider: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        return await self.gateway().refresh(instrument_ids, provider=provider, force=force)

    async def gateway_run_intraday(
        self,
        instrument_ids: Sequence[str] | None = None,
        *,
        trigger: str = "manual",
        limit: int | None = None,
    ) -> dict[str, Any]:
        return (
            await self.gateway().run_intraday_cycle(instrument_ids, trigger=trigger, limit=limit)
        ).to_dict()

    async def gateway_start(self) -> dict[str, Any]:
        return await self.gateway().start()

    async def gateway_stop(self) -> dict[str, Any]:
        if self._gateway is None:
            return self.gateway_status()
        return await self._gateway.stop()

    def gateway_providers(self) -> tuple[dict[str, Any], ...]:
        return self.gateway().provider_contracts()

    def gateway_latency(self) -> dict[str, Any]:
        return cast("dict[str, Any]", self.gateway().status()["latency"])

    # -- PI-01 prediction intelligence ---------------------------------------
    def prediction_service(self) -> PredictionIntelligenceService:
        """Open the prediction layer lazily over the existing market-data engine."""
        if self._prediction_service is not None:
            return self._prediction_service
        engine = self._ensure_market_data()
        path = self._market_data_path or (
            self._data_dir / "novamarket.sqlite3"
            if self._data_dir is not None
            else Path(":memory:")
        )
        try:
            service = PredictionIntelligenceService(
                path,
                market_data=engine,
                publisher=self._publisher,
                clock=self._clock,
            )
        except Exception as exc:
            self._prediction_error = f"{type(exc).__name__}: {exc}"
            raise
        self._prediction_service = service
        self._prediction_error = ""
        return service

    def prediction_status(self) -> dict[str, Any]:
        """PI-01 status, without opening the store just to answer a read."""
        if self._prediction_service is None:
            return {
                "phase": PI_PHASE,
                "implemented": True,
                "initialised": False,
                "contract_version": PI_CONTRACT_VERSION,
                "detail": "prediction intelligence opens with the market-data engine",
                "error": self._prediction_error,
                "authorities": {
                    "risk": "NM-08",
                    "execution": "NM-10",
                    "prediction_authorizes_action": False,
                },
            }
        return {
            "phase": PI_PHASE,
            "implemented": True,
            "initialised": True,
            "error": self._prediction_error,
            **self._prediction_service.status(),
        }

    async def register_prediction_model(self, model: Any) -> dict[str, Any]:
        return await self.prediction_service().register_model(model)

    async def create_prediction_lifecycle(
        self,
        request: Any,
        predictions: Sequence[Any],
        *,
        models: Sequence[Any] | None = None,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        return await self.prediction_service().create_lifecycle(
            request, predictions, models=models, at=at
        )

    async def confirm_prediction(
        self,
        lifecycle_id: str,
        stage: Any,
        predictions: Sequence[Any],
        *,
        models: Sequence[Any] | None = None,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        return await self.prediction_service().confirm(
            lifecycle_id, stage, predictions, models=models, at=at
        )

    async def evaluate_prediction(
        self, lifecycle_id: str, actual_price: Any, *, at: datetime | None = None
    ) -> dict[str, Any]:
        return await self.prediction_service().evaluate(lifecycle_id, actual_price, at=at)

    async def prediction_decision_evidence(
        self, lifecycle_id: str, *, portfolio_id: str = "", at: datetime | None = None
    ) -> dict[str, Any]:
        return await self.prediction_service().decision_evidence(
            lifecycle_id, portfolio_id=portfolio_id, at=at
        )

    def prediction_lifecycle(self, lifecycle_id: str) -> dict[str, Any]:
        return self.prediction_service().lifecycle(lifecycle_id)

    def prediction_history(self, lifecycle_id: str) -> dict[str, Any]:
        return self.prediction_service().forecast_history(lifecycle_id)

    # -- PI-04 features and regimes -----------------------------------------

    def _feature_path(self) -> Path:
        if self._market_data_path is not None:
            return self._market_data_path
        if self._data_dir is not None:
            return self._data_dir / "novamarket.sqlite3"
        return Path(":memory:")

    def feature_service(self) -> FeatureService:
        """Open PI-04 lazily over the existing engines and one SQLite file."""
        if self._feature_service is not None:
            return self._feature_service
        engine = self._ensure_market_data()
        dataset_store = None
        try:
            dataset_store = self.dataset_service().store
        except Exception as exc:  # noqa: BLE001 - data may be supplied directly
            self._feature_error = f"dataset store: {exc}"
        radar = None
        try:
            radar = self._radar_service()
        except Exception as exc:  # noqa: BLE001 - news context is optional
            self._feature_error = f"radar: {exc}"
        try:
            service = FeatureService(
                market_data=engine,
                store=FeatureStore(self._feature_path()),
                settings=self._config.features,
                radar=radar,
                dataset_store=dataset_store,
                event_sink=self._publish_feature_event,
                clock=self._clock,
            )
        except Exception as exc:
            self._feature_error = f"{type(exc).__name__}: {exc}"
            raise
        self._feature_service = service
        self._feature_error = ""
        return service

    async def _publish_feature_event(
        self, event_type: EventType, payload: Mapping[str, Any]
    ) -> None:
        await self._publish(event_type, **dict(payload))

    def feature_status(self) -> dict[str, Any]:
        if self._feature_service is None:
            return {
                "phase": "PI-04",
                "implemented": True,
                "initialised": False,
                "contract_version": FEATURE_CONTRACT_VERSION,
                "feature_set_version": FEATURE_SET_VERSION,
                "regime_method_version": REGIME_METHOD_VERSION,
                "feature_count": len(FEATURE_SPECS),
                "profiles": {
                    str(profile): len(profile_features(profile)) for profile in FeatureProfileName
                },
                "settings": self._config.features.to_dict(),
                "detail": "features open with the market-data engine",
                "error": self._feature_error,
                "authorities": {
                    "risk": "NM-08",
                    "execution": "NM-10",
                    "features_authorize_action": False,
                },
            }
        return {
            "phase": "PI-04",
            "implemented": True,
            "initialised": True,
            "error": self._feature_error,
            "settings": self._config.features.to_dict(),
            **self._feature_service.status(),
        }

    def feature_contract(self) -> dict[str, Any]:
        if self._feature_service is None:
            return {
                "implemented": True,
                "phase": "PI-04",
                "status": "contracts implemented; runtime opens with market data",
                "contract_version": FEATURE_CONTRACT_VERSION,
                "feature_set_version": FEATURE_SET_VERSION,
                "regime_method_version": REGIME_METHOD_VERSION,
                "feature_count": len(FEATURE_SPECS),
                "families": [str(item) for item in FeatureFamily],
                "profiles": [str(item) for item in FeatureProfileName],
                "regimes": [str(item) for item in RegimeLabel],
                "definitions": {
                    name: spec.to_dict() for name, spec in sorted(FEATURE_SPECS.items())
                },
                "leakage_rule": (
                    "Only completed bars whose bar-close timestamp is at or before as_of, "
                    "events received at or before as_of, and fundamentals available at or "
                    "before as_of are eligible. A regime is not a prediction."
                ),
                "confidence_policy": (
                    "confidence is null until a separate phase calibrates the detector"
                ),
                "automatic_feature_deletion": False,
                "settings": self._config.features.to_dict(),
                "authorities": {
                    "risk": "NM-08",
                    "execution": "NM-10",
                    "features_authorize_action": False,
                },
            }
        return self._feature_service.contract()

    async def generate_features(
        self,
        request: FeatureRequest | Mapping[str, Any],
        *,
        bars: Mapping[str, Sequence[Any]] | None = None,
        benchmark_bars: Mapping[str, Sequence[Any]] | None = None,
        sector_bars: Mapping[str, Sequence[Any]] | None = None,
        context_bars: Mapping[str, Mapping[str, Sequence[Any]]] | None = None,
        fundamentals: Sequence[Any] = (),
        news_events: Sequence[Any] | None = None,
        previous_regime: RegimeResult | None = None,
    ) -> dict[str, Any]:
        """Generate and persist one point-in-time feature row and its regime."""
        service = self.feature_service()
        if isinstance(request, Mapping):
            raw = dict(request)
            raw.setdefault("instrument_id", "")
            raw.setdefault("as_of", self._clock())
            for key in ("as_of",):
                value = raw.get(key)
                if isinstance(value, str):
                    raw[key] = datetime.fromisoformat(value.replace("Z", "+00:00"))
            request = FeatureRequest(**raw)
        if previous_regime is None:
            latest = service.latest_regime(request.instrument_id, resolution=request.resolution)
            if latest is not None and latest.as_of < request.as_of:
                previous_regime = latest
        result = await service.generate(
            request,
            bars=bars,
            benchmark_bars=benchmark_bars,
            sector_bars=sector_bars,
            context_bars=context_bars,
            fundamentals=fundamentals,
            news_events=news_events,
            previous_regime=previous_regime,
        )
        return {
            "feature_set": result.feature_set.to_dict(),
            "regime": result.regime.to_dict(),
            "persisted": result.persisted,
            "duplicate": result.duplicate,
            "store_error": result.store_error,
            "authorities": {
                "risk": "NM-08",
                "execution": "NM-10",
                "features_authorize_action": False,
            },
        }

    def feature_set(self, feature_set_id: str) -> dict[str, Any] | None:
        value = self.feature_service().feature_set(feature_set_id)
        return value.to_dict() if value is not None else None

    def latest_feature_set(
        self, instrument_id: str, *, resolution: str | None = None
    ) -> dict[str, Any] | None:
        value = self.feature_service().latest_feature_set(instrument_id, resolution=resolution)
        return value.to_dict() if value is not None else None

    def feature_sets(
        self, *, instrument_id: str | None = None, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        return self.feature_service().feature_sets(
            instrument_id=instrument_id, limit=limit, offset=offset
        )

    def latest_regime(
        self, instrument_id: str, *, resolution: str | None = None
    ) -> dict[str, Any] | None:
        value = self.feature_service().latest_regime(instrument_id, resolution=resolution)
        return value.to_dict() if value is not None else None

    def feature_regimes(
        self, *, instrument_id: str | None = None, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        return self.feature_service().regimes(
            instrument_id=instrument_id, limit=limit, offset=offset
        )

    def fit_feature_preprocessing(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        feature_names: Sequence[str] | None = None,
        method: str = "standard",
        missing_policy: str | None = None,
        outlier_z: float = 6.0,
    ) -> dict[str, Any]:
        fit = self.feature_service().fit_preprocessing(
            rows,
            feature_names=feature_names,
            method=method,
            missing_policy=missing_policy or self._config.features.missing_policy,
            outlier_z=outlier_z,
        )
        return fit.to_dict()

    def diagnose_features(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        feature_names: Sequence[str] | None = None,
        leakage_safe: bool = True,
    ) -> dict[str, Any]:
        return (
            self.feature_service()
            .diagnose(
                rows,
                feature_names=feature_names,
                leakage_safe=leakage_safe,
            )
            .to_dict()
        )

    # -- PI-02 historical datasets -------------------------------------------
    def dataset_service(self) -> HistoricalDatasetService:
        """Open the dataset layer lazily over the existing market-data engine."""
        if self._dataset_service is not None:
            return self._dataset_service
        engine = self._ensure_market_data()
        path = self._market_data_path or (
            self._data_dir / "novamarket.sqlite3"
            if self._data_dir is not None
            else Path(":memory:")
        )
        try:
            service = HistoricalDatasetService(
                path,
                market_data=engine,
                publisher=self._publisher,
                clock=self._clock,
            )
        except Exception as exc:
            self._dataset_error = f"{type(exc).__name__}: {exc}"
            raise
        self._dataset_service = service
        self._dataset_error = ""
        return service

    def dataset_status(self) -> dict[str, Any]:
        """PI-02 status, without opening the store just to answer a read."""
        if self._dataset_service is None:
            return {
                "phase": PI02_PHASE,
                "implemented": True,
                "initialised": False,
                "contract_version": DATASET_CONTRACT_VERSION,
                "detail": "historical datasets open with the market-data engine",
                "error": self._dataset_error,
                "authorities": {
                    "risk": "NM-08",
                    "execution": "NM-10",
                    "dataset_authorizes_action": False,
                },
            }
        return {
            "phase": PI02_PHASE,
            "implemented": True,
            "initialised": True,
            "error": self._dataset_error,
            **self._dataset_service.status(),
        }

    async def build_historical_dataset(
        self,
        request: DatasetBuildRequest | Mapping[str, Any],
        *,
        fetch: bool | None = None,
    ) -> dict[str, Any]:
        return await self.dataset_service().build(request, fetch=fetch)

    def dataset_bars(
        self,
        dataset_id: str,
        version: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self.dataset_service().bars(dataset_id, version, **kwargs)

    def dataset_batch(
        self,
        dataset_id: str,
        version: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self.dataset_service().batch(dataset_id, version, **kwargs)

    def dataset_metadata(self, dataset_id: str, version: str | None = None) -> dict[str, Any]:
        return self.dataset_service().metadata(dataset_id, version)

    def dataset_builds(
        self,
        dataset_id: str | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Read the dataset build journal, including failed runs."""
        return self.dataset_service().builds(dataset_id, limit=limit, offset=offset)

    def dataset_coverage(self, dataset_id: str, version: str | None = None) -> dict[str, Any]:
        return self.dataset_service().coverage(dataset_id, version)

    def dataset_quality(self, dataset_id: str, version: str | None = None) -> dict[str, Any]:
        return self.dataset_service().quality(dataset_id, version)

    def dataset_missing(
        self, dataset_id: str, version: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        return self.dataset_service().missing(dataset_id, version, **kwargs)

    def dataset_versions(
        self, dataset_id: str | None = None, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        return self.dataset_service().versions(dataset_id, limit=limit, offset=offset)

    def build_training_dataset(
        self,
        dataset_id: str,
        version: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self.dataset_service().build_training(dataset_id, version, **kwargs)

    def register_corporate_action(self, action: Any) -> dict[str, Any]:
        return self.dataset_service().register_corporate_action(action)

    def register_dataset_annotation(self, annotation: Any) -> dict[str, Any]:
        return self.dataset_service().register_annotation(annotation)

    def _reconfigure_market_data(self) -> None:
        engine = self._market_engine
        if engine is None:
            return
        registry = engine.registry
        names = self._configured_provider_names(registry)
        engine.reconfigure(
            enabled_providers=names,
            universe=self._config.universe,
            ingestion=self._config.ingestion,
            freshness=self._config.freshness,
            quality=self._config.quality,
            cache=self._config.cache,
            history_page_limit=self._config.history_page_limit,
        )
        if self._gateway is not None:
            self._gateway.reconfigure(self._config.gateway)

    async def _on_market_event(self, name: str, payload: Mapping[str, Any]) -> None:
        """Adapt the engine's event vocabulary onto the one application bus."""
        try:
            event_type = EventType(name)
        except ValueError:
            self._last_event_error = f"engine emitted unknown event {name!r}"
            self._counters["event_failures"] += 1
            return
        await self._publish(event_type, **dict(payload))

    async def _start_ingestion_if_configured(self) -> None:
        engine = self._market_engine
        if engine is None:
            return
        self._market_ingestion_error = ""
        if not self._config.ingestion.enabled:
            await engine.stop_ingestion()
        else:
            try:
                await engine.start_ingestion()
            except Exception as exc:  # noqa: BLE001 - report, do not crash the lifecycle
                self._market_ingestion_error = f"{type(exc).__name__}: {exc}"
                _LOGGER.warning("NovaMarket background ingestion did not start: %s", exc)
        # PI-03 is a separate opt-in path. It is started only by explicit
        # configuration and never replaces or changes the general worker.
        try:
            if self._config.gateway.enabled:
                await self.gateway().start()
            elif self._gateway is not None:
                await self._gateway.stop()
        except Exception as exc:  # noqa: BLE001 - gateway failure is reported, not fatal
            self._gateway_error = f"{type(exc).__name__}: {exc}"
            _LOGGER.warning("NovaMarket intraday gateway did not start: %s", exc)

    def market_data_status(self) -> dict[str, Any]:
        """The market-data half of the status surface."""
        engine = self._market_engine
        if engine is None:
            return {
                "initialised": False,
                "store": {"ok": False, "append_only": True},
                "enabled_providers": [],
                "unknown_providers": list(self._unknown_providers),
                "ingestion": {"running": False, "enabled": self._config.ingestion.enabled},
                "verified_operations": [],
                "last_error": "market-data infrastructure is not initialised",
            }
        payload = engine.status()
        payload["initialised"] = True
        payload["unknown_providers"] = list(self._unknown_providers)
        payload["verified_operations"] = sorted(self._verified_operations())
        payload["ingestion_error"] = self._market_ingestion_error
        return payload

    async def market_quote(
        self,
        instrument_id: str,
        *,
        provider: str | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """The current reconciled quote, marked with its real freshness.

        ``refresh`` forces one bounded collection attempt first. Without it the
        stored observation is returned and freshness is recomputed now — a
        stale close is reported as stale, never as a live quote.
        """
        engine = self._market_data_engine()
        if refresh:
            # An explicit refresh must still return the latest provider close
            # outside trading hours; it is labelled with its real freshness.
            await engine.run_ingestion([instrument_id], trigger="manual", force_quotes=True)
        result = engine.latest(instrument_id, provider=provider)
        instrument = engine.get_instrument(instrument_id)
        # The freshness verdict is ALWAYS attached, including when there is no
        # observation: "nothing stored because the exchange is shut" is the
        # answer a consumer needs, and it is not the same as a provider outage.
        freshness = engine.freshness_of(instrument_id, provider=provider)
        return {
            "instrument": instrument.to_dict(),
            "quote": result.to_dict(),
            "freshness": freshness.to_dict(),
            "provenance": (result.selected.provenance() if result.selected is not None else None),
            "refreshed": refresh,
        }

    def market_history(
        self,
        instrument_id: str,
        *,
        provider: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        ascending: bool = False,
    ) -> dict[str, Any]:
        engine = self._market_data_engine()
        page = engine.observations(
            instrument_id,
            start=start,
            end=end,
            provider=provider,
            limit=limit,
            offset=offset,
            ascending=ascending,
        )
        return page.to_dict()

    def market_bars(
        self,
        instrument_id: str,
        *,
        interval: DataInterval | str | None = None,
        provider: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        ascending: bool = False,
    ) -> dict[str, Any]:
        engine = self._market_data_engine()
        page = engine.bars(
            instrument_id,
            interval=interval,
            provider=provider,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
            ascending=ascending,
        )
        return page.to_dict()

    def _workbench(self) -> MarketWorkbench:
        """Compose the read-only desk over the one NM-02 engine."""
        return MarketWorkbench(
            self._market_data_engine(),
            session=self.market_session(),
        )

    def _workbench_snapshot(self, kind: str, *, interval: str, limit: int) -> dict[str, Any]:
        """Serve a repeated board from a short read-through cache.

        The boards are deterministic over stored OHLCV, so a second read inside
        the TTL is the same answer. The cache is keyed by the surface and its
        query so the two desks never share a payload they did not ask for.
        """
        key = (kind, interval, int(limit))
        now = time.monotonic()
        cached = self._workbench_cache.get(key)
        if cached is not None and now - cached[0] < self._workbench_cache_ttl:
            return dict(cached[1])
        payload = self._workbench().snapshot(interval=interval, limit=limit)
        self._workbench_cache[key] = (now, payload)
        if len(self._workbench_cache) > 8:
            oldest = sorted(
                self._workbench_cache,
                key=lambda item: self._workbench_cache[item][0],
            )[: len(self._workbench_cache) - 8]
            for stale in oldest:
                self._workbench_cache.pop(stale, None)
        return payload

    def market_screener(
        self,
        *,
        exchange: str = "all",
        cap: str = "all",
        index: str = "all",
        sector: str = "all",
        sort: str = "default",
        movers: str = "all",
        query: str = "",
        available_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Filterable Market Overview rows over stored quotes and daily bars."""
        key = (
            exchange,
            cap,
            index,
            sector,
            sort,
            movers,
            query,
            bool(available_only),
            int(limit),
            int(offset),
        )
        with self._screener_cache_lock:
            now = time.monotonic()
            cached = self._screener_cache.get(key)
            if cached is not None and now - cached[0] < self._screener_cache_ttl:
                return dict(cached[1])
            try:
                payload = MarketBoard(
                    self._market_data_engine(),
                    session=self.market_session(),
                ).snapshot(
                    exchange=exchange,
                    cap=cap,
                    index=index,
                    sector=sector,
                    sort=sort,
                    movers=movers,
                    query=query,
                    available_only=available_only,
                    limit=limit,
                    offset=offset,
                )
            except ValueError as exc:
                raise ConfigurationError(str(exc)) from exc
            self._screener_cache[key] = (time.monotonic(), payload)
            if len(self._screener_cache) > 16:
                oldest = sorted(
                    self._screener_cache,
                    key=lambda item: self._screener_cache[item][0],
                )[: len(self._screener_cache) - 16]
                for stale in oldest:
                    self._screener_cache.pop(stale, None)
            return payload

    def market_orderblock(self, *, interval: str = "1d", limit: int = 50) -> dict[str, Any]:
        """OrderBlock boards and market context from stored OHLCV only."""
        return self._workbench_snapshot("orderblock", interval=interval, limit=limit)

    def market_orderblock_instrument(
        self, instrument_id: str, *, interval: str = "1d"
    ) -> dict[str, Any]:
        """Full deterministic structure for one instrument."""
        return self._workbench().instrument(instrument_id, interval=interval)

    def market_tradingwatch(self, *, interval: str = "1d", limit: int = 50) -> dict[str, Any]:
        """TradingWatch momentum, zones, heatmap, flow and option availability."""
        return self._workbench_snapshot("tradingwatch", interval=interval, limit=limit)

    def market_tradingwatch_replay(
        self,
        instrument_id: str,
        *,
        end_date: str | None = None,
        interval: str = "1d",
        limit: int = 180,
    ) -> dict[str, Any]:
        """Historical replay with a hard look-ahead cutoff."""
        return self._workbench().replay(
            instrument_id,
            end_date=end_date,
            interval=interval,
            limit=limit,
        )

    def market_tradingwatch_watchlists(self) -> dict[str, Any]:
        """Persisted watchlists enriched with latest stored prices."""
        workbench = self._workbench()
        base = self.watchlists()
        rows: list[dict[str, Any]] = []
        prices: dict[str, dict[str, Any]] = {}
        for row in base.get("watchlists") or ():
            detail = self.watchlist(str(row.get("watchlist_id") or ""), limit=200)
            members = list(detail.get("members") or ())
            for member in members:
                instrument_id = str(member.get("instrument_id") or "")
                if not instrument_id:
                    continue
                if instrument_id not in prices:
                    try:
                        prices[instrument_id] = workbench.latest_price(instrument_id)
                    except Exception as exc:  # noqa: BLE001 - a price read is optional
                        prices[instrument_id] = {
                            "available": False,
                            "instrument_id": instrument_id,
                            "price": None,
                            "reason": f"{type(exc).__name__}: {exc}",
                        }
                member["market"] = prices[instrument_id]
            rows.append({**row, "members": members, "member_count": len(members)})
        return {
            "ok": True,
            "watchlists": rows,
            "total": len(rows),
            "price_basis": "latest stored close, or a usable reconciled quote when no bars exist",
            "disclaimer": "Prices are the backend's latest stored observation; they are not an execution quote.",
        }

    def market_instruments(
        self,
        *,
        asset_class: str | None = None,
        exchange: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        engine = self._market_data_engine()
        return engine.list_instruments(
            asset_class=asset_class,
            exchange=exchange,
            limit=limit,
            offset=offset,
        ).to_dict()

    def register_market_instrument(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Register a canonical instrument; identity is explicit, not guessed."""
        engine = self._market_data_engine()
        if not isinstance(payload, Mapping):
            raise ConfigurationError("instrument must be an object")
        unknown = sorted(
            set(payload)
            - {
                "symbol",
                "name",
                "exchange",
                "mic",
                "asset_class",
                "currency",
                "timezone",
                "provider_symbols",
                "trading_status",
                "metadata",
            }
        )
        if unknown:
            raise ConfigurationError("unknown instrument field(s): " + ", ".join(unknown))
        provider_symbols = payload.get("provider_symbols") or {}
        if not isinstance(provider_symbols, Mapping):
            raise ConfigurationError("instrument.provider_symbols must be an object")
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            raise ConfigurationError("instrument.metadata must be an object")
        try:
            instrument = Instrument.create(
                symbol=str(payload.get("symbol") or ""),
                name=str(payload.get("name") or ""),
                exchange=str(payload.get("exchange") or ""),
                mic=str(payload.get("mic") or ""),
                asset_class=str(payload.get("asset_class") or "equity"),
                currency=str(payload.get("currency") or "INR"),
                timezone=str(payload.get("timezone") or "UTC"),
                provider_symbols={str(k): str(v) for k, v in provider_symbols.items()},
                trading_status=str(payload.get("trading_status") or "unknown"),
                metadata=dict(metadata),
            )
        except (ValueError, TypeError) as exc:
            raise ConfigurationError(f"invalid instrument: {exc}") from exc
        stored = engine.register_instrument(instrument)
        return {"instrument": stored.to_dict()}

    async def lookup_market_instrument(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Resolve and register metadata through one provider."""
        engine = self._market_data_engine()
        if not isinstance(payload, Mapping):
            raise ConfigurationError("lookup must be an object")
        provider = str(payload.get("provider") or "").strip()
        provider_symbol = str(payload.get("symbol") or "").strip()
        if not provider or not provider_symbol:
            raise ConfigurationError("lookup requires provider and symbol")
        instrument = await engine.lookup_instrument(
            provider,
            provider_symbol,
            symbol=payload.get("canonical_symbol"),
            name=payload.get("name"),
            currency=payload.get("currency"),
            timezone=payload.get("timezone"),
            exchange=payload.get("exchange"),
            mic=payload.get("mic"),
        )
        return {"instrument": instrument.to_dict()}

    async def ingest_market_data(
        self,
        *,
        instrument_ids: Sequence[str] | None = None,
        intervals: Sequence[str] | None = None,
        trigger: str = "manual",
        force_quotes: bool = False,
        include_quotes: bool | None = None,
    ) -> dict[str, Any]:
        engine = self._market_data_engine()
        report = await engine.run_ingestion(
            list(instrument_ids) if instrument_ids else None,
            trigger=trigger,
            intervals=list(intervals) if intervals else None,
            force_quotes=force_quotes,
            include_quotes=include_quotes,
        )
        return report.to_dict()

    async def market_providers(self, *, probe: bool = False) -> dict[str, Any]:
        engine = self._market_data_engine()
        probes: tuple[ProviderProbe, ...] = ()
        if probe:
            probes = await engine.probe_providers()
        return {
            "providers": list(engine.provider_status()),
            "enabled_providers": list(engine.enabled_providers),
            "unknown_providers": list(self._unknown_providers),
            "verified_operations": sorted(self._verified_operations()),
            "probes": [
                {"provider": item.provider, "ok": item.ok, "detail": item.detail} for item in probes
            ],
            "probed": probe,
        }

    def dependencies(self) -> tuple[MarketDependency, ...]:
        """Every integration and whether it is present, with the honest detail."""
        return (
            MarketDependency(
                "configuration",
                True,
                not self._config_error,
                self._config_error or "validated configuration is loaded",
            ),
            MarketDependency(
                "architecture",
                True,
                not self._architecture_problems,
                "; ".join(self._architecture_problems)
                or "domain, storage and horizon declarations are consistent",
            ),
            MarketDependency(
                "contract_version",
                True,
                self._config.contract_version == MARKET_CONTRACT_VERSION,
                f"config speaks {self._config.contract_version}; "
                f"build speaks {MARKET_CONTRACT_VERSION}",
            ),
            MarketDependency(
                "event_bus",
                False,
                self._publisher.attached,
                "lifecycle events will be published"
                if self._publisher.attached
                else "no bus attached; lifecycle events will not be published",
            ),
            MarketDependency(
                "permission_manager",
                False,
                self._permissions is not None,
                "granted operations will be declared"
                if self._permissions is not None
                else "no shared permission layer; the subsystem's own gate still applies",
            ),
            MarketDependency(
                "capability_registry",
                False,
                self._capability_registry is not None,
                "capabilities will be registered"
                if self._capability_registry is not None
                else "no capability registry; the local table still answers",
            ),
            MarketDependency(
                "state_store",
                False,
                self._store is not None,
                "configuration will persist"
                if self._store is not None
                else "configuration is memory-only",
            ),
            MarketDependency(
                "market_data_store",
                False,
                self._market_store is not None,
                (
                    "append-only market-data repository is open"
                    if self._market_store is not None
                    else "market-data repository is not initialised"
                ),
            ),
            MarketDependency(
                "market_data_providers",
                False,
                bool(self._market_engine and self._market_engine.enabled_providers),
                (
                    "enabled: " + ", ".join(self._market_engine.enabled_providers)
                    if self._market_engine and self._market_engine.enabled_providers
                    else "no installed provider is enabled; nothing can be collected"
                ),
            ),
        )

    def health(self) -> MarketHealth:
        """Compute health from records already held — never a fresh disk probe."""
        reasons: list[str] = []
        if self._config_error:
            reasons.append(self._config_error)
            return MarketHealth(HealthState.FAILING, tuple(reasons), self._clock())
        if self._architecture_problems:
            reasons.extend(self._architecture_problems)
            return MarketHealth(HealthState.FAILING, tuple(reasons), self._clock())

        state = self._lifecycle.state
        if state is LifecycleState.ERROR:
            reasons.append(self._last_error or "initialization failed")
            return MarketHealth(HealthState.FAILING, tuple(reasons), self._clock())
        if state is LifecycleState.DEGRADED:
            reasons.extend(self._health_reasons or ("subsystem is degraded",))
            return MarketHealth(HealthState.DEGRADED, tuple(reasons), self._clock())
        if state in (LifecycleState.DISABLED, LifecycleState.SHUTTING_DOWN):
            reasons.append(f"subsystem is {state.value}")
            return MarketHealth(HealthState.SKIPPED, tuple(reasons), self._clock())
        if state is LifecycleState.INITIALIZING:
            reasons.append("initialization is in progress")
            return MarketHealth(HealthState.UNKNOWN, tuple(reasons), self._clock())

        if self._last_persist_error:
            reasons.append("configuration could not be persisted: " + self._last_persist_error)
            return MarketHealth(HealthState.DEGRADED, tuple(reasons), self._clock())
        if self._market_ingestion_error:
            reasons.append("background ingestion: " + self._market_ingestion_error)
            return MarketHealth(HealthState.DEGRADED, tuple(reasons), self._clock())
        if self._market_engine is not None:
            engine_reasons = self._market_engine.health_reasons()
            if engine_reasons:
                reasons.extend(engine_reasons)
                return MarketHealth(HealthState.DEGRADED, tuple(reasons), self._clock())

        missing = [
            dependency.name
            for dependency in self.dependencies()
            if not dependency.required and not dependency.available
        ]
        if missing:
            reasons.append("optional integration(s) unavailable: " + ", ".join(missing))
            return MarketHealth(HealthState.DEGRADED, tuple(reasons), self._clock())
        return MarketHealth(HealthState.OK, (), self._clock())

    # -- lifecycle ------------------------------------------------------------

    async def initialize(self, *, reason: str = "") -> dict[str, Any]:
        """Validate, register and reach READY — or ERROR, never a partial READY.

        Idempotent from READY/RUNNING/DEGRADED. From ERROR it first withdraws
        whatever a failed attempt registered, so a retry cannot duplicate a
        capability, permission declaration or subscription.
        """
        if self._lifecycle.state in (
            LifecycleState.READY,
            LifecycleState.RUNNING,
            LifecycleState.DEGRADED,
        ):
            return self.status()
        if self._lifecycle.state is LifecycleState.INITIALIZING:
            raise LifecycleError("NovaMarket initialization is already in progress")

        await self._withdraw()
        previous_health = self._health_state.value
        self._lifecycle.transition(LifecycleState.INITIALIZING, reason=reason or "initialize")
        await self._publish(
            EventType.NOVAMARKET_INITIALIZING,
            contract_version=MARKET_CONTRACT_VERSION,
        )
        try:
            await self._validate_mandatory()
            capability_ids = self._register_capabilities()
            self._declared_permissions = self._permission_model.declare(self._permissions)
            # A successful initialisation clears the previous attempt's failure:
            # an ERROR that recovered must not keep reporting as its last error.
            self._last_error = ""
            self._health_reasons = ()
            self._initialized_at = self._clock()
            self._counters["initializations"] += 1
            self._lifecycle.transition(LifecycleState.READY, reason="checks passed")
            self._set_health_from_records()
            await self._publish(
                EventType.NOVAMARKET_INITIALIZED,
                contract_version=MARKET_CONTRACT_VERSION,
                capabilities=tuple(capability_ids),
            )
            await self._publish(
                EventType.NOVAMARKET_CAPABILITIES_REGISTERED,
                capabilities=tuple(capability_ids),
                count=len(capability_ids),
            )
            await self._publish(
                EventType.NOVAMARKET_READY,
                lifecycle=LifecycleState.READY.value,
            )
            await self._announce_health(previous_health)
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._counters["errors"] += 1
            await self._withdraw()
            self._lifecycle.transition(LifecycleState.ERROR, reason=self._last_error)
            self._health_state = HealthState.FAILING
            self._health_reasons = (self._last_error,)
            _LOGGER.warning("NovaMarket initialization failed: %s", self._last_error)
            await self._publish(
                EventType.NOVAMARKET_ERROR,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            await self._announce_health(previous_health)
            raise NovaMarketInitializationError(f"NovaMarket initialization failed: {exc}") from exc
        return self.status()

    async def enable(self, *, reason: str = "") -> dict[str, Any]:
        """Reach RUNNING (or DEGRADED when the bus is unavailable)."""
        if not self._config.enabled:
            self._config = self._config.with_changes(enabled=True)
            self.persist()
        if self._lifecycle.state in (LifecycleState.DISABLED, LifecycleState.ERROR):
            await self.initialize(reason=reason or "enable")
        state = self._lifecycle.state
        if state is LifecycleState.RUNNING:
            return self.status()
        if state is LifecycleState.DEGRADED:
            await self._subscribe()
            await self._start_ingestion_if_configured()
            if self._subscriptions:
                previous_health = self._health_state.value
                self._lifecycle.transition(LifecycleState.RUNNING, reason="bus attached")
                self._set_health_from_records()
                self._counters["enables"] += 1
                await self._publish(
                    EventType.NOVAMARKET_ENABLED,
                    lifecycle=LifecycleState.RUNNING.value,
                )
                await self._announce_health(previous_health)
            return self.status()
        if state is not LifecycleState.READY:
            raise NotOperationalError(f"cannot enable NovaMarket from {state.value}")

        previous_health = self._health_state.value
        await self._subscribe()
        self._started_at = self._clock()
        if self._subscriptions:
            self._lifecycle.transition(LifecycleState.RUNNING, reason=reason or "enable")
        else:
            self._health_reasons = (
                "no event bus is attached; lifecycle is READY but the control "
                "channel is unavailable",
            )
            self._lifecycle.transition(
                LifecycleState.DEGRADED,
                reason="no event bus attached",
            )
        await self._start_ingestion_if_configured()
        self._counters["enables"] += 1
        self._set_health_from_records()
        if self._lifecycle.state is LifecycleState.RUNNING:
            await self._publish(
                EventType.NOVAMARKET_ENABLED,
                lifecycle=LifecycleState.RUNNING.value,
            )
        else:
            await self._publish(
                EventType.NOVAMARKET_WARNING,
                code="event_bus_unavailable",
                detail="NovaMarket is degraded: no event bus is attached",
            )
        await self._announce_health(previous_health)
        return self.status()

    async def disable(self, *, reason: str = "") -> dict[str, Any]:
        """Withdraw everything this subsystem contributed and stand down."""
        if self._autonomy_runtime is not None:
            try:
                self._autonomy_runtime.stop(reason=reason or "NovaMarket disabled")
            except Exception as exc:  # noqa: BLE001 - disable must finish
                self._autonomy_error = f"{type(exc).__name__}: {exc}"
        self._autonomy_monitoring = None
        if self._config.enabled:
            self._config = self._config.with_changes(enabled=False)
            self.persist()
        if self._lifecycle.state is LifecycleState.DISABLED:
            self._lifecycle.transition(LifecycleState.DISABLED, reason=reason)
            return self.status()

        previous_health = self._health_state.value
        await self._withdraw()
        self._started_at = None
        reason_text = reason or "disabled by operator"
        self._lifecycle.transition(LifecycleState.DISABLED, reason=reason_text)
        self._health_state = HealthState.SKIPPED
        self._health_reasons = (f"subsystem disabled: {reason_text}",)
        self._counters["disables"] += 1
        await self._publish(
            EventType.NOVAMARKET_DISABLED,
            reason=reason_text,
        )
        await self._announce_health(previous_health)
        return self.status()

    async def recover(self, *, reason: str = "") -> dict[str, Any]:
        """Return from DEGRADED to RUNNING once the missing integration exists."""
        if self._lifecycle.state is not LifecycleState.DEGRADED:
            raise NotOperationalError(f"NovaMarket is {self._lifecycle.state.value}, not degraded")
        previous_health = self._health_state.value
        await self._subscribe()
        if not self._subscriptions:
            raise NovaMarketError("NovaMarket cannot recover: the event bus is still unavailable")
        self._health_reasons = ()
        self._lifecycle.transition(LifecycleState.RUNNING, reason=reason or "recovered")
        self._set_health_from_records()
        await self._publish(
            EventType.NOVAMARKET_ENABLED,
            lifecycle=LifecycleState.RUNNING.value,
        )
        await self._announce_health(previous_health)
        return self.status()

    async def reinitialize(self, *, reason: str = "") -> dict[str, Any]:
        """Fully withdraw, initialize again, and restore the prior operational state.

        The withdraw step is the point: re-running initialization without it is
        how a subsystem ends up with two subscriptions and a duplicate
        capability registration. "Was enabled" is read from the LIFECYCLE, not
        from ``config.enabled`` (which defaults to true): a never-started
        subsystem given an explicit reinitialize must not silently start.
        """
        was_enabled = self._lifecycle.state in (
            LifecycleState.READY,
            LifecycleState.RUNNING,
            LifecycleState.DEGRADED,
        )
        if self._lifecycle.state is not LifecycleState.DISABLED:
            await self.disable(reason=reason or "reinitializing")
        try:
            await self.initialize(reason=reason or "reinitialize")
        except Exception:
            # ``disable`` persists enabled=false. If the operator's choice was
            # ENABLED, a failed re-validation must not silently switch the
            # subsystem off for the next boot: restore the choice, leave the
            # failure visible as ERROR, and let auto-start retry the checks.
            if was_enabled:
                self._config = self._config.with_changes(enabled=True)
                self.persist()
            raise
        if was_enabled:
            await self.enable(reason=reason or "reinitialize")
        else:
            # A disabled subsystem stays disabled: reinitialisation validates it,
            # it does not quietly switch the operator's choice back on.
            await self.disable(reason="remains disabled after reinitialization")
        self._counters["reinitializations"] += 1
        return self.status()

    async def shutdown(self, *, reason: str = "") -> dict[str, Any]:
        """Orderly stop. Idempotent, and never changes the operator's enabled flag."""
        await self.stop_runtime()
        if self._lifecycle.state in (
            LifecycleState.DISABLED,
            LifecycleState.SHUTTING_DOWN,
        ):
            # A service that only served read-only calls still opened lazily
            # created stores; withdraw closes them, so shutdown never leaves
            # SQLite file handles open (Windows refuses to unlink those).
            try:
                await self._withdraw()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket withdraw on a stopped service failed: %s", exc)
            return self.status()
        previous_health = self._health_state.value
        self._lifecycle.transition(LifecycleState.SHUTTING_DOWN, reason=reason or "shutdown")
        await self._withdraw()
        self._started_at = None
        self._lifecycle.transition(LifecycleState.DISABLED, reason=reason or "shutdown")
        self._health_state = HealthState.SKIPPED
        self._health_reasons = ("subsystem shut down",)
        await self._publish(
            EventType.NOVAMARKET_SHUTDOWN,
            reason=reason or "shutdown",
        )
        await self._announce_health(previous_health)
        return self.status()

    # -- continuous runtime and operator notifications -----------------------

    def market_session(self) -> dict[str, Any]:
        """The verified NSE/BSE session, independent of NovaMarket's lifecycle."""
        return market_session_snapshot(self._clock())

    def notifications_status(self) -> dict[str, Any]:
        return self._notification_center.status()

    async def send_test_notification(self) -> dict[str, Any]:
        return await self._notification_center.send_test()

    def runtime_status(self) -> dict[str, Any]:
        task = self._runtime_task
        return {
            "task_running": bool(task is not None and not task.done()),
            "interval_seconds": self._runtime_interval_seconds,
            "paused": self._config.paused,
            "last_tick": (self._runtime_last_tick.isoformat() if self._runtime_last_tick else None),
            "last_event_refresh": (
                self._runtime_last_event_refresh.isoformat()
                if self._runtime_last_event_refresh
                else None
            ),
            "last_position_refresh": (
                self._runtime_last_position_refresh.isoformat()
                if self._runtime_last_position_refresh
                else None
            ),
            "last_error": self._runtime_error,
            "refresh_plan": plan_market_refresh(self._clock()).to_dict(),
        }

    def start_runtime(self) -> dict[str, Any]:
        """Start the one continuous monitor on the running event loop.

        Pause is a configuration state, not a task cancellation: the monitor
        keeps its place and resumes without restarting the subsystem. Stop
        (disable) leaves the task running but idle, so the API remains able to
        accept the next enable.

        AT-01 opens the autonomy store here, and the integrated runtime then
        starts the persisted application-start mode.  A previous operator stop
        has set ``auto_start=False``, so an emergency halt survives a restart;
        a graceful application shutdown preserves ``auto_start=True``.
        """
        try:
            self._ensure_monitoring()
        except Exception as exc:  # noqa: BLE001 - a store failure must not stop NovaControl
            self._autonomy_error = f"{type(exc).__name__}: {exc}"
        try:
            self._auto_start_autonomy()
        except Exception as exc:  # noqa: BLE001 - auto-start must not stop NovaControl
            self._autonomy_error = f"auto-start: {type(exc).__name__}: {exc}"
            if self._autonomy_runtime is not None:
                self._autonomy_runtime.fail(self._autonomy_error)
        task = self._runtime_task
        if task is not None and not task.done():
            return self.runtime_status()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return self.runtime_status()
        self._runtime_interval_seconds = max(1, plan_market_refresh(self._clock()).interval_seconds)
        self._runtime_task = loop.create_task(self._run_runtime(), name="novamarket-runtime")
        self._start_autonomy_heartbeat()
        # PI-09: recover and run durable forecast checkpoints on the same
        # event loop, without making the autonomy runtime a forecast scheduler.
        try:
            scheduler = self.forecast_scheduler_service()
            self._forecast_auto_plan = self._auto_schedule_forecast_cycles(scheduler)
        except Exception as exc:  # noqa: BLE001 - a missing forecast stack is reported
            self._forecast_scheduler_error = f"{type(exc).__name__}: {exc}"
        return self.runtime_status()

    def _auto_start_autonomy(self) -> dict[str, Any]:
        """Start the persisted paper mode after the application's dependencies.

        Auto-start never runs if the operator halted the runtime, another live
        owner holds the lease, or a critical pipeline cannot be constructed.
        It does not submit an order: it only enables the mode whose existing
        gates still require NM-08 approval and NM-10 execution.
        """

        runtime = self._ensure_autonomy()
        config = runtime.config
        snapshot = runtime.status()
        if not self._config.enabled or self._lifecycle.state not in {
            LifecycleState.RUNNING,
            LifecycleState.DEGRADED,
        }:
            self._clear_autonomy_auto_start_retry()
            return snapshot
        if not config.auto_start:
            self._clear_autonomy_auto_start_retry()
            return snapshot
        if str(snapshot.get("mode")) != "disabled":
            self._clear_autonomy_auto_start_retry()
            return snapshot
        lease = snapshot.get("lease")
        if isinstance(lease, dict) and lease.get("foreign"):
            # A process that died uncleanly leaves its lease live in the store
            # for up to ``lease_seconds``.  Do not steal it from a genuinely
            # live foreign owner, and do not give up either: retry until the
            # abandoned lease must have expired.
            if self._autonomy_auto_start_retry_until is None:
                self._autonomy_auto_start_retry_until = self._clock() + timedelta(
                    seconds=runtime.lease_seconds
                    + self._autonomy_auto_start_retry_grace_seconds(),
                )
            return snapshot
        self._clear_autonomy_auto_start_retry()
        # Construct the critical pipelines before the mode becomes live, so a
        # broken dependency fails closed instead of leaving a half-started
        # runtime that the tick could later drive.
        try:
            self._ensure_decision()
            self._ensure_autonomy_execution()
            self._ensure_autonomy_exits()
        except Exception as exc:
            error = f"auto-start dependency failed: {type(exc).__name__}: {exc}"
            self._autonomy_error = error
            self._clear_autonomy_auto_start_retry()
            runtime.fail(error)
            return runtime.status()
        started = runtime.start(
            config.auto_start_mode,
            reason="application start",
            acknowledge_paper_only=config.auto_start_mode == "autonomous",
        )
        self._autonomy_error = ""
        self._clear_autonomy_auto_start_retry()
        return started

    def _autonomy_auto_start_retry_grace_seconds(self) -> float:
        """How long past lease expiry to keep checking before giving up."""

        runtime = self._autonomy_runtime
        lease_seconds = runtime.lease_seconds if runtime is not None else 60
        return max(2.0, float(lease_seconds) / 2.0)

    def _clear_autonomy_auto_start_retry(self) -> None:
        self._autonomy_auto_start_retry_until = None

    def _retry_autonomy_auto_start(self) -> None:
        """Retry a deferred application-start mode after a stale lease expires.

        A conflict is acknowledged, not stolen.  While a different live owner
        keeps renewing, the retry stops at its deadline; if that owner was an
        earlier process that died, its lease cannot be renewed and this
        process acquires it on the next retry.
        """

        deadline = self._autonomy_auto_start_retry_until
        if deadline is None:
            return
        if self._clock() >= deadline:
            self._clear_autonomy_auto_start_retry()
            return
        runtime = self._autonomy_runtime
        if runtime is None or runtime.closed:
            self._clear_autonomy_auto_start_retry()
            return
        started = self._auto_start_autonomy()
        if str(started.get("mode")) != "disabled":
            self._clear_autonomy_auto_start_retry()

    async def _autonomy_startup_reconcile(self) -> dict[str, Any]:
        """Reconcile durable AT-04/AT-05 work before the first trading tick.

        Recovery never replays an unfinished order.  If anything remains
        unresolved, the runtime is paused so new entries stop while protective
        exits and reconciliation remain available.
        """

        runtime = self._autonomy_runtime
        if runtime is None or runtime.closed:
            return {"status": "skipped", "reason": "runtime_unavailable"}
        snapshot = runtime.status()
        if str(snapshot.get("mode")) not in {"monitoring", "autonomous", "paused"}:
            return {"status": "skipped", "reason": "runtime_inactive"}
        unresolved: list[str] = []
        try:
            execution = await self._ensure_autonomy_execution().recover()
            unresolved.extend(str(item) for item in execution.get("unresolved") or ())
        except Exception as exc:  # noqa: BLE001 - fail closed, report the reason
            self._autonomy_execution_error = f"startup recovery: {type(exc).__name__}: {exc}"
            unresolved.append("execution_recovery_failed")
        try:
            exits = await self._ensure_autonomy_exits().recover()
            unresolved.extend(str(item) for item in exits.get("unresolved") or ())
        except Exception as exc:  # noqa: BLE001 - fail closed, report the reason
            self._autonomy_exits_error = f"startup recovery: {type(exc).__name__}: {exc}"
            unresolved.append("exit_recovery_failed")
        if unresolved and str(runtime.status().get("mode")) in {"monitoring", "autonomous"}:
            message = (
                "startup reconciliation left unresolved work: "
                + ", ".join(sorted(set(unresolved)))
            )
            self._autonomy_error = message
            runtime.pause(reason=message)
        return {
            "status": "completed" if not unresolved else "degraded",
            "unresolved": sorted(set(unresolved)),
            "unresolved_count": len(set(unresolved)),
            "paused": str(runtime.status().get("mode")) == "paused",
        }

    async def stop_runtime(self) -> dict[str, Any]:
        self._clear_autonomy_auto_start_retry()
        scheduler = self._forecast_scheduler
        self._forecast_scheduler = None
        if scheduler is not None:
            try:
                await scheduler.stop()
            except Exception as exc:  # noqa: BLE001 - shutdown must finish
                self._forecast_scheduler_error = f"stop: {type(exc).__name__}: {exc}"
        heartbeat = self._autonomy_heartbeat_task
        self._autonomy_heartbeat_task = None
        if heartbeat is not None and not heartbeat.done():
            heartbeat.cancel()
            try:
                await heartbeat
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - stop must finish
                pass
        if self._autonomy_runtime is not None:
            try:
                # Graceful shutdown preserves the next-launch auto-start opt-in;
                # only an operator stop is the persistent kill switch.
                self._autonomy_runtime.stop(reason="service runtime stopped")
            except Exception as exc:  # noqa: BLE001 - shutdown must finish
                self._autonomy_error = f"{type(exc).__name__}: {exc}"
        task = self._runtime_task
        self._runtime_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - stop must finish
                pass
        return self.runtime_status()

    def _start_autonomy_heartbeat(self) -> None:
        """Keep a started autonomy lease alive without a market-data cycle."""

        if self._autonomy_heartbeat_task is not None and not self._autonomy_heartbeat_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._autonomy_heartbeat_task = loop.create_task(
            self._run_autonomy_heartbeat(), name="novamarket-autonomy-heartbeat"
        )

    def _autonomy_heartbeat_interval_seconds(self) -> float:
        runtime = self._autonomy_runtime
        lease = runtime.lease_seconds if runtime is not None else 60
        # Three renewals per lease; a delayed event loop still leaves margin.
        return max(1.0, lease / 3.0)

    async def _run_autonomy_heartbeat(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._autonomy_heartbeat_interval_seconds())
                runtime = self._autonomy_runtime
                if runtime is None or runtime.closed:
                    continue
                # The heartbeat is also the one place that can retry a deferred
                # auto-start: it wakes on a fixed cadence while the runtime is
                # up and costs one cheap status read per lease interval.
                try:
                    self._retry_autonomy_auto_start()
                except Exception as exc:  # noqa: BLE001 - retry must not stop renewal
                    self._autonomy_error = f"auto-start retry: {type(exc).__name__}: {exc}"
                try:
                    runtime.heartbeat()
                except Exception as exc:  # noqa: BLE001 - report, never crash the app
                    self._autonomy_error = f"heartbeat: {type(exc).__name__}: {exc}"
        except asyncio.CancelledError:
            return

    async def _run_runtime(self) -> None:
        # Reconcile durable work before any new entry can be evaluated, then
        # run the first market tick immediately: a restart must evaluate its
        # startup state without waiting a full market interval.  Subsequent
        # ticks keep the established cadence.
        try:
            await self._autonomy_startup_reconcile()
        except Exception as exc:  # noqa: BLE001 - recovery must not stop the monitor
            self._autonomy_error = f"startup reconciliation: {type(exc).__name__}: {exc}"
            runtime = self._autonomy_runtime
            if runtime is not None and not runtime.closed:
                try:
                    runtime.pause(reason=self._autonomy_error)
                except Exception:
                    runtime.fail(self._autonomy_error)
        try:
            while True:
                plan = plan_market_refresh(self._clock())
                self._runtime_interval_seconds = max(1, plan.interval_seconds)
                await self._runtime_tick()
                await asyncio.sleep(self._runtime_interval_seconds)
        except asyncio.CancelledError:
            return

    async def _runtime_tick(self) -> None:
        self._runtime_last_tick = self._clock()
        if self._lifecycle.state not in (LifecycleState.RUNNING, LifecycleState.DEGRADED):
            return
        if self._config.paused:
            return
        scheduler = self._forecast_scheduler
        if scheduler is not None and scheduler.config.auto_schedule:
            try:
                self._forecast_auto_plan = self._auto_schedule_forecast_cycles(scheduler)
            except Exception as exc:  # noqa: BLE001 - planning must not stop the monitor
                self._forecast_scheduler_error = f"auto-plan: {type(exc).__name__}: {exc}"
        try:
            await self._notification_center.tick()
        except Exception as exc:  # noqa: BLE001 - a monitor must keep running
            self._runtime_error = f"notifications: {type(exc).__name__}: {exc}"

        now = self._clock()
        if self._radar_engine is not None and self._due(self._runtime_last_event_refresh, now, 300):
            try:
                await self.radar_refresh()
                self._runtime_last_event_refresh = now
                self._runtime_error = ""
            except Exception as exc:  # noqa: BLE001 - a feed failure is not a crash
                self._runtime_error = f"events: {type(exc).__name__}: {exc}"

        if self._market_engine is not None and self._due(
            self._runtime_last_position_refresh, now, 300
        ):
            try:
                holdings = await asyncio.to_thread(self._held_instruments)
                if holdings:
                    await self.ingest_market_data(
                        instrument_ids=holdings, intervals=["1d"], trigger="runtime"
                    )
                    self._runtime_last_position_refresh = now
            except Exception as exc:  # noqa: BLE001 - one provider outage is reported
                self._runtime_error = f"positions: {type(exc).__name__}: {exc}"

        # AT-02: the one monitoring runner is hosted by this same runtime tick;
        # there is no second scheduler, and a cycle that refuses or fails is
        # reported through the autonomy status rather than raised here.
        try:
            monitoring = self._autonomy_monitoring
            if monitoring is not None and not monitoring.closed:
                outcome = await monitoring.maybe_run()
                if outcome.get("status") == "failed":
                    self._autonomy_error = str(outcome.get("error") or "a monitoring cycle failed")
        except Exception as exc:  # noqa: BLE001 - monitoring must not stop the monitor
            self._autonomy_error = f"monitoring: {type(exc).__name__}: {exc}"

        # AT-03: the decision pipeline is hosted by this same tick, after AT-02.
        # It consumes one completed monitoring cycle and never submits an order.
        try:
            decision = self._decision_pipeline
            if decision is None and self._autonomy_runtime is not None:
                decision = self._ensure_decision()
            if decision is not None and not decision.closed:
                outcome = await decision.maybe_run()
                if outcome.get("status") == "refused":
                    self._decision_error = str(
                        outcome.get("message") or outcome.get("reason") or "refused"
                    )
        except Exception as exc:  # noqa: BLE001 - a decision failure must not stop the monitor
            self._decision_error = f"decision: {type(exc).__name__}: {exc}"

        # AT-04: forward only AT-03 risk-approved proposals, through NM-10.
        # The AT-04 pipeline re-checks autonomous mode, the lease and its
        # one-cycle lock; this tick only hosts it after the decision stage.
        try:
            execution = self._autonomy_execution
            if execution is None and self._autonomy_runtime is not None:
                execution = self._ensure_autonomy_execution()
            if execution is not None and not execution.closed:
                outcome = await execution.maybe_run()
                if outcome.get("status") == "failed":
                    self._autonomy_execution_error = str(
                        outcome.get("message") or "an AT-04 execution cycle failed"
                    )
        except Exception as exc:  # noqa: BLE001 - execution must not stop the monitor
            self._autonomy_execution_error = f"execution: {type(exc).__name__}: {exc}"

        # AT-05: protective exits are supervised by this same tick. This stage
        # runs in paused mode too: paused blocks NEW ENTRIES in AT-03/AT-04, not
        # protective exits or reconciliation of an existing position.
        try:
            exits = self._autonomy_exits
            if exits is None and self._autonomy_runtime is not None:
                exits = self._ensure_autonomy_exits()
            if exits is not None and not exits.closed:
                outcome = await exits.run_cycle(trigger="runtime")
                if outcome.get("status") == "failed":
                    self._autonomy_exits_error = str(
                        outcome.get("error") or "an AT-05 exit cycle failed"
                    )
        except Exception as exc:  # noqa: BLE001 - exits must not stop the monitor
            self._autonomy_exits_error = f"exits: {type(exc).__name__}: {exc}"

        # PI-21: scheduled portfolio reviews are hosted by this same tick and
        # by the same runtime lease.  The coordinator owns its own cadence and
        # refuses on its own authority, so this stage only asks it once per
        # tick; it never starts a timer or a second scheduler.
        try:
            coordinator = self._portfolio_management_service
            if coordinator is None and self._autonomy_runtime is not None:
                snapshot = self._autonomy_runtime.status()
                lease = snapshot.get("lease")
                lease_valid = (
                    bool(lease.get("valid")) if isinstance(lease, dict) else False
                )
                if str(snapshot.get("mode")) in {"monitoring", "autonomous"} and lease_valid:
                    coordinator = self.portfolio_management_service()
            if coordinator is not None:
                outcome = await coordinator.maybe_run()
                if outcome.get("status") in {"failed", "refused"}:
                    self._portfolio_management_scheduler_error = str(
                        outcome.get("error")
                        or outcome.get("reason")
                        or "a scheduled portfolio review did not complete"
                    )
                elif outcome.get("status") in {"completed", "not_due"}:
                    self._portfolio_management_scheduler_error = ""
        except Exception as exc:  # noqa: BLE001 - a review must not stop the monitor
            self._portfolio_management_scheduler_error = (
                f"scheduled review: {type(exc).__name__}: {exc}"
            )

        # PI-12: measure outcomes after execution/exits have updated the target
        # period.  A missing bar leaves the decision pending; no result is
        # fabricated and no model or policy is promoted.
        if self._decision_pipeline is not None and not self._decision_pipeline.closed:
            try:
                self.track_prediction_outcomes(limit=25)
            except Exception as exc:  # noqa: BLE001 - outcomes must not stop the monitor
                self._prediction_outcome_error = f"outcomes: {type(exc).__name__}: {exc}"

    def _held_instruments(self) -> list[str]:
        found: list[str] = []
        listed = self.portfolios_list(limit=200)
        for row in listed.get("portfolios") or []:
            portfolio_id = str(row.get("portfolio_id") or "")
            if not portfolio_id:
                continue
            try:
                detail = self.portfolio_detail(portfolio_id)
            except Exception:  # noqa: BLE001
                continue
            for holding in detail.get("holdings") or []:
                instrument = str(holding.get("instrument_id") or "")
                if instrument and instrument not in found:
                    found.append(instrument)
        return found

    @staticmethod
    def _due(last: datetime | None, now: datetime, seconds: int) -> bool:
        return last is None or (now - last).total_seconds() >= seconds

    async def sync_market_universe(self, *, include_bse: bool = True) -> dict[str, Any]:
        """Register the public NSE/BSE equity catalogue in one bounded pass.

        The source reader is run in a worker thread; the store is written on
        the event loop. Nothing is registered until the source has produced a
        validated plan, so a typo in the upstream CSV cannot half-fill the book.
        """
        engine = self._market_data_engine()
        try:
            plan = await asyncio.to_thread(build_indian_universe, include_bse=include_bse)
        except Exception as exc:  # noqa: BLE001 - the route names the failure
            raise MarketDataError(
                f"NSE/BSE universe could not be read: {type(exc).__name__}: {exc}"
            ) from exc

        registered = 0
        failed: list[str] = []
        for member in plan.members:
            try:
                instrument = Instrument.create(
                    symbol=member.symbol,
                    name=member.name,
                    exchange=member.exchange,
                    mic=member.mic,
                    asset_class="equity",
                    currency="INR",
                    timezone="Asia/Kolkata",
                    provider_symbols={"yahoo": member.provider_symbol},
                    metadata={
                        "universe_source": member.source,
                        "country": "IN",
                    },
                )
                engine.register_instrument(instrument)
                registered += 1
            except Exception as exc:  # noqa: BLE001 - report the row, keep the rest
                failed.append(f"{member.mic}:{member.symbol}: {type(exc).__name__}: {exc}")
        return {
            "ok": True,
            "coverage": plan.coverage,
            "nse": plan.nse_count,
            "bse": plan.bse_count,
            "bse_only": plan.bse_only_count,
            "total": len(plan.members),
            "registered": registered,
            "failed": len(failed),
            "errors": list(plan.errors) + failed[:20],
        }

    async def auto_start(self) -> dict[str, Any]:
        """Start at runtime boot WITHOUT letting a failure stop the application.

        This is the isolation seam: a NovaMarket misconfiguration or dependency
        refusal leaves the subsystem in ERROR, publishes why, and returns — the
        runtime's other modules start normally.
        """
        if self._config_error:
            try:
                await self.initialize(reason="runtime start")
            except Exception:  # noqa: BLE001 - a subsystem must not stop the app
                pass
            return self.status()
        if not self._config.enabled:
            return self.status()
        try:
            await self.enable(reason="runtime start")
        except Exception as exc:  # noqa: BLE001 - a subsystem must not stop the app
            self._last_error = f"{type(exc).__name__}: {exc}"
            _LOGGER.warning("NovaMarket did not auto-start: %s", self._last_error)
        return self.status()

    # -- configuration --------------------------------------------------------

    async def update_config(
        self, payload: Mapping[str, Any], *, reason: str = ""
    ) -> dict[str, Any]:
        """Validate and apply a configuration change, then follow enabledness."""
        if not isinstance(payload, Mapping):
            raise ConfigurationError("configuration must be an object")
        # A partial payload changes only the fields it names: the rest come from
        # the CURRENT configuration, so posting one limit cannot wipe a provider
        # reference. Unknown fields are still refused by ``from_mapping``.
        merged: dict[str, Any] = self._config.to_dict()
        merged.update(dict(payload))
        if isinstance(payload.get("resources"), Mapping):
            nested = dict(self._config.resources.to_dict())
            nested.update(dict(payload["resources"]))
            merged["resources"] = nested
        new_config = NovaMarketConfig.from_mapping(merged)
        changed = self._config.diff(new_config)
        self._config = new_config
        self._config_error = ""
        self._publisher.emit_enabled = new_config.emit_events
        self._apply_log_level()
        self.persist()
        if self._market_engine is not None:
            try:
                self._reconfigure_market_data()
            except MarketDataError as exc:
                raise ConfigurationError(f"market-data configuration was rejected: {exc}") from exc
        self._counters["configuration_updates"] += 1
        await self._publish(
            EventType.NOVAMARKET_CONFIGURATION_CHANGED,
            changed=tuple(changed),
        )

        # A configuration edit does not silently switch the subsystem on: an
        # explicit switch-on is the "enabled" field changing, or a repair of the
        # ERROR state that was blocking an already-enabled configuration.
        state = self._lifecycle.state
        should_enable = new_config.enabled and (
            state is LifecycleState.ERROR
            or (state is LifecycleState.DISABLED and "enabled" in changed)
        )
        if should_enable:
            await self.enable(reason=reason or "configuration enabled")
        elif not new_config.enabled and state not in (
            LifecycleState.DISABLED,
            LifecycleState.SHUTTING_DOWN,
        ):
            await self.disable(reason=reason or "configuration disabled")
        elif ("ingestion" in changed or "gateway" in changed) and self._market_engine is not None:
            # Turning the general worker or intraday gateway on/off is a
            # configuration change, not a lifecycle change.
            await self._start_ingestion_if_configured()

        return {
            "changed": list(changed),
            "config": self._config.to_dict(),
            "reload_required": False,
            "status": self.status(),
        }

    def persist(self) -> None:
        """Write the configuration snapshot; a failed write never raises.

        Persistence is auxiliary to the lifecycle: a store that refuses the
        write must not fail the enable/disable/config operation that produced it.
        The refusal is recorded in ``last_persist_error`` and reflected as a
        degraded health report, so it is observable rather than silent.
        """
        if self._store is None:
            return
        try:
            self._store.write(
                self._namespace,
                {
                    "schema_version": MARKET_SCHEMA_VERSION,
                    "contract_version": MARKET_CONTRACT_VERSION,
                    "updated_at": self._clock().isoformat(),
                    "config": self._config.to_dict(),
                },
            )
        except Exception as exc:  # noqa: BLE001 - a snapshot must not fail the work
            self._last_persist_error = f"{type(exc).__name__}: {exc}"
            self._counters["persist_failures"] += 1
            _LOGGER.warning("NovaMarket configuration was not persisted: %s", exc)
            return
        self._last_persist_error = ""

    # -- NM-14: activity, audit and the integrated workspace ------------------

    def attach_audit_logger(self, audit_logger: Any | None) -> None:
        """Attach the application's ONE local audit logger to NovaMarket.

        The application constructs its audit trail after the subsystem, so this
        is a late binding rather than a second logger. A failure to attach is
        not fatal: activity still works and the workspace reports the audit as
        unavailable instead of pretending it is empty.
        """
        self._audit_logger = audit_logger

    def _summary_value(self, value: Any, *, limit: int = 300) -> Any:
        """Bound one activity/audit payload value without losing its shape."""
        if isinstance(value, str):
            return value[:limit]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, Mapping):
            return {
                str(key): self._summary_value(item, limit=120)
                for key, item in list(value.items())[:8]
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self._summary_value(item, limit=120) for item in list(value)[:8]]
        return str(value)[:limit]

    def _observe_market_event(
        self, event_name: str, correlation_id: str, payload: Mapping[str, Any]
    ) -> None:
        """One observer for every event emitted through the market publisher."""
        self._record_activity(event_name, correlation_id, payload)
        self._record_audit_event(event_name, correlation_id, payload)

    def _record_activity(
        self,
        event_type: EventType | str,
        correlation_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        self._activity_counter += 1
        self._activity.append(
            {
                "id": f"market-event-{self._activity_counter}",
                "type": str(event_type),
                "at": self._clock().isoformat(),
                "correlation_id": correlation_id,
                "payload": self._summary_value(dict(payload)),
            }
        )
        if len(self._activity) > 250:
            del self._activity[: len(self._activity) - 250]

    def _record_audit_event(
        self,
        event_type: EventType | str,
        correlation_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Append a market event to the shared local audit trail.

        An audit failure is recorded and swallowed: the financial work already
        committed, and observability must never turn a successful operation into
        a failed one. The event itself remains on the bus and in local activity.
        """
        logger = self._audit_logger
        if logger is None:
            return
        try:
            name = str(event_type)
            logger.record(
                task_id=correlation_id or f"market-{self._activity_counter}",
                user_request=name,
                outcome="completed",
                source="novamarket",
                route="/market",
                intent=name.split(".")[-1],
                decision={"event": name, "payload": dict(payload)},
                timestamp=self._clock().isoformat(),
            )
            self._last_audit_error = ""
        except Exception as exc:  # noqa: BLE001 - audit cannot fail the operation
            self._last_audit_error = f"{type(exc).__name__}: {exc}"
            _LOGGER.warning("NovaMarket audit record was not written: %s", exc)

    def activity(self, limit: int = 50) -> dict[str, Any]:
        """Newest-first in-memory activity derived from published events."""
        rows = self._activity[-limit:] if limit > 0 else self._activity
        return {
            "events": [dict(row) for row in reversed(rows)],
            "count": len(self._activity),
        }

    def audit_contract(self) -> dict[str, Any]:
        logger = self._audit_logger
        if logger is None:
            return {
                "implemented": False,
                "phase": "NM-14",
                "detail": "the application audit logger has not been attached",
            }
        report = logger.report()
        return {
            "implemented": True,
            "phase": "NM-14",
            "source": "novacontrol.audit",
            "sink": report.get("sink"),
            "local_only": report.get("local_only"),
            "retention": report.get("retention"),
            "redaction_marker": report.get("redaction_marker"),
        }

    def audit_summary(self, limit: int = 20) -> dict[str, Any]:
        logger = self._audit_logger
        if logger is None:
            return {
                "available": False,
                "records": 0,
                "entries": [],
                "detail": "the application audit logger has not been attached",
            }
        try:
            rows = [
                record.to_dict()
                for record in logger.tail(1000)
                if str(getattr(record, "source", "")) == "novamarket"
            ]
            report = logger.report()
        except Exception as exc:  # noqa: BLE001 - a read must not break the workspace
            return {
                "available": False,
                "records": 0,
                "entries": [],
                "detail": f"{type(exc).__name__}: {exc}",
            }
        return {
            "available": True,
            "records": len(rows),
            "entries": rows[-limit:][::-1] if limit > 0 else rows[::-1],
            "sink": report.get("sink"),
            "local_only": report.get("local_only"),
            "retention": report.get("retention"),
            "last_error": self._last_audit_error,
        }

    def _portfolio_summary(self) -> dict[str, Any]:
        """Aggregate the authoritative portfolio valuations for the dashboard.

        Individual portfolio returns and totals come from ``portfolio_detail``;
        this method only adds those already-computed values together. It never
        re-prices a position or derives a return from prices of its own.
        """
        total_value = Decimal("0.00")
        invested = Decimal("0.00")
        cash = Decimal("0.00")
        realized = Decimal("0.00")
        unrealized = Decimal("0.00")
        weighted_return = Decimal("0.00")
        by_horizon: dict[str, dict[str, Any]] = {}
        rows: list[dict[str, Any]] = []
        try:
            listed = self.portfolios_list(limit=200)
        except Exception as exc:  # noqa: BLE001 - overview reports the reason
            return {
                "available": False,
                "detail": f"{type(exc).__name__}: {exc}",
                "count": 0,
                "by_horizon": [],
            }
        for row in listed.get("portfolios") or []:
            if not isinstance(row, dict) or not row.get("portfolio_id"):
                continue
            try:
                detail = self.portfolio_detail(str(row["portfolio_id"]))
            except Exception:  # noqa: BLE001 - one unreadable book is not all books
                continue
            valuation = detail.get("valuation") or {}
            if not isinstance(valuation, dict):
                continue
            value = Decimal(str(valuation.get("total_value") or "0"))
            book_invested = Decimal(str(valuation.get("invested") or "0"))
            book_cash = Decimal(str(valuation.get("cash") or "0"))
            book_realized = Decimal(str(valuation.get("realized_pnl") or "0"))
            book_unrealized = Decimal(str(valuation.get("unrealized_pnl") or "0"))
            book_return = Decimal(str(valuation.get("return_pct") or "0"))
            total_value += value
            invested += book_invested
            cash += book_cash
            realized += book_realized
            unrealized += book_unrealized
            weighted_return += book_return * value
            horizon = str(row.get("horizon") or "")
            bucket = by_horizon.setdefault(
                horizon,
                {
                    "horizon": horizon,
                    "count": 0,
                    "total_value": Decimal("0.00"),
                    "invested": Decimal("0.00"),
                    "cash": Decimal("0.00"),
                    "realized_pnl": Decimal("0.00"),
                    "unrealized_pnl": Decimal("0.00"),
                    "weighted_return": Decimal("0.00"),
                    "statuses": [],
                },
            )
            bucket["count"] += 1
            bucket["total_value"] += value
            bucket["invested"] += book_invested
            bucket["cash"] += book_cash
            bucket["realized_pnl"] += book_realized
            bucket["unrealized_pnl"] += book_unrealized
            bucket["weighted_return"] += book_return * value
            if row.get("status") not in bucket["statuses"]:
                bucket["statuses"].append(row.get("status"))
            rows.append(
                {
                    "portfolio_id": row.get("portfolio_id"),
                    "name": row.get("name"),
                    "horizon": horizon,
                    "status": row.get("status"),
                    "total_value_inr": money_text(value),
                    "invested_inr": money_text(book_invested),
                    "cash_inr": money_text(book_cash),
                    "pnl_inr": money_text(book_realized + book_unrealized),
                    "return_pct": money_text(book_return),
                    "holdings": valuation.get("holdings", 0),
                    "stale_positions": valuation.get("stale_positions", 0),
                }
            )
        pnl = realized + unrealized
        return_pct = (
            (weighted_return / total_value).quantize(Decimal("0.01"))
            if total_value > 0
            else Decimal("0.00")
        )
        exposure_pct = (
            (invested / total_value * Decimal("100")).quantize(Decimal("0.01"))
            if total_value > 0
            else Decimal("0.00")
        )
        horizons = []
        for item in by_horizon.values():
            value = item["total_value"]
            horizons.append(
                {
                    "horizon": item["horizon"],
                    "count": item["count"],
                    "total_value_inr": money_text(value),
                    "invested_inr": money_text(item["invested"]),
                    "cash_inr": money_text(item["cash"]),
                    "pnl_inr": money_text(item["realized_pnl"] + item["unrealized_pnl"]),
                    "return_pct": (
                        money_text((item["weighted_return"] / value).quantize(Decimal("0.01")))
                        if value > 0
                        else "0.00"
                    ),
                    "statuses": list(item["statuses"]),
                }
            )
        return {
            "available": True,
            "count": len(rows),
            "total_value_inr": money_text(total_value),
            "invested_inr": money_text(invested),
            "cash_inr": money_text(cash),
            "pnl_inr": money_text(pnl),
            "return_pct": money_text(return_pct),
            "exposure_pct": money_text(exposure_pct),
            "by_horizon": horizons,
            "rows": rows,
        }

    def _overview_activity(self, limit: int = 40) -> list[dict[str, Any]]:
        """Newest activity, retaining recent wallet events in the small window.

        Startup radar bursts can fill the newest rows before a wallet deposit is
        made. The overview still keeps a bounded newest-first window, but
        reserves slots for wallet events that would otherwise be pushed out.
        Event dictionaries are returned unchanged.
        """
        events: list[dict[str, Any]] = self.activity(limit=0)["events"]
        if limit <= 0:
            return events
        recent = events[:limit]
        if len(events) <= limit:
            return recent
        recent_ids = {str(row.get("id", "")) for row in recent}
        retained = [
            row
            for row in events
            if str(row.get("type", "")).startswith("novamarket.wallet.")
            and str(row.get("id", "")) not in recent_ids
        ][:limit]
        if not retained:
            return recent
        return recent[: limit - len(retained)] + retained

    def workspace_overview(self) -> dict[str, Any]:
        """One read-only integration view for the NM-14 operational workspace.

        Every value is read from the owning subsystem; nothing here invents a
        count, price or performance figure. Recent lists are deliberately small,
        and a subsystem that is not open contributes its own honest reason
        rather than an empty success payload.
        """
        status = self.status()
        recent_calls: tuple[tuple[str, Callable[[], Any]], ...] = (
            ("wallets", lambda: self.wallets_list(limit=5)),
            ("events", lambda: self.radar_search(limit=5)),
            ("candidates", lambda: self.discovery_candidates(limit=5)),
            ("forecasts", lambda: self.forecast_list(limit=5)),
            ("portfolios", lambda: self.portfolios_list(limit=5)),
            ("risk_decisions", lambda: self.risk_decisions(limit=5)),
            ("strategies", lambda: self.strategies(limit=5)),
            ("paper_orders", lambda: self.paper_orders(limit=5)),
            ("backtests", lambda: self.backtest_list(5)),
            ("shadow_sessions", lambda: self.shadow_sessions(5)),
            ("learning_experiments", lambda: self.learning_experiments(5)),
        )
        recent: dict[str, Any] = {}
        for key, read in recent_calls:
            try:
                recent[key] = read()
            except Exception as exc:  # noqa: BLE001 - one closed subsystem is not a page failure
                recent[key] = {
                    "ok": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
        wallet = status.get("wallet") or {}
        wallet_summary = wallet.get("summary") or {}
        execution = status.get("execution") or {}
        execution_counts = execution.get("counts") or {}
        risk = status.get("risk") or {}
        risk_metrics = risk.get("metrics") or {}
        backtesting = status.get("backtesting") or {}
        shadow = status.get("shadow") or {}
        learning = status.get("learning") or {}
        portfolio_summary = self._portfolio_summary()
        return {
            "subsystem": SUBSYSTEM_NAME,
            "phase": IMPLEMENTED_PHASE,
            "generated_at": self._clock().isoformat(),
            "status": status,
            "summary": {
                "lifecycle": status.get("lifecycle"),
                "health": (status.get("health") or {}).get("state"),
                "wallet_balance_inr": wallet_summary.get("user_balance", "0.00"),
                "wallets": wallet_summary.get("wallets", 0),
                "ledger_entries": wallet_summary.get("ledger_entries", 0),
                "paper_orders": execution_counts.get("orders", 0),
                "paper_fills": execution_counts.get("fills", 0),
                "risk_decisions": risk_metrics.get("decisions", 0),
                "backtests": backtesting.get("total", 0),
                "shadow_sessions": shadow.get("sessions", 0),
                "learning_initialised": bool(learning.get("initialised", False)),
                "portfolio_value_inr": portfolio_summary.get("total_value_inr", "0.00"),
                "portfolio_invested_inr": portfolio_summary.get("invested_inr", "0.00"),
                "portfolio_cash_inr": portfolio_summary.get("cash_inr", "0.00"),
                "portfolio_pnl_inr": portfolio_summary.get("pnl_inr", "0.00"),
                "portfolio_return_pct": portfolio_summary.get("return_pct", "0.00"),
                "portfolio_exposure_pct": portfolio_summary.get("exposure_pct", "0.00"),
                "portfolios": portfolio_summary.get("count", 0),
            },
            "portfolios": portfolio_summary,
            "recent": recent,
            "activity": self._overview_activity(),
            "audit": self.audit_summary(limit=20),
            "safety": {
                "real_money_trading": bool(
                    (status.get("permissions") or {}).get("real_money_trading", True)
                ),
                "paper_only": True,
                "broker_connected": False,
                "live_execution_routes": 0,
                "shadow_executes": False,
                "learning_executes": False,
            },
        }

    # -- introspection --------------------------------------------------------

    def _status_part(self, phase: str, reader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Read one component status without letting it take down the surface."""
        try:
            return reader()
        except Exception as exc:  # noqa: BLE001 - one component must not hide the others
            return {
                "ok": False,
                "initialised": False,
                "phase": phase,
                "detail": f"status read failed: {type(exc).__name__}: {exc}",
                "last_error": f"{type(exc).__name__}: {exc}",
            }

    def status(self) -> dict[str, Any]:
        """The operational status NovaControl reads (and the API serves)."""
        health = self.health()
        granted = [operation.value for operation in self._permission_model.granted_operations()]
        denied = [
            record["operation"]
            for record in self._permission_model.matrix()
            if not record["granted"]
        ]
        return {
            "subsystem": SUBSYSTEM_NAME,
            "phase": IMPLEMENTED_PHASE,
            "schema_version": MARKET_SCHEMA_VERSION,
            "contract_version": MARKET_CONTRACT_VERSION,
            "lifecycle": self._lifecycle.state.value,
            "enabled": self._config.enabled,
            "operational": self._lifecycle.state
            in (LifecycleState.READY, LifecycleState.RUNNING, LifecycleState.DEGRADED),
            "config": self._config.to_dict(),
            "config_error": self._config_error,
            "health": health.to_dict(),
            "capabilities": list(self.capability_records()),
            "dependencies": [item.to_dict() for item in self.dependencies()],
            "permissions": {
                "granted": granted,
                "denied": denied,
                # Derived, never hard-coded: if the permission model ever granted
                # live execution the status would say so (and initialisation would
                # refuse to reach READY) instead of claiming a safety it lost.
                "real_money_trading": self._permission_model.authorize(
                    MarketOperation.EXECUTE_LIVE_TRADE
                ).allowed,
            },
            "subscriptions": {
                "count": len(self._subscriptions),
                "event_types": sorted(self._subscriptions),
            },
            "last_error": self._last_error,
            "last_event_error": self._last_event_error,
            "last_persist_error": self._last_persist_error,
            "paused": self._config.paused,
            "market_session": self.market_session(),
            "notifications": self.notifications_status(),
            "runtime": self.runtime_status(),
            "autonomy": self._status_part("AT-01", self.autonomy_status),
            "market_data": self._status_part("NM-02", self.market_data_status),
            "wallet": self._status_part("NM-03", self.wallet_status),
            "radar": self._status_part("NM-04", self.radar_status),
            "discovery": self._status_part("NM-05", self.discovery_status),
            "forecasting": self._status_part("NM-06", self.forecast_status),
            "statistical": self._status_part("PI-05", self.statistical_status),
            "prediction": self._status_part("PI-01", self.prediction_status),
            "datasets": self._status_part("PI-02", self.dataset_status),
            "gateway": self._status_part("PI-03", self.gateway_status),
            "features": self._status_part("PI-04", self.feature_status),
            "ensemble": self._status_part("PI-08", self.ensemble_status),
            "portfolio": self._status_part("NM-07", self.portfolio_status),
            "risk": self._status_part("NM-08", self.risk_status),
            "strategy": self._status_part("NM-09", self.strategy_status),
            "execution": self._status_part("NM-10", self.execution_status),
            "backtesting": self._status_part("NM-11", self.backtest_status),
            "evaluation": self._status_part("PI-11", self.evaluation_status),
            "shadow": self._status_part("NM-12", self.shadow_status),
            "learning": self._status_part("NM-13", self.learning_status),
            "activity": {
                "events": len(self._activity),
                "last_event": self._activity[-1]["type"] if self._activity else "",
            },
            "audit": {
                "available": self._audit_logger is not None,
                "last_error": self._last_audit_error,
            },
            "initialized_at": (self._initialized_at.isoformat() if self._initialized_at else None),
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "statistics": dict(self._counters),
            "transitions": [
                transition.to_dict() for transition in self._lifecycle.history(limit=20)
            ],
        }

    def contracts(self) -> dict[str, Any]:
        """The versioned contracts as data — what a later phase builds against."""
        return {
            "subsystem": SUBSYSTEM_NAME,
            "phase": IMPLEMENTED_PHASE,
            "schema_version": MARKET_SCHEMA_VERSION,
            "contract_version": MARKET_CONTRACT_VERSION,
            "roadmap": [{"phase": phase, "title": title} for phase, title in ROADMAP],
            "prediction_roadmap": [
                {"phase": phase, "title": title} for phase, title in PREDICTION_ROADMAP
            ],
            "prediction_domains": [boundary.to_dict() for boundary in PREDICTION_DOMAIN_BOUNDARIES],
            "prediction_storage": [
                boundary.to_dict() for boundary in PREDICTION_STORAGE_BOUNDARIES
            ],
            "lifecycle": {
                "states": self._lifecycle.states(),
                "transitions": self._lifecycle.transition_table(),
                "history_limit": LIFECYCLE_HISTORY_LIMIT,
            },
            "config": {
                "schema": list(NovaMarketConfig.schema()),
                "namespace": self._namespace,
                "secret_policy": (
                    "NovaMarket stores a reference (env:NAME / store:NAME) and "
                    "never a credential value; configuration rejects "
                    "credential-shaped field names"
                ),
            },
            "capabilities": list(self.capability_records()),
            "permissions": {
                "matrix": list(self._permission_model.matrix()),
                "real_money_trading": self._permission_model.authorize(
                    MarketOperation.EXECUTE_LIVE_TRADE
                ).allowed,
            },
            "events": MarketEventPublisher.contract(),
            "money": {
                "accounting_unit": "NovaCoin",
                "currency": "INR",
                "exchange_rate": "1 NC = ₹1",
                "arithmetic": "Decimal quantized to 0.01 with ROUND_HALF_EVEN",
                "binary_floats": "refused at construction",
                "price_timestamps": "observed_at and retrieved_at are required and timezone-aware",
            },
            "horizons": [profile.to_dict() for profile in horizon_profiles()],
            "horizon_mapping": dict(HORIZON_MAPPING),
            "domains": [boundary.to_dict() for boundary in DOMAIN_BOUNDARIES],
            "storage": [boundary.to_dict() for boundary in STORAGE_BOUNDARIES],
            "market_data": (
                self._market_engine.contract()
                if self._market_engine is not None
                else {
                    "implemented": False,
                    "detail": "market-data infrastructure is not initialised",
                }
            ),
            "wallet": self.wallet_contract(),
            "radar": self.radar_contract(),
            "discovery": self.discovery_contract(),
            "forecasting": self.forecast_contract(),
            "gateway": self.gateway_contract(),
            "prediction": (
                self._prediction_service.contract()
                if self._prediction_service is not None
                else {
                    "phase": PI_PHASE,
                    "status": "contracts implemented; runtime opens with market data",
                    "contract_version": PI_CONTRACT_VERSION,
                    "authorities": {
                        "risk": "NM-08",
                        "execution": "NM-10",
                        "prediction_authorizes_action": False,
                    },
                }
            ),
            "datasets": (
                self._dataset_service.contract()
                if self._dataset_service is not None
                else {
                    "phase": PI02_PHASE,
                    "status": "contracts implemented; runtime opens with market data",
                    "contract_version": DATASET_CONTRACT_VERSION,
                    "authorities": {
                        "risk": "NM-08",
                        "execution": "NM-10",
                        "dataset_authorizes_action": False,
                    },
                }
            ),
            "features": self.feature_contract(),
            "portfolio": self.portfolio_contract(),
            "risk": self.risk_contract(),
            "strategy": self.strategy_contract(),
            "execution": self.execution_contract(),
            "autonomy": self.autonomy_contract(),
            "backtesting": self.backtest_contract(),
            "evaluation": self.evaluation_contract(),
            "shadow": self.shadow_contract(),
            "learning": self.learning_contract(),
            "audit": self.audit_contract(),
            "integrations": {
                "runtime_module": "novacontrol.novamarket.module.NovaMarketModule",
                "event_bus": "novacontrol.core.events.EventBus",
                "permissions": "novacontrol.reliability.permissions.PermissionManager",
                "capabilities": "novacontrol.intelligence.intent.CapabilityRegistry",
                "state_store": "novacontrol.persistence.json_store.JsonStateStore",
                "health": "novacontrol.core.diagnostics.HealthState",
            },
            "limitations": [
                "Market data is delayed or end-of-day according to each provider; no live exchange feed is claimed.",
                "Yahoo's chart endpoint is unofficial; CoinGecko's free tier has no volume; Alpha Vantage needs a key and is daily-only.",
                "Deposits and withdrawals move simulated NovaCoin only; no bank, broker or crypto rail exists.",
                "Paper execution is simulated; no broker or exchange order is sent in any phase.",
                "No sensitive financial configuration or secret value is stored.",
                "No real-money trading is implemented or permitted.",
                "The workspace is read-only monitoring plus lifecycle controls; financial mutations remain explicit API calls.",
            ],
        }

    # -- internals ------------------------------------------------------------

    def _apply_log_level(self) -> None:
        _LOGGER.setLevel(getattr(logging, self._config.log_level, logging.INFO))

    async def _validate_mandatory(self) -> None:
        """Every check whose failure must prevent READY, in one place."""
        if self._config_error:
            raise ConfigurationError(self._config_error)
        if self._architecture_problems:
            raise ConfigurationError(
                "NovaMarket architectural declarations are inconsistent: "
                + "; ".join(self._architecture_problems)
            )
        if self._config.contract_version != MARKET_CONTRACT_VERSION:
            raise ConfigurationError(
                f"configuration contract {self._config.contract_version!r} does "
                f"not match this build ({MARKET_CONTRACT_VERSION})"
            )
        live_trade = self._permission_model.authorize(MarketOperation.EXECUTE_LIVE_TRADE)
        if live_trade.allowed:
            raise DependencyError(
                "this build's permission model grants live trading; refusing to "
                "initialise a financial subsystem with real-money execution"
            )
        # NM-02's repository and adapters are mandatory infrastructure: a
        # subsystem that cannot open its append-only store must reach ERROR, not
        # claim READY and lose every observation.
        self._ensure_market_data()
        # PI-01 contracts share the same database file. If the prediction tables
        # cannot open, READY would be a false claim about the new foundation.
        self.prediction_service()
        # PI-02 dataset tables live in the same file and are mandatory for READY.
        self.dataset_service()
        for index, validator in enumerate(self._validators):
            try:
                outcome = validator()
                if inspect.isawaitable(outcome):
                    await outcome
            except NovaMarketError:
                raise
            except Exception as exc:
                raise DependencyError(
                    f"dependency check {index} failed: {type(exc).__name__}: {exc}"
                ) from exc

    def _capability_availability(self, capability: MarketCapability) -> tuple[bool, str]:
        decision = self._permission_model.authorize(capability.operation)
        if not decision.allowed:
            return False, decision.reason
        if capability.capability_id.startswith("market.portfolio."):
            if self._lifecycle.state not in (
                LifecycleState.READY,
                LifecycleState.RUNNING,
                LifecycleState.DEGRADED,
            ):
                return False, (
                    "portfolio management opens when NovaMarket is operational "
                    f"(currently {self._lifecycle.state.value})"
                )
            return True, ""
        if capability.capability_id.startswith("market.strategy."):
            if self._lifecycle.state not in (
                LifecycleState.READY,
                LifecycleState.RUNNING,
                LifecycleState.DEGRADED,
            ):
                return False, (
                    "strategy management opens when NovaMarket is operational "
                    f"(currently {self._lifecycle.state.value})"
                )
            return True, ""
        if capability.capability_id.startswith("market.risk."):
            if self._lifecycle.state not in (
                LifecycleState.READY,
                LifecycleState.RUNNING,
                LifecycleState.DEGRADED,
            ):
                return False, (
                    "risk management opens when NovaMarket is operational "
                    f"(currently {self._lifecycle.state.value})"
                )
            return True, ""
        if capability.capability_id == "market.data.providers":
            if self._market_engine is None:
                return False, "market-data infrastructure is not initialised"
            return True, ""
        if capability.requires_provider:
            engine = self._market_engine
            if engine is None:
                return False, "market-data infrastructure is not initialised"
            if not engine.enabled_providers:
                detail = (
                    "; configured provider(s) are not installed: "
                    + ", ".join(self._unknown_providers)
                    if self._unknown_providers
                    else ""
                )
                return (
                    False,
                    "no installed market-data provider is enabled" + detail,
                )
            if capability.verified_operation:
                verified = self._verified_operations()
                if capability.verified_operation not in verified:
                    return (
                        False,
                        "no provider has completed a verified "
                        f"{capability.verified_operation} retrieval yet; an "
                        "operation is advertised only after it is measured",
                    )
        return True, ""

    def _register_capabilities(self) -> tuple[str, ...]:
        registry = self._capability_registry
        if registry is None:
            return ()
        self._unregister_capabilities()
        registered: list[str] = []
        try:
            for item in CAPABILITIES:
                available, reason = self._capability_availability(item)
                decision = self._permission_model.authorize(item.operation)
                capability = Capability(
                    capability=item.capability_id.replace(".", "_"),
                    description=item.description,
                    # The capability catalogue keeps its own four-level enum;
                    # the value is the shared vocabulary, converted explicitly
                    # rather than relying on two StrEnums being interchangeable.
                    risk=CapabilityRiskLevel(decision.risk.value),
                    executor=SUBSYSTEM_NAME,
                    verifier="market_status_verifier",
                    supported_environments=("desktop",),
                    capability_id=item.capability_id,
                    name=item.name,
                    category="market",
                    supported_inputs=("configuration",),
                    supported_outputs=("status", "contracts"),
                    permissions=(item.operation.value,),
                    availability=(
                        CapabilityAvailability.AVAILABLE
                        if available
                        else CapabilityAvailability.UNAVAILABLE
                    ),
                    availability_reason=reason,
                    examples=("show NovaMarket status", "configure NovaMarket"),
                    tags=("market", "finance"),
                    source=CapabilitySource.DECLARED,
                )
                registry.register(capability)
                registered.append(item.capability_id)
        except Exception:
            # A half-registered table is worse than none: withdraw what landed so
            # the failed attempt leaves no capability behind.
            for capability_id in registered:
                try:
                    registry.unregister(capability_id)
                except Exception:  # noqa: BLE001 - cleanup must not mask the cause
                    pass
            self._registered_capabilities = []
            raise
        self._registered_capabilities = registered
        return tuple(registered)

    def _unregister_capabilities(self) -> None:
        registry = self._capability_registry
        if registry is not None:
            for capability_id in tuple(self._registered_capabilities):
                try:
                    registry.unregister(capability_id)
                except Exception as exc:  # noqa: BLE001 - a refusal is recorded, not raised
                    self._last_event_error = f"capability withdraw failed: {exc}"
        self._registered_capabilities = []

    async def _subscribe(self) -> None:
        """Attach the control channel ONCE; a second call is a no-op."""
        if self._subscriptions:
            return
        bus = self._publisher.event_bus
        if bus is None:
            return
        event_type = EventType.NOVAMARKET_CONFIGURATION_REQUESTED.value
        await bus.subscribe(event_type, self._configuration_handler)
        self._subscriptions[event_type] = self._configuration_handler

    async def _unsubscribe_all(self) -> None:
        bus = self._publisher.event_bus
        for event_type, handler in tuple(self._subscriptions.items()):
            if bus is not None:
                try:
                    await bus.unsubscribe(event_type, handler)
                except Exception as exc:  # noqa: BLE001 - shutdown must continue
                    self._last_event_error = f"unsubscribe failed: {exc}"
            self._subscriptions.pop(event_type, None)

    # -- asset discovery and watchlists (NM-05) -------------------------------

    @property
    def discovery(self) -> AssetDiscoveryEngine | None:
        """The discovery engine, or ``None`` before initialisation / after shutdown."""
        return self._discovery_engine

    def _discovery_path(self) -> Any:
        """The same SQLite file the market data, ledger and radar already use."""
        return self._radar_path()

    def _ensure_discovery(self) -> AssetDiscoveryEngine:
        """Open the discovery store and build the engine ONCE per run."""
        if self._discovery_engine is not None:
            return self._discovery_engine
        try:
            store = DiscoveryStore(self._discovery_path(), clock=self._clock)
            engine = AssetDiscoveryEngine(
                store,
                market_data=self._market_engine,
                radar=self._radar_engine,
                publisher=self._discovery_publish,
                clock=self._clock,
            )
        except Exception as exc:
            self._discovery_error = f"{type(exc).__name__}: {exc}"
            raise
        self._discovery_store = store
        self._discovery_engine = engine
        self._discovery_error = ""
        return engine

    async def _discovery_publish(self, event_type: str, payload: dict[str, Any]) -> None:
        await self._publish(EventType(str(event_type)), **payload)

    def _discovery_service(self) -> AssetDiscoveryEngine:
        engine = self._discovery_engine
        if engine is None:
            if self._lifecycle.state not in (
                LifecycleState.READY,
                LifecycleState.RUNNING,
                LifecycleState.DEGRADED,
            ):
                raise NotOperationalError(
                    "asset discovery is not initialised; enable NovaMarket first"
                )
            engine = self._ensure_discovery()
        # The radar and the market-data engine may open after discovery does.
        engine.set_market_data(self._market_engine)
        if self._radar_engine is not None:
            engine.set_radar(self._radar_engine)
        return engine

    def discovery_status(self) -> dict[str, Any]:
        """Cycle state, universe, sources and totals, or why it is closed."""
        if self._discovery_engine is None and self._lifecycle.state not in (
            LifecycleState.READY,
            LifecycleState.RUNNING,
            LifecycleState.DEGRADED,
        ):
            return {
                "initialised": False,
                "detail": (
                    "asset discovery opens when NovaMarket is operational "
                    f"(currently {self._lifecycle.state.value})"
                ),
                "phase": "NM-05",
                "error": self._discovery_error,
            }
        try:
            return self._discovery_service().status()
        except Exception as exc:  # noqa: BLE001 - a read must not crash status
            return {
                "initialised": False,
                "detail": f"{type(exc).__name__}: {exc}",
                "phase": "NM-05",
            }

    async def discovery_cycle(self, *, force: bool = False) -> dict[str, Any]:
        return await self._discovery_service().run_cycle(force=force)

    def discovery_candidates(self, *, limit: int = 50, min_score: float = 0.0) -> dict[str, Any]:
        return self._discovery_service().candidates(limit=limit, min_score=min_score)

    def discovery_candidate(self, instrument_id: str) -> dict[str, Any]:
        return self._discovery_service().candidate(instrument_id)

    def discovery_evaluate(self, instrument_id: str) -> dict[str, Any]:
        return self._discovery_service().evaluate(instrument_id)

    def discovery_history(
        self,
        *,
        watchlist_id: str | None = None,
        instrument_id: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        return self._discovery_service().history(
            watchlist_id=watchlist_id, instrument_id=instrument_id, limit=limit
        )

    def discovery_cycles(self, *, limit: int = 20) -> dict[str, Any]:
        return self._discovery_service().cycles(limit=limit)

    def discovery_sources(self) -> dict[str, Any]:
        return self._discovery_service().sources()

    async def discovery_reevaluate(self, *, watchlist_id: str | None = None) -> dict[str, Any]:
        return await self._discovery_service().reevaluate(watchlist_id=watchlist_id)

    async def discovery_consume_event(self, event_id: str) -> dict[str, Any]:
        return await self._discovery_service().consume_event(event_id)

    async def discovery_scheduler(
        self, action: str, *, interval_seconds: int | None = None
    ) -> dict[str, Any]:
        engine = self._discovery_service()
        verb = str(action or "").strip().lower()
        if verb == "start":
            return await engine.start_scheduler(interval_seconds)
        if verb == "stop":
            return await engine.stop_scheduler()
        raise DiscoveryValidationError("action must be 'start' or 'stop'")

    def discovery_contract(self) -> dict[str, Any]:
        engine = self._discovery_engine
        if engine is None:
            return {
                "implemented": False,
                "phase": "NM-05",
                "detail": "the asset discovery engine is not open in this run",
            }
        return engine.contract()

    # -- probabilistic forecasting (NM-06) ------------------------------------

    @property
    def forecasting(self) -> ForecastEngine | None:
        """The forecast engine, or ``None`` before initialisation / after shutdown."""
        return self._forecast_engine

    def _ensure_forecast(self) -> ForecastEngine:
        """Open the forecast store and build the engine ONCE per run."""
        if self._forecast_engine is not None:
            return self._forecast_engine
        try:
            store = ForecastStore(self._radar_path(), clock=self._clock)
            engine = ForecastEngine(
                store,
                market_data=self._market_engine,
                radar=self._radar_engine,
                discovery=self._discovery_engine,
                publisher=self._forecast_publish,
                clock=self._clock,
            )
        except Exception as exc:
            self._forecast_error = f"{type(exc).__name__}: {exc}"
            raise
        self._forecast_store = store
        self._forecast_engine = engine
        self._forecast_error = ""
        return engine

    async def _forecast_publish(self, event_type: str, payload: dict[str, Any]) -> None:
        await self._publish(EventType(str(event_type)), **payload)

    def _forecast_service(self) -> ForecastEngine:
        engine = self._forecast_engine
        if engine is None:
            if self._lifecycle.state not in (
                LifecycleState.READY,
                LifecycleState.RUNNING,
                LifecycleState.DEGRADED,
            ):
                raise NotOperationalError("forecasting is not initialised; enable NovaMarket first")
            engine = self._ensure_forecast()
        engine.set_market_data(self._market_engine)
        if self._radar_engine is not None:
            engine.set_radar(self._radar_engine)
        if self._discovery_engine is not None:
            engine.set_discovery(self._discovery_engine)
        return engine

    def forecast_status(self) -> dict[str, Any]:
        if self._forecast_engine is None and self._lifecycle.state not in (
            LifecycleState.READY,
            LifecycleState.RUNNING,
            LifecycleState.DEGRADED,
        ):
            return {
                "initialised": False,
                "detail": (
                    "forecasting opens when NovaMarket is operational "
                    f"(currently {self._lifecycle.state.value})"
                ),
                "phase": "NM-06",
                "error": self._forecast_error,
                "gradient": self.gradient_status(),
                "lstm": self.lstm_status(),
                "ensemble": self.ensemble_status(),
                "scheduler": self.forecast_scheduler_status(),
                "outlook": self.outlook_status(),
            }
        try:
            body = self._forecast_service().status()
            body["statistical"] = self.statistical_status()
            body["gradient"] = self.gradient_status()
            body["lstm"] = self.lstm_status()
            body["ensemble"] = self.ensemble_status()
            body["scheduler"] = self.forecast_scheduler_status()
            body["outlook"] = self.outlook_status()
            return body
        except Exception as exc:  # noqa: BLE001 - a read must not crash status
            return {
                "initialised": False,
                "detail": f"{type(exc).__name__}: {exc}",
                "phase": "NM-06",
            }

    async def forecast_generate(
        self,
        instrument_id: str,
        horizon: str,
        *,
        statistical: bool = False,
        engine: str = "",
        model_id: str = "",
        as_of: datetime | None = None,
        resolution: str | None = None,
        dataset_id: str = "",
        dataset_version: str = "",
        feature_set_version: str = "unspecified",
        feature_set_id: str = "",
        exchange: str = "",
        currency: str = "",
        bars: Sequence[Any] | None = None,
        config: Any | None = None,
        fundamentals: Sequence[Any] = (),
        news_events: Sequence[Any] = (),
        regime: str = "",
    ) -> dict[str, Any]:
        requested_engine = str(engine or "").strip().lower()
        family_hint = str(model_id or "").strip().lower()
        gradient_families = {"xgboost", "lightgbm"}
        if requested_engine not in {
            "",
            "statistical",
            "gradient",
            "xgboost",
            "lightgbm",
            "lstm",
            "sequence",
            "ensemble",
        }:
            raise LSTMValidationError(f"unsupported forecast engine {requested_engine!r}")
        lstm_requested = requested_engine in {"lstm", "sequence"} or family_hint in {
            "lstm",
            "sequence",
        }
        if lstm_requested and (statistical or requested_engine == "statistical"):
            raise LSTMValidationError(
                "an LSTM engine cannot be combined with a statistical request"
            )
        if lstm_requested and requested_engine in {"gradient", "xgboost", "lightgbm"}:
            raise LSTMValidationError("an LSTM engine cannot be combined with a gradient request")
        if lstm_requested and family_hint not in {"", "lstm", "sequence"}:
            raise LSTMValidationError(f"an LSTM engine does not accept model_id {family_hint!r}")
        ensemble_requested = requested_engine == "ensemble" or family_hint == "ensemble"
        if ensemble_requested:
            if statistical or requested_engine in {
                "statistical",
                "gradient",
                "xgboost",
                "lightgbm",
                "lstm",
                "sequence",
            }:
                raise EnsembleValidationError(
                    "an ensemble request cannot be combined with a single-family "
                    "engine or a statistical-only request"
                )
            if family_hint not in {"", "ensemble"}:
                raise EnsembleValidationError(
                    f"an ensemble request does not accept model_id {family_hint!r}"
                )
            if not regime:
                try:
                    latest = self.latest_regime(instrument_id, resolution=resolution)
                    latest_at = str((latest or {}).get("as_of") or "")
                    try:
                        latest_time = datetime.fromisoformat(latest_at.replace("Z", "+00:00"))
                    except ValueError:
                        latest_time = None
                    if latest_time is not None and latest_time.tzinfo is None:
                        latest_time = latest_time.replace(tzinfo=UTC)
                    if latest and (
                        as_of is None or (latest_time is not None and latest_time <= as_of)
                    ):
                        regime = str(latest.get("primary_regime") or "")
                except Exception:  # noqa: BLE001 - regime is optional context
                    regime = ""
            return await self.ensemble_service().generate(
                instrument_id,
                horizon,
                as_of=as_of,
                resolution=resolution,
                dataset_id=dataset_id,
                dataset_version=dataset_version,
                feature_set_version=feature_set_version,
                feature_set_id=feature_set_id,
                exchange=exchange,
                currency=currency,
                bars=bars,
                config=config,
                fundamentals=fundamentals,
                news_events=news_events,
                regime=regime,
            )
        if requested_engine in {"gradient", "xgboost", "lightgbm"} and (
            statistical or family_hint == "statistical"
        ):
            raise GradientValidationError(
                "a gradient engine cannot be combined with a statistical request"
            )
        if (
            requested_engine in gradient_families
            and family_hint in gradient_families
            and requested_engine != family_hint
        ):
            raise GradientValidationError(
                f"engine {requested_engine!r} conflicts with model_id "
                f"{family_hint!r}; use one gradient family"
            )
        if (
            requested_engine in {"gradient", "xgboost", "lightgbm"}
            and family_hint
            and family_hint not in gradient_families
        ):
            raise GradientValidationError(
                f"gradient inference does not accept model_id {family_hint!r}; "
                "use xgboost or lightgbm"
            )
        if (statistical or requested_engine == "statistical") and family_hint in gradient_families:
            raise GradientValidationError(
                "a statistical baseline cannot be requested with a gradient "
                f"model_id {family_hint!r}"
            )
        if lstm_requested:
            return await self.lstm_service().generate(
                instrument_id,
                horizon,
                model_id="lstm",
                version=str((config or {}).get("model_version") or "")
                if isinstance(config, Mapping)
                else "",
                as_of=as_of,
                resolution=resolution,
                dataset_id=dataset_id,
                dataset_version=dataset_version,
                bars=bars,
                fundamentals=fundamentals,
                news_events=news_events,
            )
        if requested_engine in {"xgboost", "lightgbm", "gradient"} or family_hint in {
            "xgboost",
            "lightgbm",
        }:
            return await self.gradient_service().generate(
                instrument_id,
                horizon,
                model_id=(
                    family_hint
                    if family_hint in {"xgboost", "lightgbm"}
                    else requested_engine
                    if requested_engine in gradient_families
                    else ""
                ),
                version=str((config or {}).get("model_version") or "")
                if isinstance(config, Mapping)
                else "",
                as_of=as_of,
                resolution=resolution,
                dataset_id=dataset_id,
                dataset_version=dataset_version,
                exchange=exchange,
                currency=currency,
                bars=bars,
                fundamentals=fundamentals,
                news_events=news_events,
            )
        if statistical or requested_engine == "statistical" or model_id:
            return await self.statistical_service().generate(
                instrument_id,
                horizon,
                model_id=model_id,
                as_of=as_of,
                resolution=resolution,
                dataset_id=dataset_id,
                dataset_version=dataset_version,
                feature_set_version=feature_set_version,
                feature_set_id=feature_set_id,
                exchange=exchange,
                currency=currency,
                bars=bars,
                config=config,
            )
        return await self._forecast_service().generate(instrument_id, horizon)

    def forecast_get(self, forecast_id: str) -> dict[str, Any]:
        # PI-09 versions are immutable PI-01 records with deterministic
        # ``pfc_`` ids; read those from the lifecycle store before the PI-08
        # forecast-store fallback, which has different tables.
        if forecast_id.startswith("pfc_"):
            from novacontrol.novamarket.contracts import ForecastNotFoundError

            forecast = self.prediction_service().store.get_forecast(forecast_id)
            if forecast is None:
                raise ForecastNotFoundError(f"unknown forecast {forecast_id!r}")
            return {"ok": True, **forecast.to_dict()}
        # PI-05 and PI-06 forecasts live in the same PI-01 table; the prefixes
        # route to their owner without a second forecast store.
        if forecast_id.startswith("sft_"):
            return self.statistical_service().forecast(forecast_id)
        if forecast_id.startswith("gbf_"):
            return self.gradient_service().forecast(forecast_id)
        if forecast_id.startswith("lstm_"):
            return self.lstm_service().forecast(forecast_id)
        if forecast_id.startswith(("ens_", "ensfb_")):
            return self.ensemble_service().forecast(forecast_id)
        return self._forecast_service().forecast(forecast_id)

    def forecast_list(
        self,
        *,
        instrument_id: str | None = None,
        horizon: str | None = None,
        status: str | None = None,
        limit: int = 50,
        engine: str = "",
        model_id: str | None = None,
    ) -> dict[str, Any]:
        if str(engine or "").strip().lower() == "statistical":
            return self.statistical_service().forecasts(
                instrument_id=instrument_id,
                horizon=horizon,
                model_id=model_id,
                limit=limit,
            )
        requested_engine = str(engine or "").strip().lower()
        if requested_engine == "ensemble":
            return self.ensemble_service().forecasts(
                instrument_id=instrument_id,
                horizon=horizon,
                model_id=model_id,
                limit=limit,
            )
        if requested_engine in {"lstm", "sequence"}:
            return self.lstm_service().forecasts(
                instrument_id=instrument_id,
                horizon=horizon,
                model_id=model_id or "lstm",
                limit=limit,
            )
        if requested_engine in {"xgboost", "lightgbm", "gradient"}:
            return self.gradient_service().forecasts(
                instrument_id=instrument_id,
                horizon=horizon,
                model_id=(
                    model_id
                    or (requested_engine if requested_engine in {"xgboost", "lightgbm"} else None)
                ),
                limit=limit,
            )
        return self._forecast_service().forecasts(
            instrument_id=instrument_id,
            horizon=horizon,
            status=status,
            limit=limit,
        )

    def forecast_outcome(self, forecast_id: str) -> dict[str, Any]:
        if forecast_id.startswith("pfc_"):
            from novacontrol.novamarket.contracts import ForecastNotFoundError

            store = self.prediction_service().store
            forecast = store.get_forecast(forecast_id)
            if forecast is None:
                raise ForecastNotFoundError(f"unknown forecast {forecast_id!r}")
            outcomes = [
                item
                for item in store.outcomes(forecast.lifecycle_id)
                if item.forecast_id == forecast_id
            ]
            if outcomes:
                outcome = outcomes[0]
                return {
                    "ok": True,
                    "forecast_id": forecast_id,
                    "cycle_id": forecast.lifecycle_id,
                    "status": "evaluated",
                    **outcome.to_dict(),
                }
            return {
                "ok": True,
                "forecast_id": forecast_id,
                "cycle_id": forecast.lifecycle_id,
                "status": "pending",
                "target_at": forecast.target_at.isoformat(),
                "detail": "the target timestamp has not produced an outcome yet",
            }
        return self._forecast_service().outcome(forecast_id)

    async def forecast_evaluate(
        self,
        *,
        engine: str = "",
        instrument_id: str = "",
        horizon: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        requested_engine = str(engine or "").strip().lower()
        if requested_engine == "ensemble":
            return await self.ensemble_service().evaluate(instrument_id, horizon, **kwargs)
        if requested_engine == "statistical":
            return await self.statistical_service().evaluate(instrument_id, horizon, **kwargs)
        if requested_engine in {"lstm", "sequence"}:
            return await self.lstm_service().evaluate(instrument_id, horizon, **kwargs)
        if requested_engine in {"xgboost", "lightgbm", "gradient"}:
            if requested_engine in {"xgboost", "lightgbm"}:
                kwargs["models"] = (requested_engine,)
            return await self.gradient_service().evaluate(instrument_id, horizon, **kwargs)
        return await self._forecast_service().evaluate_due()

    async def forecast_train(
        self, instrument_id: str, horizon: str, *, engine: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        requested_engine = str(engine or "").strip().lower()
        if requested_engine == "ensemble":
            return await self.ensemble_service().evaluate(instrument_id, horizon, **kwargs)
        if requested_engine == "statistical":
            return await self.statistical_service().evaluate(instrument_id, horizon, **kwargs)
        if requested_engine in {"lstm", "sequence"}:
            return await self.lstm_service().evaluate(instrument_id, horizon, **kwargs)
        if requested_engine in {"xgboost", "lightgbm", "gradient"}:
            if requested_engine in {"xgboost", "lightgbm"}:
                kwargs["models"] = (requested_engine,)
            return await self.gradient_service().evaluate(instrument_id, horizon, **kwargs)
        return await self._forecast_service().train(instrument_id, horizon)

    async def forecast_promote(
        self, model_id: str, instrument_id: str, horizon: str
    ) -> dict[str, Any]:
        return await self._forecast_service().promote(model_id, instrument_id, horizon)

    async def forecast_health(self) -> dict[str, Any]:
        return await self._forecast_service().check_health()

    def forecast_performance(
        self,
        *,
        model_id: str | None = None,
        instrument_id: str | None = None,
        horizon: str | None = None,
        limit: int = 50,
        engine: str = "",
    ) -> dict[str, Any]:
        if str(engine or "").strip().lower() == "statistical":
            service = self.statistical_service()
            return {
                "ok": True,
                "engine": "statistical",
                "evaluations": service.evaluations(limit=limit)["evaluations"],
                "performance": service.performance(
                    model_id=model_id,
                    instrument_id=instrument_id,
                    horizon=horizon,
                    limit=limit,
                ),
                "probabilities": "unavailable; no calibration has been claimed",
            }
        requested_engine = str(engine or "").strip().lower()
        if requested_engine == "ensemble":
            return {
                "ok": True,
                "engine": "ensemble",
                "evaluations": self.ensemble_service().performance(
                    instrument_id=instrument_id,
                    horizon=horizon,
                    limit=limit,
                ),
                "probabilities": (
                    "withheld unless every contributing model supplies calibrated "
                    "direction probabilities; the PI-08 baseline withholds them"
                ),
                "experimental": True,
                "automatic_promotion": False,
            }
        if requested_engine in {"lstm", "sequence"}:
            service = self.lstm_service()
            return {
                "ok": True,
                "engine": "lstm",
                "evaluations": service.evaluations(limit=limit)["evaluations"],
                "performance": service.performance(
                    model_id="lstm",
                    instrument_id=instrument_id,
                    horizon=horizon,
                    limit=limit,
                ),
                "probabilities": (
                    "withheld; the LSTM classifier is uncalibrated and forecast "
                    "contracts carry p_up/p_down/p_neutral = null"
                ),
                "experimental": True,
                "automatic_promotion": False,
            }
        if requested_engine in {"xgboost", "lightgbm", "gradient"}:
            service = self.gradient_service()
            return {
                "ok": True,
                "engine": "gradient",
                "evaluations": service.evaluations(limit=limit)["evaluations"],
                "performance": service.performance(
                    model_id=(
                        model_id
                        or (
                            requested_engine
                            if requested_engine in {"xgboost", "lightgbm"}
                            else None
                        )
                    ),
                    instrument_id=instrument_id,
                    horizon=horizon,
                    limit=limit,
                ),
                "probabilities": (
                    "withheld; the tree classifier is not calibrated and forecast "
                    "contracts carry p_up/p_down/p_neutral = null"
                ),
                "experimental": True,
                "automatic_promotion": False,
            }
        return self._forecast_service().performance(
            model_id=model_id,
            instrument_id=instrument_id,
            horizon=horizon,
            limit=limit,
        )

    def forecast_models(
        self,
        *,
        instrument_id: str | None = None,
        horizon: str | None = None,
        engine: str = "",
    ) -> dict[str, Any]:
        if str(engine or "").strip().lower() == "statistical":
            body = self.statistical_service().models(instrument_id=instrument_id, horizon=horizon)
            if instrument_id and horizon:
                body["suitability"] = self.statistical_service().suitability(instrument_id, horizon)
            return body
        requested_engine = str(engine or "").strip().lower()
        if requested_engine == "ensemble":
            body = self.ensemble_service().models(instrument_id=instrument_id, horizon=horizon)
            if instrument_id and horizon:
                body["calibration"] = self.ensemble_service().calibration(instrument_id, horizon)
            return body
        if requested_engine in {"lstm", "sequence"}:
            body = self.lstm_service().models(instrument_id=instrument_id, horizon=horizon)
            if instrument_id and horizon:
                body["suitability"] = self.lstm_service().suitability(instrument_id, horizon)
            return body
        if requested_engine in {"xgboost", "lightgbm", "gradient"}:
            body = self.gradient_service().models(instrument_id=instrument_id, horizon=horizon)
            if requested_engine in {"xgboost", "lightgbm"}:
                body["models"] = [
                    item
                    for item in body.get("models", ())
                    if str(item.get("model_id")) == requested_engine
                ]
            if instrument_id and horizon:
                body["suitability"] = self.gradient_service().suitability(instrument_id, horizon)
            return body
        return self._forecast_service().models(
            instrument_id=instrument_id,
            horizon=horizon,
        )

    def forecast_calibration(
        self,
        model_id: str,
        horizon: str,
        *,
        engine: str = "",
        instrument_id: str = "",
    ) -> dict[str, Any]:
        if str(engine or "").strip().lower() == "ensemble":
            if not str(instrument_id or "").strip():
                return {
                    "ok": True,
                    "engine": "ensemble",
                    "model_id": model_id or "ensemble",
                    "horizon": horizon,
                    "status": "unavailable",
                    "samples": 0,
                    "detail": (
                        "ensemble calibration is registered per instrument; pass instrument_id"
                    ),
                }
            return {
                "ok": True,
                "engine": "ensemble",
                "model_id": model_id or "ensemble",
                "horizon": horizon,
                **self.ensemble_service().calibration(instrument_id, horizon),
            }
        if str(engine or "").strip().lower() == "statistical":
            return {
                "ok": True,
                "engine": "statistical",
                "model_id": model_id,
                "horizon": horizon,
                "status": "not_applicable",
                "samples": 0,
                "detail": (
                    "PI-05 statistical baselines do not emit probabilities or a "
                    "confidence score; interval coverage is reported by evaluation"
                ),
            }
        if str(engine or "").strip().lower() in {"lstm", "sequence"}:
            return {
                "ok": True,
                "engine": "lstm",
                "model_id": model_id,
                "horizon": horizon,
                "status": "not_applicable",
                "samples": 0,
                "detail": (
                    "PI-07 LSTM classifiers are uncalibrated; probabilities are "
                    "withheld and only validation-residual intervals are reported"
                ),
            }
        if str(engine or "").strip().lower() in {"xgboost", "lightgbm", "gradient"}:
            return {
                "ok": True,
                "engine": "gradient",
                "model_id": model_id,
                "horizon": horizon,
                "status": "not_applicable",
                "samples": 0,
                "detail": (
                    "PI-06 tree classifiers are uncalibrated; probabilities are "
                    "withheld and only validation-residual intervals are reported"
                ),
            }
        return self._forecast_service().calibration(model_id, horizon)

    def forecast_runs(self, *, limit: int = 20) -> dict[str, Any]:
        return self._forecast_service().training_runs(limit=limit)

    def forecast_reliability(self, *, model_id: str | None = None) -> dict[str, Any]:
        return self._forecast_service().reliability(model_id=model_id)

    def forecast_contract(self) -> dict[str, Any]:
        engine = self._forecast_engine
        if engine is None:
            body: dict[str, Any] = {
                "implemented": False,
                "phase": "NM-06",
                "detail": "the forecast engine is not open in this run",
            }
            try:
                body["outlook"] = self.outlook_contract()
            except Exception as exc:  # noqa: BLE001 - contract must stay readable
                body["outlook"] = {
                    "phase": "PI-10",
                    "implemented": True,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            return body
        body = engine.contract()
        body["statistical"] = self.statistical_contract()
        body["gradient"] = self.gradient_contract()
        body["lstm"] = self.lstm_contract()
        try:
            body["ensemble"] = self.ensemble_contract()
        except Exception as exc:  # noqa: BLE001 - contract must stay readable
            body["ensemble"] = {
                "phase": ENSEMBLE_PHASE,
                "implemented": True,
                "detail": f"{type(exc).__name__}: {exc}",
            }
        try:
            body["outlook"] = self.outlook_contract()
        except Exception as exc:  # noqa: BLE001 - contract must stay readable
            body["outlook"] = {
                "phase": "PI-10",
                "implemented": True,
                "detail": f"{type(exc).__name__}: {exc}",
            }
        return body

    # -- evidence-weighted ensemble (PI-08) ----------------------------------

    def ensemble_service(self) -> EnsembleForecastingService:
        """Open PI-08 over the existing PI-01/02/04/05/06/07 surfaces."""
        if self._ensemble_service is not None:
            return self._ensemble_service
        if self._lifecycle.state not in (
            LifecycleState.READY,
            LifecycleState.RUNNING,
            LifecycleState.DEGRADED,
        ):
            raise NotOperationalError(
                "ensemble forecasting is not initialised; enable NovaMarket first"
            )
        try:
            prediction = self.prediction_service()
        except Exception as exc:  # noqa: BLE001
            self._ensemble_error = f"prediction: {type(exc).__name__}: {exc}"
            raise
        try:
            forecast_store = self._forecast_store
            if forecast_store is None:
                self._forecast_service()
                forecast_store = self._forecast_store
            if forecast_store is None:
                raise RuntimeError("the NM-06 forecast store is not open")
        except Exception as exc:  # noqa: BLE001
            self._ensemble_error = f"forecast: {type(exc).__name__}: {exc}"
            raise
        statistical: Any = None
        gradient: Any = None
        lstm: Any = None
        failures: list[str] = []
        try:
            statistical = self.statistical_service()
        except Exception as exc:  # noqa: BLE001
            failures.append(f"statistical: {type(exc).__name__}: {exc}")
        try:
            gradient = self.gradient_service()
        except Exception as exc:  # noqa: BLE001
            failures.append(f"gradient: {type(exc).__name__}: {exc}")
        try:
            lstm = self.lstm_service()
        except Exception as exc:  # noqa: BLE001
            failures.append(f"lstm: {type(exc).__name__}: {exc}")
        if statistical is None and gradient is None and lstm is None:
            self._ensemble_error = "; ".join(failures) or "no constituent service opened"
            raise RuntimeError(self._ensemble_error)
        try:
            service = EnsembleForecastingService(
                statistical_service=statistical,
                gradient_service=gradient,
                lstm_service=lstm,
                prediction_store=prediction.store,
                forecast_store=forecast_store,
                publisher=self._forecast_publish,
                clock=self._clock,
            )
        except Exception as exc:  # noqa: BLE001
            self._ensemble_error = f"{type(exc).__name__}: {exc}"
            raise
        self._ensemble_service = service
        self._ensemble_error = "; ".join(failures)
        return service

    def ensemble_status(self) -> dict[str, Any]:
        if self._ensemble_service is None:
            return {
                "phase": ENSEMBLE_PHASE,
                "implemented": True,
                "initialised": False,
                "error": self._ensemble_error,
                "version": ENSEMBLE_MODEL_VERSION,
                "contract_version": ENSEMBLE_CONTRACT_VERSION,
                "authorities": {"risk": "NM-08", "execution": "NM-10"},
            }
        try:
            return {
                "phase": ENSEMBLE_PHASE,
                "implemented": True,
                "initialised": True,
                "error": self._ensemble_error,
                **self._ensemble_service.status(),
            }
        except Exception as exc:  # noqa: BLE001 - a read must not crash status
            return {
                "phase": ENSEMBLE_PHASE,
                "implemented": True,
                "initialised": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def ensemble_contract(self) -> dict[str, Any]:
        if self._ensemble_service is None:
            return {
                "phase": ENSEMBLE_PHASE,
                "implemented": True,
                "version": ENSEMBLE_MODEL_VERSION,
                "contract_version": ENSEMBLE_CONTRACT_VERSION,
                "detail": "the ensemble opens with the market-data and forecast stores",
                "authorities": {"risk": "NM-08", "execution": "NM-10"},
            }
        return self._ensemble_service.contract()

    async def ensemble_forecast(
        self, instrument_id: str, horizon: str, **kwargs: Any
    ) -> dict[str, Any]:
        return await self.ensemble_service().generate(instrument_id, horizon, **kwargs)

    # -- PI-10 swing and long-term outlook ----------------------------------

    def outlook_service(self) -> HorizonForecastService:
        """Open PI-10 over the existing PI-01/PI-03/PI-04/PI-08 stack."""
        if self._outlook_service is not None:
            return self._outlook_service
        try:
            prediction = self.prediction_service()
            ensemble = self.ensemble_service()
            market_data = self._market_data_engine()
            service = HorizonForecastService(
                market_data=market_data,
                ensemble=ensemble,
                prediction=prediction,
                clock=self._clock,
                news_provider=self._outlook_news,
            )
        except Exception as exc:  # noqa: BLE001 - report the dependency failure
            raise NotOperationalError(
                f"swing/long-term outlook is unavailable: {type(exc).__name__}: {exc}"
            ) from exc
        self._outlook_service = service
        return service

    def outlook_status(self) -> dict[str, Any]:
        if self._outlook_service is None:
            return {
                "phase": "PI-10",
                "implemented": True,
                "initialised": False,
                "detail": "opens with the market-data, prediction and PI-08 ensemble services",
                "profiles": [HORIZON_OUTLOOK_PROFILES[item].to_dict() for item in PI10_HORIZONS],
                "profile_problems": list(validate_outlook_profiles()),
                "authority": {
                    "risk_authority": "NM-08",
                    "execution_authority": "NM-10",
                    "forecast_authorizes_action": False,
                    "new_prediction_engine": False,
                },
            }
        try:
            return {"initialised": True, **self._outlook_service.status()}
        except Exception as exc:  # noqa: BLE001 - a status read must not crash
            return {
                "phase": "PI-10",
                "implemented": True,
                "initialised": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def outlook_contract(self) -> dict[str, Any]:
        if self._outlook_service is None:
            return {
                "phase": "PI-10",
                "implemented": True,
                "detail": "the PI-10 outlook opens with the existing forecast services",
                "supported_horizons": ["1d", "3d", "5d", "30d", "90d"],
                "authorities": {"risk": "NM-08", "execution": "NM-10"},
            }
        return self._outlook_service.contract()

    async def outlook_forecast(self, instrument_id: str, **kwargs: Any) -> dict[str, Any]:
        requested = kwargs.get("horizons")
        if requested:
            for horizon in requested:
                outlook_profile(horizon)
        return await self.outlook_service().generate(instrument_id, **kwargs)

    def outlook_latest(self, instrument_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.outlook_service().latest(instrument_id, **kwargs)

    async def outlook_context(self, instrument_id: str, **kwargs: Any) -> dict[str, Any]:
        assembled = await self.outlook_service().assemble_context(
            instrument_id, include_radar=False, **kwargs
        )
        return assembled.context.to_dict()

    def outlook_accuracy(self, **kwargs: Any) -> dict[str, Any]:
        return self.outlook_service().accuracy(**kwargs)

    def _outlook_news(self, instrument_id: str) -> Sequence[Mapping[str, Any]]:
        """Existing NM-04 evidence as optional PI-10 context, never a new feed."""
        if self._radar_engine is None:
            return ()
        body = self._radar_engine.search(RadarEventQuery(instrument_id=instrument_id, limit=100))
        return tuple(dict(row) for row in body.get("events") or ())

    async def _forecast_outlook_context(
        self, instrument_id: str, horizon: Any, as_of: datetime
    ) -> dict[str, Any]:
        """Supply PI-10's point-in-time context to a PI-09 lifecycle checkpoint.

        The scheduler remains the lifecycle owner; this adapter only gives the
        existing ensemble the same validated context the PI-10 generation route
        uses.  Intraday horizons are deliberately unchanged.
        """
        if horizon not in PI10_HORIZONS:
            return {}
        profile = outlook_profile(horizon)
        assembled = await self.outlook_service().assemble_context(instrument_id, as_of=as_of)
        return {
            "fundamentals": assembled.eligible_fundamentals,
            "news_events": assembled.eligible_events,
            "benchmark_bars": assembled.benchmark_bars,
            "sector_bars": assembled.sector_bars,
            "context_bars": assembled.context_bars,
            "forecast_metadata": {
                "pi10": {
                    "phase": "PI-10",
                    "contract_version": OUTLOOK_CONTRACT_VERSION,
                    "profile_version": HORIZON_PROFILE_VERSION,
                    "horizon_family": profile.family.value,
                    "target_basis": profile.target_basis.value,
                    "target_count": profile.target_count,
                    "feature_profile": profile.feature_profile.value,
                    "context": assembled.context.to_dict(),
                    "authorizes_action": False,
                    "risk_authority": "NM-08",
                    "execution_authority": "NM-10",
                }
            },
        }

    # -- PI-09 forecast lifecycle scheduling --------------------------------

    def forecast_scheduler_service(self) -> ForecastScheduler:
        """Open the one PI-09 scheduler over the existing PI-03/PI-08 stack."""

        if self._forecast_scheduler is not None:
            return self._forecast_scheduler
        try:
            prediction = self.prediction_service()
            ensemble = self.ensemble_service()
            gateway = self.gateway()
            scheduler = ForecastScheduler(
                prediction=prediction,
                ensemble=ensemble,
                gateway=gateway,
                clock=self._clock,
                publisher=self._publisher,
                config=SchedulerConfig(),
                currency_resolver=self._forecast_currency,
                regime_resolver=self._forecast_regime,
                planner=self._auto_plan_from_scheduler,
                context_provider=self._forecast_outlook_context,
            )
            self._forecast_scheduler = scheduler
            self._forecast_scheduler_error = ""
            try:
                scheduler.start()
            except Exception as exc:  # noqa: BLE001 - API reads still work without a loop
                self._forecast_scheduler_error = f"start: {type(exc).__name__}: {exc}"
            return scheduler
        except Exception as exc:  # noqa: BLE001 - report the dependency failure
            self._forecast_scheduler_error = f"{type(exc).__name__}: {exc}"
            raise

    def _auto_plan_from_scheduler(self, scheduler: ForecastScheduler) -> dict[str, Any]:
        """Planner callback used by the scheduler's own tick loop."""

        plan = self._auto_schedule_forecast_cycles(scheduler)
        self._forecast_auto_plan = plan
        return plan

    def _auto_schedule_forecast_cycles(self, scheduler: ForecastScheduler) -> dict[str, Any]:
        """Schedule the bounded default intraday lifecycle for eligible instruments.

        This is deliberately a cycle planner, not a second discovery service:
        held instruments, existing watchlists and the registered market universe
        are read through their existing services, deduplicated and capped.  A
        failure for one instrument is reported and never prevents the others.
        """

        if not scheduler.config.enabled:
            return {"enabled": False, "detail": "scheduler is disabled"}
        if not scheduler.config.auto_schedule:
            return {"enabled": False, "detail": "scheduler auto_schedule is disabled"}
        now = self._clock()
        if (
            self._forecast_auto_plan_at is not None
            and (now - self._forecast_auto_plan_at).total_seconds() < 60
        ):
            return self._forecast_auto_plan or {
                "enabled": True,
                "throttled": True,
                "detail": "automatic planning was checked recently",
            }
        self._forecast_auto_plan_at = now
        candidates: list[str] = []
        sources: dict[str, list[str]] = {"held": [], "watchlists": [], "market": []}

        def add(instrument: str, source: str) -> None:
            value = str(instrument or "").strip().upper()
            if not value or ":" not in value or value in candidates:
                return
            candidates.append(value)
            sources[source].append(value)

        try:
            for item in self._held_instruments():
                add(item, "held")
        except Exception:  # noqa: BLE001 - optional portfolio context
            pass
        try:
            base = self.watchlists()
            for row in base.get("watchlists") or []:
                watchlist_id = str(row.get("watchlist_id") or "")
                if not watchlist_id:
                    continue
                detail = self.watchlist(watchlist_id, limit=100)
                for member in detail.get("members") or []:
                    add(str(member.get("instrument_id") or ""), "watchlists")
        except Exception:  # noqa: BLE001 - discovery may not be initialised
            pass
        try:
            for row in (
                self.market_instruments(limit=scheduler.config.auto_schedule_limit).get("items")
                or []
            ):
                status = str(row.get("trading_status") or "").strip().lower()
                if status in {"suspended", "halted", "delisted", "inactive"}:
                    continue
                add(str(row.get("instrument_id") or ""), "market")
        except Exception:  # noqa: BLE001 - the universe may not be loaded yet
            pass

        limit = scheduler.config.auto_schedule_limit
        selected = candidates[:limit]
        created: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for instrument in selected:
            try:
                active = scheduler.store.forecast_cycles(
                    instrument_id=instrument, horizon="20m", limit=200
                )
                if any(
                    item.target_at > now
                    and item.state.value in {"scheduled", "active", "confirmed", "unconfirmed"}
                    for item in active
                ):
                    skipped.append({"instrument_id": instrument, "reason": "active cycle exists"})
                    continue
                cycle = scheduler.create_cycle(
                    instrument_id=instrument,
                    exchange=instrument.split(":", 1)[0],
                    horizon="20m",
                    now=now,
                )
                created.append({"instrument_id": instrument, "cycle_id": cycle.cycle_id})
            except Exception as exc:  # noqa: BLE001 - one instrument cannot block the rest
                errors.append(
                    {"instrument_id": instrument, "error": f"{type(exc).__name__}: {exc}"}
                )
        return {
            "enabled": True,
            "horizon": "20m",
            "limit": limit,
            "considered": len(candidates),
            "selected": len(selected),
            "created": created,
            "skipped": skipped,
            "errors": errors,
            "sources": sources,
            "detail": "bounded default intraday lifecycle; no order or risk authority",
        }

    def _forecast_currency(self, instrument_id: str) -> str:
        try:
            instrument = self._ensure_market_data().get_instrument(instrument_id)
            return str(getattr(instrument, "currency", "") or "")
        except Exception:  # noqa: BLE001 - a missing currency is not a forecast failure
            return ""

    def _forecast_regime(self, instrument_id: str) -> str:
        try:
            latest = self.latest_regime(instrument_id)
            return str((latest or {}).get("regime") or "unclassified")
        except Exception:  # noqa: BLE001 - regime evidence is optional
            return "unclassified"

    def forecast_scheduler_status(self) -> dict[str, Any]:
        scheduler = self._forecast_scheduler
        if scheduler is None:
            return {
                "phase": "PI-09",
                "implemented": True,
                "initialised": False,
                "error": self._forecast_scheduler_error,
                "auto_plan": self._forecast_auto_plan,
                "detail": "the forecast scheduler opens with the gateway and PI-08 ensemble",
                "authority": {"forecast_only": True, "risk": "NM-08", "execution": "NM-10"},
            }
        try:
            return {
                "phase": "PI-09",
                "implemented": True,
                "initialised": True,
                "error": self._forecast_scheduler_error,
                "auto_plan": self._forecast_auto_plan,
                **scheduler.status(),
            }
        except Exception as exc:  # noqa: BLE001 - status must remain readable
            return {
                "phase": "PI-09",
                "implemented": True,
                "initialised": True,
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def forecast_scheduler_tick(
        self, *, limit: int | None = None, now: datetime | None = None
    ) -> dict[str, Any]:
        return await self.forecast_scheduler_service().tick(limit=limit, now=now)

    def schedule_forecast_cycle(
        self,
        *,
        instrument_id: str,
        horizon: str,
        exchange: str = "",
        target_at: datetime | None = None,
        resolution: str | None = None,
    ) -> dict[str, Any]:
        from novacontrol.novamarket.contracts import ForecastValidationError

        instrument_id = str(instrument_id or "").strip().upper()
        if ":" not in instrument_id:
            raise ForecastValidationError("instrument_id must be MIC:SYMBOL")
        chosen_exchange = str(exchange or "").strip().upper() or instrument_id.split(":", 1)[0]
        if not chosen_exchange:
            raise ForecastValidationError("exchange is required")
        cycle = self.forecast_scheduler_service().create_cycle(
            instrument_id=instrument_id,
            exchange=chosen_exchange,
            horizon=horizon,
            target_at=target_at,
            resolution=resolution,
            now=self._clock(),
        )
        return self.forecast_cycle(cycle.cycle_id)

    def forecast_cycles(self, **filters: Any) -> list[dict[str, Any]]:
        store = self.prediction_service().store
        return [item.to_dict() for item in store.forecast_cycles(**filters)]

    def forecast_cycle(self, cycle_id: str) -> dict[str, Any]:
        scheduler = self._forecast_scheduler
        if scheduler is not None:
            return scheduler.cycle_view(cycle_id)
        store = self.prediction_service().store
        cycle = store.get_forecast_cycle(cycle_id)
        if cycle is None:
            from novacontrol.novamarket.contracts import ForecastNotFoundError

            raise ForecastNotFoundError(f"forecast cycle {cycle_id!r} does not exist")
        return {
            "cycle": cycle.to_dict(),
            "checkpoints": [item.to_dict() for item in store.checkpoints(cycle_id)],
            "forecasts": [item.to_dict() for item in store.forecasts(cycle_id)],
            "stages": [item.to_dict() for item in store.stages(cycle_id)],
            "evaluations": [item.to_dict() for item in store.evaluations(cycle_id)],
            "outcomes": [item.to_dict() for item in store.outcomes(cycle_id)],
        }

    def forecast_accuracy(self, **filters: Any) -> dict[str, Any]:
        return self._forecast_read_scheduler().accuracy(**filters)

    def forecast_missed_checkpoints(self, **filters: Any) -> list[dict[str, Any]]:
        return self._forecast_read_scheduler().missed(**filters)

    def forecast_pending_evaluations(self) -> list[dict[str, Any]]:
        return self._forecast_read_scheduler().pending_evaluations()

    def _forecast_read_scheduler(self) -> ForecastScheduler:
        """Read-only scheduler view; does not require a live PI-08 ensemble."""

        if self._forecast_scheduler is not None:
            return self._forecast_scheduler
        return ForecastScheduler(
            prediction=self.prediction_service(),
            ensemble=None,
            gateway=None,
            clock=self._clock,
            config=SchedulerConfig(),
            currency_resolver=self._forecast_currency,
            regime_resolver=self._forecast_regime,
        )

    def ensemble_forecasts(self, **kwargs: Any) -> dict[str, Any]:
        return self.ensemble_service().forecasts(**kwargs)

    def ensemble_models(self, **kwargs: Any) -> dict[str, Any]:
        return self.ensemble_service().models(**kwargs)

    def ensemble_explain(self, forecast_id: str) -> dict[str, Any]:
        return self.ensemble_service().explain(forecast_id)

    def ensemble_performance(self, **kwargs: Any) -> dict[str, Any]:
        return self.ensemble_service().performance(**kwargs)

    def ensemble_calibration(self, instrument_id: str, horizon: str) -> dict[str, Any]:
        return self.ensemble_service().calibration(instrument_id, horizon)

    # -- LSTM sequence models (PI-07) ----------------------------------------

    def lstm_service(self) -> LSTMForecastingService:
        """Open PI-07 over the existing PI-01/PI-02/PI-04/PI-05 stores."""
        if self._lstm_service is not None:
            return self._lstm_service
        if self._lifecycle.state not in (
            LifecycleState.READY,
            LifecycleState.RUNNING,
            LifecycleState.DEGRADED,
        ):
            raise NotOperationalError(
                "LSTM forecasting is not initialised; enable NovaMarket first"
            )
        market = self._ensure_market_data()
        try:
            prediction = self.prediction_service()
        except Exception as exc:  # noqa: BLE001
            self._lstm_error = f"prediction: {type(exc).__name__}: {exc}"
            raise
        try:
            forecast = self._forecast_service()
            forecast_store = self._forecast_store
            if forecast_store is None:
                raise RuntimeError("the NM-06 forecast store is not open")
        except Exception as exc:  # noqa: BLE001
            self._lstm_error = f"forecast: {type(exc).__name__}: {exc}"
            raise
        dataset = None
        try:
            dataset = self.dataset_service()
        except Exception as exc:  # noqa: BLE001 - direct bars remain usable
            self._lstm_error = f"dataset: {type(exc).__name__}: {exc}"
        features = None
        try:
            features = self.feature_service()
        except Exception as exc:  # noqa: BLE001 - training reports the missing engine
            self._lstm_error = f"features: {type(exc).__name__}: {exc}"
        artifact_root = None
        if self._data_dir is not None:
            artifact_root = self._data_dir / "lstm_models"
        try:
            service = LSTMForecastingService(
                market_data=market,
                dataset_service=dataset,
                feature_service=features,
                prediction_store=prediction.store,
                forecast_store=forecast_store,
                publisher=self._forecast_publish,
                clock=self._clock,
                artifact_root=artifact_root,
            )
        except Exception as exc:  # noqa: BLE001
            self._lstm_error = f"{type(exc).__name__}: {exc}"
            raise
        self._lstm_service = service
        self._lstm_error = ""
        return service

    def lstm_status(self) -> dict[str, Any]:
        if self._lstm_service is None:
            return {
                "phase": "PI-07",
                "implemented": True,
                "initialised": False,
                "error": self._lstm_error,
                "backends": {
                    "lstm": {
                        "available": _lstm_backend_available(),
                        "version": _lstm_backend_version(),
                        "device": "cpu",
                    }
                },
                "authorities": {"risk": "NM-08", "execution": "NM-10"},
            }
        return {
            "phase": "PI-07",
            "implemented": True,
            "initialised": True,
            "error": self._lstm_error,
            **self._lstm_service.status(),
        }

    def lstm_contract(self) -> dict[str, Any]:
        if self._lstm_service is None:
            return {
                "phase": "PI-07",
                "implemented": True,
                "detail": "LSTM sequence models open with the feature engine",
            }
        return self._lstm_service.contract()

    async def lstm_forecast(
        self, instrument_id: str, horizon: str, **kwargs: Any
    ) -> dict[str, Any]:
        return await self.lstm_service().generate(instrument_id, horizon, **kwargs)

    async def lstm_evaluate(
        self, instrument_id: str, horizon: str, **kwargs: Any
    ) -> dict[str, Any]:
        return await self.lstm_service().evaluate(instrument_id, horizon, **kwargs)

    def lstm_models(self, **kwargs: Any) -> dict[str, Any]:
        return self.lstm_service().models(**kwargs)

    def lstm_explain(self, forecast_id: str) -> dict[str, Any]:
        return self.lstm_service().explain(forecast_id)

    def lstm_suitability(self, instrument_id: str, horizon: str, **kwargs: Any) -> dict[str, Any]:
        return self.lstm_service().suitability(instrument_id, horizon, **kwargs)

    # -- gradient models (PI-06) ---------------------------------------------

    def gradient_service(self) -> GradientForecastingService:
        """Open PI-06 over the existing PI-01/PI-02/PI-04/PI-05 stores."""
        if self._gradient_service is not None:
            return self._gradient_service
        if self._lifecycle.state not in (
            LifecycleState.READY,
            LifecycleState.RUNNING,
            LifecycleState.DEGRADED,
        ):
            raise NotOperationalError(
                "gradient forecasting is not initialised; enable NovaMarket first"
            )
        market = self._ensure_market_data()
        try:
            prediction = self.prediction_service()
        except Exception as exc:  # noqa: BLE001
            self._gradient_error = f"prediction: {type(exc).__name__}: {exc}"
            raise
        try:
            forecast = self._forecast_service()
            forecast_store = self._forecast_store
            if forecast_store is None:
                raise RuntimeError("the NM-06 forecast store is not open")
        except Exception as exc:  # noqa: BLE001
            self._gradient_error = f"forecast: {type(exc).__name__}: {exc}"
            raise
        dataset = None
        try:
            dataset = self.dataset_service()
        except Exception as exc:  # noqa: BLE001 - direct bars remain usable
            self._gradient_error = f"dataset: {type(exc).__name__}: {exc}"
        features = None
        try:
            features = self.feature_service()
        except Exception as exc:  # noqa: BLE001 - training reports the missing engine
            self._gradient_error = f"features: {type(exc).__name__}: {exc}"
        artifact_root = None
        if self._data_dir is not None:
            artifact_root = self._data_dir / "gradient_models"
        try:
            service = GradientForecastingService(
                market_data=market,
                dataset_service=dataset,
                feature_service=features,
                prediction_store=prediction.store,
                forecast_store=forecast_store,
                publisher=self._forecast_publish,
                clock=self._clock,
                artifact_root=artifact_root,
            )
        except Exception as exc:  # noqa: BLE001
            self._gradient_error = f"{type(exc).__name__}: {exc}"
            raise
        self._gradient_service = service
        self._gradient_error = ""
        return service

    def gradient_status(self) -> dict[str, Any]:
        if self._gradient_service is None:
            return {
                "phase": "PI-06",
                "implemented": True,
                "initialised": False,
                "error": self._gradient_error,
                "backends": {
                    family.value: {
                        "available": _gradient_backend_available(family),
                        "version": _gradient_backend_version(family),
                    }
                    for family in GradientFamily
                },
                "authorities": {"risk": "NM-08", "execution": "NM-10"},
            }
        return {
            "phase": "PI-06",
            "implemented": True,
            "initialised": True,
            "error": self._gradient_error,
            **self._gradient_service.status(),
        }

    def gradient_contract(self) -> dict[str, Any]:
        if self._gradient_service is None:
            return {
                "phase": "PI-06",
                "implemented": True,
                "detail": "gradient models open with the market-data engine",
            }
        return self._gradient_service.contract()

    async def gradient_forecast(
        self, instrument_id: str, horizon: str, **kwargs: Any
    ) -> dict[str, Any]:
        return await self.gradient_service().generate(instrument_id, horizon, **kwargs)

    async def gradient_evaluate(
        self, instrument_id: str, horizon: str, **kwargs: Any
    ) -> dict[str, Any]:
        return await self.gradient_service().evaluate(instrument_id, horizon, **kwargs)

    def gradient_forecasts(self, **kwargs: Any) -> dict[str, Any]:
        return self.gradient_service().forecasts(**kwargs)

    def gradient_models(self, **kwargs: Any) -> dict[str, Any]:
        return self.gradient_service().models(**kwargs)

    def gradient_explain(self, forecast_id: str) -> dict[str, Any]:
        return self.gradient_service().explain(forecast_id)

    def gradient_suitability(
        self, instrument_id: str, horizon: str, **kwargs: Any
    ) -> dict[str, Any]:
        return self.gradient_service().suitability(instrument_id, horizon, **kwargs)

    # -- statistical baselines (PI-05) ---------------------------------------

    def statistical_service(self) -> StatisticalForecastingService:
        """Open PI-05 over the existing PI-01 and NM-06 stores."""
        if self._statistical_service is not None:
            return self._statistical_service
        if self._lifecycle.state not in (
            LifecycleState.READY,
            LifecycleState.RUNNING,
            LifecycleState.DEGRADED,
        ):
            raise NotOperationalError(
                "statistical forecasting is not initialised; enable NovaMarket first"
            )
        market = self._ensure_market_data()
        try:
            prediction = self.prediction_service()
        except Exception as exc:  # noqa: BLE001
            self._statistical_error = f"prediction: {type(exc).__name__}: {exc}"
            raise
        try:
            forecast = self._forecast_service()
            forecast_store = self._forecast_store
            if forecast_store is None:
                raise RuntimeError("the NM-06 forecast store is not open")
        except Exception as exc:  # noqa: BLE001
            self._statistical_error = f"forecast: {type(exc).__name__}: {exc}"
            raise
        dataset = None
        try:
            dataset = self.dataset_service()
        except Exception as exc:  # noqa: BLE001 - direct bars remain usable
            self._statistical_error = f"dataset: {type(exc).__name__}: {exc}"
        features = None
        try:
            features = self.feature_service()
        except Exception as exc:  # noqa: BLE001 - regimes remain optional context
            self._statistical_error = f"features: {type(exc).__name__}: {exc}"
        try:
            service = StatisticalForecastingService(
                market_data=market,
                dataset_service=dataset,
                feature_service=features,
                prediction_store=prediction.store,
                forecast_store=forecast_store,
                publisher=self._forecast_publish,
                clock=self._clock,
            )
        except Exception as exc:
            self._statistical_error = f"{type(exc).__name__}: {exc}"
            raise
        self._statistical_service = service
        self._statistical_error = ""
        return service

    def statistical_status(self) -> dict[str, Any]:
        if self._statistical_service is None:
            try:
                self.statistical_service()
            except Exception as exc:  # noqa: BLE001 - a status read must not raise
                return {
                    "phase": "PI-05",
                    "implemented": True,
                    "initialised": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "authorities": {"risk": "NM-08", "execution": "NM-10"},
                }
        try:
            service = self._statistical_service
            if service is None:
                raise RuntimeError("statistical service did not open")
            return {
                "phase": "PI-05",
                "implemented": True,
                "initialised": True,
                "error": self._statistical_error,
                **service.status(),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "phase": "PI-05",
                "implemented": True,
                "initialised": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def statistical_contract(self) -> dict[str, Any]:
        if self._statistical_service is None:
            return {
                "phase": "PI-05",
                "implemented": True,
                "detail": "statistical baselines open with the market-data engine",
            }
        return self._statistical_service.contract()

    async def statistical_forecast(
        self, instrument_id: str, horizon: str, **kwargs: Any
    ) -> dict[str, Any]:
        return await self.statistical_service().generate(instrument_id, horizon, **kwargs)

    async def statistical_evaluate(
        self, instrument_id: str, horizon: str, **kwargs: Any
    ) -> dict[str, Any]:
        return await self.statistical_service().evaluate(instrument_id, horizon, **kwargs)

    def statistical_forecasts(self, **kwargs: Any) -> dict[str, Any]:
        return self.statistical_service().forecasts(**kwargs)

    def statistical_models(self, **kwargs: Any) -> dict[str, Any]:
        return self.statistical_service().models(**kwargs)

    def statistical_evaluations(self, **kwargs: Any) -> dict[str, Any]:
        return self.statistical_service().evaluations(**kwargs)

    def statistical_suitability(
        self, instrument_id: str, horizon: str, **kwargs: Any
    ) -> dict[str, Any]:
        return self.statistical_service().suitability(instrument_id, horizon, **kwargs)

    def watchlists(self) -> dict[str, Any]:
        return self._discovery_service().watchlists()

    def watchlist(self, watchlist_id: str, *, limit: int = 100) -> dict[str, Any]:
        return self._discovery_service().watchlist(watchlist_id, limit=limit)

    async def create_watchlist(
        self, name: str, *, kind: str = "custom", description: str = ""
    ) -> dict[str, Any]:
        try:
            parsed = DiscoveryWatchlistKind(str(kind))
        except ValueError as exc:
            allowed = ", ".join(item.value for item in DiscoveryWatchlistKind)
            raise DiscoveryValidationError(
                f"unknown watchlist kind {kind!r}; known: {allowed}"
            ) from exc
        return await self._discovery_service().create_watchlist(
            name, parsed, description=description
        )

    async def watchlist_add(
        self, watchlist_id: str, instrument_id: str, *, reason: str = ""
    ) -> dict[str, Any]:
        return await self._discovery_service().add_instrument(
            watchlist_id, instrument_id, reason=reason
        )

    async def watchlist_remove(
        self, watchlist_id: str, instrument_id: str, *, reason: str = ""
    ) -> dict[str, Any]:
        return await self._discovery_service().remove_instrument(
            watchlist_id, instrument_id, reason=reason
        )

    async def rename_watchlist(self, watchlist_id: str, name: str) -> dict[str, Any]:
        """Rename a persisted watchlist without changing its stable id."""
        return await self._discovery_service().rename_watchlist(watchlist_id, name)

    async def _withdraw(self) -> None:
        """Remove every runtime contribution, swallowing failures by design."""
        await self._unsubscribe_all()
        gateway = self._gateway
        self._gateway = None
        if gateway is not None:
            try:
                await gateway.stop()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket gateway did not stop cleanly: %s", exc)
        engine = self._market_engine
        if engine is not None:
            try:
                await engine.stop_ingestion()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                self._last_error = f"ingestion stop failed: {exc}"
        self._market_engine = None
        store = self._market_store
        self._market_store = None
        if store is not None:
            try:
                store.close()
            except Exception as exc:  # noqa: BLE001 - a closed store is not a crash
                _LOGGER.warning("NovaMarket market-data store did not close cleanly: %s", exc)
        self._market_ingestion_error = ""
        radar_engine = self._radar_engine
        self._radar_engine = None
        self._radar_store = None
        if radar_engine is not None:
            try:
                radar_engine.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket event radar did not close cleanly: %s", exc)
        discovery_engine = self._discovery_engine
        self._discovery_engine = None
        self._discovery_store = None
        if discovery_engine is not None:
            try:
                await discovery_engine.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket asset discovery did not close cleanly: %s", exc)
        self._discovery_error = ""
        gradient_service = self._gradient_service
        self._gradient_service = None
        if gradient_service is not None:
            try:
                gradient_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket gradient forecasting did not close cleanly: %s", exc)
        self._gradient_error = ""
        lstm_service = self._lstm_service
        self._lstm_service = None
        if lstm_service is not None:
            try:
                lstm_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket LSTM forecasting did not close cleanly: %s", exc)
        self._lstm_error = ""
        statistical_service = self._statistical_service
        self._statistical_service = None
        if statistical_service is not None:
            try:
                statistical_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket statistical forecasting did not close cleanly: %s", exc)
        self._statistical_error = ""
        self._outlook_service = None
        forecast_engine = self._forecast_engine
        self._forecast_engine = None
        self._forecast_store = None
        if forecast_engine is not None:
            try:
                await forecast_engine.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket forecasting did not close cleanly: %s", exc)
        feature_service = self._feature_service
        self._feature_service = None
        self._feature_error = ""
        if feature_service is not None:
            try:
                feature_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket feature engineering did not close cleanly: %s", exc)
        dataset_service = self._dataset_service
        self._dataset_service = None
        self._dataset_error = ""
        if dataset_service is not None:
            try:
                dataset_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket historical datasets did not close cleanly: %s", exc)
        prediction = self._prediction_service
        self._prediction_service = None
        self._prediction_error = ""
        if prediction is not None:
            try:
                prediction.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket prediction intelligence did not close cleanly: %s", exc)
        portfolio_engine = self._portfolio_engine
        self._portfolio_engine = None
        self._portfolio_store = None
        if portfolio_engine is not None:
            try:
                portfolio_engine.close_engine()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket portfolios did not close cleanly: %s", exc)
        risk_engine = self._risk_engine
        self._risk_engine = None
        risk_store = self._risk_store
        self._risk_store = None
        if risk_engine is not None and risk_store is not None:
            try:
                risk_store.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket risk did not close cleanly: %s", exc)
        self._risk_error = ""
        autonomy_exits = self._autonomy_exits
        self._autonomy_exits = None
        if autonomy_exits is not None:
            try:
                autonomy_exits.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket AT-05 exits did not close cleanly: %s", exc)
        self._autonomy_exits_error = ""
        autonomy_execution = self._autonomy_execution
        self._autonomy_execution = None
        if autonomy_execution is not None:
            try:
                autonomy_execution.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket AT-04 execution did not close cleanly: %s", exc)
        self._autonomy_execution_error = ""
        execution_engine = self._execution_engine
        self._execution_engine = None
        self._execution_store = None
        if execution_engine is not None:
            try:
                await execution_engine.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket paper execution did not close cleanly: %s", exc)
        self._execution_error = ""
        strategy_engine = self._strategy_engine
        self._strategy_engine = None
        strategy_store = self._strategy_store
        self._strategy_store = None
        if strategy_engine is not None and strategy_store is not None:
            try:
                strategy_store.close()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("NovaMarket strategies did not close cleanly: %s", exc)
        self._strategy_error = ""
        self._forecast_error = ""
        backtest_service = self._backtest_service
        self._backtest_service = None
        if backtest_service is not None:
            try:
                backtest_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket backtests did not close cleanly: %s", exc)
        self._backtest_error = ""
        training_service = self._training_service
        self._training_service = None
        if training_service is not None:
            try:
                training_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket training did not close cleanly: %s", exc)
        self._training_error = ""
        adaptive_service = self._adaptive_service
        self._adaptive_service = None
        if adaptive_service is not None:
            try:
                adaptive_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket adaptive intelligence did not close cleanly: %s", exc)
        self._adaptive_error = ""
        multiagent_service = self._multiagent_service
        self._multiagent_service = None
        if multiagent_service is not None:
            try:
                multiagent_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning(
                    "NovaMarket multi-agent intelligence did not close cleanly: %s",
                    exc,
                )
        self._multiagent_error = ""
        optimization_service = self._optimization_service
        self._optimization_service = None
        if optimization_service is not None:
            try:
                optimization_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning(
                    "NovaMarket portfolio optimization did not close cleanly: %s",
                    exc,
                )
        self._optimization_error = ""
        research_service = self._research_service
        self._research_service = None
        if research_service is not None:
            try:
                research_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning(
                    "NovaMarket financial research did not close cleanly: %s",
                    exc,
                )
        self._research_error = ""
        dashboard_service = self._dashboard_service
        self._dashboard_service = None
        if dashboard_service is not None:
            try:
                dashboard_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning(
                    "NovaMarket dashboard did not close cleanly: %s",
                    exc,
                )
        self._dashboard_error = ""
        portfolio_management_service = self._portfolio_management_service
        self._portfolio_management_service = None
        if portfolio_management_service is not None:
            try:
                portfolio_management_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning(
                    "NovaMarket portfolio management did not close cleanly: %s",
                    exc,
                )
        self._portfolio_management_error = ""
        risk_intelligence_service = self._risk_intelligence_service
        self._risk_intelligence_service = None
        if risk_intelligence_service is not None:
            try:
                risk_intelligence_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning(
                    "NovaMarket risk intelligence did not close cleanly: %s",
                    exc,
                )
        self._risk_intelligence_error = ""
        forecast_intelligence_service = self._forecast_intelligence_service
        self._forecast_intelligence_service = None
        if forecast_intelligence_service is not None:
            try:
                forecast_intelligence_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning(
                    "NovaMarket forecast intelligence did not close cleanly: %s",
                    exc,
                )
        self._forecast_intelligence_error = ""
        event_intelligence_service = self._event_intelligence_service
        self._event_intelligence_service = None
        if event_intelligence_service is not None:
            try:
                event_intelligence_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning(
                    "NovaMarket event intelligence did not close cleanly: %s",
                    exc,
                )
        self._event_intelligence_error = ""
        reward_service = self._reward_service
        self._reward_service = None
        if reward_service is not None:
            try:
                reward_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket rewards did not close cleanly: %s", exc)
        self._reward_error = ""
        rl_service = self._rl_service
        self._rl_service = None
        if rl_service is not None:
            try:
                rl_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket RL environment did not close cleanly: %s", exc)
        self._rl_error = ""
        self._rl_forecast_cache.clear()
        evaluation_service = self._evaluation_service
        self._evaluation_service = None
        if evaluation_service is not None:
            try:
                evaluation_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket forecast evaluation did not close cleanly: %s", exc)
        self._evaluation_error = ""
        shadow_service = self._shadow_service
        self._shadow_service = None
        if shadow_service is not None:
            try:
                await shadow_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket shadow mode did not close cleanly: %s", exc)
        self._shadow_error = ""
        learning_service = self._learning_service
        self._learning_service = None
        if learning_service is not None:
            try:
                await learning_service.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket learning did not close cleanly: %s", exc)
        self._learning_error = ""
        wallet_engine = self._wallet_engine
        self._wallet_engine = None
        self._wallet_store = None
        if wallet_engine is not None:
            try:
                wallet_engine.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket wallet ledger did not close cleanly: %s", exc)
        decision_pipeline = self._decision_pipeline
        self._decision_pipeline = None
        if decision_pipeline is not None:
            try:
                decision_pipeline.close()
            except Exception:  # noqa: BLE001 - shutdown must continue
                pass
        self._decision_error = ""
        self._pi12_decision_source_cache = None
        self._prediction_outcome_last = {}
        self._prediction_outcome_error = ""
        autonomy_runtime = self._autonomy_runtime
        if autonomy_runtime is not None:
            try:
                autonomy_runtime.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning("NovaMarket autonomy shell did not close cleanly: %s", exc)
        self._autonomy_error = ""
        self._unregister_capabilities()
        try:
            MarketPermissionModel.undeclare(self._permissions, self._declared_permissions)
        except Exception as exc:  # noqa: BLE001 - a failed withdraw is not a crash
            self._last_event_error = f"permission withdraw failed: {exc}"
        self._declared_permissions = ()

    async def _handle_configuration_request(self, event: Event) -> None:
        payload = event.payload.get("configuration")
        if not isinstance(payload, Mapping):
            await self._publish(
                EventType.NOVAMARKET_WARNING,
                code="invalid_configuration_request",
                detail="configuration_requested requires an object at 'configuration'",
                correlation_id=event.correlation_id,
            )
            return
        try:
            await self.update_config(dict(payload), reason=f"bus request {event.correlation_id}")
        except (NovaMarketError, TypeError, ValueError) as exc:
            await self._publish(
                EventType.NOVAMARKET_WARNING,
                code="configuration_rejected",
                detail=str(exc),
                correlation_id=event.correlation_id,
            )

    async def _publish(
        self,
        event_type: EventType,
        *,
        correlation_id: str = "",
        **payload: Any,
    ) -> None:
        """Publish through the contract; a failure is recorded, never raised."""
        try:
            await self._publisher.publish(event_type, correlation_id=correlation_id, **payload)
        except Exception as exc:  # noqa: BLE001 - announcements never fail the work
            self._last_event_error = f"{type(exc).__name__}: {exc}"
            self._counters["event_failures"] += 1
            _LOGGER.warning("NovaMarket event %s was not published: %s", event_type, exc)
            return

    def _set_health_from_records(self) -> None:
        report = self.health()
        self._health_state = report.state
        self._health_reasons = report.reasons

    async def _announce_health(self, previous: str) -> None:
        current = self._health_state.value
        if current == previous:
            return
        await self._publish(
            EventType.NOVAMARKET_HEALTH_CHANGED,
            health=current,
            previous=previous,
        )


__all__ = [
    "CAPABILITIES",
    "MarketCapability",
    "MarketDependency",
    "MarketHealth",
    "NovaMarketService",
    "STATE_NAMESPACE",
    "StateStore",
]
