"""Company-event, news, macro and sector adapters for PI-19.

External text is data.  Nothing in this module evaluates instructions found in
an article, filing or announcement.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any, cast

from .models import (
    CorporateEvent,
    EventKind,
    EventOccurrence,
    EvidenceKind,
    EvidenceRecord,
    MacroObservation,
    NewsArticle,
    PeerRecord,
    SectorSnapshot,
    SourceTier,
    VerificationStatus,
    clean,
    optional_float,
    optional_time,
)


def _event_status(record: EvidenceRecord, explicit: str) -> str:
    if explicit:
        return explicit
    if record.source_tier == SourceTier.PRIMARY_OFFICIAL.value:
        return VerificationStatus.VERIFIED.value
    if record.source_tier in {
        SourceTier.OFFICIAL_SECONDARY.value,
        SourceTier.INDEPENDENT.value,
    }:
        return VerificationStatus.PARTIALLY_VERIFIED.value
    return VerificationStatus.UNVERIFIED.value


def build_corporate_events(
    *,
    instrument_id: str,
    records: Sequence[EvidenceRecord],
    payload_events: Sequence[Mapping[str, Any]],
    as_of: datetime,
    cutoff: datetime,
) -> tuple[list[CorporateEvent], list[dict[str, Any]], list[str]]:
    events: list[CorporateEvent] = []
    excluded: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for record in records:
        if record.kind != EvidenceKind.CORPORATE_EVENT.value:
            continue
        if record.published_at is None or record.published_at > cutoff:
            excluded.append(
                {
                    "evidence_id": record.evidence_id,
                    "reason": "corporate event is not point-in-time available",
                }
            )
            continue
        meta = record.metadata
        event_type = clean(meta.get("event_type"), limit=40).lower() or EventKind.OTHER.value
        if event_type not in {item.value for item in EventKind}:
            event_type = EventKind.OTHER.value
            warnings.append(f"{record.evidence_id}: unknown event type retained as 'other'")
        effective_at = optional_time(
            meta.get("effective_at") or meta.get("effective_date"),
            "effective_at",
        )
        cancelled = bool(meta.get("cancelled"))
        occurrence = (
            EventOccurrence.CANCELLED.value
            if cancelled
            else EventOccurrence.EFFECTIVE.value
            if effective_at is not None and effective_at <= as_of
            else EventOccurrence.ANNOUNCED.value
        )
        event = CorporateEvent(
            event_id=clean(meta.get("event_id"), limit=80) or "pev_" + record.content_hash[:20],
            instrument_id=instrument_id,
            event_type=event_type,
            announced_at=record.published_at,
            effective_at=effective_at,
            source_name=record.source_name,
            source_url=record.source_url,
            source_tier=record.source_tier,
            description=record.claim,
            financial_details=cast(Mapping[str, Any], meta.get("financial_details") or {}),
            verification=_event_status(record, record.verification),
            occurrence=occurrence,
            interpretation=clean(meta.get("interpretation"), limit=2_000),
            evidence_ids=(record.evidence_id,),
            instruction_risk=record.instruction_risk,
        )
        if event.event_id not in seen:
            seen.add(event.event_id)
            events.append(event)
    for row in payload_events:
        if not (row.get("announced_at") or row.get("announcement_date")):
            excluded.append(
                {
                    "source": "payload",
                    "reason": "corporate event has no announcement timestamp",
                }
            )
            continue
        try:
            event = CorporateEvent.from_payload(
                row, instrument_id=instrument_id, default_time=cutoff
            )
        except Exception as exc:  # noqa: BLE001 - malformed event is explicit
            excluded.append({"source": "payload", "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if event.announced_at > cutoff:
            excluded.append(
                {
                    "event_id": event.event_id,
                    "reason": "announcement is after the research cutoff",
                }
            )
            continue
        if event.event_id in seen:
            continue
        seen.add(event.event_id)
        if event.effective_at is not None and event.effective_at <= as_of:
            event = replace(event, occurrence=EventOccurrence.EFFECTIVE.value)
        events.append(event)
    events.sort(key=lambda item: item.announced_at, reverse=True)
    return events, excluded, warnings


def build_news(
    *,
    instrument_id: str,
    records: Sequence[EvidenceRecord],
    payload_news: Sequence[Mapping[str, Any]],
    as_of: datetime,
    cutoff: datetime,
) -> tuple[list[NewsArticle], list[dict[str, Any]], list[str]]:
    articles: list[NewsArticle] = []
    excluded: list[dict[str, Any]] = []
    warnings: list[str] = []
    for record in records:
        if record.kind != EvidenceKind.NEWS.value:
            continue
        if record.published_at is None or record.published_at > cutoff:
            excluded.append(
                {
                    "evidence_id": record.evidence_id,
                    "reason": "news is not point-in-time available",
                }
            )
            continue
        meta = record.metadata
        try:
            article = NewsArticle.from_payload(
                {
                    **dict(meta),
                    "article_id": meta.get("article_id") or "pna_" + record.content_hash[:20],
                    "headline": meta.get("headline") or record.claim,
                    "summary": meta.get("summary") or record.summary,
                    "source_name": record.source_name,
                    "source_id": record.source_id,
                    "source_url": record.source_url,
                    "source_tier": record.source_tier,
                    "verification": record.verification,
                    "evidence_ids": (record.evidence_id,),
                    "published_at": record.published_at,
                    "retrieved_at": record.retrieved_at,
                    "instrument_ids": (instrument_id,),
                    "is_ai_interpretation": record.is_ai_interpretation,
                    "instruction_risk": record.instruction_risk,
                },
                instrument_id=instrument_id,
                default_time=record.retrieved_at,
            )
        except Exception as exc:  # noqa: BLE001 - malformed news is explicit
            excluded.append(
                {"evidence_id": record.evidence_id, "reason": f"{type(exc).__name__}: {exc}"}
            )
            continue
        articles.append(article)
    for row in payload_news:
        if not (row.get("published_at") or row.get("publication_time")):
            excluded.append(
                {
                    "source": "payload",
                    "reason": "news has no publication timestamp",
                }
            )
            continue
        try:
            article = NewsArticle.from_payload(
                row, instrument_id=instrument_id, default_time=cutoff
            )
        except Exception as exc:  # noqa: BLE001
            excluded.append({"source": "payload", "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if article.published_at is None or article.published_at > cutoff:
            excluded.append(
                {"article_id": article.article_id, "reason": "news is not point-in-time available"}
            )
            continue
        articles.append(article)
    by_id: dict[str, NewsArticle] = {}
    for article in articles:
        existing = by_id.get(article.article_id)
        if existing is None or (article.published_at or as_of) > (existing.published_at or as_of):
            by_id[article.article_id] = article
    result = sorted(by_id.values(), key=lambda item: item.published_at or as_of, reverse=True)
    if len(result) > 100:
        warnings.append("news was truncated to the configured 100-article limit")
        result = result[:100]
    return result, excluded, warnings


def build_macro(
    *,
    payload: Sequence[Mapping[str, Any]],
    as_of: datetime,
    cutoff: datetime,
) -> tuple[list[MacroObservation], list[dict[str, Any]], list[str]]:
    result: list[MacroObservation] = []
    excluded: list[dict[str, Any]] = []
    warnings: list[str] = []
    for row in payload:
        if not row.get("published_at"):
            excluded.append(
                {
                    "indicator": clean(row.get("indicator"), limit=120),
                    "reason": "macro release has no publication timestamp",
                }
            )
            continue
        try:
            item = MacroObservation.from_payload(row, default_time=cutoff)
        except Exception as exc:  # noqa: BLE001
            excluded.append({"source": "macro", "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if item.published_at is None or item.published_at > cutoff:
            excluded.append(
                {
                    "indicator": item.indicator,
                    "reason": "macro release is not point-in-time available",
                }
            )
            continue
        if item.revised_at is not None and item.revised_at <= as_of:
            # The revision is the value a historical reader could actually see.
            item = replace(item, revision_status="revised", value=item.value)
        result.append(item)
    result.sort(key=lambda item: item.published_at or cutoff, reverse=True)
    return result[:100], excluded, warnings


def build_sector(
    *,
    target_sector: str,
    records: Sequence[EvidenceRecord],
    payload: Mapping[str, Any] | None,
    as_of: datetime,
    cutoff: datetime,
) -> tuple[SectorSnapshot | None, list[str]]:
    warnings: list[str] = []
    candidates: list[SectorSnapshot] = []
    if payload:
        try:
            item = SectorSnapshot.from_payload(payload, default_time=cutoff)
            if item.published_at is not None and item.published_at <= cutoff:
                candidates.append(item)
            else:
                warnings.append("sector snapshot is not point-in-time available")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"sector snapshot rejected: {type(exc).__name__}: {exc}")
    for record in records:
        if record.kind != EvidenceKind.SECTOR.value:
            continue
        if record.published_at is None or record.published_at > cutoff:
            warnings.append(f"{record.evidence_id}: sector evidence is not point-in-time available")
            continue
        meta = record.metadata
        try:
            item = SectorSnapshot.from_payload(
                {
                    **dict(meta),
                    "sector": meta.get("sector") or record.claim,
                    "as_of": record.published_at,
                    "source_name": record.source_name,
                    "published_at": record.published_at,
                    "retrieval_at": record.retrieved_at,
                    "evidence_id": record.evidence_id,
                    "verification": record.verification,
                },
                default_time=cutoff,
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{record.evidence_id}: sector evidence rejected: {exc}")
            continue
        candidates.append(item)
    if not candidates:
        return None, warnings
    candidates.sort(
        key=lambda item: (
            item.published_at or cutoff,
            bool(item.evidence_id),
        ),
        reverse=True,
    )
    return candidates[0], warnings


def build_peers(
    *,
    payload: Sequence[Mapping[str, Any]],
    as_of: datetime,
    cutoff: datetime,
) -> tuple[list[PeerRecord], list[dict[str, Any]], list[str]]:
    peers: list[PeerRecord] = []
    excluded: list[dict[str, Any]] = []
    warnings: list[str] = []
    for row in payload:
        if not row.get("period_end"):
            excluded.append(
                {"source": "peer", "reason": "peer has no reporting period"}
            )
            continue
        try:
            peer = PeerRecord.from_payload(row, default_time=cutoff)
        except Exception as exc:  # noqa: BLE001
            excluded.append({"source": "peer", "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if peer.published_at is not None and peer.published_at > cutoff:
            excluded.append(
                {"instrument_id": peer.instrument_id, "reason": "peer data is after cutoff"}
            )
            continue
        if peer.period_end > cutoff:
            excluded.append(
                {"instrument_id": peer.instrument_id, "reason": "peer period ends after cutoff"}
            )
            continue
        if peer.published_at is None:
            warnings.append(f"{peer.instrument_id}: peer has no publication timestamp")
        peers.append(peer)
    return peers, excluded, warnings


def forecast_context(
    records: Sequence[Mapping[str, Any]], *, horizon: str, cutoff: datetime
) -> dict[str, Any]:
    usable: list[dict[str, Any]] = []
    excluded: list[str] = []
    for row in records:
        try:
            row_horizon = clean(row.get("horizon"), limit=8).lower()
            if row_horizon and row_horizon != horizon:
                excluded.append("horizon mismatch")
                continue
            available = optional_time(
                row.get("available_at") or row.get("generated_at"), "forecast time"
            )
            if available is None:
                excluded.append("forecast has no publication timestamp")
                continue
            if available > cutoff:
                excluded.append("forecast was not available at cutoff")
                continue
            expected = optional_float(
                row.get("expected_return")
                or row.get("predicted_return")
                or row.get("expected_return_fraction")
            )
        except Exception as exc:  # noqa: BLE001 - malformed forecast is explicit
            excluded.append(f"forecast rejected: {type(exc).__name__}: {exc}")
            continue
        usable.append(
            {
                "forecast_id": clean(row.get("forecast_id") or row.get("id"), limit=80),
                "horizon": row_horizon or horizon,
                "expected_return": expected,
                "uncertainty": clean(
                    row.get("uncertainty") or row.get("forecast_uncertainty"), limit=40
                ),
                "model_version": clean(row.get("model_version") or row.get("model_id"), limit=80),
                "available_at": available.isoformat() if available else None,
                "disagreement": optional_float(row.get("disagreement")),
                "is_calibrated": bool(
                    row.get("probability_is_calibrated") or row.get("calibrated")
                ),
            }
        )
    returns = [item["expected_return"] for item in usable if item["expected_return"] is not None]
    disagreement = (
        max(returns) - min(returns)
        if len(returns) >= 2
        else (usable[0]["disagreement"] if usable else None)
    )
    return {
        "available": bool(usable),
        "horizon": horizon,
        "forecasts": usable,
        "expected_return_mean": sum(returns) / len(returns) if returns else None,
        "disagreement": disagreement,
        "calibrated_probability_claimed": any(item["is_calibrated"] for item in usable),
        "excluded": sorted(set(excluded)),
        "warnings": [] if usable else ["no horizon-compatible point-in-time forecast was supplied"],
        "is_prediction": True,
        "is_research_direction": False,
    }
