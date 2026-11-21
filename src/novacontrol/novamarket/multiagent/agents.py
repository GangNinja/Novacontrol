"""Deterministic, read-only financial-intelligence agents.

Each agent receives only its declared context slice and emits structured
findings plus provenance.  Agents never call risk assessment, execution,
portfolio mutation, promotion or autonomy controls.  External text is handled as
untrusted data and is never interpreted as instructions.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from novacontrol.novamarket.multiagent.models import (
    AgentDefinition,
    AgentRequest,
    AgentResponse,
    AgentRunStatus,
    EvidenceKind,
    EvidenceRecord,
    FinancialAgentRole,
    Freshness,
    RecommendationAction,
    ValidationStatus,
    canonical_horizon,
    digest,
    freshness_for,
    optional_time,
    stable_id,
    text,
    utcnow,
)

DEFINITIONS: tuple[AgentDefinition, ...] = (
    AgentDefinition(
        "fin-market-research",
        "Market Research Agent",
        FinancialAgentRole.MARKET_RESEARCH,
        (
            "summarize market context",
            "identify supporting and contradictory external evidence",
            "preserve source timestamps and provenance",
        ),
        "AgentRequest.context.market/news",
        "AgentResponse.findings + evidence",
        ("market.read", "news.read"),
        20,
        required=False,
    ),
    AgentDefinition(
        "fin-technical",
        "Technical Analysis Agent",
        FinancialAgentRole.TECHNICAL_ANALYSIS,
        (
            "interpret PI-04 technical features",
            "assess trend, momentum, volatility and volume",
            "identify support/resistance and contradictory signals",
        ),
        "AgentRequest.context.features/bars",
        "AgentResponse.findings + evidence",
        ("features.read", "market.read"),
        15,
    ),
    AgentDefinition(
        "fin-fundamental",
        "Fundamental Analysis Agent",
        FinancialAgentRole.FUNDAMENTAL_ANALYSIS,
        (
            "analyze point-in-time fundamentals",
            "assess growth, profitability and balance-sheet risk",
            "surface missing-data warnings",
        ),
        "AgentRequest.context.fundamentals",
        "AgentResponse.findings + evidence",
        ("fundamentals.read",),
        20,
        required=False,
    ),
    AgentDefinition(
        "fin-prediction",
        "Prediction Intelligence Agent",
        FinancialAgentRole.PREDICTION_INTELLIGENCE,
        (
            "consume PI-05 through PI-10 forecasts",
            "compare horizons and models",
            "surface uncertainty and model disagreement",
        ),
        "AgentRequest.context.forecasts",
        "AgentResponse.findings + evidence",
        ("forecasts.read", "evaluation.read"),
        15,
    ),
    AgentDefinition(
        "fin-regime",
        "Market Regime Agent",
        FinancialAgentRole.MARKET_REGIME,
        (
            "consume PI-04/PI-16 regime evidence",
            "identify transitions and market-condition warnings",
            "never invent a competing classification",
        ),
        "AgentRequest.context.regime",
        "AgentResponse.findings + evidence",
        ("regime.read", "adaptive.read"),
        15,
    ),
    AgentDefinition(
        "fin-risk",
        "Risk Analysis Agent",
        FinancialAgentRole.RISK_ANALYSIS,
        (
            "interpret existing NM-08 assessments",
            "surface restrictions and warnings",
            "never approve, reject or override an order",
        ),
        "AgentRequest.context.risk",
        "AgentResponse.findings + evidence",
        ("risk.read", "portfolio.read"),
        15,
    ),
    AgentDefinition(
        "fin-portfolio",
        "Portfolio Intelligence Agent",
        FinancialAgentRole.PORTFOLIO_INTELLIGENCE,
        (
            "interpret authoritative portfolio state",
            "assess concentration and available capital",
            "identify portfolio conflicts without mutation",
        ),
        "AgentRequest.context.portfolio",
        "AgentResponse.findings + evidence",
        ("portfolio.read", "risk.read"),
        15,
    ),
    AgentDefinition(
        "fin-rl-policy",
        "RL Policy Intelligence Agent",
        FinancialAgentRole.RL_POLICY_INTELLIGENCE,
        (
            "interpret eligible PI-15 experimental policy evidence",
            "compare suitability with the current regime",
            "never activate or execute a policy",
        ),
        "AgentRequest.context.rl/adaptive",
        "AgentResponse.findings + evidence",
        ("rl.read", "adaptive.read", "evaluation.read"),
        20,
        required=False,
    ),
)

DEFINITION_BY_ID = {item.agent_id: item for item in DEFINITIONS}


class FinancialAgent(Protocol):
    definition: AgentDefinition

    def analyze(self, request: AgentRequest, definition: AgentDefinition) -> AgentResponse:
        """Return one validated research response."""


def _number(value: object) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(str(value))
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _decimal(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _timestamp(value: object) -> datetime | None:
    try:
        return optional_time(value)
    except Exception:  # noqa: BLE001 - malformed source timestamps are missing evidence
        return None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: object) -> list[Mapping[str, Any]]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _values(context: Mapping[str, Any]) -> Mapping[str, Any]:
    features = _mapping(context.get("features"))
    raw = features.get("values")
    if isinstance(raw, Mapping):
        return raw
    raw = features.get("features")
    return raw if isinstance(raw, Mapping) else {}


def _bar_prices(context: Mapping[str, Any]) -> list[float]:
    bars = _rows(_mapping(context.get("market")).get("bars"))
    values: list[float] = []
    for row in bars:
        price = _number(row.get("close") or row.get("price") or row.get("last"))
        if price is not None and price > 0:
            values.append(price)
    return values


def _portfolio_id(request: AgentRequest) -> str:
    portfolio = _mapping(request.context.get("portfolio"))
    return text(
        _mapping(portfolio.get("portfolio")).get("portfolio_id")
        or portfolio.get("portfolio_id")
        or request.message.payload.get("portfolio_id")
    )


def _market_evidence(
    request: AgentRequest,
    definition: AgentDefinition,
    *,
    source: str,
    value: Mapping[str, Any],
) -> list[EvidenceRecord]:
    market = _mapping(request.context.get("market"))
    observed = _timestamp(
        market.get("observed_at")
        or market.get("as_of")
        or _mapping(market.get("quote")).get("observed_at")
    )
    if observed and observed > request.as_of:
        observed = None
    return [
        EvidenceRecord(
            evidence_id=stable_id("mae_", request.message.cycle_id, definition.agent_id, source),
            cycle_id=request.message.cycle_id,
            agent_id=definition.agent_id,
            kind=EvidenceKind.VERIFIED if observed else EvidenceKind.MISSING,
            source_identity=source,
            source_reference=text(market.get("source_reference"), source),
            instrument_id=request.message.instrument_id,
            exchange=request.message.exchange,
            summary="market observations available at the cycle cutoff",
            value=dict(value),
            original_at=observed,
            retrieved_at=request.as_of,
            available_at=observed,
            data_version=text(market.get("data_version")),
            freshness=freshness_for(observed, now=request.as_of) if observed else Freshness.UNKNOWN,
            validation=ValidationStatus.VALID if observed else ValidationStatus.UNVERIFIED,
            correlation_group=f"market:{request.message.instrument_id}",
        )
    ]


def _evidence(
    request: AgentRequest,
    definition: AgentDefinition,
    *,
    kind: EvidenceKind,
    source_identity: str,
    source_reference: str,
    summary: str,
    value: Mapping[str, Any],
    original_at: datetime | None,
    published_at: datetime | None = None,
    available_at: datetime | None = None,
    data_version: str = "",
    forecast_version: str = "",
    model_version: str = "",
    feature_set_version: str = "",
    correlation_group: str = "",
    independent: bool = True,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=stable_id(
            "mae_",
            request.message.cycle_id,
            definition.agent_id,
            source_identity,
            source_reference,
            digest(value),
        ),
        cycle_id=request.message.cycle_id,
        agent_id=definition.agent_id,
        kind=kind,
        source_identity=source_identity,
        source_reference=source_reference,
        instrument_id=request.message.instrument_id,
        exchange=request.message.exchange,
        summary=summary,
        value=dict(value),
        original_at=original_at,
        retrieved_at=request.as_of,
        published_at=published_at,
        available_at=available_at or original_at,
        data_version=data_version,
        forecast_version=forecast_version,
        model_version=model_version,
        feature_set_version=feature_set_version,
        freshness=freshness_for(available_at or original_at, now=request.as_of),
        validation=ValidationStatus.VALID
        if (available_at or original_at)
        else ValidationStatus.UNVERIFIED,
        independent=independent,
        correlation_group=correlation_group,
    )


def _base(
    request: AgentRequest,
    definition: AgentDefinition,
    *,
    started: datetime,
    action: RecommendationAction,
    status: AgentRunStatus,
    findings: Mapping[str, Any],
    support: Sequence[str] = (),
    contradict: Sequence[str] = (),
    uncertainty: Sequence[str] = (),
    missing: Sequence[str] = (),
    evidence: Sequence[EvidenceRecord] = (),
    references: Sequence[str] = (),
    confidence: float | None = None,
    confidence_kind: str = "unavailable",
    error: str = "",
) -> AgentResponse:
    finished = utcnow()
    return AgentResponse(
        message_id=request.message.message_id,
        task_id=request.message.task_id,
        cycle_id=request.message.cycle_id,
        agent_id=definition.agent_id,
        role=definition.role,
        status=status,
        action=action,
        confidence=confidence if confidence_kind.startswith("calibrated") else None,
        confidence_kind=confidence_kind,
        findings=dict(findings),
        supporting=tuple(dict.fromkeys(str(item) for item in support if str(item).strip())),
        contradicting=tuple(dict.fromkeys(str(item) for item in contradict if str(item).strip())),
        uncertainties=tuple(dict.fromkeys(str(item) for item in uncertainty if str(item).strip())),
        missing=tuple(dict.fromkeys(str(item) for item in missing if str(item).strip())),
        evidence=tuple(evidence),
        source_references=tuple(dict.fromkeys(references)),
        error=error[:2_000],
        latency_ms=max(0.0, (finished - started).total_seconds() * 1000.0),
        config_version=request.message.config_version,
        model_version=definition.model_version,
        started_at=started,
        finished_at=finished,
    )


class MarketResearchAgent:
    definition = DEFINITION_BY_ID["fin-market-research"]

    def analyze(self, request: AgentRequest, definition: AgentDefinition) -> AgentResponse:
        started = utcnow()
        event_context = _mapping(_mapping(request.context.get("market")).get("events"))
        event_rows = [
            row
            for row in (
                list(event_context.get("verified_events") or [])
                + list(event_context.get("unverified_events") or [])
            )
            if isinstance(row, Mapping)
        ]
        event_news = [
            {
                "event_id": row.get("event_id"),
                "headline": row.get("title"),
                "summary": row.get("title"),
                "published_at": row.get("published_at"),
                "verification": row.get("verification"),
                "importance": row.get("importance"),
                "category": row.get("event_group"),
                "source": next(
                    (
                        source.get("publisher") or source.get("source_id")
                        for source in row.get("source_references") or []
                        if isinstance(source, Mapping)
                    ),
                    "NM-04 event radar",
                ),
                "source_reference": row.get("event_id"),
                "direction": "not_estimated",
            }
            for row in event_rows
        ]
        news = [
            row
            for row in _rows(_mapping(request.context.get("market")).get("news"))
            + _rows(request.context.get("news"))
            + event_news
            if (_timestamp(row.get("available_at") or row.get("published_at")) or request.as_of)
            <= request.as_of
        ]
        metadata = _mapping(_mapping(request.context.get("market")).get("instrument"))
        if not news and not metadata:
            return _base(
                request,
                definition,
                started=started,
                action=RecommendationAction.ABSTAIN,
                status=AgentRunStatus.INSUFFICIENT_EVIDENCE,
                findings={"context_available": False},
                missing=("instrument metadata or news/event evidence",),
            )
        evidence: list[EvidenceRecord] = []
        support: list[str] = []
        contradict: list[str] = []
        uncertainty: list[str] = []
        for index, row in enumerate(news[:20]):
            published = _timestamp(row.get("published_at"))
            available = _timestamp(row.get("available_at")) or published
            if available and available > request.as_of:
                continue
            summary = text(row.get("summary") or row.get("headline"), "market event")
            verified = text(row.get("verification")).lower() in {
                "officially_confirmed",
                "multi_source_corroborated",
            }
            sentiment = text(row.get("sentiment")).lower()
            if sentiment == "positive":
                support.append(f"positive event evidence: {summary}")
            elif sentiment == "negative":
                contradict.append(f"negative event evidence: {summary}")
            if not verified:
                uncertainty.append(f"unverified research item: {summary}")
            evidence.append(
                _evidence(
                    request,
                    definition,
                    kind=EvidenceKind.VERIFIED if verified else EvidenceKind.UNVERIFIED,
                    source_identity=text(row.get("source"), "news_event"),
                    source_reference=text(row.get("source_reference") or row.get("event_id")),
                    summary=summary,
                    value={
                        "importance": text(row.get("importance")),
                        "verification": text(row.get("verification")),
                        "sentiment": sentiment,
                    },
                    original_at=published,
                    published_at=published,
                    available_at=available,
                    correlation_group=f"news:{text(row.get('event_id') or index)}",
                    independent=verified,
                )
            )
        return _base(
            request,
            definition,
            started=started,
            action=RecommendationAction.ABSTAIN,
            status=AgentRunStatus.COMPLETED,
            findings={
                "research_summary": text(
                    metadata.get("name") or metadata.get("instrument_id"),
                    request.message.instrument_id,
                )
                + " context assembled from available point-in-time sources.",
                "market_context": {
                    "sector": text(metadata.get("sector")),
                    "asset_class": text(metadata.get("asset_class")),
                    "exchange": request.message.exchange,
                },
                "news_items": len(evidence),
                "event_items": len(event_news),
                "event_uncertainty": event_context.get("uncertainty"),
                "conflicting_reports": bool(event_context.get("conflicting_reports")),
                "verified_items": sum(1 for item in evidence if item.kind is EvidenceKind.VERIFIED),
                "positive_factors": support,
                "negative_factors": contradict,
                "uncertainties": uncertainty,
            },
            support=support,
            contradict=contradict,
            uncertainty=uncertainty,
            missing=() if news else ("news and corporate-event evidence",),
            evidence=evidence,
            references=[item.source_reference for item in evidence],
        )


class TechnicalAnalysisAgent:
    definition = DEFINITION_BY_ID["fin-technical"]

    def analyze(self, request: AgentRequest, definition: AgentDefinition) -> AgentResponse:
        started = utcnow()
        feature_as_of = _timestamp(
            _mapping(request.context.get("features")).get("as_of")
            or _mapping(request.context.get("market")).get("observed_at")
        )
        if feature_as_of and feature_as_of > request.as_of:
            return _base(
                request,
                definition,
                started=started,
                action=RecommendationAction.ABSTAIN,
                status=AgentRunStatus.INVALID,
                findings={"features_available": False},
                error="the supplied technical snapshot is future-dated",
            )
        values = _values(request.context)
        if not values:
            return _base(
                request,
                definition,
                started=started,
                action=RecommendationAction.ABSTAIN,
                status=AgentRunStatus.INSUFFICIENT_EVIDENCE,
                findings={"features_available": False},
                missing=("PI-04 feature evidence",),
            )
        close = _number(values.get("close"))
        sma20 = _number(values.get("sma_20"))
        sma50 = _number(values.get("sma_50"))
        rsi = _number(values.get("rsi_14"))
        adx = _number(values.get("adx_14"))
        macd_hist = _number(values.get("macd_histogram") or values.get("macd_hist"))
        atr = _number(values.get("atr_14") or values.get("volatility_20"))
        volume_ratio = _number(values.get("volume_ratio"))
        support: list[str] = []
        contradict: list[str] = []
        missing: list[str] = []
        score = 0
        if close is not None and sma20 is not None:
            if close > sma20:
                score += 1
                support.append("price is above the 20-period average")
            else:
                score -= 1
                contradict.append("price is below the 20-period average")
        else:
            missing.append("close/sma_20")
        if sma20 is not None and sma50 is not None:
            if sma20 > sma50:
                score += 1
                support.append("short-term trend is above the long-term average")
            else:
                score -= 1
                contradict.append("short-term trend is below the long-term average")
        else:
            missing.append("sma_50")
        if rsi is not None:
            if rsi >= 60:
                score += 1
                support.append(f"momentum is constructive (RSI {rsi:.1f})")
            elif rsi <= 40:
                score -= 1
                contradict.append(f"momentum is weak (RSI {rsi:.1f})")
        else:
            missing.append("rsi_14")
        if macd_hist is not None:
            score += 1 if macd_hist > 0 else -1
        if adx is not None and adx < 18:
            support.append("trend strength is low")
            score = int(score / 2)
        prices = _bar_prices(request.context)
        levels: dict[str, float] = {}
        if len(prices) >= 5:
            levels = {
                "support_20": min(prices[-20:]),
                "resistance_20": max(prices[-20:]),
            }
        if score >= 2:
            action = RecommendationAction.BUY
        elif score <= -2:
            action = RecommendationAction.SELL
        else:
            action = RecommendationAction.HOLD
        status = (
            AgentRunStatus.INSUFFICIENT_EVIDENCE if len(missing) >= 3 else AgentRunStatus.COMPLETED
        )
        feature_version = text(_mapping(request.context.get("features")).get("version"))
        market_ev = _market_evidence(
            request,
            definition,
            source="NM-02 market observations",
            value={"prices": prices[-20:]},
        )
        feature_ev = _evidence(
            request,
            definition,
            kind=EvidenceKind.DERIVED,
            source_identity="PI-04 feature engineering",
            source_reference=text(
                _mapping(request.context.get("features")).get("feature_set_id"),
                "pi04-features",
            ),
            summary="PI-04 technical feature snapshot",
            value={key: values[key] for key in values if _number(values[key]) is not None},
            original_at=_timestamp(
                _mapping(request.context.get("features")).get("source_last_at")
                or _mapping(request.context.get("features")).get("as_of")
                or _mapping(request.context.get("market")).get("observed_at")
            ),
            feature_set_version=feature_version,
            correlation_group=f"technical:{request.message.instrument_id}",
            independent=False,
        )
        return _base(
            request,
            definition,
            started=started,
            action=action if status is AgentRunStatus.COMPLETED else RecommendationAction.ABSTAIN,
            status=status,
            findings={
                "technical_assessment": "constructive"
                if score >= 2
                else "weak"
                if score <= -2
                else "mixed",
                "trend_direction": "up" if score > 0 else "down" if score < 0 else "sideways",
                "momentum": (
                    "strong" if rsi and rsi >= 60 else "weak" if rsi and rsi <= 40 else "neutral"
                ),
                "volatility": None if atr is None else {"atr_14": atr},
                "volume_behavior": None if volume_ratio is None else {"volume_ratio": volume_ratio},
                "levels": levels,
                "score": score,
                "features_used": [key for key in values if _number(values[key]) is not None],
            },
            support=support,
            contradict=contradict,
            uncertainty=("technical features are descriptive, not calibrated probabilities",),
            missing=missing,
            evidence=(*market_ev, feature_ev),
            references=[feature_ev.source_reference],
        )


class FundamentalAnalysisAgent:
    definition = DEFINITION_BY_ID["fin-fundamental"]

    def analyze(self, request: AgentRequest, definition: AgentDefinition) -> AgentResponse:
        started = utcnow()
        rows = _rows(request.context.get("fundamentals"))
        usable: list[Mapping[str, Any]] = []
        rejected_future = 0
        for row in rows:
            available = _timestamp(row.get("available_at") or row.get("published_at"))
            if available is None:
                continue
            if available > request.as_of:
                rejected_future += 1
                continue
            usable.append(row)
        if not usable:
            return _base(
                request,
                definition,
                started=started,
                action=RecommendationAction.ABSTAIN,
                status=AgentRunStatus.INSUFFICIENT_EVIDENCE,
                findings={
                    "fundamentals_available": False,
                    "future_items_excluded": rejected_future,
                },
                missing=("point-in-time fundamental evidence",),
            )
        latest = max(
            usable,
            key=lambda row: str(row.get("available_at") or row.get("published_at") or ""),
        )
        growth = _number(latest.get("revenue_growth") or latest.get("earnings_growth"))
        margin = _number(latest.get("net_margin") or latest.get("profit_margin"))
        debt = _number(latest.get("debt_to_equity"))
        cash_flow = _number(latest.get("free_cash_flow"))
        pe = _number(latest.get("pe_ratio"))
        score = 0
        support: list[str] = []
        contradict: list[str] = []
        missing: list[str] = []
        if growth is not None:
            score += 1 if growth > 0.05 else -1 if growth < 0 else 0
            (support if growth > 0.05 else contradict if growth < 0 else support).append(
                f"growth is {growth:.2%}"
            )
        else:
            missing.append("growth")
        if margin is not None:
            score += 1 if margin > 0.1 else -1 if margin < 0 else 0
        else:
            missing.append("profitability")
        if debt is not None:
            score += 1 if debt < 0.8 else -1 if debt > 2.0 else 0
            if debt > 2.0:
                contradict.append(f"leverage is elevated (D/E {debt:.2f})")
        else:
            missing.append("debt")
        if cash_flow is not None:
            score += 1 if cash_flow > 0 else -1
        if pe is not None and pe > 45:
            contradict.append(f"valuation multiple is elevated (P/E {pe:.1f})")
            score -= 1
        action = (
            RecommendationAction.BUY
            if score >= 2
            else RecommendationAction.SELL
            if score <= -2
            else RecommendationAction.HOLD
        )
        available = _timestamp(latest.get("available_at") or latest.get("published_at"))
        evidence = _evidence(
            request,
            definition,
            kind=EvidenceKind.VERIFIED,
            source_identity=text(latest.get("source"), "fundamental_snapshot"),
            source_reference=text(latest.get("source_reference") or latest.get("report_id")),
            summary="point-in-time fundamental snapshot",
            value=dict(latest),
            original_at=available,
            published_at=_timestamp(latest.get("published_at")),
            available_at=available,
            data_version=text(latest.get("data_version")),
            correlation_group=f"fundamental:{text(latest.get('fiscal_period'))}",
        )
        return _base(
            request,
            definition,
            started=started,
            action=action,
            status=AgentRunStatus.COMPLETED,
            findings={
                "fundamental_assessment": "positive"
                if score >= 2
                else "negative"
                if score <= -2
                else "mixed",
                "growth_indicators": {"growth": growth},
                "profitability": {"net_margin": margin},
                "financial_health": {"debt_to_equity": debt, "free_cash_flow": cash_flow},
                "valuation": {"pe_ratio": pe},
                "risk_factors": contradict,
                "future_items_excluded": rejected_future,
            },
            support=support,
            contradict=contradict,
            uncertainty=("fundamental snapshots can lag the market and may be restated",),
            missing=missing,
            evidence=(evidence,),
            references=[evidence.source_reference],
        )


class PredictionIntelligenceAgent:
    definition = DEFINITION_BY_ID["fin-prediction"]

    def analyze(self, request: AgentRequest, definition: AgentDefinition) -> AgentResponse:
        started = utcnow()
        horizon = canonical_horizon(request.horizon)
        forecasts: list[Mapping[str, Any]] = []
        excluded = 0
        for row in _rows(request.context.get("forecasts")):
            row_horizon = text(row.get("horizon"))
            if row_horizon and row_horizon != horizon:
                continue
            generated = _timestamp(
                row.get("available_at")
                or row.get("generated_at")
                or row.get("created_at")
                or row.get("as_of")
            )
            if generated and generated > request.as_of:
                excluded += 1
                continue
            if text(row.get("validity")).lower() in {"invalid", "expired"}:
                continue
            forecasts.append(row)
        if not forecasts:
            return _base(
                request,
                definition,
                started=started,
                action=RecommendationAction.ABSTAIN,
                status=AgentRunStatus.INSUFFICIENT_EVIDENCE,
                findings={"forecasts_available": False, "future_forecasts_excluded": excluded},
                missing=(f"stored forecast for horizon {horizon}",),
            )
        returns = [
            value
            for row in forecasts
            if (value := _number(row.get("expected_return") or row.get("predicted_return")))
            is not None
        ]
        directions = [
            text(row.get("direction")).lower() for row in forecasts if row.get("direction")
        ]
        unique = set(directions)
        mean_return = statistics.fmean(returns) if returns else None
        uncertainties = [
            text(row.get("uncertainty")).lower()
            for row in forecasts
            if text(row.get("uncertainty"))
        ]
        high_uncertainty = any(item in {"high", "very_high"} for item in uncertainties)
        calibrated: list[float] = []
        for row in forecasts:
            probability = _number(row.get("p_up"))
            if (
                text(row.get("probability_status") or row.get("calibration")).lower()
                == "calibrated"
                and probability is not None
            ):
                calibrated.append(probability)
        confidence = (
            statistics.fmean(calibrated)
            if calibrated and len(calibrated) == len(forecasts)
            else None
        )
        if mean_return is None:
            action = RecommendationAction.ABSTAIN
        elif mean_return > 0.002 and "down" not in unique:
            action = RecommendationAction.BUY
        elif mean_return < -0.002 and "up" not in unique:
            action = RecommendationAction.SELL
        else:
            action = RecommendationAction.HOLD
        model_versions = sorted(
            {text(row.get("model_version") or row.get("model_id")) for row in forecasts} - {""}
        )
        evidence: list[EvidenceRecord] = []
        for index, row in enumerate(forecasts[:10]):
            at = _timestamp(
                row.get("available_at") or row.get("generated_at") or row.get("created_at")
            )
            evidence.append(
                _evidence(
                    request,
                    definition,
                    kind=EvidenceKind.VERIFIED,
                    source_identity=text(row.get("source"), "PI-05..PI-10 forecast"),
                    source_reference=text(
                        row.get("forecast_id") or row.get("ensemble_id") or index
                    ),
                    summary=f"forecast for {horizon}",
                    value={
                        "direction": text(row.get("direction")),
                        "expected_return": _number(row.get("expected_return")),
                        "uncertainty": text(row.get("uncertainty")),
                        "model_id": text(row.get("model_id")),
                    },
                    original_at=at,
                    available_at=at,
                    forecast_version=text(
                        row.get("ensemble_version") or row.get("forecast_version")
                    ),
                    model_version=text(row.get("model_version") or row.get("model_id")),
                    correlation_group=f"forecast:{text(row.get('model_id') or 'model')}",
                    independent=False,
                )
            )
        support: list[str] = []
        contradict: list[str] = []
        if mean_return is not None:
            (support if mean_return > 0 else contradict).append(
                f"mean expected return is {mean_return:.4%}"
            )
        if "up" in unique:
            support.append("at least one stored forecast is directionally up")
        if "down" in unique:
            contradict.append("at least one stored forecast is directionally down")
        if len(unique) > 1:
            contradict.append("stored models disagree on direction")
        return _base(
            request,
            definition,
            started=started,
            action=action,
            status=AgentRunStatus.COMPLETED,
            findings={
                "forecast_summary": {
                    "horizon": horizon,
                    "forecasts": len(forecasts),
                    "mean_expected_return": mean_return,
                    "directions": sorted(unique),
                },
                "directional_assessment": "up"
                if action is RecommendationAction.BUY
                else "down"
                if action is RecommendationAction.SELL
                else "mixed",
                "expected_return": mean_return,
                "forecast_uncertainty": high_uncertainty,
                "model_disagreement": len(unique) > 1,
                "forecast_validity": "valid",
                "model_versions": model_versions,
                "future_forecasts_excluded": excluded,
                "confidence_basis": "calibrated p_up" if confidence is not None else "uncalibrated",
            },
            support=support,
            contradict=contradict,
            uncertainty=(
                *(("one or more forecasts report high uncertainty",) if high_uncertainty else ()),
                *(("forecast models disagree",) if len(unique) > 1 else ()),
            ),
            missing=() if confidence is not None else ("calibrated forecast probability",),
            evidence=evidence,
            references=[item.source_reference for item in evidence],
            confidence=confidence,
            confidence_kind="calibrated_forecast_mean" if confidence is not None else "unavailable",
        )


class MarketRegimeAgent:
    definition = DEFINITION_BY_ID["fin-regime"]

    def analyze(self, request: AgentRequest, definition: AgentDefinition) -> AgentResponse:
        started = utcnow()
        regime = _mapping(request.context.get("regime"))
        if not regime:
            return _base(
                request,
                definition,
                started=started,
                action=RecommendationAction.ABSTAIN,
                status=AgentRunStatus.INSUFFICIENT_EVIDENCE,
                findings={"regime_available": False},
                missing=("PI-04/PI-16 regime evidence",),
            )
        observed = _timestamp(
            regime.get("source_timestamp")
            or regime.get("as_of")
            or regime.get("available_at")
            or regime.get("detected_at")
        )
        if observed and observed > request.as_of:
            return _base(
                request,
                definition,
                started=started,
                action=RecommendationAction.ABSTAIN,
                status=AgentRunStatus.INVALID,
                findings={"regime_available": False},
                missing=("regime evidence available at the cutoff",),
                error="the supplied regime is future-dated",
            )
        primary = text(regime.get("primary_regime") or regime.get("regime"), "uncertain")
        confidence = _number(regime.get("confidence"))
        if primary in {"strong_up", "weak_up", "breakout"}:
            action = RecommendationAction.BUY
        elif primary in {"strong_down", "weak_down", "breakdown"}:
            action = RecommendationAction.SELL
        elif primary in {"sideways", "low_volatility"}:
            action = RecommendationAction.HOLD
        else:
            action = RecommendationAction.WAIT
        support: list[str] = []
        contradict: list[str] = []
        if action is RecommendationAction.BUY:
            support.append(f"source regime classification is {primary.replace('_', ' ')}")
        elif action is RecommendationAction.SELL:
            contradict.append(f"source regime classification is {primary.replace('_', ' ')}")
        warnings: list[str] = []
        if primary in {"high_volatility", "uncertain", "potential_reversal"}:
            warnings.append(f"regime {primary.replace('_', ' ')} requires caution")
        if primary in {"potential_reversal", "breakout", "breakdown"}:
            warnings.append("transition evidence is present")
        transition = text(request.context.get("regime_transition") or regime.get("transition"))
        evidence = _evidence(
            request,
            definition,
            kind=EvidenceKind.VERIFIED,
            source_identity=text(regime.get("source"), "PI-04 market regime"),
            source_reference=text(regime.get("regime_id") or regime.get("source_reference")),
            summary=f"source regime classification: {primary}",
            value={
                "primary_regime": primary,
                "secondary_conditions": list(regime.get("secondary_conditions") or []),
                "confidence": confidence,
                "transition": transition,
            },
            original_at=observed,
            available_at=observed,
            feature_set_version=text(regime.get("feature_set_version")),
            model_version=text(regime.get("method_version") or regime.get("regime_version")),
            correlation_group=f"regime:{request.message.instrument_id}",
            independent=False,
        )
        return _base(
            request,
            definition,
            started=started,
            action=action,
            status=AgentRunStatus.COMPLETED,
            findings={
                "current_regime": primary,
                "secondary_regimes": list(regime.get("secondary_conditions") or []),
                "regime_confidence": confidence,
                "confidence_status": text(regime.get("confidence_status"), "source-provided"),
                "transition_evidence": transition,
                "market_condition_warnings": warnings,
                "source_version": text(
                    regime.get("method_version") or regime.get("regime_version")
                ),
            },
            support=support,
            contradict=contradict,
            uncertainty=(
                f"regime confidence is {text(regime.get('confidence_status'), 'not calibrated')}",
            )
            if confidence is None
            else (),
            evidence=(evidence,),
            references=[evidence.source_reference],
        )


class RiskAnalysisAgent:
    definition = DEFINITION_BY_ID["fin-risk"]

    def analyze(self, request: AgentRequest, definition: AgentDefinition) -> AgentResponse:
        started = utcnow()
        risk = _mapping(request.context.get("risk"))
        if not risk:
            return _base(
                request,
                definition,
                started=started,
                action=RecommendationAction.ABSTAIN,
                status=AgentRunStatus.INSUFFICIENT_EVIDENCE,
                findings={"risk_assessment_available": False},
                missing=("existing NM-08 assessment/report",),
            )
        outcome = text(risk.get("outcome") or risk.get("status"), "unknown")
        restrictions = [str(item) for item in risk.get("restrictions") or [] if str(item).strip()]
        breaches = [item for item in risk.get("breaches") or [] if isinstance(item, Mapping)]
        warnings = [item for item in risk.get("warnings") or [] if isinstance(item, Mapping)]
        assessed = _timestamp(risk.get("assessed_at") or risk.get("created_at"))
        if assessed and assessed > request.as_of + timedelta(seconds=5):
            return _base(
                request,
                definition,
                started=started,
                action=RecommendationAction.ABSTAIN,
                status=AgentRunStatus.INVALID,
                findings={"risk_assessment_available": False},
                error="the supplied NM-08 assessment is future-dated",
            )
        evidence_at = assessed
        if (
            assessed
            and assessed > request.as_of
            and assessed <= request.as_of + timedelta(seconds=5)
        ):
            # The read happens immediately after the cycle cutoff.  A few
            # milliseconds of transport latency must not become "future data",
            # but the cutoff, not the wall clock, remains the evidence boundary.
            evidence_at = request.as_of
        evidence = _evidence(
            request,
            definition,
            kind=EvidenceKind.VERIFIED,
            source_identity="NM-08 risk engine (read-only report)",
            source_reference=text(risk.get("decision_id") or risk.get("source_reference")),
            summary=f"existing NM-08 risk outcome: {outcome}",
            value={
                "outcome": outcome,
                "restrictions": restrictions,
                "breaches": breaches,
                "warnings": warnings,
            },
            original_at=evidence_at,
            available_at=evidence_at,
            model_version=text(risk.get("policy_version")),
            correlation_group=f"risk:{_portfolio_id(request)}",
            independent=True,
        )
        return _base(
            request,
            definition,
            started=started,
            action=RecommendationAction.ABSTAIN,
            status=AgentRunStatus.COMPLETED,
            findings={
                "risk_summary": outcome,
                "exposure_observations": dict(_mapping(risk.get("metrics"))),
                "concentration_observations": dict(_mapping(risk.get("concentration"))),
                "existing_restrictions": restrictions,
                "risk_warnings": warnings,
                "breaches": breaches,
                "risk_authority": "NM-08",
                "agent_can_approve_or_override": False,
            },
            contradict=[f"NM-08 restriction: {item}" for item in restrictions],
            uncertainty=(
                "the risk report is interpreted, not re-assessed; NM-08 remains authoritative",
            ),
            evidence=(evidence,),
            references=[evidence.source_reference],
        )


class PortfolioIntelligenceAgent:
    definition = DEFINITION_BY_ID["fin-portfolio"]

    def analyze(self, request: AgentRequest, definition: AgentDefinition) -> AgentResponse:
        started = utcnow()
        portfolio = _mapping(request.context.get("portfolio"))
        if not portfolio:
            return _base(
                request,
                definition,
                started=started,
                action=RecommendationAction.ABSTAIN,
                status=AgentRunStatus.INSUFFICIENT_EVIDENCE,
                findings={"portfolio_available": False},
                missing=("authoritative portfolio state",),
            )
        record = _mapping(portfolio.get("portfolio"))
        valuation = _mapping(portfolio.get("valuation"))
        holdings = _rows(portfolio.get("holdings"))
        cash = _decimal(valuation.get("cash"))
        total = _decimal(valuation.get("total_value") or valuation.get("total"))
        cash_weight = _number(valuation.get("cash_weight"))
        held = [
            row
            for row in holdings
            if text(row.get("instrument_id")) == request.message.instrument_id
        ]
        concentration = max(
            (
                value
                for row in holdings
                if (value := _number(row.get("weight") or row.get("weight_pct"))) is not None
            ),
            default=None,
        )
        at = _timestamp(
            portfolio.get("as_of") or record.get("updated_at") or portfolio.get("created_at")
        )
        if at and at > request.as_of + timedelta(seconds=5):
            return _base(
                request,
                definition,
                started=started,
                action=RecommendationAction.ABSTAIN,
                status=AgentRunStatus.INVALID,
                findings={"portfolio_available": False},
                error="the supplied portfolio snapshot is future-dated",
            )
        evidence_at = at
        if at and at > request.as_of and at <= request.as_of + timedelta(seconds=5):
            evidence_at = request.as_of
        evidence = _evidence(
            request,
            definition,
            kind=EvidenceKind.VERIFIED,
            source_identity="NM-07 portfolio service (read-only)",
            source_reference=text(record.get("portfolio_id") or _portfolio_id(request)),
            summary="authoritative portfolio holdings and valuation",
            value={
                "portfolio_id": text(record.get("portfolio_id") or _portfolio_id(request)),
                "cash": str(cash) if cash is not None else "",
                "total_value": str(total) if total is not None else "",
                "holdings": len(holdings),
                "held_instrument": bool(held),
            },
            original_at=evidence_at,
            available_at=evidence_at,
            data_version=text(portfolio.get("data_version")),
            correlation_group=(
                f"portfolio:{text(record.get('portfolio_id') or _portfolio_id(request))}"
            ),
        )
        compatible = True
        warnings: list[str] = []
        optimization = _mapping(portfolio.get("optimization"))
        optimization_summary: dict[str, Any] | None = None
        if optimization:
            optimization_summary = {
                "run_id": text(optimization.get("run_id")),
                "status": text(optimization.get("status")),
                "horizon": text(optimization.get("horizon")),
                "cash_reserve": _number(optimization.get("cash_reserve")),
                "target_weights": {
                    text(key): _number(value)
                    for key, value in _mapping(optimization.get("target_weights")).items()
                    if _number(value) is not None
                },
                "advisory_only": True,
                "production_authority": False,
            }
            if (
                optimization_summary["horizon"]
                and optimization_summary["horizon"] != request.horizon
            ):
                warnings.append("the latest optimization was produced for a different horizon")
            if optimization_summary["status"] not in {
                "completed",
                "completed_with_warnings",
            }:
                warnings.append(
                    f"the latest optimization status is {optimization_summary['status']}"
                )
        if cash is not None and cash <= 0:
            compatible = False
            warnings.append("no available cash for a new long position")
        if concentration is not None and concentration > 0.4:
            warnings.append(f"largest position weight is {concentration:.1%}")
        if len(held):
            warnings.append("the instrument is already held; exposure may increase")
        return _base(
            request,
            definition,
            started=started,
            action=RecommendationAction.ABSTAIN,
            status=AgentRunStatus.COMPLETED,
            findings={
                "portfolio_summary": {
                    "portfolio_id": text(record.get("portfolio_id")),
                    "horizon": text(record.get("horizon")),
                    "status": text(record.get("status")),
                },
                "available_capital": str(cash) if cash is not None else None,
                "cash_weight": cash_weight,
                "existing_exposure": [
                    {
                        "instrument_id": text(row.get("instrument_id")),
                        "weight": _number(row.get("weight")),
                    }
                    for row in holdings[:20]
                ],
                "position_context": {"held": bool(held), "holding": held[:1]},
                "optimization_context": optimization_summary,
                "opportunity_compatibility": compatible,
                "concentration": concentration,
                "portfolio_warnings": warnings,
                "state_mutated": False,
            },
            contradict=warnings,
            uncertainty=("portfolio compatibility is advisory and not a position-size approval",),
            evidence=(evidence,),
            references=[evidence.source_reference],
        )


class RLPolicyIntelligenceAgent:
    definition = DEFINITION_BY_ID["fin-rl-policy"]

    def analyze(self, request: AgentRequest, definition: AgentDefinition) -> AgentResponse:
        started = utcnow()
        rl = _mapping(request.context.get("rl"))
        policies = _rows(rl.get("policies"))
        suitability_rows = _rows(_mapping(request.context.get("adaptive")).get("suitability"))
        eligible: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for policy in policies:
            status = text(policy.get("status")).lower()
            if status not in {"validated", "tested"}:
                continue
            if not text(policy.get("artifact_hash") or policy.get("artifact_version")):
                continue
            policy_at = _timestamp(policy.get("updated_at") or policy.get("created_at"))
            if policy_at and policy_at > request.as_of:
                continue
            test_metrics = _mapping(policy.get("test_metrics"))
            validation = _mapping(policy.get("validation_metrics"))
            if not test_metrics and not validation:
                continue
            policy_id = text(policy.get("policy_id"))
            suitability = next(
                (
                    row
                    for row in suitability_rows
                    if text(row.get("policy_id")) == policy_id
                    and text(row.get("regime"))
                    == text(_mapping(request.context.get("regime")).get("primary_regime"))
                ),
                {},
            )
            eligible.append((policy, suitability))
        if not eligible:
            return _base(
                request,
                definition,
                started=started,
                action=RecommendationAction.ABSTAIN,
                status=AgentRunStatus.INSUFFICIENT_EVIDENCE,
                findings={"eligible_policies": 0},
                missing=("eligible PI-15 policy with validation/test evidence",),
            )
        policy, suitability = eligible[0]
        proposed = text(
            _mapping(rl.get("proposal")).get("action")
            or policy.get("advisory_action")
            or policy.get("last_action")
        ).lower()
        action_map = {
            "buy": RecommendationAction.BUY,
            "sell": RecommendationAction.SELL,
            "hold": RecommendationAction.HOLD,
            "wait": RecommendationAction.WAIT,
            "no_action": RecommendationAction.NO_ACTION,
        }
        action = action_map.get(proposed, RecommendationAction.ABSTAIN)
        suitability_status = text(suitability.get("status"), "not_evaluated")
        limitations = [
            "experimental research policy; no production authority",
            "policy cannot be activated or executed by this subsystem",
        ]
        if suitability_status not in {"suitable", "conditionally_suitable"}:
            limitations.append(f"regime suitability is {suitability_status}")
        evidence = _evidence(
            request,
            definition,
            kind=EvidenceKind.DERIVED,
            source_identity="PI-15 policy artifact + PI-14 evaluation",
            source_reference=text(policy.get("policy_id")),
            summary="eligible experimental policy evidence",
            value={
                "policy_id": text(policy.get("policy_id")),
                "artifact_version": text(policy.get("artifact_version")),
                "artifact_hash": text(policy.get("artifact_hash")),
                "suitability": suitability_status,
                "status": text(policy.get("status")),
            },
            original_at=_timestamp(policy.get("updated_at") or policy.get("created_at")),
            available_at=_timestamp(policy.get("updated_at") or policy.get("created_at")),
            model_version=text(policy.get("artifact_version")),
            correlation_group=f"rl:{text(policy.get('run_id'))}",
            independent=False,
        )
        return _base(
            request,
            definition,
            started=started,
            action=action,
            status=AgentRunStatus.COMPLETED,
            findings={
                "proposed_advisory_action": action.value,
                "policy_id": text(policy.get("policy_id")),
                "policy_version": text(policy.get("artifact_version")),
                "training_evidence": dict(_mapping(policy.get("training_metrics"))),
                "evaluation_evidence": dict(_mapping(policy.get("test_metrics"))),
                "regime_suitability": suitability_status,
                "suitability_score": _number(suitability.get("suitability_score")),
                "uncertainty": "experimental artifact; suitability is research evidence",
                "limitations": limitations,
                "activation_available": False,
                "production_authority": False,
            },
            uncertainty=(
                "RL policy evidence is experimental and may not generalize",
                *(
                    ("regime suitability is not suitable",)
                    if suitability_status not in {"suitable", "conditionally_suitable"}
                    else ()
                ),
            ),
            evidence=(evidence,),
            references=[evidence.source_reference],
        )


AGENT_IMPLEMENTATIONS: dict[str, FinancialAgent] = {
    "fin-market-research": MarketResearchAgent(),
    "fin-technical": TechnicalAnalysisAgent(),
    "fin-fundamental": FundamentalAnalysisAgent(),
    "fin-prediction": PredictionIntelligenceAgent(),
    "fin-regime": MarketRegimeAgent(),
    "fin-risk": RiskAnalysisAgent(),
    "fin-portfolio": PortfolioIntelligenceAgent(),
    "fin-rl-policy": RLPolicyIntelligenceAgent(),
}


def agent_for(agent_id: str) -> FinancialAgent:
    try:
        return AGENT_IMPLEMENTATIONS[agent_id]
    except KeyError as exc:
        raise KeyError(f"unknown financial agent {agent_id!r}") from exc


def relevant_agents(
    *,
    horizon: str,
    available_context: Mapping[str, Any],
    requested: Sequence[str] = (),
) -> tuple[str, ...]:
    """Select only agents whose declared evidence can materially contribute."""
    if requested:
        unknown = [item for item in requested if item not in DEFINITION_BY_ID]
        if unknown:
            raise ValueError(f"unknown financial agents: {', '.join(unknown)}")
        return tuple(dict.fromkeys(requested))
    selected = ["fin-technical", "fin-prediction", "fin-regime", "fin-risk", "fin-portfolio"]
    if available_context.get("news") or _mapping(available_context.get("market")).get("news"):
        selected.insert(0, "fin-market-research")
    if canonical_horizon(horizon) in {"30d", "90d"} or available_context.get("fundamentals"):
        selected.append("fin-fundamental")
    if _mapping(available_context.get("rl")).get("policies"):
        selected.append("fin-rl-policy")
    return tuple(selected)
