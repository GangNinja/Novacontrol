"""Verified paper-trade feedback for the isolated RL research layer.

This module turns a *complete, reconciled* AT-05 round trip into a deterministic
experience and reward.  It has no policy, training, order, risk or portfolio
handle.  It never promotes a policy and never rewards deposits: only realized
paper-trade outcomes net of fees and slippage are scored.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

AUTOMATIC_POLICY_PROMOTION = False
PAPER_EXPERIENCE_VERSION = "at06-paper-experience-v1"


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _number(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _weighted_average(rows: Sequence[Mapping[str, Any]]) -> tuple[Decimal, Decimal]:
    quantity = Decimal("0")
    notional = Decimal("0")
    for row in rows:
        qty = _decimal(row.get("quantity")) or Decimal("0")
        price = _decimal(row.get("price")) or Decimal("0")
        quantity += qty
        notional += qty * price
    return quantity, (notional / quantity if quantity > 0 else Decimal("0"))


def build_paper_experience(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Build one verified experience, or state exactly why it is not eligible."""

    evidence_id = str(evidence.get("evidence_id") or "")
    status = str(evidence.get("status") or "")
    if status != "complete":
        return {
            "status": "ineligible",
            "reason": f"evidence status is {status or 'unknown'}, not complete",
            "reward": None,
            "automatic_policy_promotion": AUTOMATIC_POLICY_PROMOTION,
        }
    entry = evidence.get("entry")
    exit_row = evidence.get("exit")
    if not isinstance(entry, Mapping) or not isinstance(exit_row, Mapping):
        return {
            "status": "ineligible",
            "reason": "entry or exit evidence is missing",
            "reward": None,
            "automatic_policy_promotion": AUTOMATIC_POLICY_PROMOTION,
        }
    if str(exit_row.get("reconciliation_status") or "") != "passed":
        return {
            "status": "ineligible",
            "reason": "the exit has not passed ledger reconciliation",
            "reward": None,
            "automatic_policy_promotion": AUTOMATIC_POLICY_PROMOTION,
        }
    entry_fills = entry.get("fills")
    exit_fills = exit_row.get("fills")
    if not isinstance(entry_fills, Sequence) or not isinstance(exit_fills, Sequence):
        return {
            "status": "ineligible",
            "reason": "fill evidence is missing",
            "reward": None,
            "automatic_policy_promotion": AUTOMATIC_POLICY_PROMOTION,
        }
    entry_qty, entry_avg = _weighted_average(entry_fills)
    exit_qty, exit_avg = _weighted_average(exit_fills)
    if entry_qty <= 0 or exit_qty <= 0:
        return {
            "status": "ineligible",
            "reason": "entry or exit quantity is zero",
            "reward": None,
            "automatic_policy_promotion": AUTOMATIC_POLICY_PROMOTION,
        }
    quantity = min(entry_qty, exit_qty)
    gross_pnl = (exit_avg - entry_avg) * quantity
    entry_fees = sum(
        (_decimal(row.get("total_fees")) or Decimal("0") for row in entry_fills),
        Decimal("0"),
    )
    exit_fees = sum(
        (_decimal(row.get("total_fees")) or Decimal("0") for row in exit_fills),
        Decimal("0"),
    )
    slippage = sum(
        (
            _decimal(row.get("slippage_amount")) or Decimal("0")
            for row in list(entry_fills) + list(exit_fills)
        ),
        Decimal("0"),
    )
    fees = entry_fees + exit_fees
    realized = _decimal((evidence.get("realized") or {}).get("position_realized_pnl"))
    net_pnl = realized if realized is not None else gross_pnl - fees
    basis = (
        "authoritative position realized P&L"
        if realized is not None
        else "reconciled fill prices minus entry/exit fees"
    )
    entry_notional = entry_avg * quantity
    return_pct = float(net_pnl / entry_notional) if entry_notional > 0 else 0.0
    cost_ratio = float((fees + slippage) / entry_notional) if entry_notional > 0 else 0.0
    drawdown_penalty = min(0.25, max(0.0, -return_pct) * 0.5)
    cost_penalty = min(0.20, max(0.0, cost_ratio) / 0.02 * 0.20)
    realized_component = _clamp(return_pct / 0.05) * 0.60
    reward = _clamp(realized_component - cost_penalty - drawdown_penalty)
    created_at = str(evidence.get("created_at") or "")
    return {
        "experience_id": "xrl_" + (evidence_id or "unknown"),
        "version": PAPER_EXPERIENCE_VERSION,
        "status": "verified",
        "source": "AT-05 complete paper round trip",
        "source_evidence_id": evidence_id,
        "decision_id": str(evidence.get("decision_id") or ""),
        "portfolio_id": str(evidence.get("portfolio_id") or ""),
        "instrument_id": str(evidence.get("instrument_id") or ""),
        "strategy_id": str(evidence.get("strategy_id") or ""),
        "strategy_version": str(evidence.get("strategy_version") or ""),
        "action": "sell",
        "entry_order_id": str(entry.get("order_id") or ""),
        "exit_order_id": str(exit_row.get("order_id") or ""),
        "entry_at": str(entry.get("opened_at") or ""),
        "exit_at": created_at,
        "chronological_key": created_at,
        "quantity": str(quantity),
        "entry_average_price": str(entry_avg),
        "exit_average_price": str(exit_avg),
        "realized_pnl": str(net_pnl),
        "unrealized_pnl": "0",
        "gross_pnl": str(gross_pnl),
        "fees": str(fees),
        "slippage": str(slippage),
        "transaction_cost_ratio": round(cost_ratio, 8),
        "return_pct": round(return_pct, 8),
        "reward": round(reward, 8),
        "reward_components": {
            "realized_pnl": round(realized_component, 8),
            "transaction_and_slippage_cost": round(-cost_penalty, 8),
            "drawdown": round(-drawdown_penalty, 8),
            "unrealized_pnl": 0.0,
            "deposit": 0.0,
            "realized": True,
            "basis": basis,
        },
        "verified": True,
        "reconciled": True,
        "paper_only": True,
        "live_trading": False,
        "automatic_policy_promotion": AUTOMATIC_POLICY_PROMOTION,
        "training_performed": False,
        "policy_modified": False,
        "chronology": {
            "ordered_by": "exit_at ascending",
            "lookahead": False,
            "verified_outcomes_only": True,
        },
    }


def chronological_splits(
    experiences: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Assign train/validation/test by time order, never by random leakage."""

    rows = sorted(
        (dict(row) for row in experiences),
        key=lambda row: str(row.get("chronological_key") or ""),
    )
    total = len(rows)
    if total == 0:
        return {"train": 0, "validation": 0, "test": 0, "total": 0, "rows": []}
    train_end = max(1, int(total * 0.70))
    validation_end = max(train_end, int(total * 0.85))
    for index, row in enumerate(rows):
        row["split"] = (
            "train"
            if index < train_end
            else "validation"
            if index < validation_end
            else "test"
        )
    rewards = [_number(_decimal(row.get("reward"))) or 0.0 for row in rows]
    return {
        "train": train_end,
        "validation": validation_end - train_end,
        "test": total - validation_end,
        "total": total,
        "mean_reward": round(sum(rewards) / total, 8),
        "rows": rows,
        "chronological": True,
        "automatic_policy_promotion": AUTOMATIC_POLICY_PROMOTION,
    }


__all__ = [
    "AUTOMATIC_POLICY_PROMOTION",
    "PAPER_EXPERIENCE_VERSION",
    "build_paper_experience",
    "chronological_splits",
]
