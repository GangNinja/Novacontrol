"""NovaMarket's architectural boundaries, roadmap and data ownership.

NM-01 exists to make the next thirteen phases buildable without architectural
rework. That means naming, in one importable place, the *domains* NovaMarket is
divided into, what each one owns, which phase implements it, and where its data
lives. The declarations here are read by the status/contract surfaces and
pinned by tests, so a later phase cannot quietly move a boundary or make a
historical table mutable.

Nothing in this module executes work. A boundary is a statement about
responsibility: the market-data boundary owns quotes, the risk boundary owns
limits and may veto, and the accounting boundary owns ledger entries. Keeping
the statements declarative is what lets the phases arrive one at a time.
"""

from __future__ import annotations

from dataclasses import dataclass

from novacontrol.novamarket.contracts import IMPLEMENTED_PHASE

#: The canonical NovaMarket roadmap, verbatim. Phases are never renamed,
#: merged, reordered or redefined; a phase that changes shape is a new phase.
ROADMAP: tuple[tuple[str, str], ...] = (
    ("NM-01", "Architecture, Contracts & Integration"),
    ("NM-02", "Market Data Reliability"),
    ("NM-03", "NovaCoin Wallet & Deterministic Ledger"),
    ("NM-04", "Evidence-Backed Market Event Radar"),
    ("NM-05", "Autonomous Asset Discovery & Watchlists"),
    ("NM-06", "Measured Probabilistic Forecasting"),
    ("NM-07", "Autonomous Three-Horizon Portfolio Manager"),
    ("NM-08", "Independent Risk Engine"),
    ("NM-09", "Trading Strategies"),
    ("NM-10", "Realistic Paper Execution"),
    ("NM-11", "Reproducible Backtesting"),
    ("NM-12", "Shadow Mode"),
    ("NM-13", "Controlled Adaptive Learning"),
    ("NM-14", "Integrated Workspace & Final Acceptance"),
)

ROADMAP_PHASES: tuple[str, ...] = tuple(phase for phase, _ in ROADMAP)
ROADMAP_TITLES: dict[str, str] = dict(ROADMAP)

#: The Prediction Intelligence program is a separate, additive roadmap. It does
#: not rename, replace or reorder NM-01..NM-14; it gives the new capability its
#: own sixteen phases and its own acceptance gate. PI-01 establishes contracts
#: only. Later phases may implement models, schedulers and learning, but they may
#: never replace NM-08 risk or NM-10 paper execution.
PREDICTION_ROADMAP: tuple[tuple[str, str], ...] = (
    ("PI-01", "Architecture, Contracts & Existing-System Integration"),
    ("PI-02", "Market Data, Features & Dataset Foundations"),
    ("PI-03", "Low-Latency Market Data Gateway"),
    ("PI-04", "Feature Engineering & Market Regime Detection"),
    ("PI-05", "Statistical Baseline Forecasting Models"),
    ("PI-06", "Gradient-Boosting Forecasting Models"),
    ("PI-07", "LSTM Deep Learning Forecasting Models"),
    ("PI-08", "Intraday Prediction Intelligence"),
    ("PI-09", "Swing Prediction Intelligence"),
    ("PI-10", "Long-Term Fundamental Intelligence"),
    ("PI-11", "Prediction Evaluation & Backtesting"),
    ("PI-12", "Paper-Trading Feedback & Outcome Learning"),
    ("PI-13", "Reinforcement-Learning Environment"),
    ("PI-14", "Controlled Policy Evaluation & Governance"),
    ("PI-15", "Observability, Model Registry & Drift"),
    ("PI-16", "Performance, Hardening & Program Acceptance"),
)
PREDICTION_ROADMAP_PHASES: tuple[str, ...] = tuple(
    phase for phase, _ in PREDICTION_ROADMAP
)
PREDICTION_ROADMAP_TITLES: dict[str, str] = dict(PREDICTION_ROADMAP)


@dataclass(frozen=True, slots=True)
class DomainBoundary:
    """One responsibility boundary and the interface it will be reached through.

    ``executes_actions`` is the safety statement. In NM-01 every boundary that
    could move money or send an order says False because none of them is
    implemented; a later phase flips its own boundary only together with the
    independent risk controls that phase is required to ship.
    """

    domain: str
    title: str
    responsibility: str
    owned_data: tuple[str, ...]
    interface: str
    phase: str
    executes_actions: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "title": self.title,
            "responsibility": self.responsibility,
            "owned_data": list(self.owned_data),
            "interface": self.interface,
            "phase": self.phase,
            "executes_actions": self.executes_actions,
        }


#: The twelve boundaries the mandate names, in implementation order.
DOMAIN_BOUNDARIES: tuple[DomainBoundary, ...] = (
    DomainBoundary(
        domain="market_data",
        title="Market data",
        responsibility=(
            "Fetch, timestamp and retain market prices with their provenance; "
            "never overwrite an observation."
        ),
        owned_data=("market_quotes", "historical_prices"),
        interface="MarketDataProvider / PriceHistory",
        phase="NM-02",
    ),
    DomainBoundary(
        domain="asset_discovery",
        title="Asset discovery and watchlists",
        responsibility=(
            "Propose assets to follow and maintain watchlists, with the evidence "
            "that caused an asset to appear."
        ),
        owned_data=("asset_metadata",),
        interface="AssetDiscoveryContract",
        phase="NM-05",
    ),
    DomainBoundary(
        domain="market_intelligence",
        title="Market intelligence and event detection",
        responsibility=(
            "Turn timestamped observations and sources into evidence-backed "
            "market events."
        ),
        owned_data=("market_events",),
        interface="MarketEventRadarContract",
        phase="NM-04",
    ),
    DomainBoundary(
        domain="forecasting",
        title="Forecasting",
        responsibility=(
            "Produce probabilistic forecasts with stated uncertainty and the "
            "measurements that support or refute them."
        ),
        owned_data=("forecasts",),
        interface="ForecastContract",
        phase="NM-06",
    ),
    DomainBoundary(
        domain="portfolio_management",
        title="Portfolio management",
        responsibility=(
            "Manage three distinguishable investment horizons, each with its own "
            "constraints and accounting."
        ),
        owned_data=("portfolios",),
        interface="PortfolioContract",
        phase="NM-07",
    ),
    DomainBoundary(
        domain="risk_management",
        title="Risk management",
        responsibility=(
            "Independently evaluate limits and veto an action; never be the "
            "component that proposes the action."
        ),
        owned_data=("risk_limits",),
        interface="RiskEngineContract",
        phase="NM-08",
    ),
    DomainBoundary(
        domain="strategy_management",
        title="Strategy management",
        responsibility=(
            "Define, version and select trading strategies without executing "
            "them."
        ),
        owned_data=("strategies",),
        interface="StrategyContract",
        phase="NM-09",
    ),
    DomainBoundary(
        domain="paper_execution",
        title="Paper execution",
        responsibility=(
            "Simulate fills and record paper orders; a live broker is outside "
            "this boundary in every phase that has not declared it."
        ),
        owned_data=("paper_orders", "execution_records"),
        interface="PaperExecutionContract",
        phase="NM-10",
        executes_actions=True,
    ),
    DomainBoundary(
        domain="backtesting",
        title="Backtesting",
        responsibility=(
            "Replay a strategy against retained history reproducibly and report "
            "its measured outcome."
        ),
        owned_data=("backtest_results",),
        interface="BacktestContract",
        phase="NM-11",
    ),
    DomainBoundary(
        domain="accounting_ledger",
        title="Accounting and ledger",
        responsibility=(
            "Record NovaCoin balances and entries deterministically; 1 NC is "
            "1 INR and every entry is reproducible."
        ),
        owned_data=("ledger_entries",),
        interface="LedgerContract",
        phase="NM-03",
    ),
    DomainBoundary(
        domain="adaptive_learning",
        title="Adaptive learning",
        responsibility=(
            "Improve strategy parameters under explicit, bounded and reversible "
            "control — never by loosening a risk limit."
        ),
        owned_data=("learning_state",),
        interface="AdaptiveLearningContract",
        phase="NM-13",
    ),
    DomainBoundary(
        domain="observability_audit",
        title="Observability and audit",
        responsibility=(
            "Publish lifecycle events, expose health and keep a durable financial "
            "audit trail that reuses the NovaControl audit layer."
        ),
        owned_data=("audit_records",),
        interface="MarketAuditContract",
        phase="NM-14",
    ),
)


@dataclass(frozen=True, slots=True)
class StorageBoundary:
    """Where one kind of NovaMarket data is owned and how it may change.

    ``append_only`` is the historical-price invariant made explicit. A store may
    be replaced in a later phase (NM-02's reliability work), but a table that is
    append-only here does not become mutable without changing this declaration
    and the tests that pin it.
    """

    data: str
    owner: str
    persistence: str
    append_only: bool
    phase: str

    def to_dict(self) -> dict[str, object]:
        return {
            "data": self.data,
            "owner": self.owner,
            "persistence": self.persistence,
            "append_only": self.append_only,
            "phase": self.phase,
        }


#: The storage strategy for every data family the mandate names. NovaMarket uses
#: NovaControl's existing JSON state store for small snapshots and adds no second
#: database engine; a future phase may move a family behind a repository
#: interface, but the ownership and mutability statements stay here.
STORAGE_BOUNDARIES: tuple[StorageBoundary, ...] = (
    StorageBoundary(
        "market_quotes", "market_data", "state-store namespace: novamarket_market_data", True, "NM-02"
    ),
    StorageBoundary(
        "historical_prices", "market_data", "append-only quote history (NM-02 repository)", True, "NM-02"
    ),
    StorageBoundary(
        "asset_metadata", "asset_discovery", "state-store namespace: novamarket_assets", False, "NM-05"
    ),
    StorageBoundary(
        "market_events", "market_intelligence", "append-only evidence records", True, "NM-04"
    ),
    StorageBoundary(
        "forecasts", "forecasting", "append-only forecast records with outcomes", True, "NM-06"
    ),
    StorageBoundary(
        "portfolios", "portfolio_management", "state-store namespace per horizon", False, "NM-07"
    ),
    StorageBoundary(
        "ledger_entries", "accounting_ledger", "append-only ledger; deterministic decimals", True, "NM-03"
    ),
    StorageBoundary(
        "risk_limits", "risk_management", "state-store namespace: novamarket_risk", False, "NM-08"
    ),
    StorageBoundary(
        "strategies", "strategy_management", "versioned strategy registry", False, "NM-09"
    ),
    StorageBoundary(
        "paper_orders", "paper_execution", "append-only paper order records", True, "NM-10"
    ),
    StorageBoundary(
        "execution_records", "paper_execution", "append-only execution records", True, "NM-10"
    ),
    StorageBoundary(
        "backtest_results", "backtesting", "reproducible run records with seeds", True, "NM-11"
    ),
    StorageBoundary(
        "learning_state", "adaptive_learning", "bounded, reversible parameter snapshots", False, "NM-13"
    ),
    StorageBoundary(
        "audit_records", "observability_audit", "novacontrol.audit (local-only, bounded)", True, "NM-14"
    ),
    StorageBoundary(
        "lifecycle_history", "observability_audit", "bounded in-memory + lifecycle events", True, "NM-01"
    ),
)


#: The seven capability domains of the Prediction Intelligence program. They
#: are capability boundaries inside NovaMarket, not independent execution
#: systems. None of them executes an action; NM-08 and NM-10 remain the sole
#: financial authorities.
PREDICTION_DOMAIN_BOUNDARIES: tuple[DomainBoundary, ...] = (
    DomainBoundary(
        domain="virtual_market",
        title="Virtual stock market and financial system",
        responsibility=(
            "Keep NovaCoin, portfolios, positions, valuation and accounting under "
            "the existing financial authorities; prediction never mutates them."
        ),
        owned_data=("prediction_market_context",),
        interface="Existing portfolio, wallet, ledger and market-data contracts",
        phase="PI-01",
    ),
    DomainBoundary(
        domain="market_data_intelligence",
        title="Market data intelligence",
        responsibility=(
            "Consume timestamped quotes, bars, fundamentals, news and events through "
            "the existing market-data authority, preserving provenance and freshness."
        ),
        owned_data=(
            "prediction_snapshots",
            "historical_dataset_versions",
            "historical_dataset_members",
            "dataset_gap_records",
            "corporate_action_records",
            "dataset_annotations",
            "feature_definitions",
            "feature_sets",
            "feature_regimes",
            "feature_preprocessing_fits",
            "feature_selection_reports",
        ),
        interface=(
            "MarketDataGateway and PredictionDataGateway over MarketDataEngine; "
            "HistoricalDatasetService over MarketDataStore"
        ),
        phase="PI-01",
    ),
    DomainBoundary(
        domain="prediction_intelligence",
        title="Prediction intelligence",
        responsibility=(
            "Assemble eligible model outputs into versioned, calibrated where "
            "possible forecasts, and abstain when evidence is insufficient."
        ),
        owned_data=(
            "prediction_requests",
            "prediction_forecasts",
            "training_dataset_versions",
            "training_dataset_samples",
        ),
        interface="PredictionForecastContract / EnsembleAssembler / DatasetTrainingContract",
        phase="PI-01",
    ),
    DomainBoundary(
        domain="forecast_lifecycle",
        title="Forecast lifecycle and confirmation",
        responsibility=(
            "Preserve T-20, T-5 and T-1 forecasts as immutable stages; never let a "
            "confirmation overwrite the initial prediction."
        ),
        owned_data=("prediction_lifecycles", "prediction_stages"),
        interface="PredictionLifecycleContract",
        phase="PI-01",
    ),
    DomainBoundary(
        domain="autonomous_trading_intelligence",
        title="Autonomous trading intelligence",
        responsibility=(
            "Provide read-only decision evidence to the existing decision pipeline; "
            "risk, permission and paper execution remain with NM-08, the autonomy "
            "controls and NM-10."
        ),
        owned_data=("prediction_evidence",),
        interface="DecisionForecastEvidence (authorizes_action=False)",
        phase="PI-01",
    ),
    DomainBoundary(
        domain="reinforcement_learning",
        title="Reinforcement learning and adaptive intelligence",
        responsibility=(
            "Define advisory experience, policy and evaluation boundaries; no "
            "automatic promotion, no risk-policy mutation and no order authority."
        ),
        owned_data=("prediction_learning_experience",),
        interface="LearningBoundaryContract (advisory only)",
        phase="PI-01",
    ),
    DomainBoundary(
        domain="performance_observability",
        title="Performance, evaluation and observability",
        responsibility=(
            "Evaluate forecasts against actual outcomes, expose errors and keep "
            "provider/data limitations visible without creating a second telemetry bus."
        ),
        owned_data=("prediction_evaluations", "prediction_models"),
        interface="PredictionEvaluationContract / existing NovaMarket events",
        phase="PI-01",
    ),
)

#: Prediction-owned persistence. The physical store is the existing NovaMarket
#: SQLite database; this declaration makes ownership and append-only behaviour
#: explicit without creating a second database engine.
PREDICTION_STORAGE_BOUNDARIES: tuple[StorageBoundary, ...] = (
    StorageBoundary("prediction_requests", "prediction_intelligence", "prediction SQLite tables; append-only", True, "PI-01"),
    StorageBoundary("prediction_forecasts", "prediction_intelligence", "prediction SQLite tables; immutable trigger", True, "PI-01"),
    StorageBoundary("prediction_lifecycles", "forecast_lifecycle", "prediction SQLite tables; status may advance", False, "PI-01"),
    StorageBoundary("prediction_stages", "forecast_lifecycle", "prediction SQLite tables; immutable trigger", True, "PI-01"),
    StorageBoundary("prediction_snapshots", "market_data_intelligence", "prediction SQLite tables; append-only", True, "PI-01"),
    StorageBoundary("prediction_evidence", "autonomous_trading_intelligence", "prediction SQLite tables; immutable trigger", True, "PI-01"),
    StorageBoundary("prediction_evaluations", "performance_observability", "prediction SQLite tables; immutable trigger", True, "PI-01"),
    StorageBoundary("prediction_models", "performance_observability", "prediction SQLite tables; versioned, state updatable", False, "PI-01"),
    StorageBoundary("prediction_learning_experience", "reinforcement_learning", "contract only in PI-01; no training store written", False, "PI-01"),
    StorageBoundary("prediction_market_context", "virtual_market", "references existing portfolio/valuation records; no copy", False, "PI-01"),
    StorageBoundary("historical_dataset_versions", "market_data_intelligence", "dataset SQLite tables; immutable version + exact bar references", True, "PI-02"),
    StorageBoundary("historical_dataset_members", "market_data_intelligence", "dataset SQLite tables; bar_id references, no price copy", True, "PI-02"),
    StorageBoundary("dataset_gap_records", "market_data_intelligence", "dataset SQLite tables; classified missing observations", True, "PI-02"),
    StorageBoundary("corporate_action_records", "market_data_intelligence", "dataset SQLite tables; sourced and verified actions only", True, "PI-02"),
    StorageBoundary("dataset_annotations", "market_data_intelligence", "dataset SQLite tables; caller-supplied halts/suspensions/closures", True, "PI-02"),
    StorageBoundary("training_dataset_versions", "prediction_intelligence", "dataset SQLite tables; immutable chronological splits", True, "PI-02"),
    StorageBoundary("training_dataset_samples", "prediction_intelligence", "dataset SQLite tables; decision/window/target bar references", True, "PI-02"),
    StorageBoundary("feature_definitions", "market_data_intelligence", "feature SQLite tables; immutable versioned definitions", True, "PI-04"),
    StorageBoundary("feature_sets", "market_data_intelligence", "feature SQLite tables; immutable point-in-time rows", True, "PI-04"),
    StorageBoundary("feature_regimes", "market_data_intelligence", "feature SQLite tables; immutable regime results tied to a feature set", True, "PI-04"),
    StorageBoundary("feature_preprocessing_fits", "market_data_intelligence", "feature SQLite tables; immutable fitted parameters and training digest", True, "PI-04"),
    StorageBoundary("feature_selection_reports", "market_data_intelligence", "feature SQLite tables; diagnostics only, never automatic deletion", True, "PI-04"),
)


def validate_prediction_architecture() -> tuple[str, ...]:
    """Structural checks for the PI-01 architecture and its authority boundaries."""
    problems: list[str] = []
    phases = set(PREDICTION_ROADMAP_PHASES)
    if len(PREDICTION_ROADMAP) != 16 or len(phases) != 16:
        problems.append("the prediction roadmap must contain exactly sixteen unique phases")
    if PREDICTION_ROADMAP[0][0] != "PI-01" or PREDICTION_ROADMAP[-1][0] != "PI-16":
        problems.append("the prediction roadmap must begin at PI-01 and end at PI-16")
    expected_domains = {
        "virtual_market",
        "market_data_intelligence",
        "prediction_intelligence",
        "forecast_lifecycle",
        "autonomous_trading_intelligence",
        "reinforcement_learning",
        "performance_observability",
    }
    found = {item.domain for item in PREDICTION_DOMAIN_BOUNDARIES}
    if found != expected_domains or len(PREDICTION_DOMAIN_BOUNDARIES) != 7:
        problems.append(
            "prediction architecture must declare exactly the seven capability domains"
        )
    owners = {item.domain for item in PREDICTION_DOMAIN_BOUNDARIES}
    data_owners: dict[str, str] = {}
    declared: set[str] = set()
    for boundary in PREDICTION_DOMAIN_BOUNDARIES:
        if boundary.phase not in phases:
            problems.append(
                f"prediction domain {boundary.domain!r} names unknown phase {boundary.phase!r}"
            )
        if boundary.executes_actions:
            problems.append(
                f"prediction domain {boundary.domain!r} must not execute actions"
            )
        if not boundary.interface.strip():
            problems.append(f"prediction domain {boundary.domain!r} has no interface")
        declared.update(boundary.owned_data)
    for storage in PREDICTION_STORAGE_BOUNDARIES:
        if storage.owner not in owners:
            problems.append(
                f"prediction data {storage.data!r} has unknown owner {storage.owner!r}"
            )
        if storage.phase not in phases:
            problems.append(
                f"prediction data {storage.data!r} names unknown phase {storage.phase!r}"
            )
        if storage.data in data_owners:
            problems.append(f"prediction data {storage.data!r} has more than one owner")
        data_owners[storage.data] = storage.owner
    for item in sorted(declared):
        if item not in data_owners:
            problems.append(f"prediction owned data {item!r} has no storage boundary")
    for item in sorted(data_owners):
        if item not in declared:
            problems.append(f"prediction storage {item!r} is not owned by a prediction domain")
    forbidden_data = {"paper_orders", "execution_records", "ledger_entries", "risk_limits"}
    overlap = forbidden_data & set(data_owners)
    if overlap:
        problems.append(
            "prediction may not own financial authority data: " + ", ".join(sorted(overlap))
        )
    if not any(
        item.domain == "autonomous_trading_intelligence"
        and "read-only" in item.responsibility.lower()
        for item in PREDICTION_DOMAIN_BOUNDARIES
    ):
        problems.append("prediction decision evidence must be declared read-only")
    return tuple(problems)


def validate_architecture() -> tuple[str, ...]:
    """Every architectural declaration's consistency problems, or ``()``.

    Called by the service at construction and by the architecture tests. The
    checks are structural statements, not opinions: a boundary owns data, an
    owner exists, a phase exists, three horizons are distinct, and nothing in
    this build claims to execute a real order.
    """
    problems: list[str] = []
    phases = set(ROADMAP_PHASES)
    if len(ROADMAP) != 14 or len(phases) != 14:
        problems.append("the roadmap must contain exactly fourteen unique phases")
    if ROADMAP[0][0] != "NM-01" or ROADMAP[-1][0] != "NM-14":
        problems.append("the roadmap must begin at NM-01 and end at NM-14")

    domains: set[str] = set()
    for boundary in DOMAIN_BOUNDARIES:
        if not boundary.domain.strip():
            problems.append("a domain boundary has no name")
        if boundary.domain in domains:
            problems.append(f"duplicate domain boundary: {boundary.domain}")
        domains.add(boundary.domain)
        if boundary.phase not in phases:
            problems.append(
                f"domain {boundary.domain!r} names unknown phase {boundary.phase!r}"
            )
        # Paper execution is the one current-phase domain allowed to "execute":
        # its actions are simulated order/fill records, never a live broker
        # action.  Live execution remains outside every implemented boundary.
        if (
            boundary.phase == IMPLEMENTED_PHASE
            and boundary.executes_actions
            and boundary.domain != "paper_execution"
        ):
            problems.append(
                f"domain {boundary.domain!r} executes actions in {IMPLEMENTED_PHASE}"
            )
        if not boundary.interface.strip():
            problems.append(f"domain {boundary.domain!r} declares no interface")

    data_owners: dict[str, str] = {}
    for storage in STORAGE_BOUNDARIES:
        if storage.owner not in domains:
            problems.append(
                f"data {storage.data!r} is owned by unknown domain {storage.owner!r}"
            )
        if storage.phase not in phases:
            problems.append(
                f"data {storage.data!r} names unknown phase {storage.phase!r}"
            )
        if storage.data in data_owners:
            problems.append(f"data {storage.data!r} has more than one owner")
        data_owners[storage.data] = storage.owner

    declared_data = {item for boundary in DOMAIN_BOUNDARIES for item in boundary.owned_data}
    for declared in sorted(declared_data):
        if declared not in data_owners:
            problems.append(f"owned data {declared!r} has no storage boundary")
    for stored in sorted(data_owners):
        if stored not in declared_data and stored != "lifecycle_history":
            problems.append(f"storage boundary {stored!r} is not owned by any domain")

    if not any(
        boundary.domain == "risk_management" and not boundary.executes_actions
        for boundary in DOMAIN_BOUNDARIES
    ):
        problems.append("the risk boundary must not execute the actions it evaluates")
    problems.extend(validate_prediction_architecture())

    return tuple(problems)


__all__ = [
    "DOMAIN_BOUNDARIES",
    "DomainBoundary",
    "PREDICTION_DOMAIN_BOUNDARIES",
    "PREDICTION_ROADMAP",
    "PREDICTION_ROADMAP_PHASES",
    "PREDICTION_ROADMAP_TITLES",
    "PREDICTION_STORAGE_BOUNDARIES",
    "ROADMAP",
    "ROADMAP_PHASES",
    "ROADMAP_TITLES",
    "STORAGE_BOUNDARIES",
    "StorageBoundary",
    "validate_architecture",
    "validate_prediction_architecture",
]
