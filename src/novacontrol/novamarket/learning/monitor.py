"""NM-13 performance monitoring and cross-source divergence detection.

Metrics are computed from *evidence*, grouped by its origin.  A backtest return,
a paper-trading return and a hypothetical shadow return are never averaged
together: they answer different questions, so the monitor keeps three snapshots
and reports divergence as a signal rather than blending it away.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from novacontrol.novamarket.learning.models import (
    EvidenceKind,
    EvidenceRecord,
    EvidenceStatus,
    PerformanceSnapshot,
    learning_id,
)


def _number(metrics: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = metrics.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def normalise_metrics(record: EvidenceRecord) -> dict[str, Any]:
    """One shape for all sources, with percentages made explicit.

    Backtest metrics use ratios (``total_return``); paper and shadow evidence
    store percentages.  Mixing the two units is how a 2% return becomes a 200%
    one, so the conversion happens once, here.
    """
    metrics = record.metrics
    if record.kind is EvidenceKind.BACKTEST:
        raw_return = _number(metrics, "return_pct")
        if raw_return is None:
            ratio = _number(metrics, "total_return", "return")
            raw_return = None if ratio is None else ratio * 100.0
        drawdown = _number(metrics, "max_drawdown_pct")
        if drawdown is None:
            ratio = _number(metrics, "max_drawdown")
            drawdown = None if ratio is None else ratio * 100.0
        win_rate = _number(metrics, "win_rate_pct")
        if win_rate is None:
            ratio = _number(metrics, "win_rate")
            win_rate = None if ratio is None else ratio * 100.0
    else:
        raw_return = _number(metrics, "return_pct", "mean_return_pct", "total_return")
        drawdown = _number(metrics, "max_drawdown_pct", "max_drawdown")
        win_rate = _number(metrics, "win_rate_pct", "win_rate")

    return {
        "return_pct": raw_return,
        "sharpe": _number(metrics, "sharpe"),
        "sortino": _number(metrics, "sortino"),
        "max_drawdown_pct": drawdown,
        "win_rate_pct": win_rate,
        "profit_factor": _number(metrics, "profit_factor"),
        "average_gain_pct": _number(metrics, "average_gain_pct", "average_win"),
        "average_loss_pct": _number(metrics, "average_loss_pct", "average_loss"),
        "volatility_pct": _number(metrics, "volatility_pct", "volatility"),
        "exposure_pct": _number(metrics, "exposure_pct", "invested_fraction"),
        "trades": _number(metrics, "trades", "fills"),
        "signals": _number(metrics, "signals", "decisions"),
        "by_instrument": (
            metrics.get("by_instrument")
            if isinstance(metrics.get("by_instrument"), Mapping)
            else None
        ),
        "by_horizon": (
            metrics.get("by_horizon")
            if isinstance(metrics.get("by_horizon"), Mapping)
            else None
        ),
    }


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def build_snapshot(
    strategy_id: str,
    kind: EvidenceKind,
    records: Sequence[EvidenceRecord],
    *,
    now: datetime,
    min_observations: int,
    window_days: int = 30,
) -> PerformanceSnapshot:
    """Aggregate accepted evidence in one window for one origin."""
    cutoff = now - timedelta(days=max(1, int(window_days)))
    usable = [
        record
        for record in records
        if record.kind is kind
        and record.status is not EvidenceStatus.REJECTED
        and (
            record.window_end is None
            or cutoff <= record.window_end <= now
        )
    ]
    if not usable:
        return PerformanceSnapshot(
            snapshot_id=learning_id("les_", strategy_id, kind.value, now.isoformat()),
            strategy_id=strategy_id,
            strategy_version="",
            kind=kind,
            window_start=cutoff,
            window_end=now,
            observations=0,
            metrics={"available": False, "reason": "no accepted evidence in the window"},
            enough_evidence=False,
            deterioration="insufficient_evidence",
            reason="no accepted evidence in the window",
            regimes=(),
            evidence_refs=(),
            created_at=now,
        )
    normalised = [normalise_metrics(record) for record in usable]
    observations = sum(record.observations for record in usable)
    enough = observations >= max(1, int(min_observations)) and len(usable) >= 1
    metrics: dict[str, Any] = {
        "available": enough,
        "evidence_count": len(usable),
        "reliability": usable[0].to_dict()["reliability"],
    }
    if enough:
        for key in (
            "return_pct",
            "sharpe",
            "sortino",
            "max_drawdown_pct",
            "win_rate_pct",
            "profit_factor",
            "average_gain_pct",
            "average_loss_pct",
            "volatility_pct",
            "exposure_pct",
            "trades",
            "signals",
        ):
            values: list[float] = []
            for row in normalised:
                value = row.get(key)
                if isinstance(value, (int, float)):
                    values.append(float(value))
            mean = _mean(values)
            metrics[key] = round(mean, 6) if mean is not None else None
        for breakdown_key in ("by_instrument", "by_horizon"):
            weighted: dict[str, float] = {}
            weights: dict[str, int] = {}
            for row in normalised:
                mapping = row.get(breakdown_key)
                if not isinstance(mapping, Mapping):
                    continue
                for name, bucket in mapping.items():
                    if not isinstance(bucket, Mapping):
                        continue
                    value = _number(bucket, "mean_return_pct", "return_pct")
                    if value is None:
                        continue
                    weight = max(1, int(bucket.get("resolved") or 1))
                    weighted[str(name)] = weighted.get(str(name), 0.0) + value * weight
                    weights[str(name)] = weights.get(str(name), 0) + weight
            if weighted:
                metrics[breakdown_key] = {
                    name: round(weighted[name] / weights[name], 6)
                    for name in sorted(weighted)
                }
    else:
        metrics["reason"] = (
            f"{observations} observation(s); at least {min_observations} are required"
        )
    regimes: list[str] = []
    for record in usable:
        for regime in record.regimes:
            if regime not in regimes:
                regimes.append(regime)
    return PerformanceSnapshot(
        snapshot_id=learning_id("les_", strategy_id, kind.value, now.isoformat()),
        strategy_id=strategy_id,
        strategy_version=usable[0].strategy_version,
        kind=kind,
        window_start=cutoff,
        window_end=now,
        observations=observations,
        metrics=metrics,
        enough_evidence=enough,
        deterioration="insufficient_evidence" if not enough else "stable",
        reason="" if enough else metrics["reason"],
        regimes=tuple(regimes),
        evidence_refs=tuple(record.evidence_id for record in usable),
        created_at=now,
    )


def detect_deterioration(
    current: PerformanceSnapshot,
    previous: PerformanceSnapshot | None,
    *,
    return_drop_pct: float = 1.0,
    drawdown_worsening_pct: float = 3.0,
    win_rate_drop_pct: float = 10.0,
    volatility_rise_pct: float = 5.0,
) -> tuple[str, str]:
    """Label a window against the preceding one, with no fabricated metric."""
    if not current.enough_evidence:
        return "insufficient_evidence", str(
            current.metrics.get("reason") or "insufficient evidence"
        )
    if previous is None or not previous.enough_evidence:
        return "stable", "no comparable previous window"
    now_return = current.metrics.get("return_pct")
    before_return = previous.metrics.get("return_pct")
    reasons: list[str] = []
    if (
        isinstance(now_return, (int, float))
        and isinstance(before_return, (int, float))
        and now_return < before_return - return_drop_pct
    ):
        reasons.append(
            f"return fell {before_return - now_return:.2f}pp "
            f"({before_return:.2f}% -> {now_return:.2f}%)"
        )
    drawdown = current.metrics.get("max_drawdown_pct")
    prior_drawdown = previous.metrics.get("max_drawdown_pct")
    if (
        isinstance(drawdown, (int, float))
        and isinstance(prior_drawdown, (int, float))
        and abs(drawdown) > abs(prior_drawdown) + drawdown_worsening_pct
    ):
        reasons.append(
            f"max drawdown worsened {abs(prior_drawdown):.2f}% -> {abs(drawdown):.2f}%"
        )
    win_rate = current.metrics.get("win_rate_pct")
    prior_win_rate = previous.metrics.get("win_rate_pct")
    if (
        isinstance(win_rate, (int, float))
        and isinstance(prior_win_rate, (int, float))
        and win_rate < prior_win_rate - win_rate_drop_pct
    ):
        reasons.append(
            f"win rate fell {prior_win_rate:.1f}% -> {win_rate:.1f}%"
        )
    volatility = current.metrics.get("volatility_pct")
    prior_volatility = previous.metrics.get("volatility_pct")
    if (
        isinstance(volatility, (int, float))
        and isinstance(prior_volatility, (int, float))
        and volatility > prior_volatility + volatility_rise_pct
    ):
        reasons.append(
            f"volatility rose {prior_volatility:.2f}% -> {volatility:.2f}%"
        )
    if reasons:
        return "degraded", "; ".join(reasons)
    return "stable", "within configured tolerances"


def detect_anomalies(
    current: PerformanceSnapshot,
    previous: PerformanceSnapshot | None,
    *,
    volatility_rise_pct: float = 5.0,
    signal_change_ratio: float = 0.25,
    exposure_rise_pct: float = 10.0,
) -> list[dict[str, Any]]:
    """Return named, measured anomalies for the alert surface.

    Each row is a concrete observation (with both windows' values), not a
    generic "something changed" message.  A missing or insufficient previous
    window produces no anomaly: there is nothing comparable to measure.
    """
    if not current.enough_evidence or previous is None or not previous.enough_evidence:
        return []
    rows: list[dict[str, Any]] = []

    def number(snapshot: PerformanceSnapshot, key: str) -> float | None:
        value = snapshot.metrics.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    volatility = number(current, "volatility_pct")
    prior_volatility = number(previous, "volatility_pct")
    if (
        volatility is not None
        and prior_volatility is not None
        and volatility > prior_volatility + volatility_rise_pct
    ):
        rows.append(
            {
                "kind": "volatility_spike",
                "severity": "warning",
                "message": (
                    f"volatility rose {prior_volatility:.2f}% -> {volatility:.2f}% "
                    f"(threshold +{volatility_rise_pct:.1f}pp)"
                ),
                "metrics": {
                    "previous_volatility_pct": prior_volatility,
                    "volatility_pct": volatility,
                },
            }
        )

    signals = number(current, "signals")
    prior_signals = number(previous, "signals")
    if (
        signals is not None
        and prior_signals is not None
        and signals >= 5
        and prior_signals >= 5
        and abs(signals - prior_signals) / prior_signals >= signal_change_ratio
    ):
        rows.append(
            {
                "kind": "signal_shift",
                "severity": "info",
                "message": (
                    f"signal count changed {prior_signals:.0f} -> {signals:.0f} "
                    f"({abs(signals - prior_signals) / prior_signals:.0%})"
                ),
                "metrics": {
                    "previous_signals": prior_signals,
                    "signals": signals,
                    "change_ratio": round(
                        abs(signals - prior_signals) / prior_signals, 6
                    ),
                },
            }
        )

    exposure = number(current, "exposure_pct")
    prior_exposure = number(previous, "exposure_pct")
    drawdown = number(current, "max_drawdown_pct")
    prior_drawdown = number(previous, "max_drawdown_pct")
    risk_reasons: list[str] = []
    risk_metrics: dict[str, float] = {}
    if (
        exposure is not None
        and prior_exposure is not None
        and exposure > prior_exposure + exposure_rise_pct
    ):
        risk_reasons.append(
            f"exposure rose {prior_exposure:.2f}% -> {exposure:.2f}%"
        )
        risk_metrics.update(
            {"previous_exposure_pct": prior_exposure, "exposure_pct": exposure}
        )
    if (
        drawdown is not None
        and prior_drawdown is not None
        and abs(drawdown) > abs(prior_drawdown) + 3.0
    ):
        risk_reasons.append(
            f"max drawdown worsened {abs(prior_drawdown):.2f}% -> {abs(drawdown):.2f}%"
        )
        risk_metrics.update(
            {
                "previous_max_drawdown_pct": prior_drawdown,
                "max_drawdown_pct": drawdown,
            }
        )
    if risk_reasons:
        rows.append(
            {
                "kind": "risk_increase",
                "severity": "warning",
                "message": "; ".join(risk_reasons),
                "metrics": risk_metrics,
            }
        )

    win_rate = number(current, "win_rate_pct")
    prior_win_rate = number(previous, "win_rate_pct")
    if (
        drawdown is not None
        and prior_drawdown is not None
        and win_rate is not None
        and prior_win_rate is not None
        and abs(drawdown) > abs(prior_drawdown) + 3.0
        and win_rate < prior_win_rate - 10.0
    ):
        rows.append(
            {
                "kind": "instability",
                "severity": "warning",
                "message": (
                    "drawdown worsened while win rate fell "
                    f"{prior_win_rate:.1f}% -> {win_rate:.1f}%"
                ),
                "metrics": {
                    "previous_win_rate_pct": prior_win_rate,
                    "win_rate_pct": win_rate,
                    "previous_max_drawdown_pct": prior_drawdown,
                    "max_drawdown_pct": drawdown,
                },
            }
        )
    return rows


def regime_change(
    current: PerformanceSnapshot, previous: PerformanceSnapshot | None
) -> str:
    if previous is None or not current.regimes or not previous.regimes:
        return ""
    added = sorted(set(current.regimes) - set(previous.regimes))
    removed = sorted(set(previous.regimes) - set(current.regimes))
    if not added and not removed:
        return ""
    parts: list[str] = []
    if added:
        parts.append("new regimes: " + ", ".join(added))
    if removed:
        parts.append("absent regimes: " + ", ".join(removed))
    return "; ".join(parts)


def source_divergence(
    snapshots: Sequence[PerformanceSnapshot], *, threshold_pct: float = 5.0
) -> dict[str, Any] | None:
    """Return the spread across origins, or ``None`` when they agree."""
    measured: list[tuple[str, float]] = []
    for snapshot in snapshots:
        value = snapshot.metrics.get("return_pct")
        if snapshot.enough_evidence and isinstance(value, (int, float)):
            measured.append((snapshot.kind.value, float(value)))
    if len(measured) < 2:
        return None
    values = [value for _, value in measured]
    spread = max(values) - min(values)
    if spread <= threshold_pct:
        return None
    return {
        "spread_pct": round(spread, 6),
        "returns_pct": {kind: round(value, 6) for kind, value in measured},
        "threshold_pct": threshold_pct,
        "interpretation": (
            "backtest, paper and shadow results disagree; they are not "
            "interchangeable and were not averaged"
        ),
    }


__all__ = [
    "build_snapshot",
    "detect_anomalies",
    "detect_deterioration",
    "normalise_metrics",
    "regime_change",
    "source_divergence",
]
