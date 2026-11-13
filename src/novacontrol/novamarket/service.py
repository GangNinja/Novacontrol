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
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:  # pragma: no cover - typing only
    from novacontrol.novamarket.portfolio import PortfolioEngine
    from novacontrol.novamarket.risk import RiskEngine
    from novacontrol.novamarket.shadow import ShadowService
    from novacontrol.novamarket.backtesting import BacktestingService
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
    ROADMAP,
    STORAGE_BOUNDARIES,
    validate_architecture,
)
from novacontrol.novamarket.config import NovaMarketConfig
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

from novacontrol.novamarket.forecasting import (
    ForecastEngine,
    ForecastPolicy,
    ForecastStore,
    Horizon,
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
        description=(
            "Search stored market events, their evidence, mappings and history."
        ),
        operation=MarketOperation.READ_EVENTS,
    ),
    MarketCapability(
        capability_id="market.events.refresh",
        name="Refresh event sources",
        description=(
            "Fetch a configured event feed and store the events it reports."
        ),
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
        description=(
            "Read scored candidates, their factor breakdown and evaluation history."
        ),
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
    ),    MarketCapability(
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
        self._market_data_path = (
            Path(market_data_path) if market_data_path is not None else None
        )
        self._transport = transport
        self._secret_resolver: SecretResolver = (
            secret_resolver or EnvironmentSecretResolver()
        )
        self._market_store: MarketDataStore | None = None
        self._market_engine: MarketDataEngine | None = None
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
        self._execution_engine: Any | None = None
        self._execution_store: Any | None = None
        self._execution_error = ""
        # AT-01: the durable autonomy shell. This object owns a mode and a
        # lease only; it has no strategy, risk, execution or portfolio path.
        self._autonomy_runtime: AutonomyRuntime | None = None
        self._autonomy_monitoring: AutonomyMonitoring | None = None
        self._decision_pipeline: Any | None = None
        self._decision_error = ""
        # AT-04: controlled paper-order forwarding through the one NM-10 engine.
        self._autonomy_execution: Any | None = None
        self._autonomy_execution_error = ""
        # AT-05: stop-loss supervision over existing positions and NM-08/NM-10.
        self._autonomy_exits: Any | None = None
        self._autonomy_exits_error = ""
        self._autonomy_recovery: dict[str, Any] = {}
        self._autonomy_error = ""
        # NM-11: reproducible historical backtesting. It owns an isolated
        # database and never shares the paper-execution or wallet stores.
        self._backtest_service: Any | None = None
        self._backtest_error = ""
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
        self._publisher = MarketEventPublisher(
            event_bus, emit_enabled=self._config.emit_events
        )
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
                "stored configuration could not be read: "
                f"{type(exc).__name__}: {exc}"
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
                self._autonomy_runtime = AutonomyRuntime(
                    self._autonomy_path(), clock=self._clock
                )
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
            )
        return self._autonomy_monitoring

    def _autonomy_instrument_lookup(
        self, instrument_id: str
    ) -> dict[str, Any] | None:
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
        return runtime.status(
            operator_authorized=operator_authorized,
            operator_configured=configured,
        )

    def autonomy_start(
        self,
        mode: AutonomyMode | str,
        *,
        reason: str = "",
        acknowledge_paper_only: bool = False,
    ) -> dict[str, Any]:
        return self._ensure_autonomy().start(
            mode,
            reason=reason,
            acknowledge_paper_only=acknowledge_paper_only,
        )

    def autonomy_pause(self, *, reason: str = "") -> dict[str, Any]:
        return self._ensure_autonomy().pause(reason=reason)

    def autonomy_resume(
        self,
        mode: AutonomyMode | str,
        *,
        reason: str = "",
        acknowledge_paper_only: bool = False,
    ) -> dict[str, Any]:
        return self._ensure_autonomy().resume(
            mode,
            reason=reason,
            acknowledge_paper_only=acknowledge_paper_only,
        )

    def autonomy_stop(self, *, reason: str = "") -> dict[str, Any]:
        return self._ensure_autonomy().stop(reason=reason)

    async def autonomy_recover(self, *, reason: str = "operator recovery") -> dict[str, Any]:
        """Reconcile durable autonomy work without resuming autonomous mode.

        Recovery is a read/reconcile operation, never a replay.  It closes the
        runtime lease, interrupts abandoned cycles, marks abandoned decision
        runs failed, reconciles NM-10 order/fill records and pending exit
        decisions.  The resulting mode is always ``disabled``; an operator must
        explicitly start or resume afterwards.
        """

        runtime = self._ensure_autonomy()
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
        """Static AT-01 metadata; no user or financial record is included."""

        return {
            "milestone": "AT-01",
            "status": "runtime safety shell only",
            "trading_implemented": TRADING_IMPLEMENTED,
            "paper_only": PAPER_ONLY,
            "live_trading": LIVE_TRADING,
            "broker_connected": False,
            "orders_possible": False,
            "release_gate": "closed",
            "modes": [mode.value for mode in AutonomyMode],
            "runtime_states": [
                "stopped",
                "starting",
                "running",
                "recovering",
                "degraded",
                "stopping",
            ],
            "mode_transitions": {
                mode.value: sorted(item.value for item in targets)
                for mode, targets in MODE_TRANSITIONS.items()
            },
            "one_cycle_at_a_time": True,
            "paused_exit_policy": "protective",
            "exit_supervision_implemented": False,
            "learning_promotion_implemented": False,
            "horizon_mapping": dict(HORIZON_MAPPING),
            "calendar": dict(CALENDAR_STATUS),
            "storage": {
                "schema": "autonomy_meta",
                "version": 3,
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
                "AT-01 has no scan, signal, proposal, order, fill, exit or learning promotion path.",
                "Autonomous is a persisted shell state; it does not enable trading.",
                "AT-02 monitoring discovers and monitors instruments only; it creates no signal, proposal, order or fill.",
                "Decision, execution and exit supervision remain AT-03 through AT-06 and are not implemented.",
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
                raise MonitoringRefused(
                    "CYCLE_NOT_FOUND", f"unknown monitoring cycle {cycle_id!r}"
                )
            monitoring = self._ensure_monitoring()
        return monitoring.cycle(cycle_id)

    async def autonomy_monitoring_run(
        self, *, trigger: str = "manual"
    ) -> dict[str, Any]:
        """Run one authorized monitoring cycle through the same runner."""

        return await self._ensure_monitoring().run_cycle(trigger=trigger)

    def autonomy_monitoring_configure(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Persist validated monitoring configuration; no unsafe value is accepted."""

        monitoring = self._ensure_monitoring()
        current = monitoring.config()
        config = MonitoringConfig(
            enabled=bool(payload.get("enabled", current.enabled)),
            exchange=str(payload.get("exchange", current.exchange)),
            interval_seconds=int(
                payload.get("interval_seconds", current.interval_seconds)
            ),
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

    # -- AT-03 decision and risk pipeline ------------------------------------

    def _ensure_decision(self) -> Any:
        """Open the AT-03 pipeline over the already accepted engines.

        It composes NM-09 evaluate() and NM-08 assess(); it never installs an
        NM-09 forwarder and never touches NM-10.  A restart marks an abandoned
        AT-03 run failed instead of resuming it.
        """

        from novacontrol.novamarket.decision import AutonomyDecisionPipeline

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
        return self._learning().record_evidence({
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
        })

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
                "implemented": True, "available": False, "phase": "AT-05",
                "paper_only": True, "live_trading": False, "broker_connected": False,
                "supported_exit_conditions": ["stop_loss", "target_price", "trailing_stop"],
                "unsupported_exit_conditions": [],
                "supported_directions": ["long"],
                "detail": "AT-05 supervision opens with the NovaMarket runtime",
            }
        return cast(dict[str, Any], pipeline.contract())

    def autonomy_exits_status(self) -> dict[str, Any]:
        pipeline = self._autonomy_exits
        if pipeline is None:
            return {"ok": True, "initialised": False, "phase": "AT-05",
                    "paper_only": True, "live_trading": False,
                    "last_error": self._autonomy_exits_error}
        body = cast(dict[str, Any], pipeline.status())
        body["supervision"] = pipeline.supervision_view()
        return body

    async def run_autonomy_exits(self, *, trigger: str = "operator") -> dict[str, Any]:
        return cast(dict[str, Any],
                    await self._ensure_autonomy_exits().run_cycle(trigger=trigger))

    def autonomy_exit_decisions(self, *, portfolio_id: str = "", limit: int = 100) -> dict[str, Any]:
        if self._autonomy_exits is not None:
            return cast(dict[str, Any],
                        self._autonomy_exits.decisions(portfolio_id=portfolio_id, limit=limit))
        rows = self._execution_service().store.exit_decisions(
            portfolio_id=portfolio_id, limit=limit)
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
        return cast(dict[str, Any],
                    await self._ensure_autonomy_exits().reconcile_decision(decision_id))

    def autonomy_exit_events(self, decision_id: str, *, limit: int = 200) -> dict[str, Any]:
        if self._autonomy_exits is not None:
            return cast(dict[str, Any], self._autonomy_exits.events(decision_id, limit=limit))
        row = self._execution_service().store.exit_decision(decision_id=decision_id)
        if row is None:
            from novacontrol.novamarket.autonomy_exits import ExitNotFound
            raise ExitNotFound(f"unknown exit decision {decision_id!r}")
        events = self._execution_service().store.exit_events(decision_id, limit=limit)
        return {"ok": True, "decision_id": decision_id, "events": list(events),
                "total": len(events)}

    def autonomy_exit_positions(self, *, limit: int = 200) -> dict[str, Any]:
        rows = self._execution_service().store.exit_states(limit=limit)
        return {"ok": True, "phase": "AT-05", "positions": list(rows),
                "total": len(rows), "paper_only": True, "live_trading": False}

    def autonomy_exit_configurations(self, *, limit: int = 200) -> dict[str, Any]:
        rows = self._execution_service().store.exit_configurations(limit=limit)
        return {"ok": True, "phase": "AT-05", "configurations": list(rows),
                "total": len(rows), "paper_only": True, "live_trading": False}

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
            (
                row for row in holdings
                if str(row.get("instrument_id") or "") == instrument_id
            ),
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
            payload["target_price"] if "target_price" in payload
            else existing.get("target_price")
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
                if "trailing_stop_fraction" in payload else None
            ),
            trailing_stop_percent=trailing_percent,
            average_cost=holding.get("average_cost"),
            existing=existing,
        )
        stored = store.save_exit_configuration(body)
        return {"ok": True, "phase": "AT-05", "configuration": stored,
                "paper_only": True, "live_trading": False}

    def delete_autonomy_exit_configuration(
        self, portfolio_id: str, instrument_id: str
    ) -> dict[str, Any]:
        removed = self._execution_service().store.delete_exit_configuration(
            portfolio_id, instrument_id
        )
        return {"ok": True, "removed": removed, "portfolio_id": portfolio_id,
                "instrument_id": instrument_id, "paper_only": True,
                "live_trading": False}

    def autonomy_learning_evidence(self, *, decision_id: str = "", limit: int = 100) -> dict[str, Any]:
        if self._autonomy_exits is not None:
            return cast(dict[str, Any],
                        self._autonomy_exits.learning(decision_id=decision_id, limit=limit))
        rows = self._execution_service().store.learning_evidence(
            decision_id=decision_id, limit=limit)
        return {"ok": True, "evidence": list(rows), "total": len(rows),
                "advisory_only": True, "auto_promoted": False}

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

    def autonomy_execution_events(
        self, execution_id: str, *, limit: int = 200
    ) -> dict[str, Any]:
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
            result = await pipeline.run_cycle(
                trigger=trigger, actor=actor, proposal_id=proposal_id
            )
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
            return Path(self._market_data_path).with_name(
                "novamarket_shadow.sqlite3"
            )
        return ":memory:"

    def _learning_path(self) -> Any:
        """NM-13's own database file, never the market/wallet/shadow file."""
        if self._data_dir is not None:
            return self._data_dir / "novamarket_learning.sqlite3"
        if self._market_data_path is not None:
            return Path(self._market_data_path).with_name(
                "novamarket_learning.sqlite3"
            )
        return ":memory:"

    def _radar_enum(
        self, enum_type: Any, value: str, field_name: str
    ) -> Any:
        """Coerce a query string to its enum; an unknown value is a 422, not a 500."""
        try:
            return enum_type(value)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in enum_type)
            raise EventValidationError(
                f"{field_name} must be one of: {allowed}"
            ) from exc

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
            verification=self._radar_enum(
                RadarVerificationState, verification, "verification"
            )
            if verification
            else None,
            importance=self._radar_enum(
                RadarImportance, importance, "importance"
            )
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

    def radar_instrument_events(
        self, instrument_id: str, *, limit: int = 50
    ) -> dict[str, Any]:
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
            store = open_wallet_store(
                self._data_dir, clock=self._clock, path=self._wallet_path
            )
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

    async def _portfolio_publish(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
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
                horizon.value: policy_for(horizon).to_dict()
                for horizon in PortfolioHorizon
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
        return self._portfolio_service().portfolios(
            horizon=horizon, status=status, limit=limit
        )

    def portfolio_detail(self, portfolio_id: str) -> dict[str, Any]:
        return self._portfolio_service().detail(portfolio_id)

    def portfolio_holdings(self, portfolio_id: str) -> dict[str, Any]:
        return self._portfolio_service().holdings(portfolio_id)

    async def portfolio_valuation(self, portfolio_id: str) -> dict[str, Any]:
        return await self._portfolio_service().valuate(portfolio_id)

    def portfolio_valuations(
        self, portfolio_id: str, *, limit: int = 200
    ) -> dict[str, Any]:
        return self._portfolio_service().valuations(portfolio_id, limit=limit)

    def portfolio_performance(self, portfolio_id: str) -> dict[str, Any]:
        return self._portfolio_service().performance(portfolio_id)

    def portfolio_decisions(
        self, portfolio_id: str, *, limit: int = 100
    ) -> dict[str, Any]:
        return self._portfolio_service().decisions(portfolio_id, limit=limit)

    def portfolio_proposals(
        self,
        portfolio_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._portfolio_service().proposals(
            portfolio_id, status=status, limit=limit
        )

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
        return await self._portfolio_service().set_status(
            portfolio_id, action, reason=reason
        )

    async def portfolio_close(
        self, portfolio_id: str, *, reason: str = ""
    ) -> dict[str, Any]:
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

    async def _risk_publish(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
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
        engine.set_market_data(self._market_engine)
        engine.set_forecasts(self._forecast_engine)
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
        return await self._risk_service().update_policy(
            policy_id, changes, actor=actor
        )

    def risk_alerts(self, *, status: str = "", limit: int = 100) -> dict[str, Any]:
        return self._risk_service().alerts(status=status, limit=limit)

    async def risk_acknowledge_alert(
        self, alert_id: str, *, actor: str = ""
    ) -> dict[str, Any]:
        return await self._risk_service().acknowledge_alert(alert_id, actor=actor)

    async def risk_resolve_alert(
        self, alert_id: str, *, actor: str = ""
    ) -> dict[str, Any]:
        return await self._risk_service().resolve_alert(alert_id, actor=actor)

    async def risk_stress(
        self, portfolio_id: str, scenario: str = "market_decline", *, actor: str = ""
    ) -> dict[str, Any]:
        return await self._risk_service().stress(
            portfolio_id, scenario, actor=actor
        )

    def risk_stress_results(
        self, portfolio_id: str = "", limit: int = 50
    ) -> dict[str, Any]:
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
        engine.set_radar(self._radar_engine)
        engine.set_discovery(self._discovery_engine)
        engine.set_risk(self._risk_service())
        engine.set_forwarder(self._forward_strategy_to_execution)
        return engine

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

    async def _execution_publish(
        self, event_type: str, payload: Mapping[str, Any]
    ) -> None:
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
                and (
                    str(row.get("policy_id")) == version
                    or str(row.get("version")) == version
                )
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
        if self._forecast_engine is None:
            return ()
        body = self._forecast_engine.forecasts(instrument_id=instrument_id, limit=200)
        return tuple(dict(row) for row in body.get("forecasts") or ())

    async def _backtest_publish(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
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
        return await asyncio.to_thread(
            self._backtesting().validate, payload, load_data=load_data
        )

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
        return await self._backtesting().walk_forward(
            backtest_id, train_bars, test_bars
        )

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

    async def _shadow_publish(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
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
        return await self._shadowing().run_cycle(
            session_id, observations=body.get("observations")
        )

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

    def shadow_projections(
        self, session_id: str, limit: int = 200
    ) -> dict[str, Any]:
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

    def shadow_comparisons(
        self, session_id: str, limit: int = 50
    ) -> dict[str, Any]:
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

    async def _learning_publish(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
        await self._publish(EventType(str(event_type)), **payload)

    def _learning_apply(
        self, strategy_id: str, config: Mapping[str, Any]
    ) -> Mapping[str, Any]:
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
        selected = [
            row for row in rows if str(row.get("strategy_id") or "") == strategy_id
        ]
        fills: list[dict[str, Any]] = []
        for row in selected:
            fills.extend(
                engine.fills(str(row.get("order_id"))).get("fills") or ()
            )
        stamps = [
            str(row.get("created_at"))
            for row in selected
            if row.get("created_at")
        ]
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
            "strategy_version": first.get("strategy_version")
            or record.get("version"),
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

    def learning_record_evidence(
        self, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
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
        return self._learning().monitor(
            strategy_id, kind=kind, window_days=window_days
        )

    def learning_alerts(
        self, *, strategy_id: str = "", status: str = "", limit: int = 100
    ) -> dict[str, Any]:
        return self._learning().alerts(
            strategy_id=strategy_id, status=status, limit=limit
        )

    def learning_acknowledge_alert(
        self, alert_id: str, actor: str = ""
    ) -> dict[str, Any]:
        return self._learning().acknowledge_alert(alert_id, actor)

    def learning_create_experiment(
        self, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._learning().create_experiment(payload)

    async def learning_start_experiment(
        self, experiment_id: str
    ) -> dict[str, Any]:
        return await self._learning().start_experiment(experiment_id)

    async def learning_run_experiment(
        self, experiment_id: str
    ) -> dict[str, Any]:
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

    async def learning_evaluate_candidate(
        self, candidate_id: str
    ) -> dict[str, Any]:
        return await self._learning().evaluate_candidate(candidate_id)

    def learning_approve_candidate(
        self, candidate_id: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._learning().approve_candidate(candidate_id, payload)

    def learning_reject_candidate(
        self, candidate_id: str, reason: str = "", actor: str = ""
    ) -> dict[str, Any]:
        return self._learning().reject_candidate(
            candidate_id, reason=reason, actor=actor
        )

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

    def learning_rollbacks(
        self, strategy_id: str = "", limit: int = 50
    ) -> dict[str, Any]:
        return self._learning().rollbacks(strategy_id=strategy_id, limit=limit)

    def strategy_catalog(self) -> dict[str, Any]:
        return self._strategy_service().catalog()  # type: ignore[no-any-return]

    def strategies(self, *, status: str = "", category: str = "", limit: int = 100) -> dict[str, Any]:
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
        self, *, strategy_id: str = "", portfolio_id: str = "", signal_type: str = "", limit: int = 200
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
        return await self._wallet_service().create_wallet(
            owner, label=label, wallet_id=wallet_id
        )

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
        return await self._wallet_service().set_wallet_status(
            wallet_id, status, reason=reason
        )

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
                "balance": (
                    "stored balance equals the sum of the wallet's signed entries"
                ),
                "double_entry": "every transaction's entries sum to zero",
                "issuance_account": (
                    "novamarket:issuance carries the negative side of deposits "
                    "and withdrawals"
                ),
            },
            "append_only": {
                "tables": ["ledger_entries", "wallet_transactions"],
                "enforced_by": "SQLite triggers that abort UPDATE and DELETE",
                "corrections": (
                    "a reversal transaction with mirrored entries and a required "
                    "reason"
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
                "NovaMarket market data is not initialised; enable NovaMarket "
                "first"
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
            _LOGGER.warning(
                "NovaMarket could not read verified provider operations: %s", exc
            )
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
        self._unknown_providers = tuple(
            name for name in configured if name not in installed
        )
        if configured:
            return tuple(name for name in configured if name in installed)
        # Nothing configured: enable the installed adapters that can actually
        # work on this machine. An adapter needing a credential is enabled only
        # when its reference resolves — installing it is not the same as having
        # a key, and a provider that must fail every call is not enabled.
        rows = {
            str(row["provider"]): row for row in registry.capability_matrix()
        }
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
        path = (
            self._market_data_path
            or (
                self._data_dir / "novamarket.sqlite3"
                if self._data_dir is not None
                else Path(":memory:")
            )
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

    async def _on_market_event(
        self, name: str, payload: Mapping[str, Any]
    ) -> None:
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
            return
        try:
            await engine.start_ingestion()
        except Exception as exc:  # noqa: BLE001 - report, do not crash the lifecycle
            self._market_ingestion_error = f"{type(exc).__name__}: {exc}"
            _LOGGER.warning(
                "NovaMarket background ingestion did not start: %s", exc
            )

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
            await engine.run_ingestion(
                [instrument_id], trigger="manual", force_quotes=True
            )
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
            "provenance": (
                result.selected.provenance() if result.selected is not None else None
            ),
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

    def _workbench_snapshot(
        self, kind: str, *, interval: str, limit: int
    ) -> dict[str, Any]:
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

    def register_market_instrument(
        self, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
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
            raise ConfigurationError(
                "unknown instrument field(s): " + ", ".join(unknown)
            )
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
                provider_symbols={
                    str(k): str(v) for k, v in provider_symbols.items()
                },
                trading_status=str(payload.get("trading_status") or "unknown"),
                metadata=dict(metadata),
            )
        except (ValueError, TypeError) as exc:
            raise ConfigurationError(f"invalid instrument: {exc}") from exc
        stored = engine.register_instrument(instrument)
        return {"instrument": stored.to_dict()}

    async def lookup_market_instrument(
        self, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
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
                {"provider": item.provider, "ok": item.ok, "detail": item.detail}
                for item in probes
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
                "configuration will persist" if self._store is not None else "configuration is memory-only",
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
                    "enabled: "
                    + ", ".join(self._market_engine.enabled_providers)
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
            reasons.append(
                "configuration could not be persisted: " + self._last_persist_error
            )
            return MarketHealth(HealthState.DEGRADED, tuple(reasons), self._clock())
        if self._market_ingestion_error:
            reasons.append("background ingestion: " + self._market_ingestion_error)
            return MarketHealth(HealthState.DEGRADED, tuple(reasons), self._clock())
        if self._market_engine is not None:
            engine_reasons = self._market_engine.health_reasons()
            if engine_reasons:
                reasons.extend(engine_reasons)
                return MarketHealth(
                    HealthState.DEGRADED, tuple(reasons), self._clock()
                )

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
        self._lifecycle.transition(
            LifecycleState.INITIALIZING, reason=reason or "initialize"
        )
        await self._publish(
            EventType.NOVAMARKET_INITIALIZING,
            contract_version=MARKET_CONTRACT_VERSION,
        )
        try:
            await self._validate_mandatory()
            capability_ids = self._register_capabilities()
            self._declared_permissions = self._permission_model.declare(
                self._permissions
            )
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
            raise NovaMarketInitializationError(
                f"NovaMarket initialization failed: {exc}"
            ) from exc
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
                self._lifecycle.transition(
                    LifecycleState.RUNNING, reason="bus attached"
                )
                self._set_health_from_records()
                self._counters["enables"] += 1
                await self._publish(
                    EventType.NOVAMARKET_ENABLED,
                    lifecycle=LifecycleState.RUNNING.value,
                )
                await self._announce_health(previous_health)
            return self.status()
        if state is not LifecycleState.READY:
            raise NotOperationalError(
                f"cannot enable NovaMarket from {state.value}"
            )

        previous_health = self._health_state.value
        await self._subscribe()
        self._started_at = self._clock()
        if self._subscriptions:
            self._lifecycle.transition(
                LifecycleState.RUNNING, reason=reason or "enable"
            )
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
            raise NotOperationalError(
                f"NovaMarket is {self._lifecycle.state.value}, not degraded"
            )
        previous_health = self._health_state.value
        await self._subscribe()
        if not self._subscriptions:
            raise NovaMarketError(
                "NovaMarket cannot recover: the event bus is still unavailable"
            )
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
            return self.status()
        previous_health = self._health_state.value
        self._lifecycle.transition(
            LifecycleState.SHUTTING_DOWN, reason=reason or "shutdown"
        )
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
            "last_tick": (
                self._runtime_last_tick.isoformat()
                if self._runtime_last_tick
                else None
            ),
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

        AT-01 deliberately opens the autonomy store here as a recovery point.
        Opening it never resumes a persisted mode; the shell always comes up
        disabled until an authorized operator starts it.
        """
        try:
            self._ensure_monitoring()
        except Exception as exc:  # noqa: BLE001 - a store failure must not stop NovaControl
            self._autonomy_error = f"{type(exc).__name__}: {exc}"
        task = self._runtime_task
        if task is not None and not task.done():
            return self.runtime_status()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return self.runtime_status()
        self._runtime_interval_seconds = max(
            1, plan_market_refresh(self._clock()).interval_seconds
        )
        self._runtime_task = loop.create_task(
            self._run_runtime(), name="novamarket-runtime"
        )
        self._start_autonomy_heartbeat()
        return self.runtime_status()

    async def stop_runtime(self) -> dict[str, Any]:
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
                try:
                    runtime.heartbeat()
                except Exception as exc:  # noqa: BLE001 - report, never crash the app
                    self._autonomy_error = f"heartbeat: {type(exc).__name__}: {exc}"
        except asyncio.CancelledError:
            return

    async def _run_runtime(self) -> None:
        # The first tick is deliberately delayed: the API and the market-data
        # store should be ready before a network refresh is attempted, and a
        # test process that starts and stops quickly never reaches a provider.
        try:
            while True:
                plan = plan_market_refresh(self._clock())
                self._runtime_interval_seconds = max(1, plan.interval_seconds)
                await asyncio.sleep(self._runtime_interval_seconds)
                await self._runtime_tick()
        except asyncio.CancelledError:
            return

    async def _runtime_tick(self) -> None:
        self._runtime_last_tick = self._clock()
        if self._lifecycle.state not in (LifecycleState.RUNNING, LifecycleState.DEGRADED):
            return
        if self._config.paused:
            return
        try:
            await self._notification_center.tick()
        except Exception as exc:  # noqa: BLE001 - a monitor must keep running
            self._runtime_error = f"notifications: {type(exc).__name__}: {exc}"

        now = self._clock()
        if self._radar_engine is not None and self._due(
            self._runtime_last_event_refresh, now, 300
        ):
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
                    self._autonomy_error = str(
                        outcome.get("error") or "a monitoring cycle failed"
                    )
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
            self._autonomy_execution_error = (
                f"execution: {type(exc).__name__}: {exc}"
            )

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

    async def sync_market_universe(
        self, *, include_bse: bool = True
    ) -> dict[str, Any]:
        """Register the public NSE/BSE equity catalogue in one bounded pass.

        The source reader is run in a worker thread; the store is written on
        the event loop. Nothing is registered until the source has produced a
        validated plan, so a typo in the upstream CSV cannot half-fill the book.
        """
        engine = self._market_data_engine()
        try:
            plan = await asyncio.to_thread(
                build_indian_universe, include_bse=include_bse
            )
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
                raise ConfigurationError(
                    f"market-data configuration was rejected: {exc}"
                ) from exc
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
        elif "ingestion" in changed and self._market_engine is not None:
            # Turning the worker on/off (or changing its universe) is a
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
            _LOGGER.warning(
                "NovaMarket configuration was not persisted: %s", exc
            )
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
            "activity": self.activity(limit=40)["events"],
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

    def _status_part(
        self, phase: str, reader: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
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
        granted = [
            operation.value
            for operation in self._permission_model.granted_operations()
        ]
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
            "portfolio": self._status_part("NM-07", self.portfolio_status),
            "risk": self._status_part("NM-08", self.risk_status),
            "strategy": self._status_part("NM-09", self.strategy_status),
            "execution": self._status_part("NM-10", self.execution_status),
            "backtesting": self._status_part("NM-11", self.backtest_status),
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
            "initialized_at": (
                self._initialized_at.isoformat() if self._initialized_at else None
            ),
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
            "roadmap": [
                {"phase": phase, "title": title} for phase, title in ROADMAP
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
            "portfolio": self.portfolio_contract(),
            "risk": self.risk_contract(),
            "strategy": self.strategy_contract(),
            "execution": self.execution_contract(),
            "autonomy": self.autonomy_contract(),
            "backtesting": self.backtest_contract(),
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
        _LOGGER.setLevel(
            getattr(logging, self._config.log_level, logging.INFO)
        )

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

    def _capability_availability(
        self, capability: MarketCapability
    ) -> tuple[bool, str]:
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

    async def _discovery_publish(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
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

    def discovery_candidates(
        self, *, limit: int = 50, min_score: float = 0.0
    ) -> dict[str, Any]:
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

    async def discovery_reevaluate(
        self, *, watchlist_id: str | None = None
    ) -> dict[str, Any]:
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

    async def _forecast_publish(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
        await self._publish(EventType(str(event_type)), **payload)

    def _forecast_service(self) -> ForecastEngine:
        engine = self._forecast_engine
        if engine is None:
            if self._lifecycle.state not in (
                LifecycleState.READY,
                LifecycleState.RUNNING,
                LifecycleState.DEGRADED,
            ):
                raise NotOperationalError(
                    "forecasting is not initialised; enable NovaMarket first"
                )
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
            }
        try:
            return self._forecast_service().status()
        except Exception as exc:  # noqa: BLE001 - a read must not crash status
            return {
                "initialised": False,
                "detail": f"{type(exc).__name__}: {exc}",
                "phase": "NM-06",
            }

    async def forecast_generate(
        self, instrument_id: str, horizon: str
    ) -> dict[str, Any]:
        return await self._forecast_service().generate(instrument_id, horizon)

    def forecast_get(self, forecast_id: str) -> dict[str, Any]:
        return self._forecast_service().forecast(forecast_id)

    def forecast_list(
        self,
        *,
        instrument_id: str | None = None,
        horizon: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._forecast_service().forecasts(
            instrument_id=instrument_id,
            horizon=horizon,
            status=status,
            limit=limit,
        )

    def forecast_outcome(self, forecast_id: str) -> dict[str, Any]:
        return self._forecast_service().outcome(forecast_id)

    async def forecast_evaluate(self) -> dict[str, Any]:
        return await self._forecast_service().evaluate_due()

    async def forecast_train(
        self, instrument_id: str, horizon: str
    ) -> dict[str, Any]:
        return await self._forecast_service().train(instrument_id, horizon)

    async def forecast_promote(
        self, model_id: str, instrument_id: str, horizon: str
    ) -> dict[str, Any]:
        return await self._forecast_service().promote(
            model_id, instrument_id, horizon
        )

    async def forecast_health(self) -> dict[str, Any]:
        return await self._forecast_service().check_health()

    def forecast_performance(
        self,
        *,
        model_id: str | None = None,
        instrument_id: str | None = None,
        horizon: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._forecast_service().performance(
            model_id=model_id,
            instrument_id=instrument_id,
            horizon=horizon,
            limit=limit,
        )

    def forecast_models(
        self, *, instrument_id: str | None = None, horizon: str | None = None
    ) -> dict[str, Any]:
        return self._forecast_service().models(
            instrument_id=instrument_id,
            horizon=horizon,
        )

    def forecast_calibration(self, model_id: str, horizon: str) -> dict[str, Any]:
        return self._forecast_service().calibration(model_id, horizon)

    def forecast_runs(self, *, limit: int = 20) -> dict[str, Any]:
        return self._forecast_service().training_runs(limit=limit)

    def forecast_reliability(self, *, model_id: str | None = None) -> dict[str, Any]:
        return self._forecast_service().reliability(model_id=model_id)

    def forecast_contract(self) -> dict[str, Any]:
        engine = self._forecast_engine
        if engine is None:
            return {
                "implemented": False,
                "phase": "NM-06",
                "detail": "the forecast engine is not open in this run",
            }
        return engine.contract()

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
                _LOGGER.warning(
                    "NovaMarket market-data store did not close cleanly: %s", exc
                )
        self._market_ingestion_error = ""
        radar_engine = self._radar_engine
        self._radar_engine = None
        self._radar_store = None
        if radar_engine is not None:
            try:
                radar_engine.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning(
                    "NovaMarket event radar did not close cleanly: %s", exc
                )
        discovery_engine = self._discovery_engine
        self._discovery_engine = None
        self._discovery_store = None
        if discovery_engine is not None:
            try:
                await discovery_engine.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning(
                    "NovaMarket asset discovery did not close cleanly: %s", exc
                )
        self._discovery_error = ""
        forecast_engine = self._forecast_engine
        self._forecast_engine = None
        self._forecast_store = None
        if forecast_engine is not None:
            try:
                await forecast_engine.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning(
                    "NovaMarket forecasting did not close cleanly: %s", exc
                )
        portfolio_engine = self._portfolio_engine
        self._portfolio_engine = None
        self._portfolio_store = None
        if portfolio_engine is not None:
            try:
                portfolio_engine.close_engine()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning(
                    "NovaMarket portfolios did not close cleanly: %s", exc
                )
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
                _LOGGER.warning(
                    "NovaMarket wallet ledger did not close cleanly: %s", exc
                )
        decision_pipeline = self._decision_pipeline
        self._decision_pipeline = None
        if decision_pipeline is not None:
            try:
                decision_pipeline.close()
            except Exception:  # noqa: BLE001 - shutdown must continue
                pass
        self._decision_error = ""
        autonomy_runtime = self._autonomy_runtime
        if autonomy_runtime is not None:
            try:
                autonomy_runtime.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                _LOGGER.warning(
                    "NovaMarket autonomy shell did not close cleanly: %s", exc
                )
        self._autonomy_error = ""
        self._unregister_capabilities()
        try:
            MarketPermissionModel.undeclare(
                self._permissions, self._declared_permissions
            )
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
            await self.update_config(
                dict(payload), reason=f"bus request {event.correlation_id}"
            )
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
            await self._publisher.publish(
                event_type, correlation_id=correlation_id, **payload
            )
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
