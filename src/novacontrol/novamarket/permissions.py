"""NovaMarket's permission model: what this phase may do, and what it may not.

The boundary the mandate draws is between *reading*, *managing internal state*
and *executing a financial action*. NM-01 implements the first two at a low
level and none of the third:

==============  =====================================================  =========
operation       meaning                                                granted
==============  =====================================================  =========
``status.read`` read lifecycle, health and configuration                yes
``data.read``   read timestamped market information                     yes (no provider is configured)
``config.manage`` change and persist NovaMarket configuration           yes, confirmation at MEDIUM
``config.sensitive.read`` read broker/credential configuration          no — none is stored
``portfolio.simulate`` create or modify a simulated portfolio           no — NM-07
``trade.paper`` execute a simulated trade                               no — NM-10
``trade.live``  send a real-money order                                 no — never in NM-01
==============  =====================================================  =========

The denied operations keep their risk in this model (so the contract endpoint
can show *why* each is refused) but are deliberately NOT declared in the shared
:class:`~novacontrol.reliability.permissions.PermissionManager`. A declaration is
an operator telling the system how dangerous an action is; not declaring an
action that cannot be reached is the stronger statement, and it means
``manager.declared()`` never grows an entry a caller could mistake for a grant.
Nothing in the build registers a tool, route or capability for them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from novacontrol.core.security import PermissionScope, RiskLevel
from novacontrol.reliability.permissions import PermissionDeclaration

if TYPE_CHECKING:  # pragma: no cover - typing only
    from novacontrol.reliability.permissions import PermissionManager


class MarketOperation(StrEnum):
    """Every financial operation NovaMarket will ever expose, named once."""

    READ_STATUS = "market.status.read"
    READ_MARKET = "market.data.read"
    MANAGE_CONFIG = "market.config.manage"
    READ_WALLET = "market.wallet.read"
    MOVE_SIMULATED_FUNDS = "market.wallet.move"
    CORRECT_LEDGER = "market.ledger.correct"
    READ_SENSITIVE_CONFIG = "market.config.sensitive.read"
    MANAGE_SIMULATED_PORTFOLIO = "market.portfolio.simulate"
    EXECUTE_PAPER_TRADE = "market.trade.paper"
    READ_EXECUTION = "market.execution.read"
    RECONCILE_EXECUTION = "market.execution.reconcile"
    EXECUTE_LIVE_TRADE = "market.trade.live"
    READ_EVENTS = "market.event.read"
    REFRESH_EVENTS = "market.event.refresh"
    DISCOVER_ASSETS = "market.discovery.run"
    READ_WATCHLISTS = "market.watchlist.read"
    MANAGE_WATCHLISTS = "market.watchlist.manage"
    READ_FORECASTS = "market.forecast.read"
    RUN_FORECASTS = "market.forecast.run"
    TRAIN_FORECAST_MODELS = "market.forecast.train"
    READ_PORTFOLIOS = "market.portfolio.read"
    READ_RISK = "market.risk.read"
    READ_STRATEGY = "market.strategy.read"
    MANAGE_STRATEGY = "market.strategy.manage"
    RUN_STRATEGIES = "market.strategy.evaluate"
    ASSESS_RISK = "market.risk.assess"
    MANAGE_RISK_POLICY = "market.risk.policy"
    OVERRIDE_RISK = "market.risk.override"
    ACKNOWLEDGE_RISK_ALERTS = "market.risk.alerts"
    READ_BACKTESTS = "market.backtest.read"
    RUN_BACKTESTS = "market.backtest.run"
    COMPARE_BACKTESTS = "market.backtest.compare"
    READ_SHADOW = "market.shadow.read"
    MANAGE_SHADOW = "market.shadow.manage"
    RUN_SHADOW = "market.shadow.run"
    COMPARE_SHADOW = "market.shadow.compare"
    READ_LEARNING = "market.learning.read"
    MANAGE_LEARNING = "market.learning.manage"
    RUN_LEARNING = "market.learning.run"
    APPROVE_LEARNING = "market.learning.approve"
    READ_PREDICTIONS = "market.prediction.read"
    RUN_PREDICTIONS = "market.prediction.run"
    MANAGE_PREDICTION_MODELS = "market.prediction.models"
    READ_DECISION_EVIDENCE = "market.prediction.evidence"
    READ_DATASETS = "market.dataset.read"
    BUILD_DATASETS = "market.dataset.build"
    MANAGE_DATASETS = "market.dataset.manage"
    READ_FEATURES = "market.feature.read"
    GENERATE_FEATURES = "market.feature.generate"
    MANAGE_FEATURES = "market.feature.manage"


@dataclass(frozen=True, slots=True)
class MarketOperationPolicy:
    """One operation's risk, availability and the phase that will implement it."""

    operation: MarketOperation
    declaration: PermissionDeclaration
    granted: bool
    phase: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation.value,
            "risk": self.declaration.risk_level.value,
            "required_permission": (
                self.declaration.required_permission.value
                if self.declaration.required_permission
                else None
            ),
            "requires_confirmation": self.declaration.requires_confirmation,
            "reversible": self.declaration.reversible,
            "destructive": self.declaration.destructive,
            "external_side_effect": self.declaration.external_side_effect,
            "granted": self.granted,
            "phase": self.phase,
            "reason": self.reason,
        }


#: The model, one row per operation. ``granted`` is a statement about THIS
#: build, not about how dangerous the operation is.
_POLICIES: tuple[MarketOperationPolicy, ...] = (
    MarketOperationPolicy(
        operation=MarketOperation.READ_STATUS,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="NM-01",
        reason="reading lifecycle, health and non-sensitive configuration",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.READ_MARKET,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.LOW,
            required_permission=PermissionScope.NETWORK_ACCESS,
        ),
        granted=True,
        phase="NM-01",
        reason=(
            "market information is read-only; NM-01 implements no collection, and "
            "NM-02 will use this grant behind the provider contract"
        ),
    ),
    MarketOperationPolicy(
        operation=MarketOperation.MANAGE_CONFIG,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.MEDIUM,
            required_permission=PermissionScope.FILESYSTEM_WRITE,
            requires_confirmation=True,
            reversible=True,
        ),
        granted=True,
        phase="NM-01",
        reason="changing persisted NovaMarket configuration is confirmable and reversible",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.READ_WALLET,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="NM-03",
        reason=(
            "wallet balances, transactions and ledger entries are internal "
            "accounting records; reading them changes nothing"
        ),
    ),
    MarketOperationPolicy(
        operation=MarketOperation.MOVE_SIMULATED_FUNDS,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.MEDIUM,
            required_permission=PermissionScope.FILESYSTEM_WRITE,
            requires_confirmation=False,
            reversible=True,
        ),
        granted=True,
        phase="NM-03",
        reason=(
            "deposits, withdrawals and transfers move simulated NovaCoin only; "
            "every movement is append-only and reversible by a recorded reversal"
        ),
    ),
    MarketOperationPolicy(
        operation=MarketOperation.CORRECT_LEDGER,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.HIGH,
            required_permission=PermissionScope.FILESYSTEM_WRITE,
            requires_confirmation=True,
            reversible=False,
        ),
        granted=True,
        phase="NM-03",
        reason=(
            "a reversal posts new compensating entries against posted history; it "
            "is auditable and requires an explicit reason, and it cannot edit the "
            "original transaction"
        ),
    ),
    MarketOperationPolicy(
        operation=MarketOperation.READ_SENSITIVE_CONFIG,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.HIGH,
            required_permission=PermissionScope.SECRET_READ,
            requires_confirmation=True,
        ),
        granted=False,
        phase="NM-03",
        reason=(
            "no broker or credential configuration is stored in NM-01; secrets "
            "stay behind an operator-supplied reference"
        ),
    ),
    MarketOperationPolicy(
        operation=MarketOperation.MANAGE_SIMULATED_PORTFOLIO,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.MEDIUM,
            required_permission=PermissionScope.FILESYSTEM_WRITE,
            requires_confirmation=True,
            reversible=True,
        ),
        granted=True,
        phase="NM-07",
        reason=("managing simulated portfolios constructs book positions and moves NovaCoin through ledger transfers only; nothing is ordered or executed against a market"),
    ),
    MarketOperationPolicy(
        operation=MarketOperation.EXECUTE_PAPER_TRADE,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.HIGH,
            required_permission=PermissionScope.FILESYSTEM_WRITE,
            requires_confirmation=True,
            reversible=True,
        ),
        granted=True,
        phase="NM-10",
        reason=(
            "paper execution moves only simulated NovaCoin and is reachable only "
            "through an approved NM-09 proposal with a bound NM-08 decision"
        ),
    ),
    MarketOperationPolicy(
        operation=MarketOperation.READ_EXECUTION,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="NM-10",
        reason="reading paper orders, fills, reservations and execution status changes nothing",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.RECONCILE_EXECUTION,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="NM-10",
        reason="paper reconciliation only reads records and persists its findings; it never repairs",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.EXECUTE_LIVE_TRADE,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.CRITICAL,
            required_permission=PermissionScope.NETWORK_ACCESS,
            requires_confirmation=True,
            reversible=False,
            destructive=True,
            external_side_effect=True,
        ),
        granted=False,
        phase="none",
        reason="real-money trading is out of scope; NM-01 grants no live execution",
    ),
    # NM-04: evidence-backed market events. Reading stored events is LOW; a
    # refresh performs outbound fetches, so it is MEDIUM and declared to have an
    # external side effect. Neither can move money or place an order.
    MarketOperationPolicy(
        operation=MarketOperation.READ_EVENTS,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="NM-04",
        reason="reading stored market events, evidence and source health",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.REFRESH_EVENTS,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.MEDIUM,
            external_side_effect=True,
        ),
        granted=True,
        phase="NM-04",
        reason=(
            "fetching a configured event feed; reversible because it only reads "
            "a public source and stores what it published"
        ),
    ),
    # NM-05: discovery is local computation over stored instruments, quotes and
    # events. It reads no new external source, moves no money and places no order.
    MarketOperationPolicy(
        operation=MarketOperation.DISCOVER_ASSETS,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="NM-05",
        reason="scoring stored instruments against stored market data and events",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.READ_WATCHLISTS,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="NM-05",
        reason="reading candidates, watchlists and membership history",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.MANAGE_WATCHLISTS,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW, reversible=True),
        granted=True,
        phase="NM-05",
        reason=(
            "adding or removing a watchlist membership; it records attention, "
            "never an order or a capital allocation"
        ),
    ),
    # NM-06: probabilistic forecasting over stored data. Generation and training
    # are local computation; neither moves money nor places an order.
    MarketOperationPolicy(
        operation=MarketOperation.READ_FORECASTS,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="NM-06",
        reason="reading stored forecasts, outcomes, models, calibration and metrics",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.RUN_FORECASTS,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="NM-06",
        reason="generating and evaluating probabilistic forecasts from stored data",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.TRAIN_FORECAST_MODELS,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.MEDIUM, reversible=True
        ),
        granted=True,
        phase="NM-06",
        reason=(
            "walk-forward training performs bounded local computation and writes "
            "model metadata; it is reversible by retraining and cannot trade"
        ),
    ),


    MarketOperationPolicy(
        operation=MarketOperation.READ_PORTFOLIOS,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="NM-07",
        reason="reading portfolio records, holdings, valuations and decisions changes nothing",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.READ_RISK,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="NM-08",
        reason="reading risk decisions, metrics, alerts and policies changes nothing",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.ASSESS_RISK,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="NM-08",
        reason="an assessment reads existing state and records a decision; it cannot trade",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.MANAGE_RISK_POLICY,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.MEDIUM,
            requires_confirmation=True,
            reversible=True,
        ),
        granted=True,
        phase="NM-08",
        reason="a policy update is versioned, audited and reversible by a later version",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.OVERRIDE_RISK,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.HIGH,
            requires_confirmation=True,
            reversible=False,
        ),
        granted=True,
        phase="NM-08",
        reason="an override needs a named actor and reason, is time-bounded and audited",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.READ_STRATEGY,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="NM-09",
        reason="reading strategy definitions, signals, proposals and performance changes nothing",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.MANAGE_STRATEGY,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.MEDIUM,
            requires_confirmation=True,
            reversible=True,
        ),
        granted=True,
        phase="NM-09",
        reason="strategy configuration changes are versioned and reversible by a later version",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.RUN_STRATEGIES,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="NM-09",
        reason="evaluation generates signals and risk-assessed proposals; it cannot execute",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.ACKNOWLEDGE_RISK_ALERTS,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="NM-08",
        reason="acknowledging or resolving an alert records a lifecycle change only",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.READ_BACKTESTS,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="NM-11",
        reason="reading persisted historical simulations changes nothing",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.RUN_BACKTESTS,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.MEDIUM,
            required_permission=PermissionScope.FILESYSTEM_WRITE,
            requires_confirmation=False,
            reversible=True,
        ),
        granted=True,
        phase="NM-11",
        reason=(
            "a backtest writes only an isolated simulation record; it never moves "
            "a wallet, paper order or live order"
        ),
    ),
    MarketOperationPolicy(
        operation=MarketOperation.COMPARE_BACKTESTS,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="NM-11",
        reason="comparison reads completed simulations and persists no trading action",
    ),
    # NM-12: Shadow Mode observes and records; it never executes.
    MarketOperationPolicy(
        operation=MarketOperation.READ_SHADOW,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="NM-12",
        reason="reading shadow sessions, decisions, projections and outcomes changes nothing",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.MANAGE_SHADOW,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.MEDIUM,
            required_permission=PermissionScope.FILESYSTEM_WRITE,
            requires_confirmation=True,
            reversible=True,
        ),
        granted=True,
        phase="NM-12",
        reason="session lifecycle changes are versioned records in the shadow boundary only",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.RUN_SHADOW,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.MEDIUM,
            required_permission=PermissionScope.FILESYSTEM_WRITE,
            requires_confirmation=False,
            reversible=True,
        ),
        granted=True,
        phase="NM-12",
        reason=(
            "a shadow cycle writes hypothetical decisions and projections; it "
            "cannot create a paper order, move a wallet or reserve capital"
        ),
    ),
    MarketOperationPolicy(
        operation=MarketOperation.COMPARE_SHADOW,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="NM-12",
        reason="comparison reads shadow and reference evidence and persists no trading action",
    ),
    # NM-13: learning evaluates evidence and proposes configurations; only an
    # explicit approval may promote one, and no operation can trade.
    MarketOperationPolicy(
        operation=MarketOperation.READ_LEARNING,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="NM-13",
        reason="reading evidence, experiments, versions and alerts changes nothing",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.RUN_LEARNING,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.MEDIUM,
            required_permission=PermissionScope.FILESYSTEM_WRITE,
            requires_confirmation=False,
            reversible=True,
        ),
        granted=True,
        phase="NM-13",
        reason=(
            "a bounded experiment writes learning records and may run NM-11 "
            "replays; it cannot create an order or move a wallet"
        ),
    ),
    MarketOperationPolicy(
        operation=MarketOperation.MANAGE_LEARNING,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.MEDIUM,
            required_permission=PermissionScope.FILESYSTEM_WRITE,
            requires_confirmation=True,
            reversible=True,
        ),
        granted=True,
        phase="NM-13",
        reason="experiment and evidence lifecycle changes are versioned records only",
    ),
    MarketOperationPolicy(
        operation=MarketOperation.APPROVE_LEARNING,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.HIGH,
            required_permission=PermissionScope.FILESYSTEM_WRITE,
            requires_confirmation=True,
            reversible=True,
        ),
        granted=True,
        phase="NM-13",
        reason=(
            "approval, promotion and rollback change the active strategy record; "
            "each is explicit, versioned and reversible"
        ),
    ),
    # PI-01: prediction intelligence. These grants cover local forecasting and
    # immutable evidence only; they never authorize a financial action.
    MarketOperationPolicy(
        operation=MarketOperation.READ_PREDICTIONS,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="PI-01",
        reason=(
            "reading prediction forecasts, lifecycle stages and evaluations is "
            "read-only and never authorizes a trade"
        ),
    ),
    MarketOperationPolicy(
        operation=MarketOperation.RUN_PREDICTIONS,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.LOW,
            required_permission=PermissionScope.NETWORK_ACCESS,
            reversible=True,
        ),
        granted=True,
        phase="PI-01",
        reason=(
            "generating and confirming forecasts performs local computation over "
            "the existing market-data authority; it cannot order or settle"
        ),
    ),
    MarketOperationPolicy(
        operation=MarketOperation.MANAGE_PREDICTION_MODELS,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.MEDIUM,
            required_permission=PermissionScope.FILESYSTEM_WRITE,
            requires_confirmation=True,
            reversible=True,
        ),
        granted=True,
        phase="PI-01",
        reason=(
            "registering model metadata is versioned and reversible; it cannot "
            "promote a model automatically or modify NM-08 risk policy"
        ),
    ),
    MarketOperationPolicy(
        operation=MarketOperation.READ_DECISION_EVIDENCE,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="PI-01",
        reason=(
            "decision evidence is read-only and explicitly carries "
            "authorizes_action=false; NM-08 and NM-10 remain authoritative"
        ),
    ),
    # PI-02: historical datasets. Reading and building a dataset is local
    # evidence work; it never creates a second collector, order or fill.
    MarketOperationPolicy(
        operation=MarketOperation.READ_DATASETS,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="PI-02",
        reason=(
            "reading dataset versions, coverage, quality, missing data and "
            "training metadata is read-only"
        ),
    ),
    MarketOperationPolicy(
        operation=MarketOperation.BUILD_DATASETS,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.MEDIUM,
            required_permission=PermissionScope.NETWORK_ACCESS,
            reversible=True,
        ),
        granted=True,
        phase="PI-02",
        reason=(
            "a bounded historical acquisition uses the existing approved "
            "providers, their rate limits and the fair-rotation policy; it cannot "
            "order or settle"
        ),
    ),
    MarketOperationPolicy(
        operation=MarketOperation.MANAGE_DATASETS,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.MEDIUM,
            required_permission=PermissionScope.FILESYSTEM_WRITE,
            requires_confirmation=True,
            reversible=True,
        ),
        granted=True,
        phase="PI-02",
        reason=(
            "recording corporate actions and market annotations writes versioned "
            "metadata only; it never rewrites a stored observation"
        ),
    ),
    MarketOperationPolicy(
        operation=MarketOperation.READ_FEATURES,
        declaration=PermissionDeclaration(risk_level=RiskLevel.LOW),
        granted=True,
        phase="PI-04",
        reason=(
            "reading feature definitions, sets, regimes and quality evidence "
            "changes nothing and confers no execution authority"
        ),
    ),
    MarketOperationPolicy(
        operation=MarketOperation.GENERATE_FEATURES,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.MEDIUM,
            required_permission=PermissionScope.FILESYSTEM_WRITE,
            reversible=True,
        ),
        granted=True,
        phase="PI-04",
        reason=(
            "generation derives and persists versioned analytical features from "
            "already validated observations; it cannot acquire prices or place an order"
        ),
    ),
    MarketOperationPolicy(
        operation=MarketOperation.MANAGE_FEATURES,
        declaration=PermissionDeclaration(
            risk_level=RiskLevel.MEDIUM,
            required_permission=PermissionScope.FILESYSTEM_WRITE,
            requires_confirmation=True,
            reversible=True,
        ),
        granted=True,
        phase="PI-04",
        reason=(
            "recording preprocessing fits and selection diagnostics adds versioned "
            "metadata; it never deletes a feature or changes a stored feature row"
        ),
    ),
)

PERMISSION_POLICY: Mapping[MarketOperation, MarketOperationPolicy] = {
    policy.operation: policy for policy in _POLICIES
}


@dataclass(frozen=True, slots=True)
class MarketPermissionDecision:
    """The subsystem's own verdict, independent of the shared risk layer."""

    operation: MarketOperation
    allowed: bool
    risk: RiskLevel
    requires_confirmation: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation.value,
            "allowed": self.allowed,
            "risk": self.risk.value,
            "requires_confirmation": self.requires_confirmation,
            "reason": self.reason,
        }


class MarketPermissionModel:
    """Answers "may NovaMarket do this?" for every named operation."""

    def policy(self, operation: MarketOperation | str) -> MarketOperationPolicy:
        try:
            wanted = MarketOperation(operation)
        except ValueError as exc:
            known = ", ".join(item.value for item in MarketOperation)
            raise ValueError(
                f"unknown NovaMarket operation {operation!r}; known: {known}"
            ) from exc
        return PERMISSION_POLICY[wanted]

    def granted_operations(self) -> tuple[MarketOperation, ...]:
        return tuple(
            policy.operation for policy in _POLICIES if policy.granted
        )

    def authorize(self, operation: MarketOperation | str) -> MarketPermissionDecision:
        policy = self.policy(operation)
        return MarketPermissionDecision(
            operation=policy.operation,
            allowed=policy.granted,
            risk=policy.declaration.risk_level,
            requires_confirmation=policy.declaration.requires_confirmation,
            reason=policy.reason,
        )

    def matrix(self) -> tuple[dict[str, object], ...]:
        return tuple(
            policy.to_dict()
            for policy in sorted(_POLICIES, key=lambda row: list(MarketOperation).index(row.operation))
        )

    def declare(self, manager: PermissionManager | None) -> tuple[str, ...]:
        """Declare only the granted operations, returning what was declared."""
        if manager is None:
            return ()
        declared: list[str] = []
        for policy in _POLICIES:
            if not policy.granted:
                continue
            manager.declare(policy.operation.value, policy.declaration)
            declared.append(policy.operation.value)
        return tuple(declared)

    @staticmethod
    def undeclare(manager: PermissionManager | None, names: tuple[str, ...]) -> tuple[str, ...]:
        """Withdraw exactly the names this model declared."""
        if manager is None:
            return ()
        removed: list[str] = []
        for name in names:
            if manager.undeclare(name):
                removed.append(name)
        return tuple(removed)


__all__ = [
    "MarketOperation",
    "MarketOperationPolicy",
    "MarketPermissionDecision",
    "MarketPermissionModel",
    "PERMISSION_POLICY",
]
