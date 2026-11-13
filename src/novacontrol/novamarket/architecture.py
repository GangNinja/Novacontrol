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

    return tuple(problems)


__all__ = [
    "DOMAIN_BOUNDARIES",
    "DomainBoundary",
    "ROADMAP",
    "ROADMAP_PHASES",
    "ROADMAP_TITLES",
    "STORAGE_BOUNDARIES",
    "StorageBoundary",
    "validate_architecture",
]
