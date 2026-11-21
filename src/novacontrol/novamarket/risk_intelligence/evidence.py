"""Read-only evidence adapters for PI-22.

Every callback reads an existing authoritative or advisory service.  The cache
lives for one analysis only, so a risk overview never repeats the same bars,
forecast or research query for every position.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


def items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [dict(row) for row in value if isinstance(row, Mapping)]
    if isinstance(value, Mapping):
        for key in (
            "items",
            "rows",
            "bars",
            "orders",
            "forecasts",
            "decisions",
            "proposals",
            "valuations",
            "policies",
            "results",
        ):
            body = value.get(key)
            if isinstance(body, Sequence) and not isinstance(body, (str, bytes, bytearray)):
                return [dict(row) for row in body if isinstance(row, Mapping)]
    return []


def mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(slots=True)
class RiskIntelligenceSources:
    portfolio_list: Callable[[int], Any]
    portfolio_detail: Callable[[str], Any]
    valuation_history: Callable[[str, int], Any]
    bars: Callable[[str, int], Any]
    forecast_views: Callable[[str, str], Any]
    instrument_evidence: Callable[[str], Any]
    agent_decisions: Callable[[str, int], Any]
    optimization_latest: Callable[[str, str], Any]
    risk_portfolio: Callable[[str], Any]
    risk_position: Callable[[str, str], Any]
    runtime_status: Callable[[], Any]
    pending_orders: Callable[[str], Any] | None = None
    reserved_cash: Callable[[str], Any] | None = None
    management_snapshot: Callable[[str], Any] | None = None
    benchmark_history: Callable[[str, int], Any] | None = None
    regime_history: Callable[[str, int], Any] | None = None


def require_sources(sources: RiskIntelligenceSources) -> RiskIntelligenceSources:
    required = (
        "portfolio_list",
        "portfolio_detail",
        "valuation_history",
        "bars",
        "forecast_views",
        "instrument_evidence",
        "agent_decisions",
        "optimization_latest",
        "risk_portfolio",
        "risk_position",
        "runtime_status",
    )
    missing = [name for name in required if not callable(getattr(sources, name, None))]
    if missing:
        raise ValueError("missing risk-intelligence source(s): " + ", ".join(missing))
    return sources


class RiskEvidenceBook:
    def __init__(self, sources: RiskIntelligenceSources) -> None:
        self._sources = sources
        self._detail: dict[str, dict[str, Any]] = {}
        self._valuations: dict[str, list[dict[str, Any]]] = {}
        self._bars: dict[str, list[dict[str, Any]]] = {}
        self._forecasts: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._instruments: dict[str, dict[str, Any]] = {}
        self._agents: dict[str, dict[str, Any]] = {}
        self._optimizations: dict[tuple[str, str], dict[str, Any]] = {}
        self._risks: dict[str, dict[str, Any]] = {}
        self._positions_risk: dict[tuple[str, str], dict[str, Any]] = {}
        self.errors: dict[str, str] = {}

    def _safe(self, key: str, call: Callable[[], Any], default: Any) -> Any:
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - every gap is reported, never invented
            self.errors[key] = f"{type(exc).__name__}: {exc}"
            return default

    def portfolio_detail(self, portfolio_id: str) -> dict[str, Any]:
        if portfolio_id not in self._detail:
            self._detail[portfolio_id] = mapping(
                self._safe(
                    f"portfolio:{portfolio_id}",
                    lambda: self._sources.portfolio_detail(portfolio_id),
                    {},
                )
            )
        return dict(self._detail[portfolio_id])

    def valuation_history(self, portfolio_id: str, limit: int = 500) -> list[dict[str, Any]]:
        if portfolio_id not in self._valuations:
            body = self._safe(
                f"valuations:{portfolio_id}",
                lambda: self._sources.valuation_history(portfolio_id, limit),
                [],
            )
            self._valuations[portfolio_id] = items(body)
        return [dict(row) for row in self._valuations[portfolio_id]]

    def bars(self, instrument_id: str, limit: int = 260) -> list[dict[str, Any]]:
        key = instrument_id.upper()
        if key not in self._bars:
            body = self._safe(
                f"bars:{key}",
                lambda: self._sources.bars(key, limit),
                [],
            )
            self._bars[key] = items(body)
        return [dict(row) for row in self._bars[key]]

    def forecasts(self, instrument_id: str, horizon: str) -> list[dict[str, Any]]:
        key = (instrument_id.upper(), horizon)
        if key not in self._forecasts:
            body = self._safe(
                f"forecasts:{key[0]}:{horizon}",
                lambda: self._sources.forecast_views(key[0], horizon),
                [],
            )
            self._forecasts[key] = items(body)
        return [dict(row) for row in self._forecasts[key]]

    def instrument(self, instrument_id: str) -> dict[str, Any]:
        key = instrument_id.upper()
        if key not in self._instruments:
            self._instruments[key] = mapping(
                self._safe(
                    f"instrument:{key}",
                    lambda: self._sources.instrument_evidence(key),
                    {},
                )
            )
        return dict(self._instruments[key])

    def agent(self, instrument_id: str) -> dict[str, Any]:
        key = instrument_id.upper()
        if key not in self._agents:
            body = self._safe(
                f"agents:{key}",
                lambda: self._sources.agent_decisions(key, 20),
                {},
            )
            rows = items(body)
            latest = rows[0] if rows else {}
            nested = mapping(latest.get("decision"))
            self._agents[key] = {**latest, **nested} if nested else latest
        return dict(self._agents[key])

    def optimization(self, portfolio_id: str, horizon: str) -> dict[str, Any]:
        key = (portfolio_id, horizon)
        if key not in self._optimizations:
            self._optimizations[key] = mapping(
                self._safe(
                    f"optimization:{portfolio_id}:{horizon}",
                    lambda: self._sources.optimization_latest(portfolio_id, horizon),
                    {},
                )
            )
        return dict(self._optimizations[key])

    def risk(self, portfolio_id: str) -> dict[str, Any]:
        if portfolio_id not in self._risks:
            self._risks[portfolio_id] = mapping(
                self._safe(
                    f"risk:{portfolio_id}",
                    lambda: self._sources.risk_portfolio(portfolio_id),
                    {},
                )
            )
        return dict(self._risks[portfolio_id])

    def position_risk(self, portfolio_id: str, instrument_id: str) -> dict[str, Any]:
        key = (portfolio_id, instrument_id.upper())
        if key not in self._positions_risk:
            self._positions_risk[key] = mapping(
                self._safe(
                    f"position-risk:{portfolio_id}:{instrument_id}",
                    lambda: self._sources.risk_position(portfolio_id, instrument_id),
                    {},
                )
            )
        return dict(self._positions_risk[key])

    def management_snapshot(self, portfolio_id: str) -> dict[str, Any]:
        source = self._sources.management_snapshot
        if source is None:
            return {}
        return mapping(
            self._safe(
                f"management:{portfolio_id}",
                lambda: source(portfolio_id),
                {},
            )
        )

    def pending_orders(self, portfolio_id: str) -> list[dict[str, Any]]:
        source = self._sources.pending_orders
        if source is None:
            return []
        body = self._safe(
            f"pending-orders:{portfolio_id}",
            lambda: source(portfolio_id),
            [],
        )
        return items(body)

    def reserved_cash(self, portfolio_id: str) -> float | None:
        source = self._sources.reserved_cash
        if source is None:
            return None
        body = self._safe(
            f"reserved-cash:{portfolio_id}",
            lambda: source(portfolio_id),
            None,
        )
        try:
            return None if body is None else float(str(body))
        except (TypeError, ValueError):
            return None

    def benchmark(self, instrument_id: str, limit: int = 260) -> list[dict[str, Any]]:
        source = self._sources.benchmark_history
        if source is None:
            return []
        body = self._safe(
            f"benchmark:{instrument_id}",
            lambda: source(instrument_id, limit),
            [],
        )
        return items(body)

    def regime_history(self, portfolio_id: str, limit: int = 200) -> list[dict[str, Any]]:
        source = self._sources.regime_history
        if source is None:
            return []
        body = self._safe(
            f"regimes:{portfolio_id}",
            lambda: source(portfolio_id, limit),
            [],
        )
        return items(body)


__all__ = [
    "RiskEvidenceBook",
    "RiskIntelligenceSources",
    "items",
    "mapping",
    "require_sources",
]
