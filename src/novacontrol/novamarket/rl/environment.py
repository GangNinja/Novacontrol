"""Deterministic, isolated historical trading environment for PI-13.

A decision made after a completed bar at T is simulated no earlier than the
NEXT completed bar's open.  The environment never fills at the decision bar's
close, never assumes intraday depth from daily OHLCV, and owns only its private
virtual cash and position.  There is no NM-07, NM-08 or NM-10 dependency here:
the NM-10 *pricing functions* are reused, while its stores and order methods are
not imported.
"""

from __future__ import annotations

import hashlib
import statistics
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from novacontrol.novamarket.backtesting.core import HistoricalBar
from novacontrol.novamarket.execution.models import MarketSnapshot, OrderSide
from novacontrol.novamarket.execution.pricing import execution_price, fee_breakdown
from novacontrol.novamarket.marketdata.calendar import calendar_by_mic

from .models import (
    ActionType,
    EpisodeStatus,
    ExecutionStatus,
    RLAction,
    RLDataError,
    RLEnvironmentConfig,
    RLEpisodeError,
    RLExecution,
    RLInvalidActionError,
    RLObservation,
    RLPortfolioState,
    RLPositionState,
    RLReward,
    RLTransition,
    RLValidationError,
    TerminationReason,
    decimal_text,
    money,
    parse_time,
    quantity,
)
from .observation import ObservationBuilder, validate_action_shape


def _timeline_signature(bars: Sequence[HistoricalBar]) -> str:
    def token(item: HistoricalBar) -> str:
        available = item.available_at.isoformat() if item.available_at else ""
        retrieved = item.retrieved_at.isoformat() if item.retrieved_at else ""
        return "|".join(
            (
                item.instrument_id,
                item.observed_at.isoformat(),
                available,
                retrieved,
                str(item.open),
                str(item.high),
                str(item.low),
                str(item.close),
                str(item.volume),
                item.source,
                str(item.adjusted),
                str(item.tradable),
            )
        )

    payload = "\n".join(token(item) for item in bars)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mic(instrument_id: str) -> str:
    return str(instrument_id).split(":", 1)[0].strip().upper()


class RewardInterface:
    """Expose transition-derived reward components, never a policy formula.

    ``total`` is deliberately zero.  PI-14 owns weights and a reward policy;
    inventing one here would create an authoritative training signal by accident.
    """

    def evaluate(
        self,
        *,
        action: RLAction,
        execution: RLExecution,
        before: RLPortfolioState,
        after: RLPortfolioState,
        observation: RLObservation,
        next_observation: RLObservation,
    ) -> RLReward:
        realized_delta = after.realized_pnl - before.realized_pnl
        unrealized_delta = after.unrealized_pnl - before.unrealized_pnl
        forecast = next_observation.forecasts
        direction = str(forecast.get("direction") or "unavailable")
        consistency = "unavailable"
        if action.kind is ActionType.BUY and direction in ("up", "down"):
            consistency = "aligned" if direction == "up" else "opposed"
        elif action.kind is ActionType.SELL and direction in ("up", "down"):
            consistency = "aligned" if direction == "down" else "opposed"
        elif action.kind in (ActionType.HOLD, ActionType.WAIT, ActionType.NO_ACTION):
            consistency = "neutral"
        trading = action.kind.is_trade and execution.status in (
            ExecutionStatus.FILLED,
            ExecutionStatus.PARTIAL,
        )
        components: dict[str, Any] = {
            "realized_pnl_delta": decimal_text(money(realized_delta)),
            "unrealized_pnl_delta": decimal_text(money(unrealized_delta)),
            "equity_return": (
                float((after.equity - before.equity) / before.equity) if before.equity else None
            ),
            "risk_exposure": decimal_text(after.exposure),
            "drawdown": decimal_text(after.drawdown),
            "transaction_cost": decimal_text(after.fees_paid - before.fees_paid),
            "slippage_cost": decimal_text(execution.slippage_amount),
            "decision_consistency": consistency,
            "forecast_direction": direction,
            "forecast_validity": str(forecast.get("validity") or "unavailable"),
            "forecast_uncertainty": str(forecast.get("uncertainty") or "unknown"),
            # A trade attempt that never filled is the observable no-value case;
            # PI-14 may safely refine the policy meaning of "unnecessary".
            "unnecessary_trading": 1 if action.kind.is_trade and execution.quantity == 0 else 0,
            "constraint_violations": 1 if execution.status is ExecutionStatus.REJECTED else 0,
            "traded": trading,
            "observation_id": observation.observation_id,
            "next_observation_id": next_observation.observation_id,
        }
        return RLReward(total=Decimal("0"), components=components)


class RLEnvironment:
    """One instance-owned simulated book, timeline cursor and transition log."""

    def __init__(
        self,
        config: RLEnvironmentConfig,
        bars: Sequence[HistoricalBar],
        *,
        feature_snapshots: Mapping[datetime, Mapping[str, Any]] | None = None,
        forecast_snapshots: Mapping[datetime, Mapping[str, Any]] | None = None,
        clock: Any = None,
    ) -> None:
        if len(config.instruments) != 1:
            raise RLValidationError("PI-13 starts with exactly one instrument per environment")
        self._config = config
        self._clock = clock or (lambda: datetime.now(UTC))
        self._reward = RewardInterface()
        self._builder = ObservationBuilder(config)
        self._episode_id = ""
        self._status = EpisodeStatus.CREATED
        self._termination_reason = ""
        self._index = 0
        self._steps = 0
        self._sequence = 0
        self._invalid_actions = 0
        self._warnings: list[str] = []
        self._bars = self._prepare_bars(bars)
        if len(self._bars) < 2:
            raise RLDataError("an episode needs at least two completed bars for a next-bar fill")
        self._features = self._normalise_snapshots(feature_snapshots, "feature")
        self._forecasts = self._normalise_snapshots(forecast_snapshots, "forecast")
        self._timeline_signature = _timeline_signature(self._bars)
        self._cash = config.initial_capital
        self._position_quantity = Decimal("0")
        self._average_entry = Decimal("0.00")
        self._realized_pnl = Decimal("0.00")
        self._fees_paid = Decimal("0.00")
        self._peak_equity = config.initial_capital
        self._portfolio = self._mark(self._bars[0])
        self._observation: RLObservation | None = None
        self._last_transition: RLTransition | None = None
        self._last_observation_seconds = 0.0
        self._error = ""

    # -- read surfaces --------------------------------------------------------
    @property
    def config(self) -> RLEnvironmentConfig:
        return self._config

    @property
    def episode_id(self) -> str:
        return self._episode_id

    @property
    def status(self) -> EpisodeStatus:
        return self._status

    @property
    def observation(self) -> RLObservation:
        if self._observation is None:
            raise RLEpisodeError("episode has not been reset")
        return self._observation

    @property
    def portfolio(self) -> RLPortfolioState:
        return self._portfolio

    @property
    def steps(self) -> int:
        return self._steps

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def invalid_actions(self) -> int:
        return self._invalid_actions

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(self._warnings)

    @property
    def last_transition(self) -> RLTransition | None:
        return self._last_transition

    @property
    def current_timestamp(self) -> datetime | None:
        return self._bars[self._index].observed_at if self._bars else None

    @property
    def timeline_length(self) -> int:
        return len(self._bars)

    @property
    def feature_set_version(self) -> str:
        return (
            str(self._observation.provenance.get("feature_set_version", "unavailable"))
            if self._observation
            else "unavailable"
        )

    @property
    def last_observation_seconds(self) -> float:
        return self._last_observation_seconds

    # -- lifecycle -----------------------------------------------------------
    def reset(self, episode_id: str | None = None) -> RLObservation:
        if episode_id is not None:
            value = str(episode_id).strip()
            if not value:
                raise RLValidationError("episode_id cannot be empty")
            self._episode_id = value
        if not self._episode_id:
            raise RLValidationError("episode_id is required before reset")
        self._cash = self._config.initial_capital
        self._position_quantity = Decimal("0")
        self._average_entry = Decimal("0.00")
        self._realized_pnl = Decimal("0.00")
        self._fees_paid = Decimal("0.00")
        self._peak_equity = self._config.initial_capital
        self._index = 0
        self._steps = 0
        self._sequence = 0
        self._invalid_actions = 0
        self._last_transition = None
        self._error = ""
        self._termination_reason = ""
        self._status = EpisodeStatus.RUNNING
        self._portfolio = self._mark(self._bars[0])
        self._observation = self._build_observation(self._bars[0], self._index, "initial")
        return self._observation

    def step(
        self, action: RLAction | Mapping[str, Any]
    ) -> tuple[RLObservation, RLReward, bool, bool, RLTransition]:
        if self._status is not EpisodeStatus.RUNNING:
            raise RLEpisodeError(f"episode is {self._status.value}, not running")
        if self._observation is None:
            raise RLEpisodeError("episode has not been reset")
        try:
            parsed = action if isinstance(action, RLAction) else RLAction.from_mapping(action)
            validate_action_shape(parsed.to_dict())
            self._validate_action(parsed)
        except RLInvalidActionError:
            self._invalid_actions += 1
            raise
        except RLValidationError as exc:
            self._invalid_actions += 1
            raise RLInvalidActionError(str(exc)) from exc

        observed_at = self._bars[self._index].observed_at
        before = self._portfolio
        if self._index >= len(self._bars) - 1:
            execution = RLExecution(
                status=ExecutionStatus.NO_NEXT_BAR,
                side="none",
                quantity=Decimal("0"),
                reference_price=self._bars[self._index].close,
                execution_price=self._bars[self._index].close,
                notional=Decimal("0"),
                slippage_amount=Decimal("0"),
                fees=Decimal("0"),
                fee_components={},
                at=None,
                reason="no later completed bar exists for a simulated fill",
            )
            reward = self._reward.evaluate(
                action=parsed,
                execution=execution,
                before=before,
                after=before,
                observation=self._observation,
                next_observation=self._observation,
            )
            self._terminal(TerminationReason.HISTORICAL_DATA_EXHAUSTED)
            transition = self._transition(
                sequence=self._sequence + 1,
                at=self._bars[self._index].observed_at,
                action=parsed,
                execution=execution,
                reward=reward,
                next_observation=self._observation,
                before=before,
                after=before,
                terminated=True,
                truncated=False,
                reason=TerminationReason.HISTORICAL_DATA_EXHAUSTED,
            )
            self._record_transition(transition)
            self._steps += 1
            return self._observation, reward, True, False, transition

        next_bar = self._bars[self._index + 1]
        execution = self._execute(parsed, next_bar)
        if execution.status in (ExecutionStatus.FILLED, ExecutionStatus.PARTIAL):
            self._apply_execution(parsed, execution)
        self._index += 1
        self._steps += 1
        self._portfolio = self._mark(next_bar)
        next_observation = self._build_observation(
            next_bar, self._index, "decision_after_completed_bar"
        )
        reward = self._reward.evaluate(
            action=parsed,
            execution=execution,
            before=before,
            after=self._portfolio,
            observation=self._observation,
            next_observation=next_observation,
        )
        terminated = False
        truncated = False
        reason = ""
        if next_bar.observed_at >= self._config.end or self._index >= len(self._bars) - 1:
            terminated = True
            reason = TerminationReason.HISTORICAL_DATA_EXHAUSTED.value
        elif self._steps >= self._config.max_steps:
            truncated = True
            reason = TerminationReason.STEP_LIMIT.value
        transition = self._transition(
            sequence=self._sequence + 1,
            at=observed_at,
            action=parsed,
            execution=execution,
            reward=reward,
            next_observation=next_observation,
            before=before,
            after=self._portfolio,
            terminated=terminated,
            truncated=truncated,
            reason=reason,
        )
        self._record_transition(transition)
        self._observation = next_observation
        if terminated:
            self._terminal(TerminationReason(reason))
        elif truncated:
            self._status = EpisodeStatus.TRUNCATED
            self._termination_reason = reason
        return next_observation, reward, terminated, truncated, transition

    def cancel(self) -> dict[str, Any]:
        if self._status in (
            EpisodeStatus.TERMINATED,
            EpisodeStatus.TRUNCATED,
            EpisodeStatus.CANCELLED,
        ):
            return self.state()
        if self._status is EpisodeStatus.FAILED:
            raise RLEpisodeError("a failed episode cannot be cancelled into a success state")
        self._status = EpisodeStatus.CANCELLED
        self._termination_reason = TerminationReason.CANCELLED.value
        return self.state()

    def fail(self, reason: str) -> None:
        self._status = EpisodeStatus.FAILED
        self._error = str(reason)[:1_000]
        self._termination_reason = TerminationReason.ENVIRONMENT_FAILURE.value

    # -- snapshot / recovery -------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "environment_id": self._config.environment_id,
            "episode_id": self._episode_id,
            "config_hash": self._config.config_hash,
            "environment_version": self._config.environment_version,
            "timeline_signature": self._timeline_signature,
            "timeline_length": len(self._bars),
            "index": self._index,
            "steps": self._steps,
            "sequence": self._sequence,
            "invalid_actions": self._invalid_actions,
            "status": self._status.value,
            "termination_reason": self._termination_reason,
            "cash": decimal_text(self._cash),
            "position_quantity": decimal_text(self._position_quantity),
            "average_entry": decimal_text(self._average_entry),
            "realized_pnl": decimal_text(self._realized_pnl),
            "fees_paid": decimal_text(self._fees_paid),
            "peak_equity": decimal_text(self._peak_equity),
            "warnings": list(self._warnings),
            "error": self._error,
            "feature_set_version": self.feature_set_version,
            "last_transition_id": self._last_transition.transition_id
            if self._last_transition
            else "",
        }

    def restore(self, snapshot: Mapping[str, Any]) -> RLObservation:
        if str(snapshot.get("environment_id")) != self._config.environment_id:
            raise RLDataError("stored episode belongs to another environment")
        if str(snapshot.get("config_hash")) != self._config.config_hash:
            raise RLDataError("environment configuration changed since the episode was persisted")
        if str(snapshot.get("timeline_signature")) != self._timeline_signature:
            raise RLDataError("historical timeline changed since the episode was persisted")
        if int(snapshot.get("timeline_length", -1)) != len(self._bars):
            raise RLDataError("historical timeline length changed since the episode was persisted")
        index = int(snapshot.get("index", -1))
        if not 0 <= index < len(self._bars):
            raise RLDataError("stored episode cursor is outside the historical timeline")
        episode_id = str(snapshot.get("episode_id") or "").strip()
        if not episode_id:
            raise RLDataError("stored episode has no identity")
        self._episode_id = episode_id
        self._index = index
        self._steps = int(snapshot.get("steps", 0))
        self._sequence = int(snapshot.get("sequence", 0))
        self._invalid_actions = int(snapshot.get("invalid_actions", 0))
        try:
            self._status = EpisodeStatus(str(snapshot.get("status", "running")))
        except ValueError as exc:
            raise RLDataError("stored episode has an unknown status") from exc
        self._termination_reason = str(snapshot.get("termination_reason") or "")
        self._cash = money(snapshot.get("cash"))
        self._position_quantity = quantity(snapshot.get("position_quantity"))
        self._average_entry = money(snapshot.get("average_entry"))
        self._realized_pnl = money(snapshot.get("realized_pnl"))
        self._fees_paid = money(snapshot.get("fees_paid"))
        self._peak_equity = money(snapshot.get("peak_equity"))
        self._warnings = [str(item) for item in snapshot.get("warnings") or ()]
        self._error = str(snapshot.get("error") or "")
        if self._position_quantity < 0:
            raise RLDataError("stored episode has a negative long-only position")
        if self._cash < 0:
            raise RLDataError("stored episode has negative virtual cash")
        self._portfolio = self._mark(self._bars[self._index])
        if self._status is EpisodeStatus.RUNNING:
            self._observation = self._build_observation(
                self._bars[self._index], self._index, "recovered"
            )
        else:
            self._observation = self._build_observation(
                self._bars[self._index], self._index, "final"
            )
        return self._observation

    def state(self) -> dict[str, Any]:
        observation = self._observation
        return {
            "environment_id": self._config.environment_id,
            "episode_id": self._episode_id,
            "status": self._status.value,
            "current_timestamp": observation.timestamp.isoformat() if observation else None,
            "step_count": self._steps,
            "transition_count": self._sequence,
            "invalid_actions": self._invalid_actions,
            "portfolio": self._portfolio.to_dict(),
            "observation": observation.to_dict() if observation else None,
            "terminated": self._status is EpisodeStatus.TERMINATED,
            "truncated": self._status is EpisodeStatus.TRUNCATED,
            "termination_reason": self._termination_reason,
            "error": self._error,
            "warnings": list(self._warnings),
            "snapshot": self.snapshot(),
            "summary": self.summary(),
            "simulated": True,
            "advisory_only": True,
            "production_authority": False,
        }

    def summary(self) -> dict[str, Any]:
        equity = self._portfolio.equity
        total_return = (
            (equity / self._config.initial_capital - Decimal("1"))
            if self._config.initial_capital
            else Decimal("0")
        )
        return {
            "status": self._status.value,
            "steps": self._steps,
            "transitions": self._sequence,
            "invalid_actions": self._invalid_actions,
            "initial_capital": decimal_text(self._config.initial_capital),
            "final_equity": decimal_text(equity),
            "total_return": decimal_text(total_return),
            "total_return_pct": float(total_return * Decimal("100")),
            "realized_pnl": decimal_text(self._portfolio.realized_pnl),
            "unrealized_pnl": decimal_text(self._portfolio.unrealized_pnl),
            "fees_paid": decimal_text(self._portfolio.fees_paid),
            "max_drawdown": decimal_text(self._portfolio.drawdown),
            "exposure": decimal_text(self._portfolio.exposure),
            "termination_reason": self._termination_reason,
            "feature_set_version": self.feature_set_version,
            "dataset_version": self._config.dataset_version,
            "environment_version": self._config.environment_version,
            "simulation_config_hash": self._config.config_hash,
            "complete": self._status
            in (
                EpisodeStatus.TERMINATED,
                EpisodeStatus.TRUNCATED,
                EpisodeStatus.CANCELLED,
            ),
            "not_live_performance": True,
        }

    # -- action validation and execution ------------------------------------
    def validate_action(self, action: RLAction | Mapping[str, Any]) -> RLAction:
        parsed = action if isinstance(action, RLAction) else RLAction.from_mapping(action)
        self._validate_action(parsed)
        return parsed

    def _validate_action(self, action: RLAction) -> None:
        if self._status is not EpisodeStatus.RUNNING:
            raise RLInvalidActionError(f"episode is {self._status.value}, not running")
        if action.instrument_id and action.instrument_id != self._config.instrument_id:
            raise RLInvalidActionError("action instrument is outside the environment universe")
        if action.kind.is_trade and not action.instrument_id:
            raise RLInvalidActionError("trading actions must name the environment instrument")
        if action.notional is not None and action.notional > self._config.max_order_notional:
            raise RLInvalidActionError("action notional exceeds the configured maximum")
        if action.quantity is not None:
            current = self._bars[self._index].close
            if action.quantity * current > self._config.max_order_notional * Decimal("1.000001"):
                raise RLInvalidActionError(
                    "action quantity exceeds the configured notional maximum"
                )
        if (
            action.size_fraction is not None
            and action.size_fraction > self._config.max_position_weight
        ):
            raise RLInvalidActionError(
                "size_fraction exceeds the configured position-weight maximum"
            )
        if action.kind is ActionType.BUY:
            if self._cash <= 0 or self._portfolio.equity <= 0:
                raise RLInvalidActionError("no virtual cash is available")
            exposure_limit = min(self._config.max_position_weight, self._config.max_gross_exposure)
            if self._current_position_value() >= self._portfolio.equity * exposure_limit:
                raise RLInvalidActionError("position/exposure boundary would be exceeded")
        if action.kind is ActionType.SELL:
            if self._position_quantity <= 0:
                raise RLInvalidActionError("no simulated position exists to sell")
            if action.quantity is not None and action.quantity > self._position_quantity:
                raise RLInvalidActionError(
                    "sell quantity exceeds the simulated position (no short selling)"
                )
            if action.notional is not None:
                current = self._bars[self._index].close
                if action.notional > self._position_quantity * current * Decimal("1.000001"):
                    raise RLInvalidActionError(
                        "sell notional exceeds the simulated position (no short selling)"
                    )
            if action.size_fraction is not None and action.size_fraction > Decimal("1"):
                raise RLInvalidActionError("sell size_fraction cannot exceed 1")

    def _execute(self, action: RLAction, bar: HistoricalBar) -> RLExecution:
        if action.kind in (ActionType.HOLD, ActionType.WAIT, ActionType.NO_ACTION):
            return RLExecution(
                status=ExecutionStatus.NOT_APPLICABLE,
                side="none",
                quantity=Decimal("0"),
                reference_price=bar.open,
                execution_price=bar.open,
                notional=Decimal("0"),
                slippage_amount=Decimal("0"),
                fees=Decimal("0"),
                fee_components={},
                at=bar.observed_at,
                reason=f"{action.kind.value} leaves the simulated book unchanged",
            )
        if not bar.tradable:
            return self._rejection(bar, "trading is halted on the next historical bar")
        current = self._bars[self._index].close
        if current <= 0:
            return self._rejection(bar, "the decision bar has no positive close price")
        desired = self._desired_quantity(action, current)
        if desired <= 0:
            return self._rejection(bar, "the action resolves to zero quantity")
        config = self._config.execution
        liquidity = quantity(bar.volume * config.participation_rate)
        if liquidity <= 0:
            return self._rejection(bar, "no simulated liquidity is available on the next bar")
        fill_quantity = min(desired, liquidity)
        status = ExecutionStatus.PARTIAL if fill_quantity < desired else ExecutionStatus.FILLED
        volatility = self._volatility()
        snapshot = MarketSnapshot(
            instrument_id=bar.instrument_id,
            reference_price=bar.open,
            observed_at=bar.observed_at,
            source=bar.source,
            volume=bar.volume,
            available_quantity=liquidity,
            volatility_pct=Decimal(str(volatility)),
            trading_status="open",
            reasons=("daily OHLCV has no order-book depth; fill is an approximation",),
        )
        side = OrderSide.BUY if action.kind is ActionType.BUY else OrderSide.SELL
        try:
            price, _per_unit, _pct = execution_price(
                snapshot,
                side=side,
                order_type="market",  # type: ignore[arg-type]
                quantity=fill_quantity,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a rejected transition
            return self._rejection(bar, f"pricing refused: {type(exc).__name__}: {exc}")
        notional = money(fill_quantity * price)
        if notional > self._config.max_order_notional:
            capped = quantity(self._config.max_order_notional / price)
            if capped <= 0:
                return self._rejection(bar, "the configured order boundary permits no fill")
            fill_quantity = capped
            notional = money(fill_quantity * price)
            status = ExecutionStatus.PARTIAL
        if action.kind is ActionType.BUY:
            # Re-check the position/exposure boundary using the simulated fill price.
            projected = self._current_position_value(bar.open) + notional
            exposure_limit = min(self._config.max_position_weight, self._config.max_gross_exposure)
            if projected > self._portfolio.equity * exposure_limit * Decimal("1.000001"):
                return self._rejection(
                    bar, "simulated fill would exceed the position/exposure boundary"
                )
        fees_map = fee_breakdown(notional, side, config)
        fees = money(fees_map["total"])
        if action.kind is ActionType.BUY and notional + fees > self._cash:
            return self._rejection(bar, "virtual cash is insufficient after simulated costs")
        slippage = money(abs(price - bar.open) * fill_quantity)
        return RLExecution(
            status=status,
            side=side.value,
            quantity=fill_quantity,
            reference_price=bar.open,
            execution_price=price,
            notional=notional,
            slippage_amount=slippage,
            fees=fees,
            fee_components=fees_map,
            at=bar.observed_at,
            reason="",
        )

    def _desired_quantity(self, action: RLAction, current_price: Decimal) -> Decimal:
        if action.kind is ActionType.SELL:
            if action.quantity is not None:
                return quantity(action.quantity)
            if action.notional is not None:
                return quantity(action.notional / current_price)
            if action.size_fraction is not None:
                return quantity(self._position_quantity * action.size_fraction)
            return self._position_quantity
        if action.quantity is not None:
            return quantity(action.quantity)
        if action.notional is not None:
            return quantity(min(action.notional, self._config.max_order_notional) / current_price)
        if action.size_fraction is not None:
            return quantity(self._portfolio.equity * action.size_fraction / current_price)
        budget = min(self._cash, self._config.max_order_notional)
        return quantity(budget / current_price)

    def _apply_execution(self, action: RLAction, execution: RLExecution) -> None:
        if action.kind is ActionType.BUY:
            old_cost = self._position_quantity * self._average_entry
            self._cash = money(self._cash - execution.notional - execution.fees)
            self._position_quantity = quantity(self._position_quantity + execution.quantity)
            self._average_entry = money(
                (old_cost + execution.notional) / self._position_quantity
                if self._position_quantity
                else Decimal("0")
            )
        else:
            self._cash = money(self._cash + execution.notional - execution.fees)
            self._realized_pnl = money(
                self._realized_pnl
                + execution.quantity * (execution.execution_price - self._average_entry)
                - execution.fees
            )
            self._position_quantity = quantity(self._position_quantity - execution.quantity)
            if self._position_quantity == 0:
                self._average_entry = Decimal("0.00")
        self._fees_paid = money(self._fees_paid + execution.fees)

    def _rejection(self, bar: HistoricalBar, reason: str) -> RLExecution:
        return RLExecution(
            status=ExecutionStatus.REJECTED,
            side="none",
            quantity=Decimal("0"),
            reference_price=bar.open,
            execution_price=bar.open,
            notional=Decimal("0"),
            slippage_amount=Decimal("0"),
            fees=Decimal("0"),
            fee_components={},
            at=bar.observed_at,
            reason=reason,
        )

    # -- private helpers -----------------------------------------------------
    def _prepare_bars(self, bars: Sequence[HistoricalBar]) -> list[HistoricalBar]:
        if len(bars) > 20_000:
            raise RLDataError("historical data exceeds the 20,000-bar environment limit")
        selected = [
            item
            for item in bars
            if item.instrument_id == self._config.instrument_id
            and self._config.start <= item.observed_at <= self._config.end
        ]
        selected.sort(key=lambda item: item.observed_at)
        if len({item.observed_at for item in selected}) != len(selected):
            raise RLDataError("historical data contains duplicate timestamps for one instrument")
        kept: list[HistoricalBar] = []
        for bar in selected:
            if bar.available_at is None or bar.available_at > bar.observed_at:
                raise RLDataError("historical bar was not available at its observation time")
            if min(bar.open, bar.high, bar.low, bar.close) <= 0:
                raise RLDataError(
                    f"historical bar has a non-positive price at {bar.observed_at.isoformat()}"
                )
            if bar.volume < 0:
                raise RLDataError(
                    f"historical bar has negative volume at {bar.observed_at.isoformat()}"
                )
            calendar = calendar_by_mic(_mic(bar.instrument_id))
            if calendar is not None:
                try:
                    local = calendar.local(bar.observed_at)
                    if calendar.coverage(bar.observed_at) and not calendar.is_trading_day(
                        local.date()
                    ):
                        self._warn(f"closed-session bar skipped at {bar.observed_at.isoformat()}")
                        continue
                except Exception as exc:  # noqa: BLE001 - calendar is advisory
                    self._warn(f"calendar check unavailable: {type(exc).__name__}")
            if not bar.tradable:
                self._warn(f"trading halt recorded at {bar.observed_at.isoformat()}")
            kept.append(bar)
        if not kept:
            raise RLDataError("no completed historical bars fall inside the environment window")
        if kept[0].observed_at < self._config.start or kept[-1].observed_at > self._config.end:
            raise RLDataError("prepared timeline escaped the configured window")
        return kept

    def _warn(self, message: str) -> None:
        if len(self._warnings) < 20:
            self._warnings.append(message)
        elif len(self._warnings) == 20:
            self._warnings.append("additional data warnings omitted")

    def _normalise_snapshots(
        self, snapshots: Mapping[datetime, Mapping[str, Any]] | None, label: str
    ) -> dict[datetime, Mapping[str, Any]]:
        result: dict[datetime, Mapping[str, Any]] = {}
        for key, value in dict(snapshots or {}).items():
            at = parse_time(key, f"{label} snapshot key")
            if not isinstance(value, Mapping):
                raise RLDataError(f"{label} snapshot at {at.isoformat()} is not an object")
            result[at] = dict(value)
        return result

    def _build_observation(self, bar: HistoricalBar, index: int, phase: str) -> RLObservation:
        started = time.perf_counter()
        try:
            history = self._bars[: index + 1]
            return self._builder.build(
                environment_id=self._config.environment_id,
                episode_id=self._episode_id,
                timestamp=bar.observed_at,
                bar=bar,
                history=history,
                portfolio=self._portfolio,
                feature_snapshot=self._features.get(bar.observed_at),
                forecast_snapshot=self._forecasts.get(bar.observed_at),
                phase=phase,
            )
        finally:
            self._last_observation_seconds = time.perf_counter() - started

    def _mark(self, bar: HistoricalBar) -> RLPortfolioState:
        positions: tuple[RLPositionState, ...]
        if self._position_quantity > 0:
            market_value = money(self._position_quantity * bar.close)
            unrealized = money(self._position_quantity * (bar.close - self._average_entry))
            position = RLPositionState(
                instrument_id=self._config.instrument_id,
                quantity=self._position_quantity,
                average_entry_price=self._average_entry,
                last_price=bar.close,
                market_value=market_value,
                unrealized_pnl=unrealized,
            )
            positions = (position,)
        else:
            market_value = Decimal("0.00")
            unrealized = Decimal("0.00")
            positions = ()
        equity = money(self._cash + market_value)
        self._peak_equity = max(self._peak_equity, equity)
        drawdown = (
            money(min(Decimal("0"), (equity - self._peak_equity) / self._peak_equity))
            if self._peak_equity > 0
            else Decimal("0.00")
        )
        exposure = money(market_value / equity) if equity > 0 else Decimal("0.00")
        return RLPortfolioState(
            initial_capital=self._config.initial_capital,
            cash=money(self._cash),
            positions=positions,
            realized_pnl=money(self._realized_pnl),
            unrealized_pnl=unrealized,
            fees_paid=money(self._fees_paid),
            equity=equity,
            exposure=exposure,
            drawdown=drawdown,
            peak_equity=money(self._peak_equity),
        )

    def _current_position_value(self, price: Decimal | None = None) -> Decimal:
        value = price if price is not None else self._bars[self._index].close
        return money(self._position_quantity * value)

    def _volatility(self) -> float:
        closes = [item.close for item in self._bars[max(0, self._index - 20) : self._index + 1]]
        returns = [
            float(closes[i] / closes[i - 1] - Decimal("1"))
            for i in range(1, len(closes))
            if closes[i - 1]
        ]
        return statistics.pstdev(returns) if len(returns) > 1 else 0.0

    def _terminal(self, reason: TerminationReason) -> None:
        self._status = EpisodeStatus.TERMINATED
        self._termination_reason = reason.value

    def _transition(
        self,
        *,
        sequence: int,
        at: datetime,
        action: RLAction,
        execution: RLExecution,
        reward: RLReward,
        next_observation: RLObservation,
        before: RLPortfolioState,
        after: RLPortfolioState,
        terminated: bool,
        truncated: bool,
        reason: str,
    ) -> RLTransition:
        assert self._observation is not None
        return RLTransition(
            transition_id=f"rl_tx_{self._episode_id}_{sequence:06d}",
            environment_id=self._config.environment_id,
            episode_id=self._episode_id,
            sequence=sequence,
            timestamp=at,
            observation=self._observation.to_dict(),
            action=action.to_dict(),
            execution=execution.to_dict(),
            reward=reward.to_dict(),
            next_observation=next_observation.to_dict(),
            terminated=terminated,
            truncated=truncated,
            termination_reason=reason,
            dataset_version=self._config.dataset_version,
            feature_set_version=self.feature_set_version,
            environment_version=self._config.environment_version,
            simulation_config_hash=self._config.config_hash,
            portfolio_before=before.to_dict(),
            portfolio_after=after.to_dict(),
        )

    def _record_transition(self, transition: RLTransition) -> None:
        self._sequence = transition.sequence
        self._last_transition = transition
