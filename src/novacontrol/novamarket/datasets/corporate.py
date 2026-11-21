"""Corporate-action recording and raw-vs-adjusted series separation (PI-02).

The engine never writes an adjusted price back into ``market_bars``.  It applies
only actions a caller has recorded with a source and, for price-relevant kinds,
an explicit ``price_factor``.  An unverified action, or one without a factor, is
reported as unapplied — the engine does not derive a factor from a price jump
and does not claim an adjustment it cannot source.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from novacontrol.novamarket.datasets.models import (
    AdjustmentBasis,
    CorporateAction,
    CorporateActionKind,
    DatasetValidationError,
)
from novacontrol.novamarket.marketdata.models import OhlcvBar


@dataclass(frozen=True, slots=True)
class AdjustedBar:
    observed_at: str
    open: str
    high: str
    low: str
    close: str
    volume: str | None
    factor: str
    applied_action_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "factor": self.factor,
            "applied_action_ids": list(self.applied_action_ids),
        }


@dataclass(frozen=True, slots=True)
class AdjustedSeries:
    """A derived view; ``raw_preserved`` is always true."""

    instrument_id: str
    basis: AdjustmentBasis
    bars: tuple[AdjustedBar, ...]
    applied_action_ids: tuple[str, ...] = ()
    unapplied_action_ids: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    digest_value: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "basis", AdjustmentBasis(self.basis))
        if not self.digest_value:
            canonical = json.dumps(
                [bar.to_dict() for bar in self.bars],
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            object.__setattr__(
                self,
                "digest_value",
                hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32],
            )

    @property
    def raw_preserved(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "basis": self.basis.value,
            "bar_count": len(self.bars),
            "bars": [bar.to_dict() for bar in self.bars],
            "applied_action_ids": list(self.applied_action_ids),
            "unapplied_action_ids": list(self.unapplied_action_ids),
            "limitations": list(self.limitations),
            "raw_preserved": True,
            "store_rewritten": False,
            "digest": self.digest_value,
        }


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0000000001"))


def adjust_bars(
    instrument_id: str,
    bars: Sequence[OhlcvBar],
    actions: Sequence[CorporateAction],
    *,
    basis: AdjustmentBasis | str = AdjustmentBasis.SPLIT_ADJUSTED,
) -> AdjustedSeries:
    """Apply verified split/bonus/rights (and optionally dividend) factors.

    Price factors multiply observations strictly before the action's effective
    instant.  Volume is left unchanged and explicitly documented as unadjusted:
    adjusting it without a per-provider unit contract would invent a number.
    """
    try:
        selected_basis = AdjustmentBasis(basis)
    except ValueError as exc:
        raise DatasetValidationError(f"unknown adjustment basis: {basis!r}") from exc
    if selected_basis is AdjustmentBasis.PROVIDER_ADJUSTED:
        raise DatasetValidationError(
            "no installed provider supplies a verified provider-adjusted series; "
            "use raw, split_adjusted or total_return_adjusted from recorded actions"
        )
    if selected_basis is AdjustmentBasis.RAW:
        return AdjustedSeries(
            instrument_id=instrument_id,
            basis=selected_basis,
            bars=tuple(
                AdjustedBar(
                    observed_at=bar.observed_at.isoformat(),
                    open=format(bar.open, "f"),
                    high=format(bar.high, "f"),
                    low=format(bar.low, "f"),
                    close=format(bar.close, "f"),
                    volume=None if bar.volume is None else format(bar.volume, "f"),
                    factor="1",
                    applied_action_ids=(),
                )
                for bar in bars
            ),
            limitations=("raw series: no adjustment factor was applied",),
        )
    all_relevant = [
        action
        for action in actions
        if action.instrument_id == instrument_id
        and CorporateActionKind(action.kind).price_relevant
    ]
    relevant = all_relevant
    if selected_basis is AdjustmentBasis.SPLIT_ADJUSTED:
        relevant = [
            action
            for action in relevant
            if action.kind is not CorporateActionKind.DIVIDEND
        ]
    applicable = [action for action in relevant if action.applicable]
    unapplied = [action for action in relevant if not action.applicable]
    ordered = sorted(applicable, key=lambda item: (item.effective_at, item.action_id))
    adjusted: list[AdjustedBar] = []
    global_applied: list[str] = []
    for bar in bars:
        factor = Decimal(1)
        applied: list[str] = []
        for action in ordered:
            if bar.observed_at < action.effective_at:
                assert action.price_factor is not None  # guaranteed by `applicable`
                factor *= action.price_factor
                applied.append(action.action_id)
                if action.action_id not in global_applied:
                    global_applied.append(action.action_id)
        adjusted.append(
            AdjustedBar(
                observed_at=bar.observed_at.isoformat(),
                open=format(_quantize(bar.open * factor), "f"),
                high=format(_quantize(bar.high * factor), "f"),
                low=format(_quantize(bar.low * factor), "f"),
                close=format(_quantize(bar.close * factor), "f"),
                volume=None if bar.volume is None else format(bar.volume, "f"),
                factor=format(_quantize(factor), "f"),
                applied_action_ids=tuple(applied),
            )
        )
    limitations: list[str] = []
    if unapplied:
        limitations.append(
            f"{len(unapplied)} corporate action(s) are unverified or lack an explicit "
            "price factor; no adjustment was invented for them"
        )
    if selected_basis is AdjustmentBasis.SPLIT_ADJUSTED and any(
        action.kind is CorporateActionKind.DIVIDEND for action in all_relevant
    ):
        limitations.append(
            "split-adjusted basis excludes dividend actions; request "
            "total_return_adjusted for dividend factors recorded by a source"
        )
    limitations.append("volume is not adjusted; its unit is the provider's")
    return AdjustedSeries(
        instrument_id=instrument_id,
        basis=selected_basis,
        bars=tuple(adjusted),
        applied_action_ids=tuple(global_applied),
        unapplied_action_ids=tuple(action.action_id for action in unapplied),
        limitations=tuple(limitations),
    )


__all__ = ["AdjustedBar", "AdjustedSeries", "adjust_bars"]
