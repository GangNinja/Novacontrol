"""Evidence adapters for PI-21.

Every value is read from an existing authoritative or advisory service.  The
adapter caches within one management cycle so a portfolio review does not repeat
the same forecast, research or agent query for every decision.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from novacontrol.novamarket.portfolio_management.models import (
    HORIZON_ALIASES,
    PortfolioManagementStateError,
    evidence_horizon,
)


def _items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [dict(row) for row in value if isinstance(row, Mapping)]
    if isinstance(value, Mapping):
        for key in ("items", "forecasts", "decisions", "runs", "policies", "results"):
            rows = value.get(key)
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)):
                return [dict(row) for row in rows if isinstance(row, Mapping)]
    return []


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(slots=True)
class ManagementSources:
    """The composition surface PI-21 may read or ask PI-12 to continue.

    ``execute_proposal`` is the existing AT-04 path.  It accepts a persisted,
    risk-approved AT-03 proposal id; it cannot accept an order, instrument,
    quantity or price from PI-21.
    """

    portfolio_list: Callable[[int], Any]
    portfolio_detail: Callable[[str], Any]
    pending_orders: Callable[[str], Any]
    reservations: Callable[[str], Any]
    reserved_cash: Callable[[str], Any]
    fills: Callable[[str], Any]
    risk_portfolio: Callable[[str], Any]
    exit_configurations: Callable[[int], Any]
    forecast_views: Callable[[str | None, str], Any]
    instrument_evidence: Callable[[str], Any]
    agent_decisions: Callable[[str, int], Any]
    optimization_latest: Callable[[str, str], Any]
    training_policies: Callable[[int], Any]
    at03_proposals: Callable[[str], Any]
    runtime_status: Callable[[], Any]
    execute_proposal: Callable[[str, str], Any]
    reconcile_execution: Callable[[str], Any]
    recover_autonomy: Callable[[str], Any] | None = None
    valuation_history: Callable[[str, int], Any] | None = None


class EvidenceBook:
    """Per-cycle cache over the existing PI-04..PI-20 evidence services."""

    def __init__(self, sources: ManagementSources) -> None:
        self._sources = sources
        self._forecasts: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._instruments: dict[str, dict[str, Any]] = {}
        self._agents: dict[str, dict[str, Any]] = {}
        self._optimizations: dict[tuple[str, str], dict[str, Any]] = {}
        self._risks: dict[str, dict[str, Any]] = {}
        self._policies: list[dict[str, Any]] | None = None
        self._exits: list[dict[str, Any]] | None = None
        self.errors: dict[str, str] = {}

    def _safe(self, key: str, call: Callable[[], Any], default: Any) -> Any:
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - missing evidence is reported, never invented
            self.errors[key] = f"{type(exc).__name__}: {exc}"
            return default

    def forecasts(self, instrument_id: str | None, horizon: str) -> list[dict[str, Any]]:
        canonical = evidence_horizon(horizon)
        key = (str(instrument_id or "").upper(), canonical)
        if key not in self._forecasts:
            body = self._safe(
                f"forecasts:{key[0] or '*'}:{canonical}",
                lambda: self._sources.forecast_views(
                    instrument_id.upper() if instrument_id else None, canonical
                ),
                [],
            )
            rows = _items(body)
            normalized: list[dict[str, Any]] = []
            for row in rows:
                row_horizon = str(row.get("horizon") or row.get("forecast_horizon") or "").lower()
                row_horizon = HORIZON_ALIASES.get(row_horizon, row_horizon)
                accepted = {"", row_horizon}
                if row_horizon in {"1h", "60m"}:
                    accepted.update({"1h", "60m"})
                if canonical not in accepted:
                    continue
                normalized.append(dict(row))
            self._forecasts[key] = normalized
        return list(self._forecasts[key])

    def instrument(self, instrument_id: str) -> dict[str, Any]:
        canonical = instrument_id.upper()
        if canonical not in self._instruments:
            self._instruments[canonical] = _dict(
                self._safe(
                    f"instrument:{canonical}",
                    lambda: self._sources.instrument_evidence(canonical),
                    {},
                )
            )
        return dict(self._instruments[canonical])

    def agent_decision(self, instrument_id: str) -> dict[str, Any]:
        canonical = instrument_id.upper()
        if canonical not in self._agents:
            body = self._safe(
                f"agents:{canonical}",
                lambda: self._sources.agent_decisions(canonical, 20),
                {},
            )
            rows = _items(body)
            latest = rows[0] if rows else {}
            nested = _dict(latest.get("decision"))
            self._agents[canonical] = {**latest, **nested} if nested else latest
        return dict(self._agents[canonical])

    def optimization(self, portfolio_id: str, horizon: str) -> dict[str, Any]:
        key = (portfolio_id, horizon)
        if key not in self._optimizations:
            self._optimizations[key] = _dict(
                self._safe(
                    f"optimization:{portfolio_id}:{horizon}",
                    lambda: self._sources.optimization_latest(portfolio_id, horizon),
                    {},
                )
            )
        return dict(self._optimizations[key])

    def risk(self, portfolio_id: str) -> dict[str, Any]:
        if portfolio_id not in self._risks:
            self._risks[portfolio_id] = _dict(
                self._safe(
                    f"risk:{portfolio_id}",
                    lambda: self._sources.risk_portfolio(portfolio_id),
                    {},
                )
            )
        return dict(self._risks[portfolio_id])

    def policies(self) -> list[dict[str, Any]]:
        if self._policies is None:
            self._policies = _items(
                self._safe(
                    "training_policies",
                    lambda: self._sources.training_policies(30),
                    [],
                )
            )
        return list(self._policies)

    def exits(self) -> list[dict[str, Any]]:
        if self._exits is None:
            self._exits = _items(
                self._safe(
                    "exit_configurations",
                    lambda: self._sources.exit_configurations(500),
                    [],
                )
            )
        return list(self._exits)

    def exit_configuration(self, portfolio_id: str, instrument_id: str) -> dict[str, Any]:
        for row in self.exits():
            if (
                str(row.get("portfolio_id") or "") == portfolio_id
                and str(row.get("instrument_id") or "").upper() == instrument_id.upper()
            ):
                return dict(row)
        return {}

    def risk_position(self, portfolio_id: str, instrument_id: str) -> dict[str, Any]:
        for row in _items(self.risk(portfolio_id).get("positions")):
            if str(row.get("instrument_id") or "").upper() == instrument_id.upper():
                return row
        return {}

    def policy_advisory(self, instrument_id: str, horizon: str, regime: str) -> dict[str, Any]:
        for row in self.policies():
            policy_horizon = str(row.get("horizon") or row.get("forecast_horizon") or "")
            if policy_horizon and policy_horizon not in {horizon, evidence_horizon(horizon)}:
                continue
            row_instrument = str(row.get("instrument_id") or "")
            if row_instrument and row_instrument.upper() != instrument_id.upper():
                continue
            row_regime = str(row.get("regime") or row.get("market_regime") or "")
            if row_regime and regime and row_regime != regime:
                continue
            return {
                "policy_id": str(row.get("policy_id") or row.get("artifact_id") or ""),
                "version": str(
                    row.get("artifact_version")
                    or row.get("policy_version")
                    or row.get("version")
                    or ""
                ),
                "status": str(row.get("status") or row.get("lifecycle_status") or ""),
                "suitability": row.get("suitability") or row.get("regime_suitability"),
                "degraded": bool(row.get("degraded") or row.get("degradation")),
                "validation_metrics": row.get("validation_metrics") or {},
                "test_metrics": row.get("test_metrics") or {},
                "experimental": True,
                "advisory_only": True,
                "activation_available": False,
                "production_authority": False,
                "note": "Experimental policy output is advisory and was not activated.",
            }
        return {}

    def observed_at(self, instrument_evidence: Mapping[str, Any]) -> datetime | None:
        raw = _dict(instrument_evidence.get("instrument")).get("price_at")
        if raw in (None, ""):
            return None
        try:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def require_sources(value: Any) -> ManagementSources:
    if isinstance(value, ManagementSources):
        return value
    required = (
        "portfolio_list",
        "portfolio_detail",
        "pending_orders",
        "reserved_cash",
        "runtime_status",
        "execute_proposal",
    )
    missing = [name for name in required if not callable(getattr(value, name, None))]
    if missing:
        raise PortfolioManagementStateError(
            "portfolio-management sources are incomplete: " + ", ".join(missing)
        )
    return cast(ManagementSources, value)


__all__ = ["EvidenceBook", "ManagementSources", "require_sources"]
