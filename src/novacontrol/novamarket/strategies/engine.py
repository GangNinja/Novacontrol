"""NM-09 strategy engine: evaluate, signal, propose, and defer to NM-08.

The engine never executes and never approves its own proposal. Every actionable
signal becomes a proposed action only after the independent risk engine has
returned a decision, and a proposal that is not approved is not executable.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from novacontrol.novamarket.contracts import (
    StrategyNotFoundError,
    StrategyStateError,
    StrategyValidationError,
)
from novacontrol.novamarket.risk.metrics import sharpe as sharpe_metric
from novacontrol.novamarket.strategies.catalog import (
    EVALUATORS,
    STRATEGIES,
    SignalDecision,
    StrategyContext,
    validate_config,
)
from novacontrol.novamarket.strategies.models import (
    STRATEGY_CONTRACT_VERSION,
    STATUS_TRANSITIONS,
    ConflictOutcome,
    ConflictRecord,
    ConflictType,
    LifecycleRecord,
    PerformanceReport,
    ProposalStatus,
    ProposedAction,
    SignalType,
    StrategyCategory,
    StrategyDefinition,
    StrategyRecord,
    StrategySignal,
    StrategyStatus,
    strategy_id as make_id,
    utcnow,
)
from novacontrol.novamarket.strategies.store import StrategyStore

_LOGGER = logging.getLogger("novacontrol.novamarket.strategies")
Publisher = Callable[[str, dict[str, Any]], Awaitable[None]]

_ACTIONABLE = {SignalType.BUY, SignalType.SELL, SignalType.EXIT}
_SIDE_BY_SIGNAL = {
    SignalType.BUY: "buy",
    SignalType.SELL: "sell",
    SignalType.EXIT: "sell",
}
_ACTION_BY_SIGNAL = {
    SignalType.BUY: "add_position",
    SignalType.SELL: "reduce_position",
    SignalType.EXIT: "remove_position",
}
_STATUS_EVENTS = {
    StrategyStatus.ACTIVE: "novamarket.strategy.activated",
    StrategyStatus.PAUSED: "novamarket.strategy.paused",
    StrategyStatus.DEGRADED: "novamarket.strategy.degraded",
    StrategyStatus.SUSPENDED: "novamarket.strategy.suspended",
    StrategyStatus.RETIRED: "novamarket.strategy.retired",
}


class StrategyEngine:
    def __init__(
        self,
        store: StrategyStore,
        portfolios: Any,
        risk: Any,
        *,
        market_data: Any | None = None,
        forecasts: Any | None = None,
        radar: Any | None = None,
        discovery: Any | None = None,
        publisher: Publisher | None = None,
        forwarder: Callable[[str, str], Awaitable[Mapping[str, Any]]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._portfolios = portfolios
        self._risk = risk
        self._market_data = market_data
        self._forecasts = forecasts
        self._radar = radar
        self._discovery = discovery
        self._publisher = publisher
        self._forwarder = forwarder
        self._clock = clock or utcnow
        self._last_error = ""
        self._evaluations = 0

    @property
    def store(self) -> StrategyStore:
        return self._store

    @property
    def last_error(self) -> str:
        return self._last_error

    def _now(self) -> datetime:
        return self._clock()

    def set_market_data(self, market_data: Any | None) -> None:
        self._market_data = market_data

    def set_forecasts(self, forecasts: Any | None) -> None:
        self._forecasts = forecasts

    def set_radar(self, radar: Any | None) -> None:
        self._radar = radar

    def set_discovery(self, discovery: Any | None) -> None:
        self._discovery = discovery

    def set_risk(self, risk: Any) -> None:
        self._risk = risk

    def set_publisher(self, publisher: Publisher | None) -> None:
        self._publisher = publisher

    def set_forwarder(
        self, forwarder: Callable[[str, str], Awaitable[Mapping[str, Any]]] | None
    ) -> None:
        """Install the NM-10 handoff without making NM-09 import execution."""
        self._forwarder = forwarder

    async def _publish(self, name: str, **payload: Any) -> None:
        if self._publisher is None:
            return
        try:
            await self._publisher(name, payload)
        except Exception as exc:  # noqa: BLE001 - a committed record stands
            self._last_error = f"{name}: {type(exc).__name__}: {exc}"
            _LOGGER.warning("novamarket.strategy.event_not_published event=%s", name)

    # -- catalog and definitions ---------------------------------------------
    def catalog(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": "NM-09",
            "strategies": [item.to_dict() for item in STRATEGIES.values()],
            "categories": [item.value for item in StrategyCategory],
        }

    async def create(
        self,
        catalog_id: str,
        *,
        name: str = "",
        config: Mapping[str, Any] | None = None,
        horizons: Sequence[str] | None = None,
        asset_classes: Sequence[str] | None = None,
        priority: int = 50,
        actor: str = "",
    ) -> dict[str, Any]:
        definition = STRATEGIES.get(str(catalog_id))
        if definition is None:
            raise StrategyValidationError(f"unknown strategy catalog entry {catalog_id!r}")
        try:
            validated = validate_config(catalog_id, dict(config or {}))
        except ValueError as exc:
            raise StrategyValidationError(str(exc)) from exc
        chosen_horizons = tuple(
            str(item) for item in (definition.horizons if horizons is None else horizons)
        )
        if not chosen_horizons:
            raise StrategyValidationError("a strategy needs at least one horizon")
        unknown = set(chosen_horizons) - set(definition.horizons)
        if unknown:
            raise StrategyValidationError(
                f"horizon(s) not supported by {catalog_id}: {', '.join(sorted(unknown))}"
            )
        classes = tuple(
            str(item) for item in (definition.asset_classes if asset_classes is None else asset_classes)
        )
        if not classes:
            raise StrategyValidationError("a strategy needs at least one asset class")
        unknown_classes = set(classes) - set(definition.asset_classes)
        if unknown_classes:
            raise StrategyValidationError(
                f"asset class(es) not supported by {catalog_id}: "
                f"{', '.join(sorted(unknown_classes))}"
            )
        if isinstance(priority, bool):
            raise StrategyValidationError("priority must be an integer within 0..100")
        try:
            priority_value = int(priority)
        except (TypeError, ValueError) as exc:
            raise StrategyValidationError("priority must be an integer within 0..100") from exc
        if not 0 <= priority_value <= 100:
            raise StrategyValidationError("priority must be within 0..100")
        moment = self._now()
        label = (name or definition.name).strip() or definition.name
        sid = make_id("strat_", catalog_id, label, moment.isoformat(), uuid.uuid4().hex)
        version = f"nm09-{catalog_id}-1.0"
        record = StrategyRecord(
            strategy_id=sid,
            catalog_id=catalog_id,
            name=label,
            category=definition.category,
            status=StrategyStatus.DRAFT,
            version=version,
            config=validated,
            horizons=chosen_horizons,
            asset_classes=classes,
            priority=priority_value,
            created_at=moment,
            updated_at=moment,
            status_reason="created; activation is explicit",
        )
        self._store.save_strategy(record, actor=actor)
        self._store.save_lifecycle(
            LifecycleRecord(sid, moment, "", StrategyStatus.DRAFT.value, "created", actor)
        )
        await self._publish(
            "novamarket.strategy.created",
            strategy_id=sid,
            catalog_id=catalog_id,
            category=definition.category.value,
        )
        return {"ok": True, "strategy": record.to_dict()}

    def strategies(
        self, *, status: str = "", category: str = "", limit: int = 100
    ) -> dict[str, Any]:
        rows = self._store.strategies(status=status, category=category, limit=limit)
        return {"ok": True, "strategies": [row.to_dict() for row in rows], "total": len(rows)}

    def strategy(self, strategy_id: str) -> dict[str, Any]:
        return self._record(strategy_id).to_dict()

    def versions(self, strategy_id: str) -> dict[str, Any]:
        self._record(strategy_id)
        rows = self._store.versions(strategy_id)
        return {"ok": True, "strategy_id": strategy_id, "versions": rows, "total": len(rows)}

    def update_config(
        self,
        strategy_id: str,
        changes: Mapping[str, Any],
        *,
        actor: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        record = self._record(strategy_id)
        if record.status is StrategyStatus.RETIRED:
            raise StrategyStateError("a retired strategy cannot be reconfigured")
        merged = dict(record.config)
        merged.update(dict(changes or {}))
        try:
            validated = validate_config(record.catalog_id, merged)
        except ValueError as exc:
            raise StrategyValidationError(str(exc)) from exc
        if validated == record.config:
            return {"ok": True, "strategy": record.to_dict(), "unchanged": True}
        version = f"nm09-{record.catalog_id}-1.{len(self._store.versions(strategy_id))}"
        moment = self._now()
        updated = replace(
            record,
            config=validated,
            version=version,
            updated_at=moment,
        )
        self._store.save_strategy(updated, actor=actor)
        return {
            "ok": True,
            "strategy": updated.to_dict(),
            "reason": reason,
            "history": self._store.versions(strategy_id),
        }

    # -- lifecycle ------------------------------------------------------------
    async def activate(
        self, strategy_id: str, *, reason: str = "", actor: str = ""
    ) -> dict[str, Any]:
        return await self._transition(
            strategy_id, StrategyStatus.ACTIVE, reason=reason or "activated", actor=actor
        )

    async def pause(
        self, strategy_id: str, *, reason: str = "", actor: str = ""
    ) -> dict[str, Any]:
        return await self._transition(
            strategy_id, StrategyStatus.PAUSED, reason=reason or "paused", actor=actor
        )

    async def suspend(
        self, strategy_id: str, *, reason: str = "", actor: str = ""
    ) -> dict[str, Any]:
        return await self._transition(
            strategy_id, StrategyStatus.SUSPENDED, reason=reason or "suspended", actor=actor
        )

    async def degrade(
        self, strategy_id: str, *, reason: str = "", actor: str = ""
    ) -> dict[str, Any]:
        return await self._transition(
            strategy_id, StrategyStatus.DEGRADED, reason=reason or "performance degraded", actor=actor
        )

    async def retire(
        self, strategy_id: str, *, reason: str = "", actor: str = ""
    ) -> dict[str, Any]:
        return await self._transition(
            strategy_id, StrategyStatus.RETIRED, reason=reason or "retired", actor=actor
        )

    async def _transition(
        self, strategy_id: str, target: StrategyStatus, *, reason: str, actor: str
    ) -> dict[str, Any]:
        record = self._record(strategy_id)
        if target not in STATUS_TRANSITIONS[record.status]:
            raise StrategyStateError(
                f"cannot move {strategy_id} from {record.status.value} to {target.value}"
            )
        moment = self._now()
        updated = replace(record, status=target, updated_at=moment, status_reason=reason)
        self._store.save_strategy(updated, actor=actor)
        self._store.save_lifecycle(
            LifecycleRecord(
                strategy_id, moment, record.status.value, target.value, reason, actor
            )
        )
        await self._publish(
            _STATUS_EVENTS[target],
            strategy_id=strategy_id,
            reason=reason,
            actor=actor,
        )
        return {"ok": True, "strategy": updated.to_dict()}

    def lifecycle(self, strategy_id: str) -> dict[str, Any]:
        self._record(strategy_id)
        rows = self._store.lifecycle(strategy_id)
        return {"ok": True, "transitions": rows, "total": len(rows)}

    # -- evaluation -----------------------------------------------------------
    async def evaluate(
        self,
        strategy_id: str,
        portfolio_id: str,
        *,
        instrument_ids: Sequence[str] | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        record = self._record(strategy_id)
        if record.status is not StrategyStatus.ACTIVE:
            raise StrategyStateError(
                f"strategy {strategy_id} is {record.status.value}; only an active strategy evaluates"
            )
        detail = self._portfolios.detail(portfolio_id)
        horizon = str(detail["portfolio"]["horizon"])
        if horizon not in record.horizons:
            raise StrategyStateError(
                f"strategy {strategy_id} does not apply to the {horizon} portfolio"
            )
        ids = self._instruments(detail, instrument_ids)
        signals: list[StrategySignal] = []
        for instrument_id in ids:
            signal = self._evaluate_one(record, detail, horizon, instrument_id)
            if signal is None:
                continue
            signals.append(signal)
            await self._publish(
                "novamarket.strategy.signal.generated",
                signal_id=signal.signal_id,
                strategy_id=strategy_id,
                instrument_id=instrument_id,
                signal_type=signal.signal_type.value,
                confidence=str(signal.confidence),
            )
        moment = self._now()
        self._store.save_strategy(
            replace(record, last_evaluated_at=moment, updated_at=moment), actor=actor
        )
        self._evaluations += 1
        await self._publish(
            "novamarket.strategy.evaluated",
            strategy_id=strategy_id,
            portfolio_id=portfolio_id,
            signals=len(signals),
        )
        return {
            "ok": True,
            "strategy_id": strategy_id,
            "portfolio_id": portfolio_id,
            "signals": [item.to_dict() for item in signals],
            "total": len(signals),
        }

    def _evaluate_one(
        self,
        record: StrategyRecord,
        detail: Mapping[str, Any],
        horizon: str,
        instrument_id: str,
    ) -> StrategySignal | None:
        moment = self._now()
        market = self._market(instrument_id)
        observed = market.get("observed_at")
        cutoff = observed if isinstance(observed, datetime) else moment
        price = market.get("price")
        closes = list(market.get("closes") or ())
        max_age = float(record.config.get("max_data_age_hours", 24))
        stale = (
            not market.get("usable")
            or not isinstance(price, Decimal)
            or not isinstance(observed, datetime)
            or (moment - observed).total_seconds() / 3600.0 > max_age
        )
        if stale:
            reason = (
                "no usable timestamped price is available"
                if not market.get("usable")
                else f"market data is older than {max_age:g}h"
            )
            decision = SignalDecision(
                SignalType.NO_SIGNAL,
                Decimal("0"),
                ("stale_or_unavailable_market_data",),
                {"closes": len(closes)},
                "No signal: " + reason + ".",
            )
            data_quality = "stale" if market.get("usable") else "unavailable"
        else:
            holdings = {
                str(row.get("instrument_id")): row
                for row in detail.get("holdings", ()) or ()
            }
            held_row = holdings.get(instrument_id)
            held_weight = Decimal(str(held_row.get("weight") or "0")) if held_row else Decimal("0")
            context = StrategyContext(
                instrument_id=instrument_id,
                portfolio_id=str(detail["portfolio"]["portfolio_id"]),
                horizon=horizon,
                closes=tuple(closes),
                highs=tuple(float(value) for value in market.get("highs") or ()),
                lows=tuple(float(value) for value in market.get("lows") or ()),
                volumes=tuple(float(value) for value in market.get("volumes") or ()),
                price=price,  # type: ignore[arg-type]
                observed_at=observed,  # type: ignore[arg-type]
                now=moment,
                forecasts=tuple(self._forecast_rows(instrument_id)),
                events=tuple(self._events(instrument_id)),
                held=held_row is not None,
                held_weight=held_weight,
                cash=Decimal(str(detail.get("cash") or "0")),
                total_value=Decimal(str(detail["valuation"]["total_value"])),
            )
            try:
                decision = EVALUATORS[record.catalog_id](context, record.config)
            except Exception as exc:  # noqa: BLE001 - isolate one strategy
                self._last_error = f"{type(exc).__name__}: {exc}"
                decision = SignalDecision(
                    SignalType.NO_SIGNAL,
                    Decimal("0"),
                    ("strategy_evaluation_failed",),
                    {},
                    f"The strategy could not evaluate {instrument_id}: {type(exc).__name__}.",
                )
            data_quality = "measured"
        sid = make_id(
            "sig_",
            record.strategy_id,
            record.version,
            instrument_id,
            str(detail["portfolio"]["portfolio_id"]),
            cutoff.isoformat(),
            decision.signal_type.value,
        )
        existing = self._store.signal(sid)
        if existing is not None:
            return existing
        signal = StrategySignal(
            signal_id=sid,
            strategy_id=record.strategy_id,
            strategy_version=record.version,
            instrument_id=instrument_id,
            portfolio_id=str(detail["portfolio"]["portfolio_id"]),
            signal_type=decision.signal_type,
            created_at=moment,
            data_cutoff=cutoff,
            evaluation_horizon=horizon,
            indicators=dict(decision.indicators),
            forecast_refs=decision.forecast_refs,
            event_refs=decision.event_refs,
            conditions=decision.conditions,
            confidence=decision.confidence,
            explanation=decision.explanation,
            expires_at=moment
            + timedelta(minutes=int(record.config.get("signal_validity_minutes", 60))),
            reference_price=price if isinstance(price, Decimal) else None,
            data_quality=data_quality,
            horizon_days=decision.horizon_days,
        )
        self._store.save_signal(signal)
        return signal

    # -- proposals and risk ---------------------------------------------------
    async def propose(
        self,
        signal_id: str,
        *,
        notional: object | None = None,
        conflict_id: str = "",
        actor: str = "",
    ) -> dict[str, Any]:
        signal = self._signal(signal_id)
        record = self._record(signal.strategy_id)
        if record.status is not StrategyStatus.ACTIVE:
            raise StrategyStateError(
                f"strategy {record.strategy_id} is {record.status.value}; it cannot propose"
            )
        if signal.signal_type not in _ACTIONABLE:
            raise StrategyStateError(
                f"a {signal.signal_type.value} signal is not an actionable proposal"
            )
        if signal.strategy_version != record.version:
            raise StrategyStateError(
                f"signal {signal_id} was generated under {signal.strategy_version}; "
                f"strategy is now {record.version}; re-evaluate before proposing"
            )
        moment = self._now()
        if signal.expires_at <= moment:
            await self._publish(
                "novamarket.strategy.signal.expired", signal_id=signal_id
            )
            raise StrategyStateError(f"signal {signal_id} has expired")
        detail = self._portfolios.detail(signal.portfolio_id)
        holding = next(
            (
                row
                for row in detail.get("holdings", ()) or ()
                if str(row.get("instrument_id")) == signal.instrument_id
            ),
            None,
        )
        if signal.signal_type in {SignalType.SELL, SignalType.EXIT} and holding is None:
            raise StrategyStateError(
                f"{signal.signal_type.value} needs an existing holding of {signal.instrument_id}"
            )
        action = _ACTION_BY_SIGNAL[signal.signal_type]
        side = _SIDE_BY_SIGNAL[signal.signal_type]
        amount = self._notional(record, signal, detail, holding, notional)
        if amount <= 0:
            raise StrategyValidationError("a proposal needs a positive notional")
        if holding is not None:
            holding_value = Decimal(str(holding.get("market_value") or "0")).quantize(
                Decimal("0.01")
            )
            if signal.signal_type is SignalType.SELL and amount > holding_value:
                raise StrategyValidationError(
                    f"SELL notional {amount} exceeds the held value {holding_value}"
                )
            if signal.signal_type is SignalType.EXIT and amount != holding_value:
                raise StrategyValidationError(
                    f"EXIT must close the whole position ({holding_value}), not {amount}"
                )
        proposal_id = make_id("sprop_", signal_id, action, str(amount))
        existing = self._store.proposal(proposal_id)
        if existing is not None:
            return {"ok": True, "proposal": existing.to_dict(), "replayed": True}
        for prior in self._store.proposals(strategy_id=record.strategy_id, limit=500):
            if (
                prior.signal_id == signal_id
                and prior.status in {ProposalStatus.PENDING_RISK, ProposalStatus.APPROVED}
            ):
                return {"ok": True, "proposal": prior.to_dict(), "replayed": True}
        proposal = ProposedAction(
            proposal_id=proposal_id,
            signal_id=signal_id,
            strategy_id=record.strategy_id,
            strategy_version=signal.strategy_version,
            instrument_id=signal.instrument_id,
            portfolio_id=signal.portfolio_id,
            action=action,
            side=side,
            notional=amount,
            reference_price=signal.reference_price,
            price_at=signal.data_cutoff,
            created_at=moment,
            expires_at=moment + timedelta(hours=2),
            reason=signal.explanation,
            forecast_refs=signal.forecast_refs,
            event_refs=signal.event_refs,
            status=ProposalStatus.CREATED,
            conflict_id=conflict_id,
        )
        self._store.save_proposal(proposal)
        await self._publish(
            "novamarket.strategy.proposal.created",
            proposal_id=proposal_id,
            strategy_id=record.strategy_id,
            signal_id=signal_id,
            instrument_id=signal.instrument_id,
        )
        await self._publish(
            "novamarket.strategy.proposal.risk.requested",
            proposal_id=proposal_id,
            signal_id=signal_id,
        )
        risk_decision: dict[str, Any] | None = None
        risk_error = ""
        try:
            if self._risk is None:
                raise StrategyStateError("the NM-08 risk engine is unavailable")
            risk_decision = await self._risk.assess(
                action,
                portfolio_id=signal.portfolio_id,
                instrument_id=signal.instrument_id,
                side=side,
                notional=str(amount),
                actor=actor,
            )
            risk_outcome = str(risk_decision.get("outcome") or "")
        except Exception as exc:  # noqa: BLE001 - a failure is never approval
            risk_outcome = "error"
            risk_error = f"{type(exc).__name__}: {exc}"
            self._last_error = risk_error
        status = {
            "approved": ProposalStatus.APPROVED,
            "rejected": ProposalStatus.REJECTED,
            "restricted": ProposalStatus.RESTRICTED,
        }.get(risk_outcome, ProposalStatus.PENDING_RISK)
        updated = replace(
            proposal,
            status=status,
            risk_decision_id=str((risk_decision or {}).get("decision_id") or ""),
            risk_outcome=risk_outcome,
            risk_policy_version=str((risk_decision or {}).get("policy_version") or ""),
            risk_state_version=str((risk_decision or {}).get("state_version") or ""),
            risk_restrictions=tuple(
                str(item) for item in ((risk_decision or {}).get("restrictions") or ())
            ),
            risk_error=risk_error,
            updated_at=self._now(),
        )
        self._store.update_proposal(updated)
        event = {
            ProposalStatus.APPROVED: "novamarket.strategy.proposal.approved",
            ProposalStatus.REJECTED: "novamarket.strategy.proposal.rejected",
            ProposalStatus.RESTRICTED: "novamarket.strategy.proposal.restricted",
        }.get(status)
        if event:
            await self._publish(
                event,
                proposal_id=proposal_id,
                strategy_id=record.strategy_id,
                signal_id=signal_id,
                risk_decision_id=updated.risk_decision_id,
                outcome=risk_outcome,
            )
        return {"ok": True, "proposal": updated.to_dict()}

    def _notional(
        self,
        record: StrategyRecord,
        signal: StrategySignal,
        detail: Mapping[str, Any],
        holding: Mapping[str, Any] | None,
        requested: object | None,
    ) -> Decimal:
        if requested is not None:
            try:
                value = Decimal(str(requested))
            except Exception as exc:  # noqa: BLE001
                raise StrategyValidationError("notional must be a decimal number") from exc
            if value <= 0:
                raise StrategyValidationError("notional must be positive")
            return value.quantize(Decimal("0.01"))
        total = Decimal(str(detail["valuation"]["total_value"]))
        if signal.signal_type is SignalType.BUY:
            fraction = Decimal(str(record.config.get("notional_fraction", 0.10)))
            return (total * fraction).quantize(Decimal("0.01"))
        value = Decimal(str((holding or {}).get("market_value") or "0"))
        factor = Decimal("0.50") if signal.signal_type is SignalType.SELL else Decimal("1")
        return (value * factor).quantize(Decimal("0.01"))

    async def forward(self, proposal_id: str, *, actor: str = "") -> dict[str, Any]:
        """Hand an approved proposal to NM-10 paper execution exactly once."""
        proposal = self._store.proposal(proposal_id)
        if proposal is None:
            raise StrategyNotFoundError(f"unknown strategy proposal {proposal_id!r}")
        if proposal.status not in {
            ProposalStatus.APPROVED,
            ProposalStatus.FORWARDED_TO_EXECUTION,
        }:
            raise StrategyStateError(
                f"proposal is {proposal.status.value}; only an approved proposal can be forwarded"
            )
        if proposal.expires_at <= self._now():
            raise StrategyStateError("proposal approval has expired")
        if proposal.risk_outcome != "approved":
            raise StrategyStateError("proposal has no approved NM-08 decision")
        if self._forwarder is None:
            raise StrategyStateError(
                "NM-10 paper execution is not available; no execution handoff is configured"
            )
        result = await self._forwarder(proposal.proposal_id, str(actor or ""))
        if not isinstance(result, Mapping):
            raise StrategyStateError("NM-10 returned no execution result")
        order = result.get("order")
        if not result.get("ok") or not isinstance(order, Mapping):
            raise StrategyStateError(
                "NM-10 refused the proposal: "
                + str(result.get("error") or "no paper order was returned")
            )
        updated = replace(
            proposal,
            status=ProposalStatus.FORWARDED_TO_EXECUTION,
            execution_order_id=str(order.get("order_id") or ""),
            execution_status=str(order.get("status") or ""),
            updated_at=self._now(),
        )
        self._store.update_proposal(updated)
        await self._publish(
            "novamarket.strategy.proposal.forwarded",
            proposal_id=updated.proposal_id,
            strategy_id=updated.strategy_id,
            signal_id=updated.signal_id,
            order_id=updated.execution_order_id,
            order_status=updated.execution_status,
        )
        return {
            "ok": True,
            "proposal": updated.to_dict(),
            "execution": dict(result),
            "replayed": bool(result.get("replayed", False)),
        }

    # -- conflict resolution --------------------------------------------------
    async def run(
        self,
        portfolio_id: str,
        *,
        strategy_ids: Sequence[str] | None = None,
        instrument_ids: Sequence[str] | None = None,
        conflict_policy: str = "review",
        auto_propose: bool = True,
        actor: str = "",
    ) -> dict[str, Any]:
        chosen = [
            self._record(item)
            for item in (strategy_ids or [row.strategy_id for row in self._store.strategies(status="active", limit=500)])
        ]
        signals: list[StrategySignal] = []
        errors: list[dict[str, str]] = []
        for record in chosen:
            if record.status is not StrategyStatus.ACTIVE:
                continue
            try:
                body = await self.evaluate(
                    record.strategy_id,
                    portfolio_id,
                    instrument_ids=instrument_ids,
                    actor=actor,
                )
                signals.extend(
                    self._signal(item["signal_id"])
                    for item in body["signals"]
                    if item["signal_type"] in {value.value for value in _ACTIONABLE}
                    and item["data_quality"] == "measured"
                )
            except Exception as exc:  # noqa: BLE001 - one strategy cannot stop others
                errors.append({"strategy_id": record.strategy_id, "error": str(exc)})
        selected, conflicts = await self._resolve_conflicts(signals, conflict_policy)
        conflict_by_signal = {
            conflict.selected_signal_id: conflict.conflict_id
            for conflict in conflicts
            if conflict.selected_signal_id
        }
        proposals: list[dict[str, Any]] = []
        if auto_propose:
            for signal in selected:
                try:
                    proposals.append(
                        (
                            await self.propose(
                                signal.signal_id,
                                conflict_id=conflict_by_signal.get(signal.signal_id, ""),
                                actor=actor,
                            )
                        )["proposal"]
                    )
                except Exception as exc:  # noqa: BLE001 - surface, never approve
                    errors.append({"signal_id": signal.signal_id, "error": str(exc)})
        return {
            "ok": True,
            "portfolio_id": portfolio_id,
            "signals": [item.to_dict() for item in signals],
            "conflicts": [item.to_dict() for item in conflicts],
            "proposals": proposals,
            "errors": errors,
        }

    async def _resolve_conflicts(
        self, signals: list[StrategySignal], policy: str
    ) -> tuple[list[StrategySignal], list[ConflictRecord]]:
        if policy not in {"review", "priority", "block"}:
            raise StrategyValidationError("conflict_policy must be review, priority or block")
        groups: dict[tuple[str, str], list[StrategySignal]] = {}
        for signal in signals:
            groups.setdefault((signal.portfolio_id, signal.instrument_id), []).append(signal)
        selected: list[StrategySignal] = []
        records: list[ConflictRecord] = []
        moment = self._now()
        for (portfolio_id, instrument_id), group in groups.items():
            if len(group) == 1:
                selected.append(group[0])
                continue
            unique = {item.signal_id: item for item in group}
            group = list(unique.values())
            def priority(sid: str) -> int:
                try:
                    return self._record(sid).priority
                except StrategyNotFoundError:
                    return 50

            priorities = {item.strategy_id: priority(item.strategy_id) for item in group}
            ordered = sorted(group, key=lambda item: (-priorities[item.strategy_id], item.created_at))
            sides = {_SIDE_BY_SIGNAL[item.signal_type] for item in group}
            same_side = len(sides) == 1
            ctype = (
                ConflictType.DUPLICATE_ENTRY
                if same_side
                else (
                    ConflictType.HORIZON_MISMATCH
                    if len({item.horizon_days for item in group}) > 1
                    else ConflictType.OPPOSITE_SIDES
                )
            )
            outcome = ConflictOutcome.REVIEW_REQUIRED
            chosen: StrategySignal | None = None
            if policy == "block":
                outcome = ConflictOutcome.BLOCKED
                rationale = "explicit block policy rejected every conflicting proposal"
            elif same_side and all(item.signal_type is SignalType.BUY for item in group):
                chosen = ordered[0]
                outcome = ConflictOutcome.CONSOLIDATED
                rationale = (
                    "duplicate BUY proposals were consolidated onto the highest-priority "
                    "strategy; only one proposal proceeds to NM-08"
                )
            elif policy == "priority" and (same_side or priorities[ordered[0].strategy_id] > priorities[ordered[1].strategy_id]):
                chosen = ordered[0]
                outcome = ConflictOutcome.PRIORITY_SELECTED
                rationale = "explicit priority policy selected the highest-priority signal"
            else:
                rationale = (
                    "conflicting signals require review; no proposal is generated by this run"
                )
            conflict = ConflictRecord(
                conflict_id=make_id(
                    "sconf_",
                    portfolio_id,
                    instrument_id,
                    moment.isoformat(),
                    ",".join(sorted(item.signal_id for item in group)),
                ),
                portfolio_id=portfolio_id,
                instrument_id=instrument_id,
                conflict_type=ctype,
                strategy_ids=tuple(item.strategy_id for item in group),
                signal_ids=tuple(item.signal_id for item in group),
                outcome=outcome,
                rationale=rationale,
                created_at=moment,
                selected_signal_id=chosen.signal_id if chosen else "",
                selected_strategy_id=chosen.strategy_id if chosen else "",
            )
            self._store.save_conflict(conflict)
            records.append(conflict)
            await self._publish(
                "novamarket.strategy.conflict.detected",
                conflict_id=conflict.conflict_id,
                portfolio_id=portfolio_id,
                instrument_id=instrument_id,
                conflict_type=ctype.value,
            )
            await self._publish(
                "novamarket.strategy.conflict.resolved",
                conflict_id=conflict.conflict_id,
                outcome=outcome.value,
            )
            if chosen is not None:
                selected.append(chosen)
        return selected, records

    # -- reads and performance ------------------------------------------------
    def signals(
        self,
        *,
        strategy_id: str = "",
        portfolio_id: str = "",
        signal_type: str = "",
        limit: int = 200,
    ) -> dict[str, Any]:
        if strategy_id:
            self._record(strategy_id)
        rows = self._store.signals(
            strategy_id=strategy_id,
            portfolio_id=portfolio_id,
            signal_type=signal_type,
            limit=limit,
        )
        return {"ok": True, "signals": [row.to_dict() for row in rows], "total": len(rows)}

    def signal(self, signal_id: str) -> dict[str, Any]:
        return self._signal(signal_id).to_dict()

    def proposals(
        self,
        *,
        strategy_id: str = "",
        portfolio_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        rows = self._store.proposals(
            strategy_id=strategy_id,
            portfolio_id=portfolio_id,
            status=status,
            limit=limit,
        )
        return {"ok": True, "proposals": [row.to_dict() for row in rows], "total": len(rows)}

    def proposal(self, proposal_id: str) -> dict[str, Any]:
        row = self._store.proposal(proposal_id)
        if row is None:
            raise StrategyNotFoundError(f"unknown strategy proposal {proposal_id!r}")
        return row.to_dict()

    def conflicts(self, limit: int = 100) -> dict[str, Any]:
        rows = self._store.conflicts(limit)
        return {"ok": True, "conflicts": [row.to_dict() for row in rows], "total": len(rows)}

    async def performance(self, strategy_id: str) -> dict[str, Any]:
        record = self._record(strategy_id)
        returns: list[float] = []
        for signal in self._store.signals(strategy_id=strategy_id, limit=1000):
            if signal.signal_type not in _ACTIONABLE or signal.reference_price is None:
                continue
            if (self._now() - signal.created_at).days < signal.horizon_days:
                continue
            price = self._price_after(signal)
            if price is None or signal.reference_price <= 0:
                continue
            change = float((price / signal.reference_price) - Decimal("1"))
            returns.append(change if signal.signal_type is SignalType.BUY else -change)
        minimum = int(record.config.get("min_performance_observations", 5))
        wins = sum(1 for value in returns if value > 0)
        sufficient = len(returns) >= minimum
        if returns:
            win_rate = Decimal(str(wins / len(returns))).quantize(Decimal("0.0001"))
            average = Decimal(str(sum(returns) / len(returns))).quantize(Decimal("0.0001"))
            gains = sum(value for value in returns if value > 0)
            losses = abs(sum(value for value in returns if value < 0))
            profit_factor = (
                Decimal(str(gains / losses)).quantize(Decimal("0.0001"))
                if losses > 0
                else None
            )
            equity = 1.0
            peak = 1.0
            worst = 0.0
            for value in returns:
                equity *= 1.0 + value
                peak = max(peak, equity)
                worst = max(worst, (peak - equity) / peak if peak else 0.0)
            max_drawdown = Decimal(str(worst)).quantize(Decimal("0.0001"))
            sharpe = sharpe_metric(returns, minimum=minimum)
            sharpe_value = sharpe.value if sharpe.sufficient else None
            total = Decimal(str(equity - 1.0)).quantize(Decimal("0.0001"))
        else:
            win_rate = average = profit_factor = max_drawdown = total = None  # type: ignore[assignment]
            sharpe_value = None
        if not sufficient:
            degradation = "insufficient_evidence"
            reason = f"{len(returns)} outcome(s); at least {minimum} are required"
        else:
            reason = ""
            degradation = (
                "degraded"
                if (win_rate is not None and win_rate < Decimal("0.45"))
                or (average is not None and average < 0)
                or (max_drawdown is not None and max_drawdown > Decimal("0.15"))
                else "healthy"
            )
        report = PerformanceReport(
            strategy_id=strategy_id,
            computed_at=self._now(),
            observations=len(returns),
            wins=wins,
            win_rate=win_rate,
            average_return=average,
            profit_factor=profit_factor,
            max_drawdown=max_drawdown,
            sharpe=sharpe_value,
            total_return=total,
            sufficient=sufficient,
            reason=reason,
            degradation=degradation,
            basis="historical signal outcomes from subsequent market prices; not executed trades",
        )
        payload = {"ok": True, **report.to_dict()}
        await self._publish(
            "novamarket.strategy.performance.updated",
            strategy_id=strategy_id,
            observations=report.observations,
            degradation=report.degradation,
        )
        return payload

    # -- status ---------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "initialised": True,
            "phase": "NM-09",
            "schema_version": self._store.schema_version,
            "catalog": [item.to_dict() for item in STRATEGIES.values()],
            "metrics": self._store.metrics(),
            "evaluations": self._evaluations,
            "last_error": self._last_error,
            "executes_orders": False,
        }

    def contract(self) -> dict[str, Any]:
        return {
            "implemented": True,
            "phase": "NM-09",
            "version": STRATEGY_CONTRACT_VERSION,
            "categories": [item.value for item in StrategyCategory],
            "catalog": sorted(STRATEGIES),
            "signals": [item.value for item in SignalType],
            "statuses": [item.value for item in StrategyStatus],
            "proposal_states": [item.value for item in ProposalStatus],
            "guarantees": [
                "every actionable proposal is assessed by NM-08",
                "a rejected or insufficient proposal is never executable",
                "no trade or simulated order is executed in NM-09",
                "signals record the strategy version and data cutoff",
                "conflicts are recorded with an explicit resolution",
            ],
            "executes_orders": False,
            "real_money": False,
            "handoff": "approved proposals are forwarded to NM-10 paper execution",
        }

    # -- helpers --------------------------------------------------------------
    def _record(self, strategy_id: str) -> StrategyRecord:
        row = self._store.strategy(strategy_id)
        if row is None:
            raise StrategyNotFoundError(f"unknown strategy {strategy_id!r}")
        return row

    def _signal(self, signal_id: str) -> StrategySignal:
        row = self._store.signal(signal_id)
        if row is None:
            raise StrategyNotFoundError(f"unknown strategy signal {signal_id!r}")
        return row

    def _instruments(
        self, detail: Mapping[str, Any], requested: Sequence[str] | None
    ) -> list[str]:
        ids = [str(item) for item in (requested or ()) if str(item)]
        if not ids:
            ids = [
                str(row.get("instrument_id"))
                for row in detail.get("holdings", ()) or ()
                if row.get("instrument_id")
            ]
        if not ids and self._discovery is not None:
            try:
                body = self._discovery.candidates(limit=20)
                ids = [
                    str(row.get("instrument_id"))
                    for row in body.get("candidates", ()) or ()
                    if row.get("instrument_id")
                ]
            except Exception:  # noqa: BLE001
                ids = []
        return list(dict.fromkeys(ids))[:20]

    def _market(self, instrument_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "usable": False,
            "price": None,
            "observed_at": None,
            "closes": [],
            "highs": [],
            "lows": [],
            "volumes": [],
        }
        if self._market_data is None:
            return result
        try:
            latest = self._market_data.latest(instrument_id)
            selected = getattr(latest, "selected", None)
            if getattr(latest, "usable", False) and selected is not None:
                result["usable"] = True
                result["price"] = Decimal(str(getattr(selected, "price", "0")))
                result["observed_at"] = getattr(selected, "observed_at", None)
            # Take the newest window, then restore chronological order for the
            # evaluators. Ascending here would take the OLDEST 200 bars.
            page = self._market_data.bars(instrument_id, limit=200, ascending=False)
            bars = list(getattr(page, "items", ()) or ())
            # A provider flag is a request, not a guarantee. Order the window
            # by the bar timestamp so an oldest-first or unstable source still
            # gives the evaluators a chronological series ending at the newest
            # bar. The window itself was already selected by the query limit.
            if bars and all(
                isinstance(getattr(bar, "observed_at", None), datetime)
                for bar in bars
            ):
                def _epoch(bar: Any) -> float:
                    observed = getattr(bar, "observed_at", None)
                    if not isinstance(observed, datetime):
                        return float("-inf")
                    if observed.tzinfo is None:
                        observed = observed.replace(tzinfo=timezone.utc)
                    return float(observed.timestamp())

                bars.sort(key=_epoch)
            closes: list[float] = []
            highs: list[float] = []
            lows: list[float] = []
            volumes: list[float] = []
            for bar in bars:
                close = float(getattr(bar, "close", 0) or 0)
                if close <= 0:
                    continue
                closes.append(close)
                highs.append(float(getattr(bar, "high", close) or close))
                lows.append(float(getattr(bar, "low", close) or close))
                volumes.append(float(getattr(bar, "volume", 0) or 0))
            result["closes"] = closes
            result["highs"] = highs
            result["lows"] = lows
            result["volumes"] = volumes
        except Exception as exc:  # noqa: BLE001 - no signal on unreadable data
            self._last_error = f"{type(exc).__name__}: {exc}"
        return result

    def _forecast_rows(self, instrument_id: str) -> list[dict[str, Any]]:
        if self._forecasts is None:
            return []
        try:
            body = self._forecasts.forecasts(instrument_id=instrument_id, limit=50)
            rows = [dict(row) for row in body.get("forecasts", ()) or ()]
        except Exception:  # noqa: BLE001
            return []
        states: dict[tuple[str, str], str] = {}
        try:
            models = self._forecasts.models(instrument_id=instrument_id)
            for row in models.get("models", ()) or ():
                states[
                    (str(row.get("model_id")), str(row.get("horizon")))
                ] = str(row.get("state") or "")
        except Exception:  # noqa: BLE001
            states = {}
        for row in rows:
            row["model_state"] = states.get(
                (str(row.get("model_id") or ""), str(row.get("horizon") or "")), ""
            )
        return rows

    def _price_after(self, signal: StrategySignal) -> Decimal | None:
        """Return the first close at or after the signal's horizon, not today's."""
        if self._market_data is None:
            return None
        target = signal.created_at + timedelta(days=signal.horizon_days)
        try:
            page = self._market_data.bars(
                signal.instrument_id, start=target, limit=1, ascending=True
            )
            bars = list(getattr(page, "items", ()) or ())
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            return None
        for bar in bars:
            observed = getattr(bar, "observed_at", None)
            if not isinstance(observed, datetime):
                continue
            if observed.tzinfo is None and target.tzinfo is not None:
                observed = observed.replace(tzinfo=target.tzinfo)
            elif target.tzinfo is None and observed.tzinfo is not None:
                observed = observed.replace(tzinfo=None)
            if observed < target:
                continue
            close = Decimal(str(getattr(bar, "close", 0) or 0))
            return close if close > 0 else None
        return None

    def _events(self, instrument_id: str) -> list[dict[str, Any]]:
        if self._radar is None:
            return []
        try:
            body = self._radar.instrument_events(instrument_id, limit=20)
            return [dict(row) for row in body.get("events", ()) or ()]
        except Exception:  # noqa: BLE001
            return []


__all__ = ["StrategyEngine"]
