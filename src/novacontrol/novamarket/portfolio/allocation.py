"""Capital allocation and position sizing for NM-07.

One function turns a horizon's ranked assets into target weights. The method is
part of the horizon policy (score-led for short-term, risk-budgeted for medium,
inverse-volatility for long), and every constraint is applied here so a caller
cannot size a position past a limit by asking for a different method.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from novacontrol.novamarket.portfolio.models import PAISE, PortfolioPolicy
from novacontrol.novamarket.portfolio.selection import Assessment

WEIGHT_QUANTUM = Decimal("0.0001")
ZERO = Decimal("0.00")
ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class AllocationPlan:
    weights: dict[str, Decimal]
    investable: Decimal
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "weights": {key: format(value, "f") for key, value in self.weights.items()},
            "investable": format(self.investable, "f"),
            "notes": list(self.notes),
        }


def _strength(asset: Assessment, method: str) -> Decimal:
    if method == "equal":
        return ONE
    if method == "score":
        return Decimal(str(max(asset.rank, 0.0001)))
    width = asset.interval_width if asset.interval_width is not None else 0.05
    volatility = Decimal(str(max(width, 0.005)))
    if method == "inverse_volatility":
        return ONE / volatility
    # risk_budget: more confidence per unit of uncertainty
    confidence = Decimal(str(max(0.05, min(asset.confidence, 1.0))))
    return (ONE + confidence) / volatility


def allocate(assets: Sequence[Assessment], policy: PortfolioPolicy) -> AllocationPlan:
    """Target weights that sum to at most the policy's investable fraction."""
    investable = policy.investable_fraction
    usable = [asset for asset in assets if asset.selected][: policy.max_positions]
    if not usable:
        return AllocationPlan({}, investable, ("no eligible asset; capital stays in cash",))

    strengths = {asset.instrument_id: _strength(asset, policy.weighting) for asset in usable}
    total = sum(strengths.values(), ZERO)
    if total <= 0:
        strengths = {key: ONE for key in strengths}
        total = Decimal(len(strengths))
    weights = {key: investable * value / total for key, value in strengths.items()}
    notes = [f"weighting method: {policy.weighting}"]

    # Per-position cap, redistributed to the uncapped names.
    for _ in range(8):
        over = {
            key: value - policy.max_position_weight
            for key, value in weights.items()
            if value > policy.max_position_weight
        }
        if not over:
            break
        excess = sum(over.values(), ZERO)
        for key in over:
            weights[key] = policy.max_position_weight
        room = {
            key: policy.max_position_weight - value
            for key, value in weights.items()
            if value < policy.max_position_weight
        }
        capacity = sum(room.values(), ZERO)
        if capacity <= 0 or excess <= 0:
            break
        for key, space in room.items():
            weights[key] += excess * space / capacity
        notes.append("per-position cap applied")

    # Sector cap, redistributed outside the overflowing sectors.
    for _ in range(3):
        sectors: dict[str, Decimal] = {}
        for asset in usable:
            sectors[asset.sector] = (
                sectors.get(asset.sector, ZERO) + weights.get(asset.instrument_id, ZERO)
            )
        overflowing = {
            sector: value - policy.max_sector_weight
            for sector, value in sectors.items()
            if value > policy.max_sector_weight
        }
        if not overflowing:
            break
        excess = sum(overflowing.values(), ZERO)
        for asset in usable:
            if asset.sector not in overflowing:
                continue
            share = weights[asset.instrument_id] / sectors[asset.sector]
            weights[asset.instrument_id] -= overflowing[asset.sector] * share
        recipients = [asset for asset in usable if asset.sector not in overflowing]
        recipient_total = sum(
            (weights[asset.instrument_id] for asset in recipients), ZERO
        )
        if recipient_total <= 0:
            break
        for asset in recipients:
            weights[asset.instrument_id] += excess * (
                weights[asset.instrument_id] / recipient_total
            )
        notes.append("sector cap applied")

    # Drop dust below the minimum weight, then renormalise what remains.
    for _ in range(4):
        dropped = [key for key, value in weights.items() if value < policy.min_position_weight]
        if not dropped:
            break
        for key in dropped:
            del weights[key]
        notes.append(f"dropped {len(dropped)} position(s) below the minimum weight")
        if not weights:
            break
        subtotal = sum(weights.values(), ZERO)
        if subtotal <= 0:
            break
        weights = {key: value * investable / subtotal for key, value in weights.items()}
        for key, value in list(weights.items()):
            if value > policy.max_position_weight:
                weights[key] = policy.max_position_weight
                notes.append("per-position cap re-applied after dust removal")
        if sum(weights.values(), ZERO) > investable:
            scale = investable / sum(weights.values(), ZERO)
            weights = {key: value * scale for key, value in weights.items()}

    weights = {
        key: value.quantize(WEIGHT_QUANTUM) for key, value in sorted(weights.items())
    }
    weights = {key: value for key, value in weights.items() if value > 0}
    total_weight = sum(weights.values(), ZERO)
    if total_weight > investable:
        largest = max(weights, key=lambda key: weights[key])
        weights[largest] -= total_weight - investable
        weights = {key: value for key, value in weights.items() if value > 0}
    residual = investable - sum(weights.values(), ZERO)
    if residual > 0:
        notes.append(f"residual {residual.quantize(PAISE)} stays in cash")
    notes.append(f"{len(weights)} position(s) sized")
    return AllocationPlan(weights, investable, tuple(notes))


__all__ = ["AllocationPlan", "allocate"]
