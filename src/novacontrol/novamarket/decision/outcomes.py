"""PI-12 outcome tracking: forecast, decision, execution and paper P&L are separate.

The tracker reads the already-committed decision audit, market bars and NM-10
records.  It never changes a decision, a position or a ledger entry.  An outcome
is inserted once and is immutable; if the target bar is not available yet the
decision remains pending rather than receiving a fabricated result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from novacontrol.novamarket.decision.forecast import ForecastDecisionStatus


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None
    return parsed if parsed.is_finite() else None


def _number(value: Any) -> float | None:
    parsed = _decimal(value)
    return float(parsed) if parsed is not None else None


def _direction(actual: float, neutral_band: float) -> str:
    if actual > neutral_band:
        return "up"
    if actual < -neutral_band:
        return "down"
    return "neutral"


@dataclass(frozen=True, slots=True)
class ForecastOutcomeTracker:
    store: Any
    market_data: Any
    execution_provider: Any | None = None
    clock: Any = None
    neutral_band: float = 0.002

    def __post_init__(self) -> None:
        if self.clock is None:
            object.__setattr__(
                self, "clock", lambda: datetime.now(tz=UTC)
            )

    def _now(self) -> datetime:
        return cast(datetime, self.clock())

    def track(self, *, limit: int = 50) -> dict[str, Any]:
        decisions = self.store.pending_prediction_decisions(limit=limit)
        measured: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        execution_updates = 0
        for decision in decisions:
            decision_id = str(decision.get("decision_id") or "")
            execution = self._execution(decision)
            if execution.get("order_id"):
                if self._sync_execution_status(decision, execution):
                    execution_updates += 1
            target = _parse_time(decision.get("target_at"))
            if target is None or target > self._now():
                pending.append(
                    {
                        "decision_id": decision_id,
                        "reason": "target time has not been reached",
                    }
                )
                continue
            bar = self._target_bar(str(decision.get("instrument_id") or ""), target)
            if bar is None:
                pending.append(
                    {
                        "decision_id": decision_id,
                        "reason": "no market observation exists at or after the target",
                    }
                )
                continue
            try:
                outcome = self._measure(decision, bar, execution)
            except Exception as exc:  # noqa: BLE001 - one bad row cannot stop tracking
                pending.append(
                    {
                        "decision_id": decision_id,
                        "reason": f"measurement failed closed: {type(exc).__name__}: {exc}",
                    }
                )
                continue
            saved = self.store.save_prediction_decision_outcome(outcome)
            measured.append(saved)
            self.store.append_prediction_decision_event(
                decision_id,
                "outcome.recorded",
                {
                    "status": str(decision.get("status") or ""),
                    "outcome_id": saved.get("outcome_id"),
                    "forecast_correct": saved.get("forecast", {}).get("correct"),
                    "decision_correct": saved.get("decision", {}).get("correct"),
                    "execution_status": saved.get("execution", {}).get("status"),
                },
                at=self._now(),
            )
        return {
            "ok": True,
            "measured": measured,
            "measured_count": len(measured),
            "pending": pending,
            "pending_count": len(pending),
            "execution_updates": execution_updates,
            "automatic_promotion": False,
        }

    def _execution(self, decision: Mapping[str, Any]) -> dict[str, Any]:
        proposal_id = str(decision.get("proposal_id") or "")
        if not proposal_id or self.execution_provider is None:
            return {}
        try:
            engine = (
                self.execution_provider()
                if callable(self.execution_provider)
                else self.execution_provider
            )
            if engine is None:
                return {}
            store = getattr(engine, "store", None)
            if store is None:
                return {}
            order = store.order_for_proposal(proposal_id)
            if order is None:
                return {}
            fills = store.fills(order.order_id)
            quantity = sum((fill.quantity for fill in fills), Decimal("0"))
            fees = sum((fill.total_fees for fill in fills), Decimal("0"))
            notional = sum(
                (fill.price * fill.quantity for fill in fills), Decimal("0")
            )
            average = notional / quantity if quantity > 0 else None
            return {
                "proposal_id": proposal_id,
                "order_id": order.order_id,
                "status": order.status.value,
                "requested_quantity": str(order.requested_quantity),
                "filled_quantity": str(quantity),
                "total_fees": str(fees),
                "average_fill_price": str(average) if average is not None else None,
                "fill_count": len(fills),
                "fills": [fill.to_dict() for fill in fills],
                "reconciliation": (
                    "pending"
                    if order.status.value
                    in {
                        "created",
                        "pending_validation",
                        "accepted",
                        "open",
                        "partially_filled",
                        "cancel_pending",
                    }
                    else "terminal"
                ),
            }
        except Exception as exc:  # noqa: BLE001 - execution evidence stays explicit
            return {"error": f"{type(exc).__name__}: {exc}"}

    def _sync_execution_status(
        self, decision: Mapping[str, Any], execution: Mapping[str, Any]
    ) -> bool:
        status = str(execution.get("status") or "")
        mapped = {
            "created": ForecastDecisionStatus.ORDER_SUBMITTED.value,
            "pending_validation": ForecastDecisionStatus.ORDER_SUBMITTED.value,
            "accepted": ForecastDecisionStatus.ORDER_ACCEPTED.value,
            "open": ForecastDecisionStatus.ORDER_SUBMITTED.value,
            "cancel_pending": ForecastDecisionStatus.ORDER_SUBMITTED.value,
            "partially_filled": ForecastDecisionStatus.PARTIALLY_FILLED.value,
            "filled": ForecastDecisionStatus.FILLED.value,
            "rejected": ForecastDecisionStatus.ORDER_REJECTED.value,
            "cancelled": ForecastDecisionStatus.ORDER_CANCELLED.value,
            "expired": ForecastDecisionStatus.ORDER_CANCELLED.value,
            "failed": ForecastDecisionStatus.ORDER_REJECTED.value,
        }.get(status, "")
        if not mapped:
            return False
        current = str(decision.get("status") or "")
        terminal = {
            ForecastDecisionStatus.FILLED.value,
            ForecastDecisionStatus.ORDER_REJECTED.value,
            ForecastDecisionStatus.ORDER_CANCELLED.value,
        }
        if current in terminal:
            return False
        if current == mapped:
            return False
        self.store.append_prediction_decision_event(
            str(decision.get("decision_id")),
            f"execution.{status}",
            {
                "status": mapped,
                "order_id": execution.get("order_id"),
                "filled_quantity": execution.get("filled_quantity"),
                "reconciliation": execution.get("reconciliation"),
            },
            at=self._now(),
        )
        return True

    def _target_bar(
        self, instrument_id: str, target: datetime
    ) -> Mapping[str, Any] | None:
        if not instrument_id:
            return None
        try:
            page = self.market_data.bars(
                instrument_id,
                limit=200,
                ascending=True,
                start=target - timedelta(days=1),
            )
        except Exception:  # noqa: BLE001 - the outcome stays pending
            return None
        rows = (
            page.get("bars") or page.get("items") or ()
            if isinstance(page, Mapping)
            else getattr(page, "items", ()) or ()
        )
        candidates: list[tuple[datetime, Mapping[str, Any]]] = []
        for row in rows:
            if isinstance(row, Mapping):
                body = row
            else:
                body = {
                    "close": getattr(row, "close", None),
                    "observed_at": getattr(row, "observed_at", None),
                    "high": getattr(row, "high", None),
                    "low": getattr(row, "low", None),
                    "volume": getattr(row, "volume", None),
                    "partial": getattr(row, "partial", False),
                }
            partial = bool(body.get("partial"))
            observed = _parse_time(body.get("observed_at"))
            close = _decimal(body.get("close"))
            if (
                not partial
                and observed is not None
                and close is not None
                and observed >= target
            ):
                # Normalise the observation time even when the source supplied
                # JSON strings.  ``_measure`` records it as a timestamp; leaving
                # a string here would turn a valid bar into a pending outcome.
                candidates.append(
                    (
                        observed,
                        {
                            "close": body.get("close"),
                            "observed_at": observed,
                            "high": body.get("high"),
                            "low": body.get("low"),
                            "volume": body.get("volume"),
                            "partial": False,
                        },
                    )
                )
        if not candidates:
            return None
        observed, row = min(candidates, key=lambda item: item[0])
        return {**dict(row), "observed_at": observed, "close": _decimal(row.get("close"))}

    def _measure(
        self,
        decision: Mapping[str, Any],
        bar: Mapping[str, Any],
        execution: Mapping[str, Any],
    ) -> dict[str, Any]:
        actual_price = _decimal(bar.get("close"))
        reference = _decimal(decision.get("reference_price"))
        if actual_price is None or reference is None or reference <= 0:
            raise ValueError("the target bar or reference price is invalid")
        actual_return = float((actual_price - reference) / reference)
        actual_direction = _direction(actual_return, self.neutral_band)
        primary = decision.get("primary_forecast") or {}
        forecast_direction = str(primary.get("direction") or "").lower()
        expected_return = _number(primary.get("predicted_return"))
        forecast_correct = (
            forecast_direction == actual_direction
            if forecast_direction in {"up", "down", "neutral"}
            else None
        )
        decision_type = str(decision.get("decision_type") or "").upper()
        decision_correct = self._decision_correct(
            decision_type, forecast_direction, actual_direction, actual_return
        )
        missed = (
            actual_return
            if decision_type in {"WAIT", "NO_ACTION"}
            and actual_direction == "up"
            else None
        )
        avoided_loss = (
            True
            if decision_type in {"WAIT", "NO_ACTION"}
            and actual_direction == "down"
            else None
        )
        order_status = str(execution.get("status") or "")
        filled = _decimal(execution.get("filled_quantity")) or Decimal("0")
        average_fill = _decimal(execution.get("average_fill_price"))
        fees = _decimal(execution.get("total_fees")) or Decimal("0")
        paper_pnl: Decimal | None = None
        pnl_basis = "no filled paper order"
        if filled > 0 and average_fill is not None:
            if decision_type == "SELL":
                paper_pnl = (average_fill - actual_price) * filled - fees
                pnl_basis = (
                    "mark-to-target of NM-10 sells: (average fill - target price) "
                    "* filled quantity - fees; not a portfolio ledger total"
                )
            else:
                paper_pnl = (actual_price - average_fill) * filled - fees
                pnl_basis = (
                    "mark-to-target of NM-10 buys: (target price - average fill) "
                    "* filled quantity - fees; not a portfolio ledger total"
                )
        target = _parse_time(decision.get("target_at"))
        return {
            "outcome_id": "",
            "decision_id": str(decision.get("decision_id")),
            "instrument_id": str(decision.get("instrument_id") or ""),
            "portfolio_id": str(decision.get("portfolio_id") or ""),
            "decision_type": decision_type,
            "measured_at": self._now().isoformat(),
            "target_at": (
                target.isoformat() if target else str(decision.get("target_at") or "")
            ),
            "actual_at": bar["observed_at"].isoformat(),
            "status": "measured",
            "reference_price": str(reference),
            "actual_price": str(actual_price),
            "actual_return": round(actual_return, 8),
            "actual_direction": actual_direction,
            "forecast": {
                "forecast_id": str(decision.get("primary_forecast_id") or ""),
                "horizon": str(decision.get("horizon") or ""),
                "direction": forecast_direction,
                "expected_return": expected_return,
                "correct": forecast_correct,
                "basis": "forecast direction compared with the target-period return",
            },
            "decision": {
                "type": decision_type,
                "correct": decision_correct,
                "basis": (
                    "action direction compared with the target-period return; "
                    "WAIT/NO_ACTION are opportunity outcomes, not trades"
                ),
                "missed_opportunity_return": missed,
                "avoided_loss": avoided_loss,
            },
            "execution": {
                "proposal_id": str(decision.get("proposal_id") or ""),
                "order_id": str(execution.get("order_id") or ""),
                "status": order_status or "not_submitted",
                "requested_quantity": execution.get("requested_quantity"),
                "filled_quantity": execution.get("filled_quantity"),
                "average_fill_price": execution.get("average_fill_price"),
                "total_fees": execution.get("total_fees"),
                "fill_count": execution.get("fill_count", 0),
                "reconciliation": execution.get("reconciliation", "not_applicable"),
                "error": execution.get("error", ""),
            },
            "portfolio": {
                "paper_pnl": str(paper_pnl) if paper_pnl is not None else None,
                "pnl_basis": pnl_basis,
                "authoritative_source": "NM-10 fills and the existing portfolio ledger",
                "mutates_portfolio": False,
            },
            "risk": {
                "decision_id": str(decision.get("risk_decision_id") or ""),
                "outcome": str(decision.get("risk_outcome") or ""),
                "status": str(decision.get("status") or ""),
            },
            "automatic_promotion": False,
            "learning_applied": False,
        }

    @staticmethod
    def _decision_correct(
        decision_type: str,
        forecast_direction: str,
        actual_direction: str,
        actual_return: float,
    ) -> bool | None:
        if decision_type == "BUY":
            return actual_direction == "up"
        if decision_type == "SELL":
            return actual_direction == "down"
        if decision_type == "HOLD":
            if forecast_direction == "up":
                return actual_direction != "down"
            if forecast_direction == "down":
                return actual_direction != "up"
            if forecast_direction == "neutral":
                return actual_direction == "neutral"
            return None
        return None


__all__ = ["ForecastOutcomeTracker"]
