"""AT-03 decision and risk pipeline.

The pipeline composes accepted components; it does not replace them:

* AT-01 :class:`AutonomyRuntime` owns the mode, lease and one-cycle rule.
* AT-02 monitoring records supply the eligible, processed candidate set.
* NM-09 :class:`StrategyEngine` evaluates its existing strategies.
* NM-08 :class:`RiskEngine` is the sole risk authority.

An approved proposal is a record of a risk decision.  There is deliberately no
order, fill, portfolio gateway, ledger or execution import in this module.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from novacontrol.novamarket.autonomy.models import HORIZON_MAPPING
from novacontrol.novamarket.autonomy.runtime import AutonomyRuntime
from novacontrol.novamarket.decision.models import (
    DECISION_CONTRACT_VERSION,
    DecisionError,
    DecisionErrorCode,
    DecisionEvaluation,
    DecisionEvent,
    DecisionNotFound,
    DecisionProposal,
    DecisionProposalStatus,
    DecisionRun,
    DecisionRunStatus,
    DecisionSignal,
    DecisionStateError,
    DecisionValidationError,
    EvaluationOutcome,
    MAX_INSTRUMENTS_PER_RUN,
    MAX_STRATEGIES_PER_RUN,
    PROPOSAL_TRANSITIONS,
    PROPOSAL_TTL_HOURS,
    RiskAssessment,
    SignalValidation,
    decision_id,
    digest_payload,
    ensure_proposal_transition,
    utcnow,
)
from novacontrol.novamarket.decision.forecast import (
    FORECAST_DECISION_CONTRACT_VERSION,
    ForecastDecisionConfig,
    ForecastDecisionConfigError,
    ForecastDecisionEvaluator,
    ForecastDecisionStatus,
    ForecastDecisionType,
)
from novacontrol.novamarket.decision.store import DecisionStore
from novacontrol.novamarket.portfolio.models import policy_for
from novacontrol.novamarket.strategies.models import StrategySignal, SignalType

_LOGGER = logging.getLogger("novacontrol.novamarket.decision")

_ACTIONABLE = {SignalType.BUY, SignalType.SELL, SignalType.EXIT}
_NO_ACTION = {SignalType.HOLD, SignalType.NO_SIGNAL}
_RISK_ACTION = {
    SignalType.BUY: "add_position",
    SignalType.SELL: "reduce_position",
    SignalType.EXIT: "remove_position",
}
_SIDE = {SignalType.BUY: "buy", SignalType.SELL: "sell", SignalType.EXIT: "sell"}
_ACTION = {
    SignalType.BUY: "add_position",
    SignalType.SELL: "reduce_position",
    SignalType.EXIT: "remove_position",
}
_ALLOWED_QUALITY = {"measured"}
_ALLOWED_CANDIDATE_QUALITY = {"good", "partial"}
_RISK_STATUS = {
    "approved": DecisionProposalStatus.RISK_APPROVED,
    "rejected": DecisionProposalStatus.RISK_REJECTED,
    "restricted": DecisionProposalStatus.RISK_RESTRICTED,
    "requires_review": DecisionProposalStatus.RISK_REVIEW,
    "insufficient_data": DecisionProposalStatus.RISK_REJECTED,
}


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _as_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if parsed.is_nan() or parsed.is_infinite():
        return None
    return parsed


def _text(value: object, default: str = "") -> str:
    return str(value if value is not None else default).strip()


def _freshness(quality: str) -> str:
    return {
        "good": "fresh",
        "partial": "delayed",
        "stale": "stale",
        "unavailable": "unavailable",
    }.get(quality, "unavailable")


class DecisionPipelineError(DecisionError):
    pass


class AutonomyDecisionPipeline:
    """The one AT-03 orchestrator.  It is read-only to the financial book."""

    def __init__(
        self,
        runtime: AutonomyRuntime,
        strategies: Any,
        risk: Any,
        portfolios: Any,
        *,
        market_data: Any | None = None,
        publisher: Callable[[str, dict[str, Any]], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        risk_timeout_seconds: float = 30.0,
        forecasts: Any | None = None,
        features: Any | None = None,
        radar: Any | None = None,
        evaluations: Any | None = None,
        open_orders: Callable[[str, str], Sequence[Mapping[str, Any]]] | None = None,
        forecast_config: ForecastDecisionConfig | Mapping[str, Any] | None = None,
        advisory_evidence: Callable[[DecisionSignal], Mapping[str, Any] | None]
        | None = None,
        forecast_optimization_evidence: Callable[
            [DecisionSignal], Mapping[str, Any] | None
        ]
        | None = None,
    ) -> None:
        self._runtime = runtime
        self._strategies = strategies
        self._risk = risk
        self._portfolios = portfolios
        self._market_data = market_data
        self._publisher = publisher
        self._forecasts = forecasts
        self._features = features
        self._radar = radar
        self._evaluations = evaluations
        self._open_orders = open_orders
        self._forecast_config_override = forecast_config
        # Read-only PI-17 evidence; it never selects an action or a risk outcome.
        self._advisory_evidence = advisory_evidence
        self._advisory_error = ""
        # Read-only PI-23 selection/reliability/fallback evidence; same rule.
        self._forecast_optimization_evidence = forecast_optimization_evidence
        self._forecast_optimization_error = ""
        self._clock = clock or utcnow
        self._risk_timeout_seconds = max(0.1, min(float(risk_timeout_seconds), 300.0))
        self._store = DecisionStore(runtime.store, clock=self._clock)
        self._lock = asyncio.Lock()
        self._closed = False
        self._last_error = ""
        self._last_error_code = ""

    @property
    def store(self) -> DecisionStore:
        return self._store

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_error(self) -> str:
        return self._last_error

    def _now(self) -> datetime:
        return self._clock()

    def close(self) -> None:
        self._closed = True

    def recover(self) -> int:
        """Mark abandoned decision runs failed.  It never resumes a run."""

        interrupted = self._store.recover(self._now())
        if interrupted:
            _LOGGER.warning(
                "novamarket.decision.recovered interrupted_runs=%s", interrupted
            )
        return interrupted

    # -- contract and reads ---------------------------------------------------
    def contract(self) -> dict[str, Any]:
        return {
            "implemented": True,
            "phase": "AT-03",
            "milestone": "AT-03",
            "version": DECISION_CONTRACT_VERSION,
            "paper_only": True,
            "live_trading": False,
            "broker_connected": False,
            "execution_implemented": False,
            "orders_possible": False,
            "risk_engine": "NM-08",
            "strategy_engine": "NM-09",
            "monitoring": "AT-02",
            "runtime": "AT-01",
            "risk_authority": "NM-08 is the sole risk authority",
            "execution_authority": "NM-10 (not called by AT-03)",
            "prediction_integration": {
                "phase": "PI-12",
                "contract_version": FORECAST_DECISION_CONTRACT_VERSION,
                "forecast_sources": ["PI-08", "PI-10", "PI-11"],
                "decision_types": [item.value for item in ForecastDecisionType],
                "configured": self._forecasts is not None,
                "forecast_authorizes_action": False,
                "risk_authority": "NM-08",
                "execution_authority": "NM-10",
            },
            "horizon_mapping": dict(HORIZON_MAPPING),
            "signal_directions": [item.value for item in SignalType],
            "proposal_statuses": [item.value for item in DecisionProposalStatus],
            "risk_outcomes": sorted(_RISK_STATUS),
            "error_codes": [item.value for item in DecisionErrorCode],
            "checks": [
                "AT-01 runtime ownership and lease",
                "AT-02 completed, session-eligible monitoring cycle",
                "AT-02 processed candidates only",
                "NM-09 existing strategy evaluation",
                "data freshness and reference price validation",
                "NM-08 assess() only; no proposal forwarding",
                "persisted signal -> proposal -> risk provenance",
                "record-level idempotency",
            ],
            "non_goals": [
                "order submission",
                "fill creation",
                "portfolio mutation",
                "NM-10 forwarding",
                "target-weight rebalancing",
                "automatic learning promotion",
                "live trading",
            ],
        }

    def status(self) -> dict[str, Any]:
        runtime_status: dict[str, Any] = {}
        try:
            runtime_status = dict(self._runtime.status())
        except Exception as exc:  # noqa: BLE001 - status must remain readable
            runtime_status = {"mode": "unknown", "lease": {}, "last_error": str(exc)}
        metrics = self._store.metrics()
        runs = self._store.runs(limit=10)
        last_run = runs[0] if runs else None
        cycle = self._latest_completed_cycle()
        latest_cycle_id = str((cycle or {}).get("cycle_id") or "") if cycle else ""
        recent_proposals = self._store.proposals(limit=10)
        health_reasons: list[str] = []
        if runtime_status.get("mode") in {"disabled", "unknown"}:
            health_reasons.append("the AT-01 runtime is not enabled")
        lease = runtime_status.get("lease")
        if not isinstance(lease, dict) or not lease.get("valid"):
            health_reasons.append("the AT-01 runtime lease is not valid")
        if last_run and last_run.get("status") == "failed":
            health_reasons.append(
                f"the last decision run failed: {last_run.get('error_code') or 'unknown'}"
            )
        if last_run and int(last_run.get("errors") or 0) > 0:
            health_reasons.append(
                f"the last decision run recorded {last_run.get('errors')} error(s)"
            )
        last_cycle = self._autonomy_cycle()
        if last_cycle and last_cycle.get("status") != "completed":
            health_reasons.append(
                f"the latest AT-02 monitoring cycle is {last_cycle.get('status')}"
            )
        return {
            "ok": True,
            "initialised": True,
            "phase": "AT-03",
            "milestone": "AT-03",
            "implemented": True,
            "version": DECISION_CONTRACT_VERSION,
            "paper_only": True,
            "live_trading": False,
            "broker_connected": False,
            "execution_implemented": False,
            "orders_possible": False,
            "risk_authority": "NM-08",
            "execution_authority": "NM-10",
            "runtime_mode": runtime_status.get("mode"),
            "runtime_state": runtime_status.get("runtime_state"),
            "lease_valid": bool(
                isinstance(runtime_status.get("lease"), dict)
                and runtime_status["lease"].get("valid")
            ),
            "monitoring_cycle_id": latest_cycle_id,
            "monitoring_cycle": cycle,
            "metrics": metrics,
            "last_run": last_run,
            "recent_runs": runs,
            "recent_proposals": recent_proposals,
            "counts": {
                "runs": metrics["runs"],
                "evaluations": metrics["evaluations"],
                "signals": metrics["signals"],
                "proposals": metrics["proposals"],
                "risk_assessments": metrics["risk_assessments"],
                "prediction_decisions": metrics.get("prediction_decisions", 0),
                "prediction_outcomes": metrics.get("prediction_outcomes", 0),
            },
            "health": {
                "state": "degraded" if health_reasons else "ok",
                "reasons": health_reasons,
                "last_error": self._last_error,
                "last_error_code": self._last_error_code,
            },
            "last_error": self._last_error,
            "last_error_code": self._last_error_code,
            "advisory_evidence": {
                "reader": self._advisory_evidence is not None,
                "source": "PI-17 advisory handoff (read-only)",
                "read_only": True,
                "affects_action": False,
                "last_error": self._advisory_error,
            },
            "forecast_optimization_evidence": {
                "reader": self._forecast_optimization_evidence is not None,
                "source": "PI-23 forecast optimization evidence (read-only)",
                "read_only": True,
                "affects_action": False,
                "last_error": self._forecast_optimization_error,
            },
            "controls": ["run"],
            "scheduler": {
                "hosted_by": "novamarket_runtime",
                "overlapping_runs": False,
            },
        }

    def runs(self, *, limit: int = 50) -> dict[str, Any]:
        rows = self._store.runs(limit=limit)
        return {"ok": True, "runs": rows, "total": len(rows)}

    def run_detail(self, run_id: str) -> dict[str, Any]:
        run = self._store.run(run_id)
        signals = self._store.signals(run_id=run_id, limit=500)
        proposals = self._store.proposals(run_id=run_id, limit=500)
        evaluations = self._store.evaluations(run_id)
        return {
            "ok": True,
            "run": run,
            "evaluations": evaluations,
            "signals": signals,
            "proposals": proposals,
            "prediction_decisions": self._store.prediction_decisions(
                run_id=run_id, limit=500
            )["items"],
            "events": self._store.events(run_id=run_id, limit=500),
            "execution_allowed": False,
            "orders_possible": False,
        }

    def signals(
        self,
        *,
        run_id: str = "",
        strategy_id: str = "",
        portfolio_id: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        rows = self._store.signals(
            run_id=run_id,
            strategy_id=strategy_id,
            portfolio_id=portfolio_id,
            limit=limit,
        )
        return {"ok": True, "signals": rows, "total": len(rows), "orders_possible": False}

    def proposals(
        self,
        *,
        run_id: str = "",
        portfolio_id: str = "",
        instrument_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        rows = self._store.proposals(
            run_id=run_id,
            portfolio_id=portfolio_id,
            instrument_id=instrument_id,
            status=status,
            limit=limit,
        )
        return {
            "ok": True,
            "proposals": rows,
            "total": len(rows),
            "execution_allowed": False,
            "orders_possible": False,
        }

    def proposal_detail(self, proposal_id: str) -> dict[str, Any]:
        proposal = self._store.proposal(proposal_id)
        if proposal is None:
            raise DecisionNotFound(f"unknown decision proposal {proposal_id!r}")
        signal = self._store.signal(str(proposal.get("signal_id") or ""))
        evaluation: dict[str, Any] | None = None
        if signal is not None:
            evaluation = self._store.evaluation(
                str(signal.get("evaluation_id") or "")
            )
        risk = self._store.risk_assessment(proposal_id)
        prediction_decision = self._store.prediction_decision_for_proposal(proposal_id)
        return {
            "ok": True,
            "proposal": proposal,
            "signal": signal,
            "evaluation": evaluation,
            "risk": risk,
            "prediction_decision": prediction_decision,
            "prediction_outcome": (
                self._store.prediction_outcome(
                    str(prediction_decision.get("decision_id"))
                )
                if prediction_decision
                else None
            ),
            "events": self._store.events(proposal_id=proposal_id, limit=200),
            "execution_allowed": False,
            "orders_possible": False,
            "is_executed": False,
        }

    # -- PI-12 forecast decision reads ---------------------------------------
    def forecast_config(self) -> ForecastDecisionConfig:
        if isinstance(self._forecast_config_override, ForecastDecisionConfig):
            return self._forecast_config_override
        if isinstance(self._forecast_config_override, Mapping):
            return ForecastDecisionConfig.from_mapping(self._forecast_config_override)
        stored = self._store.forecast_decision_policy()
        if stored is not None:
            try:
                return ForecastDecisionConfig.from_mapping(stored)
            except ForecastDecisionConfigError:
                _LOGGER.warning(
                    "novamarket.decision.invalid_forecast_policy; using defaults"
                )
        return ForecastDecisionConfig()

    def forecast_policy(self) -> dict[str, Any]:
        config = self.forecast_config()
        stored = self._store.forecast_decision_policy()
        return {
            "ok": True,
            "policy": config.to_dict(),
            "digest": config.digest(),
            "source": "stored" if stored is not None else "default",
            "updated_at": (stored or {}).get("updated_at"),
            "risk_authority": "NM-08",
            "execution_authority": "NM-10",
        }

    def set_forecast_policy(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        config = ForecastDecisionConfig.from_mapping(payload)
        saved = self._store.save_forecast_decision_policy(
            config.to_dict(), at=self._now()
        )
        self._forecast_config_override = config
        return {
            "ok": True,
            "policy": config.to_dict(),
            "digest": config.digest(),
            "updated_at": saved.get("updated_at"),
            "risk_authority": "NM-08",
            "execution_authority": "NM-10",
        }

    def prediction_decisions(
        self,
        *,
        run_id: str = "",
        portfolio_id: str = "",
        instrument_id: str = "",
        decision_type: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        body = self._store.prediction_decisions(
            run_id=run_id,
            portfolio_id=portfolio_id,
            instrument_id=instrument_id,
            decision_type=decision_type,
            limit=limit,
            offset=offset,
        )
        return {"ok": True, **body, "decisions": body["items"]}

    def prediction_decision_detail(self, decision_id: str) -> dict[str, Any]:
        decision = self._store.prediction_decision(decision_id)
        if decision is None:
            raise DecisionNotFound(f"unknown prediction decision {decision_id!r}")
        proposal = self._store.proposal(str(decision.get("proposal_id") or ""))
        signal = self._store.signal(str((proposal or {}).get("signal_id") or ""))
        risk = (
            self._store.risk_assessment(str((proposal or {}).get("proposal_id") or ""))
            if proposal
            else None
        )
        return {
            "ok": True,
            "decision": decision,
            "proposal": proposal,
            "signal": signal,
            "risk": risk,
            "outcome": self._store.prediction_outcome(decision_id),
            "events": decision.get("events") or [],
            "execution_allowed": False,
            "orders_possible": False,
            "paper_only": True,
            "live_trading": False,
        }

    def prediction_outcomes(
        self, *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        body = self._store.prediction_outcomes(limit=limit, offset=offset)
        return {"ok": True, **body, "outcomes": body["items"]}

    def execution_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        """The ADDITIVE NM-10 adapter view; persisted AT-03 state is untouched.

        NM-10 validates ``status == approved``.  AT-03 intentionally persists
        ``risk_approved`` as its own decision state, so this view normalizes
        only a proposal whose persisted NM-08 outcome is approved.  It is not a
        second proposal store and it never changes a proposal's lifecycle.
        """

        proposal = self._store.proposal(proposal_id)
        if proposal is None:
            return None
        body = dict(proposal)
        persisted_status = str(body.get("status") or "")
        body["decision_status"] = persisted_status
        risk = self._store.risk_assessment(proposal_id)
        if (
            persisted_status == DecisionProposalStatus.RISK_APPROVED.value
            and str(body.get("risk_outcome") or "").lower() == "approved"
            and isinstance(risk, Mapping)
            and str(risk.get("outcome") or "").lower() == "approved"
            and str(risk.get("proposal_id") or "") == proposal_id
            and str(risk.get("risk_decision_id") or "")
            == str(body.get("risk_decision_id") or "")
        ):
            body["status"] = "approved"
            body["execution_allowed"] = True
        return body

    # -- runtime / cycle validation ------------------------------------------
    def _autonomy_cycle(self) -> dict[str, Any] | None:
        return self._store.autonomy.latest_monitoring_cycle()

    def _latest_completed_cycle(self) -> dict[str, Any] | None:
        for row in self._store.autonomy.monitoring_cycles(limit=50):
            if str(row.get("status")) == "completed":
                return dict(row)
        return None

    def _assert_runtime_ready(self) -> dict[str, Any]:
        if self._closed:
            raise DecisionStateError(
                DecisionErrorCode.RUNTIME_DISABLED.value,
                "the decision pipeline is closed",
            )
        status = self._runtime.status()
        mode = str(status.get("mode") or "disabled")
        if mode == "paused":
            raise DecisionStateError(
                DecisionErrorCode.RUNTIME_PAUSED.value,
                "the runtime is paused; no new entry-oriented decision may run",
            )
        if mode not in {"monitoring", "autonomous"}:
            raise DecisionStateError(
                DecisionErrorCode.RUNTIME_DISABLED.value,
                "the runtime is disabled; start monitoring or autonomous mode first",
            )
        lease = status.get("lease")
        if not isinstance(lease, dict) or not lease.get("valid"):
            raise DecisionStateError(
                DecisionErrorCode.LEASE_INVALID.value,
                "the runtime lease is not valid; this runtime may not decide",
            )
        if lease.get("conflict"):
            raise DecisionStateError(
                DecisionErrorCode.LEASE_INVALID.value,
                "another runtime owns the AT-01 lease",
            )
        return status

    def _cycle_for(
        self, monitoring_cycle_id: str = ""
    ) -> dict[str, Any]:
        if monitoring_cycle_id:
            cycle = self._store.autonomy.monitoring_cycle(monitoring_cycle_id)
            if cycle is None:
                raise DecisionStateError(
                    DecisionErrorCode.MONITORING_CYCLE_REQUIRED.value,
                    f"unknown monitoring cycle {monitoring_cycle_id!r}",
                )
        else:
            cycle = self._latest_completed_cycle()
            if cycle is None:
                raise DecisionStateError(
                    DecisionErrorCode.MONITORING_CYCLE_REQUIRED.value,
                    "no completed AT-02 monitoring cycle is available",
                )
        cycle = dict(cycle)
        if str(cycle.get("status")) != "completed":
            raise DecisionStateError(
                DecisionErrorCode.MONITORING_CYCLE_INCOMPLETE.value,
                f"monitoring cycle {cycle.get('cycle_id')} is {cycle.get('status')}",
            )
        if not bool(cycle.get("session_eligible")):
            raise DecisionStateError(
                DecisionErrorCode.MONITORING_CYCLE_NOT_ELIGIBLE.value,
                f"monitoring cycle {cycle.get('cycle_id')} was not session-eligible",
            )
        if not bool(cycle.get("calendar_verified")):
            raise DecisionStateError(
                DecisionErrorCode.MONITORING_CYCLE_NOT_ELIGIBLE.value,
                f"monitoring cycle {cycle.get('cycle_id')} has no verified calendar",
            )
        return cycle

    # -- the run --------------------------------------------------------------
    async def run_cycle(
        self,
        *,
        monitoring_cycle_id: str = "",
        trigger: str = "manual",
        actor: str = "",
    ) -> dict[str, Any]:
        if self._lock.locked():
            raise DecisionStateError(
                DecisionErrorCode.ALREADY_RUNNING.value,
                "a decision run is already active",
            )
        async with self._lock:
            runtime_status = self._assert_runtime_ready()
            if trigger not in {"manual", "scheduled"}:
                raise DecisionValidationError("trigger must be manual or scheduled")
            cycle = self._cycle_for(monitoring_cycle_id)
            cycle_id = str(cycle.get("cycle_id") or "")
            candidates = self._candidates(cycle_id)
            if not candidates:
                raise DecisionStateError(
                    DecisionErrorCode.NO_ELIGIBLE_CANDIDATES.value,
                    f"monitoring cycle {cycle_id} has no processed eligible candidates",
                )
            config = self._store.autonomy.config()
            portfolio_ids = list(config.portfolio_ids)
            if not portfolio_ids:
                listed = self._portfolios.portfolios(status="active", limit=50)
                portfolio_ids = [
                    str(row.get("portfolio_id"))
                    for row in listed.get("portfolios") or ()
                    if isinstance(row, dict) and row.get("portfolio_id")
                ]
            portfolios = self._eligible_portfolios(portfolio_ids)
            if not portfolios:
                raise DecisionStateError(
                    DecisionErrorCode.NO_PORTFOLIO_CONFIGURED.value,
                    "no active configured portfolio is eligible for decisions",
                )
            active_strategies = self._active_strategies()
            if not active_strategies and self._forecasts is None:
                raise DecisionStateError(
                    DecisionErrorCode.NO_ACTIVE_STRATEGY.value,
                    "no active NM-09 strategy applies to the configured horizon",
                )

            started = self._now()
            selection_key = digest_payload(
                {
                    "cycle": cycle_id,
                    "candidates": [row["instrument_id"] for row in candidates],
                    "portfolios": [row["portfolio_id"] for row in portfolios],
                    "strategies": [row["strategy_id"] for row in active_strategies],
                    "prediction_integration": self._forecasts is not None,
                }
            )
            run = DecisionRun(
                run_id=decision_id("drun_", selection_key, started.isoformat(), uuid4().hex),
                selection_key=selection_key,
                monitoring_cycle_id=cycle_id,
                mode=str(runtime_status.get("mode") or ""),
                status=DecisionRunStatus.RUNNING,
                trigger=trigger,
                portfolio_ids=tuple(row["portfolio_id"] for row in portfolios),
                strategy_ids=tuple(row["strategy_id"] for row in active_strategies),
                instrument_ids=tuple(row["instrument_id"] for row in candidates),
                evaluations=0,
                signals=0,
                proposals=0,
                approved=0,
                rejected=0,
                invalid=0,
                errors=0,
                started_at=started,
                finished_at=None,
                error_code="",
                error="",
                actor=actor,
            )
            self._last_error = ""
            self._last_error_code = ""
            self._store.create_run(run)
            self._event(
                run.run_id,
                "run.started",
                f"Decision run started from monitoring cycle {cycle_id}",
                monitoring_cycle_id=cycle_id,
                trigger=trigger,
                mode=run.mode,
            )
            counts = {
                "evaluations": 0,
                "signals": 0,
                "proposals": 0,
                "approved": 0,
                "rejected": 0,
                "invalid": 0,
                "errors": 0,
                "prediction_decisions": 0,
                "prediction_buy": 0,
                "prediction_sell": 0,
                "prediction_hold": 0,
                "prediction_wait": 0,
                "prediction_no_action": 0,
                "prediction_skipped": 0,
            }
            failure_code = ""
            failure_message = ""
            strategy_skip: set[tuple[str, str]] = set()
            try:
                if self._forecasts is not None:
                    strategy_skip = await self._evaluate_forecasts(
                        run, cycle, portfolios, candidates, counts, actor=actor
                    )
                for portfolio in portfolios:
                    for strategy in active_strategies:
                        if portfolio["horizon"] not in strategy["horizons"]:
                            continue
                        try:
                            await self._evaluate_strategy(
                                run,
                                cycle,
                                portfolio,
                                strategy,
                                candidates,
                                counts,
                                actor=actor,
                                skip={
                                    instrument
                                    for portfolio_id, instrument in strategy_skip
                                    if portfolio_id == portfolio["portfolio_id"]
                                },
                            )
                        except DecisionError as exc:
                            counts["errors"] += 1
                            failure_code = failure_code or exc.code
                            failure_message = failure_message or exc.message
                            self._event(
                                run.run_id,
                                "strategy.failed",
                                exc.message,
                                code=exc.code,
                                strategy_id=strategy["strategy_id"],
                                portfolio_id=portfolio["portfolio_id"],
                            )
                        except Exception as exc:  # noqa: BLE001 - isolate one strategy
                            counts["errors"] += 1
                            failure_code = (
                                failure_code
                                or DecisionErrorCode.STRATEGY_EVALUATION_FAILED.value
                            )
                            failure_message = failure_message or str(exc)
                            _LOGGER.exception(
                                "novamarket.decision.strategy_failed run=%s strategy=%s",
                                run.run_id,
                                strategy["strategy_id"],
                            )
                            self._event(
                                run.run_id,
                                "strategy.failed",
                                f"{type(exc).__name__}: {exc}",
                                code=DecisionErrorCode.STRATEGY_EVALUATION_FAILED.value,
                                strategy_id=strategy["strategy_id"],
                                portfolio_id=portfolio["portfolio_id"],
                            )
            except Exception as exc:  # noqa: BLE001 - never report a failed run as complete
                counts["errors"] += 1
                failure_code = DecisionErrorCode.PIPELINE_FAILED.value
                failure_message = f"{type(exc).__name__}: {exc}"
                _LOGGER.exception("novamarket.decision.run_failed run=%s", run.run_id)

            if counts["errors"] and not failure_code:
                failure_code = (
                    self._last_error_code
                    or DecisionErrorCode.RISK_ASSESSMENT_FAILED.value
                )
                failure_message = self._last_error or (
                    "the decision run recorded a risk assessment failure"
                )
            finished = replace(
                run,
                status=(
                    DecisionRunStatus.FAILED
                    if failure_code
                    else DecisionRunStatus.COMPLETED
                ),
                finished_at=self._now(),
                evaluations=counts["evaluations"],
                signals=counts["signals"],
                proposals=counts["proposals"],
                approved=counts["approved"],
                rejected=counts["rejected"],
                invalid=counts["invalid"],
                errors=counts["errors"],
                error_code=failure_code,
                error=failure_message,
            )
            self._store.update_run(finished)
            self._event(
                run.run_id,
                "run.failed" if failure_code else "run.completed",
                failure_message or "Decision run completed without execution",
                error_code=failure_code,
                proposals=counts["proposals"],
                approved=counts["approved"],
                rejected=counts["rejected"],
                invalid=counts["invalid"],
                errors=counts["errors"],
            )
            if failure_code:
                self._last_error_code = failure_code
                self._last_error = failure_message
                raise DecisionStateError(
                    failure_code,
                    failure_message or "the decision run failed",
                ) from None
            body = finished.to_dict()
            body.update(
                {
                    "ok": True,
                    "execution_allowed": False,
                    "orders_possible": False,
                    "paper_only": True,
                    "live_trading": False,
                    "prediction_decisions": counts["prediction_decisions"],
                    "prediction_decisions_by_type": {
                        "BUY": counts["prediction_buy"],
                        "SELL": counts["prediction_sell"],
                        "HOLD": counts["prediction_hold"],
                        "WAIT": counts["prediction_wait"],
                        "NO_ACTION": counts["prediction_no_action"],
                    },
                    "prediction_skipped_existing": counts["prediction_skipped"],
                }
            )
            return body

    async def maybe_run(self) -> dict[str, Any]:
        """Scheduler seam: one attempt per completed monitoring cycle."""

        if self._closed:
            return {"status": "skipped", "reason": "closed"}
        if self._lock.locked():
            return {"status": "skipped", "reason": "already_running"}
        cycle = self._latest_completed_cycle()
        if cycle is None:
            return {"status": "skipped", "reason": "no_completed_cycle"}
        cycle_id = str(cycle.get("cycle_id") or "")
        existing = self._store.runs_for_cycle(cycle_id)
        if existing:
            return {
                "status": "skipped",
                "reason": "cycle_already_processed",
                "monitoring_cycle_id": cycle_id,
                "run_id": existing[0].get("run_id"),
            }
        try:
            body = await self.run_cycle(
                monitoring_cycle_id=cycle_id,
                trigger="scheduled",
                actor="novamarket-runtime",
            )
        except DecisionError as exc:
            return {
                "status": "refused",
                "reason": exc.code,
                "message": exc.message,
                "monitoring_cycle_id": cycle_id,
            }
        return {"status": "run", "monitoring_cycle_id": cycle_id, "run": body}

    # -- candidate / portfolio / strategy selection --------------------------
    def _candidates(self, cycle_id: str) -> list[dict[str, Any]]:
        rows = self._store.autonomy.monitoring_candidates(cycle_id)
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            if str(row.get("disposition") or "") != "processed":
                continue
            instrument_id = _text(row.get("instrument_id"))
            if not instrument_id or instrument_id in seen:
                continue
            exchange = _text(row.get("exchange"))
            if not exchange:
                exchange = instrument_id.split(":", 1)[0] if ":" in instrument_id else ""
            if ":" not in instrument_id or not exchange:
                continue
            if instrument_id.split(":", 1)[0] != exchange:
                continue
            if not self._instrument_supported(instrument_id):
                continue
            body = dict(row)
            body["exchange"] = exchange
            body["instrument_id"] = instrument_id
            selected.append(body)
            seen.add(instrument_id)
            if len(selected) >= MAX_INSTRUMENTS_PER_RUN:
                break
        return selected

    def _instrument_supported(self, instrument_id: str) -> bool:
        if self._market_data is None:
            return True
        lookup = getattr(self._market_data, "get_instrument", None)
        if lookup is None:
            return True
        try:
            found = lookup(instrument_id)
        except Exception:  # noqa: BLE001 - an unavailable instrument fails closed
            return False
        return found is not None

    def _eligible_portfolios(
        self, portfolio_ids: Sequence[str]
    ) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        for portfolio_id in portfolio_ids:
            try:
                detail = self._portfolios.detail(portfolio_id)
            except Exception as exc:  # noqa: BLE001 - one missing book cannot decide
                self._event(
                    "",
                    "portfolio.ineligible",
                    f"{portfolio_id}: {type(exc).__name__}: {exc}",
                    portfolio_id=portfolio_id,
                )
                continue
            portfolio = detail.get("portfolio") if isinstance(detail, dict) else None
            if not isinstance(portfolio, dict):
                continue
            if str(portfolio.get("status") or "").lower() != "active":
                continue
            horizon = str(portfolio.get("horizon") or "")
            if horizon not in HORIZON_MAPPING:
                continue
            valuation = detail.get("valuation")
            total = _as_decimal(
                (valuation or {}).get("total_value") if isinstance(valuation, dict) else None
            )
            if total is None or total <= 0:
                continue
            selected.append(
                {
                    "portfolio_id": portfolio_id,
                    "horizon": horizon,
                    "portfolio": portfolio,
                    "detail": detail,
                    "total_value": total,
                }
            )
        return selected

    def _active_strategies(self) -> list[dict[str, Any]]:
        body = self._strategies.strategies(status="active", limit=50)
        rows = body.get("strategies") if isinstance(body, dict) else None
        selected: list[dict[str, Any]] = []
        for row in rows or ():
            if not isinstance(row, dict):
                continue
            horizons = [str(item) for item in row.get("horizons") or ()]
            for horizon in horizons:
                if horizon in HORIZON_MAPPING:
                    selected.append(
                        {
                            "strategy_id": str(row.get("strategy_id") or ""),
                            "strategy": row,
                            "horizons": tuple(
                                item for item in horizons if item in HORIZON_MAPPING
                            ),
                        }
                    )
                    break
            if len(selected) >= MAX_STRATEGIES_PER_RUN:
                break
        return selected

    # -- PI-12 forecast evaluation -------------------------------------------
    async def _evaluate_forecasts(
        self,
        run: DecisionRun,
        cycle: Mapping[str, Any],
        portfolios: Sequence[Mapping[str, Any]],
        candidates: Sequence[Mapping[str, Any]],
        counts: dict[str, int],
        *,
        actor: str,
    ) -> set[tuple[str, str]]:
        config = self.forecast_config()
        evaluator = ForecastDecisionEvaluator(config)
        skip: set[tuple[str, str]] = set()
        for portfolio in portfolios:
            portfolio_id = str(portfolio["portfolio_id"])
            for candidate in candidates:
                instrument_id = str(candidate["instrument_id"])
                exchange = str(candidate["exchange"])
                quality = _text(candidate.get("data_quality")) or "unavailable"
                forecast_error = ""
                try:
                    forecasts = self._forecast_rows(instrument_id)
                except Exception as exc:  # noqa: BLE001 - required evidence fails closed
                    forecasts = []
                    forecast_error = f"{type(exc).__name__}: {exc}"
                orders, order_error = self._orders_for(portfolio_id, instrument_id)
                if forecast_error or order_error:
                    message = (
                        "required forecast evidence is unavailable; no trade is allowed: "
                        f"{forecast_error}"
                        if forecast_error
                        else "open-order state is unavailable; no trade is allowed: "
                        f"{order_error}"
                    )
                    self._persist_forecast_error(
                        run,
                        cycle,
                        portfolio,
                        candidate,
                        config,
                        counts,
                        skip,
                        message,
                    )
                    raise DecisionPipelineError(
                        DecisionErrorCode.PIPELINE_FAILED.value, message
                    )
                try:
                    evaluation = self._forecast_evaluation(
                        instrument_id, ""
                    )
                except Exception as exc:  # noqa: BLE001 - evidence is optional
                    evaluation = {
                        "available": False,
                        "reason": f"PI-11 evidence unavailable: {type(exc).__name__}: {exc}",
                        "automatic_promotion": False,
                    }
                try:
                    bars = self._forecast_bars(instrument_id)
                    regime = self._forecast_regime(instrument_id)
                    events = self._forecast_events(instrument_id)
                    draft = evaluator.evaluate(
                        instrument_id=instrument_id,
                        exchange=exchange,
                        portfolio=portfolio,
                        forecasts=forecasts,
                        bars=bars,
                        regime=regime,
                        events=events,
                        open_orders=orders,
                        evaluation_evidence=evaluation,
                        candidate_quality=quality,
                        now=self._now(),
                    )
                    await self._persist_forecast_decision(
                        run,
                        cycle,
                        portfolio,
                        candidate,
                        draft,
                        config,
                        counts,
                        skip,
                        actor=actor,
                        quality=quality,
                    )
                except Exception as exc:  # noqa: BLE001 - isolate candidate evaluation
                    _LOGGER.exception(
                        "novamarket.decision.forecast_failed run=%s instrument=%s",
                        run.run_id,
                        instrument_id,
                    )
                    self._persist_forecast_error(
                        run,
                        cycle,
                        portfolio,
                        candidate,
                        config,
                        counts,
                        skip,
                        f"{type(exc).__name__}: {exc}",
                    )
        return skip

    def _forecast_rows(self, instrument_id: str) -> list[dict[str, Any]]:
        if self._forecasts is None:
            return []
        body = self._forecasts.forecasts(instrument_id)
        rows = body.get("forecasts") if isinstance(body, Mapping) else body
        return [dict(row) for row in rows or () if isinstance(row, Mapping)]

    def _forecast_evaluation(
        self, instrument_id: str, horizon: str
    ) -> dict[str, Any]:
        if self._forecasts is not None and callable(
            getattr(self._forecasts, "evaluation_evidence", None)
        ):
            return dict(self._forecasts.evaluation_evidence(instrument_id, horizon))
        if self._evaluations is not None:
            body = self._evaluations.list(limit=20)
            return {
                "available": bool(body.get("evaluations")),
                "reports": body.get("evaluations") or [],
                "automatic_promotion": False,
            }
        return {"available": False, "automatic_promotion": False}

    def _forecast_bars(self, instrument_id: str) -> list[Any]:
        if self._market_data is None:
            return []
        try:
            page = self._market_data.bars(
                instrument_id, limit=60, ascending=True
            )
        except Exception:  # noqa: BLE001 - an empty context is explicit
            return []
        if isinstance(page, Mapping):
            rows = page.get("bars") or page.get("items") or ()
        else:
            rows = getattr(page, "items", ()) or ()
        # A partial session bar is not a completed observation.  It may be
        # useful to an intraday forecast, but it must not become the technical
        # confirmation for a swing/long-term decision.
        return [
            row
            for row in rows
            if not (
                bool(row.get("partial"))
                if isinstance(row, Mapping)
                else bool(getattr(row, "partial", False))
            )
        ]

    def _forecast_regime(self, instrument_id: str) -> Any:
        if self._features is None:
            return None
        try:
            return self._features.latest_regime(instrument_id)
        except Exception:  # noqa: BLE001
            return None

    def _forecast_events(self, instrument_id: str) -> list[Mapping[str, Any]]:
        if self._radar is None:
            return []
        try:
            body = self._radar.instrument_events(instrument_id, limit=20)
        except Exception:  # noqa: BLE001
            return []
        rows = body.get("events") if isinstance(body, Mapping) else body
        return [row for row in rows or () if isinstance(row, Mapping)]

    def _orders_for(
        self, portfolio_id: str, instrument_id: str
    ) -> tuple[list[Mapping[str, Any]], str]:
        if self._open_orders is None:
            return [], ""
        try:
            rows = self._open_orders(portfolio_id, instrument_id)
            if rows is None:
                return [], "the open-order provider returned no readable state"
            return [row for row in rows if isinstance(row, Mapping)], ""
        except Exception as exc:  # noqa: BLE001 - unknown orders must fail closed
            return [], f"{type(exc).__name__}: {exc}"

    def _forecast_signal(
        self,
        run: DecisionRun,
        portfolio: Mapping[str, Any],
        candidate: Mapping[str, Any],
        prediction_decision_id: str,
        draft: Any,
        config: ForecastDecisionConfig,
    ) -> DecisionSignal:
        now = self._now()
        primary = dict(draft.primary_forecast or {})
        target = _as_datetime(primary.get("target_at")) or (
            now + timedelta(minutes=config.decision_ttl_minutes)
        )
        ttl = now + timedelta(minutes=config.decision_ttl_minutes)
        expires_at = min(target, ttl) if target > now else ttl
        forecast_ids = tuple(
            dict.fromkeys(
                str(row.get("forecast_id") or "")
                for row in draft.forecasts
                if row.get("forecast_id")
            )
        )
        models = tuple(
            dict.fromkeys(
                item
                for item in [
                    _text(primary.get("model_version")),
                    *[str(value) for value in primary.get("member_versions") or ()],
                ]
                if item
            )
        )
        confidence = (
            draft.probability_up
            if draft.side == "buy"
            else draft.probability_down
        )
        rationale = "PI-12 forecast decision: " + "; ".join(draft.reasons[:5])
        return DecisionSignal(
            signal_id=decision_id("psig_", prediction_decision_id),
            evaluation_id="",
            run_id=run.run_id,
            monitoring_cycle_id=run.monitoring_cycle_id,
            strategy_id="pi12_forecast",
            strategy_version=config.version,
            portfolio_id=str(portfolio["portfolio_id"]),
            instrument_id=str(candidate["instrument_id"]),
            exchange=str(candidate["exchange"]),
            horizon=str(portfolio["horizon"]),
            direction=draft.side,
            created_at=now,
            source_market_data_at=_as_datetime(primary.get("data_observed_at"))
            or now,
            expires_at=expires_at,
            rationale=rationale,
            confidence=str(confidence) if confidence is not None else None,
            reference_price=draft.reference_price or None,
            data_quality="measured",
            freshness="fresh",
            validation=SignalValidation.VALID,
            validation_error="",
            conditions=tuple(
                [
                    "pi12_forecast",
                    "forecast_authorizes_action:false",
                    "risk_authority:NM-08",
                    "execution_authority:NM-10",
                    f"prediction_decision:{prediction_decision_id}",
                    f"forecast_horizon:{draft.horizon}",
                    *[f"forecast:{item}" for item in forecast_ids],
                ]
            ),
            prediction_decision_id=prediction_decision_id,
            forecast_ids=forecast_ids,
            forecast_horizon=draft.horizon,
            ensemble_version=_text(primary.get("ensemble_id")),
            model_versions=models,
        )

    async def _persist_forecast_decision(
        self,
        run: DecisionRun,
        cycle: Mapping[str, Any],
        portfolio: Mapping[str, Any],
        candidate: Mapping[str, Any],
        draft: Any,
        config: ForecastDecisionConfig,
        counts: dict[str, int],
        skip: set[tuple[str, str]],
        *,
        actor: str,
        quality: str,
    ) -> None:
        instrument_id = str(candidate["instrument_id"])
        portfolio_id = str(portfolio["portfolio_id"])
        primary = dict(draft.primary_forecast or {})
        forecast_id = _text(primary.get("forecast_id")) or "none"
        key_payload = {
            "cycle": run.monitoring_cycle_id,
            "portfolio": portfolio_id,
            "instrument": instrument_id,
            "horizon": draft.horizon or "none",
            "forecast": forecast_id,
            "policy": config.digest(),
            "type": draft.decision_type.value,
        }
        decision_key = digest_payload(key_payload)
        decision_id = "pdec_" + decision_key
        existing = self._store.prediction_decision(decision_id)
        if existing is None:
            existing = self._store.prediction_decision_by_key(decision_key)
        if existing is not None:
            counts["prediction_skipped"] += 1
            skip.add((portfolio_id, instrument_id))
            return
        proposal: DecisionProposal | None = None
        status = ForecastDecisionStatus.DECISION_GENERATED.value
        signal_id = ""
        skip_reason = ""
        if draft.decision_type in {
            ForecastDecisionType.BUY,
            ForecastDecisionType.SELL,
        } and draft.action and draft.side:
            signal = self._forecast_signal(
                run, portfolio, candidate, decision_id, draft, config
            )
            signal_id = signal.signal_id
            self._store.save_signal(signal)
            counts["signals"] += 1
            proposal, created, skip_reason = self._ensure_proposal(
                run, signal, portfolio, candidate
            )
            if proposal is None:
                status = ForecastDecisionStatus.PROPOSAL_SKIPPED.value
                counts["invalid"] += 1
            else:
                if created:
                    counts["proposals"] += 1
                if proposal.status is DecisionProposalStatus.RISK_PENDING:
                    proposal = await self._assess_proposal(
                        run, proposal, counts, actor=actor
                    )
                if proposal.status is DecisionProposalStatus.RISK_APPROVED:
                    status = ForecastDecisionStatus.RISK_APPROVED.value
                    counts["approved"] += 1
                elif proposal.status is DecisionProposalStatus.RISK_PENDING:
                    status = ForecastDecisionStatus.RISK_PENDING.value
                elif proposal.status in {
                    DecisionProposalStatus.RISK_REJECTED,
                    DecisionProposalStatus.RISK_RESTRICTED,
                    DecisionProposalStatus.RISK_REVIEW,
                }:
                    counts["rejected"] += 1
                    status = {
                        DecisionProposalStatus.RISK_REJECTED: ForecastDecisionStatus.RISK_REJECTED.value,
                        DecisionProposalStatus.RISK_RESTRICTED: ForecastDecisionStatus.RISK_RESTRICTED.value,
                        DecisionProposalStatus.RISK_REVIEW: ForecastDecisionStatus.RISK_REVIEW.value,
                    }[proposal.status]
                else:
                    status = ForecastDecisionStatus.PROPOSAL_SKIPPED.value
        payload = draft.to_payload()
        payload.update(
            {
                "decision_id": decision_id,
                "decision_key": decision_key,
                "run_id": run.run_id,
                "monitoring_cycle_id": run.monitoring_cycle_id,
                "cycle_id": run.monitoring_cycle_id,
                "portfolio_id": portfolio_id,
                "instrument_id": instrument_id,
                "exchange": str(candidate["exchange"]),
                "candidate_quality": quality,
                "mode": run.mode,
                "actor": actor,
                "created_at": self._now().isoformat(),
                "status": status,
                "decision_status": status,
                "primary_forecast_id": forecast_id if forecast_id != "none" else "",
                "forecast_ids": [
                    str(row.get("forecast_id") or "")
                    for row in draft.forecasts
                    if row.get("forecast_id")
                ],
                "forecast_version": _text(primary.get("model_version")),
                "ensemble_version": _text(primary.get("ensemble_id")),
                "model_versions": [
                    str(value)
                    for value in [
                        row.get("forecast_id") and row.get("model_version")
                        for row in draft.forecasts
                    ]
                    if value
                ],
                "signal_id": signal_id,
                "proposal_id": str(proposal.proposal_id) if proposal else "",
                "order_id": "",
                "risk_decision_id": (
                    str(proposal.risk_decision_id) if proposal else ""
                ),
                "risk_outcome": str(proposal.risk_outcome) if proposal else "",
                "risk_result": (
                    self._store.risk_assessment(proposal.proposal_id)
                    if proposal
                    else None
                ),
                "proposal_skip_reason": skip_reason,
                "error": "",
                "policy": config.to_dict(),
                "policy_digest": config.digest(),
            }
        )
        payload["decision_digest"] = digest_payload(
            {
                "decision_id": decision_id,
                "decision_type": draft.decision_type.value,
                "horizon": draft.horizon,
                "forecast_ids": payload["forecast_ids"],
                "policy_digest": payload["policy_digest"],
                "reasons": list(draft.reasons),
            }
        )
        saved = self._store.save_prediction_decision(payload)
        self._store.append_prediction_decision_event(
            decision_id,
            "decision.generated",
            {
                "status": status,
                "decision_type": draft.decision_type.value,
                "proposal_id": payload["proposal_id"],
                "risk_outcome": payload["risk_outcome"],
                "error": "",
            },
            at=self._now(),
        )
        if proposal is not None:
            self._store.append_prediction_decision_event(
                decision_id,
                "proposal.created",
                {
                    "status": status,
                    "proposal_id": proposal.proposal_id,
                    "risk_decision_id": proposal.risk_decision_id,
                    "risk_outcome": proposal.risk_outcome,
                },
                at=self._now(),
            )
        counts["prediction_decisions"] += 1
        counter = {
            ForecastDecisionType.BUY: "prediction_buy",
            ForecastDecisionType.SELL: "prediction_sell",
            ForecastDecisionType.HOLD: "prediction_hold",
            ForecastDecisionType.WAIT: "prediction_wait",
            ForecastDecisionType.NO_ACTION: "prediction_no_action",
        }[draft.decision_type]
        counts[counter] += 1
        # A candidate with stored forecast evidence is owned by PI-12, including
        # NO_ACTION (for example a downward forecast with no long position) so an
        # independent strategy cannot silently trade against the recorded
        # forecast. With no selected forecast at all, existing NM-09 strategy
        # evaluation remains available, preserving prior autonomous behaviour.
        if (
            draft.decision_type is not ForecastDecisionType.NO_ACTION
            or draft.horizon_evidence
        ):
            skip.add((portfolio_id, instrument_id))
        self._event(
            run.run_id,
            f"prediction.{draft.decision_type.value.lower()}",
            draft.reasons[0] if draft.reasons else "forecast decision recorded",
            decision_id=decision_id,
            instrument_id=instrument_id,
            portfolio_id=portfolio_id,
            horizon=draft.horizon,
            status=status,
            proposal_id=str(saved.get("proposal_id") or ""),
        )

    def _persist_forecast_error(
        self,
        run: DecisionRun,
        cycle: Mapping[str, Any],
        portfolio: Mapping[str, Any],
        candidate: Mapping[str, Any],
        config: ForecastDecisionConfig,
        counts: dict[str, int],
        skip: set[tuple[str, str]],
        message: str,
    ) -> None:
        portfolio_id = str(portfolio["portfolio_id"])
        instrument_id = str(candidate["instrument_id"])
        key = digest_payload(
            {
                "cycle": run.monitoring_cycle_id,
                "portfolio": portfolio_id,
                "instrument": instrument_id,
                "error": message,
            }
        )
        decision_id = "pdec_" + key
        if self._store.prediction_decision(decision_id) is None:
            payload = {
                "decision_id": decision_id,
                "decision_key": key,
                "run_id": run.run_id,
                "monitoring_cycle_id": run.monitoring_cycle_id,
                "cycle_id": run.monitoring_cycle_id,
                "portfolio_id": portfolio_id,
                "instrument_id": instrument_id,
                "exchange": str(candidate["exchange"]),
                "decision_type": ForecastDecisionType.WAIT.value,
                "horizon": "",
                "primary_forecast_id": "",
                "status": ForecastDecisionStatus.DECISION_ERROR.value,
                "created_at": self._now().isoformat(),
                "reasons": ["the forecast decision could not be evaluated; no trade is allowed"],
                "contradictions": [message],
                "observations": [],
                "model_conclusions": [],
                "forecast_ids": [],
                "signal_id": "",
                "proposal_id": "",
                "order_id": "",
                "error": message,
                "policy": config.to_dict(),
                "policy_digest": config.digest(),
                "risk_authority": "NM-08",
                "execution_authority": "NM-10",
                "automatic_promotion": False,
            }
            self._store.save_prediction_decision(payload)
            self._store.append_prediction_decision_event(
                decision_id,
                "decision.error",
                {"status": ForecastDecisionStatus.DECISION_ERROR.value, "error": message},
                at=self._now(),
            )
            counts["prediction_decisions"] += 1
            counts["prediction_wait"] += 1
        skip.add((portfolio_id, instrument_id))
        self._event(
            run.run_id,
            "prediction.error",
            message,
            decision_id=decision_id,
            instrument_id=instrument_id,
            portfolio_id=portfolio_id,
        )

    # -- evaluation -----------------------------------------------------------
    async def _evaluate_strategy(
        self,
        run: DecisionRun,
        cycle: Mapping[str, Any],
        portfolio: Mapping[str, Any],
        strategy: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
        counts: dict[str, int],
        *,
        actor: str,
        skip: set[str] | None = None,
    ) -> None:
        strategy_id = str(strategy["strategy_id"])
        portfolio_id = str(portfolio["portfolio_id"])
        record_body = self._strategies.strategy(strategy_id)
        if isinstance(record_body, dict) and isinstance(
            record_body.get("strategy"), dict
        ):
            record = record_body["strategy"]
        else:
            record = record_body if isinstance(record_body, dict) else None
        if not isinstance(record, dict):
            raise DecisionPipelineError(
                DecisionErrorCode.STRATEGY_UNAVAILABLE.value,
                f"strategy {strategy_id} has no readable definition",
            )
        if str(record.get("status")) != "active":
            raise DecisionPipelineError(
                DecisionErrorCode.STRATEGY_UNAVAILABLE.value,
                f"strategy {strategy_id} is not active",
            )
        instrument_ids = [str(row["instrument_id"]) for row in candidates]
        result = await self._strategies.evaluate(
            strategy_id,
            portfolio_id,
            instrument_ids=instrument_ids,
            actor=actor,
        )
        returned = result.get("signals") if isinstance(result, dict) else None
        by_instrument: dict[str, dict[str, Any]] = {}
        for row in returned or ():
            if isinstance(row, dict):
                instrument = _text(row.get("instrument_id"))
                if instrument:
                    by_instrument[instrument] = dict(row)

        for candidate in candidates:
            instrument_id = str(candidate["instrument_id"])
            if skip and instrument_id in skip:
                self._event(
                    run.run_id,
                    "strategy.skipped_prediction_primary",
                    "a PI-12 forecast decision owns this candidate in this run",
                    strategy_id=strategy_id,
                    instrument_id=instrument_id,
                )
                continue
            raw = by_instrument.get(instrument_id)
            await self._evaluate_instrument(
                run,
                cycle,
                portfolio,
                strategy,
                record,
                candidate,
                raw,
                counts,
                actor=actor,
            )

    async def _evaluate_instrument(
        self,
        run: DecisionRun,
        cycle: Mapping[str, Any],
        portfolio: Mapping[str, Any],
        strategy: Mapping[str, Any],
        record: Mapping[str, Any],
        candidate: Mapping[str, Any],
        raw: Mapping[str, Any] | None,
        counts: dict[str, int],
        *,
        actor: str,
    ) -> None:
        now = self._now()
        instrument_id = str(candidate["instrument_id"])
        exchange = str(candidate["exchange"])
        portfolio_id = str(portfolio["portfolio_id"])
        horizon = str(portfolio["horizon"])
        strategy_id = str(strategy["strategy_id"])
        version = str(record.get("version") or "")
        quality = _text(candidate.get("data_quality")) or "unavailable"
        cutoff_text = (
            _text((raw or {}).get("market_data_cutoff"))
            or _text(cycle.get("finished_at"))
            or now.isoformat()
        )
        evaluation_key = digest_payload(
            {
                "cycle": cycle.get("cycle_id"),
                "strategy": strategy_id,
                "version": version,
                "portfolio": portfolio_id,
                "instrument": instrument_id,
                "cutoff": cutoff_text,
            }
        )
        evaluation_id = decision_id("deval_", evaluation_key)
        signal_id = _text((raw or {}).get("signal_id"))
        direction = _text((raw or {}).get("signal_type")) or "no_signal"
        valid, validation_error = self._validate_signal(
            raw,
            candidate,
            portfolio,
            record,
            horizon,
            instrument_id,
        )
        if raw is None:
            outcome = EvaluationOutcome.NO_ACTION
            validation = SignalValidation.NO_ACTION
            explanation = "The strategy returned no signal for this instrument."
        elif direction in {item.value for item in _NO_ACTION}:
            outcome = EvaluationOutcome.NO_ACTION
            validation = SignalValidation.NO_ACTION
            explanation = _text(raw.get("explanation")) or "The strategy returned no action."
        elif valid:
            outcome = EvaluationOutcome.SIGNAL
            validation = SignalValidation.VALID
            explanation = _text(raw.get("explanation")) or "Signal passed validation."
        else:
            outcome = EvaluationOutcome.INVALID
            validation = SignalValidation.INVALID
            explanation = validation_error or "Signal failed validation."
        evaluation_id_final = evaluation_id
        evaluation = DecisionEvaluation(
            evaluation_id=evaluation_id_final,
            evaluation_key=evaluation_key,
            run_id=run.run_id,
            monitoring_cycle_id=run.monitoring_cycle_id,
            strategy_id=strategy_id,
            strategy_version=version,
            config_digest=digest_payload(record.get("config") or {}),
            portfolio_id=portfolio_id,
            instrument_id=instrument_id,
            exchange=exchange,
            horizon=horizon,
            market_data_cutoff=cutoff_text,
            data_quality=quality,
            outcome=outcome,
            signal_id=signal_id,
            explanation=explanation,
            conditions=tuple(str(item) for item in (raw or {}).get("triggered_conditions") or ()),
            created_at=now,
        )
        self._store.save_evaluation(evaluation)
        counts["evaluations"] += 1
        self._event(
            run.run_id,
            f"evaluation.{outcome.value}",
            explanation,
            evaluation_id=evaluation_id_final,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            outcome=outcome.value,
        )
        if raw is None:
            return
        market_at = _as_datetime(raw.get("market_data_cutoff")) or now
        expires_at = _as_datetime(raw.get("expires_at")) or now
        confidence = _as_decimal(raw.get("confidence"))
        reference_price = _as_decimal(raw.get("reference_price"))
        signal = DecisionSignal(
            signal_id=signal_id,
            evaluation_id=evaluation_id_final,
            run_id=run.run_id,
            monitoring_cycle_id=run.monitoring_cycle_id,
            strategy_id=strategy_id,
            strategy_version=version,
            portfolio_id=portfolio_id,
            instrument_id=instrument_id,
            exchange=exchange,
            horizon=horizon,
            direction=direction,
            created_at=_as_datetime(raw.get("created_at")) or now,
            source_market_data_at=market_at,
            rationale=_text(raw.get("explanation")),
            expires_at=expires_at,
            confidence=str(confidence) if confidence is not None else None,
            reference_price=str(reference_price) if reference_price is not None else None,
            data_quality=_text(raw.get("data_quality")),
            freshness=_freshness(quality),
            validation=validation,
            validation_error=validation_error,
            conditions=tuple(str(item) for item in raw.get("triggered_conditions") or ()),
        )
        self._store.save_signal(signal)
        counts["signals"] += 1
        self._event(
            run.run_id,
            f"signal.{validation.value}",
            signal.rationale or explanation,
            signal_id=signal_id,
            evaluation_id=evaluation_id_final,
            validation=validation.value,
            direction=direction,
        )
        if outcome is not EvaluationOutcome.SIGNAL:
            if outcome is EvaluationOutcome.INVALID:
                counts["invalid"] += 1
            return
        if not signal_id:
            counts["invalid"] += 1
            return
        proposal, created, skip_reason = self._ensure_proposal(
            run,
            signal,
            portfolio,
            candidate,
        )
        if proposal is None:
            counts["invalid"] += 1
            self._event(
                run.run_id,
                "proposal.skipped",
                skip_reason or "the signal did not pass proposal validation",
                signal_id=signal_id,
                strategy_id=strategy_id,
                portfolio_id=portfolio_id,
                instrument_id=instrument_id,
            )
            return
        counts["proposals"] += 1 if created else 0
        if proposal.status is DecisionProposalStatus.INVALID:
            counts["invalid"] += 1
            return
        if proposal.status is DecisionProposalStatus.RISK_PENDING:
            proposal = await self._assess_proposal(run, proposal, counts, actor=actor)
        if proposal.status is DecisionProposalStatus.RISK_APPROVED:
            counts["approved"] += 1
        elif proposal.status in {
            DecisionProposalStatus.RISK_REJECTED,
            DecisionProposalStatus.RISK_RESTRICTED,
            DecisionProposalStatus.RISK_REVIEW,
        }:
            counts["rejected"] += 1

    # -- signal validation ----------------------------------------------------
    def _validate_signal(
        self,
        raw: Mapping[str, Any] | None,
        candidate: Mapping[str, Any],
        portfolio: Mapping[str, Any],
        record: Mapping[str, Any],
        horizon: str,
        instrument_id: str,
    ) -> tuple[bool, str]:
        if raw is None:
            return False, "the strategy returned no signal"
        if str(raw.get("signal_type") or "") not in {
            item.value for item in _ACTIONABLE | _NO_ACTION
        }:
            return False, DecisionErrorCode.INVALID_SIGNAL.value
        if str(raw.get("strategy_id") or "") != str(record.get("strategy_id") or ""):
            return False, "strategy provenance does not match the evaluated strategy"
        if str(raw.get("strategy_version") or "") != str(record.get("version") or ""):
            return False, "signal strategy version is not the current version"
        if str(raw.get("portfolio_id") or "") != str(portfolio.get("portfolio_id") or ""):
            return False, "signal portfolio does not match the evaluated portfolio"
        if str(raw.get("instrument_id") or "") != instrument_id:
            return False, "signal instrument does not match the candidate"
        if str(raw.get("evaluation_horizon") or "") != horizon:
            return False, DecisionErrorCode.HORIZON_MISMATCH.value
        if str(candidate.get("data_quality") or "") not in _ALLOWED_CANDIDATE_QUALITY:
            return False, DecisionErrorCode.STALE_MARKET_DATA.value
        if str(raw.get("data_quality") or "") not in _ALLOWED_QUALITY:
            return False, DecisionErrorCode.STALE_MARKET_DATA.value
        reference = _as_decimal(raw.get("reference_price"))
        if reference is None or reference <= 0:
            return False, DecisionErrorCode.MISSING_REFERENCE_PRICE.value
        created = _as_datetime(raw.get("created_at"))
        cutoff = _as_datetime(raw.get("market_data_cutoff"))
        expires = _as_datetime(raw.get("expires_at"))
        if created is None or cutoff is None or expires is None:
            return False, DecisionErrorCode.INVALID_SIGNAL.value
        now = self._now()
        if expires <= now:
            return False, "signal has expired"
        if cutoff > now + timedelta(minutes=5):
            return False, "market-data timestamp is in the future"
        if not _text(raw.get("explanation")):
            return False, "signal has no rationale"
        if str(raw.get("signal_type") or "") in {item.value for item in _ACTIONABLE}:
            if not self._instrument_supported(instrument_id):
                return False, DecisionErrorCode.INSTRUMENT_INELIGIBLE.value
        return True, ""

    # -- proposal -------------------------------------------------------------
    def _ensure_proposal(
        self,
        run: DecisionRun,
        signal: DecisionSignal,
        portfolio: Mapping[str, Any],
        candidate: Mapping[str, Any],
    ) -> tuple[DecisionProposal | None, bool, str]:
        existing = self._store.proposal_for_signal(signal.signal_id)
        if existing is not None:
            return self._proposal_from_dict(existing), False, ""
        try:
            signal_type = SignalType(signal.direction)
        except ValueError:
            return None, False, "the signal direction is not actionable"
        if signal_type not in _ACTIONABLE:
            return None, False, "the signal is not actionable"
        action = _ACTION.get(signal_type)
        side = _SIDE.get(signal_type)
        if action is None or side is None:
            return None, False, "the signal has no decision-action mapping"
        reference = _as_decimal(signal.reference_price)
        if reference is None or reference <= 0:
            return None, False, "the signal has no positive reference price"
        detail = portfolio.get("detail") if isinstance(portfolio, dict) else None
        if not isinstance(detail, dict):
            return None, False, "the portfolio detail is unavailable"
        total = _as_decimal(portfolio.get("total_value"))
        if total is None or total <= 0:
            return None, False, "the portfolio valuation is unavailable"
        holding = self._holding(detail, signal.instrument_id)
        notional: Decimal | None = None
        if signal_type is SignalType.BUY:
            if holding is None:
                try:
                    max_positions = policy_for(signal.horizon).max_positions
                except (KeyError, ValueError):
                    return None, False, "the portfolio horizon has no position policy"
                if self._open_positions(detail) >= max_positions:
                    return (
                        None,
                        False,
                        f"the horizon max_positions guard ({max_positions}) refuses a new entry",
                    )
            cash = _as_decimal(detail.get("cash"))
            if cash is None or cash <= 0:
                return None, False, "the portfolio has no cash for a new entry"
            notional = (total * Decimal("0.10")).quantize(Decimal("0.01"))
        elif signal_type is SignalType.SELL:
            if holding is None:
                return None, False, "a reduce action needs an existing holding"
            market_value = _as_decimal(holding.get("market_value"))
            if market_value is None or market_value <= 0:
                return None, False, "the existing holding has no positive market value"
            notional = (market_value * Decimal("0.50")).quantize(Decimal("0.01"))
        elif signal_type is SignalType.EXIT:
            if holding is None:
                return None, False, "a close action needs an existing holding"
            market_value = _as_decimal(holding.get("market_value"))
            if market_value is None or market_value <= 0:
                return None, False, "the existing holding has no positive market value"
            notional = market_value.quantize(Decimal("0.01"))
        if notional is None or notional <= 0:
            return None, False, "the proposal notional is not positive"
        now = self._now()
        # The source signal expiry is authoritative; a bounded AT-03 lifetime
        # prevents a stale intent from surviving a restart.
        expires_at = _as_datetime(signal.expires_at) or (
            now + timedelta(hours=PROPOSAL_TTL_HOURS)
        )
        if expires_at <= now:
            return None, False, "the signal expires before a proposal can be recorded"
        proposal_id = decision_id(
            "dprop_", signal.signal_id, action, str(notional), signal.strategy_version
        )
        advisory = self._advisory_for_signal(signal)
        optimization = self._forecast_optimization_for_signal(signal)
        proposal = DecisionProposal(
            proposal_id=proposal_id,
            run_id=run.run_id,
            signal_id=signal.signal_id,
            monitoring_cycle_id=run.monitoring_cycle_id,
            strategy_id=signal.strategy_id,
            strategy_version=signal.strategy_version,
            portfolio_id=signal.portfolio_id,
            instrument_id=signal.instrument_id,
            exchange=signal.exchange,
            horizon=signal.horizon,
            action=action,
            side=side,
            notional=notional,
            reference_price=reference,
            price_at=signal.source_market_data_at,
            created_at=now,
            expires_at=expires_at,
            reason=signal.rationale,
            status=DecisionProposalStatus.CREATED,
            data_quality=signal.data_quality,
            source_market_data_at=signal.source_market_data_at,
            updated_at=now,
            prediction_decision_id=signal.prediction_decision_id,
            forecast_ids=signal.forecast_ids,
            forecast_horizon=signal.forecast_horizon,
            ensemble_version=signal.ensemble_version,
            model_versions=signal.model_versions,
            advisory_evidence=advisory,
            forecast_optimization_evidence=optimization,
        )
        saved = self._store.save_proposal(proposal)
        if str(saved.get("status")) != DecisionProposalStatus.CREATED.value:
            return self._proposal_from_dict(saved), False, ""
        self._event(
            run.run_id,
            "proposal.created",
            f"Proposal {proposal_id} created; it is not an order",
            proposal_id=proposal_id,
            signal_id=signal.signal_id,
            action=action,
            notional=str(notional),
        )
        pending = replace(
            proposal,
            status=DecisionProposalStatus.VALIDATION_PENDING,
            updated_at=self._now(),
        )
        ensure_proposal_transition(proposal.status, pending.status)
        self._store.update_proposal(pending)
        self._event(
            run.run_id,
            "proposal.validation",
            "Proposal validation passed; submitting intent to NM-08",
            proposal_id=proposal_id,
            status=pending.status.value,
        )
        risk_pending = replace(
            pending,
            status=DecisionProposalStatus.RISK_PENDING,
            updated_at=self._now(),
        )
        ensure_proposal_transition(pending.status, risk_pending.status)
        self._store.update_proposal(risk_pending)
        return risk_pending, True, ""

    async def _assess_proposal(
        self,
        run: DecisionRun,
        proposal: DecisionProposal,
        counts: dict[str, int],
        *,
        actor: str,
    ) -> DecisionProposal:
        if proposal.status is not DecisionProposalStatus.RISK_PENDING:
            return proposal
        action = proposal.action
        try:
            if not callable(getattr(self._risk, "assess", None)):
                raise DecisionPipelineError(
                    DecisionErrorCode.RISK_ENGINE_UNAVAILABLE.value,
                    "the NM-08 risk engine does not expose assess()",
                )
            decision = await asyncio.wait_for(
                self._risk.assess(
                    action,
                    portfolio_id=proposal.portfolio_id,
                    proposal_id=proposal.proposal_id,
                    instrument_id=proposal.instrument_id,
                    side=proposal.side,
                    notional=str(proposal.notional),
                    actor=actor,
                ),
                timeout=self._risk_timeout_seconds,
            )
        except asyncio.TimeoutError:
            self._risk_failure(
                run,
                proposal,
                DecisionErrorCode.RISK_ASSESSMENT_FAILED.value,
                "NM-08 risk assessment exceeded the timeout",
                counts,
            )
            return self._reload_proposal(proposal)
        except DecisionError as exc:
            self._risk_failure(run, proposal, exc.code, exc.message, counts)
            return self._reload_proposal(proposal)
        except Exception as exc:  # noqa: BLE001 - a risk failure is never approval
            code = (
                DecisionErrorCode.RISK_ENGINE_UNAVAILABLE.value
                if isinstance(exc, (ImportError, AttributeError))
                else DecisionErrorCode.RISK_ASSESSMENT_FAILED.value
            )
            self._risk_failure(run, proposal, code, f"{type(exc).__name__}: {exc}", counts)
            return self._reload_proposal(proposal)
        if not isinstance(decision, dict):
            self._risk_failure(
                run,
                proposal,
                DecisionErrorCode.RISK_ASSESSMENT_FAILED.value,
                "NM-08 returned an invalid decision payload",
                counts,
            )
            return proposal
        decision_key = _text(decision.get("decision_id"))
        if not decision_key:
            self._risk_failure(
                run,
                proposal,
                DecisionErrorCode.RISK_ASSESSMENT_FAILED.value,
                "NM-08 returned no decision id",
                counts,
            )
            return proposal
        stored_risk = decision
        try:
            lookup = getattr(self._risk, "decision", None)
            if callable(lookup):
                found = lookup(decision_key)
                if isinstance(found, dict):
                    stored_risk = found
        except Exception:  # noqa: BLE001 - returned decision remains evidence
            stored_risk = decision
        outcome = _text(stored_risk.get("outcome")) or _text(decision.get("outcome"))
        if outcome not in _RISK_STATUS:
            self._risk_failure(
                run,
                proposal,
                DecisionErrorCode.RISK_ASSESSMENT_FAILED.value,
                f"NM-08 returned an unknown outcome {outcome!r}",
                counts,
            )
            return proposal
        assessed_at = _as_datetime(stored_risk.get("created_at")) or self._now()
        assessment = RiskAssessment(
            proposal_id=proposal.proposal_id,
            risk_decision_id=decision_key,
            engine="NM-08",
            engine_version=str(
                stored_risk.get("engine_version")
                or stored_risk.get("policy_version")
                or "nm08-risk-1.0"
            ),
            outcome=outcome,
            assessed_at=assessed_at,
            policy_id=_text(stored_risk.get("policy_id")),
            policy_version=_text(stored_risk.get("policy_version")),
            state_version=_text(stored_risk.get("state_version")),
            data_quality=_text(stored_risk.get("data_quality")),
            metrics=dict(stored_risk.get("metrics") or {}),
            violations=tuple(
                dict(item)
                for item in stored_risk.get("violations") or ()
                if isinstance(item, Mapping)
            ),
            warnings=tuple(
                dict(item)
                for item in stored_risk.get("warnings") or ()
                if isinstance(item, Mapping)
            ),
            restrictions=tuple(
                str(item) for item in stored_risk.get("restrictions") or ()
            ),
            explanation=_text(stored_risk.get("explanation")),
            references=tuple(str(item) for item in stored_risk.get("references") or ()),
            raw=dict(stored_risk),
        )
        self._store.save_risk_assessment(assessment)
        target = _RISK_STATUS[outcome]
        updated = replace(
            proposal,
            status=target,
            risk_decision_id=decision_key,
            risk_outcome=outcome,
            risk_policy_id=assessment.policy_id,
            risk_policy_version=assessment.policy_version,
            risk_state_version=assessment.state_version,
            risk_restrictions=assessment.restrictions,
            risk_error="",
            updated_at=self._now(),
        )
        ensure_proposal_transition(proposal.status, updated.status)
        self._store.update_proposal(updated)
        self._event(
            run.run_id,
            f"risk.{outcome}",
            assessment.explanation or f"NM-08 returned {outcome}",
            proposal_id=proposal.proposal_id,
            risk_decision_id=decision_key,
            outcome=outcome,
            policy_version=assessment.policy_version,
            restrictions=list(assessment.restrictions),
        )
        return updated

    def _reload_proposal(self, proposal: DecisionProposal) -> DecisionProposal:
        body = self._store.proposal(proposal.proposal_id)
        return self._proposal_from_dict(body) if body is not None else proposal

    def _risk_failure(
        self,
        run: DecisionRun,
        proposal: DecisionProposal,
        code: str,
        message: str,
        counts: dict[str, int],
    ) -> None:
        counts["errors"] += 1
        updated = replace(
            proposal,
            risk_error=message,
            updated_at=self._now(),
        )
        self._store.update_proposal(updated)
        self._event(
            run.run_id,
            "risk.failed",
            message,
            proposal_id=proposal.proposal_id,
            code=code,
        )
        self._last_error_code = code
        self._last_error = message

    # -- helpers --------------------------------------------------------------
    def _holding(
        self, detail: Mapping[str, Any], instrument_id: str
    ) -> dict[str, Any] | None:
        for row in detail.get("holdings") or ():
            if isinstance(row, Mapping) and str(row.get("instrument_id")) == instrument_id:
                return dict(row)
        return None

    def _open_positions(self, detail: Mapping[str, Any]) -> int:
        """Count real holdings; a row with neither quantity nor value is noise."""
        count = 0
        for row in detail.get("holdings") or ():
            if not isinstance(row, Mapping):
                continue
            quantity = _as_decimal(row.get("quantity"))
            market_value = _as_decimal(row.get("market_value"))
            if (quantity is not None and quantity > 0) or (
                quantity is None and market_value is not None and market_value > 0
            ):
                count += 1
        return count

    def _proposal_from_dict(self, body: Mapping[str, Any]) -> DecisionProposal:
        return DecisionProposal(
            proposal_id=str(body["proposal_id"]),
            run_id=str(body.get("run_id") or ""),
            signal_id=str(body.get("signal_id") or ""),
            monitoring_cycle_id=str(body.get("monitoring_cycle_id") or ""),
            strategy_id=str(body.get("strategy_id") or ""),
            strategy_version=str(body.get("strategy_version") or ""),
            portfolio_id=str(body.get("portfolio_id") or ""),
            instrument_id=str(body.get("instrument_id") or ""),
            exchange=str(body.get("exchange") or ""),
            horizon=str(body.get("horizon") or ""),
            action=str(body.get("action") or ""),
            side=str(body.get("side") or ""),
            notional=Decimal(str(body.get("notional") or "0")),
            reference_price=_as_decimal(body.get("reference_price")),
            price_at=_as_datetime(body.get("price_at")),
            created_at=_as_datetime(body.get("created_at")) or utcnow(),
            expires_at=_as_datetime(body.get("expires_at")) or utcnow(),
            reason=str(body.get("reason") or ""),
            status=DecisionProposalStatus(str(body.get("status") or "created")),
            data_quality=str(body.get("data_quality") or ""),
            source_market_data_at=_as_datetime(body.get("source_market_data_at")),
            risk_decision_id=str(body.get("risk_decision_id") or ""),
            risk_outcome=str(body.get("risk_outcome") or ""),
            risk_policy_id=str(body.get("risk_policy_id") or ""),
            risk_policy_version=str(body.get("risk_policy_version") or ""),
            risk_state_version=str(body.get("risk_state_version") or ""),
            risk_restrictions=tuple(
                str(item) for item in body.get("risk_restrictions") or ()
            ),
            risk_error=str(body.get("risk_error") or ""),
            updated_at=_as_datetime(body.get("updated_at")),
            prediction_decision_id=str(body.get("prediction_decision_id") or ""),
            forecast_ids=tuple(str(item) for item in body.get("forecast_ids") or ()),
            forecast_horizon=str(body.get("forecast_horizon") or ""),
            ensemble_version=str(body.get("ensemble_version") or ""),
            model_versions=tuple(
                str(item) for item in body.get("model_versions") or ()
            ),
            advisory_evidence=(
                dict(body["advisory_evidence"])
                if isinstance(body.get("advisory_evidence"), Mapping)
                else None
            ),
            forecast_optimization_evidence=(
                dict(body["forecast_optimization_evidence"])
                if isinstance(body.get("forecast_optimization_evidence"), Mapping)
                else None
            ),
        )

    def _advisory_for_signal(
        self, signal: DecisionSignal
    ) -> dict[str, Any] | None:
        """Return the PI-17 advisory evidence for this signal, or ``None``.

        The reader is only asked once a proposal is actually being created, so a
        disabled, paused or foreign-leased runtime never consumes the handoff.
        A failure is recorded and ignored: advisory evidence must never turn a
        decision into an error.
        """

        reader = self._advisory_evidence
        if reader is None:
            self._advisory_error = ""
            return None
        try:
            body = reader(signal)
        except Exception as exc:  # noqa: BLE001 - evidence must never block a decision
            self._advisory_error = f"{type(exc).__name__}: {exc}"[:300]
            return None
        self._advisory_error = ""
        if not isinstance(body, Mapping):
            return None
        return {str(key): value for key, value in body.items()}

    def _forecast_optimization_for_signal(
        self, signal: DecisionSignal
    ) -> dict[str, Any] | None:
        """Return the PI-23 evidence for this signal, or ``None``.

        Asked only when a proposal is actually being created, so a disabled,
        paused or foreign-leased runtime never consumes the records.  A failure
        is recorded and ignored: evidence must never turn a decision into an
        error.
        """

        reader = self._forecast_optimization_evidence
        if reader is None:
            self._forecast_optimization_error = ""
            return None
        try:
            body = reader(signal)
        except Exception as exc:  # noqa: BLE001 - evidence must never block a decision
            self._forecast_optimization_error = f"{type(exc).__name__}: {exc}"[:300]
            return None
        self._forecast_optimization_error = ""
        if not isinstance(body, Mapping):
            return None
        return {str(key): value for key, value in body.items()}

    def _event(
        self,
        run_id: str,

        kind: str,
        message: str,
        *,
        proposal_id: str = "",
        **payload: Any,
    ) -> None:
        moment = self._now()
        event = DecisionEvent(
            event_id=decision_id(
                "devt_", run_id, kind, proposal_id, message[:80], moment.isoformat()
            ),
            at=moment,
            run_id=run_id,
            proposal_id=proposal_id,
            kind=kind,
            message=message[:500],
            payload=payload,
        )
        try:
            self._store.append_event(event)
        except Exception:  # noqa: BLE001 - an event failure cannot corrupt a decision
            _LOGGER.exception("novamarket.decision.event_not_recorded event=%s", kind)
        if self._publisher is not None:
            try:
                result = self._publisher(
                    f"novamarket.decision.{kind}",
                    {
                        "run_id": run_id,
                        "proposal_id": proposal_id,
                        "message": message[:500],
                        **payload,
                    },
                )
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:  # noqa: BLE001 - a committed decision stands
                _LOGGER.warning("novamarket.decision.event_not_published event=%s", kind)


__all__ = ["AutonomyDecisionPipeline", "DecisionPipelineError"]
