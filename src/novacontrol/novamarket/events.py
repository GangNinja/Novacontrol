"""The NovaMarket event contract and the one door events leave through.

NovaMarket publishes its lifecycle on the ONE application-wide
:class:`~novacontrol.core.events.EventBus`, using typed
:class:`~novacontrol.core.events.EventType` members so payload validation is the
bus's normal behaviour rather than a convention in this package. The implemented
events are:

``novamarket.initializing`` / ``initialized``
    validation started / completed (contract version, registered capabilities)
``novamarket.ready`` / ``enabled`` / ``disabled``
    the lifecycle reaching READY, RUNNING and DISABLED, respectively
``novamarket.capabilities_registered``
    what this build can do, announced separately from readiness
``novamarket.configuration_requested`` / ``configuration_changed``
    the bus control channel and the announcement that a change was applied
``novamarket.health_changed``
    a transition between ok/degraded/failing/skipped
``novamarket.warning`` / ``novamarket.error``
    an operational problem that did / did not stop initialisation
``novamarket.shutdown``
    an orderly stop (a disable is the operator action; shutdown is lifecycle)

Future market events (data availability, freshness, forecasts, risk breaches,
paper fills, backtests, shadow divergence) are declared in
:data:`FUTURE_EVENT_SPECS` with their payload contract and the phase that may
publish them. This build refuses to publish one: a declared-but-unimplemented
event is documentation of an extension point, and emitting it before the data
exists would fabricate a fact. The refusal is what keeps the extension path
honest while still making it checkable in tests.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from novacontrol.core.events import (
    EventBus,
    EventType,
    PublishResult,
    validate_payload,
)
from novacontrol.novamarket.architecture import ROADMAP_PHASES
from novacontrol.novamarket.contracts import IMPLEMENTED_PHASE, SUBSYSTEM_NAME


@dataclass(frozen=True, slots=True)
class MarketEventSpec:
    """A future event's name, required payload fields and owning phase."""

    name: str
    payload_fields: tuple[str, ...]
    phase: str
    description: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "payload_fields": list(self.payload_fields),
            "phase": self.phase,
            "description": self.description,
            "publishable_in": IMPLEMENTED_PHASE if self.phase == IMPLEMENTED_PHASE else self.phase,
        }


#: Events this build publishes. The order is the lifecycle's order, which is
#: also the order a reader should subscribe in.
IMPLEMENTED_EVENTS: tuple[EventType, ...] = (
    EventType.NOVAMARKET_INITIALIZING,
    EventType.NOVAMARKET_INITIALIZED,
    EventType.NOVAMARKET_READY,
    EventType.NOVAMARKET_ENABLED,
    EventType.NOVAMARKET_CAPABILITIES_REGISTERED,
    EventType.NOVAMARKET_CONFIGURATION_REQUESTED,
    EventType.NOVAMARKET_CONFIGURATION_CHANGED,
    EventType.NOVAMARKET_HEALTH_CHANGED,
    EventType.NOVAMARKET_WARNING,
    EventType.NOVAMARKET_ERROR,
    EventType.NOVAMARKET_RL_EPISODE_STARTED,
    EventType.NOVAMARKET_RL_STEP,
    EventType.NOVAMARKET_RL_EPISODE_COMPLETED,
    EventType.NOVAMARKET_RL_EPISODE_RESET,
    EventType.NOVAMARKET_RL_EPISODE_CANCELLED,
    EventType.NOVAMARKET_DISABLED,
    EventType.NOVAMARKET_SHUTDOWN,
    # NM-02: market-data reliability. Every payload names what was observed or
    # refused; none carries a price, a credential or a provider response body.
    EventType.NOVAMARKET_DATA_AVAILABLE,
    EventType.NOVAMARKET_DATA_UNAVAILABLE,
    EventType.NOVAMARKET_DATA_RECEIVED,
    EventType.NOVAMARKET_DATA_VALIDATED,
    EventType.NOVAMARKET_DATA_REJECTED,
    EventType.NOVAMARKET_DATA_STALE,
    EventType.NOVAMARKET_DATA_FRESHNESS_CHANGED,
    EventType.NOVAMARKET_DATA_PROVIDER_UNAVAILABLE,
    EventType.NOVAMARKET_DATA_PROVIDER_RECOVERED,
    EventType.NOVAMARKET_DATA_QUALITY_CHANGED,
    EventType.NOVAMARKET_DATA_INGESTION_FAILED,
    # NM-03: NovaCoin wallets and the deterministic ledger. Money events are
    # published after commit, so every payload describes a stored entry.
    EventType.NOVAMARKET_WALLET_CREATED,
    EventType.NOVAMARKET_WALLET_BALANCE_UPDATED,
    EventType.NOVAMARKET_WALLET_DEPOSIT_COMPLETED,
    EventType.NOVAMARKET_WALLET_WITHDRAWAL_COMPLETED,
    EventType.NOVAMARKET_WALLET_TRANSFER_COMPLETED,
    EventType.NOVAMARKET_WALLET_TRANSACTION_FAILED,
    EventType.NOVAMARKET_LEDGER_ENTRY_CREATED,
    EventType.NOVAMARKET_LEDGER_RECONCILIATION_COMPLETED,
    EventType.NOVAMARKET_LEDGER_INTEGRITY_VIOLATION,
    # NM-04: evidence-backed market events. Each name is published only
    # after the event version and its evidence are committed.
    EventType.NOVAMARKET_MARKET_EVENT_DISCOVERED,
    EventType.NOVAMARKET_MARKET_EVENT_EXTRACTED,
    EventType.NOVAMARKET_MARKET_EVENT_NORMALIZED,
    EventType.NOVAMARKET_MARKET_EVENT_VERIFIED,
    EventType.NOVAMARKET_MARKET_EVENT_CLASSIFIED,
    EventType.NOVAMARKET_MARKET_EVENT_UPDATED,
    EventType.NOVAMARKET_MARKET_EVENT_RETRACTED,
    EventType.NOVAMARKET_MARKET_EVENT_EXPIRED,
    EventType.NOVAMARKET_MARKET_EVENT_SOURCE_UNAVAILABLE,
    EventType.NOVAMARKET_MARKET_EVENT_EVIDENCE_CONFLICT,
    # NM-05: asset discovery and watchlists. Published after the
    # evaluation, membership or cycle row is committed.
    EventType.NOVAMARKET_ASSET_DISCOVERED,
    EventType.NOVAMARKET_ASSET_CANDIDATE_EVALUATED,
    EventType.NOVAMARKET_ASSET_CANDIDATE_RANKED,
    EventType.NOVAMARKET_WATCHLIST_CREATED,
    EventType.NOVAMARKET_WATCHLIST_RENAMED,
    EventType.NOVAMARKET_WATCHLIST_INSTRUMENT_ADDED,
    EventType.NOVAMARKET_WATCHLIST_INSTRUMENT_UPDATED,
    EventType.NOVAMARKET_WATCHLIST_INSTRUMENT_DOWNGRADED,
    EventType.NOVAMARKET_WATCHLIST_INSTRUMENT_REMOVED,
    EventType.NOVAMARKET_WATCHLIST_EVALUATION_COMPLETED,
    EventType.NOVAMARKET_ASSET_DISCOVERY_FAILED,
    # NM-06: probabilistic forecasting. Published after the forecast,
    # outcome, model state or calibration row is committed.
    EventType.NOVAMARKET_FORECAST_CREATED,
    EventType.NOVAMARKET_FORECAST_COMPLETED,
    EventType.NOVAMARKET_FORECAST_EVALUATION_PENDING,
    EventType.NOVAMARKET_FORECAST_EVALUATED,
    EventType.NOVAMARKET_FORECAST_MODEL_TRAINED,
    EventType.NOVAMARKET_FORECAST_MODEL_VALIDATED,
    EventType.NOVAMARKET_FORECAST_MODEL_ACTIVATED,
    EventType.NOVAMARKET_FORECAST_MODEL_DEGRADED,
    EventType.NOVAMARKET_FORECAST_MODEL_SUSPENDED,
    EventType.NOVAMARKET_FORECAST_CALIBRATION_COMPLETED,
    EventType.NOVAMARKET_FORECAST_PERFORMANCE_UPDATED,
    # NM-07: three-horizon portfolios, published after the book and the
    # ledger transfers behind it are committed.
    EventType.NOVAMARKET_PORTFOLIO_CREATED,
    EventType.NOVAMARKET_PORTFOLIO_ACTIVATED,
    EventType.NOVAMARKET_PORTFOLIO_ALLOCATION_UPDATED,
    EventType.NOVAMARKET_PORTFOLIO_POSITION_ADDED,
    EventType.NOVAMARKET_PORTFOLIO_POSITION_UPDATED,
    EventType.NOVAMARKET_PORTFOLIO_POSITION_REMOVED,
    EventType.NOVAMARKET_PORTFOLIO_VALUATION_UPDATED,
    EventType.NOVAMARKET_PORTFOLIO_REBALANCE_PROPOSED,
    EventType.NOVAMARKET_PORTFOLIO_REBALANCE_COMPLETED,
    EventType.NOVAMARKET_PORTFOLIO_PERFORMANCE_UPDATED,
    EventType.NOVAMARKET_PORTFOLIO_PAUSED,
    EventType.NOVAMARKET_PORTFOLIO_SUSPENDED,
    EventType.NOVAMARKET_PORTFOLIO_CLOSED,    EventType.NOVAMARKET_RISK_ASSESSMENT_REQUESTED,
    EventType.NOVAMARKET_RISK_ASSESSMENT_COMPLETED,
    EventType.NOVAMARKET_RISK_ACTION_APPROVED,
    EventType.NOVAMARKET_RISK_ACTION_REJECTED,
    EventType.NOVAMARKET_RISK_ACTION_RESTRICTED,
    EventType.NOVAMARKET_RISK_REVIEW_REQUIRED,
    EventType.NOVAMARKET_RISK_INSUFFICIENT_DATA,
    EventType.NOVAMARKET_RISK_LIMIT_BREACHED,
    EventType.NOVAMARKET_RISK_ALERT_CREATED,
    EventType.NOVAMARKET_RISK_ALERT_ACKNOWLEDGED,
    EventType.NOVAMARKET_RISK_ALERT_RESOLVED,
    EventType.NOVAMARKET_RISK_SCENARIO_COMPLETED,
    EventType.NOVAMARKET_RISK_POLICY_UPDATED,
    EventType.NOVAMARKET_RISK_OVERRIDE_RECORDED,
    EventType.NOVAMARKET_RISK_ACTION_APPLIED,
    EventType.NOVAMARKET_STRATEGY_CREATED,
    EventType.NOVAMARKET_STRATEGY_ACTIVATED,
    EventType.NOVAMARKET_STRATEGY_PAUSED,
    EventType.NOVAMARKET_STRATEGY_DEGRADED,
    EventType.NOVAMARKET_STRATEGY_SUSPENDED,
    EventType.NOVAMARKET_STRATEGY_RETIRED,
    EventType.NOVAMARKET_STRATEGY_EVALUATED,
    EventType.NOVAMARKET_STRATEGY_SIGNAL_GENERATED,
    EventType.NOVAMARKET_STRATEGY_SIGNAL_EXPIRED,
    EventType.NOVAMARKET_STRATEGY_PROPOSAL_CREATED,
    EventType.NOVAMARKET_STRATEGY_PROPOSAL_RISK_REQUESTED,
    EventType.NOVAMARKET_STRATEGY_PROPOSAL_APPROVED,
    EventType.NOVAMARKET_STRATEGY_PROPOSAL_REJECTED,
    EventType.NOVAMARKET_STRATEGY_PROPOSAL_RESTRICTED,
    EventType.NOVAMARKET_STRATEGY_PROPOSAL_FORWARDED,
    EventType.NOVAMARKET_STRATEGY_CONFLICT_DETECTED,
    EventType.NOVAMARKET_STRATEGY_CONFLICT_RESOLVED,
    EventType.NOVAMARKET_STRATEGY_PERFORMANCE_UPDATED,
    # NM-10: realistic paper execution, published after the order/fill/reconciliation
    # row is committed. These are simulated records, never live-market facts.
    EventType.NOVAMARKET_ORDER_CREATED,
    EventType.NOVAMARKET_ORDER_VALIDATED,
    EventType.NOVAMARKET_ORDER_OPENED,
    EventType.NOVAMARKET_ORDER_PARTIALLY_FILLED,
    EventType.NOVAMARKET_ORDER_FILLED,
    EventType.NOVAMARKET_ORDER_CANCEL_REQUESTED,
    EventType.NOVAMARKET_ORDER_CANCELLED,
    EventType.NOVAMARKET_ORDER_REJECTED,
    EventType.NOVAMARKET_ORDER_EXPIRED,
    EventType.NOVAMARKET_ORDER_FAILED,
    EventType.NOVAMARKET_EXECUTION_FILL_RECORDED,
    EventType.NOVAMARKET_EXECUTION_RECONCILED,
    EventType.NOVAMARKET_EXECUTION_RECONCILIATION_FAILED,
    # NM-11: committed backtest records and replay verification.
    EventType.NOVAMARKET_BACKTEST_CREATED,
    EventType.NOVAMARKET_BACKTEST_STARTED,
    EventType.NOVAMARKET_BACKTEST_PROGRESS_UPDATED,
    EventType.NOVAMARKET_BACKTEST_COMPLETED,
    EventType.NOVAMARKET_BACKTEST_FAILED,
    EventType.NOVAMARKET_BACKTEST_CANCELLED,
    EventType.NOVAMARKET_BACKTEST_CHECKPOINT_CREATED,
    EventType.NOVAMARKET_BACKTEST_PERFORMANCE_CALCULATED,
    EventType.NOVAMARKET_BACKTEST_COMPARISON_COMPLETED,
    EventType.NOVAMARKET_BACKTEST_DATA_QUALITY_WARNING,
    EventType.NOVAMARKET_BACKTEST_REPRODUCIBILITY_VERIFIED,
    # NM-12: non-executing Shadow Mode. Published after the session, decision,
    # projection, outcome or comparison row is committed.
    EventType.NOVAMARKET_SHADOW_SESSION_CREATED,
    EventType.NOVAMARKET_SHADOW_SESSION_STARTED,
    EventType.NOVAMARKET_SHADOW_SESSION_PAUSED,
    EventType.NOVAMARKET_SHADOW_SESSION_RESUMED,
    EventType.NOVAMARKET_SHADOW_SESSION_STOPPED,
    EventType.NOVAMARKET_SHADOW_SESSION_COMPLETED,
    EventType.NOVAMARKET_SHADOW_SESSION_CANCELLED,
    EventType.NOVAMARKET_SHADOW_SESSION_FAILED,
    EventType.NOVAMARKET_SHADOW_DECISION_RECORDED,
    EventType.NOVAMARKET_SHADOW_RISK_EVALUATED,
    EventType.NOVAMARKET_SHADOW_PROJECTION_CREATED,
    EventType.NOVAMARKET_SHADOW_OUTCOME_UPDATED,
    EventType.NOVAMARKET_SHADOW_CYCLE_COMPLETED,
    EventType.NOVAMARKET_SHADOW_DATA_QUALITY_FAILED,
    EventType.NOVAMARKET_SHADOW_RECOVERED,
    EventType.NOVAMARKET_SHADOW_COMPARISON_COMPLETED,
    EventType.NOVAMARKET_SHADOW_ANALYTICS_UPDATED,
    EventType.NOVAMARKET_SHADOW_DIVERGENCE_DETECTED,
    # NM-13: controlled adaptive learning. Every payload names persisted
    # evidence, an experiment or a version; none is a fill or a live return.
    EventType.NOVAMARKET_LEARNING_EVIDENCE_RECORDED,
    EventType.NOVAMARKET_LEARNING_EVIDENCE_REJECTED,
    EventType.NOVAMARKET_LEARNING_MONITOR_COMPLETED,
    EventType.NOVAMARKET_LEARNING_ALERT_RAISED,
    EventType.NOVAMARKET_LEARNING_ALERT_ACKNOWLEDGED,
    EventType.NOVAMARKET_LEARNING_EXPERIMENT_CREATED,
    EventType.NOVAMARKET_LEARNING_EXPERIMENT_STARTED,
    EventType.NOVAMARKET_LEARNING_EXPERIMENT_COMPLETED,
    EventType.NOVAMARKET_LEARNING_EXPERIMENT_FAILED,
    EventType.NOVAMARKET_LEARNING_EXPERIMENT_CANCELLED,
    EventType.NOVAMARKET_LEARNING_CANDIDATE_EVALUATED,
    EventType.NOVAMARKET_LEARNING_CANDIDATE_REJECTED,
    EventType.NOVAMARKET_LEARNING_CANDIDATE_APPROVED,
    EventType.NOVAMARKET_LEARNING_CANDIDATE_SHADOW_VALIDATED,
    EventType.NOVAMARKET_LEARNING_CANDIDATE_PROMOTED,
    EventType.NOVAMARKET_LEARNING_STRATEGY_ROLLED_BACK,
    # PI-01: prediction intelligence. Published only after the immutable
    # forecast, stage, evaluation or evidence row is committed. These carry
    # evidence identifiers, never an order or a fill.
    EventType.NOVAMARKET_PREDICTION_LIFECYCLE_CREATED,
    EventType.NOVAMARKET_PREDICTION_MODEL_REGISTERED,
    EventType.NOVAMARKET_PREDICTION_ABSTAINED,
    EventType.NOVAMARKET_PREDICTION_FORECAST_GENERATED,
    EventType.NOVAMARKET_PREDICTION_FORECAST_CONFIRMED,
    EventType.NOVAMARKET_PREDICTION_CONFIRMATION_SKIPPED,
    EventType.NOVAMARKET_PREDICTION_FORECAST_EVALUATED,
    EventType.NOVAMARKET_PREDICTION_DECISION_EVIDENCE_READY,
    # PI-02: historical dataset builds and versions. Evidence identifiers and
    # measured coverage only; no price payload and no order/fill.
    EventType.NOVAMARKET_DATASET_BUILD_STARTED,
    EventType.NOVAMARKET_DATASET_BUILD_COMPLETED,
    EventType.NOVAMARKET_DATASET_BUILD_FAILED,
    EventType.NOVAMARKET_DATASET_VERSION_CREATED,
    EventType.NOVAMARKET_DATASET_VALIDATION_INCOMPLETE,
    # PI-04: features, regimes and preprocessing. Identifiers, quality and
    # versions only; never a price vector or a trading action.
    EventType.NOVAMARKET_FEATURES_GENERATED,
    EventType.NOVAMARKET_FEATURES_FAILED,
    EventType.NOVAMARKET_REGIME_DETECTED,
    EventType.NOVAMARKET_REGIME_TRANSITIONED,
    EventType.NOVAMARKET_PREPROCESSING_FITTED,
    EventType.NOVAMARKET_FEATURE_SELECTION_COMPLETED,
    # PI-17/PI-18/PI-19 and PI-14..PI-16 health: one announcement per completed
    # operation so open dashboard views refetch without polling. Each payload
    # names the committed record; none carries an order or a mutation.
    EventType.NOVAMARKET_AGENT_CYCLE_COMPLETED,
    EventType.NOVAMARKET_OPTIMIZATION_RECOMMENDATION_CREATED,
    EventType.NOVAMARKET_RESEARCH_RUN_COMPLETED,
    EventType.NOVAMARKET_POLICY_DEGRADED,
)

IMPLEMENTED_EVENT_NAMES: frozenset[str] = frozenset(
    str(event_type) for event_type in IMPLEMENTED_EVENTS
)

#: Extension contract for the phases that add market data and the surfaces
#: built on it. Every name is namespaced and every phase is on the roadmap.
FUTURE_EVENT_SPECS: tuple[MarketEventSpec, ...] = (
    MarketEventSpec(
        "novamarket.portfolio.updated",
        ("portfolio_id", "horizon"),
        "NM-07",
        "A simulated portfolio changed for one horizon.",
    ),
    MarketEventSpec(
        "novamarket.risk.breach",
        ("portfolio_id", "limit"),
        "NM-08",
        "The independent risk engine refused or flagged an exposure.",
    ),
)

FUTURE_EVENTS: Mapping[str, MarketEventSpec] = {
    spec.name: spec for spec in FUTURE_EVENT_SPECS
}


def _validate_extension_contract() -> None:
    """Fail at import if the extension table itself is malformed."""
    names: set[str] = set()
    for spec in FUTURE_EVENT_SPECS:
        if spec.name in names:
            raise ValueError(f"duplicate future NovaMarket event: {spec.name}")
        names.add(spec.name)
        if not spec.name.startswith("novamarket."):
            raise ValueError(f"future event {spec.name!r} is not namespaced")
        if not spec.payload_fields:
            raise ValueError(f"future event {spec.name!r} declares no payload fields")
        if spec.phase not in ROADMAP_PHASES or spec.phase == IMPLEMENTED_PHASE:
            raise ValueError(
                f"future event {spec.name!r} names phase {spec.phase!r}, which is "
                "not a later roadmap phase"
            )
    overlap = names & IMPLEMENTED_EVENT_NAMES
    if overlap:
        raise ValueError(f"event(s) declared in both tables: {sorted(overlap)}")


_validate_extension_contract()


def validate_market_event(name: str, payload: Mapping[str, Any]) -> None:
    """Check a market event's required fields, implemented or future.

    Implemented events delegate to the bus's own validator, so there is exactly
    one payload contract for them. An unknown name is refused: this package does
    not publish arbitrary event types.
    """
    if name in IMPLEMENTED_EVENT_NAMES:
        validate_payload(name, payload)
        return
    spec = FUTURE_EVENTS.get(name)
    if spec is None:
        raise ValueError(
            f"unregistered NovaMarket event {name!r}; declare it in "
            "novacontrol.novamarket.events before publishing it"
        )
    missing = [field for field in spec.payload_fields if field not in payload]
    if missing:
        raise ValueError(
            f"NovaMarket event {name!r} is missing required payload field(s): "
            f"{', '.join(missing)}"
        )


class MarketEventPublisher:
    """Validates and publishes NovaMarket events; owns no second bus.

    ``emit_enabled=False`` turns publication off without removing the event
    contract: payloads are still validated, so a disabled emitter cannot hide a
    malformed event from the tests. With no bus attached (NovaMarket constructed
    before a runtime exists) publishing is a no-op that returns ``None``.
    """

    def __init__(
        self,
        event_bus: EventBus | None = None,
        *,
        emit_enabled: bool = True,
        source: str = SUBSYSTEM_NAME,
    ) -> None:
        self._event_bus = event_bus
        self.emit_enabled = bool(emit_enabled)
        self.source = source
        # NM-14: one observation seam for every subsystem publisher. Engines
        # receive this same publisher, so wallet/risk/execution events reach
        # activity and audit without each engine growing its own observer.
        self._observer: Callable[[str, str, Mapping[str, Any]], None] | None = None

    @property
    def attached(self) -> bool:
        return self._event_bus is not None

    @property
    def event_bus(self) -> EventBus | None:
        """The bus subscriptions go through — the SAME one publications use."""
        return self._event_bus

    def attach(self, event_bus: EventBus | None) -> None:
        self._event_bus = event_bus

    def detach(self) -> None:
        self._event_bus = None

    def set_observer(
        self, observer: Callable[[str, str, Mapping[str, Any]], None] | None
    ) -> None:
        """Install the one NM-14 activity/audit observer, or clear it."""
        self._observer = observer

    async def publish(
        self,
        event_type: EventType | str,
        /,
        *,
        correlation_id: str = "",
        causation_id: str | None = None,
        **payload: Any,
    ) -> PublishResult | None:
        """Validate, then publish when a bus is attached and emitting is on.

        ``correlation_id``/``causation_id`` are envelope arguments, not payload:
        a reply to a bus request keeps the request's correlation so a watcher can
        follow the whole exchange.
        """
        name = str(event_type)
        if "source" in payload:
            # ``source`` is the envelope's own field. Letting it through here
            # would raise a TypeError at ``emit`` and silently drop an event that
            # looked publishable; refuse it where the caller can see why.
            raise ValueError(
                "'source' belongs to the event envelope and may not be a payload "
                "field; name the provenance for what it is (e.g. 'selection_source')"
            )
        if name in FUTURE_EVENTS:
            spec = FUTURE_EVENTS[name]
            raise ValueError(
                f"{name} is declared for {spec.phase}; {IMPLEMENTED_PHASE} may "
                "declare it but cannot publish a fact it does not produce"
            )
        validate_market_event(name, payload)
        # NM-14 observation is independent of the bus switch: a committed wallet,
        # risk or execution operation must still be auditable when announcements
        # are disabled or no bus is attached.
        if self._observer is not None:
            try:
                self._observer(name, correlation_id, payload)
            except Exception:  # observation must never fail a committed operation
                pass
        if self._event_bus is None or not self.emit_enabled:
            return None
        return await self._event_bus.emit(
            name,
            source=self.source,
            correlation_id=correlation_id,
            causation_id=causation_id,
            **payload,
        )

    @staticmethod
    def contract() -> dict[str, object]:
        """The event contract as data, for ``/market/contracts``."""
        return {
            "implemented": [
                {
                    "name": str(event_type),
                    "payload_fields": list(
                        _implemented_fields(event_type)
                    ),
                }
                for event_type in IMPLEMENTED_EVENTS
            ],
            "extension": [spec.to_dict() for spec in FUTURE_EVENT_SPECS],
            "source": SUBSYSTEM_NAME,
            "schema_version": 1,
        }


def _implemented_fields(event_type: EventType) -> tuple[str, ...]:
    """The bus-declared required fields for an implemented event."""
    from novacontrol.core.events import EVENT_PAYLOAD_FIELDS

    return EVENT_PAYLOAD_FIELDS.get(event_type, ())


__all__ = [
    "FUTURE_EVENTS",
    "FUTURE_EVENT_SPECS",
    "IMPLEMENTED_EVENTS",
    "IMPLEMENTED_EVENT_NAMES",
    "MarketEventPublisher",
    "MarketEventSpec",
    "validate_market_event",
]
