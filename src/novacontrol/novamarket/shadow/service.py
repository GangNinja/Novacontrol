"""NM-12 Shadow Mode service.

The pipeline is deliberately small and reuses the existing engines:

NM-02 observation -> NM-09 catalog evaluator -> NM-08 ``evaluate_policy`` ->
NM-10 pricing functions (pure) -> hypothetical projection -> later NM-02
observations -> outcome analytics.

There is no execution engine, wallet, ledger or portfolio mutation in this
module.  A shadow session cannot become an execution session.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Callable, Mapping, Sequence

from novacontrol.novamarket.execution.config import ExecutionConfig
from novacontrol.novamarket.execution.models import (
    MarketSnapshot,
    OrderSide,
    OrderType,
    money,
    quantity,
)
from novacontrol.novamarket.execution.pricing import (
    ExecutionPricingError,
    execution_price,
    fee_breakdown,
)
from novacontrol.novamarket.risk.models import POLICY_DEFAULTS, RiskOutcome, RiskPolicy
from novacontrol.novamarket.risk.policies import RiskSnapshot, evaluate_policy
from novacontrol.novamarket.shadow.guards import ShadowExecutionGuard, ShadowIsolationError
from novacontrol.novamarket.shadow.models import (
    SESSION_TRANSITIONS,
    TERMINAL_STATUSES,
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
    decimal,
    digest,
    money as money_text,
    optional_time,
    parse_time,
    quantity as quantity_text,
    shadow_id,
    text,
    utcnow,
    with_status,
)
from novacontrol.novamarket.shadow.store import ShadowStore
from novacontrol.novamarket.strategies.catalog import (
    EVALUATORS,
    SignalDecision,
    StrategyContext,
    validate_config,
)
from novacontrol.novamarket.strategies.models import SignalType

_ACTIONABLE = frozenset({SignalType.BUY, SignalType.SELL, SignalType.EXIT})
_EVENT_BY_STATUS = {
    ShadowSessionStatus.CREATED: "novamarket.shadow.session.created",
    ShadowSessionStatus.RUNNING: "novamarket.shadow.session.started",
    ShadowSessionStatus.PAUSED: "novamarket.shadow.session.paused",
    ShadowSessionStatus.STOPPED: "novamarket.shadow.session.stopped",
    ShadowSessionStatus.COMPLETED: "novamarket.shadow.session.completed",
    ShadowSessionStatus.CANCELLED: "novamarket.shadow.session.cancelled",
    ShadowSessionStatus.FAILED: "novamarket.shadow.session.failed",
}


class ShadowError(RuntimeError):
    """Base class for every NM-12 refusal."""


class ShadowValidationError(ShadowError, ValueError):
    """A session or observation payload is invalid."""


class ShadowNotFoundError(ShadowError):
    """An unknown session, decision or projection was requested."""


class ShadowStateError(ShadowError):
    """A lifecycle transition is not part of the documented session machine."""


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _decimal_or_none(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return decimal(value)
    except (TypeError, ValueError):
        return None


def _value(item: object) -> str:
    return str(getattr(item, "value", item) or "").lower()


class ShadowService:
    """Session lifecycle, live decision pipeline, outcomes and analytics."""

    def __init__(
        self,
        path: str,
        *,
        market_data: Any | None = None,
        strategy_loader: Callable[[str], Mapping[str, Any]] | None = None,
        portfolio_loader: Callable[[str], Mapping[str, Any]] | None = None,
        risk_policy_loader: Callable[[str], Mapping[str, Any]] | None = None,
        backtest_loader: Callable[[str], Mapping[str, Any]] | None = None,
        publisher: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        execution_config: ExecutionConfig | None = None,
        min_sample_size: int = 5,
        stale_after_seconds: int = 900,
        recover: bool = True,
    ) -> None:
        self._store = ShadowStore(path, clock=clock)
        self._market_data = market_data
        self._strategy_loader = strategy_loader
        self._portfolio_loader = portfolio_loader
        self._risk_policy_loader = risk_policy_loader
        self._backtest_loader = backtest_loader
        self._publisher = publisher
        self._clock = clock or utcnow
        self._execution_config = execution_config or ExecutionConfig()
        self._min_sample_size = max(1, int(min_sample_size))
        self._stale_after_seconds = max(1, int(stale_after_seconds))
        self._guard = ShadowExecutionGuard()
        self._monitor_tasks: dict[str, asyncio.Task[Any]] = {}
        self._last_error = ""
        if recover:
            self.recover()

    # -- introspection --------------------------------------------------------
    @property
    def execution_guard(self) -> ShadowExecutionGuard:
        return self._guard

    def status(self) -> dict[str, Any]:
        sessions = self._store.sessions(limit=500)
        counts = self._store.status_counts()
        decisions = sum(item.decision_count for item in sessions)
        return {
            "ok": True,
            "phase": "NM-12",
            "simulated": True,
            "real_money": False,
            "execution_connected": False,
            "nm13_activated": False,
            "sessions": len(sessions),
            "by_status": counts,
            "decisions": decisions,
            "last_error": self._last_error,
            "guarantees": [
                "no broker or exchange call",
                "no NM-10 paper order",
                "no wallet, ledger or portfolio mutation",
                "no capital reservation",
                "no automatic NM-13 learning",
            ],
        }

    def contract(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": "NM-12",
            "implemented": True,
            "simulated": True,
            "real_money": False,
            "execution_connected": False,
            "lifecycle": {
                "states": [item.value for item in ShadowSessionStatus],
                "transitions": {
                    source.value: sorted(target.value for target in targets)
                    for source, targets in SESSION_TRANSITIONS.items()
                },
            },
            "pipeline": [
                "NM-02 observation",
                "freshness and quality gate",
                "immutable portfolio snapshot",
                "NM-09 catalog evaluator",
                "NM-08 evaluate_policy",
                "NM-10 pure pricing projection",
                "later NM-02 observations",
                "hypothetical analytics",
            ],
            "projection": {
                "label": "hypothetical order projection",
                "execution_order_id": None,
                "execution_permitted": False,
            },
            "analytics": {
                "hypothetical": True,
                "small_sample_gate": self._min_sample_size,
            },
            "safety": {
                "execution_guard": type(self._guard).__name__,
                "nm13": "not implemented",
            },
            "limitations": [
                "daily bars and latest quotes only",
                "no order-book depth",
                "no live provider configured in the test harness",
                "outcomes are hypothetical, never investment returns",
            ],
        }

    # -- sessions -------------------------------------------------------------
    async def create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ShadowValidationError("a shadow session payload must be an object")
        now = self._clock()
        instruments = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in payload.get("instruments") or ()
                if str(item).strip()
            )
        )
        if not instruments:
            raise ShadowValidationError("a shadow session needs at least one instrument")
        strategies, inline = self._normalise_strategies(payload.get("strategies") or ())
        if not strategies:
            raise ShadowValidationError("a shadow session needs at least one strategy")
        portfolio_id = str(payload.get("portfolio_id") or "").strip()
        baseline_payload = payload.get("baseline_snapshot") or payload.get("baseline")
        if not portfolio_id and not isinstance(baseline_payload, Mapping):
            raise ShadowValidationError(
                "a session needs a portfolio_id or a baseline_snapshot"
            )
        start_at = optional_time(payload.get("start_at"))
        end_at = optional_time(payload.get("end_at"))
        if start_at and end_at and end_at <= start_at:
            raise ShadowValidationError("end_at must be after start_at")
        interval = int(
            payload.get("monitoring_interval_seconds")
            or payload.get("monitoring_interval")
            or 60
        )
        if interval < 1:
            raise ShadowValidationError("monitoring_interval_seconds must be positive")
        intervals = tuple(
            sorted(
                {
                    int(item)
                    for item in payload.get("observation_intervals") or (1, 5, 20)
                }
            )
        )
        if not intervals or any(item < 1 for item in intervals):
            raise ShadowValidationError("observation_intervals must be positive")
        horizons = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in payload.get("horizons") or ("short_term",)
                if str(item).strip()
            )
        )
        metadata = dict(payload.get("metadata") or {})
        if inline:
            metadata["inline_strategies"] = inline
        if isinstance(baseline_payload, Mapping):
            metadata["baseline_payload"] = dict(baseline_payload)
        metadata["auto_monitor"] = bool(payload.get("auto_monitor", True))
        metadata.setdefault("notional_fraction", "0.05")
        metadata.setdefault("outcome_horizon_days", 30)
        session_id = shadow_id(
            "shs_",
            payload.get("name") or "",
            instruments,
            strategies,
            now.isoformat(),
            time.time_ns(),
        )
        session = ShadowSession(
            session_id=session_id,
            name=str(payload.get("name") or f"Shadow session {session_id[-6:]}"),
            description=str(payload.get("description") or ""),
            status=ShadowSessionStatus.CREATED,
            portfolio_id=portfolio_id,
            instruments=instruments,
            strategies=strategies,
            horizons=horizons or ("short_term",),
            market_data_source=str(payload.get("market_data_source") or "nm02"),
            start_at=start_at,
            end_at=end_at,
            monitoring_interval_seconds=interval,
            risk_policy_version=str(payload.get("risk_policy_version") or ""),
            observation_intervals=intervals,
            metadata=metadata,
            baseline=None,
            created_at=now,
            updated_at=now,
        )
        self._store.save_session(session)
        await self._publish(
            _EVENT_BY_STATUS[ShadowSessionStatus.CREATED],
            session_id=session_id,
            instruments=len(instruments),
            strategies=len(strategies),
        )
        return {"ok": True, "session": session.to_dict()}

    def get(self, session_id: str) -> dict[str, Any]:
        return {"ok": True, "session": self._load(session_id).to_dict()}

    def list(self, *, limit: int = 50) -> dict[str, Any]:
        rows = self._store.sessions(limit=limit)
        return {
            "ok": True,
            "sessions": [item.to_dict() for item in rows],
            "total": len(rows),
        }

    def history(self, session_id: str) -> dict[str, Any]:
        session = self._load(session_id)
        decisions = self._store.decisions(session_id, limit=1000)
        projections = self._store.projections(session_id, limit=1000)
        outcomes = self._store.outcomes(session_id, limit=5000)
        return {
            "ok": True,
            "session": session.to_dict(),
            "counts": {
                "decisions": len(decisions),
                "projections": len(projections),
                "outcomes": len(outcomes),
                "errors": len(self._store.errors(session_id, limit=500)),
            },
            "timestamps": {
                "created_at": session.created_at.isoformat(),
                "started_at": session.started_at.isoformat() if session.started_at else None,
                "paused_at": session.paused_at.isoformat() if session.paused_at else None,
                "stopped_at": session.stopped_at.isoformat() if session.stopped_at else None,
                "completed_at": session.completed_at.isoformat() if session.completed_at else None,
                "cancelled_at": session.cancelled_at.isoformat() if session.cancelled_at else None,
            },
        }

    async def start(self, session_id: str) -> dict[str, Any]:
        session = self._load(session_id)
        self._require_source(
            session,
            {ShadowSessionStatus.CREATED, ShadowSessionStatus.PAUSED},
            "start",
        )
        now = self._clock()
        if session.baseline is None:
            try:
                baseline = self._capture_baseline(session, now)
            except Exception as exc:
                failed = with_status(
                    session,
                    ShadowSessionStatus.FAILED,
                    at=now,
                    error=f"baseline capture failed: {type(exc).__name__}: {exc}",
                )
                self._store.save_session(failed)
                self._record_error(
                    session,
                    ShadowErrorKind.VALIDATION,
                    failed.error,
                    at=now,
                )
                await self._publish(
                    _EVENT_BY_STATUS[ShadowSessionStatus.FAILED],
                    session_id=session_id,
                    error=failed.error,
                )
                raise
            session = replace(session, baseline=baseline)
        session = self._freeze_selection(session)
        updated = with_status(session, ShadowSessionStatus.RUNNING, at=now)
        self._store.save_session(updated)
        await self._publish(
            _EVENT_BY_STATUS[ShadowSessionStatus.RUNNING],
            session_id=session_id,
            baseline_snapshot_id=updated.baseline.snapshot_id if updated.baseline else "",
            portfolio_id=updated.portfolio_id,
        )
        if bool(updated.metadata.get("auto_monitor")):
            self._ensure_monitor(updated)
        return {"ok": True, "session": updated.to_dict()}

    async def pause(self, session_id: str) -> dict[str, Any]:
        session = self._load(session_id)
        self._require_source(
            session, {ShadowSessionStatus.RUNNING}, "pause"
        )
        self._cancel_monitor(session_id)
        updated = with_status(session, ShadowSessionStatus.PAUSED, at=self._clock())
        self._store.save_session(updated)
        await self._publish(
            _EVENT_BY_STATUS[ShadowSessionStatus.PAUSED], session_id=session_id
        )
        return {"ok": True, "session": updated.to_dict()}

    async def resume(self, session_id: str) -> dict[str, Any]:
        session = self._load(session_id)
        self._require_source(
            session, {ShadowSessionStatus.PAUSED}, "resume"
        )
        updated = with_status(session, ShadowSessionStatus.RUNNING, at=self._clock())
        self._store.save_session(updated)
        await self._publish(
            "novamarket.shadow.session.resumed",
            session_id=session_id,
        )
        if bool(updated.metadata.get("auto_monitor")):
            self._ensure_monitor(updated)
        return {"ok": True, "session": updated.to_dict()}

    async def stop(self, session_id: str) -> dict[str, Any]:
        session = self._load(session_id)
        self._require_source(
            session,
            {ShadowSessionStatus.RUNNING, ShadowSessionStatus.PAUSED},
            "stop",
        )
        self._cancel_monitor(session_id)
        updated = with_status(session, ShadowSessionStatus.STOPPED, at=self._clock())
        self._store.save_session(updated)
        await self._publish(
            _EVENT_BY_STATUS[ShadowSessionStatus.STOPPED], session_id=session_id
        )
        return {"ok": True, "session": updated.to_dict()}

    async def complete(self, session_id: str) -> dict[str, Any]:
        session = self._load(session_id)
        self._require_source(
            session,
            {
                ShadowSessionStatus.RUNNING,
                ShadowSessionStatus.PAUSED,
                ShadowSessionStatus.STOPPED,
            },
            "complete",
        )
        self._cancel_monitor(session_id)
        updated = with_status(session, ShadowSessionStatus.COMPLETED, at=self._clock())
        self._store.save_session(updated)
        await self._publish(
            _EVENT_BY_STATUS[ShadowSessionStatus.COMPLETED],
            session_id=session_id,
            decisions=updated.decision_count,
        )
        return {"ok": True, "session": updated.to_dict()}

    async def cancel(self, session_id: str) -> dict[str, Any]:
        session = self._load(session_id)
        self._require_source(
            session,
            {
                ShadowSessionStatus.CREATED,
                ShadowSessionStatus.RUNNING,
                ShadowSessionStatus.PAUSED,
                ShadowSessionStatus.STOPPED,
                ShadowSessionStatus.FAILED,
            },
            "cancel",
        )
        self._cancel_monitor(session_id)
        updated = with_status(session, ShadowSessionStatus.CANCELLED, at=self._clock())
        self._store.save_session(updated)
        await self._publish(
            _EVENT_BY_STATUS[ShadowSessionStatus.CANCELLED], session_id=session_id
        )
        return {"ok": True, "session": updated.to_dict()}

    def _require_source(
        self,
        session: ShadowSession,
        sources: set[ShadowSessionStatus],
        action: str,
    ) -> None:
        if session.status not in sources:
            allowed = ", ".join(sorted(item.value for item in sources))
            raise ShadowStateError(
                f"shadow session {session.session_id} is {session.status.value}; "
                f"it cannot {action} (allowed from: {allowed})"
            )

    def recover(self) -> dict[str, Any]:
        """Recover persisted sessions after a restart.

        A session that was RUNNING is paused rather than silently resumed: it
        would otherwise start observing without its operator knowing.  Paused and
        created sessions are preserved unchanged.
        """
        recovered = 0
        paused = 0
        for session in self._store.sessions(limit=500):
            if session.status is ShadowSessionStatus.RUNNING:
                updated = with_status(
                    session,
                    ShadowSessionStatus.PAUSED,
                    at=self._clock(),
                    error="application restarted; resume after checking market data",
                )
                self._store.save_session(updated)
                paused += 1
            recovered += 1
        return {"ok": True, "sessions": recovered, "paused": paused}

    # -- pipeline -------------------------------------------------------------
    async def run_cycle(
        self,
        session_id: str,
        *,
        now: datetime | None = None,
        observations: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        session = self._load(session_id)
        if session.status is not ShadowSessionStatus.RUNNING:
            raise ShadowStateError(
                f"shadow session {session_id} is {session.status.value}; "
                "only a running session observes data"
            )
        moment = parse_time(now) if now is not None else self._clock()
        if session.end_at is not None and moment >= session.end_at:
            completed = await self.complete(session_id)
            return {"ok": True, "completed": True, "session": completed["session"]}
        if session.start_at is not None and moment < session.start_at:
            return {"ok": True, "skipped": "before_start", "decisions": 0}
        baseline = session.baseline
        if baseline is None:
            raise ShadowStateError("the session has no baseline snapshot; start it first")
        started = time.monotonic()
        counts = {
            "decisions": 0,
            "duplicates": 0,
            "data_failures": 0,
            "strategy_failures": 0,
            "risk_blocked": 0,
            "projections": 0,
        }
        for instrument_id in session.instruments:
            try:
                observation = self._observe(
                    instrument_id,
                    moment,
                    observations=observations,
                    source=session.market_data_source,
                )
            except Exception as exc:  # noqa: BLE001 - a provider outage is a data failure
                counts["data_failures"] += 1
                self._record_error(
                    session,
                    ShadowErrorKind.DATA_UNAVAILABLE,
                    f"{type(exc).__name__}: {exc}",
                    at=moment,
                    instrument_id=instrument_id,
                )
                continue
            if observation.get("error"):
                counts["data_failures"] += 1
                self._record_error(
                    session,
                    ShadowErrorKind.DATA_STALE
                    if observation.get("stale")
                    else ShadowErrorKind.DATA_UNAVAILABLE,
                    str(observation["error"]),
                    at=moment,
                    instrument_id=instrument_id,
                )
                continue
            for strategy_id in session.strategies:
                try:
                    record = self._strategy(strategy_id, session)
                except Exception as exc:  # noqa: BLE001 - one strategy never stops a cycle
                    counts["strategy_failures"] += 1
                    self._record_error(
                        session,
                        ShadowErrorKind.STRATEGY_FAILED,
                        f"{strategy_id}: {type(exc).__name__}: {exc}",
                        at=moment,
                        instrument_id=instrument_id,
                        strategy_id=strategy_id,
                    )
                    continue
                try:
                    decision, duplicate = await self._decide(
                        session,
                        baseline,
                        record,
                        instrument_id,
                        observation,
                        moment,
                    )
                except Exception as exc:  # noqa: BLE001 - a decision failure is recorded
                    counts["strategy_failures"] += 1
                    self._record_error(
                        session,
                        ShadowErrorKind.STRATEGY_FAILED,
                        f"{strategy_id}/{instrument_id}: {type(exc).__name__}: {exc}",
                        at=moment,
                        instrument_id=instrument_id,
                        strategy_id=strategy_id,
                    )
                    continue
                if duplicate:
                    counts["duplicates"] += 1
                    continue
                counts["decisions"] += 1
                if decision.status is ShadowDecisionStatus.RISK_REJECTED:
                    counts["risk_blocked"] += 1
                if decision.projection_id:
                    counts["projections"] += 1
        elapsed_ms = (time.monotonic() - started) * 1000.0
        updated = replace(
            session,
            cycles=session.cycles + 1,
            last_cycle_at=moment,
            updated_at=moment,
            decision_count=session.decision_count + counts["decisions"],
        )
        self._store.save_session(updated)
        await self._publish(
            "novamarket.shadow.cycle.completed",
            session_id=session_id,
            decisions=counts["decisions"],
            duplicates=counts["duplicates"],
            data_failures=counts["data_failures"],
            latency_ms=round(elapsed_ms, 3),
        )
        await self._publish(
            "novamarket.shadow.analytics.updated",
            session_id=session_id,
            decisions=updated.decision_count,
            cycles=updated.cycles,
        )
        return {
            "ok": True,
            "session_id": session_id,
            "at": moment.isoformat(),
            "counts": counts,
            "latency_ms": round(elapsed_ms, 3),
        }

    async def _decide(
        self,
        session: ShadowSession,
        baseline: PortfolioSnapshot,
        record: Mapping[str, Any],
        instrument_id: str,
        observation: Mapping[str, Any],
        moment: datetime,
    ) -> tuple[ShadowDecision, bool]:
        started = time.perf_counter()
        strategy_id = str(record["strategy_id"])
        version = str(record.get("version") or "unknown")
        horizon = self._horizon(session, record)
        signal: SignalDecision = self._evaluate(
            record, baseline, instrument_id, observation, moment, horizon
        )
        key = shadow_id(
            "",
            session.session_id,
            strategy_id,
            version,
            instrument_id,
            observation["observed_at"].isoformat(),
            signal.signal_type.value,
        )
        existing = self._store.find_decision(session.session_id, key)
        if existing is not None:
            return existing, True
        held = self._holding(baseline, instrument_id)
        proposal: dict[str, Any] = {"horizon": horizon}
        risk: dict[str, Any] = {}
        projection: ShadowProjection | None = None
        status = ShadowDecisionStatus.SIGNAL
        projection_id = ""
        if signal.signal_type in _ACTIONABLE:
            reference = decimal(observation["price"])
            side = "buy" if signal.signal_type is SignalType.BUY else "sell"
            action = (
                "open_or_increase"
                if side == "buy"
                else ("close" if signal.signal_type is SignalType.EXIT else "reduce")
            )
            try:
                qty = self._proposal_quantity(
                    session, baseline, instrument_id, held, reference, side, signal
                )
                notional = money(qty * reference)
            except ShadowValidationError as exc:
                return (
                    self._decision(
                        session,
                        strategy_id,
                        version,
                        instrument_id,
                        signal,
                        moment,
                        observation,
                        baseline,
                        ShadowDecisionStatus.BLOCKED,
                        {"horizon": horizon, "side": side, "action": action},
                        {"outcome": "not_evaluated", "approved": False, "error": str(exc)},
                        "",
                        latency_ms=(time.perf_counter() - started) * 1000.0,
                    ),
                    False,
                )
            proposal.update(
                {
                    "side": side,
                    "action": action,
                    "quantity": text(qty),
                    "notional": text(notional),
                    "reference_price": text(reference),
                    "order_type": "market",
                    "held_quantity": text(
                        decimal(held.get("quantity") or "0") if held else Decimal("0")
                    ),
                }
            )
            try:
                policy = self._policy_for(session)
                risk_snapshot = self._risk_snapshot(
                    baseline,
                    instrument_id,
                    side,
                    qty,
                    reference,
                    moment,
                    observation,
                    policy,
                )
                evaluation = evaluate_policy(
                    policy,
                    risk_snapshot,
                    action="add_position" if side == "buy" else "reduce_position",
                    adds_exposure=side == "buy",
                )
                risk = evaluation.to_dict()
                risk.update(
                    {
                        "policy_id": policy.policy_id,
                        "policy_version": policy.version,
                        "approved": evaluation.outcome is RiskOutcome.APPROVED,
                        "limits": policy.to_dict(),
                    }
                )
                if evaluation.outcome is RiskOutcome.APPROVED:
                    status = ShadowDecisionStatus.RISK_APPROVED
                    projection = self._projection(
                        session,
                        instrument_id,
                        side,
                        action,
                        qty,
                        notional,
                        reference,
                        observation,
                        moment,
                        key,
                    )
                    projection_id = projection.projection_id
                    status = ShadowDecisionStatus.PROJECTED
                else:
                    status = ShadowDecisionStatus.RISK_REJECTED
            except Exception as exc:  # noqa: BLE001 - a failed risk check is never approval
                status = ShadowDecisionStatus.BLOCKED
                risk = {
                    "outcome": "not_evaluated",
                    "approved": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                self._record_error(
                    session,
                    ShadowErrorKind.RISK_FAILED,
                    f"{strategy_id}/{instrument_id}: {type(exc).__name__}: {exc}",
                    at=moment,
                    instrument_id=instrument_id,
                    strategy_id=strategy_id,
                )
        decision = self._decision(
            session,
            strategy_id,
            version,
            instrument_id,
            signal,
            moment,
            observation,
            baseline,
            status,
            proposal,
            risk,
            projection_id,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )
        decision = self._store.save_decision(decision)
        if projection is not None:
            self._guard.assert_projection(projection)
            self._store.save_projection(projection)
            await self._publish(
                "novamarket.shadow.projection.created",
                session_id=session.session_id,
                decision_id=decision.decision_id,
                projection_id=projection.projection_id,
                instrument_id=instrument_id,
                side=projection.side,
                simulated=True,
            )
        await self._publish(
            "novamarket.shadow.decision.recorded",
            session_id=session.session_id,
            decision_id=decision.decision_id,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            signal_type=decision.signal_type,
            status=decision.status.value,
        )
        if risk:
            await self._publish(
                "novamarket.shadow.risk.evaluated",
                session_id=session.session_id,
                decision_id=decision.decision_id,
                outcome=str(risk.get("outcome") or ""),
                approved=bool(risk.get("approved")),
            )
        return decision, False

    def _decision(
        self,
        session: ShadowSession,
        strategy_id: str,
        version: str,
        instrument_id: str,
        signal: SignalDecision,
        moment: datetime,
        observation: Mapping[str, Any],
        baseline: PortfolioSnapshot,
        status: ShadowDecisionStatus,
        proposal: dict[str, Any],
        risk: dict[str, Any],
        projection_id: str,
        latency_ms: float = 0.0,
    ) -> ShadowDecision:
        key = shadow_id(
            "",
            session.session_id,
            strategy_id,
            version,
            instrument_id,
            observation["observed_at"].isoformat(),
            signal.signal_type.value,
        )
        return ShadowDecision(
            decision_id="shd_" + key,
            session_id=session.session_id,
            strategy_id=strategy_id,
            strategy_version=version,
            instrument_id=instrument_id,
            signal_type=signal.signal_type.value,
            confidence=str(signal.confidence),
            decision_at=moment,
            data_timestamp=observation["observed_at"],
            data_reference=dict(observation["reference"]),
            portfolio_snapshot_id=baseline.snapshot_id,
            risk_policy_version=str(
                risk.get("policy_version") or session.risk_policy_version
            ),
            status=status,
            proposal=proposal,
            risk=risk,
            projection_id=projection_id,
            explanation=signal.explanation,
            conditions=signal.conditions,
            idempotency_key=key,
            latency_ms=latency_ms,
        )

    def _evaluate(
        self,
        record: Mapping[str, Any],
        baseline: PortfolioSnapshot,
        instrument_id: str,
        observation: Mapping[str, Any],
        moment: datetime,
        horizon: str,
    ) -> SignalDecision:
        catalog_id = str(record.get("catalog_id") or "")
        evaluator = EVALUATORS.get(catalog_id)
        if evaluator is None:
            raise ShadowValidationError(f"unknown strategy catalog entry {catalog_id!r}")
        config = dict(record.get("config") or {})
        closes = tuple(float(item) for item in observation.get("closes") or ())
        highs = tuple(float(item) for item in observation.get("highs") or ())
        lows = tuple(float(item) for item in observation.get("lows") or ())
        volumes = tuple(float(item) for item in observation.get("volumes") or ())
        held = self._holding(baseline, instrument_id)
        total = decimal(baseline.total_value)
        context = StrategyContext(
            instrument_id=instrument_id,
            portfolio_id=baseline.portfolio_id,
            horizon=horizon,
            closes=closes,
            highs=highs,
            lows=lows,
            volumes=volumes,
            price=decimal(observation["price"]),
            observed_at=observation["observed_at"],
            now=moment,
            forecasts=tuple(observation.get("forecasts") or ()),
            events=tuple(observation.get("events") or ()),
            held=held is not None,
            held_weight=decimal(held.get("weight") or "0") if held else Decimal("0"),
            cash=decimal(baseline.cash),
            total_value=total,
        )
        return evaluator(context, config)

    # -- observations ---------------------------------------------------------
    def _observe(
        self,
        instrument_id: str,
        moment: datetime,
        *,
        observations: Mapping[str, Mapping[str, Any]] | None,
        source: str = "",
    ) -> dict[str, Any]:
        price: Decimal | None
        observed_at: datetime | None
        explicit = (observations or {}).get(instrument_id)
        if isinstance(explicit, Mapping):
            observed_at = parse_time(explicit.get("observed_at") or moment)
            if observed_at > moment:
                raise ShadowValidationError(
                    "the observation timestamp is after the decision time"
                )
            price = decimal(explicit.get("price"))
            if price <= 0:
                raise ShadowValidationError("the observed price must be positive")
            quality = str(explicit.get("quality") or "valid").lower()
            freshness = str(explicit.get("freshness") or "fresh").lower()
            stale = freshness in {"stale", "expired"} or quality in {
                "invalid",
                "suspect",
                "stale",
            }
            error = ""
            if stale:
                error = f"explicit observation quality={quality} freshness={freshness}"
            bars = self._explicit_bars(explicit, moment)
            return self._observation(
                instrument_id,
                price,
                observed_at,
                quality,
                freshness,
                bars,
                moment,
                source="explicit",
                error=error,
                stale=stale,
            )
        if self._market_data is None:
            raise ShadowValidationError("no market-data source is configured")
        provider = str(source or "").strip()
        if provider.lower() in {"", "nm02", "market", "market_data", "default"}:
            provider = ""
        market_kwargs = {"provider": provider} if provider else {}
        result = self._market_data.latest(instrument_id, **market_kwargs)
        usable = getattr(result, "usable", True)
        selected = getattr(result, "selected", None)
        if selected is None:
            reason = getattr(result, "reason", "") or "no selected observation"
            return self._observation(
                instrument_id,
                None,
                None,
                "unavailable",
                "missing",
                (),
                moment,
                source="nm02",
                error=f"no usable market observation: {reason}",
                stale=False,
            )
        price = _decimal_or_none(getattr(selected, "price", None))
        observed_at = getattr(selected, "observed_at", None)
        quality = _value(getattr(selected, "quality", "unknown"))
        freshness = _value(getattr(selected, "freshness", "unknown"))
        if (
            not usable
            or price is None
            or price <= 0
            or observed_at is None
            or quality in {"invalid", "suspect"}
        ):
            return self._observation(
                instrument_id,
                price,
                observed_at,
                quality,
                freshness,
                (),
                moment,
                source="nm02",
                error=f"market data is not usable (quality={quality}, freshness={freshness})",
                stale=freshness in {"stale", "expired"},
            )
        if not isinstance(observed_at, datetime):
            observed_at = parse_time(observed_at)
        age = (moment - observed_at).total_seconds()
        stale = age > self._stale_after_seconds or freshness in {"stale", "expired"}
        bars = self._bars(instrument_id, moment, provider=provider)
        error = (
            f"market data is {age:.0f}s old (limit {self._stale_after_seconds}s)"
            if stale
            else ""
        )
        return self._observation(
            instrument_id,
            price,
            observed_at,
            quality,
            freshness,
            bars,
            moment,
            source="nm02",
            error=error,
            stale=stale,
        )

    def _observation(
        self,
        instrument_id: str,
        price: Decimal | None,
        observed_at: datetime | None,
        quality: str,
        freshness: str,
        bars: Sequence[Mapping[str, Any]],
        moment: datetime,
        *,
        source: str,
        error: str,
        stale: bool,
    ) -> dict[str, Any]:
        closes = [item["close"] for item in bars]
        # The newest bar already contains the latest close.  Appending the
        # observation again would duplicate the final return and can change a
        # crossover decision; use the quote only when there is no bar history.
        if price is not None and not closes:
            closes = [price]
        reference = {
            "instrument_id": instrument_id,
            "data_source": source,
            "observed_at": observed_at.isoformat() if observed_at else None,
            "retrieved_at": moment.isoformat(),
            "quality": quality,
            "freshness": freshness,
            "bar_count": len(bars),
            "usable": not bool(error),
        }
        return {
            "instrument_id": instrument_id,
            "price": text(price) if price is not None else None,
            "observed_at": observed_at,
            "quality": quality,
            "freshness": freshness,
            "bars": tuple(bars),
            "closes": tuple(closes),
            "highs": tuple(item["high"] for item in bars),
            "lows": tuple(item["low"] for item in bars),
            "volumes": tuple(item["volume"] for item in bars),
            "reference": reference,
            "error": error,
            "stale": stale,
        }

    def _explicit_bars(
        self, payload: Mapping[str, Any], moment: datetime
    ) -> tuple[dict[str, Any], ...]:
        bars = []
        for item in payload.get("bars") or ():
            if not isinstance(item, Mapping):
                continue
            observed = parse_time(item.get("observed_at"))
            available = optional_time(item.get("available_at"))
            if observed > moment or (available is not None and available > moment):
                continue
            bars.append(
                {
                    "observed_at": observed,
                    "close": decimal(item.get("close")),
                    "high": decimal(item.get("high") or item.get("close")),
                    "low": decimal(item.get("low") or item.get("close")),
                    "volume": decimal(item.get("volume") or "0"),
                }
            )
        return tuple(sorted(bars, key=lambda row: row["observed_at"]))

    def _bars(
        self, instrument_id: str, moment: datetime, *, provider: str = ""
    ) -> tuple[dict[str, Any], ...]:
        if self._market_data is None:
            return ()
        try:
            market_kwargs = {"provider": provider} if provider else {}
            page = self._market_data.bars(
                instrument_id, limit=60, ascending=False, **market_kwargs
            )
        except Exception:  # noqa: BLE001 - missing history is not a crash
            return ()
        items = getattr(page, "items", page) or ()
        rows: list[dict[str, Any]] = []
        for bar in items:
            if bool(getattr(bar, "partial", False)):
                continue
            observed = getattr(bar, "observed_at", None)
            if not isinstance(observed, datetime) or observed > moment:
                continue
            available = getattr(bar, "available_at", None)
            if isinstance(available, datetime) and available > moment:
                continue
            close = _decimal_or_none(getattr(bar, "close", None))
            if close is None:
                continue
            rows.append(
                {
                    "observed_at": observed,
                    "close": close,
                    "high": _decimal_or_none(getattr(bar, "high", None)) or close,
                    "low": _decimal_or_none(getattr(bar, "low", None)) or close,
                    "volume": _decimal_or_none(getattr(bar, "volume", None))
                    or Decimal("0"),
                }
            )
        return tuple(sorted(rows, key=lambda row: row["observed_at"]))

    # -- proposals and projections -------------------------------------------
    def _proposal_quantity(
        self,
        session: ShadowSession,
        baseline: PortfolioSnapshot,
        instrument_id: str,
        held: Mapping[str, Any] | None,
        reference: Decimal,
        side: str,
        signal: SignalDecision,
    ) -> Decimal:
        if side == "buy":
            fraction = decimal(session.metadata.get("notional_fraction") or "0.05")
            if fraction <= 0 or fraction > Decimal("0.25"):
                raise ShadowValidationError("notional_fraction must be within (0, 0.25]")
            notional = money(decimal(baseline.total_value) * fraction)
            qty = quantity(notional / reference)
            if qty <= 0:
                raise ShadowValidationError("the proposed quantity is zero")
            return qty
        if held is None:
            raise ShadowValidationError(
                f"a {signal.signal_type.value} signal needs a held position"
            )
        held_qty = decimal(held.get("quantity") or "0")
        if held_qty <= 0:
            value = decimal(held.get("market_value") or "0")
            held_qty = quantity(value / reference) if reference > 0 else Decimal("0")
        if held_qty <= 0:
            raise ShadowValidationError("the held position has no measurable quantity")
        if signal.signal_type is SignalType.EXIT:
            return quantity(held_qty)
        fraction = decimal(session.metadata.get("notional_fraction") or "0.05")
        requested = quantity(decimal(baseline.total_value) * fraction / reference)
        return quantity(min(held_qty, requested))

    def _projection(
        self,
        session: ShadowSession,
        instrument_id: str,
        side: str,
        action: str,
        qty: Decimal,
        notional: Decimal,
        reference: Decimal,
        observation: Mapping[str, Any],
        moment: datetime,
        key: str,
    ) -> ShadowProjection:
        config = self._execution_config
        volume = (
            sum((item["volume"] for item in observation.get("bars") or ()), Decimal("0"))
            / Decimal(len(observation.get("bars") or ()))
            if observation.get("bars")
            else Decimal("0")
        )
        available = quantity(volume * config.participation_rate)
        volatility = self._volatility(observation.get("closes") or ())
        snapshot = MarketSnapshot(
            instrument_id=instrument_id,
            reference_price=reference,
            observed_at=observation["observed_at"],
            source=str(observation["reference"].get("data_source") or "nm02"),
            volume=quantity(volume),
            available_quantity=available,
            volatility_pct=volatility or Decimal("0"),
            trading_status="trading",
            currency="INR",
            is_fallback=False,
            fresh=True,
            reasons=(),
        )
        status = ShadowProjectionStatus.PROJECTED
        estimated: Decimal | None = None
        slippage_pct = Decimal("0")
        notes = ""
        try:
            estimated, _, slippage_pct = execution_price(
                snapshot,
                side=OrderSide(side),
                order_type=OrderType.MARKET,
                quantity=qty,
                config=config,
            )
        except ExecutionPricingError as exc:
            status = ShadowProjectionStatus.UNAVAILABLE_LIQUIDITY
            notes = str(exc)
        fees = fee_breakdown(notional, OrderSide(side), config)
        projection_id = "shp_" + shadow_id("", key)[:20]
        projection = ShadowProjection(
            projection_id=projection_id,
            decision_id="shd_" + key,
            session_id=session.session_id,
            instrument_id=instrument_id,
            side=side,
            action=action,
            quantity=text(qty) or "0",
            order_type="market",
            reference_price=text(reference) or "0",
            estimated_price=text(estimated),
            estimated_slippage_pct=text(slippage_pct) or "0",
            estimated_fees={name: text(value) or "0" for name, value in fees.items()},
            estimated_total_fees=text(fees["total"]) or "0",
            estimated_notional=text(
                money(qty * (estimated if estimated is not None else reference))
            )
            or "0",
            assumptions=(
                "reference price is the latest NM-02 observation",
                f"participation_rate={config.participation_rate}",
                "NM-10 synthetic spread and square-root impact; no order book",
                "fees are the illustrative NM-10 schedule",
                "no order was created, submitted, filled or reserved",
            ),
            status=status,
            created_at=moment,
            notes=notes,
        )
        return projection

    # -- risk -----------------------------------------------------------------
    def _policy(self, version: str) -> RiskPolicy:
        payload: Any = None
        if self._risk_policy_loader is not None:
            payload = self._risk_policy_loader(version)
        if isinstance(payload, RiskPolicy):
            return payload
        if payload is None:
            if self._risk_policy_loader is None:
                return POLICY_DEFAULTS["medium_term"]
            raise ShadowValidationError(
                f"no risk policy is available for version {version!r}"
            )
        if not isinstance(payload, Mapping):
            raise ShadowValidationError("a risk policy must be an object")
        return self._coerce_policy(payload)

    def _policy_for(self, session: ShadowSession) -> RiskPolicy:
        frozen = session.metadata.get("frozen_policy")
        if isinstance(frozen, Mapping):
            return self._coerce_policy(frozen)
        return self._policy(session.risk_policy_version)

    def _coerce_policy(self, payload: Mapping[str, Any]) -> RiskPolicy:
        base = POLICY_DEFAULTS["medium_term"]
        candidate = payload.get("policy")
        body: Mapping[str, Any] = candidate if isinstance(candidate, Mapping) else payload
        updates: dict[str, Any] = {}
        for name in base.__dataclass_fields__:
            if name in body:
                updates[name] = body[name]
        aliases = {
            "max_gross_exposure": "max_invested_fraction",
            "max_drawdown": "max_drawdown_pct",
        }
        for source, target in aliases.items():
            if source in body and target not in updates:
                updates[target] = body[source]
        for name in (
            "max_position_weight",
            "max_sector_weight",
            "max_invested_fraction",
            "min_cash_fraction",
            "max_drawdown_pct",
            "max_volatility_pct",
            "min_liquidity_value",
            "max_turnover",
            "max_aggregate_sector_weight",
        ):
            if name in updates:
                updates[name] = decimal(updates[name])
        if "restricted_instruments" in updates:
            updates["restricted_instruments"] = tuple(updates["restricted_instruments"])
        if "allowed_model_states" in updates:
            updates["allowed_model_states"] = tuple(updates["allowed_model_states"])
        try:
            return replace(base, **updates)
        except (TypeError, ValueError) as exc:
            raise ShadowValidationError(f"invalid risk policy: {exc}") from exc

    def _risk_snapshot(
        self,
        baseline: PortfolioSnapshot,
        instrument_id: str,
        side: str,
        qty: Decimal,
        reference: Decimal,
        moment: datetime,
        observation: Mapping[str, Any],
        policy: RiskPolicy,
    ) -> RiskSnapshot:
        total = decimal(baseline.total_value)
        positions = [dict(item) for item in baseline.positions]
        notional = money(qty * reference)
        target = next(
            (
                item
                for item in positions
                if str(item.get("instrument_id")) == instrument_id
            ),
            None,
        )
        if side == "buy":
            if target is None:
                target = {
                    "instrument_id": instrument_id,
                    "sector": "unknown",
                    "quantity": "0",
                    "market_value": "0.00",
                }
                positions.append(target)
            current_qty = decimal(target.get("quantity") or "0")
            target["quantity"] = text(current_qty + qty)
            target["market_value"] = text(
                decimal(target.get("market_value") or "0") + notional
            )
        else:
            if target is None:
                raise ShadowValidationError("a sell has no held position to reduce")
            current_qty = decimal(target.get("quantity") or "0")
            target["quantity"] = text(max(Decimal("0"), current_qty - qty))
            target["market_value"] = text(
                max(Decimal("0"), decimal(target.get("market_value") or "0") - notional)
            )
        projected = []
        for item in positions:
            value = decimal(item.get("market_value") or "0")
            row = dict(item)
            row["weight"] = text(value / total)
            projected.append(row)
        cash = decimal(baseline.cash) - notional if side == "buy" else decimal(baseline.cash) + notional
        cash = max(Decimal("0"), cash)
        closes = tuple(decimal(item) for item in observation.get("closes") or ())
        volatility = self._volatility(closes)
        volumes = [decimal(item["volume"]) for item in observation.get("bars") or ()]
        average_volume = _mean(volumes)
        liquidity = (
            money(average_volume * reference) if average_volume is not None else None
        )
        data_age = (moment - observation["observed_at"]).total_seconds() / 3600.0
        state_age = (moment - baseline.captured_at).total_seconds() / 3600.0
        sectors: dict[str, Decimal] = {}
        for item in projected:
            sector = str(item.get("sector") or "unknown")
            sectors[sector] = sectors.get(sector, Decimal("0")) + decimal(
                item.get("weight") or "0"
            )
        return RiskSnapshot(
            positions=tuple(dict(item) for item in baseline.positions),
            projected=tuple(projected),
            cash_weight=cash / total,
            invested_fraction=max(Decimal("0"), (total - cash) / total),
            drawdown_pct=Decimal("0"),
            volatility_pct=volatility,
            turnover=notional / total,
            data_age_hours=round(data_age, 6),
            state_age_hours=round(state_age, 6),
            aggregate_sector_weights=sectors,
            liquidity=liquidity,
            capital_request=notional if side == "buy" else None,
            available_cash=decimal(baseline.cash),
        )

    @staticmethod
    def _volatility(closes: Sequence[Any]) -> Decimal | None:
        values = [float(item) for item in closes]
        if len(values) < 3:
            return None
        returns = [
            (current - previous) / previous
            for previous, current in zip(values, values[1:])
            if previous > 0
        ]
        if len(returns) < 2:
            return None
        return Decimal(str(statistics.pstdev(returns) * 100)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_EVEN
        )

    # -- outcomes -------------------------------------------------------------
    async def track_outcomes(
        self,
        session_id: str,
        *,
        now: datetime | None = None,
        observations: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        session = self._load(session_id)
        if session.status is ShadowSessionStatus.CREATED:
            raise ShadowStateError("the session has not started; there is nothing to track")
        moment = parse_time(now) if now is not None else self._clock()
        horizon_days = int(session.metadata.get("outcome_horizon_days") or 30)
        written = 0
        resolved = 0
        unresolved = 0
        for decision in self._store.decisions(session_id, limit=1000):
            if decision.status is not ShadowDecisionStatus.PROJECTED:
                continue
            projection = self._store.projection_for(decision.decision_id)
            if projection is None:
                continue
            observation = self._outcome_observation(
                session, decision.instrument_id, moment, observations
            )
            if observation is None:
                for interval in session.observation_intervals:
                    outcome = self._outcome(
                        decision,
                        interval,
                        ShadowOutcomeStatus.UNRESOLVED,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        0,
                        "no later market observation is available",
                        moment,
                    )
                    self._store.save_outcome(outcome)
                    written += 1
                    unresolved += 1
                continue
            observed_at = observation["observed_at"]
            if observed_at <= decision.decision_at:
                for interval in session.observation_intervals:
                    outcome = self._outcome(
                        decision,
                        interval,
                        ShadowOutcomeStatus.UNRESOLVED,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        0,
                        "the observation is not later than the decision",
                        moment,
                    )
                    self._store.save_outcome(outcome)
                    written += 1
                    unresolved += 1
                continue
            subsequent = [
                item
                for item in observation.get("bars") or ()
                if item["observed_at"] > decision.decision_at
                and item["observed_at"] <= observed_at
            ]
            subsequent.sort(key=lambda item: item["observed_at"])
            age_days = (moment - decision.decision_at).total_seconds() / 86400.0
            for interval in session.observation_intervals:
                if len(subsequent) >= interval:
                    bar = subsequent[interval - 1]
                    price: Decimal | None = bar["close"]
                    outcome_at: datetime | None = bar["observed_at"]
                    use = subsequent[:interval]
                    status = ShadowOutcomeStatus.RESOLVED
                    resolved += 1
                    note = f"observed after {interval} subsequent bar(s)"
                else:
                    price = None
                    outcome_at = None
                    use = subsequent
                    status = (
                        ShadowOutcomeStatus.EXPIRED
                        if age_days > horizon_days
                        else ShadowOutcomeStatus.UNRESOLVED
                    )
                    if status is ShadowOutcomeStatus.EXPIRED:
                        note = "the outcome horizon elapsed before the interval was reached"
                    else:
                        note = f"{len(subsequent)} later bar(s); interval {interval} not reached"
                    unresolved += 1
                mfe, mae = self._excursions(
                    decision.proposal, decision.decision_at, use
                )
                result = self._outcome(
                    decision,
                    interval,
                    status,
                    outcome_at,
                    price,
                    None if price is None else self._return_pct(decision, price),
                    None if price is None else self._pnl(decision, price),
                    mfe,
                    mae,
                    len(use),
                    note,
                    moment,
                )
                self._store.save_outcome(result)
                written += 1
                await self._publish(
                    "novamarket.shadow.outcome.updated",
                    session_id=session_id,
                    decision_id=decision.decision_id,
                    interval=interval,
                    status=result.status.value,
                    hypothetical=True,
                )
        return {
            "ok": True,
            "written": written,
            "resolved": resolved,
            "unresolved": unresolved,
        }

    def _outcome_observation(
        self,
        session: ShadowSession,
        instrument_id: str,
        moment: datetime,
        observations: Mapping[str, Mapping[str, Any]] | None,
    ) -> dict[str, Any] | None:
        try:
            observation = self._observe(
                instrument_id,
                moment,
                observations=observations,
                source=session.market_data_source,
            )
        except Exception:  # noqa: BLE001 - missing outcome data is unresolved
            return None
        if observation.get("error") or observation.get("price") is None:
            return None
        return observation

    def _outcome(
        self,
        decision: ShadowDecision,
        interval: int,
        status: ShadowOutcomeStatus,
        observed_at: datetime | None,
        price: Decimal | None,
        return_pct: Decimal | None,
        pnl: Decimal | None,
        mfe: Decimal | None,
        mae: Decimal | None,
        bars: int,
        note: str,
        moment: datetime,
    ) -> ShadowOutcome:
        return ShadowOutcome(
            outcome_id="sho_" + shadow_id("", decision.decision_id, interval)[:20],
            decision_id=decision.decision_id,
            session_id=decision.session_id,
            instrument_id=decision.instrument_id,
            interval=interval,
            status=status,
            observed_at=observed_at,
            price=text(price),
            return_pct=text(return_pct),
            pnl=text(pnl),
            mfe_pct=text(mfe),
            mae_pct=text(mae),
            bars_observed=bars,
            note=note,
            updated_at=moment,
        )

    def _return_pct(self, decision: ShadowDecision, price: Decimal) -> Decimal:
        reference = decimal(decision.proposal.get("reference_price") or "0")
        if reference <= 0:
            return Decimal("0")
        move = (price - reference) / reference
        return (move * Decimal("100")).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_EVEN
        )

    def _pnl(self, decision: ShadowDecision, price: Decimal) -> Decimal:
        reference = decimal(decision.proposal.get("reference_price") or "0")
        qty = decimal(decision.proposal.get("quantity") or "0")
        sign = Decimal("1") if str(decision.proposal.get("side")) == "buy" else Decimal("-1")
        return money((price - reference) * qty * sign)

    def _excursions(
        self,
        proposal: Mapping[str, Any],
        decision_at: datetime,
        bars: Sequence[Mapping[str, Any]],
    ) -> tuple[Decimal | None, Decimal | None]:
        if not bars:
            return None, None
        reference = decimal(proposal.get("reference_price") or "0")
        if reference <= 0:
            return None, None
        side = str(proposal.get("side") or "")
        if side == "buy":
            best = max(item["high"] for item in bars)
            worst = min(item["low"] for item in bars)
            mfe = max(Decimal("0"), (best - reference) / reference * 100)
            mae = min(Decimal("0"), (worst - reference) / reference * 100)
        else:
            best = min(item["low"] for item in bars)
            worst = max(item["high"] for item in bars)
            mfe = max(Decimal("0"), (reference - best) / reference * 100)
            mae = min(Decimal("0"), (reference - worst) / reference * 100)
        return (
            mfe.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN),
            mae.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN),
        )

    # -- analytics ------------------------------------------------------------
    def analytics(self, session_id: str) -> dict[str, Any]:
        session = self._load(session_id)
        decisions = self._store.decisions(session_id, limit=1000)
        projections = self._store.projections(session_id, limit=1000)
        outcomes = self._store.outcomes(session_id, limit=5000)
        errors = self._store.errors(session_id, limit=500)
        signals = Counter(item.signal_type for item in decisions)
        statuses = Counter(item.status.value for item in decisions)
        projection_status = Counter(item.status.value for item in projections)
        outcome_status = Counter(item.status.value for item in outcomes)
        resolved = [
            item for item in outcomes if item.status is ShadowOutcomeStatus.RESOLVED
        ]
        returns = [
            decimal(item.return_pct)
            for item in resolved
            if item.return_pct is not None
        ]
        wins = [item for item in returns if item > 0]
        losses = [item for item in returns if item < 0]
        mfe = [decimal(item.mfe_pct) for item in resolved if item.mfe_pct is not None]
        mae = [decimal(item.mae_pct) for item in resolved if item.mae_pct is not None]
        rejection_reasons: Counter[str] = Counter()
        for decision in decisions:
            for breach in decision.risk.get("breaches") or ():
                rejection_reasons[str(breach.get("code") or breach.get("message") or "")] += 1
            for reason in decision.risk.get("insufficient") or ():
                rejection_reasons[str(reason)] += 1
        data_failures = [
            item
            for item in errors
            if str(item.get("kind", "")).startswith("data_")
        ]
        by_strategy: dict[str, dict[str, Any]] = {}
        by_instrument: dict[str, dict[str, Any]] = {}
        by_horizon: dict[str, dict[str, Any]] = {}
        for decision in decisions:
            bucket = by_strategy.setdefault(
                decision.strategy_id,
                {"decisions": 0, "projected": 0, "approved": 0, "rejected": 0},
            )
            bucket["decisions"] += 1
            if decision.projection_id:
                bucket["projected"] += 1
            if decision.risk.get("approved"):
                bucket["approved"] += 1
            if decision.status is ShadowDecisionStatus.RISK_REJECTED:
                bucket["rejected"] += 1
            instrument = by_instrument.setdefault(
                decision.instrument_id,
                {"decisions": 0, "projected": 0, "resolved": 0, "mean_return_pct": None},
            )
            instrument["decisions"] += 1
            if decision.projection_id:
                instrument["projected"] += 1
            horizon = str(decision.proposal.get("horizon") or "unknown")
            hbucket = by_horizon.setdefault(
                horizon, {"decisions": 0, "resolved": 0, "mean_return_pct": None}
            )
            hbucket["decisions"] += 1
        latest_outcomes: dict[str, ShadowOutcome] = {}
        for resolved_row in resolved:
            latest_outcomes[resolved_row.decision_id] = resolved_row
        for decision in decisions:
            outcome_row = latest_outcomes.get(decision.decision_id)
            if outcome_row is None:
                continue
            value = decimal(outcome_row.return_pct or "0")
            instrument_bucket = by_instrument.get(decision.instrument_id)
            if instrument_bucket is not None:
                instrument_bucket["resolved"] = int(instrument_bucket["resolved"]) + 1
                previous_mean = instrument_bucket["mean_return_pct"]
                instrument_bucket["mean_return_pct"] = text(
                    value
                    if previous_mean is None
                    else (
                        decimal(previous_mean)
                        * (int(instrument_bucket["resolved"]) - 1)
                        + value
                    )
                    / int(instrument_bucket["resolved"])
                )
            horizon = str(decision.proposal.get("horizon") or "unknown")
            horizon_bucket = by_horizon.get(horizon)
            if horizon_bucket is not None:
                horizon_bucket["resolved"] = int(horizon_bucket["resolved"]) + 1
                previous_mean = horizon_bucket["mean_return_pct"]
                horizon_bucket["mean_return_pct"] = text(
                    value
                    if previous_mean is None
                    else (
                        decimal(previous_mean)
                        * (int(horizon_bucket["resolved"]) - 1)
                        + value
                    )
                    / int(horizon_bucket["resolved"])
                )
        mean_return = _mean(returns)
        resolved_decisions = {item.decision_id for item in resolved}
        sample_sufficient = len(resolved_decisions) >= self._min_sample_size
        latencies = [item.latency_ms for item in decisions if item.latency_ms]
        return {
            "ok": True,
            "session_id": session_id,
            "status": session.status.value,
            "hypothetical": True,
            "simulated": True,
            "decisions": {
                "total": len(decisions),
                "signals": dict(signals),
                "statuses": dict(statuses),
                "buys": signals.get("buy", 0),
                "sells": signals.get("sell", 0),
                "exits": signals.get("exit", 0),
                "holds": signals.get("hold", 0),
                "no_signal": signals.get("no_signal", 0),
            },
            "risk": {
                "approved": sum(1 for item in decisions if item.risk.get("approved")),
                "rejected": sum(
                    1
                    for item in decisions
                    if item.status is ShadowDecisionStatus.RISK_REJECTED
                ),
                "blocked": sum(
                    1
                    for item in decisions
                    if item.status is ShadowDecisionStatus.BLOCKED
                ),
                "rejection_reasons": dict(rejection_reasons),
            },
            "projections": {
                "total": len(projections),
                "statuses": dict(projection_status),
                "any_execution_order_id": any(
                    item.execution_order_id for item in projections
                ),
                "execution_permitted": False,
            },
            "outcomes": {
                "total": len(outcomes),
                "statuses": dict(outcome_status),
                "resolved": len(resolved),
                "resolved_decisions": len(resolved_decisions),
                "unresolved": outcome_status.get("unresolved", 0),
                "expired": outcome_status.get("expired", 0),
                "sample_sufficient": sample_sufficient,
                "minimum_sample": self._min_sample_size,
                "win_rate_pct": (
                    text((Decimal(len(wins)) / Decimal(len(returns)) * 100).quantize(Decimal("0.01")))
                    if sample_sufficient and returns
                    else None
                ),
                "wins": len(wins),
                "losses": len(losses),
                "mean_return_pct": text(mean_return),
                "mean_mfe_pct": text(_mean(mfe)),
                "mean_mae_pct": text(_mean(mae)),
            },
            "by_strategy": by_strategy,
            "by_instrument": by_instrument,
            "by_horizon": by_horizon,
            "data_quality_failures": len(data_failures),
            "errors": len(errors),
            "decision_latency_ms": (
                round(sum(latencies) / len(latencies), 3) if latencies else None
            ),
            "note": (
                "All figures are hypothetical shadow observations. They are not "
                "actual investment returns and may use a sample too small to be "
                "meaningful."
            ),
        }

    # -- health, errors and comparison ---------------------------------------
    def health(self, session_id: str) -> dict[str, Any]:
        session = self._load(session_id)
        errors = self._store.errors(session_id, limit=20)
        data_failures = [item for item in errors if str(item.get("kind", "")).startswith("data_")]
        last_cycle = session.last_cycle_at
        age = (
            (self._clock() - last_cycle).total_seconds()
            if last_cycle is not None
            else None
        )
        return {
            "ok": True,
            "session_id": session_id,
            "status": session.status.value,
            "cycles": session.cycles,
            "last_cycle_at": last_cycle.isoformat() if last_cycle else None,
            "last_cycle_age_seconds": None if age is None else round(age, 3),
            "data_failures": len(data_failures),
            "errors": len(errors),
            "healthy": session.status is not ShadowSessionStatus.FAILED
            and not (age is not None and age > session.monitoring_interval_seconds * 3),
            "recent_errors": errors[:5],
            "execution_connected": False,
        }

    def errors(self, session_id: str, *, limit: int = 100) -> dict[str, Any]:
        rows = self._store.errors(session_id, limit=limit)
        return {"ok": True, "errors": rows, "total": len(rows)}

    async def compare(
        self, session_id: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        session = self._load(session_id)
        body = dict(payload or {})
        analytics = self.analytics(session_id)
        categories: list[str] = []
        evidence: dict[str, Any] = {}
        compatible = False
        note = ""
        reference = body.get("reference")
        backtest_id = str(body.get("backtest_id") or "")
        if backtest_id and self._backtest_loader is not None:
            try:
                result = self._backtest_loader(backtest_id)
                metrics = dict(result.get("metrics") or {})
                result_body = result.get("result")
                if not metrics and isinstance(result_body, Mapping):
                    metrics = dict(result_body.get("metrics") or {})
                reference_return = metrics.get("total_return") or metrics.get(
                    "total_return_pct"
                )
                if reference_return is not None:
                    reference_value = decimal(reference_return)
                    if abs(reference_value) <= 1:
                        reference_value *= Decimal("100")
                    reference_return = text(reference_value)
                shadow_return = analytics["outcomes"].get("mean_return_pct")
                evidence["backtest"] = {
                    "backtest_id": backtest_id,
                    "metrics": metrics,
                    "reference_return": reference_return,
                }
                evidence["shadow_return_pct"] = shadow_return
                compatible = bool(reference_return is not None and shadow_return is not None)
                if compatible:
                    difference = abs(
                        decimal(shadow_return) - decimal(reference_return)
                    )
                    if difference > decimal(body.get("tolerance_pct") or "1"):
                        categories.append("outcome_divergence")
                    categories.append("timing_difference")
                    categories.append("execution_assumption_difference")
                else:
                    note = "the backtest has no comparable return metric"
            except Exception as exc:  # noqa: BLE001 - comparison never fabricates
                note = f"backtest reference unavailable: {type(exc).__name__}: {exc}"
        elif isinstance(reference, Mapping):
            compatible = True
            expected_signals = reference.get("signals")
            if isinstance(expected_signals, Mapping):
                actual = analytics["decisions"]["signals"]
                for key, value in expected_signals.items():
                    if int(value) != int(actual.get(str(key), 0)):
                        categories.append("signal_disagreement")
                        break
                evidence["signals"] = {
                    "shadow": actual,
                    "reference": dict(expected_signals),
                }
            expected_risk = reference.get("risk_outcomes")
            if isinstance(expected_risk, Mapping):
                actual_risk = {
                    "approved": analytics["risk"]["approved"],
                    "rejected": analytics["risk"]["rejected"],
                }
                if any(
                    int(value) != int(actual_risk.get(str(key), 0))
                    for key, value in expected_risk.items()
                ):
                    categories.append("risk_decision_disagreement")
                evidence["risk"] = {
                    "shadow": actual_risk,
                    "reference": dict(expected_risk),
                }
            expected_data = reference.get("data_quality")
            if isinstance(expected_data, Mapping):
                actual_failures = int(analytics["data_quality_failures"])
                expected_failures = int(expected_data.get("failures", 0))
                if expected_failures != actual_failures:
                    categories.append("data_mismatch")
                evidence["data_quality"] = {
                    "shadow_failures": actual_failures,
                    "reference_failures": expected_failures,
                }
            expected_trades = reference.get("trades")
            if expected_trades is not None:
                if int(expected_trades) != int(analytics["projections"]["total"]):
                    categories.append("execution_assumption_difference")
                evidence["trades"] = {
                    "shadow_projections": analytics["projections"]["total"],
                    "reference_trades": int(expected_trades),
                }
            if reference.get("metrics"):
                evidence["metrics"] = dict(reference["metrics"])
                categories.append("outcome_divergence") if not categories else None
            categories.append("timing_difference")
        else:
            note = (
                "no compatible reference was supplied; compare needs a backtest_id "
                "or an explicit reference object"
            )
        categories = sorted(set(categories))
        comparison = ShadowComparison(
            comparison_id="shc_" + shadow_id("", session_id, time.time_ns())[:20],
            session_id=session_id,
            categories=tuple(categories),
            evidence=evidence,
            compatible=compatible,
            note=note,
            created_at=self._clock(),
        )
        self._store.save_comparison(comparison)
        if "outcome_divergence" in categories:
            await self._publish(
                "novamarket.shadow.divergence.detected",
                session_id=session_id,
                comparison_id=comparison.comparison_id,
                category="outcome_divergence",
            )
        await self._publish(
            "novamarket.shadow.comparison.completed",
            session_id=session_id,
            comparison_id=comparison.comparison_id,
            compatible=compatible,
            categories=categories,
        )
        return {"ok": True, "comparison": comparison.to_dict()}

    def comparisons(self, session_id: str, *, limit: int = 50) -> dict[str, Any]:
        rows = self._store.comparisons(session_id, limit=limit)
        return {
            "ok": True,
            "comparisons": [item.to_dict() for item in rows],
            "total": len(rows),
        }

    # -- reads ----------------------------------------------------------------
    def decisions(
        self,
        session_id: str,
        *,
        limit: int = 200,
        instrument_id: str = "",
        strategy_id: str = "",
        signal_type: str = "",
        status: str = "",
    ) -> dict[str, Any]:
        self._load(session_id)
        rows = self._store.decisions(
            session_id,
            limit=limit,
            instrument_id=instrument_id,
            strategy_id=strategy_id,
            signal_type=signal_type,
            status=status,
        )
        return {"ok": True, "decisions": [item.to_dict() for item in rows], "total": len(rows)}

    def projections(self, session_id: str, *, limit: int = 200) -> dict[str, Any]:
        self._load(session_id)
        rows = self._store.projections(session_id, limit=limit)
        return {
            "ok": True,
            "projections": [item.to_dict() for item in rows],
            "total": len(rows),
        }

    def outcomes(self, session_id: str, *, limit: int = 500) -> dict[str, Any]:
        self._load(session_id)
        rows = self._store.outcomes(session_id, limit=limit)
        return {"ok": True, "outcomes": [item.to_dict() for item in rows], "total": len(rows)}

    # -- helpers --------------------------------------------------------------
    def _load(self, session_id: str) -> ShadowSession:
        session = self._store.session(str(session_id))
        if session is None:
            raise ShadowNotFoundError(f"unknown shadow session {session_id!r}")
        return session

    def _normalise_strategies(
        self, raw: Sequence[Any]
    ) -> tuple[tuple[str, ...], dict[str, dict[str, Any]]]:
        ids: list[str] = []
        inline: dict[str, dict[str, Any]] = {}
        for item in raw:
            if isinstance(item, Mapping):
                strategy_id = str(item.get("strategy_id") or item.get("id") or "").strip()
                if not strategy_id:
                    raise ShadowValidationError("an inline strategy needs a strategy_id")
                catalog_id = str(item.get("catalog_id") or strategy_id)
                if catalog_id not in EVALUATORS:
                    raise ShadowValidationError(
                        f"unknown strategy catalog entry {catalog_id!r}"
                    )
                config = validate_config(
                    catalog_id, dict(item.get("config") or {})
                )
                inline[strategy_id] = {
                    "strategy_id": strategy_id,
                    "catalog_id": catalog_id,
                    "version": str(item.get("version") or "inline-1"),
                    "config": config,
                    "status": str(item.get("status") or "active"),
                    "horizons": list(
                        item.get("horizons")
                        or ("short_term", "medium_term", "long_term")
                    ),
                }
                ids.append(strategy_id)
            else:
                strategy_id = str(item).strip()
                if not strategy_id:
                    continue
                ids.append(strategy_id)
        return tuple(dict.fromkeys(ids)), inline

    def _strategy(self, strategy_id: str, session: ShadowSession) -> Mapping[str, Any]:
        frozen = (session.metadata.get("frozen_strategies") or {}).get(strategy_id)
        if isinstance(frozen, Mapping):
            return frozen
        inline = (session.metadata.get("inline_strategies") or {}).get(strategy_id)
        if isinstance(inline, Mapping):
            return inline
        if self._strategy_loader is None:
            raise ShadowValidationError(
                f"strategy {strategy_id!r} cannot be resolved without a loader"
            )
        record = self._strategy_loader(strategy_id)
        if not isinstance(record, Mapping):
            raise ShadowValidationError(f"strategy {strategy_id!r} is not a mapping")
        candidate = record.get("strategy")
        body: Mapping[str, Any] = candidate if isinstance(candidate, Mapping) else record
        if str(body.get("status") or "active") != "active":
            raise ShadowStateError(
                f"strategy {strategy_id} is {body.get('status')}; only active strategies evaluate"
            )
        return body

    def _horizon(self, session: ShadowSession, record: Mapping[str, Any]) -> str:
        offered = tuple(str(item) for item in record.get("horizons") or ())
        for horizon in session.horizons:
            if not offered or horizon in offered:
                return horizon
        return session.horizons[0] if session.horizons else "short_term"

    def _holding(
        self, baseline: PortfolioSnapshot, instrument_id: str
    ) -> Mapping[str, Any] | None:
        return next(
            (
                item
                for item in baseline.positions
                if str(item.get("instrument_id")) == instrument_id
            ),
            None,
        )

    def _freeze_selection(self, session: ShadowSession) -> ShadowSession:
        """Freeze the resolved strategy definitions and policy at session start.

        A later strategy edit or policy edit must not silently change an
        already-running shadow session; the frozen copies are what the decisions
        will use.  A resolution failure is left for the cycle to report as a
        structured strategy/risk failure instead of fabricating a selection.
        """
        metadata = dict(session.metadata)
        if "frozen_strategies" not in metadata:
            frozen: dict[str, dict[str, Any]] = {}
            for strategy_id in session.strategies:
                try:
                    frozen[strategy_id] = dict(self._strategy(strategy_id, session))
                except Exception:  # noqa: BLE001 - reported when the cycle runs
                    continue
            if frozen:
                metadata["frozen_strategies"] = frozen
        if "frozen_policy" not in metadata:
            try:
                metadata["frozen_policy"] = self._policy(
                    session.risk_policy_version
                ).to_dict()
            except Exception:  # noqa: BLE001 - reported as a blocked risk check
                pass
        return replace(session, metadata=metadata)

    def _capture_baseline(self, session: ShadowSession, moment: datetime) -> PortfolioSnapshot:
        payload = session.metadata.get("baseline_payload")
        if session.portfolio_id and self._portfolio_loader is not None:
            detail = self._portfolio_loader(session.portfolio_id)
            return PortfolioSnapshot.from_detail(
                session.portfolio_id, detail, at=moment
            )
        if isinstance(payload, Mapping):
            return PortfolioSnapshot.from_mapping(payload, at=moment)
        raise ShadowValidationError(
            "no portfolio loader and no baseline snapshot; cannot start the session"
        )

    def _record_error(
        self,
        session: ShadowSession,
        kind: ShadowErrorKind,
        message: str,
        *,
        at: datetime,
        instrument_id: str = "",
        strategy_id: str = "",
    ) -> None:
        self._last_error = message
        error = {
            "error_id": "she_"
            + shadow_id("", session.session_id, kind.value, message, at.isoformat())[:20],
            "session_id": session.session_id,
            "kind": kind.value,
            "message": message,
            "at": at.isoformat(),
            "instrument_id": instrument_id,
            "strategy_id": strategy_id,
        }
        self._store.save_error(
            error_id=error["error_id"],
            session_id=session.session_id,
            kind=kind.value,
            at=at,
            payload=error,
        )

    def _ensure_monitor(self, session: ShadowSession) -> None:
        existing = self._monitor_tasks.get(session.session_id)
        if existing is not None and not existing.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._monitor_tasks[session.session_id] = loop.create_task(
            self._monitor(session.session_id)
        )

    async def _monitor(self, session_id: str) -> None:
        while True:
            session = self._store.session(session_id)
            if session is None or session.status is not ShadowSessionStatus.RUNNING:
                return
            interval = session.monitoring_interval_seconds
            try:
                await self.run_cycle(session_id)
                await self.track_outcomes(session_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a monitor never dies silently
                self._last_error = f"{type(exc).__name__}: {exc}"
                session = self._store.session(session_id)
                if session is not None:
                    self._record_error(
                        session,
                        ShadowErrorKind.CYCLE_FAILED,
                        self._last_error,
                        at=self._clock(),
                    )
            await asyncio.sleep(max(1, interval))

    def _cancel_monitor(self, session_id: str) -> None:
        task = self._monitor_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _publish(self, event_type: str, **payload: Any) -> None:
        if self._publisher is None:
            return
        try:
            result = self._publisher(event_type, payload)
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:  # noqa: BLE001 - publishing never breaks a cycle
            self._last_error = f"event publish failed: {type(exc).__name__}: {exc}"

    async def close(self) -> None:
        for session_id in tuple(self._monitor_tasks):
            self._cancel_monitor(session_id)
        await asyncio.sleep(0)
        self._store.close()


__all__ = [
    "ShadowError",
    "ShadowNotFoundError",
    "ShadowService",
    "ShadowStateError",
    "ShadowValidationError",
]
