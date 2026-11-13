"""NM-08 independent risk engine.

It reads NM-02…NM-07 state, measures exposure and risk, applies a versioned
policy, and returns an enforceable decision. It never proposes a trade, never
mutates a portfolio except through the explicit ``apply_decision`` gate, and
never turns a failure into an approval.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from novacontrol.novamarket.contracts import (
    PortfolioNotFoundError,
    RiskNotFoundError,
    RiskStateError,
    RiskValidationError,
)
from novacontrol.novamarket.risk.metrics import (
    annualized_volatility,
    capital_utilization,
    concentration,
    historical_cvar,
    historical_var,
    liquidity_value,
    max_drawdown,
    returns_from_closes,
    sharpe,
    sortino,
    turnover_ratio,
)
from novacontrol.novamarket.risk.models import (
    DECISION_TTL_HOURS,
    POLICY_DEFAULTS,
    POLICY_VERSION,
    SCENARIOS,
    AlertStatus,
    ConstraintSeverity,
    MetricResult,
    RiskAction,
    RiskAlert,
    RiskCategory,
    RiskDecision,
    RiskOutcome,
    RiskOverride,
    RiskPolicy,
    Severity,
    StressScenario,
    dec,
    digest_payload,
    risk_id,
    utcnow,
)
from novacontrol.novamarket.risk.policies import (
    UNCERTAINTY_RANK,
    ConstraintBreach,
    RiskSnapshot,
    evaluate_policy,
)
from novacontrol.novamarket.risk.store import RiskStore

_LOGGER = logging.getLogger("novacontrol.novamarket.risk")

Publisher = Callable[[str, dict[str, Any]], Awaitable[None]]

_OUTCOME_EVENTS = {
    RiskOutcome.APPROVED: "novamarket.risk.action.approved",
    RiskOutcome.REJECTED: "novamarket.risk.action.rejected",
    RiskOutcome.RESTRICTED: "novamarket.risk.action.restricted",
    RiskOutcome.REQUIRES_REVIEW: "novamarket.risk.review.required",
    RiskOutcome.INSUFFICIENT_DATA: "novamarket.risk.insufficient_data",
}

_ACTIONS = {action.value for action in RiskAction}

_FORECAST_HORIZON = {
    "short_term": "few_sessions",
    "medium_term": "one_month",
    "long_term": "three_months",
    "aggregate": "one_month",
}


class RiskEngine:
    """Independent assessment, policy enforcement, monitoring and governance."""

    def __init__(
        self,
        store: RiskStore,
        portfolios: Any,
        *,
        market_data: Any | None = None,
        forecasts: Any | None = None,
        radar: Any | None = None,
        discovery: Any | None = None,
        wallets: Any | None = None,
        publisher: Publisher | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._portfolios = portfolios
        self._market_data = market_data
        self._forecasts = forecasts
        self._radar = radar
        self._discovery = discovery
        self._wallets = wallets
        self._publisher = publisher
        self._clock = clock or utcnow
        self._last_error = ""
        self._latencies: list[float] = []

    # -- wiring ---------------------------------------------------------------

    @property
    def store(self) -> RiskStore:
        return self._store

    @property
    def last_error(self) -> str:
        return self._last_error

    def set_market_data(self, market_data: Any | None) -> None:
        self._market_data = market_data

    def set_forecasts(self, forecasts: Any | None) -> None:
        self._forecasts = forecasts

    def set_radar(self, radar: Any | None) -> None:
        self._radar = radar

    def set_discovery(self, discovery: Any | None) -> None:
        self._discovery = discovery

    def set_wallets(self, wallets: Any | None) -> None:
        self._wallets = wallets

    def set_publisher(self, publisher: Publisher | None) -> None:
        self._publisher = publisher

    def _now(self) -> datetime:
        return self._clock()

    async def _publish(self, event_name: str, **payload: Any) -> None:
        if self._publisher is None:
            return
        try:
            await self._publisher(event_name, payload)
        except Exception as exc:  # noqa: BLE001 - a committed decision stands
            self._last_error = f"{event_name}: {type(exc).__name__}: {exc}"
            _LOGGER.warning(
                "novamarket.risk.event_not_published event=%s error=%s",
                event_name,
                type(exc).__name__,
            )

    # -- policies -------------------------------------------------------------

    def policy(self, policy_id: str) -> RiskPolicy:
        if policy_id not in POLICY_DEFAULTS:
            raise RiskValidationError(f"unknown risk policy {policy_id!r}")
        stored = self._store.latest_policy(policy_id)
        if stored is not None:
            return stored
        default = POLICY_DEFAULTS[policy_id]
        self._store.save_policy(default, actor="default")
        return default

    def policies(self) -> dict[str, Any]:
        policies = [self.policy(policy_id).to_dict() for policy_id in POLICY_DEFAULTS]
        return {
            "ok": True,
            "version": POLICY_VERSION,
            "policies": policies,
            "history": self._store.policies(),
        }

    async def update_policy(
        self, policy_id: str, changes: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        current = self.policy(policy_id)
        allowed = {
            "max_position_weight": Decimal,
            "max_sector_weight": Decimal,
            "max_invested_fraction": Decimal,
            "min_cash_fraction": Decimal,
            "max_drawdown_pct": Decimal,
            "max_volatility_pct": Decimal,
            "min_liquidity_value": Decimal,
            "max_turnover": Decimal,
            "max_forecast_age_hours": float,
            "max_data_age_hours": float,
            "max_state_age_hours": float,
            "min_observations": int,
            "max_aggregate_sector_weight": Decimal,
            "max_uncertainty": str,
            "restricted_instruments": tuple,
            "allowed_model_states": tuple,
        }
        try:
            supplied = dict(changes or {})
        except (TypeError, ValueError) as exc:
            raise RiskValidationError("policy changes must be a mapping") from exc
        updates: dict[str, Any] = {}
        for name, raw in supplied.items():
            if name not in allowed:
                raise RiskValidationError(f"risk policy field {name!r} is not editable")
            converter = allowed[name]
            try:
                if converter is tuple:
                    updates[name] = tuple(str(item) for item in raw)
                elif converter is str:
                    updates[name] = str(raw)
                else:
                    updates[name] = converter(raw)
            except (TypeError, ValueError, ArithmeticError) as exc:
                raise RiskValidationError(
                    f"risk policy field {name!r} has an invalid value"
                ) from exc
        if not updates:
            raise RiskValidationError("no policy change was supplied")
        updated = current
        for name, value in updates.items():
            updated = _replace_policy(updated, name, value)
        version = f"{POLICY_VERSION}+{digest_payload(updated.to_dict())}"
        updated = _replace_policy(updated, "version", version)
        self._store.save_policy(updated, actor=actor or "operator")
        await self._publish(
            "novamarket.risk.policy.updated",
            policy_id=policy_id,
            version=version,
            actor=actor or "operator",
            fields=sorted(updates),
        )
        return {"ok": True, "policy": updated.to_dict(), "history": self._store.policies(policy_id)}

    # -- assessment -----------------------------------------------------------

    async def assess(
        self,
        action: str = RiskAction.APPLY_PROPOSAL.value,
        *,
        portfolio_id: str = "",
        proposal_id: str = "",
        instrument_id: str = "",
        side: str = "",
        notional: object | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        """Assess one proposed action and persist the decision.

        A repeated assessment for the same proposal, policy version and portfolio
        state returns the stored decision instead of creating a second one.
        """
        chosen = str(action or "").strip().lower()
        if chosen not in _ACTIONS:
            raise RiskValidationError(f"unknown risk action {action!r}")
        started = time.monotonic()
        await self._publish(
            "novamarket.risk.assessment.requested",
            action=chosen,
            portfolio_id=portfolio_id,
            proposal_id=proposal_id,
        )
        try:
            decision = self._assess(
                chosen,
                portfolio_id=portfolio_id,
                proposal_id=proposal_id,
                instrument_id=instrument_id,
                side=side,
                notional=notional,
                actor=actor,
            )
        except (RiskNotFoundError, RiskValidationError, RiskStateError):
            raise
        except Exception as exc:  # noqa: BLE001 - a failure is never an approval
            self._last_error = f"{type(exc).__name__}: {exc}"
            decision = self._failure_decision(
                chosen,
                portfolio_id=portfolio_id,
                proposal_id=proposal_id,
                instrument_id=instrument_id,
                actor=actor,
                reason=f"{type(exc).__name__}: {exc}",
            )
        existing = self._store.decision(decision.decision_id)
        if existing is not None:
            self._latencies.append((time.monotonic() - started) * 1000.0)
            return existing
        self._store.save_decision(decision)
        alerts = await self._raise_alerts(decision)
        await self._publish(
            _OUTCOME_EVENTS[decision.outcome],
            decision_id=decision.decision_id,
            action=decision.action,
            portfolio_id=decision.portfolio_id,
            proposal_id=decision.proposal_id,
            outcome=decision.outcome.value,
            policy_id=decision.policy_id,
            policy_version=decision.policy_version,
            violations=len(decision.violations),
        )
        if decision.violations:
            await self._publish(
                "novamarket.risk.limit.breached",
                decision_id=decision.decision_id,
                portfolio_id=decision.portfolio_id,
                codes=[item.get("code") for item in decision.violations],
            )
        await self._publish(
            "novamarket.risk.assessment.completed",
            decision_id=decision.decision_id,
            outcome=decision.outcome.value,
            alerts=len(alerts),
            latency_ms=round((time.monotonic() - started) * 1000.0, 3),
        )
        self._latencies.append((time.monotonic() - started) * 1000.0)
        return decision.to_dict()

    def _assess(
        self,
        action: str,
        *,
        portfolio_id: str,
        proposal_id: str,
        instrument_id: str,
        side: str,
        notional: object | None,
        actor: str,
    ) -> RiskDecision:
        moment = self._now()
        if action in {RiskAction.APPLY_PROPOSAL.value, RiskAction.REBALANCE.value}:
            if not portfolio_id:
                raise RiskValidationError("a proposal assessment needs a portfolio")
            if action == RiskAction.APPLY_PROPOSAL.value and not proposal_id:
                raise RiskValidationError("an apply assessment needs a proposal id")
        if action in {
            RiskAction.ADD_POSITION.value,
            RiskAction.INCREASE_POSITION.value,
            RiskAction.REDUCE_POSITION.value,
            RiskAction.REMOVE_POSITION.value,
            RiskAction.PAPER_ORDER.value,
        } and not instrument_id:
            raise RiskValidationError("a position action needs an instrument id")

        detail = self._portfolios.detail(portfolio_id) if portfolio_id else None
        proposal = self._proposal(portfolio_id, proposal_id) if proposal_id else None
        if action == RiskAction.APPLY_PROPOSAL.value and proposal is None:
            raise RiskNotFoundError(f"unknown proposal {proposal_id!r}")
        if proposal is not None and str(proposal.get("status")) != "validated":
            raise RiskStateError(
                f"proposal {proposal_id} is {proposal.get('status')}; only a validated "
                "proposal can be assessed for apply"
            )
        if action in {RiskAction.REDUCE_POSITION.value, RiskAction.REMOVE_POSITION.value}:
            held = {str(row.get("instrument_id")) for row in _position_rows(detail)}
            if instrument_id not in held:
                raise RiskNotFoundError(
                    f"{instrument_id} is not held by portfolio {portfolio_id}"
                )

        policy_id = (
            str(detail["portfolio"]["horizon"]) if detail is not None else "aggregate"
        )
        policy = self.policy(policy_id)
        total = (
            Decimal(str(detail["valuation"]["total_value"])) if detail is not None else Decimal("0")
        )
        amount = _optional_decimal(notional)
        if action in {
            RiskAction.ADD_POSITION.value,
            RiskAction.INCREASE_POSITION.value,
            RiskAction.ALLOCATE_CAPITAL.value,
            RiskAction.RESERVE_CAPITAL.value,
            RiskAction.PAPER_ORDER.value,
        }:
            if amount is None or amount <= 0:
                raise RiskValidationError(
                    f"{action} needs a positive notional amount"
                )
        if action in {RiskAction.ADD_POSITION.value, RiskAction.INCREASE_POSITION.value} and side.strip().lower() == "sell":
            raise RiskValidationError("an add/increase action cannot have side=sell")
        if action in {RiskAction.ALLOCATE_CAPITAL.value, RiskAction.RESERVE_CAPITAL.value} and not portfolio_id:
            raise RiskValidationError("a capital action needs a portfolio")
        projected, adds = self._project(
            detail=detail,
            proposal=proposal,
            action=action,
            instrument_id=instrument_id,
            side=side,
            amount=amount,
        )
        invested_fraction, cash_weight = _fractions(projected)
        turnover = self._turnover(proposal, amount, total, action)
        metrics, volatility, drawdown, liquidity, data_age, state_age, insuff = (
            self._measure(
                detail=detail,
                projected=projected,
                policy=policy,
                adds_exposure=adds,
                total=total,
            )
        )
        forecast_failures = self._forecast_failures(projected, detail, policy, adds)
        event_pressure = self._event_pressure(projected)
        if data_age is not None and data_age > policy.max_data_age_hours:
            insuff = tuple(insuff)  # the policy evaluator reports the hard breach
        combined_sectors = self._combined_sector_weights(portfolio_id, projected)
        capital_request: Decimal | None = None
        available_cash: Decimal | None = None
        if action in {
            RiskAction.ALLOCATE_CAPITAL.value,
            RiskAction.RESERVE_CAPITAL.value,
        }:
            capital_request = amount
            if detail is not None:
                available_cash = Decimal(str(detail["valuation"]["cash"]))
        snapshot = RiskSnapshot(
            positions=tuple(_position_rows(detail)),
            projected=tuple(projected),
            cash_weight=cash_weight,
            invested_fraction=invested_fraction,
            drawdown_pct=drawdown,
            volatility_pct=volatility,
            turnover=turnover,
            data_age_hours=data_age,
            state_age_hours=state_age,
            aggregate_sector_weights=combined_sectors,
            liquidity=liquidity,
            forecast_failures=tuple(forecast_failures),
            event_pressure=tuple(event_pressure),
            insufficient=tuple(insuff),
            capital_request=capital_request,
            available_cash=available_cash,
        )
        evaluation = evaluate_policy(
            policy, snapshot, action=action, adds_exposure=adds
        )
        outcome = evaluation.outcome
        state_version = self._state_version(portfolio_id, proposal, detail)
        decision_id = risk_id(
            "rdec_",
            action,
            portfolio_id,
            proposal_id,
            instrument_id,
            side.strip().lower(),
            "" if amount is None else str(amount),
            state_version,
            policy.version,
        )
        explanation = _explain(outcome, evaluation, snapshot, policy)
        metrics_payload = {
            **metrics,
            "drawdown_pct": MetricResult(
                name="drawdown_pct",
                value=drawdown,
                unit="percent",
                observations=max(1, len(self._valuation_totals(portfolio_id))),
                sufficient=True,
                assumptions=("largest peak-to-trough decline of stored valuations",),
            ),
            "capital_utilization": capital_utilization(
                (invested_fraction * total).quantize(Decimal("0.01")), total
            ),
            "concentration_hhi": concentration(
                [float(Decimal(str(row.get("weight") or "0"))) for row in projected]
            ),
        }
        if proposal is not None:
            references = [f"proposal:{proposal_id}", f"portfolio:{portfolio_id}"]
        elif portfolio_id:
            references = [f"portfolio:{portfolio_id}"]
        else:
            references = ["aggregate"]
        return RiskDecision(
            decision_id=decision_id,
            action=action,
            outcome=outcome,
            created_at=moment,
            expires_at=moment + timedelta(hours=DECISION_TTL_HOURS),
            policy_id=policy.policy_id,
            policy_version=policy.version,
            state_version=state_version,
            portfolio_id=portfolio_id,
            proposal_id=proposal_id,
            instrument_id=instrument_id,
            data_quality=("measured" if not evaluation.insufficient else "insufficient"),
            explanation=explanation,
            metrics=metrics_payload,
            violations=tuple(item.to_dict() for item in evaluation.breaches),
            warnings=tuple(item.to_dict() for item in evaluation.warnings),
            restrictions=evaluation.restrictions,
            references=tuple(references),
            actor=actor,
        )

    def _failure_decision(
        self,
        action: str,
        *,
        portfolio_id: str,
        proposal_id: str,
        instrument_id: str,
        actor: str,
        reason: str,
    ) -> RiskDecision:
        moment = self._now()
        policy = POLICY_DEFAULTS["aggregate" if not portfolio_id else "short_term"]
        return RiskDecision(
            decision_id=risk_id(
                "rdec_", action, portfolio_id, proposal_id, instrument_id, moment.isoformat(), "failure"
            ),
            action=action,
            outcome=RiskOutcome.INSUFFICIENT_DATA,
            created_at=moment,
            expires_at=moment + timedelta(hours=DECISION_TTL_HOURS),
            policy_id=policy.policy_id,
            policy_version=policy.version,
            state_version="unavailable",
            portfolio_id=portfolio_id,
            proposal_id=proposal_id,
            instrument_id=instrument_id,
            data_quality="assessment_failed",
            explanation=(
                "The risk assessment could not complete and was NOT approved: "
                + reason
            ),
            metrics={},
            violations=(),
            warnings=(),
            restrictions=("retry the assessment after the underlying error is resolved",),
            references=(),
            actor=actor,
        )

    # -- projection and measurement ------------------------------------------

    def _project(
        self,
        *,
        detail: Mapping[str, Any] | None,
        proposal: Mapping[str, Any] | None,
        action: str,
        instrument_id: str,
        side: str,
        amount: Decimal | None,
    ) -> tuple[list[dict[str, Any]], bool]:
        rows = _position_rows(detail)
        total = (
            Decimal(str(detail["valuation"]["total_value"]))
            if detail is not None
            else Decimal("0")
        )
        weights = {
            str(row["instrument_id"]): Decimal(str(row["weight"])) for row in rows
        }
        adds = action in {
            RiskAction.ADD_POSITION.value,
            RiskAction.INCREASE_POSITION.value,
        } or action in {
            RiskAction.APPLY_PROPOSAL.value,
            RiskAction.REBALANCE.value,
        } or (
            action == RiskAction.PAPER_ORDER.value and side.strip().lower() != "sell"
        )
        if proposal is not None:
            for intent in proposal.get("intents", ()) or ():
                iid = str(intent.get("instrument_id") or "")
                if not iid:
                    continue
                target = Decimal(str(intent.get("target_weight") or "0"))
                if intent.get("side") == "sell" and target <= 0:
                    weights.pop(iid, None)
                else:
                    weights[iid] = target
        elif instrument_id:
            delta = Decimal("0")
            if total > 0 and amount is not None:
                delta = (amount / total).quantize(Decimal("0.0001"))
            if action in {RiskAction.REDUCE_POSITION.value, RiskAction.REMOVE_POSITION.value} or (
                action == RiskAction.PAPER_ORDER.value and side.strip().lower() == "sell"
            ):
                delta = -delta
            if action == RiskAction.REMOVE_POSITION.value:
                weights.pop(instrument_id, None)
            else:
                weights[instrument_id] = max(
                    Decimal("0"), weights.get(instrument_id, Decimal("0")) + delta
                )
        projected: list[dict[str, Any]] = []
        for iid, weight in sorted(weights.items()):
            if weight <= 0:
                continue
            current = next(
                (row for row in rows if str(row["instrument_id"]) == iid), None
            )
            projected.append(
                {
                    "instrument_id": iid,
                    "weight": weight,
                    "sector": str((current or {}).get("sector") or "unknown"),
                    "asset_class": str((current or {}).get("asset_class") or "unknown"),
                    "value": (weight * total).quantize(Decimal("0.01")),
                }
            )
        return projected, adds

    def _measure(
        self,
        *,
        detail: Mapping[str, Any] | None,
        projected: Sequence[Mapping[str, Any]],
        policy: RiskPolicy,
        adds_exposure: bool,
        total: Decimal,
    ) -> tuple[
        dict[str, MetricResult],
        Decimal | None,
        Decimal,
        Decimal | None,
        float | None,
        float | None,
        tuple[str, ...],
    ]:
        insufficient: list[str] = []
        metrics: dict[str, MetricResult] = {}
        portfolio_id = str((detail or {}).get("portfolio", {}).get("portfolio_id") or "")

        # Data age: the oldest price that the projection depends on.
        ages: list[float] = []
        for row in projected:
            observed = self._observed_at(str(row["instrument_id"]))
            if observed is None:
                insufficient.append(
                    f"no timestamped price for {row['instrument_id']}"
                )
                continue
            ages.append((self._now() - observed).total_seconds() / 3600.0)
        data_age = max(ages) if ages else 0.0

        state_age: float | None = None
        if portfolio_id:
            valuations = self._valuation_totals(portfolio_id)
            if valuations:
                last_at = self._last_valuation_at(portfolio_id)
                if last_at is not None:
                    state_age = (self._now() - last_at).total_seconds() / 3600.0
            else:
                insufficient.append("the portfolio has no stored valuation")

        # Drawdown from the append-only valuation history.
        drawdown = Decimal("0")
        if detail is not None:
            initial = Decimal(str(detail["portfolio"]["initial_capital"]))
            values = [float(initial)] + [float(value) for value in self._valuation_totals(portfolio_id)]
            result = max_drawdown(values, minimum=2)
            metrics["drawdown"] = result
            drawdown = result.value or Decimal("0")

        # Volatility: a weighted return series from priced bars.
        volatility: Decimal | None = None
        returns: dict[str, list[float]] = {}
        for row in projected:
            closes = self._closes(str(row["instrument_id"]))
            if len(closes) < policy.min_observations + 1:
                insufficient.append(
                    f"{row['instrument_id']} has {max(0, len(closes) - 1)} return(s); "
                    f"at least {policy.min_observations} are required for volatility"
                )
                continue
            returns[str(row["instrument_id"])] = returns_from_closes(closes)
        if projected and len(returns) == len(projected):
            length = min(len(series) for series in returns.values())
            weights = [Decimal(str(row["weight"])) for row in projected]
            blended: list[float] = []
            for index in range(-length, 0):
                value = 0.0
                for row, weight in zip(projected, weights):
                    value += float(weight) * returns[str(row["instrument_id"])][index]
                blended.append(value)
            closes = [100.0]
            for value in blended:
                closes.append(closes[-1] * (1.0 + value))
            result = annualized_volatility(
                closes, minimum=policy.min_observations
            )
            metrics["volatility"] = result
            volatility = result.value
            metrics["sharpe"] = sharpe(blended, minimum=policy.min_observations)
            metrics["sortino"] = sortino(blended, minimum=policy.min_observations)
        elif not projected:
            volatility = Decimal("0")
            metrics["volatility"] = MetricResult(
                name="annualized_volatility_pct",
                value=Decimal("0"),
                unit="percent",
                observations=0,
                sufficient=True,
                assumptions=("no positions; volatility is zero by construction",),
            )

        metrics.setdefault(
            "beta",
            MetricResult.insufficient(
                "beta", "no benchmark return series is available in this build"
            ),
        )
        metrics.setdefault(
            "correlation",
            MetricResult.insufficient(
                "correlation", "no benchmark return series is available in this build"
            ),
        )

        # Liquidity: the weakest position in the projection.
        liquidities: list[Decimal] = []
        for row in projected:
            bars = self._bars(str(row["instrument_id"]))
            result = liquidity_value(bars)
            if result.value is None:
                if adds_exposure:
                    insufficient.append(
                        f"liquidity for {row['instrument_id']} is unavailable"
                    )
                continue
            liquidities.append(result.value)
        liquidity = min(liquidities) if liquidities else None
        if liquidity is not None:
            metrics["liquidity"] = MetricResult(
                name="average_daily_value",
                value=liquidity,
                unit="currency",
                observations=len(liquidities),
                sufficient=True,
                assumptions=("minimum average daily traded value across positions",),
            )

        return (
            metrics,
            volatility,
            drawdown,
            liquidity,
            data_age,
            state_age,
            tuple(dict.fromkeys(insufficient)),
        )

    # -- exposure helpers -----------------------------------------------------

    def _position_rows(self, detail: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        if detail is None:
            return []
        return [dict(row) for row in detail.get("holdings", ()) or ()]

    def _proposal(self, portfolio_id: str, proposal_id: str) -> Mapping[str, Any] | None:
        body = self._portfolios.proposals(portfolio_id, limit=100)
        for row in body.get("proposals", ()) or ():
            if str(row.get("proposal_id")) == proposal_id:
                return dict(row)
        return None

    def _turnover(
        self,
        proposal: Mapping[str, Any] | None,
        amount: Decimal | None,
        total: Decimal,
        action: str,
    ) -> Decimal:
        if total <= 0:
            return Decimal("0")
        if proposal is not None:
            notional = sum(
                (Decimal(str(intent.get("notional") or "0")) for intent in proposal.get("intents", ())),
                Decimal("0"),
            )
            return (notional / total).quantize(Decimal("0.0001"))
        if amount is not None and action in {
            RiskAction.ADD_POSITION.value,
            RiskAction.INCREASE_POSITION.value,
            RiskAction.REDUCE_POSITION.value,
            RiskAction.REMOVE_POSITION.value,
            RiskAction.PAPER_ORDER.value,
        }:
            return (abs(amount) / total).quantize(Decimal("0.0001"))
        return Decimal("0")

    def _forecast_failures(
        self,
        projected: Sequence[Mapping[str, Any]],
        detail: Mapping[str, Any] | None,
        policy: RiskPolicy,
        adds_exposure: bool,
    ) -> list[str]:
        if not adds_exposure:
            return []
        if self._forecasts is None:
            return ["the forecast service is unavailable; forecast reliability cannot be checked"]
        held = {
            str(row["instrument_id"]): Decimal(str(row["weight"]))
            for row in self._position_rows(detail)
        }
        failures: list[str] = []
        for row in projected:
            iid = str(row["instrument_id"])
            if Decimal(str(row["weight"])) <= held.get(iid, Decimal("0")):
                continue
            required_horizon = _FORECAST_HORIZON.get(policy.policy_id, "")
            try:
                body = self._forecasts.forecasts(instrument_id=iid, limit=50)
            except Exception:  # noqa: BLE001
                failures.append(f"forecast history for {iid} is unavailable")
                continue
            rows = [
                row
                for row in (body.get("forecasts", ()) or ())
                if str(row.get("horizon") or "") == required_horizon
            ]
            if not rows:
                failures.append(
                    f"no {required_horizon or 'matching-horizon'} forecast exists for {iid}"
                )
                continue
            latest = max(rows, key=lambda item: str(item.get("created_at") or ""))
            created = str(latest.get("created_at") or "")
            try:
                age = (self._now() - datetime.fromisoformat(created)).total_seconds() / 3600.0
            except ValueError:
                failures.append(f"forecast for {iid} has no readable timestamp")
                continue
            if age > policy.max_forecast_age_hours:
                failures.append(
                    f"{iid} forecast is {age:.1f}h old; the policy allows "
                    f"{policy.max_forecast_age_hours:.0f}h"
                )
            uncertainty = str(latest.get("uncertainty") or "unknown")
            if UNCERTAINTY_RANK.get(uncertainty, 9) > UNCERTAINTY_RANK[policy.max_uncertainty]:
                failures.append(
                    f"{iid} forecast uncertainty {uncertainty} exceeds "
                    f"{policy.max_uncertainty}"
                )
            state = self._model_state(
                iid, str(latest.get("model_id") or ""), str(latest.get("horizon") or "")
            )
            if state is None:
                failures.append(f"{iid} model has no measured state")
            elif state not in policy.allowed_model_states:
                failures.append(f"{iid} model state {state} is not accepted by the policy")
        return failures

    def _model_state(self, instrument_id: str, model_id: str, horizon: str) -> str | None:
        if self._forecasts is None:
            return None
        try:
            body = self._forecasts.models(instrument_id=instrument_id)
        except Exception:  # noqa: BLE001
            return None
        for row in body.get("models", ()) or ():
            if (
                str(row.get("model_id")) == model_id
                and str(row.get("horizon")) == horizon
            ):
                return str(row.get("state") or "")
        return None

    def _event_pressure(self, projected: Sequence[Mapping[str, Any]]) -> list[str]:
        if self._radar is None:
            return []
        pressure: list[str] = []
        for row in projected:
            iid = str(row["instrument_id"])
            try:
                body = self._radar.instrument_events(iid, limit=20)
            except Exception:  # noqa: BLE001
                continue
            for event in body.get("events", ()) or ():
                if str(event.get("verification") or "") not in {
                    "officially_confirmed",
                    "multi_source_corroborated",
                }:
                    continue
                if str(event.get("importance") or "") in {"high", "critical"}:
                    pressure.append(iid)
                    break
        return pressure

    def _combined_sector_weights(
        self, portfolio_id: str, projected: Sequence[Mapping[str, Any]]
    ) -> dict[str, Decimal]:
        values: dict[str, Decimal] = {}
        for row in projected:
            sector = str(row.get("sector") or "unknown")
            value = Decimal(str(row.get("value") or "0"))
            values[sector] = values.get(sector, Decimal("0")) + value
        total = sum(values.values(), Decimal("0"))
        if portfolio_id:
            try:
                total = Decimal(
                    str(self._portfolios.detail(portfolio_id)["valuation"]["total_value"])
                )
            except Exception:  # noqa: BLE001 - the projected values remain usable
                pass
        try:
            body = self._portfolios.portfolios(limit=100)
        except Exception:  # noqa: BLE001
            body = {"portfolios": []}
        for record in body.get("portfolios", ()) or ():
            pid = str(record.get("portfolio_id") or "")
            if pid == portfolio_id:
                continue
            try:
                detail = self._portfolios.detail(pid)
            except Exception:  # noqa: BLE001
                continue
            total += Decimal(str(detail["valuation"]["total_value"]))
            for row in detail.get("holdings", ()) or ():
                value = Decimal(str(row.get("market_value") or "0"))
                sector = str(row.get("sector") or "unknown")
                values[sector] = values.get(sector, Decimal("0")) + value
        if total <= 0:
            return {}
        return {
            sector: (value / total).quantize(Decimal("0.0001"))
            for sector, value in values.items()
        }

    def _state_version(
        self,
        portfolio_id: str,
        proposal: Mapping[str, Any] | None,
        detail: Mapping[str, Any] | None,
    ) -> str:
        if detail is None:
            return "aggregate"
        return digest_payload(
            {
                "portfolio_id": portfolio_id,
                "updated_at": detail["portfolio"].get("updated_at"),
                "total_value": detail["valuation"].get("total_value"),
                "positions": detail.get("counts", {}).get("positions"),
                "valuations": detail.get("counts", {}).get("valuations"),
                "proposal": str((proposal or {}).get("digest") or ""),
            }
        )

    # -- market-data access ---------------------------------------------------

    def _observed_at(self, instrument_id: str) -> datetime | None:
        if self._market_data is None:
            return None
        try:
            result = self._market_data.latest(instrument_id)
        except Exception:  # noqa: BLE001
            return None
        if getattr(result, "usable", False) is False:
            return None
        selected = getattr(result, "selected", None)
        return getattr(selected, "observed_at", None) if selected is not None else None

    def _bars(self, instrument_id: str) -> list[Any]:
        if self._market_data is None:
            return []
        try:
            page = self._market_data.bars(
                instrument_id, limit=160, ascending=False
            )
        except Exception:  # noqa: BLE001
            return []
        return list(reversed(getattr(page, "items", ()) or ()))

    def _closes(self, instrument_id: str) -> list[float]:
        return [float(bar.close) for bar in self._bars(instrument_id)]

    def _valuation_totals(self, portfolio_id: str) -> list[Decimal]:
        if not portfolio_id:
            return []
        try:
            rows = self._portfolios.valuations(portfolio_id, limit=500).get(
                "valuations", ()
            )
        except Exception:  # noqa: BLE001
            return []
        return [Decimal(str(row.get("total_value") or "0")) for row in rows]

    def _last_valuation_at(self, portfolio_id: str) -> datetime | None:
        try:
            rows = self._portfolios.valuations(portfolio_id, limit=500).get(
                "valuations", ()
            )
        except Exception:  # noqa: BLE001
            return None
        if not rows:
            return None
        try:
            return datetime.fromisoformat(str(rows[-1]["at"]))
        except (KeyError, ValueError):
            return None

    # -- alerts ---------------------------------------------------------------

    async def _raise_alerts(self, decision: RiskDecision) -> list[RiskAlert]:
        moment = self._now()
        created: list[RiskAlert] = []
        for violation in decision.violations:
            severity = (
                Severity.CRITICAL
                if violation.get("severity") == ConstraintSeverity.HARD.value
                else (
                    Severity.HIGH
                    if violation.get("severity") == ConstraintSeverity.REVIEW.value
                    else Severity.WARNING
                )
            )
            code = str(violation.get("code") or "")
            instrument = str(violation.get("instrument_id") or "")
            sector = str(violation.get("sector") or "")
            key = f"{decision.portfolio_id}|{code}|{instrument}|{sector}"
            existing = self._store.open_alert(key)
            if existing is not None:
                continue
            alert = RiskAlert(
                alert_id=risk_id("ralert_", key, moment.isoformat()),
                dedupe_key=key,
                category=RiskCategory(str(violation.get("category") or "operational")),
                severity=severity,
                created_at=moment,
                updated_at=moment,
                portfolio_id=decision.portfolio_id,
                instrument_id=instrument,
                metric=code,
                threshold=str(violation.get("limit") or ""),
                observed=str(violation.get("actual") or ""),
                explanation=str(violation.get("message") or ""),
                recommended_action="review the risk decision and adjust the action",
                decision_id=decision.decision_id,
            )
            self._store.save_alert(alert)
            created.append(alert)
        for alert in created:
            await self._publish(
                "novamarket.risk.alert.created",
                alert_id=alert.alert_id,
                severity=alert.severity.value,
                category=alert.category.value,
                portfolio_id=alert.portfolio_id,
            )
        return created

    def alerts(self, *, status: str = "", limit: int = 100) -> dict[str, Any]:
        if status and status not in {item.value for item in AlertStatus}:
            raise RiskValidationError(f"unknown alert status {status!r}")
        rows = self._store.alerts(status=status, limit=limit)
        return {"ok": True, "alerts": rows, "total": len(rows), "limit": limit}

    async def acknowledge_alert(self, alert_id: str, *, actor: str = "") -> dict[str, Any]:
        updated = self._store.set_alert_status(alert_id, AlertStatus.ACKNOWLEDGED)
        if updated is None:
            raise RiskNotFoundError(f"unknown risk alert {alert_id!r}")
        await self._publish(
            "novamarket.risk.alert.acknowledged",
            alert_id=alert_id,
            actor=actor,
        )
        return {"ok": True, "alert": updated}

    async def resolve_alert(self, alert_id: str, *, actor: str = "") -> dict[str, Any]:
        updated = self._store.set_alert_status(alert_id, AlertStatus.RESOLVED)
        if updated is None:
            raise RiskNotFoundError(f"unknown risk alert {alert_id!r}")
        await self._publish(
            "novamarket.risk.alert.resolved",
            alert_id=alert_id,
            actor=actor,
        )
        return {"ok": True, "alert": updated}

    # -- reports --------------------------------------------------------------

    def portfolio_risk(self, portfolio_id: str) -> dict[str, Any]:
        detail = self._portfolios.detail(portfolio_id)
        policy = self.policy(str(detail["portfolio"]["horizon"]))
        total = Decimal(str(detail["valuation"]["total_value"]))
        rows = self._position_rows(detail)
        projected = [
            {
                "instrument_id": str(row["instrument_id"]),
                "weight": Decimal(str(row["weight"])),
                "sector": str(row.get("sector") or "unknown"),
                "asset_class": str(row.get("asset_class") or "unknown"),
                "value": Decimal(str(row.get("market_value") or "0")),
            }
            for row in rows
        ]
        invested, cash = _fractions(projected)
        metrics, volatility, drawdown, liquidity, data_age, state_age, insuff = (
            self._measure(
                detail=detail,
                projected=projected,
                policy=policy,
                adds_exposure=False,
                total=total,
            )
        )
        snapshot = RiskSnapshot(
            positions=tuple(rows),
            projected=tuple(projected),
            cash_weight=cash,
            invested_fraction=invested,
            drawdown_pct=drawdown,
            volatility_pct=volatility,
            turnover=Decimal("0"),
            data_age_hours=data_age,
            state_age_hours=state_age,
            aggregate_sector_weights=self._combined_sector_weights(portfolio_id, projected),
            liquidity=liquidity,
            forecast_failures=(),
            event_pressure=tuple(self._event_pressure(projected)),
            insufficient=insuff,
        )
        evaluation = evaluate_policy(policy, snapshot, action="report", adds_exposure=False)
        return {
            "ok": True,
            "portfolio_id": portfolio_id,
            "horizon": policy.policy_id,
            "policy": policy.to_dict(),
            "outcome": evaluation.outcome.value,
            "valuation": detail["valuation"],
            "metrics": {
                **{name: result.to_dict() for name, result in metrics.items()},
                "drawdown": {
                    "value": str(drawdown),
                    "unit": "percent",
                    "sufficient": True,
                },
                "volatility": {
                    "value": None if volatility is None else str(volatility),
                    "unit": "percent",
                    "sufficient": volatility is not None,
                },
            },
            "breaches": [item.to_dict() for item in evaluation.breaches],
            "warnings": [item.to_dict() for item in evaluation.warnings],
            "restrictions": list(evaluation.restrictions),
            "insufficient": list(evaluation.insufficient),
            "positions": list(evaluation_rows(projected)),
            "is_hypothetical": False,
        }

    def position_risk(self, portfolio_id: str, instrument_id: str) -> dict[str, Any]:
        detail = self._portfolios.detail(portfolio_id)
        policy = self.policy(str(detail["portfolio"]["horizon"]))
        row = next(
            (
                item
                for item in self._position_rows(detail)
                if str(item["instrument_id"]) == instrument_id
            ),
            None,
        )
        if row is None:
            raise RiskNotFoundError(
                f"{instrument_id} is not held by portfolio {portfolio_id}"
            )
        weight = Decimal(str(row["weight"]))
        closes = self._closes(instrument_id)
        volatility = annualized_volatility(closes, minimum=policy.min_observations)
        liquidity = liquidity_value(self._bars(instrument_id))
        breaches: list[ConstraintBreach] = []
        if weight > policy.max_position_weight:
            breaches.append(
                ConstraintBreach(
                    code="max_position_weight",
                    category=RiskCategory.CONCENTRATION,
                    severity=ConstraintSeverity.HARD,
                    message=(
                        f"{instrument_id} is {weight} of the book; the limit is "
                        f"{policy.max_position_weight}"
                    ),
                    actual=str(weight),
                    limit=str(policy.max_position_weight),
                    instrument_id=instrument_id,
                )
            )
        if volatility.value is not None and volatility.value > policy.max_volatility_pct:
            breaches.append(
                ConstraintBreach(
                    code="max_volatility",
                    category=RiskCategory.MARKET,
                    severity=ConstraintSeverity.SOFT,
                    message=(
                        f"volatility {volatility.value}% exceeds "
                        f"{policy.max_volatility_pct}%"
                    ),
                    actual=f"{volatility.value}%",
                    limit=f"{policy.max_volatility_pct}%",
                    instrument_id=instrument_id,
                )
            )
        if liquidity.value is not None and liquidity.value < policy.min_liquidity_value:
            breaches.append(
                ConstraintBreach(
                    code="min_liquidity",
                    category=RiskCategory.LIQUIDITY,
                    severity=ConstraintSeverity.REVIEW,
                    message=(
                        f"average daily value {liquidity.value} is below "
                        f"{policy.min_liquidity_value}"
                    ),
                    actual=str(liquidity.value),
                    limit=str(policy.min_liquidity_value),
                    instrument_id=instrument_id,
                )
            )
        failures = self._forecast_failures(
            [
                {
                    "instrument_id": instrument_id,
                    "weight": weight,
                    "sector": row.get("sector"),
                }
            ],
            detail,
            policy,
            adds_exposure=False,
        )
        events = self._event_pressure(
            [{"instrument_id": instrument_id, "weight": weight, "sector": row.get("sector")}]
        )
        insufficient = []
        if not volatility.sufficient:
            insufficient.append(volatility.reason)
        if not liquidity.sufficient:
            insufficient.append(liquidity.reason)
        return {
            "ok": True,
            "portfolio_id": portfolio_id,
            "instrument_id": instrument_id,
            "weight": str(weight),
            "market_value": str(row.get("market_value") or ""),
            "sector": str(row.get("sector") or "unknown"),
            "metrics": {
                "volatility": volatility.to_dict(),
                "liquidity": liquidity.to_dict(),
            },
            "breaches": [item.to_dict() for item in breaches],
            "forecast_failures": failures,
            "event_pressure": events,
            "insufficient": insufficient,
            "data_quality": "measured" if not insufficient else "insufficient",
            "outcome": (
                RiskOutcome.INSUFFICIENT_DATA.value
                if insufficient
                else (
                    RiskOutcome.REJECTED.value
                    if any(item.severity is ConstraintSeverity.HARD for item in breaches)
                    else (
                        RiskOutcome.RESTRICTED.value
                        if breaches
                        else RiskOutcome.APPROVED.value
                    )
                )
            ),
        }

    def exposure(self) -> dict[str, Any]:
        body = self._portfolios.portfolios(limit=100)
        portfolios: list[dict[str, Any]] = []
        by_instrument: dict[str, Decimal] = {}
        by_sector: dict[str, Decimal] = {}
        by_class: dict[str, Decimal] = {}
        total = Decimal("0")
        for record in body.get("portfolios", ()) or ():
            pid = str(record.get("portfolio_id") or "")
            try:
                detail = self._portfolios.detail(pid)
            except Exception:  # noqa: BLE001
                continue
            value = Decimal(str(detail["valuation"]["total_value"]))
            holdings = detail.get("holdings", ()) or ()
            invested = sum(
                (Decimal(str(row.get("market_value") or "0")) for row in holdings),
                Decimal("0"),
            )
            total += value
            portfolios.append(
                {
                    "portfolio_id": pid,
                    "horizon": detail["portfolio"]["horizon"],
                    "status": detail["portfolio"]["status"],
                    "total_value": str(value),
                    "invested": str(invested),
                    "cash": str(detail["valuation"]["cash"]),
                    "cash_weight": str(detail["valuation"]["cash_weight"]),
                }
            )
            for row in holdings:
                item_value = Decimal(str(row.get("market_value") or "0"))
                iid = str(row.get("instrument_id") or "")
                sector = str(row.get("sector") or "unknown")
                asset_class = str(row.get("asset_class") or "unknown")
                by_instrument[iid] = by_instrument.get(iid, Decimal("0")) + item_value
                by_sector[sector] = by_sector.get(sector, Decimal("0")) + item_value
                by_class[asset_class] = by_class.get(asset_class, Decimal("0")) + item_value
        policy = self.policy("aggregate")
        breaches: list[dict[str, Any]] = []
        if total > 0:
            for iid, value in by_instrument.items():
                weight = value / total
                if weight > policy.max_position_weight:
                    breaches.append(
                        {
                            "code": "max_aggregate_position_weight",
                            "instrument_id": iid,
                            "weight": str(weight.quantize(Decimal("0.0001"))),
                            "limit": str(policy.max_position_weight),
                        }
                    )
            for sector, value in by_sector.items():
                weight = value / total
                if weight > policy.max_aggregate_sector_weight:
                    breaches.append(
                        {
                            "code": "max_aggregate_sector_weight",
                            "sector": sector,
                            "weight": str(weight.quantize(Decimal("0.0001"))),
                            "limit": str(policy.max_aggregate_sector_weight),
                        }
                    )
        return {
            "ok": True,
            "total_value": str(total),
            "portfolios": portfolios,
            "by_instrument": {key: str(value) for key, value in by_instrument.items()},
            "by_sector": {key: str(value) for key, value in by_sector.items()},
            "by_asset_class": {key: str(value) for key, value in by_class.items()},
            "sector_weights": {
                key: str((value / total).quantize(Decimal("0.0001"))) if total > 0 else "0"
                for key, value in by_sector.items()
            },
            "policy": policy.to_dict(),
            "breaches": breaches,
            "outcome": (
                RiskOutcome.REJECTED.value
                if any(str(item.get("code", "")).startswith("max_aggregate_position") for item in breaches)
                else (RiskOutcome.RESTRICTED.value if breaches else RiskOutcome.APPROVED.value)
            ),
        }

    # -- stress ---------------------------------------------------------------

    async def stress(
        self, portfolio_id: str, scenario_id: str = "market_decline", *, actor: str = ""
    ) -> dict[str, Any]:
        scenario = SCENARIOS.get(scenario_id)
        if scenario is None:
            raise RiskValidationError(f"unknown stress scenario {scenario_id!r}")
        detail = self._portfolios.detail(portfolio_id)
        if str(scenario.kind) == "market":
            scenario = _custom_sector(scenario, detail)
        holdings = detail.get("holdings", ()) or ()
        before = Decimal(str(detail["valuation"]["total_value"]))
        impact = Decimal("0")
        positions: list[dict[str, Any]] = []
        largest_sector = _largest_sector(holdings)
        largest_instrument = max(
            holdings,
            key=lambda row: Decimal(str(row.get("market_value") or "0")),
            default=None,
        )
        for row in holdings:
            value = Decimal(str(row.get("market_value") or "0"))
            shock = Decimal("0")
            if scenario.kind == "market":
                shock = scenario.shock_pct
            elif scenario.kind == "sector" and str(row.get("sector")) == (
                scenario.sector or largest_sector
            ):
                shock = scenario.shock_pct
            elif scenario.kind == "instrument" and largest_instrument is not None:
                if str(row.get("instrument_id")) == str(largest_instrument.get("instrument_id")):
                    shock = scenario.shock_pct
            change = (value * shock / Decimal("100")).quantize(Decimal("0.01"))
            impact += change
            positions.append(
                {
                    "instrument_id": row.get("instrument_id"),
                    "value": str(value),
                    "shock_pct": str(shock),
                    "impact": str(change),
                }
            )
        after = (before + impact).quantize(Decimal("0.01"))
        drawdown = Decimal("0")
        if before > 0:
            drawdown = (max(Decimal("0"), before - after) / before * Decimal("100")).quantize(
                Decimal("0.01")
            )
        policy = self.policy(str(detail["portfolio"]["horizon"]))
        breaches = []
        report_metrics = self.portfolio_risk(portfolio_id).get("metrics", {})
        projected_volatility: Decimal | None = None
        projected_liquidity: Decimal | None = None
        if scenario.kind == "volatility":
            current_volatility = report_metrics.get("volatility", {}).get("value")
            if current_volatility not in (None, ""):
                projected_volatility = (
                    Decimal(str(current_volatility))
                    * (Decimal("1") + scenario.shock_pct / Decimal("100"))
                ).quantize(Decimal("0.01"))
                if projected_volatility > policy.max_volatility_pct:
                    breaches.append(
                        {
                            "code": "stress_volatility",
                            "severity": ConstraintSeverity.REVIEW.value,
                            "message": (
                                f"projected volatility {projected_volatility}% exceeds "
                                f"{policy.max_volatility_pct}%"
                            ),
                        }
                    )
        if scenario.kind == "liquidity":
            current_liquidity = report_metrics.get("liquidity", {}).get("value")
            if current_liquidity not in (None, ""):
                projected_liquidity = (
                    Decimal(str(current_liquidity))
                    * (Decimal("1") + scenario.shock_pct / Decimal("100"))
                ).quantize(Decimal("0.01"))
                if projected_liquidity < policy.min_liquidity_value:
                    breaches.append(
                        {
                            "code": "stress_liquidity",
                            "severity": ConstraintSeverity.REVIEW.value,
                            "message": (
                                f"projected daily value {projected_liquidity} is below "
                                f"{policy.min_liquidity_value}"
                            ),
                        }
                    )
        if drawdown > policy.max_drawdown_pct:
            breaches.append(
                {
                    "code": "stress_drawdown",
                    "severity": ConstraintSeverity.REVIEW.value,
                    "message": (
                        f"scenario drawdown {drawdown}% exceeds "
                        f"{policy.max_drawdown_pct}%"
                    ),
                }
            )
        stress_id = risk_id(
            "rstress_", portfolio_id, scenario.scenario_id, self._now().isoformat()
        )
        payload = {
            "ok": True,
            "stress_id": stress_id,
            "portfolio_id": portfolio_id,
            "scenario": scenario.to_dict(),
            "before_value": str(before),
            "after_value": str(after),
            "impact": str(impact),
            "impact_pct": (
                str((impact / before * Decimal("100")).quantize(Decimal("0.01")))
                if before > 0
                else "0"
            ),
            "drawdown_pct": str(drawdown),
            "projected_volatility_pct": (
                None if projected_volatility is None else str(projected_volatility)
            ),
            "projected_liquidity": (
                None if projected_liquidity is None else str(projected_liquidity)
            ),
            "positions": positions,
            "breaches": breaches,
            "assumptions": [
                "the shock is applied to market value, not to quantity",
                "a hypothetical scenario is not a forecast",
                "no liquidity, tax or transaction-cost effect is modelled",
            ],
            "hypothetical": True,
            "is_forecast": False,
            "actor": actor,
            "created_at": self._now().isoformat(),
        }
        self._store.save_stress(stress_id, portfolio_id, scenario.scenario_id, payload)
        await self._publish(
            "novamarket.risk.scenario.completed",
            stress_id=stress_id,
            portfolio_id=portfolio_id,
            scenario=scenario.scenario_id,
            impact=str(impact),
        )
        return payload

    def stress_results(self, portfolio_id: str = "", limit: int = 50) -> dict[str, Any]:
        rows = self._store.stress_results(portfolio_id=portfolio_id, limit=limit)
        return {"ok": True, "results": rows, "total": len(rows)}

    # -- governance -----------------------------------------------------------

    def overrides(self, *, decision_id: str = "", limit: int = 100) -> dict[str, Any]:
        rows = self._store.overrides(decision_id=decision_id, limit=limit)
        return {"ok": True, "overrides": rows, "total": len(rows)}

    async def record_override(
        self,
        decision_id: str,
        *,
        actor: str,
        reason: str,
        ttl_hours: float = 24,
    ) -> dict[str, Any]:
        decision = self._store.decision(decision_id)
        if decision is None:
            raise RiskNotFoundError(f"unknown risk decision {decision_id!r}")
        if not actor.strip():
            raise RiskValidationError("an override needs an authorizing actor")
        if not reason.strip():
            raise RiskValidationError("an override needs a reason")
        outcome = str(decision.get("outcome"))
        if outcome not in {
            RiskOutcome.RESTRICTED.value,
            RiskOutcome.REQUIRES_REVIEW.value,
        }:
            raise RiskStateError(
                f"a {outcome} decision cannot be overridden; only restricted or "
                "review-required decisions are overridable"
            )
        ttl = max(1.0, min(float(ttl_hours), 168.0))
        moment = self._now()
        override = RiskOverride(
            override_id=risk_id("rover_", decision_id, actor, moment.isoformat()),
            decision_id=decision_id,
            actor=actor,
            reason=reason,
            created_at=moment,
            expires_at=moment + timedelta(hours=ttl),
            constraint_codes=tuple(
                str(item.get("code")) for item in decision.get("violations", ())
            ),
        )
        self._store.save_override(override)
        await self._publish(
            "novamarket.risk.override.recorded",
            override_id=override.override_id,
            decision_id=decision_id,
            actor=actor,
            expires_at=override.expires_at.isoformat(),
        )
        return {"ok": True, "override": override.to_dict()}

    async def apply_decision(self, decision_id: str) -> dict[str, Any]:
        decision = self._store.decision(decision_id)
        if decision is None:
            raise RiskNotFoundError(f"unknown risk decision {decision_id!r}")
        consumed = self._store.consumption(decision_id)
        if consumed is not None:
            return {"ok": True, "replayed": True, **consumed}
        outcome = str(decision.get("outcome"))
        override = self._store.active_override(decision_id)
        if outcome == RiskOutcome.RESTRICTED.value:
            raise RiskStateError(
                "a restricted decision cannot be applied at full size; submit a "
                "reduced or adjusted proposal and assess it again"
            )
        if outcome != RiskOutcome.APPROVED.value and override is None:
            raise RiskStateError(
                f"decision {decision_id} is {outcome}; an approved decision or a "
                "recorded override is required before applying"
            )
        try:
            expires = datetime.fromisoformat(str(decision["expires_at"]))
        except ValueError:
            expires = self._now()
        if expires <= self._now():
            raise RiskStateError("the risk decision has expired; reassess the action")
        proposal_id = str(decision.get("proposal_id") or "")
        portfolio_id = str(decision.get("portfolio_id") or "")
        if not proposal_id or not portfolio_id:
            raise RiskStateError("the decision is not bound to a portfolio proposal")
        current = self._portfolios.detail(portfolio_id)
        current_policy = self.policy(str(current["portfolio"]["horizon"]))
        if current_policy.version != str(decision.get("policy_version")):
            raise RiskStateError(
                "the risk policy changed since this decision; reassess the proposal"
            )
        proposal = self._proposal(portfolio_id, proposal_id)
        if proposal is None:
            raise RiskNotFoundError(f"unknown proposal {proposal_id!r}")
        version = self._state_version(portfolio_id, proposal, current)
        if version != str(decision.get("state_version")):
            raise RiskStateError(
                "the portfolio or proposal changed since the decision; reassess it"
            )
        result = await self._portfolios.apply(proposal_id)
        self._store.record_consumption(
            decision_id,
            result="applied",
            payload={"proposal_id": proposal_id, "portfolio_id": portfolio_id},
        )
        await self._publish(
            "novamarket.risk.action.applied",
            decision_id=decision_id,
            proposal_id=proposal_id,
            portfolio_id=portfolio_id,
            actor=str(decision.get("actor") or ""),
        )
        return {
            "ok": True,
            "decision_id": decision_id,
            "proposal_id": proposal_id,
            "portfolio_id": portfolio_id,
            "applied": result,
        }

    def decisions(
        self, *, portfolio_id: str = "", outcome: str = "", limit: int = 100
    ) -> dict[str, Any]:
        rows = self._store.decisions(
            portfolio_id=portfolio_id, outcome=outcome, limit=limit
        )
        return {"ok": True, "decisions": rows, "total": len(rows), "limit": limit}

    def decision(self, decision_id: str) -> dict[str, Any]:
        row = self._store.decision(decision_id)
        if row is None:
            raise RiskNotFoundError(f"unknown risk decision {decision_id!r}")
        return row

    # -- observability --------------------------------------------------------

    def status(self) -> dict[str, Any]:
        metrics = self._store.metrics()
        average_latency = (
            round(sum(self._latencies) / len(self._latencies), 3)
            if self._latencies
            else 0.0
        )
        return {
            "initialised": True,
            "phase": "NM-08",
            "schema_version": self._store.schema_version,
            "policy_version": POLICY_VERSION,
            "policies": [
                self.policy(policy_id).to_dict() for policy_id in POLICY_DEFAULTS
            ],
            "metrics": metrics,
            "average_assessment_ms": average_latency,
            "last_error": self._last_error,
        }

    def contract(self) -> dict[str, Any]:
        return {
            "implemented": True,
            "phase": "NM-08",
            "version": "nm08-risk-1.0",
            "outcomes": [outcome.value for outcome in RiskOutcome],
            "categories": [category.value for category in RiskCategory],
            "actions": sorted(_ACTIONS),
            "policies": {
                policy_id: policy.to_dict()
                for policy_id, policy in POLICY_DEFAULTS.items()
            },
            "metrics": [
                "annualized_volatility_pct",
                "max_drawdown_pct",
                "current_drawdown_pct",
                "var_95_pct",
                "cvar_95_pct",
                "sharpe_ratio",
                "sortino_ratio",
                "beta",
                "correlation",
                "concentration_hhi",
                "capital_utilization",
                "turnover",
                "average_daily_value",
            ],
            "events": [
                "novamarket.risk.assessment.requested",
                "novamarket.risk.assessment.completed",
                "novamarket.risk.action.approved",
                "novamarket.risk.action.rejected",
                "novamarket.risk.action.restricted",
                "novamarket.risk.review.required",
                "novamarket.risk.insufficient_data",
                "novamarket.risk.limit.breached",
                "novamarket.risk.alert.created",
                "novamarket.risk.alert.acknowledged",
                "novamarket.risk.alert.resolved",
                "novamarket.risk.scenario.completed",
                "novamarket.risk.policy.updated",
                "novamarket.risk.override.recorded",
                "novamarket.risk.action.applied",
            ],
            "guarantees": [
                "a failed assessment is never an approval",
                "insufficient data produces insufficient_data, never approved",
                "a decision is bound to the exact proposal and portfolio state",
                "a rejected decision cannot be applied and cannot be overridden",
                "the portfolio manager cannot approve its own action",
                "no order is placed and no broker exists in this build",
            ],
            "executes_orders": False,
            "real_money": False,
        }


# -- helpers ------------------------------------------------------------------


def _optional_decimal(value: object | None) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return dec(value, field_name="notional")
    except ValueError as exc:
        raise RiskValidationError("notional must be a decimal number") from exc


def _position_rows(detail: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if detail is None:
        return []
    return [dict(row) for row in detail.get("holdings", ()) or ()]


def _fractions(projected: Sequence[Mapping[str, Any]]) -> tuple[Decimal, Decimal]:
    invested = sum(
        (Decimal(str(row.get("weight") or "0")) for row in projected), Decimal("0")
    )
    invested = max(Decimal("0"), min(Decimal("1"), invested))
    return invested.quantize(Decimal("0.0001")), (Decimal("1") - invested).quantize(
        Decimal("0.0001")
    )


def _explain(
    outcome: RiskOutcome,
    evaluation: Any,
    snapshot: RiskSnapshot,
    policy: RiskPolicy,
) -> str:
    head = (
        f"{outcome.value.upper()}: {len(snapshot.projected)} position(s), "
        f"invested {snapshot.invested_fraction}, cash {snapshot.cash_weight}, "
        f"drawdown {snapshot.drawdown_pct}%, "
        f"volatility {snapshot.volatility_pct if snapshot.volatility_pct is not None else 'unmeasured'}"
        f"; policy {policy.policy_id}@{policy.version}."
    )
    if evaluation.breaches:
        text = head + " Triggered: " + "; ".join(
            f"{item.code} ({item.message})" for item in evaluation.breaches
        ) + "."
        if evaluation.insufficient:
            text += " Also unavailable: " + "; ".join(evaluation.insufficient) + "."
        return text
    if evaluation.insufficient:
        return head + " Not approved because: " + "; ".join(evaluation.insufficient) + "."
    if evaluation.warnings:
        return head + " Warnings: " + "; ".join(
            item.message for item in evaluation.warnings
        ) + "."
    return head + " No constraint breached."


def _replace_policy(policy: RiskPolicy, name: str, value: Any) -> RiskPolicy:
    from dataclasses import replace

    return replace(policy, **{name: value})


def _largest_sector(holdings: Sequence[Mapping[str, Any]]) -> str:
    totals: dict[str, Decimal] = {}
    for row in holdings:
        sector = str(row.get("sector") or "unknown")
        totals[sector] = totals.get(sector, Decimal("0")) + Decimal(
            str(row.get("market_value") or "0")
        )
    if not totals:
        return ""
    return max(totals, key=lambda key: totals[key])


def _custom_sector(scenario: StressScenario, detail: Mapping[str, Any]) -> StressScenario:
    sector = _largest_sector(detail.get("holdings", ()) or ())
    if not sector:
        return scenario
    return StressScenario(
        scenario_id=scenario.scenario_id,
        name=scenario.name,
        kind=scenario.kind,
        shock_pct=scenario.shock_pct,
        description=scenario.description,
        sector=sector,
    )


def evaluation_rows(projected: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "instrument_id": str(row.get("instrument_id")),
            "weight": str(row.get("weight")),
            "sector": str(row.get("sector") or "unknown"),
            "asset_class": str(row.get("asset_class") or "unknown"),
            "value": str(row.get("value")),
        }
        for row in projected
    ]


__all__ = ["RiskEngine"]
