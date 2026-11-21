"""PI-21 analysis composition.

The mixin contains no financial write path.  It reads the authoritative book,
composes stored evidence and produces advisory management records.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from novacontrol.novamarket.portfolio_management.evidence import (
    EvidenceBook,
    ManagementSources,
)
from novacontrol.novamarket.portfolio_management.models import (
    MAX_AGENT_REFERENCES,
    MAX_DECISIONS,
    RISK_AUTHORITY,
    CashRecommendation,
    CycleStatus,
    DataStatus,
    DeploymentRecommendation,
    EntryRecommendation,
    ExitRecommendation,
    ManagementAction,
    ManagementConfig,
    PositionAssessment,
    RebalanceAction,
    stable_id,
)

NEGATIVE_STANCES = frozenset({"bearish", "negative", "sell"})
POSITIVE_STANCES = frozenset({"bullish", "positive", "buy"})


def _text(value: object, limit: int = 400) -> str:
    return str(value or "").strip()[:limit]


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _items(value: object) -> list[dict[str, Any]]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [dict(row) for row in value if isinstance(row, Mapping)]
    if isinstance(value, Mapping):
        for key in ("items", "rows", "results", "forecasts", "decisions", "positions", "intents"):
            rows = value.get(key)
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)):
                return [dict(row) for row in rows if isinstance(row, Mapping)]
    return []


def _num(value: object) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def _pct(value: float | None) -> float | None:
    return None if value is None else round(float(value) * 100.0, 4)


def _unique(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _canonical(value: object) -> str:
    text = _text(value, 180).upper()
    return text


def _exchange(value: object) -> str:
    text = _text(value, 40).upper()
    if ":" in text:
        return text.split(":", 1)[0]
    return text or "XNSE"


def _mtime(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=__import__("datetime").timezone.utc)
    return result.astimezone(__import__("datetime").timezone.utc)


class PortfolioManagementAnalysisMixin:
    if TYPE_CHECKING:
        from novacontrol.novamarket.portfolio_management.store import (
            PortfolioManagementStore,
        )

        _store: PortfolioManagementStore
        _sources: ManagementSources

        def _now(self) -> datetime: ...

        def _mode(self, runtime: Mapping[str, Any] | None = None) -> str: ...

    # The methods below are attached to PortfolioManagementService, which
    # provides _now, _mode, _store, _sources and _config.
    def _compose_cycle(
        self,
        *,
        cycle: Mapping[str, Any],
        snapshot: dict[str, Any],
        horizon: str,
        config: ManagementConfig,
        evidence: EvidenceBook,
        started: datetime,
    ) -> dict[str, Any]:
        portfolio_id = str(snapshot["portfolio_id"])
        optimization = evidence.optimization(portfolio_id, horizon)
        targets = self._target_weights(optimization)
        risk = evidence.risk(portfolio_id)
        risk_reference = self._risk_reference(portfolio_id, risk, started)
        total = float(snapshot["valuation"].get("total_value") or 0)
        positions = [
            self._assess_position(
                portfolio_id=portfolio_id,
                holding=row,
                horizon=horizon,
                config=config,
                evidence=evidence,
                targets=targets,
                total=total,
                risk=risk,
            )
            for row in snapshot["positions"]
        ]
        opportunities = self._discover_opportunities(
            portfolio_id=portfolio_id,
            snapshot=snapshot,
            horizon=horizon,
            config=config,
            evidence=evidence,
            targets=targets,
        )
        exposure = self._exposure(snapshot, config, risk, optimization)
        capital = self._capital(snapshot, opportunities, config, positions)
        cash = self._cash(snapshot, capital, config)
        rebalance = self._rebalance(
            portfolio_id=portfolio_id,
            snapshot=snapshot,
            optimization=optimization,
            positions=positions,
            targets=targets,
            config=config,
            risk=risk,
        )
        decisions = self._decisions(
            cycle_id=str(cycle["cycle_id"]),
            snapshot=snapshot,
            positions=positions,
            opportunities=opportunities,
            capital=capital,
            config=config,
            risk_reference=risk_reference,
            targets=targets,
        )
        decisions = self._attach_proposal_candidates(portfolio_id, decisions)
        analytics = self._analytics(snapshot, decisions, positions, rebalance, config)
        data_status = self._data_status(snapshot, positions, evidence)
        status = self._cycle_status(
            mode=str(cycle.get("mode") or self._mode()),
            snapshot=snapshot,
            positions=positions,
            opportunities=opportunities,
            evidence=evidence,
            data_status=data_status,
        )
        forecast_versions: list[dict[str, Any]] = []
        research_references: list[dict[str, Any]] = []
        agent_references: list[dict[str, Any]] = []
        for row in list(positions) + list(opportunities):
            forecast_versions.extend(_items(row.get("forecast_versions")))
            if row.get("research_reference"):
                research_references.append(dict(row["research_reference"]))
            if row.get("agent_reference"):
                agent_references.append(dict(row["agent_reference"]))
        dedup_forecasts = {
            (item.get("forecast_id"), item.get("model_id"), item.get("model_version")): item
            for item in forecast_versions
        }
        dedup_research = {
            (item.get("run_id"), item.get("as_of")): item for item in research_references
        }
        dedup_agents = {
            (item.get("decision_id"), item.get("cycle_id")): item for item in agent_references
        }
        return {
            "status": status,
            "data_status": data_status,
            "portfolio_id": portfolio_id,
            "portfolio_snapshot_version": snapshot["snapshot_version"],
            "completed_at": self._now().isoformat(),
            "market_data_versions": snapshot["data_versions"].get("market_prices", []),
            "forecast_versions": list(dedup_forecasts.values()),
            "research_references": list(dedup_research.values())[:MAX_AGENT_REFERENCES],
            "optimization_reference": {
                "run_id": _text(optimization.get("run_id") or optimization.get("id"), 120),
                "status": _text(optimization.get("status"), 40),
                "advisory_only": True,
                "experimental": True,
            }
            if optimization
            else {"run_id": "", "status": "unavailable", "advisory_only": True},
            "risk_assessment_reference": risk_reference,
            "decision_configuration_version": config.version,
            "configuration_digest": config.digest,
            "agent_references": list(dedup_agents.values())[:MAX_AGENT_REFERENCES],
            "positions": positions,
            "opportunities": opportunities,
            "capital": capital,
            "cash": cash,
            "exposure": exposure,
            "rebalance": rebalance,
            "decisions": decisions,
            "analytics": analytics,
            "warnings": self._cycle_warnings(snapshot, positions, exposure, capital, evidence),
            "execution_outcome": "not_submitted",
            "order_submission": False,
            "financial_mutation": False,
            "advisory_only": True,
            "handoff": {},
        }

    @staticmethod
    def _risk_reference(portfolio_id: str, risk: Mapping[str, Any], at: datetime) -> dict[str, Any]:
        return {
            "authority": RISK_AUTHORITY,
            "portfolio_id": portfolio_id,
            "outcome": _text(risk.get("outcome"), 40) or "unavailable",
            "policy_id": _text(_dict(risk.get("policy")).get("policy_id"), 80),
            "policy_version": _text(_dict(risk.get("policy")).get("version"), 80),
            "restrictions": [_text(item, 260) for item in risk.get("restrictions") or ()],
            "breaches": _items(risk.get("breaches"))[:20],
            "captured_at": at.isoformat(),
            "action_approved": False,
            "note": (
                "This is the recorded NM-08 portfolio report. It is not an approval of any PI-21 "
                "recommendation; action approval requires an AT-03 proposal."
            ),
        }

    def _assess_position(
        self,
        *,
        portfolio_id: str,
        holding: Mapping[str, Any],
        horizon: str,
        config: ManagementConfig,
        evidence: EvidenceBook,
        targets: Mapping[str, float],
        total: float,
        risk: Mapping[str, Any],
    ) -> dict[str, Any]:
        instrument = _canonical(holding.get("instrument_id"))
        payload = evidence.instrument(instrument)
        consensus = self._forecast_consensus(
            evidence.forecasts(instrument, horizon), now=self._now(), config=config
        )
        research = self._research_view(payload)
        regime = self._regime_view(payload)
        agent = self._agent_view(evidence.agent_decision(instrument))
        events = self._event_view(payload)
        risk_position = evidence.risk_position(portfolio_id, instrument)
        protective = evidence.exit_configuration(portfolio_id, instrument)
        policy = evidence.policy_advisory(instrument, horizon, str(regime.get("primary") or ""))
        current_weight = _num(holding.get("weight"))
        target_weight = targets.get(instrument)
        quantity = _num(holding.get("quantity")) or 0.0
        pnl = _num(holding.get("unrealized_pnl"))
        score = 50.0
        supporting: list[str] = []
        contradictory: list[str] = []
        if consensus.get("available"):
            counts = _dict(consensus.get("counts"))
            supporting.append(
                f"Stored {horizon} forecasts: {counts.get('up', 0)} up, "
                f"{counts.get('down', 0)} down, {counts.get('neutral', 0)} neutral."
            )
        else:
            contradictory.append("No current valid forecast is stored for this horizon.")
        expected = _num(consensus.get("expected_return"))
        direction = str(consensus.get("direction") or "uncertain")
        agreement = _num(consensus.get("agreement")) or 0.0
        if expected is not None:
            score += max(-15.0, min(15.0, expected * 200.0))
        if direction == "up":
            score += 12.0 * agreement
            supporting.append("The forecast consensus is upward.")
        elif direction == "down":
            score -= 20.0 * max(0.35, agreement)
            contradictory.append("The forecast consensus is downward.")
        elif direction == "uncertain":
            score -= 8.0
            contradictory.append("Forecast directions disagree or are uncertain.")
        uncertainty = str(consensus.get("uncertainty") or "unknown")
        if uncertainty == "high":
            score -= 8.0
            contradictory.append("Forecast uncertainty is high.")
        elif uncertainty == "low":
            score += 4.0
        if consensus.get("freshness") in {"stale", "delayed"}:
            score -= 5.0
            contradictory.append(f"Forecast data is {consensus['freshness']}.")
        if int(consensus.get("model_count") or 0) >= 2 and direction != "uncertain":
            score += 4.0
        stance = research.get("stance")
        if research.get("status") in {"completed", "completed_with_warnings", "available"}:
            if stance in POSITIVE_STANCES:
                score += 8.0
                supporting.append(f"PI-19 thesis stance is {stance}.")
            elif stance in NEGATIVE_STANCES:
                score -= 12.0
                contradictory.append(f"PI-19 thesis stance is {stance}.")
            elif stance == "insufficient_evidence":
                contradictory.append("PI-19 research has insufficient evidence.")
        if research.get("positive_factors"):
            supporting.extend(research["positive_factors"][:2])
        if research.get("negative_factors"):
            contradictory.extend(research["negative_factors"][:2])
        agent_action = str(agent.get("action") or "").lower()
        if agent_action == "buy":
            score += 8.0
            supporting.append("The PI-17 multi-agent decision is buy (advisory).")
        elif agent_action == "sell":
            score -= 12.0
            contradictory.append("The PI-17 multi-agent decision is sell (advisory).")
        elif agent_action in {"wait", "abstain"}:
            score -= 4.0
            contradictory.append(f"The PI-17 multi-agent decision is {agent_action}.")
        if agent.get("unresolved_conflicts"):
            score -= 5.0
            contradictory.append("PI-17 reports unresolved agent conflicts.")
        if events.get("negative_count"):
            score -= min(10.0, float(events["negative_count"]) * 2.5)
            contradictory.append(f"{events['negative_count']} recorded negative event(s).")
        if events.get("positive_count"):
            score += min(5.0, float(events["positive_count"]) * 1.5)
            supporting.append(f"{events['positive_count']} recorded positive event(s).")
        if current_weight is not None and current_weight > float(config.max_position_weight):
            score -= 10.0
            contradictory.append(
                f"Position weight {current_weight:.2%} exceeds the management threshold."
            )
        if _items(risk_position.get("breaches")):
            hard = any(
                _text(row.get("severity"), 20).lower() == "hard"
                for row in _items(risk_position.get("breaches"))
            )
            score -= 18.0 if hard else 8.0
            contradictory.append(
                "NM-08 records a position-level risk breach; NM-08 remains authoritative."
            )
        if _items(risk.get("restrictions")):
            score -= 5.0
            contradictory.append("NM-08 portfolio restrictions apply.")
        score = _clamp(score)
        has_any = bool(consensus.get("available") or stance or agent_action or protective)
        downward_evidence = direction == "down" and (
            stance in NEGATIVE_STANCES
            or agent_action == "sell"
            or bool(events.get("negative_count"))
        )
        if not has_any:
            assessment = PositionAssessment.INSUFFICIENT_EVIDENCE
        elif score >= 68 and direction == "up":
            assessment = PositionAssessment.STRONG_POSITION
        elif downward_evidence and score < 45:
            assessment = PositionAssessment.EXIT_CANDIDATE
        elif score < 45 and (
            direction == "down" or stance in NEGATIVE_STANCES or agent_action == "sell"
        ):
            assessment = PositionAssessment.REDUCE_POSITION
        elif score < 55:
            assessment = PositionAssessment.REVIEW_POSITION
        else:
            assessment = PositionAssessment.HOLD_POSITION
        if protective:
            if assessment in {
                PositionAssessment.EXIT_CANDIDATE,
                PositionAssessment.REDUCE_POSITION,
            }:
                assessment = PositionAssessment.REVIEW_POSITION
            contradictory.append(
                "An AT-05 protective exit configuration exists; its stop-loss, trailing-stop "
                "and target-price precedence is authoritative."
            )
        action = {
            PositionAssessment.STRONG_POSITION: ManagementAction.HOLD,
            PositionAssessment.HOLD_POSITION: ManagementAction.HOLD,
            PositionAssessment.REVIEW_POSITION: ManagementAction.WAIT,
            PositionAssessment.REDUCE_POSITION: ManagementAction.REDUCE,
            PositionAssessment.EXIT_CANDIDATE: ManagementAction.EXIT,
            PositionAssessment.INSUFFICIENT_EVIDENCE: ManagementAction.WAIT,
        }[assessment]
        exit_recommendation = {
            ManagementAction.EXIT: ExitRecommendation.EXIT,
            ManagementAction.REDUCE: ExitRecommendation.REDUCE,
            ManagementAction.WAIT: ExitRecommendation.WAIT,
            ManagementAction.HOLD: ExitRecommendation.MAINTAIN,
            ManagementAction.ADD: ExitRecommendation.MAINTAIN,
            ManagementAction.OPEN: ExitRecommendation.MAINTAIN,
            ManagementAction.RETAIN_CASH: ExitRecommendation.MAINTAIN,
            ManagementAction.NO_ACTION: ExitRecommendation.MAINTAIN,
        }[action]
        if protective and exit_recommendation in {
            ExitRecommendation.EXIT,
            ExitRecommendation.REDUCE,
        }:
            exit_recommendation = ExitRecommendation.REVIEW_REQUIRED
        if protective and action in {ManagementAction.EXIT, ManagementAction.REDUCE}:
            action = ManagementAction.WAIT
        current_price = _num(holding.get("current_price"))
        current_value = _num(holding.get("current_value")) or 0.0
        return {
            "assessment_id": stable_id(
                "pmpa_", portfolio_id, instrument, holding.get("priced_at"), holding.get("quantity")
            ),
            "portfolio_id": portfolio_id,
            "instrument_id": instrument,
            "exchange": _exchange(holding.get("exchange") or instrument),
            "sector": _text(holding.get("sector"), 80),
            "quantity": quantity,
            "average_cost": _num(holding.get("average_cost")),
            "current_price": current_price,
            "current_value": current_value,
            "invested_capital": quantity * (_num(holding.get("average_cost")) or 0.0),
            "unrealized_pnl": pnl,
            "return_pct": (
                _pct(pnl / (quantity * (_num(holding.get("average_cost")) or 0.0)))
                if pnl is not None and quantity * (_num(holding.get("average_cost")) or 0.0) > 0
                else None
            ),
            "holding_hours": _num(holding.get("holding_hours")),
            "holding_days": (_num(holding.get("holding_hours")) or 0.0) / 24.0,
            "position_weight": current_weight,
            "target_weight": target_weight,
            "weight_gap": (
                target_weight - current_weight
                if target_weight is not None and current_weight is not None
                else None
            ),
            "contribution": _num(holding.get("contribution")),
            "stale": bool(holding.get("stale")),
            "missing_price": bool(holding.get("missing_price")),
            "assessment": assessment.value,
            "action": action.value,
            "exit_recommendation": exit_recommendation.value,
            "evidence_score": _round(score, 2),
            "score_kind": "evidence_score_not_probability",
            "forecast": consensus,
            "forecast_versions": consensus.get("forecast_versions", []),
            "forecast_uncertainty": {
                "class": uncertainty,
                "agreement": agreement,
                "model_count": consensus.get("model_count", 0),
                "kind": "forecast_uncertainty_not_probability",
            },
            "regime": regime,
            "research_reference": {
                "run_id": research.get("run_id"),
                "status": research.get("status"),
                "stance": stance,
                "as_of": research.get("as_of"),
                "advisory_only": True,
            }
            if research.get("run_id")
            else {},
            "research": research,
            "agent_reference": {
                "decision_id": agent.get("decision_id"),
                "cycle_id": agent.get("cycle_id"),
                "action": agent_action,
                "advisory_only": True,
            }
            if agent.get("decision_id")
            else {},
            "agent": agent,
            "events": events,
            "risk_reference": {
                "authority": RISK_AUTHORITY,
                "outcome": _text(risk_position.get("outcome"), 40) or "unavailable",
                "breaches": _items(risk_position.get("breaches"))[:10],
                "portfolio_restrictions": _items(risk.get("restrictions"))[:20],
            },
            "protective_controls": protective,
            "policy_advisory": policy,
            "supporting_evidence": _unique(supporting)[:10],
            "contradictory_evidence": _unique(contradictory)[:10],
            "explanation": self._position_explanation(
                instrument, assessment, direction, score, protective
            ),
            "advisory_only": True,
            "financial_mutation": False,
            "requires_pi12": True,
            "requires_nm08": True,
            "created_at": self._now().isoformat(),
            "configuration_version": config.version,
        }

    @staticmethod
    def _position_explanation(
        instrument: str,
        assessment: PositionAssessment,
        direction: str,
        score: float,
        protective: Mapping[str, Any],
    ) -> str:
        text = (
            f"{instrument} is assessed {assessment.value} from stored forecast, research, "
            f"regime and agent evidence (evidence score {score:.1f}/100, not a probability). "
            f"Forecast direction is {direction}."
        )
        if protective:
            text += " AT-05 protective controls remain authoritative; PI-21 did not override them."
        return text

    def _discover_opportunities(
        self,
        *,
        portfolio_id: str,
        snapshot: Mapping[str, Any],
        horizon: str,
        config: ManagementConfig,
        evidence: EvidenceBook,
        targets: Mapping[str, float],
    ) -> list[dict[str, Any]]:
        total = float(snapshot["valuation"].get("total_value") or 0)
        positions = {row["instrument_id"]: row for row in snapshot["positions"]}
        available = _num(_dict(snapshot.get("cash")).get("available_nc"))
        deployable = (
            max(0.0, available - total * float(config.cash_reserve_target))
            if available is not None
            else 0.0
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in evidence.forecasts(None, horizon):
            instrument = _canonical(row.get("instrument_id"))
            if instrument:
                grouped.setdefault(instrument, []).append(row)
        sector_values: dict[str, float] = {}
        for row in positions.values():
            sector = _text(row.get("sector"), 80) or "unknown"
            sector_values[sector] = sector_values.get(sector, 0.0) + float(
                row.get("current_value") or 0.0
            )
        candidates: list[dict[str, Any]] = []
        open_slots = max(0, int(config.max_positions) - len(positions))
        for instrument, forecast_rows in grouped.items():
            consensus = self._forecast_consensus(forecast_rows, now=self._now(), config=config)
            if not consensus.get("actionable") or consensus.get("direction") != "up":
                continue
            payload = evidence.instrument(instrument)
            research = self._research_view(payload)
            regime = self._regime_view(payload)
            agent = self._agent_view(evidence.agent_decision(instrument))
            policy = evidence.policy_advisory(instrument, horizon, str(regime.get("primary") or ""))
            current = positions.get(instrument)
            response_instrument = _dict(payload.get("instrument"))
            instrument_meta = {
                "price": _num(response_instrument.get("price")),
                "sector": _text(response_instrument.get("sector"), 80),
                "exchange": _text(response_instrument.get("exchange"), 20),
            }
            sector = (
                _text((current or {}).get("sector") or instrument_meta["sector"], 80) or "unknown"
            )
            sector_weight = sector_values.get(sector, 0.0) / total if total > 0 else 0.0
            current_weight = float((current or {}).get("weight") or 0.0)
            expected = _num(consensus.get("expected_return")) or 0.0
            agreement = _num(consensus.get("agreement")) or 0.0
            uncertainty = str(consensus.get("uncertainty") or "unknown")
            score = 50.0 + max(-15.0, min(20.0, expected * 250.0)) + agreement * 10.0
            supporting = [
                f"Stored {horizon} forecast consensus is upward "
                f"with expected return {expected:.2%}."
            ]
            contradictory: list[str] = []
            if uncertainty == "high":
                score -= 12.0
                contradictory.append("Forecast uncertainty is high.")
            elif uncertainty == "medium":
                score -= 4.0
            if int(consensus.get("model_count") or 0) >= 2:
                score += 4.0
            stance = research.get("stance")
            if stance in POSITIVE_STANCES:
                score += 7.0
                supporting.append(f"PI-19 research stance is {stance}.")
            elif stance in NEGATIVE_STANCES:
                score -= 15.0
                contradictory.append(f"PI-19 research stance is {stance}.")
            if research.get("negative_factors"):
                contradictory.extend(research["negative_factors"][:2])
            agent_action = str(agent.get("action") or "").lower()
            if agent_action == "buy":
                score += 7.0
                supporting.append("PI-17 multi-agent action is buy (advisory).")
            elif agent_action == "sell":
                score -= 18.0
                contradictory.append("PI-17 multi-agent action is sell.")
            if current is None and sector_weight < float(config.max_sector_weight) * 0.5:
                score += 5.0
                supporting.append("The opportunity adds a relatively underweight sector.")
            if sector_weight >= float(config.max_sector_weight):
                score -= 20.0
                contradictory.append(
                    f"Sector {sector} is already {sector_weight:.2%}, "
                    "at or above the management threshold."
                )
            elif sector_weight + float(config.max_position_weight) > float(
                config.max_sector_weight
            ):
                score -= 6.0
                contradictory.append(
                    "A full-size position would leave limited room under "
                    "the management sector threshold."
                )
            if current is not None:
                if current_weight >= float(config.max_position_weight):
                    score -= 15.0
                    contradictory.append(
                        "The existing position is already at the management position threshold."
                    )
                supporting.append(
                    "The instrument is already held; this is an add-to-existing assessment."
                )
            chart = _dict(payload.get("chart"))
            chart_rows = _items(chart.get("rows"))
            liquidity = _num(chart_rows[-1].get("volume")) if chart_rows else None
            if liquidity is None:
                contradictory.append(
                    "No current stored volume observation is available for liquidity."
                )
            else:
                supporting.append(f"Latest stored bar volume is {liquidity:,.0f}.")
            score = _clamp(score)
            target_weight = targets.get(instrument)
            if target_weight is None:
                target_weight = min(
                    float(config.max_position_weight), 1.0 / max(1, len(positions) + 1)
                )
            current_value = float((current or {}).get("current_value") or 0.0)
            proposed_value = min(
                max(0.0, total * float(config.max_position_weight) - current_value),
                max(0.0, min(deployable, total * target_weight)),
            )
            eligible = bool(
                score >= config.min_evidence_score
                and expected > 0
                and sector_weight < float(config.max_sector_weight)
                and not (current_weight >= float(config.max_position_weight))
            )
            reason = ""
            if score < config.min_evidence_score:
                reason = "evidence_score_below_threshold"
            elif expected <= 0:
                reason = "expected_return_not_positive"
            elif sector_weight >= float(config.max_sector_weight):
                reason = "sector_concentration_limit"
            elif current_weight >= float(config.max_position_weight):
                reason = "position_weight_limit"
            candidates.append(
                {
                    "opportunity_id": stable_id(
                        "pmop_", portfolio_id, instrument, horizon, snapshot["snapshot_version"]
                    ),
                    "portfolio_id": portfolio_id,
                    "instrument_id": instrument,
                    "exchange": instrument_meta["exchange"] or _exchange(instrument),
                    "sector": sector,
                    "horizon": horizon,
                    "expected_return": _round(expected, 8),
                    "forecast_direction": consensus.get("direction"),
                    "forecast_uncertainty": {
                        "class": uncertainty,
                        "agreement": agreement,
                        "model_count": consensus.get("model_count", 0),
                        "kind": "forecast_uncertainty_not_probability",
                    },
                    "forecast_versions": consensus.get("forecast_versions", []),
                    "market_regime": regime,
                    "research_reference": {
                        "run_id": research.get("run_id"),
                        "status": research.get("status"),
                        "stance": stance,
                        "advisory_only": True,
                    }
                    if research.get("run_id")
                    else {},
                    "research": research,
                    "agent_reference": {
                        "decision_id": agent.get("decision_id"),
                        "cycle_id": agent.get("cycle_id"),
                        "action": agent_action,
                        "advisory_only": True,
                    }
                    if agent.get("decision_id")
                    else {},
                    "agent": agent,
                    "policy_advisory": policy,
                    "evidence_score": _round(score, 2),
                    "score_kind": "evidence_score_not_probability",
                    "eligible": eligible,
                    "reason": reason,
                    "existing_position": current is not None,
                    "entry_recommendation": (
                        EntryRecommendation.ADD_TO_EXISTING_POSITION.value
                        if current is not None and eligible
                        else EntryRecommendation.NEW_POSITION.value
                        if eligible
                        else EntryRecommendation.NO_ENTRY.value
                    ),
                    "current_weight": current_weight,
                    "target_weight": target_weight,
                    "portfolio_compatibility": {
                        "sector": sector,
                        "sector_weight": _round(sector_weight, 6),
                        "sector_limit": float(config.max_sector_weight),
                        "correlation_proxy": (
                            "sector overlap; no instrument correlation is fabricated"
                        ),
                        "existing_position": current is not None,
                        "diversifies": sector_weight < float(config.max_sector_weight) * 0.5,
                        "concentration_warning": sector_weight
                        >= float(config.max_sector_weight) * 0.75,
                    },
                    "liquidity": {
                        "latest_volume": liquidity,
                        "status": "recorded" if liquidity is not None else "unavailable",
                        "not_a_bid_ask": True,
                    },
                    "market": instrument_meta,
                    "proposed_allocation_nc": _round(proposed_value, 2),
                    "proposed_allocation_inr": _round(proposed_value, 2),
                    "funded": bool(proposed_value > 0),
                    "supporting_evidence": _unique(supporting)[:10],
                    "contradictory_evidence": _unique(contradictory)[:10],
                    "explanation": (
                        f"{instrument} is {'eligible' if eligible else 'not eligible'} from stored "
                        "forecast, research, regime, agent and portfolio-fit evidence. "
                        "No order is implied."
                    ),
                    "advisory_only": True,
                    "requires_pi12": True,
                    "requires_nm08": True,
                    "created_at": self._now().isoformat(),
                }
            )
        candidates.sort(key=lambda row: (-float(row["evidence_score"]), str(row["instrument_id"])))
        opened = 0
        for row in candidates:
            if not row["eligible"] or row["existing_position"]:
                continue
            if opened >= open_slots:
                row["eligible"] = False
                row["reason"] = "max_positions_limit"
                row["entry_recommendation"] = EntryRecommendation.NO_ENTRY.value
            else:
                opened += 1
        return candidates[: int(config.max_opportunities)]

    def _exposure(
        self,
        snapshot: Mapping[str, Any],
        config: ManagementConfig,
        risk: Mapping[str, Any],
        optimization: Mapping[str, Any],
    ) -> dict[str, Any]:
        total = float(snapshot["valuation"].get("total_value") or 0)
        positions = _items(snapshot.get("positions"))
        by_instrument = [
            {
                "instrument_id": row.get("instrument_id"),
                "sector": _text(row.get("sector"), 80) or "unknown",
                "value_nc": row.get("current_value"),
                "weight": row.get("weight"),
                "stale": row.get("stale"),
            }
            for row in positions
        ]
        sectors: dict[str, float] = {}
        industries: dict[str, float] = {}
        for row in positions:
            value = float(row.get("current_value") or 0)
            sector = _text(row.get("sector"), 80) or "unknown"
            sectors[sector] = sectors.get(sector, 0.0) + value
            industry = _text(row.get("industry"), 80)
            if industry:
                industries[industry] = industries.get(industry, 0.0) + value
        sector_rows = [
            {
                "sector": sector,
                "value_nc": _round(value, 2),
                "weight": _round(value / total, 6) if total > 0 else None,
            }
            for sector, value in sorted(sectors.items(), key=lambda item: -item[1])
        ]
        industry_rows = [
            {
                "industry": industry,
                "value_nc": _round(value, 2),
                "weight": _round(value / total, 6) if total > 0 else None,
            }
            for industry, value in sorted(industries.items(), key=lambda item: -item[1])
        ]
        weights = [float(row.get("weight") or 0.0) for row in positions]
        hhi = sum(weight * weight for weight in weights)
        top1 = max(weights, default=0.0)
        top3 = sum(sorted(weights, reverse=True)[:3])
        warnings: list[dict[str, Any]] = []
        for row in by_instrument:
            weight = _num(row.get("weight"))
            if weight is not None and weight > float(config.max_position_weight):
                warnings.append(
                    {
                        "code": "excessive_instrument_concentration",
                        "instrument_id": row.get("instrument_id"),
                        "observed": weight,
                        "threshold": float(config.max_position_weight),
                        "authority": "management research threshold; NM-08 remains risk authority",
                    }
                )
        for row in sector_rows:
            weight = _num(row.get("weight"))
            if weight is not None and weight > float(config.max_sector_weight):
                warnings.append(
                    {
                        "code": "sector_imbalance",
                        "sector": row.get("sector"),
                        "observed": weight,
                        "threshold": float(config.max_sector_weight),
                        "authority": "management research threshold; NM-08 remains risk authority",
                    }
                )
        if (
            len(positions) >= 2
            and sectors
            and max(sectors.values(), default=0) / max(total, 1) > 0.35
        ):
            warnings.append(
                {
                    "code": "correlated_holding_proxy",
                    "detail": (
                        "Two or more holdings share a sector carrying more than 35% of the book; "
                        "this is a sector proxy, not a measured correlation."
                    ),
                }
            )
        if len(positions) < 3 or hhi > 0.4:
            warnings.append(
                {
                    "code": "insufficient_diversification",
                    "detail": "Fewer than three positions or instrument HHI above 0.40.",
                    "hhi": _round(hhi, 6),
                }
            )
        if any(row.get("stale") for row in positions):
            warnings.append(
                {
                    "code": "stale_or_missing_valuation",
                    "detail": "At least one position has a stale or missing authoritative price.",
                }
            )
        cash_weight = _num(_dict(snapshot.get("valuation")).get("cash_weight"))
        return {
            "exposure_id": stable_id(
                "pmex_", snapshot.get("portfolio_id"), snapshot.get("snapshot_version")
            ),
            "portfolio_id": snapshot.get("portfolio_id"),
            "total_value_nc": total,
            "instrument_exposure": by_instrument,
            "sector_exposure": sector_rows,
            "industry_exposure": industry_rows,
            "cash_exposure": {
                "weight": cash_weight,
                "value_nc": _num(_dict(snapshot.get("cash")).get("total_nc")),
            },
            "market_regime_exposure": {
                "regime": "portfolio-level; per-position regimes are recorded in assessments",
                "equity_exposure": 1.0 - (cash_weight or 0.0),
            },
            "concentration": {
                "hhi": _round(hhi, 6),
                "top_position_weight": _round(top1, 6),
                "top_three_weight": _round(top3, 6),
                "positions": len(positions),
            },
            "diversification": {
                "management_metric": "1 - instrument HHI",
                "score": _round(1.0 - hhi, 6),
                "optimization_metrics": _dict(optimization.get("diversification")),
                "optimization_advisory_only": True,
            },
            "correlation_exposure": {
                "method": "sector-overlap proxy",
                "measured_correlations_available": False,
                "limit": float(config.correlation_proxy_limit),
                "note": (
                    "No correlation coefficient is fabricated; PI-18 covariance remains advisory."
                ),
            },
            "warnings": warnings,
            "risk_restrictions": [_text(item, 260) for item in risk.get("restrictions") or ()],
            "risk_outcome": _text(risk.get("outcome"), 40) or "unavailable",
            "risk_authority": RISK_AUTHORITY,
            "research_thresholds_override_risk": False,
            "advisory_only": True,
            "created_at": self._now().isoformat(),
        }

    def _capital(
        self,
        snapshot: Mapping[str, Any],
        opportunities: Sequence[Mapping[str, Any]],
        config: ManagementConfig,
        positions: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        total = float(snapshot["valuation"].get("total_value") or 0)
        cash_body = _dict(snapshot.get("cash"))
        available = _num(cash_body.get("available_nc"))
        reserved = _num(cash_body.get("reserved_nc"))
        reserve_target = total * float(config.cash_reserve_target)
        deployable = max(0.0, available - reserve_target) if available is not None else None
        eligible = [row for row in opportunities if row.get("eligible")]
        proposed = 0.0
        selected: list[dict[str, Any]] = []
        if deployable is not None:
            for row in eligible:
                if len(selected) >= int(config.max_changes_per_cycle):
                    break
                allocation = min(
                    _num(row.get("proposed_allocation_nc")) or 0.0, deployable - proposed
                )
                if allocation < float(config.min_trade_notional):
                    continue
                proposed += allocation
                selected.append(
                    {
                        "instrument_id": row.get("instrument_id"),
                        "allocation_nc": _round(allocation, 2),
                        "entry_recommendation": row.get("entry_recommendation"),
                        "evidence_score": row.get("evidence_score"),
                    }
                )
        best = eligible[0] if eligible else {}
        best_score = _num(best.get("evidence_score")) or 0.0
        if available is None or reserved is None:
            recommendation = DeploymentRecommendation.WAIT_FOR_CONFIRMATION
            reason = (
                "available cash or reserved capital is unavailable; "
                "missing data is not treated as zero"
            )
        elif deployable is None or deployable <= 0 or proposed <= 0:
            recommendation = (
                DeploymentRecommendation.NO_DEPLOYMENT
                if available <= 0
                else DeploymentRecommendation.RETAIN_CASH
            )
            reason = (
                "no deployable capital remains after the reserve and trade minimums"
                if available > 0
                else "no available virtual cash"
            )
        elif best_score < config.min_evidence_score:
            recommendation = DeploymentRecommendation.RETAIN_CASH
            reason = "no opportunity clears the evidence threshold"
        elif (
            str(_dict(best.get("forecast_uncertainty")).get("class") or "") == "high"
            or (_num(_dict(best.get("forecast_uncertainty")).get("agreement")) or 0) < 0.5
        ):
            recommendation = DeploymentRecommendation.WAIT_FOR_CONFIRMATION
            reason = "forecast uncertainty or model disagreement requires confirmation"
        elif proposed < deployable * 0.5:
            recommendation = DeploymentRecommendation.PARTIAL_DEPLOYMENT
            reason = "only part of the deployable capital has adequate evidence"
        else:
            recommendation = DeploymentRecommendation.DEPLOY_CAPITAL
            reason = "eligible, funded opportunities support deployment within the reserve"
        unallocated = max(0.0, (available or 0.0) - proposed)
        return {
            "capital_id": stable_id(
                "pmcap_", snapshot.get("portfolio_id"), snapshot.get("snapshot_version")
            ),
            "portfolio_id": snapshot.get("portfolio_id"),
            "available_nc": available,
            "available_inr": available,
            "reserved_nc": reserved,
            "reserved_inr": reserved,
            "reserve_target_nc": _round(reserve_target, 2),
            "reserve_target_fraction": float(config.cash_reserve_target),
            "deployable_nc": _round(deployable, 2) if deployable is not None else None,
            "deployable_inr": _round(deployable, 2) if deployable is not None else None,
            "proposed_deployment_nc": _round(proposed, 2),
            "proposed_deployment_inr": _round(proposed, 2),
            "unallocated_nc": _round(unallocated, 2),
            "unallocated_inr": _round(unallocated, 2),
            "pending_commitments_nc": max(
                _num(snapshot.get("pending_buy_notional")) or 0.0,
                _num(snapshot.get("reservations_cash_total")) or 0.0,
            ),
            "pending_orders": len(_items(snapshot.get("pending_orders"))),
            "selected_opportunities": selected,
            "recommendation": recommendation.value,
            "reason": reason,
            "unused_cash_is_failure": False,
            "cash_mutation": False,
            "accounting_authority": "NM-07 / existing portfolio, valuation and ledger services",
            "advisory_only": True,
            "created_at": self._now().isoformat(),
        }

    def _cash(
        self,
        snapshot: Mapping[str, Any],
        capital: Mapping[str, Any],
        config: ManagementConfig,
    ) -> dict[str, Any]:
        cash = _dict(snapshot.get("cash"))
        available = _num(cash.get("available_nc"))
        reserved = _num(cash.get("reserved_nc"))
        total = float(snapshot["valuation"].get("total_value") or 0)
        ratio = (available / total) if available is not None and total > 0 else None
        if available is None:
            recommendation = CashRecommendation.WAIT
            reason = "available cash is unavailable"
        elif available < float(config.min_deployable_cash):
            recommendation = CashRecommendation.PRESERVE_RESERVES
            reason = "available cash is below the management deployment minimum"
        elif capital.get("recommendation") == DeploymentRecommendation.WAIT_FOR_CONFIRMATION.value:
            recommendation = CashRecommendation.WAIT
            reason = "capital deployment is waiting for forecast confirmation"
        elif capital.get("recommendation") == DeploymentRecommendation.RETAIN_CASH.value:
            recommendation = CashRecommendation.RETAIN_CASH
            reason = "no opportunity clears the evidence threshold; cash is a valid position"
        elif capital.get("recommendation") == DeploymentRecommendation.NO_DEPLOYMENT.value:
            recommendation = CashRecommendation.PRESERVE_RESERVES
            reason = "no deployable virtual cash remains"
        else:
            recommendation = CashRecommendation.DEPLOY_CASH
            reason = "the reserve target is met and eligible deployment remains"
        return {
            "cash_id": stable_id(
                "pmcash_", snapshot.get("portfolio_id"), snapshot.get("snapshot_version")
            ),
            "portfolio_id": snapshot.get("portfolio_id"),
            "available_nc": available,
            "reserved_nc": reserved,
            "cash_ratio": _round(ratio, 6),
            "cash_ratio_pct": _pct(ratio),
            "target_reserve_fraction": float(config.cash_reserve_target),
            "target_reserve_nc": _round(total * float(config.cash_reserve_target), 2),
            "unallocated_nc": capital.get("unallocated_nc"),
            "expected_capital_requirements_nc": capital.get("pending_commitments_nc"),
            "recommendation": recommendation.value,
            "reason": reason,
            "turnover_for_target": False,
            "cash_created": False,
            "balance_modified": False,
            "advisory_only": True,
            "created_at": self._now().isoformat(),
        }

    def _rebalance(
        self,
        *,
        portfolio_id: str,
        snapshot: Mapping[str, Any],
        optimization: Mapping[str, Any],
        positions: Sequence[Mapping[str, Any]],
        targets: Mapping[str, float],
        config: ManagementConfig,
        risk: Mapping[str, Any],
    ) -> dict[str, Any]:
        total = float(snapshot["valuation"].get("total_value") or 0)
        current = {
            str(row.get("instrument_id")): float(row.get("position_weight") or 0.0)
            for row in positions
        }
        target_map = dict(targets)
        if not target_map:
            target_map = dict(current)
            for row in positions:
                instrument = str(row.get("instrument_id"))
                if row.get("action") == ManagementAction.EXIT.value:
                    target_map[instrument] = 0.0
                elif row.get("action") == ManagementAction.REDUCE.value:
                    target_map[instrument] = min(
                        float(row.get("position_weight") or 0.0),
                        float(config.max_position_weight),
                    )
        record = _dict(snapshot.get("portfolio"))
        last_rebalanced = _mtime(record.get("last_rebalanced_at"))
        now = self._now()
        urgent = any(
            row.get("action") in {ManagementAction.EXIT.value, ManagementAction.REDUCE.value}
            for row in positions
        )
        cooldown_active = bool(
            last_rebalanced
            and now - last_rebalanced < timedelta(hours=int(config.cooldown_hours))
            and not urgent
        )
        intents: list[dict[str, Any]] = []
        optimization_rebalance = _dict(optimization.get("rebalancing"))
        optimization_intents = _items(optimization_rebalance.get("intents"))
        if optimization_intents:
            for row in optimization_intents:
                instrument = _canonical(row.get("instrument_id"))
                if not instrument:
                    continue
                before = current.get(instrument, 0.0)
                after = _num(row.get("target_weight"))
                if after is None:
                    after = before + (_num(row.get("weight_change")) or 0.0)
                intents.append(
                    self._rebalance_intent(instrument, before, after, total, row, config)
                )
        else:
            for instrument in sorted(set(current) | set(target_map)):
                before = current.get(instrument, 0.0)
                after = target_map.get(instrument, before)
                intents.append(self._rebalance_intent(instrument, before, after, total, {}, config))
        filtered: list[dict[str, Any]] = []
        for row in intents:
            if row["action"] == RebalanceAction.NO_REBALANCING_REQUIRED.value:
                continue
            if row["absolute_weight_change"] < float(config.rebalance_threshold):
                continue
            if float(row["notional_nc"] or 0) < float(config.min_trade_notional):
                continue
            improvement = _num(row.get("expected_improvement"))
            if improvement is not None and improvement < float(config.min_expected_improvement):
                continue
            filtered.append(row)
        filtered.sort(key=lambda row: -float(row["absolute_weight_change"]))
        filtered = filtered[: int(config.max_changes_per_cycle)]
        turnover = (
            sum(float(row["notional_nc"] or 0) for row in filtered) / total if total > 0 else 0.0
        )
        policy_turnover = _num(record.get("max_turnover"))
        turnover_limit = (
            min(float(config.max_turnover), policy_turnover)
            if policy_turnover is not None
            else float(config.max_turnover)
        )
        if turnover > turnover_limit:
            filtered = []
            reason = (
                f"proposed turnover {turnover:.2%} exceeds the "
                f"{turnover_limit:.2%} management policy limit"
            )
        elif cooldown_active:
            filtered = []
            reason = "rebalancing cooldown is active and no protective action is urgent"
        elif not filtered:
            reason = "no change clears the threshold, minimum trade and expected-improvement rules"
        else:
            reason = "threshold and turnover rules produced a bounded rebalancing proposal"
        action = (
            RebalanceAction.NO_REBALANCING_REQUIRED.value
            if not filtered
            else RebalanceAction.HOLD_CASH.value
            if all(row["action"] == RebalanceAction.HOLD_CASH.value for row in filtered)
            else filtered[0]["action"]
        )
        cost = sum(float(row.get("estimated_cost_nc") or 0) for row in filtered)
        return {
            "rebalance_id": stable_id("pmrb_", portfolio_id, snapshot.get("snapshot_version")),
            "portfolio_id": portfolio_id,
            "current_allocation": [
                {"instrument_id": key, "weight": _round(value, 6)}
                for key, value in sorted(current.items())
            ],
            "target_allocation": [
                {"instrument_id": key, "weight": _round(value, 6)}
                for key, value in sorted(target_map.items())
            ],
            "source": "PI-18 optimization" if targets else "management exposure maintenance",
            "action": action,
            "changes": filtered,
            "change_count": len(filtered),
            "estimated_cost_nc": _round(cost, 2),
            "estimated_cost_inr": _round(cost, 2),
            "turnover": _round(turnover, 6),
            "turnover_limit": _round(turnover_limit, 6),
            "expected_portfolio_impact": _dict(
                optimization_rebalance.get("expected_impact") or optimization.get("expected_impact")
            ),
            "risk_restrictions": [_text(item, 260) for item in risk.get("restrictions") or ()],
            "reason": reason,
            "requires_pi12": True,
            "requires_nm08": True,
            "executes_orders": False,
            "financial_mutation": False,
            "advisory_only": True,
            "created_at": self._now().isoformat(),
        }

    @staticmethod
    def _rebalance_intent(
        instrument: str,
        before: float,
        after: float,
        total: float,
        source: Mapping[str, Any],
        config: ManagementConfig,
    ) -> dict[str, Any]:
        change = after - before
        if before <= 1e-9 and after > 0:
            action = RebalanceAction.OPEN_POSITION.value
        elif before > 0 and after <= 1e-9:
            action = RebalanceAction.CLOSE_POSITION.value
        elif change > 0:
            action = RebalanceAction.INCREASE_POSITION.value
        elif change < 0:
            action = RebalanceAction.REDUCE_POSITION.value
        else:
            action = RebalanceAction.MAINTAIN_POSITION.value
        notional = abs(change) * total
        cost = notional * float(config.transaction_cost_bps) / 10000.0
        return {
            "instrument_id": instrument,
            "current_weight": _round(before, 6),
            "target_weight": _round(after, 6),
            "weight_change": _round(change, 6),
            "absolute_weight_change": _round(abs(change), 6),
            "action": action,
            "notional_nc": _round(notional, 2),
            "notional_inr": _round(notional, 2),
            "estimated_cost_nc": _round(cost, 2),
            "expected_improvement": _num(source.get("expected_improvement")),
            "reason": _text(source.get("reason"), 300)
            or "allocation gap versus the recorded target",
            "requires_pi12": True,
            "requires_nm08": True,
        }

    def _decisions(
        self,
        *,
        cycle_id: str,
        snapshot: Mapping[str, Any],
        positions: Sequence[Mapping[str, Any]],
        opportunities: Sequence[Mapping[str, Any]],
        capital: Mapping[str, Any],
        config: ManagementConfig,
        risk_reference: Mapping[str, Any],
        targets: Mapping[str, float],
    ) -> list[dict[str, Any]]:
        decisions: list[dict[str, Any]] = []
        total = float(snapshot["valuation"].get("total_value") or 0)
        budget = _num(capital.get("deployable_nc")) or 0.0
        funded = {
            str(row.get("instrument_id")): float(row.get("allocation_nc") or 0.0)
            for row in _items(capital.get("selected_opportunities"))
        }
        cash_cursor = _num(_dict(snapshot.get("cash")).get("available_nc"))
        for row in positions:
            instrument = str(row.get("instrument_id"))
            action = str(row.get("action"))
            target = targets.get(instrument)
            before = float(row.get("position_weight") or 0.0)
            if (
                action == ManagementAction.HOLD.value
                and target is not None
                and target > before + float(config.rebalance_threshold)
                and not row.get("protective_controls")
                and budget >= float(config.min_trade_notional)
                and float(row.get("evidence_score") or 0) >= config.min_evidence_score
                and bool(_dict(row.get("forecast")).get("actionable"))
            ):
                action = ManagementAction.ADD.value
            side = (
                "sell"
                if action in {ManagementAction.REDUCE.value, ManagementAction.EXIT.value}
                else "buy"
                if action == ManagementAction.ADD.value
                else ""
            )
            quantity = None
            notional = 0.0
            if action == ManagementAction.EXIT.value:
                quantity = _num(row.get("quantity"))
                notional = float(row.get("current_value") or 0.0)
            elif action == ManagementAction.REDUCE.value and target is not None and before > 0:
                reduce_weight = max(0.0, before - min(target, before))
                if reduce_weight > 0:
                    quantity = float(row.get("quantity") or 0.0) * reduce_weight / before
                    notional = total * reduce_weight
            elif action == ManagementAction.ADD.value and target is not None and before < target:
                add_weight = min(target - before, float(config.max_position_weight) - before)
                add_notional = min(total * add_weight, budget)
                if add_notional < float(config.min_trade_notional):
                    action = ManagementAction.HOLD.value
                    side = ""
                    add_notional = 0.0
                else:
                    budget -= add_notional
                price = _num(row.get("current_price"))
                if action == ManagementAction.ADD.value and price and price > 0:
                    quantity = add_notional / price
                notional = add_notional
            decisions.append(
                self._decision_record(
                    cycle_id=cycle_id,
                    snapshot=snapshot,
                    instrument=instrument,
                    exchange=str(row.get("exchange") or _exchange(instrument)),
                    action=action,
                    side=side,
                    quantity=quantity,
                    notional=notional,
                    before_weight=before,
                    target_weight=target,
                    supporting=[str(item) for item in row.get("supporting_evidence") or ()],
                    contradictory=[str(item) for item in row.get("contradictory_evidence") or ()],
                    uncertainty=_dict(row.get("forecast_uncertainty")),
                    risk_reference=risk_reference,
                    config=config,
                    available_cash_before=cash_cursor,
                    current_context={
                        "quantity": row.get("quantity"),
                        "average_cost": row.get("average_cost"),
                        "current_price": row.get("current_price"),
                        "current_value": row.get("current_value"),
                        "unrealized_pnl": row.get("unrealized_pnl"),
                        "holding_hours": row.get("holding_hours"),
                        "assessment": row.get("assessment"),
                    },
                    agent_reference=_dict(row.get("agent_reference")),
                    research_reference=_dict(row.get("research_reference")),
                    policy_advisory=_dict(row.get("policy_advisory")),
                    explanation=_text(row.get("explanation"), 800),
                    protective=bool(row.get("protective_controls")),
                    entry_recommendation=None,
                )
            )
            if cash_cursor is not None:
                if side == "buy":
                    cash_cursor = max(0.0, cash_cursor - notional)
                elif side == "sell":
                    cash_cursor += notional
        held = {str(row.get("instrument_id")) for row in positions}
        for row in opportunities:
            if not row.get("eligible") or str(row.get("instrument_id")) in held:
                continue
            instrument = str(row.get("instrument_id"))
            allocation = min(funded.get(instrument, 0.0), budget)
            action = (
                ManagementAction.OPEN.value
                if allocation >= float(config.min_trade_notional)
                else ManagementAction.WAIT.value
            )
            if action == ManagementAction.OPEN.value:
                budget -= allocation
            price = _num(_dict(row.get("market")).get("price"))
            quantity = (
                allocation / price
                if action == ManagementAction.OPEN.value and price and price > 0
                else None
            )
            decisions.append(
                self._decision_record(
                    cycle_id=cycle_id,
                    snapshot=snapshot,
                    instrument=str(row.get("instrument_id")),
                    exchange=str(row.get("exchange") or _exchange(row.get("instrument_id"))),
                    action=action,
                    side="buy" if action == ManagementAction.OPEN.value else "",
                    quantity=quantity,
                    notional=allocation if action == ManagementAction.OPEN.value else 0.0,
                    before_weight=0.0,
                    target_weight=_num(row.get("target_weight")),
                    supporting=[str(item) for item in row.get("supporting_evidence") or ()],
                    contradictory=[str(item) for item in row.get("contradictory_evidence") or ()],
                    uncertainty=_dict(row.get("forecast_uncertainty")),
                    risk_reference=risk_reference,
                    config=config,
                    available_cash_before=cash_cursor,
                    current_context={"existing_position": False},
                    agent_reference=_dict(row.get("agent_reference")),
                    research_reference=_dict(row.get("research_reference")),
                    policy_advisory=_dict(row.get("policy_advisory")),
                    explanation=_text(row.get("explanation"), 800),
                    protective=False,
                    entry_recommendation=row.get("entry_recommendation"),
                )
            )
            if cash_cursor is not None and action == ManagementAction.OPEN.value:
                cash_cursor = max(0.0, cash_cursor - allocation)
        if not any(
            row.get("action") in {ManagementAction.OPEN.value, ManagementAction.ADD.value}
            for row in decisions
        ):
            decisions.append(
                self._decision_record(
                    cycle_id=cycle_id,
                    snapshot=snapshot,
                    instrument="",
                    exchange="",
                    action=ManagementAction.RETAIN_CASH.value,
                    side="",
                    quantity=None,
                    notional=0.0,
                    before_weight=None,
                    target_weight=None,
                    supporting=["No eligible entry clears the evidence and capital thresholds."],
                    contradictory=[],
                    uncertainty={},
                    risk_reference=risk_reference,
                    config=config,
                    available_cash_before=cash_cursor,
                    current_context={
                        "available_cash": _dict(snapshot.get("cash")).get("available_nc")
                    },
                    agent_reference={},
                    research_reference={},
                    policy_advisory={},
                    explanation=(
                        "Retaining virtual cash is a valid portfolio decision when entry evidence "
                        "or capital is insufficient."
                    ),
                    protective=False,
                    entry_recommendation=EntryRecommendation.NO_ENTRY.value,
                )
            )
        return decisions[:MAX_DECISIONS]

    def _decision_record(
        self,
        *,
        cycle_id: str,
        snapshot: Mapping[str, Any],
        instrument: str,
        exchange: str,
        action: str,
        side: str,
        quantity: float | None,
        notional: float,
        before_weight: float | None,
        target_weight: float | None,
        supporting: Sequence[str],
        contradictory: Sequence[str],
        uncertainty: Mapping[str, Any],
        risk_reference: Mapping[str, Any],
        config: ManagementConfig,
        available_cash_before: float | None,
        current_context: Mapping[str, Any],
        agent_reference: Mapping[str, Any],
        research_reference: Mapping[str, Any],
        policy_advisory: Mapping[str, Any],
        explanation: str,
        protective: bool,
        entry_recommendation: str | None,
    ) -> dict[str, Any]:
        decision_id = stable_id(
            "pmd_",
            cycle_id,
            instrument or "portfolio",
            action,
            snapshot.get("snapshot_version"),
        )
        cash_before = (
            available_cash_before
            if available_cash_before is not None
            else _num(_dict(snapshot.get("cash")).get("available_nc"))
        )
        cash_after = None
        if cash_before is not None:
            cash_after = (
                cash_before - notional
                if side == "buy"
                else cash_before + notional
                if side == "sell"
                else cash_before
            )
        return {
            "decision_id": decision_id,
            "cycle_id": cycle_id,
            "portfolio_id": snapshot.get("portfolio_id"),
            "instrument_id": instrument,
            "exchange": exchange,
            "action": action,
            "side": side,
            "entry_recommendation": entry_recommendation or "",
            "quantity": _round(quantity, 6) if quantity is not None else None,
            "notional_nc": _round(notional, 2),
            "notional_inr": _round(notional, 2),
            "current_position_context": dict(current_context),
            "target_weight": target_weight,
            "portfolio_impact": {
                "weight_before": before_weight,
                "weight_after": target_weight,
                "capital_delta_nc": _round(
                    notional if side == "buy" else -notional if side == "sell" else 0.0, 2
                ),
                "capital_delta_inr": _round(
                    notional if side == "buy" else -notional if side == "sell" else 0.0, 2
                ),
            },
            "capital_impact": {
                "available_cash_before_nc": cash_before,
                "available_cash_after_nc": _round(cash_after, 2)
                if cash_after is not None
                else None,
                "reserved_capital_nc": _dict(snapshot.get("cash")).get("reserved_nc"),
                "capital_mutated": False,
            },
            "supporting_evidence": [str(item) for item in supporting][:12],
            "contradictory_evidence": [str(item) for item in contradictory][:12],
            "forecast_uncertainty": dict(uncertainty),
            "risk_engine_reference": dict(risk_reference),
            "risk_decision_id": "",
            "agent_reference": dict(agent_reference),
            "research_reference": dict(research_reference),
            "policy_advisory": dict(policy_advisory),
            "protective_controls_present": protective,
            "explanation": explanation,
            "reasoning_trace": False,
            "decision_timestamp": self._now().isoformat(),
            "configuration_version": config.version,
            "configuration_digest": config.digest,
            "advisory_only": True,
            "financial_mutation": False,
            "order_submission": False,
            "execution_outcome": "not_submitted",
            "handoff": {"eligible": False, "proposal_id": "", "reason": "not_checked"},
            "recommendation_quality": "not_evaluated_from_pnl",
        }

    def _attach_proposal_candidates(
        self, portfolio_id: str, decisions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        try:
            proposals = _items(self._sources.at03_proposals(portfolio_id))
        except Exception:
            proposals = []
        now = self._now()
        for decision in decisions:
            side = str(decision.get("side") or "")
            if not side or not decision.get("instrument_id"):
                continue
            matches: list[dict[str, Any]] = []
            for proposal in proposals:
                if _text(proposal.get("portfolio_id"), 120) != portfolio_id:
                    continue
                if _canonical(proposal.get("instrument_id")) != decision["instrument_id"]:
                    continue
                proposal_side = _text(proposal.get("side") or proposal.get("action"), 30).lower()
                aliases = {
                    side,
                    "add_position" if side == "buy" else "reduce_position",
                    "open" if side == "buy" else "exit",
                }
                if proposal_side not in aliases:
                    continue
                expiry = _mtime(proposal.get("expires_at"))
                if expiry is not None and expiry <= now:
                    continue
                matches.append(proposal)
            approved = [
                row
                for row in matches
                if _text(row.get("status"), 40) == "risk_approved"
                and _text(row.get("risk_outcome"), 30).lower() == "approved"
            ]
            rejected = [
                row
                for row in matches
                if _text(row.get("status"), 40)
                in {"risk_rejected", "risk_restricted", "risk_review"}
            ]
            chosen = approved[0] if approved else None
            decision["handoff"] = {
                "eligible": bool(chosen),
                "proposal_id": _text(chosen.get("proposal_id"), 120) if chosen else "",
                "risk_decision_id": _text(chosen.get("risk_decision_id"), 120) if chosen else "",
                "reason": (
                    "a persisted NM-08-approved AT-03 proposal matches this recommendation"
                    if chosen
                    else "NM-08 rejection recorded for this instrument/action"
                    if rejected
                    else (
                        "no matching AT-03 proposal is risk-approved; "
                        "PI-12 will not execute this recommendation"
                    )
                ),
            }
            if chosen:
                decision["risk_decision_id"] = _text(chosen.get("risk_decision_id"), 120)
                decision["handoff_reference"] = {
                    "proposal_id": _text(chosen.get("proposal_id"), 120),
                    "risk_decision_id": _text(chosen.get("risk_decision_id"), 120),
                    "status": _text(chosen.get("status"), 40),
                    "expires_at": chosen.get("expires_at"),
                }
            elif rejected:
                decision["risk_outcome_recorded"] = _text(rejected[0].get("status"), 40)
        return decisions

    def _analytics(
        self,
        snapshot: Mapping[str, Any],
        decisions: Sequence[Mapping[str, Any]],
        positions: Sequence[Mapping[str, Any]],
        rebalance: Mapping[str, Any],
        config: ManagementConfig,
    ) -> dict[str, Any]:
        valuation = _dict(snapshot.get("valuation"))
        total = _num(valuation.get("total_value")) or 0.0
        position_pnl = sum(float(row.get("unrealized_pnl") or 0.0) for row in positions)
        sectors: dict[str, float] = {}
        for row in positions:
            sector = _text(row.get("sector"), 80) or "unknown"
            sectors[sector] = sectors.get(sector, 0.0) + float(row.get("unrealized_pnl") or 0.0)
        cash_total = float(_dict(snapshot.get("cash")).get("total_nc") or 0.0)
        return {
            "portfolio_return_pct": valuation.get("return_pct"),
            "realized_pnl_nc": valuation.get("realized_pnl"),
            "unrealized_pnl_nc": valuation.get("unrealized_pnl"),
            "total_pnl_nc": valuation.get("total_pnl"),
            "positions_pnl_reconciliation_nc": _round(position_pnl, 2),
            "capital_utilization": _round((total - cash_total) / total, 6) if total > 0 else None,
            "cash_ratio": valuation.get("cash_weight"),
            "position_contribution": [
                {
                    "instrument_id": row.get("instrument_id"),
                    "pnl_nc": row.get("unrealized_pnl"),
                    "contribution": row.get("contribution"),
                }
                for row in positions
            ],
            "sector_contribution": [
                {"sector": sector, "pnl_nc": _round(value, 2)}
                for sector, value in sorted(sectors.items(), key=lambda item: -item[1])
            ],
            "turnover": rebalance.get("turnover"),
            "transaction_cost_policy_bps": float(config.transaction_cost_bps),
            "transaction_costs_recorded_nc": rebalance.get("estimated_cost_nc"),
            "holding_duration_hours": _round(
                sum(float(row.get("holding_hours") or 0.0) for row in positions) / len(positions),
                2,
            )
            if positions
            else None,
            "rebalancing_frequency": rebalance.get("change_count", 0),
            "recommendation_count": len(decisions),
            "recommendation_accuracy": {
                "value": None,
                "status": "not_evaluated",
                "reason": (
                    "No independently measured recommendation outcome sample exists in this cycle."
                ),
            },
            "decision_quality": {
                "value": None,
                "status": "not_inferred_from_pnl",
                "reason": "A profitable fill is not proof of a good decision.",
            },
            "execution_quality": {
                "value": None,
                "status": "NM-10 authoritative",
                "reason": "Slippage and fees are read from NM-10 fills, not derived here.",
            },
            "benchmark": "initial_capital",
            "comparison_period": {
                "start": _dict(snapshot.get("portfolio")).get("created_at"),
                "end": snapshot.get("at"),
            },
            "profitability_is_not_decision_quality": True,
        }

    def _data_status(
        self,
        snapshot: Mapping[str, Any],
        positions: Sequence[Mapping[str, Any]],
        evidence: EvidenceBook,
    ) -> str:
        if snapshot.get("missing_valuation"):
            return DataStatus.UNAVAILABLE.value
        if evidence.errors:
            return DataStatus.PARTIAL_DATA.value
        if any(row.get("missing_price") for row in positions):
            return DataStatus.STALE.value
        if any(row.get("stale") for row in positions):
            return DataStatus.STALE.value
        if positions and all(
            row.get("assessment") == PositionAssessment.INSUFFICIENT_EVIDENCE.value
            for row in positions
        ):
            return DataStatus.INSUFFICIENT_EVIDENCE.value
        return DataStatus.AVAILABLE.value

    @staticmethod
    def _cycle_status(
        *,
        mode: str,
        snapshot: Mapping[str, Any],
        positions: Sequence[Mapping[str, Any]],
        opportunities: Sequence[Mapping[str, Any]],
        evidence: EvidenceBook,
        data_status: str,
    ) -> str:
        if data_status == DataStatus.UNAVAILABLE.value:
            return CycleStatus.WAITING_FOR_EVIDENCE.value
        if data_status == DataStatus.STALE.value and any(
            row.get("missing_price") for row in positions
        ):
            return CycleStatus.WAITING_FOR_EVIDENCE.value
        if data_status == DataStatus.INSUFFICIENT_EVIDENCE.value:
            return CycleStatus.WAITING_FOR_EVIDENCE.value
        if evidence.errors:
            return CycleStatus.COMPLETED_WITH_WARNINGS.value
        if _items(snapshot.get("anomalies")):
            return CycleStatus.COMPLETED_WITH_WARNINGS.value
        if mode in {"disabled", "monitoring", "paused"}:
            return CycleStatus.MONITORING_ONLY.value
        return CycleStatus.COMPLETED.value

    @staticmethod
    def _cycle_warnings(
        snapshot: Mapping[str, Any],
        positions: Sequence[Mapping[str, Any]],
        exposure: Mapping[str, Any],
        capital: Mapping[str, Any],
        evidence: EvidenceBook,
    ) -> list[str]:
        warnings = [str(row.get("code")) for row in _items(snapshot.get("anomalies"))]
        warnings.extend(str(row.get("code")) for row in _items(exposure.get("warnings")))
        if snapshot.get("missing_valuation"):
            warnings.append("valuation_unavailable")
        if _dict(snapshot.get("cash")).get("available_nc") is None:
            warnings.append("available_cash_unavailable")
        if _dict(snapshot.get("cash")).get("reserved_nc") is None:
            warnings.append("reserved_capital_unavailable")
        if capital.get("recommendation") == DeploymentRecommendation.WAIT_FOR_CONFIRMATION.value:
            warnings.append("capital_waiting_for_confirmation")
        warnings.extend(evidence.errors.keys())
        return _unique(warnings)[:50]

    # -- shared evidence views ---------------------------------------------
    @staticmethod
    def _forecast_consensus(
        rows: Sequence[Mapping[str, Any]], *, now: datetime, config: ManagementConfig
    ) -> dict[str, Any]:
        actionable: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        awaiting_confirmation = False
        for raw in rows:
            row = dict(raw)
            validity = _text(row.get("validity") or row.get("status"), 40).lower()
            generated = _mtime(row.get("generated_at") or row.get("forecast_generation_timestamp"))
            age_hours = (now - generated).total_seconds() / 3600.0 if generated else None
            reason = ""
            if validity in {"unconfirmed", "pending", "awaiting_confirmation", "initial"}:
                awaiting_confirmation = True
                reason = "forecast is not confirmed"
            elif validity in {"invalid", "expired", "failed", "unavailable", "stale"}:
                reason = f"forecast validity is {validity}"
            elif age_hours is not None and age_hours > config.max_forecast_age_hours:
                reason = f"forecast is {age_hours:.1f}h old"
            if reason:
                rejected.append({"forecast_id": row.get("forecast_id"), "reason": reason})
                continue
            actionable.append(row)
        counts = {"up": 0, "down": 0, "neutral": 0, "uncertain": 0}
        for row in actionable:
            direction = _text(row.get("direction"), 20).lower()
            counts[direction if direction in counts else "uncertain"] += 1
        model_ids = {
            _text(row.get("model_id") or row.get("engine") or row.get("forecast_id"), 120)
            for row in actionable
        }
        expected_values = [
            value
            for value in (_num(row.get("expected_return")) for row in actionable)
            if value is not None
        ]
        expected = sum(expected_values) / len(expected_values) if expected_values else None
        up, down = counts["up"], counts["down"]
        if up and down:
            direction = "uncertain"
        elif up and up >= max(1, len(actionable) // 2 + 1):
            direction = "up"
        elif down and down >= max(1, len(actionable) // 2 + 1):
            direction = "down"
        elif counts["neutral"] and not up and not down:
            direction = "neutral"
        else:
            direction = "uncertain"
        agreement = (max(up, down) / len(actionable)) if actionable and not (up and down) else 0.0
        uncertainty_classes = [
            _text(
                _dict(row.get("uncertainty")).get("class") or row.get("forecast_uncertainty"),
                20,
            ).lower()
            for row in actionable
        ]
        severity = {"low": 1, "medium": 2, "high": 3}
        uncertainty = max(
            (item for item in uncertainty_classes if item in severity),
            key=lambda item: severity[item],
            default="unknown",
        )
        freshness = [
            _text(_dict(row.get("data_freshness")).get("state"), 20).lower() for row in actionable
        ]
        return {
            "available": bool(actionable),
            "direction": direction,
            "expected_return": _round(expected, 8),
            "uncertainty": uncertainty,
            "agreement": _round(agreement, 4),
            "model_count": len(model_ids),
            "valid_count": len(actionable),
            "counts": counts,
            "freshness": "stale"
            if "stale" in freshness
            else "delayed"
            if "delayed" in freshness
            else "fresh",
            "rejected": rejected,
            "horizon": _text(actionable[0].get("horizon"), 20) if actionable else "",
            "forecast_versions": [
                {
                    "forecast_id": _text(row.get("forecast_id"), 120),
                    "model_id": _text(row.get("model_id"), 120),
                    "model_version": _text(row.get("model_version"), 80),
                    "generated_at": row.get("generated_at"),
                    "horizon": _text(row.get("horizon"), 20),
                }
                for row in actionable
            ],
            "actionable": bool(
                actionable
                and direction in {"up", "down"}
                and not (up and down)
                and "stale" not in freshness
            ),
            "awaiting_confirmation": awaiting_confirmation,
        }

    @staticmethod
    def _research_view(instrument: Mapping[str, Any]) -> dict[str, Any]:
        research = _dict(instrument.get("research"))
        thesis = _dict(research.get("thesis"))
        stance = _text(
            thesis.get("stance") or research.get("thesis_stance") or research.get("stance"),
            40,
        ).lower()
        positive = _items(thesis.get("positive_factors"))
        negative = _items(thesis.get("negative_factors"))
        supporting = [
            _text(row.get("label") or row.get("factor") or row.get("description"), 220)
            for row in positive[:4]
            if _text(row.get("label") or row.get("factor") or row.get("description"), 220)
        ]
        contradictory = [
            _text(row.get("label") or row.get("factor") or row.get("description"), 220)
            for row in negative[:4]
            if _text(row.get("label") or row.get("factor") or row.get("description"), 220)
        ]
        return {
            "run_id": _text(research.get("run_id"), 120),
            "status": _text(research.get("status"), 40).lower(),
            "as_of": research.get("as_of"),
            "stance": stance,
            "positive_factors": supporting,
            "negative_factors": contradictory,
            "conclusion": _text(thesis.get("conclusion"), 600),
            "supporting_evidence": [
                _text(item, 260) for item in thesis.get("supporting_evidence") or ()
            ][:5],
            "contradictory_evidence": [
                _text(item, 260) for item in thesis.get("contradictory_evidence") or ()
            ][:5],
            "advisory_only": True,
        }

    @staticmethod
    def _regime_view(instrument: Mapping[str, Any]) -> dict[str, Any]:
        regime = _dict(instrument.get("regime"))
        return {
            "primary": _text(regime.get("primary") or regime.get("primary_regime"), 60).lower()
            or "uncertain",
            "secondary": _text(regime.get("secondary"), 60),
            "confidence": regime.get("confidence"),
            "confidence_kind": _text(regime.get("confidence_kind"), 40),
            "detected_at": regime.get("detected_at"),
            "status": _text(regime.get("status"), 40) or "available",
        }

    @staticmethod
    def _agent_view(agent: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "decision_id": _text(agent.get("decision_id"), 120),
            "cycle_id": _text(agent.get("cycle_id"), 120),
            "action": _text(agent.get("action"), 30).lower(),
            "supporting_agents": [_text(item, 80) for item in agent.get("supporting_agents") or ()],
            "contradicting_agents": [
                _text(item, 80) for item in agent.get("contradicting_agents") or ()
            ],
            "supporting_evidence": [
                _text(item, 260) for item in agent.get("supporting_evidence") or ()
            ][:5],
            "contradictory_evidence": [
                _text(item, 260) for item in agent.get("contradictory_evidence") or ()
            ][:5],
            "unresolved_conflicts": [
                _text(item, 260) for item in agent.get("unresolved_conflicts") or ()
            ][:5],
            "evidence_strength": _text(agent.get("evidence_strength"), 40),
            "confidence": agent.get("confidence"),
            "confidence_kind": _text(agent.get("confidence_kind"), 60),
            "uncertainty": _dict(agent.get("uncertainty")),
            "explanation": _text(agent.get("explanation"), 700),
            "degraded": bool(agent.get("degraded")),
            "advisory_only": True,
            "production_authority": False,
        }

    @staticmethod
    def _event_view(instrument: Mapping[str, Any]) -> dict[str, Any]:
        chart = _dict(instrument.get("chart"))
        events = _items(chart.get("news_events")) + _items(chart.get("corporate_events"))
        negative = [
            row
            for row in events
            if _text(row.get("sentiment") or row.get("impact"), 30).lower()
            in {"negative", "high_negative", "adverse"}
        ]
        positive = [
            row
            for row in events
            if _text(row.get("sentiment") or row.get("impact"), 30).lower()
            in {"positive", "high_positive", "favourable", "favorable"}
        ]
        return {
            "count": len(events),
            "negative_count": len(negative),
            "positive_count": len(positive),
            "rows": events[:10],
            "recorded_only": True,
        }

    @staticmethod
    def _target_weights(optimization: Mapping[str, Any]) -> dict[str, float]:
        weights: dict[str, float] = {}
        raw = optimization.get("target_weights")
        if isinstance(raw, Mapping):
            for key, value in raw.items():
                parsed = _num(value)
                if parsed is not None:
                    weights[_canonical(key)] = parsed
        allocation = optimization.get("allocation")
        if isinstance(allocation, Sequence) and not isinstance(allocation, (str, bytes, bytearray)):
            for row in allocation:
                if isinstance(row, Mapping):
                    instrument = _canonical(row.get("instrument_id"))
                    parsed = _num(row.get("target_weight") or row.get("weight"))
                    if instrument and parsed is not None:
                        weights[instrument] = parsed
        return weights
