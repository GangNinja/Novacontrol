"""Durable AT-05 exit configuration and deterministic trigger calculations.

A configuration belongs to one long paper position.  It may carry a fixed
target price, a trailing percentage, or both.  Stop-loss remains sourced from
the horizon profile.  The high-water mark is the highest valid observed price
since the trailing configuration was last changed; it is persisted with the
configuration so a restart cannot silently reset it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from novacontrol.novamarket.execution.models import execution_id, money

from .models import InvalidExitConfiguration, SUPPORTED_EXIT_CONDITIONS


def _decimal(value: object, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidExitConfiguration(f"{field_name} must be a decimal value") from exc
    if not result.is_finite():
        raise InvalidExitConfiguration(f"{field_name} must be finite")
    return result


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _target(value: object) -> str:
    if value in (None, ""):
        return ""
    target = money(value)
    if target <= 0:
        raise InvalidExitConfiguration("target_price must be greater than zero")
    return str(target)


def _trailing_fraction(
    *, trailing_stop_fraction: object = None, trailing_stop_percent: object = None
) -> str:
    if trailing_stop_fraction in (None, "") and trailing_stop_percent in (None, ""):
        return ""
    if trailing_stop_fraction not in (None, ""):
        fraction = _decimal(trailing_stop_fraction, "trailing_stop_fraction")
    else:
        fraction = _decimal(trailing_stop_percent, "trailing_stop_percent") / Decimal("100")
    if not Decimal("0") < fraction < Decimal("1"):
        raise InvalidExitConfiguration(
            "trailing_stop_percent must be greater than 0 and less than 100"
        )
    return format(fraction.normalize(), "f")


def build_exit_configuration(
    portfolio_id: str,
    instrument_id: str,
    *,
    position_id: str = "",
    target_price: object = None,
    trailing_stop_fraction: object = None,
    trailing_stop_percent: object = None,
    average_cost: object = None,
    existing: Mapping[str, Any] | None = None,
    version: int = 1,
) -> dict[str, Any]:
    """Validate and normalise one long-position exit configuration.

    Target validation is deliberately conservative: a long target must be
    above the authoritative average cost.  A caller that cannot provide the
    cost (for example a pure store test) gets syntax/range validation only.
    """

    if not portfolio_id.strip() or not instrument_id.strip():
        raise InvalidExitConfiguration(
            "portfolio_id and instrument_id are required for an exit configuration"
        )
    target = _target(target_price)
    fraction = _trailing_fraction(
        trailing_stop_fraction=trailing_stop_fraction,
        trailing_stop_percent=trailing_stop_percent,
    )
    if not target and not fraction:
        raise InvalidExitConfiguration(
            "at least one of target_price or trailing_stop_percent is required"
        )
    if target and average_cost not in (None, ""):
        cost = _decimal(average_cost, "average_cost")
        if cost <= 0:
            raise InvalidExitConfiguration("average_cost must be greater than zero")
        if Decimal(target) <= cost:
            raise InvalidExitConfiguration(
                "target_price must be above the position's average cost for a long position"
            )

    previous = dict(existing or {})
    same_trailing = bool(fraction) and fraction == str(previous.get("trailing_stop_fraction") or "")
    high_water = str(previous.get("high_water_mark") or "") if same_trailing else ""
    high_water_at = str(previous.get("high_water_at") or "") if same_trailing else ""
    if not fraction:
        high_water = ""
        high_water_at = ""
    now = _now_iso()
    conditions = [condition for condition, enabled in (
        ("stop_loss", True),
        ("target_price", bool(target)),
        ("trailing_stop", bool(fraction)),
    ) if enabled]
    return {
        "config_id": str(previous.get("config_id") or execution_id(
            "xcfg", portfolio_id, instrument_id
        )),
        "portfolio_id": portfolio_id,
        "instrument_id": instrument_id,
        "position_id": str(position_id or previous.get("position_id") or ""),
        "direction": "long",
        "version": max(1, int(version)),
        "target_price": target,
        "trailing_stop_fraction": fraction,
        "trailing_stop_percent": (
            format(Decimal(fraction) * Decimal("100"), "f") if fraction else ""
        ),
        "high_water_mark": high_water,
        "high_water_at": high_water_at,
        "configured_conditions": conditions,
        "supported_conditions": list(SUPPORTED_EXIT_CONDITIONS),
        "created_at": str(previous.get("created_at") or now),
        "updated_at": now,
        "last_evaluated_price": "",
        "last_market_data_timestamp": "",
        "current_trailing_stop_price": "",
        "trigger_status": "armed",
        "active_decision_id": "",
        "risk_assessment_id": "",
        "order_id": "",
        "submitted_quantity": "0",
        "filled_quantity": "0",
        "remaining_quantity": "",
        "reconciliation_status": "",
        "paper_only": True,
        "live_trading": False,
    }


def trailing_stop_price(high_water_mark: object, trailing_stop_fraction: object) -> Decimal:
    high = _decimal(high_water_mark, "high_water_mark")
    fraction = _decimal(trailing_stop_fraction, "trailing_stop_fraction")
    if high <= 0 or not Decimal("0") < fraction < Decimal("1"):
        raise InvalidExitConfiguration("trailing-stop state is not usable")
    return money(high * (Decimal("1") - fraction))


def condition_trigger_value(
    condition: str,
    *,
    stop_loss_price: str,
    target_price: str,
    trailing_price: str,
) -> str:
    if condition == "stop_loss":
        return stop_loss_price
    if condition == "target_price":
        return target_price
    if condition == "trailing_stop":
        return trailing_price
    raise ValueError(f"unknown exit condition {condition!r}")


__all__ = [
    "build_exit_configuration",
    "condition_trigger_value",
    "trailing_stop_price",
]
