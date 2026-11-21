"""PI-24 event-intelligence coordinator.

The coordinator reads NM-04's normalized, verified event history and turns it
into relevance assessments, horizon-specific warnings, point-in-time feature
snapshots, historical event studies and read-only handoffs for PI-17, PI-22 and
PI-23. It has no order, ledger, risk-decision or promotion path.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from novacontrol.novamarket.event_intelligence.models import (
    CORE_EVENT_FLAGS,
    EVALUATION_POLICY_VERSION,
    FEATURE_VERSION,
    HORIZON_DAYS,
    HORIZONS,
    PI24_CONTRACT_VERSION,
    PI24_PHASE,
    PI24_VERSION,
    RELEVANCE_ORDER,
    RELEVANCE_POLICY_VERSION,
    RISK_AUTHORITY,
    WARNING_POLICY_VERSION,
    EventGroup,
    EventIntelligenceConfig,
    EventIntelligenceNotFoundError,
    EventIntelligenceSources,
    EventIntelligenceValidationError,
    EventStatus,
    ImpactStatus,
    OutcomeStatus,
    RelevanceClass,
    WarningStatus,
    canonical_horizon,
    canonical_instrument,
    clean,
    days_between,
    digest,
    event_group,
    event_rows,
    importance_value,
    instruction_risk,
    number,
    parse_time,
    stable_id,
    utcnow,
)
from novacontrol.novamarket.event_intelligence.store import EventIntelligenceStore

_VIEWS = (
    ("overview", "Event overview", "Verified, unverified and upcoming events."),
    ("timeline", "Timeline", "Publication, event date and processing history."),
    ("relevance", "Relevance", "Instrument, sector and market relevance."),
    ("impact", "Impact", "Observed outcomes and horizon channels, never a direction."),
    ("warnings", "Forecast warnings", "Event-driven uncertainty for a horizon."),
    ("features", "Event features", "Point-in-time evidence consumable by PI-04."),
    ("outcomes", "Outcomes", "Historical event studies with correlation caveats."),
    ("agents", "Agent evidence", "Read-only evidence for the existing PI-17 agents."),
)

_VERIFIED_STATES = {"officially_confirmed", "multi_source_corroborated"}
_UNVERIFIED_STATES = {"unverified", "single_source"}
_MARKET_GROUPS = {EventGroup.ECONOMIC.value, EventGroup.MARKET.value}
_CORPORATE_GROUPS = {EventGroup.CORPORATE.value, EventGroup.COMPANY_SPECIFIC.value}


def _status_for(row: Mapping[str, Any]) -> str:
    radar_status = clean(row.get("status"), 40).lower()
    verification = clean(row.get("verification"), 40).lower()
    if radar_status == "duplicate":
        return EventStatus.DUPLICATE.value
    if radar_status == "irrelevant":
        return EventStatus.IRRELEVANT.value
    if verification == "retracted":
        return EventStatus.EXPIRED.value
    if verification == "conflicting_evidence":
        return EventStatus.CONFLICTED.value
    if radar_status == "expired" or verification == "expired":
        return EventStatus.EXPIRED.value
    if radar_status == "resolved":
        return EventStatus.COMPLETED.value
    if verification in _VERIFIED_STATES:
        return EventStatus.VERIFIED.value
    if verification in _UNVERIFIED_STATES:
        return EventStatus.UNVERIFIED.value
    if radar_status in {"discovered", "extracted", "normalized", "verification"}:
        return EventStatus.DETECTED.value
    return EventStatus.UNVERIFIED.value


def _rows(value: object) -> list[dict[str, Any]]:
    return [dict(row) for row in event_rows(value)]


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [clean(item, 160) for item in value if clean(item, 160)]
    if isinstance(value, str) and value.strip():
        return [clean(value, 160)]
    return []


def _bar_returns(bars: Sequence[Mapping[str, Any]]) -> list[float]:
    closes = [number(bar.get("close")) for bar in bars]
    usable = [value for value in closes if value is not None and value > 0]
    return [
        usable[index] / usable[index - 1] - 1.0
        for index in range(1, len(usable))
        if usable[index - 1]
    ]


def _stdev(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


class EventIntelligenceService:
    """The one PI-24 coordinator over existing event and research evidence."""

    def __init__(
        self,
        sources: EventIntelligenceSources,
        *,
        store_path: str | Any = ":memory:",
        clock: Any | None = None,
        config: EventIntelligenceConfig | None = None,
    ) -> None:
        self._sources = (
            sources if isinstance(sources, EventIntelligenceSources) else EventIntelligenceSources()
        )
        self._clock = clock or utcnow
        self._store = EventIntelligenceStore(store_path, clock=self._clock)
        self._config = config or self._store.load_config() or EventIntelligenceConfig()
        self._errors: list[str] = []
        self._instrument_cache: dict[str, dict[str, Any]] = {}
        self._regime_cache: dict[str, dict[str, Any]] = {}

    @property
    def store(self) -> EventIntelligenceStore:
        return self._store

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    @property
    def config(self) -> EventIntelligenceConfig:
        return self._config

    def close(self) -> None:
        self._store.close()

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _safe(self, label: str, reader: Any, default: Any) -> Any:
        try:
            return reader()
        except Exception as exc:  # noqa: BLE001 - evidence failure is explicit
            message = f"{label}: {type(exc).__name__}: {exc}"
            if message not in self._errors:
                self._errors.append(message)
            return default

    def _call_sources(self, name: str, **kwargs: Any) -> Any:
        reader = getattr(self._sources, name, None)
        if reader is None:
            return None
        try:
            return reader(**kwargs)
        except TypeError as exc:
            self._errors.append(f"{name}: adapter signature mismatch: {exc}")
            return None
        except Exception as exc:  # noqa: BLE001 - source failure is explicit
            self._errors.append(f"{name}: {type(exc).__name__}: {exc}")
            return None

    def _instrument_view(self, instrument_id: str) -> dict[str, Any]:
        canonical = canonical_instrument(instrument_id)
        if canonical in self._instrument_cache:
            return self._instrument_cache[canonical]
        body = self._call_sources("instrument", instrument_id=canonical) or {}
        value = _mapping(body)
        self._instrument_cache[canonical] = value
        return value

    def _companies(self, instrument_ids: Sequence[str]) -> list[str]:
        names: list[str] = []
        for instrument_id in instrument_ids[: self._config.max_instruments]:
            instrument = self._instrument_view(instrument_id)
            name = clean(instrument.get("name"), 160)
            if name:
                names.append(name)
        return names

    def _regime(self, instrument_id: str) -> dict[str, Any]:
        if instrument_id in self._regime_cache:
            return self._regime_cache[instrument_id]
        body = self._call_sources("regime", instrument_id=instrument_id) or {}
        value = _mapping(body)
        self._regime_cache[instrument_id] = value
        return value

    def normalize(self, row: Mapping[str, Any]) -> dict[str, Any]:
        event_id = clean(row.get("event_id"), 120)
        if not event_id:
            raise EventIntelligenceValidationError("an event row has no event_id")
        subcategory = clean(row.get("subcategory"), 80).lower()
        group = event_group(subcategory)
        status = _status_for(row)
        instruments = _as_list(row.get("instruments"))
        mappings = [dict(item) for item in row.get("mappings") or [] if isinstance(item, Mapping)]
        if not instruments:
            instruments = [
                clean(item.get("instrument_id"), 80)
                for item in mappings
                if clean(item.get("instrument_id"), 80)
            ]
        sectors = _as_list(row.get("sectors")) or sorted(
            {clean(item.get("sector"), 80) for item in mappings if clean(item.get("sector"), 80)}
        )
        published = parse_time(row.get("published_at"), "published_at")
        occurred = parse_time(row.get("occurred_at"), "occurred_at")
        retrieved = parse_time(row.get("retrieved_at"), "retrieved_at")
        discovered = parse_time(row.get("discovered_at"), "discovered_at")
        updated = parse_time(row.get("updated_at"), "updated_at")
        classification = _mapping(row.get("classification"))
        verification_detail = _mapping(row.get("verification_detail"))
        evidence = [dict(item) for item in row.get("evidence") or [] if isinstance(item, Mapping)]
        companies = self._companies(instruments)
        exchange = clean(row.get("exchange"), 20)
        if not exchange and instruments:
            exchange = clean(self._instrument_view(instruments[0]).get("exchange"), 20)
        event_time = occurred or published
        body = {
            "event_id": event_id,
            "event_key": clean(row.get("event_key"), 120),
            "version": int(number(row.get("version")) or 1),
            "title": clean(row.get("title"), 500),
            "description": clean(row.get("description")),
            "category": clean(row.get("category"), 40) or "unclassified",
            "event_group": group,
            "subcategory": subcategory or "unclassified",
            "status": status,
            "radar_status": clean(row.get("status"), 40),
            "verification": clean(row.get("verification"), 40) or "unverified",
            "verification_reasons": list(verification_detail.get("reasons") or []),
            "conflicts": list(verification_detail.get("conflicts") or []),
            "importance": clean(row.get("importance"), 20) or "low",
            "importance_value": importance_value(row.get("importance")),
            "confidence": number(row.get("confidence")),
            "event_date": event_time.isoformat() if event_time else None,
            "occurred_at": occurred.isoformat() if occurred else None,
            "published_at": published.isoformat() if published else None,
            "retrieved_at": retrieved.isoformat() if retrieved else None,
            "discovered_at": discovered.isoformat() if discovered else None,
            "updated_at": updated.isoformat() if updated else None,
            "exchange": exchange,
            "market": clean(row.get("market"), 40) or "GLOBAL",
            "instruments": instruments,
            "companies": companies,
            "sectors": sectors,
            "source_references": [
                {
                    "source_id": clean(item.get("source_id"), 80),
                    "publisher": clean(item.get("publisher"), 160),
                    "url": clean(item.get("url"), 500),
                    "published_at": clean(item.get("published_at"), 40),
                    "retrieved_at": clean(item.get("retrieved_at"), 40),
                    "reliability": clean(item.get("reliability"), 40),
                    "status": clean(item.get("status"), 40),
                }
                for item in evidence
            ],
            "evidence_count": int(number(row.get("evidence_count")) or len(evidence)),
            "independent_sources": int(number(row.get("independent_sources")) or 0),
            "mappings": mappings,
            "classification": classification,
            "instruction_risk": instruction_risk(
                f"{row.get('title') or ''} {row.get('description') or ''}"
            ),
            "processing_status": "normalized",
            "related_events": [
                clean(item, 120) for item in row.get("related_events") or [] if clean(item, 120)
            ],
            "is_price_prediction": False,
            "is_trading_recommendation": False,
        }
        return body

    def sync(self, *, instrument_id: str = "", limit: int | None = None) -> dict[str, Any]:
        instrument = canonical_instrument(instrument_id) if instrument_id else ""
        rows = _rows(
            self._call_sources(
                "events",
                instrument_id=instrument or None,
                limit=min(limit or self._config.max_events, self._config.max_events),
            )
            or ()
        )
        saved = 0
        skipped: list[dict[str, Any]] = []
        for row in rows:
            try:
                event = self.normalize(row)
            except Exception as exc:  # noqa: BLE001 - one malformed event is explicit
                skipped.append({"event_id": clean(row.get("event_id"), 120), "reason": str(exc)})
                continue
            record_id = f"eieve_{event['event_id']}_v{event['version']}"
            existing = self._store.get(record_id)
            if existing is None:
                self._store.save(
                    "event",
                    event,
                    record_id=record_id,
                    event_id=event["event_id"],
                    instrument_id=instrument,
                    status=event["status"],
                    created_at=str(event.get("retrieved_at") or event.get("published_at") or ""),
                )
                saved += 1
        return {
            "ok": True,
            "phase": PI24_PHASE,
            "synced": saved,
            "seen": len(rows),
            "skipped": skipped,
            "errors": self.errors,
            "sources": ["NM-04 event radar"],
            "new_feed_added": False,
            "financial_mutation": False,
        }

    def events(
        self,
        *,
        instrument_id: str = "",
        status: str = "",
        group: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        instrument = canonical_instrument(instrument_id) if instrument_id else ""
        limit = max(1, min(int(limit), self._config.max_events))
        offset = max(0, int(offset))
        rows = _rows(
            self._call_sources(
                "events",
                instrument_id=instrument or None,
                limit=min(max(limit + offset, 1), self._config.max_events),
            )
            or ()
        )
        normalized = [self.normalize(row) for row in rows]
        if status:
            normalized = [row for row in normalized if row["status"] == status]
        if group:
            normalized = [row for row in normalized if row["event_group"] == group]
        normalized.sort(
            key=lambda row: row.get("published_at") or row.get("event_date") or "", reverse=True
        )
        page = normalized[offset : offset + limit]
        return {
            "ok": True,
            "events": page,
            "total": len(normalized),
            "limit": limit,
            "offset": offset,
            "errors": self.errors,
            "sources": ["NM-04 event radar"],
        }

    def event(self, event_id: str, *, version: int | None = None) -> dict[str, Any]:
        identifier = clean(event_id, 120)
        if not identifier:
            raise EventIntelligenceValidationError("an event id is required")
        body = self._call_sources("event", event_id=identifier, version=version)
        if not isinstance(body, Mapping) or not body.get("event_id"):
            stored = self._store.latest("event", event_id=identifier)
            if stored is None:
                raise EventIntelligenceNotFoundError(f"unknown event {identifier!r}")
            body = stored
        event = self.normalize(body)
        return {
            "ok": True,
            "event": event,
            "evidence": [
                dict(item) for item in body.get("evidence") or [] if isinstance(item, Mapping)
            ],
            "transitions": list(body.get("transitions") or []),
            "errors": self.errors,
        }

    def assess_relevance(
        self,
        payload: Mapping[str, Any] | None = None,
        *,
        actor: str = "",
    ) -> dict[str, Any]:
        body = _mapping(payload)
        instrument = canonical_instrument(body.get("instrument_id"))
        horizon = canonical_horizon(body.get("horizon") or "5d")
        event_id = clean(body.get("event_id"), 120)
        as_of = parse_time(body.get("as_of"), "as_of") or self._now()
        event = self.event(event_id)["event"] if event_id else None
        events = [event] if event else self._relevant_events(instrument, horizon, as_of)
        assessments = [
            self._relevance(item, instrument, horizon, as_of) for item in events if item is not None
        ]
        primary = assessments[0] if assessments else None
        result: dict[str, Any] = {
            "ok": True,
            "phase": PI24_PHASE,
            "assessment_id": stable_id(
                "eirel_", instrument, horizon, event_id, as_of.isoformat(), digest(assessments)
            ),
            "created_at": self._now().isoformat(),
            "as_of": as_of.isoformat(),
            "instrument_id": instrument,
            "horizon": horizon,
            "event_id": event_id or None,
            "assessment": primary,
            "assessments": assessments,
            "events_considered": len(assessments),
            "policy_version": RELEVANCE_POLICY_VERSION,
            "direction": "not_estimated",
            "sentiment_prediction": False,
            "actor": clean(actor, 120),
            "limitations": [
                "Relevance is not a price direction or a probability.",
                "An event can be relevant without a defensible direction.",
            ],
        }
        self._store.save(
            "relevance",
            result,
            record_id=result["assessment_id"],
            event_id=event_id,
            instrument_id=instrument,
            horizon=horizon,
            relevance=str((primary or {}).get("relevance") or ""),
            status="assessed",
        )
        return result

    def _relevance(
        self, event: Mapping[str, Any], instrument_id: str, horizon: str, as_of: datetime
    ) -> dict[str, Any]:
        published = parse_time(event.get("published_at"), "published_at")
        occurred = parse_time(event.get("occurred_at"), "occurred_at")
        if published is None and occurred is None:
            return self._relevance_body(
                event,
                instrument_id,
                horizon,
                RelevanceClass.INSUFFICIENT_EVIDENCE,
                ["the event has neither a publication nor an event timestamp"],
            )
        if published is not None and published > as_of:
            return self._relevance_body(
                event,
                instrument_id,
                horizon,
                RelevanceClass.INSUFFICIENT_EVIDENCE,
                ["the event was not published at the assessment time"],
            )
        target = self._instrument_view(instrument_id) if instrument_id else {}
        target_sectors = {
            value.lower()
            for value in (*_as_list(target.get("sectors")), clean(target.get("sector"), 80).lower())
            if value
        }
        mappings = [item for item in event.get("mappings") or [] if isinstance(item, Mapping)]
        direct = [
            item
            for item in mappings
            if item.get("relationship") == "direct" and item.get("instrument_id") == instrument_id
        ]
        if instrument_id and instrument_id in _as_list(event.get("instruments")) and not mappings:
            direct = [{"instrument_id": instrument_id, "relationship": "direct"}]
        if direct:
            return self._relevance_body(
                event,
                instrument_id,
                horizon,
                RelevanceClass.DIRECT,
                [f"the event explicitly maps to {instrument_id}"],
            )
        event_sectors = {value.lower() for value in _as_list(event.get("sectors")) if value}
        for item in mappings:
            sector = clean(item.get("sector"), 80).lower()
            if sector:
                event_sectors.add(sector)
        shared = sorted(target_sectors & event_sectors)
        if shared:
            return self._relevance_body(
                event,
                instrument_id,
                horizon,
                RelevanceClass.SECTOR_RELATED,
                [f"shared sector exposure: {', '.join(shared)}"],
            )
        group = clean(event.get("event_group"), 40)
        importance = number(event.get("importance_value")) or 0.0
        if group in _MARKET_GROUPS and importance >= 0.5:
            return self._relevance_body(
                event,
                instrument_id,
                horizon,
                RelevanceClass.MARKET_WIDE,
                ["a market-wide event family with at least moderate importance"],
            )
        indirect = [
            item
            for item in mappings
            if item.get("relationship") == "indirect" and item.get("instrument_id") == instrument_id
        ]
        if indirect:
            return self._relevance_body(
                event,
                instrument_id,
                horizon,
                RelevanceClass.INDIRECT,
                ["the event maps to the instrument indirectly"],
            )
        if group in _CORPORATE_GROUPS:
            return self._relevance_body(
                event,
                instrument_id,
                horizon,
                RelevanceClass.NOT_RELEVANT,
                ["a company event with no instrument or sector link"],
            )
        if not _as_list(event.get("instruments")) and not _as_list(event.get("sectors")):
            return self._relevance_body(
                event,
                instrument_id,
                horizon,
                RelevanceClass.INSUFFICIENT_EVIDENCE,
                ["no instrument, company or sector mapping exists"],
            )
        return self._relevance_body(
            event,
            instrument_id,
            horizon,
            RelevanceClass.NOT_RELEVANT,
            ["no direct, sector or market-wide relationship was established"],
        )

    def _relevance_body(
        self,
        event: Mapping[str, Any],
        instrument_id: str,
        horizon: str,
        relevance: RelevanceClass,
        reasons: Sequence[str],
    ) -> dict[str, Any]:
        verified = clean(event.get("status"), 40) == EventStatus.VERIFIED.value
        confidence = (
            "high"
            if verified and relevance is RelevanceClass.DIRECT
            else "medium"
            if verified and relevance in {RelevanceClass.SECTOR_RELATED, RelevanceClass.MARKET_WIDE}
            else "low"
            if relevance is not RelevanceClass.INSUFFICIENT_EVIDENCE
            else "insufficient"
        )
        score = {
            RelevanceClass.DIRECT: 0.95,
            RelevanceClass.SECTOR_RELATED: 0.7,
            RelevanceClass.MARKET_WIDE: 0.55,
            RelevanceClass.INDIRECT: 0.4,
            RelevanceClass.NOT_RELEVANT: 0.0,
            RelevanceClass.INSUFFICIENT_EVIDENCE: None,
        }[relevance]
        horizon_group = HORIZONS[horizon]["group"]
        regime = self._regime(instrument_id) if instrument_id else {}
        horizon_effect = {
            "intraday": "immediate_reaction_and_gap_risk_may_be_material",
            "swing": "post_announcement_repricing_may_be_material",
            "long_term": "fundamental_or_structural_reassessment_may_be_material",
        }[horizon_group]
        return {
            "event_id": event.get("event_id"),
            "instrument_id": instrument_id,
            "horizon": horizon,
            "relevance": relevance.value,
            "relevance_rank": RELEVANCE_ORDER[relevance],
            "relevance_score": score,
            "confidence": confidence,
            "reasons": list(reasons),
            "event_group": event.get("event_group"),
            "subcategory": event.get("subcategory"),
            "importance": event.get("importance"),
            "verification": event.get("verification"),
            "event_status": event.get("status"),
            "horizon_effect": horizon_effect,
            "market_regime": clean(
                regime.get("primary") or regime.get("regime") or regime.get("label"),
                80,
            )
            or None,
            "regime_volatility": clean(
                regime.get("volatility") or regime.get("volatility_state"), 40
            )
            or None,
            "direction": "not_estimated",
            "is_price_prediction": False,
        }

    def _relevant_events(
        self, instrument_id: str, horizon: str, as_of: datetime
    ) -> list[dict[str, Any]]:
        rows = _rows(
            self._call_sources(
                "events",
                instrument_id=canonical_instrument(instrument_id),
                limit=self._config.max_events,
            )
            or ()
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                event = self.normalize(row)
            except EventIntelligenceValidationError as exc:
                message = f"normalize: {type(exc).__name__}: {exc}"
                if message not in self._errors:
                    self._errors.append(message)
                continue
            if event["status"] == EventStatus.EXPIRED.value:
                continue
            published = parse_time(event.get("published_at"), "published_at")
            if published is not None and published > as_of:
                continue
            if published is not None and published < (
                as_of - timedelta(days=self._config.lookback_days)
            ):
                continue
            relevance = self._relevance(event, instrument_id, horizon, as_of)
            if relevance["relevance"] in {
                RelevanceClass.DIRECT.value,
                RelevanceClass.SECTOR_RELATED.value,
                RelevanceClass.MARKET_WIDE.value,
                RelevanceClass.INDIRECT.value,
            }:
                result.append({**event, "relevance": relevance})
        result.sort(
            key=lambda row: (
                RELEVANCE_ORDER[RelevanceClass(row["relevance"]["relevance"])],
                row.get("published_at") or "",
            ),
            reverse=True,
        )
        return result

    def warnings(
        self, payload: Mapping[str, Any] | None = None, *, persist: bool = True
    ) -> dict[str, Any]:
        body = _mapping(payload)
        instrument = canonical_instrument(body.get("instrument_id"))
        horizon = canonical_horizon(body.get("horizon") or "5d")
        as_of = parse_time(body.get("as_of"), "as_of") or self._now()
        errors_before = len(self._errors)
        relevant = self._relevant_events(instrument, horizon, as_of)
        warnings: list[dict[str, Any]] = []
        now = self._now()
        horizon_end = as_of + timedelta(days=HORIZON_DAYS[horizon])
        approaching_end = horizon_end + timedelta(hours=self._config.approaching_hours)
        upcoming: list[str] = []
        for event in relevant:
            occurred = parse_time(event.get("occurred_at"), "occurred_at")
            if occurred is not None and as_of <= occurred <= approaching_end:
                upcoming.append(event["event_id"])
            if event["status"] == EventStatus.CONFLICTED.value:
                warnings.append(
                    self._warning(
                        event,
                        "event_conflicting_reports",
                        "high",
                        "Conflicting reports exist; no direction is inferred.",
                    )
                )
            elif event["status"] == EventStatus.UNVERIFIED.value:
                warnings.append(
                    self._warning(
                        event,
                        "event_unverified",
                        "watch",
                        "The event is not verified against sufficient independent evidence.",
                    )
                )
            if (
                event.get("importance_value") or 0.0
            ) >= self._config.volatility_importance_threshold:
                warnings.append(
                    self._warning(
                        event,
                        "event_high_importance_volatility",
                        "warning",
                        "A high-importance event can elevate volatility without a known direction.",
                    )
                )
        if upcoming:
            warnings.append(
                {
                    "code": "event_approaching",
                    "severity": "informational",
                    "event_ids": sorted(set(upcoming)),
                    "explanation": "Scheduled events fall inside or shortly beyond this horizon.",
                    "direction": "not_estimated",
                }
            )
        latest_event = max(
            (
                moment
                for event in relevant
                if (
                    moment := parse_time(
                        event.get("occurred_at") or event.get("published_at"),
                        "event timestamp",
                    )
                )
                is not None
            ),
            default=None,
        )
        if (
            latest_event is not None
            and (as_of - latest_event).total_seconds() > self._config.stale_hours * 3_600
        ):
            warnings.append(
                {
                    "code": "event_evidence_stale",
                    "severity": "informational",
                    "event_ids": [event["event_id"] for event in relevant],
                    "explanation": (
                        "The newest relevant event is older than the configured "
                        "event-evidence freshness window."
                    ),
                    "direction": "not_estimated",
                }
            )
        if any(item["code"] == "event_conflicting_reports" for item in warnings):
            status = WarningStatus.EVENT_UNCERTAINTY.value
        elif any(item["code"] == "event_high_importance_volatility" for item in warnings):
            status = WarningStatus.EVENT_VOLATILITY_RISK.value
        elif any(item["code"] == "event_unverified" for item in warnings):
            status = WarningStatus.UNVERIFIED_EVENT.value
        elif upcoming:
            status = WarningStatus.EVENT_APPROACHING.value
        elif not relevant and len(self._errors) > errors_before:
            status = WarningStatus.INSUFFICIENT_EVIDENCE.value
        else:
            status = WarningStatus.NORMAL.value
        result: dict[str, Any] = {
            "ok": True,
            "phase": PI24_PHASE,
            "warning_id": stable_id(
                "eiwarn_",
                instrument,
                horizon,
                as_of.isoformat(),
                digest([item["event_id"] for item in relevant]),
            ),
            "created_at": now.isoformat(),
            "as_of": as_of.isoformat(),
            "instrument_id": instrument,
            "horizon": horizon,
            "status": status,
            "warnings": warnings,
            "relevant_events": len(relevant),
            "upcoming_event_ids": sorted(set(upcoming)),
            "direction": "not_estimated",
            "calibrated_event_probability": None,
            "probability_claimed": False,
            "risk_authority": RISK_AUTHORITY,
            "creates_risk_decision": False,
            "policy_version": WARNING_POLICY_VERSION,
            "errors": self.errors,
        }
        if persist:
            self._store.save(
                "warning",
                result,
                record_id=result["warning_id"],
                instrument_id=instrument,
                horizon=horizon,
                status=status,
            )
        return result

    def _warning(
        self, event: Mapping[str, Any], code: str, severity: str, explanation: str
    ) -> dict[str, Any]:
        return {
            "code": code,
            "severity": severity,
            "event_ids": [event["event_id"]],
            "title": event.get("title"),
            "verification": event.get("verification"),
            "importance": event.get("importance"),
            "explanation": explanation,
            "direction": "not_estimated",
        }

    def impact(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = _mapping(payload)
        event_id = clean(body.get("event_id"), 120)
        if not event_id:
            raise EventIntelligenceValidationError("an event id is required")
        instrument = canonical_instrument(body.get("instrument_id"))
        horizon = canonical_horizon(body.get("horizon") or "5d")
        as_of = parse_time(body.get("as_of"), "as_of") or self._now()
        event = self.event(event_id)["event"]
        relevance = self._relevance(event, instrument, horizon, as_of)
        outcomes = [
            row
            for row in self._store.list(
                "outcome", event_id=event_id, instrument_id=instrument, limit=500
            )
            if clean(row.get("status"), 40) == OutcomeStatus.RECORDED.value
            and (
                (observed := parse_time(row.get("observed_at"), "observed_at")) is None
                or observed <= as_of
            )
        ]
        sufficient = len(outcomes) >= self._config.min_outcome_samples
        if sufficient:
            abs_returns = [
                abs(value)
                for row in outcomes
                if (value := number(row.get("post_return"))) is not None
            ]
            status = ImpactStatus.OBSERVED_HISTORICAL.value
            historical = {
                "samples": len(outcomes),
                "mean_post_return": _mean(
                    [
                        value
                        for row in outcomes
                        if (value := number(row.get("post_return"))) is not None
                    ]
                ),
                "mean_absolute_post_return": _mean(abs_returns),
                "mean_volatility_change": _mean(
                    [
                        value
                        for row in outcomes
                        if (value := number(row.get("volatility_change"))) is not None
                    ]
                ),
                "causation_established": False,
                "note": "Historical co-movement is not evidence that the event caused the move.",
            }
        else:
            status = (
                ImpactStatus.DIRECTION_UNKNOWN.value
                if relevance["relevance"] != RelevanceClass.NOT_RELEVANT.value
                else ImpactStatus.INSUFFICIENT_EVIDENCE.value
            )
            historical = {
                "samples": len(outcomes),
                "minimum_required": self._config.min_outcome_samples,
                "mean_post_return": None,
                "causation_established": False,
                "note": "Too few observed outcomes to report historical impact statistics.",
            }
        result: dict[str, Any] = {
            "ok": True,
            "phase": PI24_PHASE,
            "impact_id": stable_id("eiimp_", event_id, instrument, horizon, as_of.isoformat()),
            "created_at": self._now().isoformat(),
            "event_id": event_id,
            "instrument_id": instrument,
            "horizon": horizon,
            "status": status,
            "relevance": relevance,
            "historical": historical,
            "channels": self._impact_channels(event),
            "direction": "not_estimated",
            "price_adjustment": "none",
            "calibrated_event_probability": None,
            "probability_claimed": False,
            "causation_established": False,
            "limitations": [
                "Event impact is not a directional forecast.",
                "No price adjustment is applied from sentiment or event category.",
                "Observed co-movement does not establish causation.",
            ],
            "errors": self.errors,
        }
        self._store.save(
            "impact",
            result,
            record_id=result["impact_id"],
            event_id=event_id,
            instrument_id=instrument,
            horizon=horizon,
            relevance=relevance["relevance"],
            status=status,
        )
        return result

    @staticmethod
    def _impact_channels(event: Mapping[str, Any]) -> list[str]:
        subcategory = clean(event.get("subcategory"), 80)
        group = clean(event.get("event_group"), 40)
        channels: list[str] = ["uncertainty"]
        if subcategory in {"earnings", "guidance", "earnings_trend"}:
            channels.extend(["fundamental_expectations", "post_announcement_repricing"])
        if subcategory in {
            "dividend",
            "stock_split",
            "buyback",
            "fundraising",
            "merger_acquisition",
        }:
            channels.append("capital_structure")
        if subcategory in {"trading_halt", "circuit_breaker", "infrastructure_incident"}:
            channels.append("liquidity_and_execution")
        if subcategory in {"interest_rate", "inflation", "central_bank"}:
            channels.append("discount_rate_and_macro")
        if group in _MARKET_GROUPS:
            channels.append("market_wide_risk")
        return sorted(set(channels))

    def features(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = _mapping(payload)
        instrument = canonical_instrument(body.get("instrument_id"))
        horizon = canonical_horizon(body.get("horizon") or "5d")
        as_of = parse_time(body.get("as_of"), "as_of") or self._now()
        events = self._relevant_events(instrument, horizon, as_of)
        window_start = as_of - timedelta(days=20)
        recent = [
            event
            for event in events
            if (parse_time(event.get("published_at"), "published_at") or as_of) >= window_start
        ]
        recent_5 = [
            event
            for event in events
            if (parse_time(event.get("published_at"), "published_at") or as_of)
            >= as_of - timedelta(days=5)
        ]
        upcoming = [
            event
            for event in events
            if (occurred := parse_time(event.get("occurred_at"), "occurred_at")) is not None
            and occurred > as_of
            and occurred <= as_of + timedelta(days=self._config.upcoming_days)
        ]
        values: dict[str, tuple[float | None, str]] = {
            "event_count_5d": (float(len(recent_5)), "zero" if not recent_5 else "value"),
            "event_count_20d": (float(len(recent)), "zero" if not recent else "value"),
            "event_verified_count_20d": (
                float(sum(1 for event in recent if event["status"] == EventStatus.VERIFIED.value)),
                "value",
            ),
            "event_unverified_count_20d": (
                float(
                    sum(
                        1
                        for event in recent
                        if event["status"]
                        in {EventStatus.UNVERIFIED.value, EventStatus.CONFLICTED.value}
                    )
                ),
                "value",
            ),
            "event_importance_max_20d": (
                max((event.get("importance_value") or 0.0 for event in recent), default=None),
                "value" if recent else "missing",
            ),
            "event_relevance_max_20d": (
                max(
                    (event["relevance"]["relevance_score"] or 0.0 for event in recent), default=None
                ),
                "value" if recent else "missing",
            ),
            "event_frequency_20d": (float(len(recent)) / 20.0, "value" if recent else "zero"),
            "event_upcoming_count": (float(len(upcoming)), "zero" if not upcoming else "value"),
            "event_sector_count_20d": (
                float(len({sector for event in recent for sector in event.get("sectors") or []})),
                "value",
            ),
            "event_market_wide_flag": (
                1.0
                if any(
                    event["relevance"]["relevance"] == RelevanceClass.MARKET_WIDE.value
                    for event in recent
                )
                else 0.0,
                "value",
            ),
            "event_uncertainty": (
                1.0
                if any(
                    event["status"] in {EventStatus.UNVERIFIED.value, EventStatus.CONFLICTED.value}
                    for event in recent
                )
                else 0.0,
                "value",
            ),
        }
        published_times = [
            moment
            for event in recent
            if (moment := parse_time(event.get("published_at"), "published_at")) is not None
        ]
        latest = max(published_times, default=None)
        values["event_age_days"] = (
            days_between(as_of, latest) if latest else None,
            "value" if latest else "missing",
        )
        next_event = min(
            (
                occurred
                for event in upcoming
                if (occurred := parse_time(event.get("occurred_at"), "occurred_at")) is not None
            ),
            default=None,
        )
        values["event_proximity_days"] = (
            days_between(next_event, as_of) if next_event else None,
            "value" if next_event else "missing",
        )
        values["event_time_since_publication_hours"] = (
            days_between(as_of, latest) * 24.0 if latest else None,
            "value" if latest else "missing",
        )
        for subcategory, flag in CORE_EVENT_FLAGS.items():
            present = any(event.get("subcategory") == subcategory for event in recent)
            values[flag] = (1.0 if present else 0.0, "value")
        outcome_rows = self._store.list("outcome", instrument_id=instrument, limit=500)
        observed = [
            abs(value)
            for row in outcome_rows
            if clean(row.get("status"), 40) == OutcomeStatus.RECORDED.value
            and (
                (moment := parse_time(row.get("observed_at"), "observed_at")) is None
                or moment <= as_of
            )
            and (value := number(row.get("post_return"))) is not None
        ]
        values["event_historical_abs_return_mean"] = (
            _mean(observed) if observed else None,
            "value" if observed else "missing",
        )
        features = [
            {
                "name": name,
                "value": round(value, 6) if value is not None else None,
                "status": status if value is not None else "missing",
                "version": FEATURE_VERSION,
                "source": "pi24-event-intelligence",
            }
            for name, (value, status) in sorted(values.items())
        ]
        result: dict[str, Any] = {
            "ok": True,
            "phase": PI24_PHASE,
            "snapshot_id": stable_id(
                "eif_",
                instrument,
                horizon,
                as_of.isoformat(),
                digest([(item["name"], item["value"]) for item in features]),
            ),
            "created_at": self._now().isoformat(),
            "as_of": as_of.isoformat(),
            "instrument_id": instrument,
            "horizon": horizon,
            "feature_version": FEATURE_VERSION,
            "features": features,
            "feature_count": len(features),
            "events_used": len(recent),
            "upcoming_events": [event["event_id"] for event in upcoming],
            "point_in_time": True,
            "future_outcomes_included": False,
            "schema_versioning": FEATURE_VERSION,
            "provenance": {
                "events": "NM-04 radar",
                "event_ids": [event["event_id"] for event in recent],
                "horizon": horizon,
                "as_of": as_of.isoformat(),
            },
            "pi04_rows": [
                {
                    "event_id": event["event_id"],
                    "event_type": event.get("subcategory"),
                    "category": event.get("event_group"),
                    "subcategory": event.get("subcategory"),
                    "headline": event.get("title"),
                    "instrument_ids": event.get("instruments") or [instrument],
                    "source": "NM-04 event radar",
                    "source_url": next(
                        (
                            source.get("url")
                            for source in event.get("source_references") or []
                            if source.get("url")
                        ),
                        "",
                    ),
                    "published_at": event.get("published_at"),
                    "occurred_at": event.get("occurred_at"),
                    "retrieved_at": event.get("retrieved_at"),
                    "importance": event.get("importance_value"),
                    "relevance": event["relevance"]["relevance_score"],
                    "metadata": {
                        "event_group": event.get("event_group"),
                        "subcategory": event.get("subcategory"),
                        "verification": event.get("verification"),
                        "direction": "not_estimated",
                    },
                }
                for event in recent
            ],
            "direction": "not_estimated",
            "errors": self.errors,
        }
        self._store.save(
            "feature_snapshot",
            result,
            record_id=result["snapshot_id"],
            instrument_id=instrument,
            horizon=horizon,
            status="recorded",
        )
        return result

    def forecasts(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = _mapping(payload)
        instrument = canonical_instrument(body.get("instrument_id"))
        horizon = canonical_horizon(body.get("horizon") or "5d")
        as_of = parse_time(body.get("as_of"), "as_of") or self._now()
        events = self._relevant_events(instrument, horizon, as_of)
        warnings = self.warnings(
            {"instrument_id": instrument, "horizon": horizon, "as_of": as_of.isoformat()},
            persist=False,
        )
        features = self.features(
            {"instrument_id": instrument, "horizon": horizon, "as_of": as_of.isoformat()}
        )
        forecast_rows = _rows(
            self._call_sources(
                "forecasts",
                instrument_id=instrument,
                horizon=horizon,
                limit=50,
            )
            or ()
        )
        status = (
            "event_volatility_risk_elevated"
            if warnings["status"] == WarningStatus.EVENT_VOLATILITY_RISK.value
            else "conditionally_suitable"
            if warnings["status"]
            in {
                WarningStatus.EVENT_UNCERTAINTY.value,
                WarningStatus.UNVERIFIED_EVENT.value,
                WarningStatus.EVENT_APPROACHING.value,
            }
            else "suitable"
            if forecast_rows
            else "insufficient_evidence"
        )
        result: dict[str, Any] = {
            "ok": True,
            "phase": PI24_PHASE,
            "context_id": stable_id(
                "eifc_", instrument, horizon, as_of.isoformat(), digest(warnings["warning_id"])
            ),
            "created_at": self._now().isoformat(),
            "instrument_id": instrument,
            "horizon": horizon,
            "as_of": as_of.isoformat(),
            "status": status,
            "event_suitability": status,
            "warnings": warnings,
            "relevant_events": events,
            "feature_snapshot_id": features["snapshot_id"],
            "forecasts": forecast_rows[:20],
            "forecast_count": len(forecast_rows),
            "event_adjustment": {
                "applied": False,
                "price_adjustment": "none",
                "direction": "not_estimated",
                "reason": (
                    "PI-24 supplies event evidence and warnings; it does not adjust "
                    "a forecast without a separately validated event-impact model."
                ),
            },
            "abstention_recommended": False,
            "abstention_reason": "Event evidence alone does not justify suppressing a forecast.",
            "model_version_changed": False,
            "forecast_mutated": False,
            "errors": self.errors,
        }
        self._store.save(
            "forecast_context",
            result,
            record_id=result["context_id"],
            instrument_id=instrument,
            horizon=horizon,
            status=status,
        )
        return result

    def outcomes(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = _mapping(payload)
        event_id = clean(body.get("event_id"), 120)
        if not event_id:
            raise EventIntelligenceValidationError("an event id is required")
        instrument = canonical_instrument(body.get("instrument_id"))
        horizon = canonical_horizon(body.get("horizon") or "5d")
        as_of = parse_time(body.get("as_of"), "as_of") or self._now()
        event = self.event(event_id)["event"]
        event_time = parse_time(event.get("occurred_at") or event.get("published_at"), "event_time")
        if event_time is None:
            raise EventIntelligenceValidationError(
                "event outcome tracking needs an event or publication timestamp"
            )
        intraday = horizon in {"5m", "15m", "20m", "1h"}
        resolution = (
            "5m" if horizon in {"5m", "15m"} else "1h" if horizon in {"20m", "1h"} else "1d"
        )
        horizon_hours = {
            "5m": 5.0 / 60.0,
            "15m": 0.25,
            "20m": 1.0 / 3.0,
            "1h": 1.0,
        }.get(horizon, HORIZON_DAYS[horizon] * 24.0)
        start = event_time - timedelta(days=2 if intraday else 10)
        end = min(as_of, event_time + timedelta(hours=max(horizon_hours, 1.0 / 60.0)))
        bars = [
            dict(row)
            for row in self._call_sources(
                "bars",
                instrument_id=instrument,
                start=start,
                end=end,
                resolution=resolution,
                limit=500 if intraday else 100,
                ascending=True,
            )
            or ()
            if isinstance(row, Mapping)
        ]
        bars = [
            row
            for row in bars
            if clean(row.get("quality"), 20).lower() in {"", "valid", "warning"}
            and not bool(row.get("partial"))
            and (observed := parse_time(row.get("observed_at"), "observed_at")) is not None
            and observed <= as_of
        ]
        bars.sort(key=lambda row: str(row.get("observed_at") or ""))
        pre = [
            row
            for row in bars
            if (moment := parse_time(row.get("observed_at"), "observed_at")) is not None
            and moment <= event_time
        ]
        post = [
            row
            for row in bars
            if (moment := parse_time(row.get("observed_at"), "observed_at")) is not None
            and moment > event_time
        ]
        post = [
            row
            for row in post
            if (moment := parse_time(row.get("observed_at"), "observed_at")) is not None
            and (moment - event_time).total_seconds() / 3_600.0 <= horizon_hours
        ]
        forecast_rows = _rows(
            self._call_sources(
                "forecasts",
                instrument_id=instrument,
                horizon=horizon,
                limit=200,
            )
            or ()
        )
        before_forecasts: list[tuple[datetime, dict[str, Any]]] = []
        after_forecasts: list[tuple[datetime, dict[str, Any]]] = []
        for row in forecast_rows:
            moment = parse_time(
                row.get("as_of") or row.get("generated_at") or row.get("created_at"),
                "forecast timestamp",
            )
            if moment is None:
                continue
            if moment <= event_time:
                before_forecasts.append((moment, row))
            else:
                after_forecasts.append((moment, row))
        before_forecasts.sort(key=lambda item: item[0])
        after_forecasts.sort(key=lambda item: item[0])
        forecast_before = before_forecasts[-1][1] if before_forecasts else None
        forecast_after = after_forecasts[0][1] if after_forecasts else None
        evaluation_rows = _rows(self._call_sources("evaluations", limit=8) or ())
        evaluation_context = {
            "available": bool(evaluation_rows),
            "count": len(evaluation_rows),
            "evaluation_ids": [
                clean(row.get("evaluation_id"), 120)
                for row in evaluation_rows[:8]
                if clean(row.get("evaluation_id"), 120)
            ],
            "note": "PI-11 evaluation reports are historical evidence, not event causation.",
        }
        if len(pre) < 2 or len(post) < 1:
            insufficient: dict[str, Any] = {
                "ok": True,
                "phase": PI24_PHASE,
                "outcome_id": stable_id("eiout_", event_id, instrument, horizon, as_of.isoformat()),
                "created_at": self._now().isoformat(),
                "event_id": event_id,
                "instrument_id": instrument,
                "horizon": horizon,
                "status": OutcomeStatus.INSUFFICIENT_DATA.value,
                "bars_before": len(pre),
                "bars_after": len(post),
                "resolution": resolution,
                "observed_at": None,
                "event_time": event_time.isoformat(),
                "pre_return": None,
                "post_return": None,
                "price_move": None,
                "volatility_change": None,
                "prediction_error": None,
                "forecast_comparison": self._forecast_comparison(
                    forecast_before, forecast_after, None, None, event_time
                ),
                "evaluation_context": evaluation_context,
                "causation_established": False,
                "limitations": ["At least two pre-event bars and one post-event bar are required."],
            }
            self._store.save(
                "outcome",
                insufficient,
                record_id=insufficient["outcome_id"],
                event_id=event_id,
                instrument_id=instrument,
                horizon=horizon,
                status=insufficient["status"],
            )
            return insufficient
        pre_closes = [number(row.get("close")) for row in pre]
        post_closes = [number(row.get("close")) for row in post]
        base = next(
            (value for value in reversed(pre_closes) if value is not None and value > 0), None
        )
        first_pre = next((value for value in pre_closes if value is not None and value > 0), None)
        last_post = next(
            (value for value in reversed(post_closes) if value is not None and value > 0), None
        )
        pre_returns = _bar_returns(pre)
        post_returns = _bar_returns([pre[-1], *post]) if base else []
        pre_vol = _stdev(pre_returns)
        post_vol = _stdev(post_returns)
        actual = last_post
        supplied_error = number(body.get("prediction_error"))
        forecast_comparison = self._forecast_comparison(
            forecast_before, forecast_after, actual, base, event_time
        )
        result: dict[str, Any] = {
            "ok": True,
            "phase": PI24_PHASE,
            "outcome_id": stable_id("eiout_", event_id, instrument, horizon, as_of.isoformat()),
            "created_at": self._now().isoformat(),
            "event_id": event_id,
            "instrument_id": instrument,
            "horizon": horizon,
            "status": OutcomeStatus.RECORDED.value,
            "bars_before": len(pre),
            "bars_after": len(post),
            "resolution": resolution,
            "observed_at": post[-1].get("observed_at"),
            "event_time": event_time.isoformat(),
            "pre_return": (base / first_pre - 1.0) if base and first_pre else None,
            "post_return": (last_post / base - 1.0) if last_post and base else None,
            "price_move": (last_post - base) if last_post and base else None,
            "volatility_change": (post_vol - pre_vol)
            if post_vol is not None and pre_vol is not None
            else None,
            "prediction_error": supplied_error
            if supplied_error is not None
            else forecast_comparison.get("prediction_error"),
            "forecast_comparison": forecast_comparison,
            "evaluation_context": evaluation_context,
            "causation_established": False,
            "correlation_only": True,
            "limitations": [
                "The observed move is co-movement, not proof of causation.",
                "No direction is inferred from the event category or sentiment.",
            ],
            "errors": self.errors,
        }
        self._store.save(
            "outcome",
            result,
            record_id=result["outcome_id"],
            event_id=event_id,
            instrument_id=instrument,
            horizon=horizon,
            status=result["status"],
        )
        return result

    @staticmethod
    def _forecast_comparison(
        before: Mapping[str, Any] | None,
        after: Mapping[str, Any] | None,
        actual: float | None,
        base: float | None,
        event_time: datetime,
    ) -> dict[str, Any]:
        predicted = number((before or {}).get("predicted_price"))
        error = (
            actual / predicted - 1.0
            if actual is not None and predicted is not None and predicted > 0
            else None
        )
        return {
            "before_event": {
                "forecast_id": (before or {}).get("forecast_id"),
                "model_id": (before or {}).get("model_id"),
                "model_version": (before or {}).get("model_version")
                or (before or {}).get("version"),
                "predicted_price": predicted,
                "as_of": (before or {}).get("as_of") or (before or {}).get("generated_at"),
            }
            if before
            else None,
            "after_event": {
                "forecast_id": (after or {}).get("forecast_id"),
                "model_id": (after or {}).get("model_id"),
                "model_version": (after or {}).get("model_version") or (after or {}).get("version"),
                "predicted_price": number((after or {}).get("predicted_price")),
                "as_of": (after or {}).get("as_of") or (after or {}).get("generated_at"),
            }
            if after
            else None,
            "actual_price": actual,
            "base_price": base,
            "prediction_error": error,
            "event_time": event_time.isoformat(),
            "method": (
                "latest stored forecast at or before the event compared with the observed close"
            ),
            "causation_established": False,
            "note": "A stored forecast is not proof that the event caused its error.",
        }

    def outcome_study(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = _mapping(payload)
        instrument = (
            canonical_instrument(body.get("instrument_id")) if body.get("instrument_id") else ""
        )
        rows = self._store.list(
            "outcome", instrument_id=instrument, limit=self._config.max_outcomes
        )
        recorded = [
            row for row in rows if clean(row.get("status"), 40) == OutcomeStatus.RECORDED.value
        ]
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in recorded:
            groups.setdefault(clean(row.get("horizon"), 20) or "unknown", []).append(row)
        studies = []
        for horizon, values in sorted(groups.items()):
            returns = [
                value for row in values if (value := number(row.get("post_return"))) is not None
            ]
            abs_returns = [abs(value) for value in returns]
            changes = [
                value
                for row in values
                if (value := number(row.get("volatility_change"))) is not None
            ]
            studies.append(
                {
                    "horizon": horizon,
                    "samples": len(values),
                    "sufficient": len(values) >= self._config.min_outcome_samples,
                    "mean_post_return": _mean(returns),
                    "mean_absolute_post_return": _mean(abs_returns),
                    "mean_volatility_change": _mean(changes),
                    "causation_established": False,
                }
            )
        return {
            "ok": True,
            "phase": PI24_PHASE,
            "instrument_id": instrument,
            "studies": studies,
            "outcomes": recorded[: self._config.max_outcomes],
            "total": len(recorded),
            "minimum_samples": self._config.min_outcome_samples,
            "policy_version": "pi24.event-outcome.1.0",
            "correlation_only": True,
            "causation_established": False,
            "limitations": [
                "Historical co-movement is not proof that an event caused a price move.",
                "Studies below the minimum sample size are marked insufficient.",
            ],
        }

    def forecast_evaluation(
        self, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Event-aware versus non-event-aware forecast error, or insufficient data.

        The records come from PI-11's own collector (stored lifecycle and NM-06
        outcomes, filtered and de-duplicated exactly as an evaluation report),
        and are scored with PI-11's metric functions. A record counts as
        event-labelled when it declares event awareness (`metadata.event_aware`,
        `metadata.event_ids`, or an event feature-set version), or when a stored
        event was already published inside the point-in-time window
        `[generated_at - lookback_days, generated_at]` for that instrument.

        A comparison is only reported when BOTH cohorts reach the configured
        minimum sample size; otherwise the result is an explicit
        ``insufficient_data`` record and no improvement is claimed. Nothing here
        is causal: the move may have had other drivers.
        """

        from novacontrol.novamarket.evaluation import EvaluationConfig
        from novacontrol.novamarket.evaluation.metrics import (
            direction_metrics,
            price_metrics,
            return_metrics,
        )

        body = _mapping(payload)
        instrument = (
            canonical_instrument(body.get("instrument_id"))
            if body.get("instrument_id")
            else ""
        )
        horizon = canonical_horizon(body.get("horizon")) if body.get("horizon") else ""
        requested_min = number(body.get("min_samples"))
        min_samples = (
            int(requested_min)
            if requested_min is not None
            else self._config.min_evaluation_samples
        )
        if min_samples < 2:
            raise EventIntelligenceValidationError("min_samples must be at least 2")
        as_of = parse_time(body.get("as_of"), "as_of") or self._now()
        request: dict[str, Any] = {
            "name": "pi24 event-aware forecast evaluation",
            "sources": ["lifecycle", "nm06"],
            "instruments": [instrument] if instrument else [],
            "horizons": [horizon] if horizon else [],
            "min_samples": 2,
            "max_records": min(self._config.max_outcomes, 10_000),
            "as_of": as_of.isoformat(),
        }
        config = EvaluationConfig.from_mapping(request)
        collected = self._call_sources("evaluation_records", payload=request)
        rows = collected.get("records") if isinstance(collected, Mapping) else None
        records = [row for row in rows or () if row is not None]
        rejected = (
            int(number(collected.get("rejected_count")) or 0)
            if isinstance(collected, Mapping)
            else 0
        )
        events_cache: dict[str, list[datetime]] = {}

        def event_times(symbol: str) -> list[datetime]:
            if symbol in events_cache:
                return events_cache[symbol]
            moments: list[datetime] = []
            raw = self._call_sources(
                "events", instrument_id=symbol, limit=self._config.max_events
            )
            for row in _rows(raw or ()):
                try:
                    event = self.normalize(row)
                except EventIntelligenceValidationError:
                    continue
                moment = parse_time(
                    event.get("published_at") or event.get("occurred_at"), "event_time"
                )
                if moment is not None and moment <= as_of:
                    moments.append(moment)
            moments.sort()
            events_cache[symbol] = moments
            return moments

        window = timedelta(days=self._config.lookback_days)
        basis: dict[str, int] = {"declared": 0, "event_proximity": 0}

        def labelled(record: Any) -> bool:
            metadata = getattr(record, "metadata", None)
            metadata = metadata if isinstance(metadata, Mapping) else {}
            feature = str(getattr(record, "feature_set_version", "") or "").lower()
            if (
                metadata.get("event_aware") is True
                or metadata.get("event_ids")
                or "event" in feature
            ):
                basis["declared"] += 1
                return True
            generated = getattr(record, "generated_at", None)
            if not isinstance(generated, datetime):
                return False
            for moment in event_times(str(getattr(record, "instrument_id", ""))):
                if generated - window <= moment <= generated:
                    basis["event_proximity"] += 1
                    return True
            return False

        # Label every record exactly once: a second pass would double-count the
        # basis breakdown and compare frozen records by value.
        flags = [labelled(row) for row in records]
        event_aware = [row for row, flag in zip(records, flags) if flag]
        non_event = [row for row, flag in zip(records, flags) if not flag]

        def cohort(items: Sequence[Any]) -> dict[str, Any]:
            if not items:
                return {"samples": 0, "return": None, "price": None, "direction": None}
            try:
                direction = direction_metrics(items, config)
            except Exception as exc:  # noqa: BLE001 - report the gap, never fabricate
                self._errors.append(f"direction_metrics: {type(exc).__name__}: {exc}")
                direction = None
            return {
                "samples": len(items),
                "return": return_metrics(items, config),
                "price": price_metrics(items, config),
                "direction": direction,
            }

        aware_block = cohort(event_aware)
        baseline_block = cohort(non_event)
        sufficient = len(event_aware) >= min_samples and len(non_event) >= min_samples
        threshold = 0.02
        comparison: dict[str, Any] | None = None
        verdict: str | None = None
        if sufficient:
            aware_error = (aware_block["return"] or {}).get("mean_absolute_error")
            baseline_error = (baseline_block["return"] or {}).get("mean_absolute_error")
            if (
                isinstance(aware_error, (int, float))
                and isinstance(baseline_error, (int, float))
                and baseline_error > 0
            ):
                delta = float(aware_error) - float(baseline_error)
                relative = delta / float(baseline_error)
                verdict = (
                    "event_aware_better"
                    if relative <= -threshold
                    else "non_event_aware_better"
                    if relative >= threshold
                    else "no_material_difference"
                )
                comparison = {
                    "metric": "return_mae",
                    "event_aware": {
                        "samples": len(event_aware),
                        "return_mae": aware_error,
                        "price_mae": (aware_block["price"] or {}).get("mae"),
                        "directional_accuracy": (aware_block["direction"] or {}).get("accuracy"),
                    },
                    "non_event_aware": {
                        "samples": len(non_event),
                        "return_mae": baseline_error,
                        "price_mae": (baseline_block["price"] or {}).get("mae"),
                        "directional_accuracy": (baseline_block["direction"] or {}).get(
                            "accuracy"
                        ),
                    },
                    "delta": {
                        "return_mae": delta,
                        "relative_return_mae_change": relative,
                    },
                    "material_difference_threshold": threshold,
                    "verdict": verdict,
                    "measured": True,
                }
            else:
                verdict = "not_comparable"
        created = self._now().isoformat()
        result: dict[str, Any] = {
            "ok": True,
            "phase": PI24_PHASE,
            "evaluation_id": stable_id(
                "eieval_", instrument, horizon, as_of.isoformat(), len(records)
            ),
            "created_at": created,
            "instrument_id": instrument,
            "horizon": horizon,
            "as_of": as_of.isoformat(),
            "status": "completed" if sufficient else "insufficient_data",
            "sample_sufficient": sufficient,
            "improvement_claimed": verdict == "event_aware_better",
            "verdict": verdict,
            "minimum_samples": min_samples,
            "samples": {
                "total": len(records),
                "event_aware": len(event_aware),
                "non_event_aware": len(non_event),
                "rejected": rejected,
                "label_basis": dict(basis),
            },
            "cohorts": {"event_aware": aware_block, "non_event_aware": baseline_block},
            "comparison": comparison,
            "method": (
                "PI-11 collected records scored by PI-11 return, price and direction "
                "metrics; cohorts split by declared event awareness or by a stored "
                "event published inside the point-in-time lookback window."
            ),
            "label_policy": (
                "A record is event-labelled when metadata.event_aware is true, "
                "metadata.event_ids is non-empty, the feature-set version names "
                "events, or a stored event was published within lookback_days "
                "before the forecast was generated."
            ),
            "causation_established": False,
            "correlation_only": True,
            "policy_version": EVALUATION_POLICY_VERSION,
            "limitations": [
                "This is a measured error comparison on stored records, not a causal "
                "claim about the events.",
                "A cohort below the minimum sample size yields insufficient_data, "
                "never a claimed improvement.",
                "Event proximity labels context availability, not that the forecast "
                "model consumed the event features.",
            ],
            "errors": self.errors,
        }
        if not sufficient:
            result["reasons"] = [
                (
                    f"event-aware cohort has {len(event_aware)} records; "
                    f"{min_samples} are required"
                ),
                (
                    f"non-event-aware cohort has {len(non_event)} records; "
                    f"{min_samples} are required"
                ),
            ]
        self._store.save(
            "forecast_evaluation",
            result,
            record_id=result["evaluation_id"],
            instrument_id=instrument,
            horizon=horizon,
            status=result["status"],
        )
        return result

    def timeline(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = _mapping(payload)
        event_id = clean(body.get("event_id"), 120)
        if event_id:
            history = _mapping(self._call_sources("event_history", event_id=event_id) or {})
            detail = self.event(event_id)
            versions = [
                {
                    "event_id": event_id,
                    "version": int(number(row.get("version")) or 1),
                    "status": clean(row.get("status"), 40),
                    "verification": clean(row.get("verification"), 40),
                    "published_at": row.get("published_at"),
                    "event_date": row.get("occurred_at") or row.get("published_at"),
                }
                for row in history.get("versions") or []
                if isinstance(row, Mapping)
            ]
            return {
                "ok": True,
                "phase": PI24_PHASE,
                "event_id": event_id,
                "timeline": sorted(
                    versions, key=lambda row: (row["event_date"] or "", row["version"])
                ),
                "transitions": list(history.get("transitions") or detail.get("transitions") or []),
                "event": detail["event"],
            }
        instrument = (
            canonical_instrument(body.get("instrument_id")) if body.get("instrument_id") else ""
        )
        horizon = canonical_horizon(body.get("horizon") or "5d")
        events = (
            self._relevant_events(instrument, horizon, self._now())
            if instrument
            else self.events(limit=200)["events"]
        )
        return {
            "ok": True,
            "phase": PI24_PHASE,
            "instrument_id": instrument,
            "timeline": [
                {
                    "event_id": event["event_id"],
                    "title": event.get("title"),
                    "event_date": event.get("event_date"),
                    "published_at": event.get("published_at"),
                    "status": event.get("status"),
                    "verification": event.get("verification"),
                    "event_group": event.get("event_group"),
                    "relevance": (event.get("relevance") or {}).get("relevance"),
                }
                for event in sorted(events, key=lambda row: row.get("event_date") or "")
            ],
            "total": len(events),
        }

    def agent_evidence(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = _mapping(payload)
        instrument = canonical_instrument(body.get("instrument_id"))
        horizon = canonical_horizon(body.get("horizon") or "5d")
        as_of = parse_time(body.get("as_of"), "as_of") or self._now()
        events = self._relevant_events(instrument, horizon, as_of)
        warnings = self.warnings(
            {"instrument_id": instrument, "horizon": horizon, "as_of": as_of.isoformat()},
            persist=False,
        )
        verified = [event for event in events if event["status"] == EventStatus.VERIFIED.value]
        unverified = [
            event
            for event in events
            if event["status"] in {EventStatus.UNVERIFIED.value, EventStatus.CONFLICTED.value}
        ]
        source_refs = [
            source
            for event in events
            for source in event.get("source_references") or []
            if isinstance(source, Mapping)
        ]
        portfolio_id = clean(body.get("portfolio_id"), 160)
        portfolio = self.portfolio_context(portfolio_id, horizon=horizon) if portfolio_id else None
        roles = {
            "fin-market-research": {"events": [event["event_id"] for event in events]},
            "fin-fundamental": {
                "events": [
                    event["event_id"]
                    for event in events
                    if event.get("event_group") in _CORPORATE_GROUPS
                ]
            },
            "fin-technical": {
                "events": [
                    event["event_id"]
                    for event in events
                    if event.get("event_group") == EventGroup.MARKET.value
                ]
            },
            "fin-prediction": {
                "events": [event["event_id"] for event in events],
                "warnings": warnings["warning_id"],
            },
            "fin-regime": {
                "events": [
                    event["event_id"]
                    for event in events
                    if (event.get("relevance") or {}).get("relevance")
                    == RelevanceClass.MARKET_WIDE.value
                ]
            },
            "fin-risk": {
                "events": [event["event_id"] for event in events],
                "warning_status": warnings["status"],
                "warnings": [item["code"] for item in warnings["warnings"]],
            },
            "fin-portfolio": {
                "instrument_ids": [instrument],
                "portfolio_status": (portfolio or {}).get("status"),
            },
            "fin-rl-policy": {
                "events": [
                    event["event_id"]
                    for event in events
                    if event.get("event_group") in _MARKET_GROUPS
                ],
                "advisory_only": True,
            },
        }
        return {
            "ok": True,
            "phase": PI24_PHASE,
            "created_at": self._now().isoformat(),
            "instrument_id": instrument,
            "horizon": horizon,
            "as_of": as_of.isoformat(),
            "verified_events": verified,
            "unverified_events": unverified,
            "conflicting_reports": warnings["status"] == WarningStatus.EVENT_UNCERTAINTY.value,
            "affected_instruments": sorted(
                {item for event in events for item in event.get("instruments") or []}
            ),
            "horizon_relevance": HORIZONS[horizon]["group"],
            "uncertainty": warnings["status"],
            "supporting_sources": source_refs,
            "portfolio": portfolio,
            "roles": roles,
            "unverified_presented_as_fact": False,
            "execution_authority": False,
            "trading_decision_authority_unchanged": True,
            "errors": self.errors,
        }

    def portfolio_context(self, portfolio_id: str, *, horizon: str = "5d") -> dict[str, Any]:
        portfolio = clean(portfolio_id, 160)
        if not portfolio:
            raise EventIntelligenceValidationError("a portfolio id is required")
        horizon = canonical_horizon(horizon)
        detail = _mapping(self._call_sources("portfolio", portfolio_id=portfolio) or {})
        holdings = [row for row in detail.get("holdings") or [] if isinstance(row, Mapping)][
            : self._config.max_instruments
        ]
        positions: list[dict[str, Any]] = []
        warning_rows: list[dict[str, Any]] = []
        for holding in holdings:
            instrument_id = clean(holding.get("instrument_id"), 80)
            if not instrument_id:
                continue
            try:
                warning = self.warnings(
                    {"instrument_id": instrument_id, "horizon": horizon},
                    persist=False,
                )
            except Exception as exc:  # noqa: BLE001 - one holding cannot fail the book
                warning_rows.append(
                    {
                        "code": "event_evidence_unavailable",
                        "instrument_id": instrument_id,
                        "explanation": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            positions.append(
                {
                    "instrument_id": instrument_id,
                    "status": warning["status"],
                    "warning_id": warning["warning_id"],
                    "relevant_events": warning["relevant_events"],
                }
            )
            for item in warning["warnings"]:
                warning_rows.append({**item, "instrument_id": instrument_id})
        severe = [
            item for item in warning_rows if item.get("severity") in {"high", "critical", "warning"}
        ]
        return {
            "ok": True,
            "phase": PI24_PHASE,
            "portfolio_id": portfolio,
            "horizon": horizon,
            "status": WarningStatus.EVENT_VOLATILITY_RISK.value
            if severe
            else WarningStatus.EVENT_UNCERTAINTY.value
            if any(item.get("code") == "event_conflicting_reports" for item in warning_rows)
            else WarningStatus.UNVERIFIED_EVENT.value
            if any(item.get("code") == "event_unverified" for item in warning_rows)
            else WarningStatus.NORMAL.value,
            "positions": positions,
            "warnings": warning_rows,
            "risk_authority": RISK_AUTHORITY,
            "creates_risk_decision": False,
            "financial_mutation": False,
        }

    def analyze(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = _mapping(payload)
        instrument = canonical_instrument(body.get("instrument_id"))
        horizon = canonical_horizon(body.get("horizon") or "5d")
        relevance = self.assess_relevance(
            {"instrument_id": instrument, "horizon": horizon, "as_of": body.get("as_of")}
        )
        features = self.features(
            {"instrument_id": instrument, "horizon": horizon, "as_of": body.get("as_of")}
        )
        warnings = self.warnings(
            {"instrument_id": instrument, "horizon": horizon, "as_of": body.get("as_of")},
            persist=False,
        )
        context = self.forecasts(
            {"instrument_id": instrument, "horizon": horizon, "as_of": body.get("as_of")}
        )
        agents = self.agent_evidence(
            {"instrument_id": instrument, "horizon": horizon, "as_of": body.get("as_of")}
        )
        result: dict[str, Any] = {
            "ok": True,
            "phase": PI24_PHASE,
            "analysis_id": stable_id(
                "eian_",
                instrument,
                horizon,
                digest([relevance["assessment_id"], features["snapshot_id"]]),
            ),
            "created_at": self._now().isoformat(),
            "instrument_id": instrument,
            "horizon": horizon,
            "results": {
                "relevance": relevance,
                "features": features,
                "warnings": warnings,
                "forecast_context": context,
                "agent_evidence": agents,
            },
            "direction": "not_estimated",
            "price_adjustment": "none",
            "risk_authority": RISK_AUTHORITY,
            "automatic_promotion": False,
            "financial_mutation": False,
            "errors": self.errors,
        }
        self._store.save(
            "analysis",
            result,
            record_id=result["analysis_id"],
            instrument_id=instrument,
            horizon=horizon,
            status="completed",
        )
        return result

    def overview(self, *, instrument_id: str = "", horizon: str = "5d") -> dict[str, Any]:
        horizon = canonical_horizon(horizon)
        instrument = canonical_instrument(instrument_id) if instrument_id else ""
        listing = self.events(instrument_id=instrument, limit=200)
        events = list(listing.get("events") or [])
        groups: dict[str, int] = {}
        statuses: dict[str, int] = {}
        for event in events:
            groups[event["event_group"]] = groups.get(event["event_group"], 0) + 1
            statuses[event["status"]] = statuses.get(event["status"], 0) + 1
        warning = (
            self.warnings({"instrument_id": instrument, "horizon": horizon}, persist=False)
            if instrument
            else None
        )
        return {
            "ok": True,
            "phase": PI24_PHASE,
            "instrument_id": instrument,
            "horizon": horizon,
            "events": events[:50],
            "total": listing["total"],
            "counts": {"by_group": groups, "by_status": statuses},
            "verified": statuses.get(EventStatus.VERIFIED.value, 0),
            "unverified": statuses.get(EventStatus.UNVERIFIED.value, 0),
            "conflicted": statuses.get(EventStatus.CONFLICTED.value, 0),
            "warning": warning,
            "direction": "not_estimated",
            "sources": ["NM-04 event radar"],
            "errors": self.errors,
        }

    def history(
        self,
        *,
        kind: str = "",
        event_id: str = "",
        instrument_id: str = "",
        horizon: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        rows = self._store.list(
            kind or None,
            event_id=event_id,
            instrument_id=instrument_id,
            horizon=horizon,
            limit=limit,
            offset=offset,
        )
        return {
            "ok": True,
            "records": rows,
            "total": self._store.total(
                kind or None,
                event_id=event_id,
                instrument_id=instrument_id,
                horizon=horizon,
            ),
        }

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": PI24_PHASE,
            "store": self._store.integrity(),
            "counts": self._store.counts(),
            "config": self._config.to_dict(),
            "errors": self.errors,
        }

    def status(self) -> dict[str, Any]:
        counts = self._store.counts()
        return {
            "ok": True,
            "phase": PI24_PHASE,
            "version": PI24_VERSION,
            "implemented": True,
            "contract_version": PI24_CONTRACT_VERSION,
            "initialised": True,
            "counts": counts,
            "latest": {
                "relevance": self._store.latest("relevance"),
                "warning": self._store.latest("warning"),
                "feature_snapshot": self._store.latest("feature_snapshot"),
                "outcome": self._store.latest("outcome"),
            },
            "errors": self.errors,
        }

    def contract(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": PI24_PHASE,
            "version": PI24_VERSION,
            "implemented": True,
            "contract_version": PI24_CONTRACT_VERSION,
            "scope": (
                "Advisory event intelligence over NM-04 normalized event evidence and "
                "PI-19 research context; no feed, forecast or execution authority."
            ),
            "event_groups": [group.value for group in EventGroup],
            "event_statuses": [status.value for status in EventStatus],
            "relevance_classes": [item.value for item in RelevanceClass],
            "warning_statuses": [item.value for item in WarningStatus],
            "feature_version": FEATURE_VERSION,
            "relevance_policy_version": RELEVANCE_POLICY_VERSION,
            "warning_policy_version": WARNING_POLICY_VERSION,
            "sources": {
                "events": "NM-04 event radar",
                "research": "PI-19 financial research",
                "market_data": "NM-02 stored bars",
                "forecasts": "PI-05 through PI-11 and PI-23",
            },
            "authority": {
                "risk_authority": RISK_AUTHORITY,
                "execution_authority": "NM-10",
                "creates_risk_decision": False,
                "submits_orders": False,
                "changes_forecast": False,
                "changes_model": False,
                "financial_mutation": False,
            },
            "direction": "not_estimated",
            "sentiment_to_price_prediction": False,
            "new_feed_added": False,
            "limitations": [
                "Sentiment is never converted into a price direction.",
                "Event relevance is categorical and event outcome studies are correlational.",
                "Only sources already configured in NM-04 are ingested.",
            ],
        }

    def navigation(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": PI24_PHASE,
            "route": "/market/event-intelligence",
            "views": [
                {"id": identifier, "label": label, "description": description}
                for identifier, label, description in _VIEWS
            ],
            "authority": self.contract()["authority"],
        }

    def config_view(self) -> dict[str, Any]:
        return {"ok": True, "config": self._config.to_dict()}

    def update_config(self, payload: Mapping[str, Any], *, actor: str = "") -> dict[str, Any]:
        config = EventIntelligenceConfig.from_mapping(payload)
        self._config = config
        self._store.save_config(config)
        return {
            "ok": True,
            "config": config.to_dict(),
            "actor": clean(actor, 120),
            "automatic_promotion": False,
        }


__all__ = ["EventIntelligenceService"]
