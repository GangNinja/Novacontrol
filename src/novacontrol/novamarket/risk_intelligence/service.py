"""PI-22 advanced risk-intelligence and portfolio-protection coordinator.

The coordinator reads authoritative portfolio/valuation state and existing
market, forecast, research, agent, optimization, PI-21 and NM-08 evidence.  It
returns explainable analytics and advisory protective recommendations; it has
no order, risk-decision, cash, ledger or portfolio-writing method.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from itertools import combinations, islice
from typing import Any

from novacontrol.novamarket.risk_intelligence.analytics import (
    concentration_metrics,
    data_freshness,
    drawdown_episodes,
    level_from_drawdown,
    level_from_loss,
    liquidity_metrics,
    normalise_bars,
    pairwise_correlation,
    scenario_impact,
    volatility_metrics,
)
from novacontrol.novamarket.risk_intelligence.evidence import (
    RiskEvidenceBook,
    RiskIntelligenceSources,
    items,
    mapping,
    require_sources,
)
from novacontrol.novamarket.risk_intelligence.models import (
    CONFIG_VERSION,
    MAX_SCENARIOS,
    RI_CONTRACT_VERSION,
    RI_PHASE,
    RISK_LEVEL_RANK,
    AlertSeverity,
    AnalysisStatus,
    LiquidityClass,
    ProtectiveRecommendation,
    RiskIntelligenceNotFound,
    RiskIntelligenceStateError,
    RiskIntelligenceValidationError,
    RiskLevel,
    RiskThresholds,
    RiskTrend,
    authority_contract,
    digest,
    money_text,
    number,
    parse_time,
    stable_id,
    utcnow,
)
from novacontrol.novamarket.risk_intelligence.store import RiskIntelligenceStore

_LOGGER = logging.getLogger("novacontrol.novamarket.risk_intelligence")

ACTIVE_ORDER_STATUSES = frozenset(
    {"created", "pending_validation", "accepted", "open", "partially_filled", "cancel_pending"}
)
VALID_FORECAST_STATES = frozenset({"valid", "confirmed", "active"})
WAIT_FORECAST_STATES = frozenset({"unconfirmed", "pending", "awaiting_confirmation", "initial"})
BENCHMARK_INSTRUMENT = "NSE:NIFTY50"

SCENARIO_KINDS = frozenset(
    {
        "market_decline",
        "market_rally",
        "sector_decline",
        "sector_rally",
        "instrument_decline",
        "volatility_expansion",
        "liquidity_deterioration",
        "correlated_decline",
        "cash_depletion",
        "multiple_pending_orders",
        "combined",
    }
)
STRESS_MODES = frozenset(
    {"historical", "hypothetical", "volatility", "correlation", "liquidity", "combined"}
)


def _text(value: object, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _num(value: object) -> float | None:
    return number(value)


def _dict(value: object) -> dict[str, Any]:
    return mapping(value)


def _rank(value: object) -> int:
    return RISK_LEVEL_RANK.get(_text(value, 40), -1)


def _max_level(values: Sequence[object]) -> str:
    valid = [_text(value, 40) for value in values if _rank(value) >= 0]
    if not valid:
        return RiskLevel.INSUFFICIENT_DATA.value
    return max(valid, key=_rank)


def _severity(value: object) -> str:
    level = _text(value, 40)
    if level == RiskLevel.CRITICAL.value:
        return AlertSeverity.CRITICAL_ALERT.value
    if level == RiskLevel.HIGH.value:
        return AlertSeverity.HIGH_ALERT.value
    if level == RiskLevel.ELEVATED.value:
        return AlertSeverity.WARNING.value
    if level == RiskLevel.NORMAL.value:
        return AlertSeverity.INFORMATIONAL.value
    return AlertSeverity.WATCH.value


def _position_value(row: Mapping[str, Any]) -> float | None:
    return _num(row.get("market_value") or row.get("market_value_nc") or row.get("current_value"))


def _position_return(row: Mapping[str, Any]) -> float | None:
    value = _num(row.get("return_pct"))
    if value is not None:
        # NM-07 position/valuation return fields are percentages, never ratios.
        return value / 100.0
    price = _num(row.get("last_price") or row.get("current_price"))
    average = _num(row.get("average_cost") or row.get("average_price"))
    if price is not None and average and average > 0:
        return (price / average) - 1.0
    return None


class RiskIntelligenceService:
    """The one PI-22 coordinator.  Every financial authority stays external."""

    def __init__(
        self,
        sources: RiskIntelligenceSources,
        *,
        store_path: str | Any = ":memory:",
        clock: Any | None = None,
        event_loader: Callable[..., Mapping[str, Any]] | None = None,
    ) -> None:
        self._sources = require_sources(sources)
        self._store = RiskIntelligenceStore(store_path)
        self._clock = clock or utcnow
        self._event_loader = event_loader
        self._last_error = ""
        self._closed = False

    @property
    def store(self) -> RiskIntelligenceStore:
        return self._store

    @property
    def last_error(self) -> str:
        return self._last_error

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._store.close()

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _config(self) -> RiskThresholds:
        return self._store.config()[1]

    def _runtime(self) -> dict[str, Any]:
        try:
            body = self._sources.runtime_status()
            result = _dict(body)
            return result if result else {"mode": "disabled", "available": False}
        except Exception as exc:  # noqa: BLE001 - status reports the failure
            return {
                "mode": "disabled",
                "available": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _mode(self) -> str:
        return _text(_dict(self._runtime()).get("mode") or "disabled", 32).lower() or "disabled"

    def _kill_switch(self) -> bool:
        runtime = _dict(self._runtime())
        values = [
            runtime.get("kill_switch"),
            runtime.get("kill_switch_active"),
            runtime.get("halted"),
        ]
        safety = _dict(runtime.get("safety"))
        values.extend([safety.get("kill_switch"), safety.get("kill_switch_active")])
        return any(
            item is True or _text(item, 10).lower() in {"1", "true", "yes", "on"} for item in values
        )

    def _event_risk(self, portfolio_id: str, horizon: str) -> dict[str, Any]:
        """PI-24 event uncertainty, read only; NM-08 remains the risk authority."""
        if self._event_loader is None:
            return {
                "available": False,
                "status": "insufficient_evidence",
                "warnings": [],
                "risk_authority": "NM-08",
                "creates_risk_decision": False,
            }
        try:
            body = self._event_loader(portfolio_id, horizon)
        except Exception as exc:  # noqa: BLE001 - event evidence is optional
            _LOGGER.warning("PI-22 could not read PI-24 event risk: %s", exc)
            return {
                "available": False,
                "status": "insufficient_evidence",
                "warnings": [],
                "error": f"{type(exc).__name__}: {exc}",
                "risk_authority": "NM-08",
                "creates_risk_decision": False,
            }
        result = dict(body) if isinstance(body, Mapping) else {}
        result["available"] = bool(result)
        result.setdefault("status", "insufficient_evidence")
        result.setdefault("warnings", [])
        result["risk_authority"] = "NM-08"
        result["creates_risk_decision"] = False
        return result

    def status(self, *, operator_authorized: bool | None = None) -> dict[str, Any]:
        runtime = self._runtime()
        latest = self._store.latest_assessment()
        return {
            "ok": True,
            "phase": RI_PHASE,
            "implemented": True,
            "initialised": True,
            "contract_version": RI_CONTRACT_VERSION,
            "config_version": CONFIG_VERSION,
            "mode": self._mode(),
            "runtime_state": _text(runtime.get("runtime_state"), 40) or "unknown",
            "runtime_available": runtime.get("available") is not False,
            "kill_switch": self._kill_switch(),
            "operator_authorized": operator_authorized,
            "operator_authorization_required": True,
            "latest_assessment_id": (latest or {}).get("assessment_id", ""),
            "latest_assessment_status": (latest or {}).get("status", ""),
            "latest_assessment_at": (latest or {}).get("generated_at", ""),
            "store": self._store.status(),
            "authorities": authority_contract(),
            "safety": {
                "paper_only": True,
                "live_trading": False,
                "broker_connected": False,
                "direct_financial_mutation": False,
                "direct_order_submission": False,
                "risk_override": False,
                "risk_decision_creation": False,
                "automatic_protective_action": False,
                "scenario_is_prediction": False,
                "recovery_resumes_trading": False,
            },
            "last_error": self._last_error,
        }

    def contract(self) -> dict[str, Any]:
        config = self._config()
        return {
            "ok": True,
            "phase": RI_PHASE,
            "name": "Advanced Risk Intelligence and Portfolio Protection",
            "contract_version": RI_CONTRACT_VERSION,
            "advisory_only": True,
            "read_only_financial": True,
            "risk_levels": [item.value for item in RiskLevel],
            "analysis_statuses": [item.value for item in AnalysisStatus],
            "liquidity_classes": [item.value for item in LiquidityClass],
            "alert_severities": [item.value for item in AlertSeverity],
            "recommendations": [item.value for item in ProtectiveRecommendation],
            "stress_modes": sorted(STRESS_MODES),
            "scenario_kinds": sorted(SCENARIO_KINDS),
            "authorities": authority_contract(),
            "workflow": [
                "read authoritative NM-07 portfolio and valuation state",
                "read stored PI-04/PI-16 market evidence",
                "compute bounded position, portfolio, concentration, correlation, "
                "volatility, drawdown and liquidity analytics",
                "run isolated historical/hypothetical scenario and stress analysis",
                "generate explainable warnings and advisory protective recommendations",
                "return evidence to the PI-21 portfolio-management workflow",
                "request NM-08 authoritative assessment when an action is proposed",
                "PI-12 and NM-10 remain the only execution path",
                "record immutable analytical history and provenance",
            ],
            "methodology": {
                "volatility": "population standard deviation of simple returns, annualised "
                "by sqrt(periods_per_year); NM-08 implementation",
                "var_cvar": "historical simulation at the configured confidence; no "
                "normal-distribution assumption; VaR is not maximum loss",
                "correlation": "Pearson correlation on exact-timestamp intersected simple "
                "returns; unmatched observations excluded",
                "concentration": "normalised position weights and NM-08 HHI; top-N and "
                "sector/industry weights",
                "drawdown": "peak-to-trough declines of authoritative valuation snapshots",
                "liquidity": "mean close x volume ADV, position/ADV days and explicit "
                "participation assumptions",
                "scenario": "hypothetical shocks on isolated analytical values; never a "
                "prediction and never a balance mutation",
                "stress": "historical, hypothetical, volatility, correlation, liquidity "
                "and combined modes with stated assumptions",
            },
            "limits": {
                "min_observations": config.min_observations,
                "min_correlation_observations": config.min_correlation_observations,
                "max_series_points": config.max_series_points,
                "max_correlation_pairs": config.max_correlation_pairs,
                "max_positions": config.max_positions,
                "max_alerts": config.max_alerts,
                "max_history": config.max_history,
            },
            "composite_score": None,
            "composite_score_note": "PI-22 deliberately has no arbitrary weighted "
            "portfolio score; the headline level is the maximum explained component level.",
        }

    def navigation(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": RI_PHASE,
            "views": [
                {"id": "overview", "label": "Risk overview"},
                {"id": "positions", "label": "Position risk"},
                {"id": "concentration", "label": "Concentration & correlation"},
                {"id": "volatility", "label": "Volatility & drawdown"},
                {"id": "liquidity", "label": "Liquidity"},
                {"id": "scenarios", "label": "Scenarios & stress"},
                {"id": "alerts", "label": "Alerts & recommendations"},
            ],
            "controls": ["analyze", "scenario", "stress", "alert_status", "recover"],
            "direct_financial_controls": [],
        }

    def config(self) -> dict[str, Any]:
        version, config = self._store.config()
        return {
            "ok": True,
            "version": version,
            "config": config.to_dict(),
            "history": self._store.config_history(),
        }

    def update_config(self, payload: Mapping[str, Any], *, actor: str = "") -> dict[str, Any]:
        current = self._config()
        merged = {**current.to_dict(), **dict(payload or {})}
        updated = RiskThresholds.from_mapping(merged)
        saved = self._store.save_config(updated)
        return {
            "ok": True,
            "config": saved["config"],
            "version": saved["version"],
            "actor": _text(actor, 160),
            "financial_mutation": False,
        }

    async def analyze(
        self,
        portfolio_id: str,
        *,
        horizon: str = "5d",
        trigger: str = "manual",
        request_id: str = "",
        actor: str = "",
    ) -> dict[str, Any]:
        portfolio = _text(portfolio_id, 160)
        if not portfolio:
            raise RiskIntelligenceValidationError("a portfolio id is required")
        if trigger == "scheduled" and self._mode() == "disabled":
            raise RiskIntelligenceStateError(
                "scheduled risk scans are disabled while autonomy mode is disabled",
                code="SCAN_DISABLED",
            )
        config = self._config()
        evidence = RiskEvidenceBook(self._sources)
        detail = evidence.portfolio_detail(portfolio)
        if not detail:
            raise RiskIntelligenceNotFound(f"unknown or unavailable portfolio {portfolio!r}")
        now = self._now()
        portfolio_body = _dict(detail.get("portfolio"))
        valuation = _dict(detail.get("valuation"))
        holdings = items(detail.get("holdings"))
        total = _num(valuation.get("total_value"))
        cash = _num(detail.get("cash")) or _num(valuation.get("cash"))
        reserved = evidence.reserved_cash(portfolio)
        available = None if cash is None else max(0.0, cash - (reserved or 0.0))
        pending = self._pending(evidence, portfolio)
        pending["available_cash_nc"] = None if available is None else round(available, 2)
        capital = self._capital_analysis(
            valuation=valuation,
            total=total,
            cash=cash,
            available=available,
            reserved=reserved,
            pending=pending,
        )
        snapshot_version = stable_id(
            "risnap_",
            portfolio,
            valuation.get("snapshot_id"),
            valuation.get("at"),
            digest(valuation),
        )
        data = self._market_data(evidence, holdings, config)
        freshness = data_freshness(
            [row.get("priced_at") for row in holdings if row.get("priced_at")],
            now=now,
            stale_hours=float(config.data_stale_hours),
        )
        if total is None:
            data_status = AnalysisStatus.INSUFFICIENT_DATA.value
        elif freshness.get("stale"):
            data_status = AnalysisStatus.STALE_DATA.value
        elif evidence.errors or data["missing_bars"]:
            data_status = AnalysisStatus.COMPLETED_WITH_WARNINGS.value
        else:
            data_status = AnalysisStatus.COMPLETED.value
        positions = self._position_analysis(
            portfolio_id=portfolio,
            evidence=evidence,
            holdings=holdings,
            total=total,
            data=data,
            config=config,
            now=now,
            horizon=_text(horizon, 32) or "5d",
        )
        concentration = concentration_metrics(positions, config)
        correlation = self._correlation_analysis(evidence, positions, data, config)
        volatility = self._portfolio_volatility(positions, data, config)
        drawdown = self._portfolio_drawdown(evidence, portfolio, total, config)
        liquidity = self._portfolio_liquidity(positions, config)
        regime = self._regime_analysis(evidence, positions, config)
        if total is None:
            risk_level = RiskLevel.INSUFFICIENT_DATA.value
        else:
            risk_level = _max_level(
                [
                    concentration.get("risk_level"),
                    correlation.get("risk_level"),
                    volatility.get("risk_level"),
                    level_from_drawdown(drawdown.get("current_drawdown_pct"), config),
                    liquidity.get("risk_level"),
                    regime.get("risk_level"),
                    *[row.get("risk_level") for row in positions],
                ]
            )
            if risk_level == RiskLevel.NORMAL.value and (
                data["missing_bars"]
                or volatility.get("status") == "insufficient_data"
                or correlation.get("status") == "insufficient_data"
            ):
                # A normal headline is only justified when the market-risk
                # components that could contradict it are actually available.
                risk_level = RiskLevel.INSUFFICIENT_DATA.value
        warnings = self._warnings(
            portfolio=portfolio,
            positions=positions,
            concentration=concentration,
            correlation=correlation,
            volatility=volatility,
            drawdown=drawdown,
            liquidity=liquidity,
            regime=regime,
            freshness=freshness,
            pending=pending,
            config=config,
            now=now,
        )
        if data["missing_bars"]:
            warnings.append(
                self._warning(
                    portfolio,
                    "",
                    "data",
                    "historical_observations_missing",
                    AlertSeverity.WARNING.value,
                    "Stored OHLCV history is missing for one or more holdings; affected "
                    "metrics are unavailable.",
                    {"instruments": list(data["missing_bars"])[:20]},
                    now,
                )
            )
        event_risk = self._event_risk(portfolio, _text(horizon, 32) or "5d")
        for item in (event_risk.get("warnings") or [])[:10]:
            if not isinstance(item, Mapping):
                continue
            severity = _text(item.get("severity"), 20).lower()
            warnings.append(
                self._warning(
                    portfolio,
                    _text(item.get("instrument_id"), 80),
                    "event",
                    _text(item.get("code"), 80) or "event_risk",
                    AlertSeverity.HIGH_ALERT.value
                    if severity in {"high", "critical"}
                    else AlertSeverity.WATCH.value
                    if severity == "watch"
                    else AlertSeverity.WARNING.value,
                    _text(item.get("explanation"), 500)
                    or "Relevant event evidence elevates forecast uncertainty.",
                    {
                        "event_ids": list(item.get("event_ids") or []),
                        "verification": item.get("verification"),
                        "importance": item.get("importance"),
                        "direction": "not_estimated",
                    },
                    now,
                )
            )
        if data.get("positions_omitted"):
            warnings.append(
                self._warning(
                    portfolio,
                    "",
                    "data",
                    "analysis_position_limit",
                    AlertSeverity.WATCH.value,
                    "The configured position limit omitted holdings from this analytical run.",
                    {"positions_omitted": data["positions_omitted"]},
                    now,
                )
            )
        if volatility.get("status") == "insufficient_data":
            warnings.append(
                self._warning(
                    portfolio,
                    "",
                    "volatility",
                    "portfolio_volatility_unavailable",
                    AlertSeverity.WATCH.value,
                    "Portfolio volatility is unavailable because aligned observations "
                    "are insufficient.",
                    volatility,
                    now,
                )
            )
        if correlation.get("status") == "insufficient_data":
            warnings.append(
                self._warning(
                    portfolio,
                    "",
                    "correlation",
                    "correlation_unavailable",
                    AlertSeverity.WATCH.value,
                    "Correlation is unavailable because overlapping observations are insufficient.",
                    correlation,
                    now,
                )
            )
        previous = self._store.latest_assessment(portfolio)
        trend = self._trend(previous, risk_level, warnings)
        recommendations = self._recommendations(
            portfolio=portfolio,
            positions=positions,
            warnings=warnings,
            concentration=concentration,
            correlation=correlation,
            liquidity=liquidity,
            risk_level=risk_level,
            risk=_dict(evidence.risk(portfolio)),
            config=config,
            now=now,
        )
        generated_at = now.isoformat()
        assessment_id = stable_id(
            "riassess_",
            portfolio,
            snapshot_version,
            config.digest,
            request_id or generated_at,
        )
        status_value = data_status
        if data_status == AnalysisStatus.COMPLETED.value and any(
            warning.get("code")
            in {
                "historical_observations_missing",
                "portfolio_volatility_unavailable",
                "correlation_unavailable",
                "liquidity_data_missing",
                "pending_cash_evidence_missing",
                "analysis_position_limit",
            }
            for warning in warnings
        ):
            status_value = AnalysisStatus.COMPLETED_WITH_WARNINGS.value
        if risk_level == RiskLevel.CRITICAL.value:
            status_value = AnalysisStatus.REQUIRES_REVIEW.value
        if data_status == AnalysisStatus.INSUFFICIENT_DATA.value:
            status_value = AnalysisStatus.INSUFFICIENT_DATA.value
        portfolio_risk = _dict(evidence.risk(portfolio))
        response: dict[str, Any] = {
            "ok": status_value not in {AnalysisStatus.CALCULATION_FAILED.value},
            "phase": RI_PHASE,
            "assessment_id": assessment_id,
            "portfolio_id": portfolio,
            "horizon": _text(horizon, 32) or "5d",
            "trigger": _text(trigger, 32) or "manual",
            "status": status_value,
            "data_status": data_status,
            "generated_at": generated_at,
            "snapshot_version": snapshot_version,
            "config_version": config.version,
            "config_digest": config.digest,
            "risk_level": risk_level,
            "trend": trend,
            "previous_assessment": {
                "assessment_id": (previous or {}).get("assessment_id"),
                "risk_level": (previous or {}).get("risk_level"),
                "generated_at": (previous or {}).get("generated_at"),
            }
            if previous
            else None,
            "assessment_confidence": None,
            "assessment_confidence_reason": (
                "No calibrated probability model is claimed. Component sample sizes and evidence "
                "sufficiency are reported instead."
            ),
            "portfolio": {
                "portfolio_id": portfolio,
                "name": _text(portfolio_body.get("name"), 160),
                "horizon": _text(portfolio_body.get("horizon"), 32),
                "total_value_nc": None if total is None else money_text(total),
                "total_value_inr": None if total is None else money_text(total),
                "invested_nc": None
                if _num(valuation.get("invested")) is None
                else money_text(valuation.get("invested")),
                "invested_inr": None
                if _num(valuation.get("invested")) is None
                else money_text(valuation.get("invested")),
                "cash_nc": None if cash is None else money_text(cash),
                "cash_inr": None if cash is None else money_text(cash),
                "available_cash_nc": None if available is None else money_text(available),
                "available_cash_inr": None if available is None else money_text(available),
                "reserved_cash_nc": None if reserved is None else money_text(reserved),
                "pending_order_exposure_nc": money_text(pending["notional_nc"]),
                "unrealized_pnl_nc": None
                if _num(valuation.get("unrealized_pnl")) is None
                else money_text(valuation.get("unrealized_pnl")),
                "realized_pnl_nc": None
                if _num(valuation.get("realized_pnl")) is None
                else money_text(valuation.get("realized_pnl")),
                "position_count": len(positions),
                "currency": "NC",
                "inr_equivalent": "1 NC = 1 INR",
            },
            "positions": positions,
            "concentration": concentration,
            "correlation": correlation,
            "volatility": volatility,
            "drawdown": drawdown,
            "liquidity": liquidity,
            "regime": regime,
            "event_risk": event_risk,
            "pending_orders": pending,
            "capital": capital,
            "warnings": warnings,
            "risk_contributors": self._contributors(
                positions, concentration, correlation, volatility
            ),
            "data_freshness": freshness,
            "data_versions": data["versions"],
            "positions_omitted": data.get("positions_omitted", 0),
            "evidence_errors": evidence.errors,
            "nm08": {
                "available": bool(portfolio_risk),
                "outcome": _text(portfolio_risk.get("outcome"), 40) or None,
                "decision_id": _text(portfolio_risk.get("decision_id"), 160) or None,
                "reference": _text(portfolio_risk.get("decision_id"), 160) or None,
                "authority": "NM-08",
                "pi22_creates_risk_decision": False,
            },
            "recommendations": recommendations,
            "alerts": [],
            "alerts_deduplicated": 0,
            "methodology": self.contract()["methodology"],
            "limitations": self._limitations(data_status, data["missing_bars"]),
            "authorities": authority_contract(),
            "advisory_only": True,
            "financial_mutation": False,
            "ledger_write": False,
            "order_submission": False,
        }
        alerts, created = self._persist_alerts(warnings, config=config)
        response["alerts"] = alerts
        response["alerts_deduplicated"] = created
        self._store.save_assessment(
            response,
            positions,
            {
                "concentration": concentration,
                "correlation": correlation,
                "volatility": volatility,
                "drawdown": drawdown,
                "liquidity": liquidity,
                "regime": regime,
                "pending_orders": pending,
                "capital": capital,
            },
        )
        for recommendation in recommendations:
            self._store.save_recommendation(recommendation)
        return response

    def overview(self, portfolio_id: str) -> dict[str, Any]:
        latest = self._store.latest_assessment(portfolio_id)
        if latest is None:
            return {
                "ok": False,
                "phase": RI_PHASE,
                "portfolio_id": portfolio_id,
                "status": "not_run",
                "message": "Run a risk analysis to record the first assessment.",
            }
        return latest

    def assessment(self, assessment_id: str) -> dict[str, Any]:
        return self._store.get_assessment(assessment_id)

    def history(
        self, portfolio_id: str = "", *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        return self._store.assessments(portfolio_id=portfolio_id, limit=limit, offset=offset)

    def trend(self, portfolio_id: str) -> dict[str, Any]:
        rows = self._store.assessments(portfolio_id=portfolio_id, limit=20)["items"]
        if len(rows) < 2:
            return {
                "ok": True,
                "portfolio_id": portfolio_id,
                "trend": RiskTrend.UNKNOWN.value,
                "reason": "at least two assessments are required",
                "points": rows,
            }
        latest, previous = rows[0], rows[1]
        trend = self._trend(previous, latest.get("risk_level"), latest.get("warnings") or [])
        return {
            "ok": True,
            "portfolio_id": portfolio_id,
            "trend": trend,
            "latest_assessment_id": latest.get("assessment_id"),
            "previous_assessment_id": previous.get("assessment_id"),
            "points": [
                {
                    "assessment_id": row.get("assessment_id"),
                    "generated_at": row.get("generated_at"),
                    "risk_level": row.get("risk_level"),
                    "data_status": row.get("data_status"),
                    "warning_count": len(row.get("warnings") or []),
                }
                for row in rows
            ],
        }

    def alerts(
        self, *, portfolio_id: str = "", status: str = "", limit: int = 100
    ) -> dict[str, Any]:
        rows = self._store.alerts(portfolio_id=portfolio_id, status=status, limit=limit)
        return {"ok": True, "alerts": rows, "total": len(rows)}

    async def update_alert(self, alert_id: str, *, action: str, actor: str = "") -> dict[str, Any]:
        status = {"acknowledge": "acknowledged", "resolve": "resolved", "reopen": "open"}.get(
            _text(action, 30).lower()
        )
        if status is None:
            raise RiskIntelligenceValidationError(
                "alert action must be acknowledge, resolve or reopen"
            )
        row = self._store.update_alert(alert_id, status=status, actor=actor)
        return {
            "ok": True,
            "alert": row,
            "financial_mutation": False,
            "order_submission": False,
        }

    def recommendations(self, *, portfolio_id: str = "", limit: int = 100) -> dict[str, Any]:
        rows = self._store.recommendations(portfolio_id=portfolio_id, limit=limit)
        return {"ok": True, "recommendations": rows, "total": len(rows)}

    # -- construction helpers ----------------------------------------------
    def _pending(self, evidence: RiskEvidenceBook, portfolio_id: str) -> dict[str, Any]:
        rows = [
            row
            for row in evidence.pending_orders(portfolio_id)
            if _text(row.get("status"), 40).lower() in ACTIVE_ORDER_STATUSES
        ]
        buy_notional = 0.0
        sell_notional = 0.0
        for row in rows:
            qty = _num(row.get("remaining_quantity"))
            price = _num(row.get("reference_price") or row.get("limit_price"))
            if not qty or not price:
                continue
            side = _text(row.get("side"), 20).lower()
            if side == "buy":
                buy_notional += qty * price
            elif side == "sell":
                sell_notional += qty * price
        return {
            "count": len(rows),
            "notional_nc": round(buy_notional, 2),
            "buy_notional_nc": round(buy_notional, 2),
            "sell_notional_nc": round(sell_notional, 2),
            "net_notional_nc": round(buy_notional - sell_notional, 2),
            "orders": rows[:100],
        }

    @staticmethod
    def _capital_analysis(
        *,
        valuation: Mapping[str, Any],
        total: float | None,
        cash: float | None,
        available: float | None,
        reserved: float | None,
        pending: Mapping[str, Any],
    ) -> dict[str, Any]:
        invested = _num(valuation.get("invested"))
        buy_notional = _num(pending.get("buy_notional_nc")) or 0.0
        return {
            "status": "completed" if total is not None else "insufficient_data",
            "total_value_nc": None if total is None else round(total, 2),
            "invested_nc": None if invested is None else round(invested, 2),
            "cash_nc": None if cash is None else round(cash, 2),
            "reserved_cash_nc": None if reserved is None else round(reserved, 2),
            "available_cash_nc": None if available is None else round(available, 2),
            "invested_weight": round(invested / total, 6)
            if invested is not None and total
            else None,
            "cash_weight": round(cash / total, 6) if cash is not None and total else None,
            "available_cash_weight": round(available / total, 6)
            if available is not None and total
            else None,
            "pending_buy_notional_nc": round(buy_notional, 2),
            "pending_to_available_cash_pct": round(buy_notional / available * 100.0, 6)
            if available
            else None,
            "source": "NM-07 authoritative valuation and NM-10 reserved cash",
            "limitations": [
                "Pending exposure is buy notional from active paper orders only.",
                "Cash weights are analytical and do not allocate or reserve capital.",
            ],
        }

    def _market_data(
        self,
        evidence: RiskEvidenceBook,
        holdings: Sequence[Mapping[str, Any]],
        config: RiskThresholds,
    ) -> dict[str, Any]:
        bars: dict[str, list[dict[str, Any]]] = {}
        missing: list[str] = []
        versions: list[dict[str, Any]] = []
        for holding in holdings[: int(config.max_positions)]:
            instrument = _text(holding.get("instrument_id"), 160).upper()
            if not instrument:
                continue
            points = normalise_bars(
                evidence.bars(instrument, int(config.max_series_points)),
                max_points=int(config.max_series_points),
            )
            bars[instrument] = points
            if not points:
                missing.append(instrument)
            else:
                versions.append(
                    {
                        "instrument_id": instrument,
                        "observations": len(points),
                        "latest_at": points[-1]["at"].isoformat(),
                        "provider": points[-1].get("provider", ""),
                    }
                )
        return {
            "bars": bars,
            "missing_bars": missing,
            "versions": versions,
            "positions_omitted": max(0, len(holdings) - int(config.max_positions)),
        }

    def _position_analysis(
        self,
        *,
        portfolio_id: str,
        evidence: RiskEvidenceBook,
        holdings: Sequence[Mapping[str, Any]],
        total: float | None,
        data: Mapping[str, Any],
        config: RiskThresholds,
        now: datetime,
        horizon: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        bars_by = _dict(data.get("bars"))
        for holding in holdings[: int(config.max_positions)]:
            instrument = _text(holding.get("instrument_id"), 160).upper()
            if not instrument:
                continue
            points = bars_by.get(instrument, [])
            value = _position_value(holding)
            weight = _num(holding.get("weight"))
            if weight is None and total and value is not None:
                weight = value / total
            weight = max(0.0, weight or 0.0)
            vol = volatility_metrics(points, config)
            price_dd = drawdown_episodes(
                [{"at": row["at"], "total_value": row["close"]} for row in points]
            )
            instrument_evidence = evidence.instrument(instrument)
            spread = _num(
                instrument_evidence.get("bid_ask_spread_bps")
                or _dict(instrument_evidence.get("instrument")).get("bid_ask_spread_bps")
            )
            liquidity = liquidity_metrics(points, value, config, spread_bps=spread)
            forecast = self._forecast_view(evidence, instrument, horizon, config, now)
            research = self._research_view(instrument_evidence)
            agent = evidence.agent(instrument)
            regime = self._regime_view(instrument_evidence)
            nm08_position = evidence.position_risk(portfolio_id, instrument)
            concentration_level = (
                RiskLevel.HIGH.value
                if weight >= float(config.max_position_weight)
                else RiskLevel.ELEVATED.value
                if weight >= float(config.max_position_weight) * 0.75
                else RiskLevel.NORMAL.value
            )
            loss = _position_return(holding)
            loss_level = level_from_loss(loss, config)
            forecast_level = self._forecast_risk(forecast, config)
            research_level = (
                RiskLevel.ELEVATED.value
                if research["negative_factor_count"]
                or research["stance"] in {"bearish", "negative", "sell"}
                or _text(agent.get("action"), 20).lower() == "sell"
                else RiskLevel.NORMAL.value
            )
            levels = [
                concentration_level,
                vol["risk_level"],
                level_from_drawdown(price_dd.get("current_drawdown_pct"), config),
                liquidity["risk_level"],
                forecast_level,
                research_level,
                loss_level,
            ]
            risk_level = _max_level(levels)
            if not points or not any(
                value != RiskLevel.INSUFFICIENT_DATA.value
                for value in (vol["risk_level"], liquidity["risk_level"], forecast_level)
            ):
                risk_level = RiskLevel.INSUFFICIENT_DATA.value
            contributors = [
                name
                for name, level in zip(
                    (
                        "position_concentration",
                        "volatility",
                        "drawdown",
                        "liquidity",
                        "forecast",
                        "research_or_agent",
                        "unrealized_loss",
                    ),
                    levels,
                    strict=True,
                )
                if _rank(level) == _rank(risk_level) and _rank(level) > 0
            ]
            rows.append(
                {
                    "assessment_id": stable_id("ripos_", instrument, data.get("versions")),
                    "instrument_id": instrument,
                    "sector": _text(holding.get("sector"), 80) or "unknown",
                    "industry": _text(holding.get("industry"), 80)
                    or _text(holding.get("sector"), 80)
                    or "unknown",
                    "quantity": _text(holding.get("quantity"), 40),
                    "average_cost": _text(holding.get("average_cost"), 40),
                    "current_price": _text(
                        holding.get("last_price") or holding.get("current_price"), 40
                    ),
                    "market_value_nc": None if value is None else money_text(value),
                    "market_value_inr": None if value is None else money_text(value),
                    "weight": round(weight, 6),
                    "unrealized_pnl_nc": _text(holding.get("unrealized_pnl"), 40),
                    "return_pct": None if loss is None else round(loss * 100.0, 6),
                    "volatility": vol,
                    "drawdown": price_dd,
                    "liquidity": liquidity,
                    "liquidity_class": liquidity.get("liquidity_class"),
                    "forecast": forecast,
                    "research": research,
                    "agent": agent,
                    "regime": regime,
                    "nm08_reference": _text(
                        nm08_position.get("decision_id") or nm08_position.get("outcome"), 160
                    )
                    or None,
                    "risk_category": contributors[0] if contributors else "none",
                    "risk_contributors": contributors,
                    "risk_level": risk_level,
                    "recommended_action": self._position_action(risk_level, liquidity, forecast),
                    "evidence_score": None,
                    "score_kind": "no_arbitrary_position_risk_probability",
                    "advisory_only": True,
                    "financial_mutation": False,
                    "created_at": now.isoformat(),
                }
            )
        return rows

    def _forecast_view(
        self,
        evidence: RiskEvidenceBook,
        instrument: str,
        horizon: str,
        config: RiskThresholds,
        now: datetime,
    ) -> dict[str, Any]:
        rows = evidence.forecasts(instrument, horizon)
        valid: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for row in rows:
            validity = _text(row.get("validity") or row.get("status"), 40).lower()
            generated = parse_time(
                row.get("generated_at") or row.get("forecast_generation_timestamp")
            )
            age = (now - generated).total_seconds() / 3600.0 if generated else None
            if validity in WAIT_FORECAST_STATES:
                rejected.append({"forecast_id": row.get("forecast_id"), "reason": "unconfirmed"})
                continue
            if validity not in VALID_FORECAST_STATES and validity not in {"", "unknown"}:
                rejected.append(
                    {"forecast_id": row.get("forecast_id"), "reason": f"validity={validity}"}
                )
                continue
            if age is not None and age > config.max_forecast_age_hours:
                rejected.append({"forecast_id": row.get("forecast_id"), "reason": "stale"})
                continue
            valid.append(row)
        directions = [_text(row.get("direction"), 20).lower() for row in valid]
        ups = directions.count("up")
        downs = directions.count("down")
        direction = (
            "uncertain" if ups and downs else ("up" if ups else "down" if downs else "neutral")
        )
        expected = [_num(row.get("expected_return")) for row in valid]
        expected_values = [value for value in expected if value is not None]
        uncertainties = [_text(row.get("uncertainty"), 20).lower() for row in valid]
        uncertainty = (
            "high"
            if "high" in uncertainties or (ups and downs)
            else "medium"
            if "medium" in uncertainties
            else "low"
        )
        return {
            "available": bool(valid),
            "actionable": bool(valid) and direction in {"up", "down"} and uncertainty != "high",
            "direction": direction,
            "expected_return": round(sum(expected_values) / len(expected_values), 8)
            if expected_values
            else None,
            "uncertainty": uncertainty,
            "model_count": len(
                {str(row.get("model_id") or row.get("forecast_id") or "") for row in valid}
            ),
            "agreement": round(max(ups, downs) / len(valid), 6) if valid else None,
            "validity": "confirmed" if valid else "unavailable",
            "forecast_versions": [
                {
                    "forecast_id": row.get("forecast_id"),
                    "model_id": row.get("model_id"),
                    "generated_at": row.get("generated_at"),
                }
                for row in valid[:10]
            ],
            "rejected": rejected[:10],
        }

    @staticmethod
    def _research_view(instrument_evidence: Mapping[str, Any]) -> dict[str, Any]:
        research = _dict(instrument_evidence.get("research"))
        thesis = _dict(research.get("thesis"))
        factors = items(thesis.get("negative_factors"))
        return {
            "run_id": _text(research.get("run_id"), 160),
            "status": _text(research.get("status"), 40),
            "stance": _text(thesis.get("stance"), 40).lower(),
            "negative_factor_count": len(factors),
            "thesis": thesis,
            "advisory_only": True,
        }

    @staticmethod
    def _regime_view(instrument_evidence: Mapping[str, Any]) -> dict[str, Any]:
        regime = _dict(instrument_evidence.get("regime"))
        primary = _text(regime.get("primary"), 40).lower()
        confidence = _num(regime.get("confidence"))
        return {
            "primary": primary or "uncertain",
            "confidence": confidence,
            "uncertain": primary in {"", "uncertain", "unknown"}
            or (confidence is not None and confidence < 0.5),
            "source": "PI-04/PI-16 stored regime evidence",
        }

    @staticmethod
    def _forecast_risk(forecast: Mapping[str, Any], config: RiskThresholds) -> str:
        del config
        if not forecast.get("actionable"):
            return (
                RiskLevel.INSUFFICIENT_DATA.value
                if not forecast.get("available")
                else RiskLevel.ELEVATED.value
            )
        direction = forecast.get("direction")
        expected = _num(forecast.get("expected_return"))
        if direction == "down" and expected is not None and expected < 0:
            if forecast.get("uncertainty") == "high" or expected <= -0.10:
                return RiskLevel.HIGH.value
            return RiskLevel.ELEVATED.value
        if direction == "uncertain" or forecast.get("uncertainty") == "high":
            return RiskLevel.ELEVATED.value
        return RiskLevel.NORMAL.value

    @staticmethod
    def _position_action(
        level: str, liquidity: Mapping[str, Any], forecast: Mapping[str, Any]
    ) -> str:
        if level == RiskLevel.CRITICAL.value:
            return ProtectiveRecommendation.EXIT_REVIEW.value
        if level == RiskLevel.HIGH.value:
            return ProtectiveRecommendation.REDUCE_EXPOSURE.value
        if liquidity.get("liquidity_class") in {
            LiquidityClass.LOW_LIQUIDITY.value,
            LiquidityClass.VERY_LOW_LIQUIDITY.value,
        }:
            return ProtectiveRecommendation.WAIT_FOR_BETTER_LIQUIDITY.value
        if forecast.get("direction") == "down":
            return ProtectiveRecommendation.REVIEW_POSITION.value
        if level == RiskLevel.ELEVATED.value:
            return ProtectiveRecommendation.REVIEW_POSITION.value
        return ProtectiveRecommendation.MAINTAIN_POSITION.value

    def _correlation_analysis(
        self,
        evidence: RiskEvidenceBook,
        positions: Sequence[Mapping[str, Any]],
        data: Mapping[str, Any],
        config: RiskThresholds,
    ) -> dict[str, Any]:
        instruments = [
            str(row.get("instrument_id")) for row in positions if row.get("instrument_id")
        ]
        bars = _dict(data.get("bars"))
        matrix: dict[str, dict[str, Any]] = {instrument: {} for instrument in instruments}
        for instrument in instruments:
            matrix[instrument][instrument] = 1.0 if bars.get(instrument) else None
        pairs: list[dict[str, Any]] = []
        for left, right in islice(combinations(instruments, 2), int(config.max_correlation_pairs)):
            result = pairwise_correlation(bars.get(left, []), bars.get(right, []), config)
            entry = {
                "instrument_a": left,
                "instrument_b": right,
                "correlation": result.get("value"),
                "paired_observations": result.get("paired_observations", 0),
                "sufficient": result.get("sufficient", False),
                "risk_level": result.get("risk_level"),
                "change": result.get("change"),
                "change_state": result.get("change_state"),
                "reason": result.get("reason", ""),
            }
            pairs.append(entry)
            matrix[left][right] = entry["correlation"]
            matrix[right][left] = entry["correlation"]
        valid = [row for row in pairs if row["sufficient"]]
        omitted_pairs = max(0, len(instruments) * (len(instruments) - 1) // 2 - len(pairs))
        high = [row for row in valid if row["risk_level"] in {RiskLevel.HIGH.value}]
        elevated = [row for row in valid if row["risk_level"] == RiskLevel.ELEVATED.value]
        sector_values: dict[str, list[float]] = {}
        by_instrument = {str(row.get("instrument_id")): row for row in positions}
        for row in valid:
            sector_a = str(by_instrument.get(row["instrument_a"], {}).get("sector") or "unknown")
            sector_b = str(by_instrument.get(row["instrument_b"], {}).get("sector") or "unknown")
            if sector_a == sector_b and row["correlation"] is not None:
                sector_values.setdefault(sector_a, []).append(float(row["correlation"]))
        change = [row for row in valid if row["change_state"] in {"increasing", "breakdown"}]
        if not valid:
            risk_level = RiskLevel.INSUFFICIENT_DATA.value
        else:
            risk_level = _max_level([row["risk_level"] for row in valid])
        return {
            "status": "completed" if valid else "insufficient_data",
            "matrix": matrix,
            "pairs": pairs,
            "pair_count": len(pairs),
            "pairs_omitted": omitted_pairs,
            "valid_pair_count": len(valid),
            "highly_correlated": high,
            "elevated_correlations": elevated,
            "correlation_changes": change,
            "sector_correlation": [
                {
                    "sector": key,
                    "average": round(sum(values) / len(values), 6),
                    "pairs": len(values),
                }
                for key, values in sorted(sector_values.items())
            ],
            "benchmark": self._benchmark_correlation(evidence, instruments, data, config),
            "risk_level": risk_level,
            "min_observations": config.min_correlation_observations,
            "max_pairs": config.max_correlation_pairs,
            "alignment": "exact timestamp intersection",
            "limitations": [
                "Historical correlation is not stable and is not a forecast.",
                "Different trading calendars reduce the paired sample rather than shifting it.",
                "No correlation is calculated from mismatched or non-overlapping observations.",
            ],
        }

    def _benchmark_correlation(
        self,
        evidence: RiskEvidenceBook,
        instruments: Sequence[str],
        data: Mapping[str, Any],
        config: RiskThresholds,
    ) -> dict[str, Any]:
        benchmark = normalise_bars(
            evidence.benchmark(BENCHMARK_INSTRUMENT, int(config.max_series_points)),
            max_points=int(config.max_series_points),
        )
        if not benchmark:
            return {
                "available": False,
                "instrument_id": BENCHMARK_INSTRUMENT,
                "reason": "benchmark history is unavailable",
            }
        bars = _dict(data.get("bars"))
        rows = []
        for instrument in instruments[:20]:
            result = pairwise_correlation(bars.get(instrument, []), benchmark, config)
            rows.append(
                {
                    "instrument_id": instrument,
                    "correlation": result.get("value"),
                    "paired_observations": result.get("paired_observations", 0),
                    "sufficient": result.get("sufficient", False),
                }
            )
        return {"available": True, "instrument_id": BENCHMARK_INSTRUMENT, "pairs": rows}

    def _portfolio_volatility(
        self,
        positions: Sequence[Mapping[str, Any]],
        data: Mapping[str, Any],
        config: RiskThresholds,
    ) -> dict[str, Any]:
        bars = _dict(data.get("bars"))
        weights = {
            str(row.get("instrument_id")): float(row.get("weight") or 0.0) for row in positions
        }
        usable = {instrument: bars.get(instrument, []) for instrument in weights}
        if not usable or any(not points for points in usable.values()):
            return {
                "status": "insufficient_data",
                "risk_level": RiskLevel.INSUFFICIENT_DATA.value,
                "reason": "portfolio volatility needs a stored series for every held instrument",
                "per_position": [row.get("volatility") for row in positions],
            }
        returns_maps = {instrument: _returns(points) for instrument, points in usable.items()}
        common = sorted(set.intersection(*(set(mapping) for mapping in returns_maps.values())))
        if len(common) < int(config.min_observations):
            return {
                "status": "insufficient_data",
                "risk_level": RiskLevel.INSUFFICIENT_DATA.value,
                "reason": f"{len(common)} aligned return(s); at least "
                f"{config.min_observations} are required",
                "aligned_observations": len(common),
            }
        blended = []
        synthetic = []
        level = 100.0
        for at in common:
            value = sum(
                weights[instrument] * returns_maps[instrument][at] for instrument in weights
            )
            blended.append(value)
            level *= 1.0 + value
            synthetic.append({"at": at, "close": level})
        result = volatility_metrics(synthetic, config, role="portfolio")
        result["aligned_observations"] = len(common)
        result["weights"] = {key: round(value, 6) for key, value in weights.items()}
        result["methodology"]["portfolio_returns"] = "weighted sum of aligned simple returns"
        result["per_position"] = [
            {"instrument_id": row.get("instrument_id"), "volatility": row.get("volatility")}
            for row in positions
        ]
        return result

    def _portfolio_drawdown(
        self,
        evidence: RiskEvidenceBook,
        portfolio_id: str,
        total: float | None,
        config: RiskThresholds,
    ) -> dict[str, Any]:
        rows = evidence.valuation_history(portfolio_id, int(config.max_history))
        result = drawdown_episodes(rows)
        result["risk_level"] = level_from_drawdown(result.get("current_drawdown_pct"), config)
        result["source"] = "authoritative NM-07 valuation snapshots"
        if total is not None:
            result["current_value_nc"] = round(total, 2)
            peak = _num(result.get("peak_value_nc"))
            if peak is not None:
                result["current_drawdown_nc"] = round(max(0.0, peak - total), 2)
        return result

    def _portfolio_liquidity(
        self, positions: Sequence[Mapping[str, Any]], config: RiskThresholds
    ) -> dict[str, Any]:
        del config
        rows = [row.get("liquidity") or {} for row in positions]
        valid = [row for row in rows if row.get("status") == "completed"]
        covered = [
            row
            for row in valid
            if row.get("liquidity_class") != LiquidityClass.INSUFFICIENT_DATA.value
        ]
        uncovered = [
            {
                "instrument_id": str(position.get("instrument_id") or ""),
                "weight": float(position.get("weight") or 0.0),
                "reason": _text(_dict(position.get("liquidity")).get("reason"), 200)
                or "insufficient volume or position-value evidence",
            }
            for position in positions
            if _dict(position.get("liquidity")).get("liquidity_class")
            == LiquidityClass.INSUFFICIENT_DATA.value
        ]
        uncovered_weight = 0.0
        covered_weight = 0.0
        for position in positions:
            weight = float(position.get("weight") or 0.0)
            if (
                _dict(position.get("liquidity")).get("liquidity_class")
                == LiquidityClass.INSUFFICIENT_DATA.value
            ):
                uncovered_weight += weight
            else:
                covered_weight += weight
        if not covered:
            return {
                "status": "insufficient_data",
                "liquidity_class": LiquidityClass.INSUFFICIENT_DATA.value,
                "risk_level": RiskLevel.INSUFFICIENT_DATA.value,
                "reason": "no position has sufficient volume observations",
                "positions": rows,
                "uncovered_positions": uncovered,
                "uncovered_weight": round(uncovered_weight, 6),
                "covered_weight": round(covered_weight, 6),
            }
        worst = max(covered, key=lambda row: float(row.get("position_to_adv") or 0.0))
        weighted_days = 0.0
        for position, row in zip(positions, rows, strict=True):
            if row.get("liquidity_class") == LiquidityClass.INSUFFICIENT_DATA.value:
                continue
            if row.get("status") != "completed":
                continue
            weighted_days += float(row.get("position_to_adv") or 0.0) * float(
                position.get("weight") or 0.0
            )
        level = _max_level([row.get("risk_level") for row in covered])
        if uncovered_weight > 0 and _rank(level) < _rank(RiskLevel.ELEVATED.value):
            level = RiskLevel.ELEVATED.value
        return {
            "status": "completed_with_warnings" if uncovered else "completed",
            "liquidity_class": worst.get("liquidity_class"),
            "risk_level": level,
            "weighted_days_to_liquidate": round(weighted_days, 4),
            "worst_position": worst,
            "positions": rows,
            "covered_positions": len(covered),
            "covered_weight": round(covered_weight, 6),
            "uncovered_positions": uncovered,
            "uncovered_weight": round(uncovered_weight, 6),
            "market_depth": "unavailable unless supplied by the provider",
            "limitations": [
                "The entire position may not liquidate at the last traded price.",
                "Bid-ask spread and market depth are never fabricated.",
                "Uncovered positions are reported explicitly and are not treated as liquid.",
            ],
        }

    def _regime_analysis(
        self,
        evidence: RiskEvidenceBook,
        positions: Sequence[Mapping[str, Any]],
        config: RiskThresholds,
    ) -> dict[str, Any]:
        del evidence, config
        rows = [row.get("regime") or {} for row in positions]
        weighted: dict[str, float] = {}
        confidence: list[float] = []
        for row, position in zip(rows, positions, strict=True):
            primary = _text(row.get("primary"), 40) or "uncertain"
            weighted[primary] = weighted.get(primary, 0.0) + float(position.get("weight") or 0.0)
            if row.get("confidence") is not None:
                confidence.append(float(row["confidence"]))
        if not weighted:
            return {
                "primary": "uncertain",
                "confidence": None,
                "risk_level": RiskLevel.INSUFFICIENT_DATA.value,
                "uncertain": True,
                "source": "PI-04/PI-16",
            }
        primary = max(weighted, key=lambda key: weighted[key])
        avg_confidence = sum(confidence) / len(confidence) if confidence else None
        if primary in {"bear", "bearish", "high_volatility", "high_vol"}:
            level = RiskLevel.HIGH.value
        elif avg_confidence is None or avg_confidence < 0.5:
            level = RiskLevel.ELEVATED.value
        else:
            level = RiskLevel.NORMAL.value
        return {
            "primary": primary,
            "confidence": None if avg_confidence is None else round(avg_confidence, 6),
            "weighted_exposure": {key: round(value, 6) for key, value in sorted(weighted.items())},
            "uncertain": avg_confidence is None or avg_confidence < 0.5,
            "risk_level": level,
            "source": "PI-04/PI-16 stored regime evidence",
            "limitations": ["Uncertain regimes are not treated as confirmed events."],
        }

    def _warnings(
        self,
        *,
        portfolio: str,
        positions: Sequence[Mapping[str, Any]],
        concentration: Mapping[str, Any],
        correlation: Mapping[str, Any],
        volatility: Mapping[str, Any],
        drawdown: Mapping[str, Any],
        liquidity: Mapping[str, Any],
        regime: Mapping[str, Any],
        freshness: Mapping[str, Any],
        pending: Mapping[str, Any],
        config: RiskThresholds,
        now: datetime,
    ) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []
        if freshness.get("stale"):
            warnings.append(
                self._warning(
                    portfolio,
                    "",
                    "data",
                    "stale_market_data",
                    AlertSeverity.WARNING.value,
                    "Timestamped market data is stale.",
                    freshness,
                    now,
                )
            )
        if pending.get("count"):
            warnings.append(
                self._warning(
                    portfolio,
                    "",
                    "capital",
                    "pending_order_exposure",
                    AlertSeverity.INFORMATIONAL.value,
                    "Pending orders create unexecuted exposure.",
                    pending,
                    now,
                )
            )
            available_cash = _num(pending.get("available_cash_nc"))
            buy_notional = _num(pending.get("buy_notional_nc")) or 0.0
            if available_cash is None:
                warnings.append(
                    self._warning(
                        portfolio,
                        "",
                        "capital",
                        "pending_cash_evidence_missing",
                        AlertSeverity.WATCH.value,
                        "Pending buy exposure cannot be compared with available cash because "
                        "cash evidence is unavailable.",
                        pending,
                        now,
                    )
                )
            elif buy_notional > available_cash:
                warnings.append(
                    self._warning(
                        portfolio,
                        "",
                        "capital",
                        "pending_orders_exceed_available_cash",
                        AlertSeverity.HIGH_ALERT.value,
                        "Pending buy exposure exceeds available cash after reservations.",
                        {
                            "pending_buy_notional_nc": buy_notional,
                            "available_cash_nc": available_cash,
                        },
                        now,
                    )
                )
        for row in positions:
            instrument = str(row.get("instrument_id") or "")
            if _rank(row.get("risk_level")) >= _rank(RiskLevel.HIGH.value):
                warnings.append(
                    self._warning(
                        portfolio,
                        instrument,
                        "position",
                        "position_risk_high",
                        _severity(row.get("risk_level")),
                        "Position risk is high.",
                        row,
                        now,
                    )
                )
            if row.get("liquidity_class") in {
                LiquidityClass.LOW_LIQUIDITY.value,
                LiquidityClass.VERY_LOW_LIQUIDITY.value,
            }:
                warnings.append(
                    self._warning(
                        portfolio,
                        instrument,
                        "liquidity",
                        "liquidity_low",
                        _severity(_dict(row.get("liquidity")).get("risk_level")),
                        "Position liquidity is low.",
                        _dict(row.get("liquidity")),
                        now,
                    )
                )
            if row.get("liquidity_class") == LiquidityClass.INSUFFICIENT_DATA.value:
                warnings.append(
                    self._warning(
                        portfolio,
                        instrument,
                        "liquidity",
                        "liquidity_data_missing",
                        AlertSeverity.WATCH.value,
                        "Position liquidity is unknown; it is not treated as liquid.",
                        _dict(row.get("liquidity")),
                        now,
                    )
                )
            forecast = _dict(row.get("forecast"))
            if forecast.get("direction") == "down" or forecast.get("actionable") is False:
                warnings.append(
                    self._warning(
                        portfolio,
                        instrument,
                        "forecast",
                        "forecast_degradation",
                        AlertSeverity.WATCH.value
                        if forecast.get("available")
                        else AlertSeverity.WARNING.value,
                        "Forecast evidence is negative, unconfirmed or unavailable.",
                        forecast,
                        now,
                    )
                )
            draw = _dict(row.get("drawdown"))
            if _rank(level_from_drawdown(draw.get("current_drawdown_pct"), config)) >= _rank(
                RiskLevel.ELEVATED.value
            ):
                warnings.append(
                    self._warning(
                        portfolio,
                        instrument,
                        "drawdown",
                        "position_drawdown",
                        AlertSeverity.WARNING.value,
                        "Position drawdown is elevated.",
                        draw,
                        now,
                    )
                )
        if _rank(concentration.get("risk_level")) >= _rank(RiskLevel.ELEVATED.value):
            warnings.append(
                self._warning(
                    portfolio,
                    "",
                    "concentration",
                    "portfolio_concentration",
                    _severity(concentration.get("risk_level")),
                    "Portfolio concentration is elevated.",
                    concentration,
                    now,
                )
            )
        if correlation.get("highly_correlated"):
            warnings.append(
                self._warning(
                    portfolio,
                    "",
                    "correlation",
                    "correlation_high",
                    AlertSeverity.HIGH_ALERT.value,
                    "Highly correlated holdings increase combined downside.",
                    correlation,
                    now,
                )
            )
        if correlation.get("correlation_changes"):
            warnings.append(
                self._warning(
                    portfolio,
                    "",
                    "correlation",
                    "correlation_shift",
                    AlertSeverity.WATCH.value,
                    "Pairwise correlation changed materially.",
                    correlation,
                    now,
                )
            )
        if _rank(volatility.get("risk_level")) >= _rank(RiskLevel.ELEVATED.value):
            warnings.append(
                self._warning(
                    portfolio,
                    "",
                    "volatility",
                    "portfolio_volatility",
                    _severity(volatility.get("risk_level")),
                    "Portfolio volatility is elevated.",
                    volatility,
                    now,
                )
            )
        if _rank(level_from_drawdown(drawdown.get("current_drawdown_pct"), config)) >= _rank(
            RiskLevel.ELEVATED.value
        ):
            warnings.append(
                self._warning(
                    portfolio,
                    "",
                    "drawdown",
                    "portfolio_drawdown",
                    _severity(level_from_drawdown(drawdown.get("current_drawdown_pct"), config)),
                    "Portfolio drawdown is elevated.",
                    drawdown,
                    now,
                )
            )
        if _rank(liquidity.get("risk_level")) >= _rank(RiskLevel.ELEVATED.value):
            warnings.append(
                self._warning(
                    portfolio,
                    "",
                    "liquidity",
                    "portfolio_liquidity",
                    _severity(liquidity.get("risk_level")),
                    "Portfolio liquidity is constrained.",
                    liquidity,
                    now,
                )
            )
        if _rank(regime.get("risk_level")) >= _rank(RiskLevel.ELEVATED.value) or regime.get(
            "uncertain"
        ):
            warnings.append(
                self._warning(
                    portfolio,
                    "",
                    "market_regime",
                    "regime_risk",
                    _severity(regime.get("risk_level")),
                    "Regime is defensive or uncertain.",
                    regime,
                    now,
                )
            )
        return warnings

    def _warning(
        self,
        portfolio: str,
        instrument: str,
        category: str,
        code: str,
        severity: str,
        explanation: str,
        evidence: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        return {
            "alert_id": stable_id("rialert_", portfolio, instrument, category, code),
            "dedupe_key": f"{portfolio}|{instrument}|{category}|{code}",
            "portfolio_id": portfolio,
            "instrument_id": instrument,
            "risk_category": category,
            "severity": severity,
            "status": "open",
            "code": code,
            "explanation": explanation,
            "evidence": dict(evidence),
            "observed_at": now.isoformat(),
            "review_condition": "Re-evaluate after the next authoritative snapshot "
            "or when the condition clears.",
            "recommended_response": ProtectiveRecommendation.INCREASE_MONITORING.value,
            "advisory_only": True,
        }

    def _persist_alerts(
        self, warnings: Sequence[Mapping[str, Any]], *, config: RiskThresholds
    ) -> tuple[list[dict[str, Any]], int]:
        created = 0
        rows: list[dict[str, Any]] = []
        for warning in warnings[: int(config.max_alerts)]:
            body, is_new = self._store.save_alert(
                warning, cooldown_hours=float(config.alert_cooldown_hours)
            )
            rows.append(body)
            created += 1 if is_new else 0
        return rows, created

    def _recommendations(
        self,
        *,
        portfolio: str,
        positions: Sequence[Mapping[str, Any]],
        warnings: Sequence[Mapping[str, Any]],
        concentration: Mapping[str, Any],
        correlation: Mapping[str, Any],
        liquidity: Mapping[str, Any],
        risk_level: str,
        risk: Mapping[str, Any],
        config: RiskThresholds,
        now: datetime,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        nm08_reference = _text(risk.get("decision_id"), 160) or None

        def add(instrument: str, category: str, action: str, severity: str, evidence: Any) -> None:
            rows.append(
                {
                    "recommendation_id": stable_id(
                        "rirec_", portfolio, instrument, category, action
                    ),
                    "portfolio_id": portfolio,
                    "instrument_id": instrument,
                    "risk_category": category,
                    "severity": severity,
                    "action": action,
                    "triggering_evidence": dict(evidence)
                    if isinstance(evidence, Mapping)
                    else {"value": evidence},
                    "historical_context": "See the linked risk assessment and "
                    "authoritative valuation history.",
                    "expected_portfolio_impact": "Advisory; NM-08 must assess any proposed action.",
                    "suggested_response": action.replace("_", " ").title(),
                    "horizon": "5d",
                    "nm08_reference": nm08_reference,
                    "nm08_required": True,
                    "created_at": now.isoformat(),
                    "config_version": config.version,
                    "workflow": [
                        "PI-21 review",
                        "NM-08 assessment",
                        "PI-12 orchestration",
                        "NM-10 paper execution",
                    ],
                    "advisory_only": True,
                    "execution_authorized": False,
                    "financial_mutation": False,
                    "order_submission": False,
                }
            )

        if risk_level in {RiskLevel.CRITICAL.value, RiskLevel.HIGH.value}:
            add(
                "",
                "portfolio",
                ProtectiveRecommendation.REDUCE_NEW_ENTRIES.value,
                _severity(risk_level),
                {"risk_level": risk_level},
            )
        if risk_level == RiskLevel.CRITICAL.value:
            add(
                "",
                "portfolio",
                ProtectiveRecommendation.AVOID_NEW_EXPOSURE.value,
                AlertSeverity.CRITICAL_ALERT.value,
                {"risk_level": risk_level},
            )
        if _rank(concentration.get("risk_level")) >= _rank(RiskLevel.ELEVATED.value):
            add(
                "",
                "concentration",
                ProtectiveRecommendation.REVIEW_SECTOR_CONCENTRATION.value,
                _severity(concentration.get("risk_level")),
                concentration,
            )
        if correlation.get("highly_correlated") or correlation.get("elevated_correlations"):
            add(
                "",
                "correlation",
                ProtectiveRecommendation.REVIEW_CORRELATED_HOLDINGS.value,
                _severity(correlation.get("risk_level")),
                correlation,
            )
        if _rank(liquidity.get("risk_level")) >= _rank(RiskLevel.ELEVATED.value):
            add(
                "",
                "liquidity",
                ProtectiveRecommendation.WAIT_FOR_BETTER_LIQUIDITY.value,
                _severity(liquidity.get("risk_level")),
                liquidity,
            )
        if not warnings:
            add(
                "",
                "portfolio",
                ProtectiveRecommendation.MAINTAIN_POSITION.value,
                AlertSeverity.INFORMATIONAL.value,
                {"warnings": 0},
            )
        for row in positions:
            action = _text(row.get("recommended_action"), 80)
            if action and action != ProtectiveRecommendation.MAINTAIN_POSITION.value:
                add(
                    str(row.get("instrument_id") or ""),
                    str(row.get("risk_category") or "position"),
                    action,
                    _severity(row.get("risk_level")),
                    row,
                )
        # Keep one recommendation per stable key while preserving order.
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            unique.setdefault(str(row["recommendation_id"]), row)
        return list(unique.values())

    @staticmethod
    def _contributors(
        positions: Sequence[Mapping[str, Any]],
        concentration: Mapping[str, Any],
        correlation: Mapping[str, Any],
        volatility: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        rows = [
            {
                "kind": "position",
                "instrument_id": row.get("instrument_id"),
                "risk_level": row.get("risk_level"),
                "weight": row.get("weight"),
                "contributors": row.get("risk_contributors") or [],
            }
            for row in sorted(
                positions, key=lambda item: _rank(item.get("risk_level")), reverse=True
            )[:10]
        ]
        rows.extend(
            [
                {
                    "kind": "concentration",
                    "risk_level": concentration.get("risk_level"),
                    "metric": concentration.get("hhi"),
                },
                {
                    "kind": "correlation",
                    "risk_level": correlation.get("risk_level"),
                    "metric": correlation.get("valid_pair_count"),
                },
                {
                    "kind": "volatility",
                    "risk_level": volatility.get("risk_level"),
                    "metric": _dict(volatility.get("annualized_volatility_pct")).get("value"),
                },
            ]
        )
        return rows

    @staticmethod
    def _trend(
        previous: Mapping[str, Any] | None, risk_level: object, warnings: Sequence[object]
    ) -> str:
        if not previous:
            return RiskTrend.UNKNOWN.value
        before = _rank(previous.get("risk_level"))
        after = _rank(risk_level)
        before_warnings = len(previous.get("warnings") or [])
        after_warnings = len(warnings)
        if after > before or (after == before and after_warnings > before_warnings + 2):
            return RiskTrend.DETERIORATING.value
        if after < before or (after == before and after_warnings + 2 < before_warnings):
            return RiskTrend.IMPROVING.value
        return RiskTrend.STABLE.value

    @staticmethod
    def _limitations(status: str, missing_bars: Sequence[str]) -> list[str]:
        rows = [
            "Risk levels are analytical classifications, not NM-08 decisions.",
            "Historical correlation and volatility are not stable forecasts.",
            "Hypothetical scenarios are not predictions and do not mutate balances.",
        ]
        if missing_bars:
            rows.append("Missing bar history: " + ", ".join(list(missing_bars)[:20]))
        if status in {AnalysisStatus.INSUFFICIENT_DATA.value, AnalysisStatus.STALE_DATA.value}:
            rows.append(
                "Critical data is missing or stale; no action should be inferred "
                "from this assessment."
            )
        return rows

    # -- scenarios and stress ----------------------------------------------
    def create_scenario(self, payload: Mapping[str, Any], *, actor: str = "") -> dict[str, Any]:
        body = dict(payload or {})
        portfolio = _text(body.get("portfolio_id"), 160)
        kind = _text(body.get("kind"), 60).lower()
        if not portfolio:
            raise RiskIntelligenceValidationError("a portfolio id is required for a scenario")
        if kind not in SCENARIO_KINDS:
            raise RiskIntelligenceValidationError(f"unsupported scenario kind {kind!r}")
        shock = _num(body.get("shock_pct"))
        if shock is None or not -100.0 <= shock <= 100.0:
            raise RiskIntelligenceValidationError("shock_pct must be between -100 and 100")
        if self._store.status()["counts"]["scenarios"] >= MAX_SCENARIOS:
            raise RiskIntelligenceStateError(
                "scenario limit reached; a new bounded scenario cannot be created",
                code="SCENARIO_LIMIT",
            )
        scenario = {
            "scenario_id": stable_id(
                "riscen_", portfolio, kind, _text(body.get("name"), 120), digest(body)
            ),
            "portfolio_id": portfolio,
            "name": _text(body.get("name"), 120) or kind.replace("_", " ").title(),
            "kind": kind,
            "shock_pct": round(float(shock), 6),
            "sector": _text(body.get("sector"), 80),
            "instrument_id": _text(body.get("instrument_id"), 160),
            "description": _text(body.get("description"), 500)
            or "Hypothetical scenario; not a prediction.",
            "assumptions": ["Isolated analytical valuation; no balance mutation."],
            "created_at": self._now().isoformat(),
            "created_by": _text(actor, 160),
            "hypothetical": True,
            "is_forecast": False,
        }
        self._store.save_scenario(scenario)
        return {"ok": True, "scenario": scenario, "financial_mutation": False}

    def scenarios(self, portfolio_id: str = "", *, limit: int = 100) -> dict[str, Any]:
        rows = self._store.scenarios(portfolio_id=portfolio_id, limit=limit)
        return {"ok": True, "scenarios": rows, "total": len(rows), "max_scenarios": MAX_SCENARIOS}

    def scenario_runs(self, portfolio_id: str = "", *, limit: int = 50) -> dict[str, Any]:
        rows = self._store.scenario_runs(portfolio_id=portfolio_id, limit=limit)
        return {"ok": True, "scenario_runs": rows, "total": len(rows)}

    async def run_scenario(
        self,
        portfolio_id: str,
        *,
        scenario_id: str = "",
        config_payload: Mapping[str, Any] | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        latest = self._store.latest_assessment(portfolio_id)
        if latest is None:
            raise RiskIntelligenceStateError(
                "run a risk analysis before a scenario", code="ANALYSIS_REQUIRED"
            )
        if scenario_id:
            scenario = self._store.scenario(scenario_id)
            if _text(scenario.get("portfolio_id"), 160) != portfolio_id:
                raise RiskIntelligenceNotFound(
                    f"scenario {scenario_id!r} does not belong to portfolio {portfolio_id!r}"
                )
        else:
            body = dict(config_payload or {})
            shock_value = _num(body.get("shock_pct"))
            shock = -10.0 if shock_value is None else shock_value
            kind = _text(body.get("kind"), 60).lower() or "market_decline"
            if kind not in SCENARIO_KINDS:
                raise RiskIntelligenceValidationError(f"unsupported scenario kind {kind!r}")
            if not -100.0 <= shock <= 100.0:
                raise RiskIntelligenceValidationError("shock_pct must be between -100 and 100")
            scenario = {
                "scenario_id": stable_id(
                    "riscen_",
                    portfolio_id,
                    kind,
                    shock,
                    body.get("sector"),
                    body.get("instrument_id"),
                ),
                "portfolio_id": portfolio_id,
                "name": _text(body.get("name"), 120) or kind.replace("_", " ").title(),
                "kind": kind,
                "shock_pct": shock,
                "sector": _text(body.get("sector"), 80),
                "instrument_id": _text(body.get("instrument_id"), 160),
                "description": "Inline hypothetical scenario; not a prediction.",
                "assumptions": ["Isolated analytical valuation; no balance mutation."],
                "hypothetical": True,
                "is_forecast": False,
            }
            self._store.save_scenario(scenario)
        positions = latest.get("positions") or []
        kind = _text(scenario.get("kind"), 60).lower()
        shock = float(scenario.get("shock_pct") or 0.0)
        sector = str(scenario.get("sector") or "")
        instrument = str(scenario.get("instrument_id") or "")
        matched = [
            row
            for row in positions
            if (not sector or _text(row.get("sector"), 80).lower() == sector.lower())
            and (
                not instrument or _text(row.get("instrument_id"), 160).upper() == instrument.upper()
            )
        ]
        portfolio = latest.get("portfolio") or {}
        total = _num(portfolio.get("total_value_nc"))
        remaining_cash = _num(portfolio.get("available_cash_nc"))
        pending = _num(portfolio.get("pending_order_exposure_nc")) or 0.0
        status = AnalysisStatus.COMPLETED.value
        reason = ""
        impact: dict[str, Any]
        if kind in {"volatility_expansion", "correlated_decline"}:
            impact = scenario_impact(positions, shock_pct=0.0)
            multiplier = 1.0 + abs(shock) / 100.0 if kind == "volatility_expansion" else 1.0
            losses: list[dict[str, Any]] = []
            missing: list[str] = []
            for row in positions:
                value = _num(row.get("market_value_nc")) or 0.0
                var = _num(_dict(_dict(row.get("volatility")).get("var")).get("value"))
                if var is None:
                    missing.append(str(row.get("instrument_id") or ""))
                    continue
                loss = value * var / 100.0 * multiplier
                losses.append(
                    {
                        "instrument_id": row.get("instrument_id"),
                        "sector": row.get("sector") or "unknown",
                        "before_value_nc": round(value, 2),
                        "estimated_change_nc": round(-loss, 2),
                        "estimated_value_nc": round(value - loss, 2),
                    }
                )
            impact["position_contributions"] = losses
            impact["estimated_change_nc"] = round(
                -sum(abs(float(row["estimated_change_nc"])) for row in losses), 2
            )
            impact["volatility_multiplier"] = round(multiplier, 6)
            impact["missing_volatility_evidence"] = missing
            if not positions:
                status = AnalysisStatus.INSUFFICIENT_DATA.value
                reason = "the portfolio has no positions"
            elif missing or not losses:
                status, reason = (
                    AnalysisStatus.INSUFFICIENT_DATA.value,
                    "scenario requires one-day VaR evidence for every position",
                )
        elif kind == "multiple_pending_orders":
            impact = scenario_impact(positions, shock_pct=0.0)
            if not pending:
                status, reason = (
                    AnalysisStatus.INSUFFICIENT_DATA.value,
                    "the latest assessment records no pending order exposure",
                )
        else:
            impact = scenario_impact(
                positions,
                shock_pct=shock,
                sector=sector,
                instrument=instrument,
            )
            if kind not in {"cash_depletion"} and not positions:
                status = AnalysisStatus.INSUFFICIENT_DATA.value
                reason = "the portfolio has no positions"
            elif kind in {"sector_decline", "sector_rally", "instrument_decline"} and not matched:
                status, reason = (
                    AnalysisStatus.INSUFFICIENT_DATA.value,
                    "the scenario target does not match any assessed position",
                )
        cash_change = float(impact.get("estimated_change_nc") or 0.0)
        if total is not None:
            if kind == "cash_depletion":
                depleted = min(max(0.0, remaining_cash or 0.0), total)
                cash_change = -depleted
                impact["cash_depletion_nc"] = round(depleted, 2)
                impact["estimated_change_nc"] = round(cash_change, 2)
                impact["position_contributions"] = []
            if kind in {"liquidity_deterioration", "combined"} and positions:
                cost = 0.0
                covered_value = 0.0
                total_position_value = 0.0
                missing_liquidity: list[str] = []
                for row in positions:
                    value = _num(row.get("market_value_nc")) or 0.0
                    total_position_value += value
                    liq = _dict(row.get("liquidity"))
                    high = _num(_dict(liq.get("estimated_impact_bps_range")).get("high"))
                    if high is not None:
                        cost += value * high / 10000.0
                        covered_value += value
                    else:
                        missing_liquidity.append(str(row.get("instrument_id") or ""))
                cash_change -= cost
                impact["liquidity_cost_nc"] = round(cost, 2)
                impact["liquidity_cost_coverage_pct"] = round(
                    (covered_value / total_position_value) * 100.0 if total_position_value else 0.0,
                    6,
                )
                impact["missing_liquidity_evidence"] = missing_liquidity
                impact["estimated_change_nc"] = round(cash_change, 2)
                # A position without impact evidence cannot be charged zero: an
                # unmeasured cost would understate the scenario, so it fails closed.
                if not covered_value:
                    status, reason = (
                        AnalysisStatus.INSUFFICIENT_DATA.value,
                        "no position carries liquidity-impact evidence",
                    )
                elif missing_liquidity:
                    status, reason = (
                        AnalysisStatus.INSUFFICIENT_DATA.value,
                        "scenario requires liquidity-impact evidence for every position",
                    )
            if kind == "multiple_pending_orders" and pending:
                pending_loss = pending * float(scenario.get("shock_pct") or 0.0) / 100.0
                cash_change += pending_loss
                impact["pending_order_loss_nc"] = round(pending_loss, 2)
                impact["estimated_change_nc"] = round(cash_change, 2)
                impact["position_contributions"] = []
            impact["portfolio_value_before_nc"] = round(total, 2)
            impact["estimated_portfolio_value_nc"] = round(total + cash_change, 2)
            impact["estimated_change_pct"] = round(cash_change / total * 100.0, 6) if total else 0.0
            impact["remaining_cash_nc"] = (
                None
                if remaining_cash is None
                else round(0.0 if kind == "cash_depletion" else remaining_cash, 2)
            )
            impact["remaining_cash_inr"] = impact["remaining_cash_nc"]
            impact["pending_order_exposure_nc"] = round(pending, 2)
        if total is None and status == AnalysisStatus.COMPLETED.value:
            status, reason = (
                AnalysisStatus.INSUFFICIENT_DATA.value,
                "portfolio value is unavailable",
            )
        impact["matched_positions"] = len(matched)
        if reason:
            impact["reason"] = reason
        core = {
            "portfolio_id": portfolio_id,
            "scenario_id": scenario.get("scenario_id"),
            "snapshot_version": latest.get("snapshot_version"),
            "config_digest": latest.get("config_digest"),
            "scenario": scenario,
            "impact": impact,
        }
        result = {
            "ok": True,
            "phase": RI_PHASE,
            "run_id": stable_id(
                "riscenrun_", portfolio_id, scenario.get("scenario_id"), digest(core)
            ),
            "portfolio_id": portfolio_id,
            "assessment_id": latest.get("assessment_id"),
            "scenario_id": scenario.get("scenario_id"),
            "scenario": scenario,
            "status": status,
            "generated_at": self._now().isoformat(),
            "result_digest": digest(core),
            **impact,
            "assumptions": [
                "Hypothetical shock applied to authoritative position market values.",
                "No order, cash, ledger or position record is changed.",
                "Liquidation costs are not silently included in a price shock.",
                "Missing VaR or liquidity evidence is reported, never treated as zero.",
            ],
            "limitations": [
                "A hypothetical scenario is not a prediction.",
                "Correlation and liquidity are not assumed to remain stable.",
            ],
            "methodology": (
                "one-day historical VaR scaled by the volatility-expansion assumption"
                if kind == "volatility_expansion"
                else "simultaneous one-day historical VaR assuming perfect correlation"
                if kind == "correlated_decline"
                else "deterministic shock on isolated position values"
            ),
            "advisory_only": True,
            "financial_mutation": False,
            "order_submission": False,
            "created_by": _text(actor, 160),
        }
        self._store.save_scenario_run(result)
        return result

    async def run_stress(
        self,
        portfolio_id: str,
        *,
        mode: str = "hypothetical",
        scenario_id: str = "",
        shock_pct: float | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        chosen = _text(mode, 40).lower()
        if chosen not in STRESS_MODES:
            raise RiskIntelligenceValidationError(f"unsupported stress mode {chosen!r}")
        shock_value = _num(shock_pct)
        if shock_pct is not None and shock_value is None:
            raise RiskIntelligenceValidationError("shock_pct must be a finite number")
        if shock_value is not None and not -100.0 <= shock_value <= 100.0:
            raise RiskIntelligenceValidationError("shock_pct must be between -100 and 100")
        latest = self._store.latest_assessment(portfolio_id)
        if latest is None:
            raise RiskIntelligenceStateError(
                "run a risk analysis before a stress test", code="ANALYSIS_REQUIRED"
            )
        scenario = self._store.scenario(scenario_id) if scenario_id else None
        if scenario is not None and _text(scenario.get("portfolio_id"), 160) != portfolio_id:
            raise RiskIntelligenceNotFound(
                f"scenario {scenario_id!r} does not belong to portfolio {portfolio_id!r}"
            )
        if scenario is None:
            scenario = {
                "scenario_id": stable_id(
                    "ristresscfg_",
                    portfolio_id,
                    chosen,
                    -10.0 if shock_value is None else shock_value,
                ),
                "name": chosen.replace("_", " ").title(),
                "kind": "market_decline",
                "shock_pct": -10.0 if shock_value is None else shock_value,
                "sector": "",
                "instrument_id": "",
                "hypothetical": True,
                "is_forecast": False,
            }
        positions = latest.get("positions") or []
        portfolio = latest.get("portfolio") or {}
        total = _num(portfolio.get("total_value_nc"))
        status = AnalysisStatus.COMPLETED.value
        assumptions: list[str] = []
        limitations: list[str] = []
        methodology = ""
        result_payload: dict[str, Any]
        hypothetical = True
        if chosen == "historical":
            dd = latest.get("drawdown") or {}
            max_dd = _num(dd.get("max_drawdown_pct"))
            if max_dd is None or total is None:
                status = AnalysisStatus.INSUFFICIENT_DATA.value
                result_payload = {"reason": "authoritative valuation history is insufficient"}
            else:
                hypothetical = False
                result_payload = {
                    "estimated_loss_nc": round(total * max_dd / 100.0, 2),
                    "estimated_loss_pct": round(max_dd, 6),
                    "estimated_value_nc": round(total * (1.0 - max_dd / 100.0), 2),
                    "episodes": dd.get("episodes") or [],
                }
                methodology = "worst historical peak-to-trough valuation drawdown"
                limitations.append("Actual history is not a forecast of future loss.")
        elif chosen in {"hypothetical", "combined"}:
            if not positions:
                status = AnalysisStatus.INSUFFICIENT_DATA.value
                result_payload = {"reason": "the latest assessment has no positions"}
            else:
                impact = scenario_impact(
                    positions,
                    shock_pct=float(scenario.get("shock_pct") or -10.0),
                    sector=str(scenario.get("sector") or ""),
                    instrument=str(scenario.get("instrument_id") or ""),
                )
                result_payload = impact
                methodology = "hypothetical deterministic shock on isolated position values"
                assumptions.append("The configured shock is applied to every matching position.")
                limitations.append("Hypothetical scenario results are not predictions.")
                if chosen == "combined":
                    cost = 0.0
                    covered_value = 0.0
                    total_position_value = 0.0
                    missing_liquidity: list[str] = []
                    for row in positions:
                        liq = _dict(row.get("liquidity"))
                        value = _num(row.get("market_value_nc")) or 0.0
                        total_position_value += value
                        high = _num(_dict(liq.get("estimated_impact_bps_range")).get("high"))
                        if high is None:
                            missing_liquidity.append(str(row.get("instrument_id") or ""))
                            continue
                        cost += value * high / 10000.0
                        covered_value += value
                    result_payload["liquidity_cost_nc"] = round(cost, 2)
                    result_payload["liquidity_cost_coverage_pct"] = round(
                        (covered_value / total_position_value) * 100.0
                        if total_position_value
                        else 0.0,
                        6,
                    )
                    result_payload["missing_liquidity_evidence"] = missing_liquidity
                    result_payload["estimated_change_nc"] = round(
                        float(result_payload.get("estimated_change_nc") or 0.0) - cost, 2
                    )
                    result_payload["estimated_portfolio_value_nc"] = round(
                        float(result_payload.get("estimated_portfolio_value_nc") or 0.0) - cost,
                        2,
                    )
                    assumptions.append("Liquidity impact is added after the price shock.")
                    assumptions.append(
                        "Missing liquidity evidence is reported and is not treated as zero cost."
                    )
                    methodology = "combined price shock and explicit liquidity-impact estimate"
                    if not covered_value:
                        status = AnalysisStatus.INSUFFICIENT_DATA.value
                        result_payload["reason"] = "no position carries liquidity-impact evidence"
                    elif missing_liquidity:
                        status = AnalysisStatus.INSUFFICIENT_DATA.value
                        result_payload["reason"] = (
                            "combined scenario requires liquidity-impact evidence "
                            "for every position"
                        )
        else:
            vals: list[float] = []
            for row in positions:
                value = _num(row.get("market_value_nc")) or 0.0
                var = _num(_dict(_dict(row.get("volatility")).get("var")).get("value"))
                liq = _dict(row.get("liquidity"))
                if chosen in {"volatility", "correlation"}:
                    if var is None:
                        vals = []
                        break
                    vals.append(value * var / 100.0)
                else:
                    liquidation_bps = _num(_dict(liq.get("estimated_impact_bps_range")).get("high"))
                    if liquidation_bps is None:
                        vals = []
                        break
                    vals.append(value * liquidation_bps / 10000.0)
            if not vals or total is None:
                status = AnalysisStatus.INSUFFICIENT_DATA.value
                result_payload = {
                    "reason": f"{chosen} stress requires complete per-position evidence"
                }
            else:
                loss = sum(vals)
                result_payload = {
                    "estimated_loss_nc": round(loss, 2),
                    "estimated_loss_pct": round(loss / total * 100.0, 6) if total else None,
                    "estimated_value_nc": round(total - loss, 2),
                    "position_contributions": [
                        {
                            "instrument_id": row.get("instrument_id"),
                            "estimated_loss_nc": round(value, 2),
                        }
                        for row, value in zip(positions, vals, strict=True)
                    ],
                }
                methodology = (
                    "all positions realise their one-day historical VaR simultaneously "
                    "under an explicit perfect-correlation assumption"
                    if chosen == "correlation"
                    else "all positions realise their one-day historical VaR"
                    if chosen == "volatility"
                    else "position liquidation impact at the high end of the "
                    "explicit participation range"
                )
                assumptions.append(
                    "This is an assumption about simultaneous stress, not a forecast."
                )
            limitations.append("Sample size and historical coverage limit the estimate.")
        if hypothetical and total is not None and "estimated_change_nc" in result_payload:
            change = float(result_payload.get("estimated_change_nc") or 0.0)
            result_payload["portfolio_value_before_nc"] = round(total, 2)
            result_payload["estimated_portfolio_value_nc"] = round(total + change, 2)
            result_payload["estimated_change_pct"] = (
                round(change / total * 100.0, 6) if total else 0.0
            )
        core = {
            "portfolio_id": portfolio_id,
            "mode": chosen,
            "scenario": scenario,
            "assessment_id": latest.get("assessment_id"),
            "snapshot_version": latest.get("snapshot_version"),
            "input_data_versions": latest.get("data_versions") or [],
            "assumptions": assumptions,
            "methodology": methodology,
            "result": result_payload,
        }
        result = {
            "ok": status != AnalysisStatus.CALCULATION_FAILED.value,
            "phase": RI_PHASE,
            "stress_id": stable_id("ristress_", portfolio_id, chosen, digest(core)),
            "portfolio_id": portfolio_id,
            "assessment_id": latest.get("assessment_id"),
            "scenario_id": scenario.get("scenario_id"),
            "mode": chosen,
            "status": status,
            "generated_at": self._now().isoformat(),
            "snapshot_version": latest.get("snapshot_version"),
            "config_version": latest.get("config_version"),
            "input_data_versions": latest.get("data_versions") or [],
            "scenario_configuration": scenario,
            "assumptions": assumptions,
            "methodology": methodology,
            "limitations": limitations,
            "result_digest": digest(core),
            "hypothetical": hypothetical,
            "is_prediction": False,
            **result_payload,
            "advisory_only": True,
            "financial_mutation": False,
            "order_submission": False,
            "created_by": _text(actor, 160),
        }
        self._store.save_stress(result)
        return result

    def stress_results(self, portfolio_id: str = "", *, limit: int = 50) -> dict[str, Any]:
        rows = self._store.stress_results(portfolio_id=portfolio_id, limit=limit)
        return {"ok": True, "stress_results": rows, "total": len(rows)}

    async def recover(self, *, reason: str = "PI-22 operator recovery") -> dict[str, Any]:
        local = self._store.recover(at=self._now())
        return {
            "ok": True,
            "phase": RI_PHASE,
            "reason": _text(reason, 200),
            **local,
            "mode": self._mode(),
            "paper_only": True,
            "live_trading": False,
            "financial_mutation": False,
        }


def _returns(points: Sequence[Mapping[str, Any]]) -> dict[datetime, float]:
    result: dict[datetime, float] = {}
    for previous, current in zip(points, points[1:], strict=False):
        before = _num(previous.get("close"))
        after = _num(current.get("close"))
        at = current.get("at")
        if before and after is not None and before > 0 and isinstance(at, datetime):
            result[at] = (after / before) - 1.0
    return result


__all__ = ["RiskIntelligenceService"]
