"""The NM-04 radar: discover, extract, verify, classify, map, persist, publish.

One pipeline, in that order, per source entry:

```
feed entry -> extraction -> dedupe -> verification -> classification -> mapping
           -> store (append-only version) -> publish
```

The engine owns the sequencing and nothing else: extraction rules live in
``extraction``, verification in ``verification``, scoring in ``classification``,
identity in ``dedupe`` and storage in ``store``. A behaviour change belongs in the
one module that owns it.

Guarantees this class is responsible for:

* **Failure isolation.** One source (or one entry) failing never stops the others;
  the failure is recorded on the source and counted, not swallowed.
* **Publish after commit.** Events are emitted only once the version and its
  evidence are stored, so a subscriber can never see an event that is not durable.
* **No silent ingestion success.** A refresh reports ``unavailable`` with the
  reason; an empty result from a failed fetch is never reported as "no news".
* **Append-only history.** An update is a new version with ``supersedes`` set; the
  previous version and its evidence remain queryable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from novacontrol.core.events import EventType
from novacontrol.novamarket.contracts import (
    EventSourceError,
    EventStateError,
    EventValidationError,
)
from novacontrol.novamarket.radar import classification as classification_module
from novacontrol.novamarket.radar import dedupe as dedupe_module
from novacontrol.novamarket.radar import extraction as extraction_module
from novacontrol.novamarket.radar import mapping as mapping_module
from novacontrol.novamarket.radar import verification as verification_module
from novacontrol.novamarket.radar.models import (
    SUBCATEGORIES,
    SUBCATEGORY_INDEX,
    VERIFICATION_TRANSITIONS,
    EventCategory,
    UNCLASSIFIED,
    EventMapping,
    EventQuery,
    EventSourceDefinition,
    EventStatus,
    EvidenceRecord,
    EvidenceStatus,
    ExtractedEvent,
    Importance,
    MarketEvent,
    Relationship,
    SourceEntry,
    SourceHealth,
    VerificationDecision,
    VerificationState,
    clip,
    content_hash,
    event_id_for,
    event_key,
    expiry_for,
    normalize_text,
    utcnow,
)
from novacontrol.novamarket.radar.sources import (
    EventFeedClient,
    FeedResult,
    SourceCatalog,
)
from novacontrol.novamarket.radar.store import EventStore

_LOGGER = logging.getLogger(__name__)

#: A subject is the entity (or, without one, the headline's first significant
#: words). It is what dedupe compares, so it must be stable, not clever.
SUBJECT_WORDS = 8


class EventPublisher(Protocol):
    """The one seam events leave through (NM-01's bus, injected)."""

    async def publish(
        self, event_type: EventType | str, /, **payload: Any
    ) -> Any: ...


def entity_hints(
    instruments: Iterable[mapping_module.InstrumentView],
) -> tuple[extraction_module.EntityHint, ...]:
    hints: list[extraction_module.EntityHint] = []
    for instrument in instruments:
        aliases = tuple(
            value
            for value in (*instrument.aliases, instrument.symbol)
            if value and value != instrument.name
        )
        hints.append(extraction_module.EntityHint(instrument.name, aliases))
    return tuple(hints)


def subject_of(event: ExtractedEvent) -> str:
    """The comparison subject: named entities, else the headline's words."""
    if event.entity_names:
        return " | ".join(sorted(normalize_text(name) for name in event.entity_names))
    words: list[str] = []
    for raw in normalize_text(event.title).replace(":", " ").replace("|", " ").split():
        # Punctuation belongs to the sentence, not to the subject: "cent," and
        # "cent" are the same word, or two reports of one story look unrelated.
        word = raw.strip(".,;:!?()[]{}\"'")
        if len(word) <= 2:
            continue
        if word.replace(".", "").replace(",", "").replace("%", "").isdigit():
            continue
        words.append(word)
    words = words[:SUBJECT_WORDS]
    return " ".join(words) or normalize_text(event.title)


def independence_key_for(definition: EventSourceDefinition, url: str) -> str:
    """Two records from one publisher domain are one origin, not two."""
    host = ""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    host = host.removeprefix("www.")
    return host or definition.source_id


@dataclass
class SourceRefresh:
    """What one source refresh actually did."""

    source_id: str
    status: str
    entries_seen: int = 0
    created: int = 0
    updated: int = 0
    duplicates: int = 0
    unclassified: int = 0
    rejected: int = 0
    unchanged: bool = False
    error: str = ""
    error_code: str = ""
    fetched_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "status": self.status,
            "entries_seen": self.entries_seen,
            "events_created": self.created,
            "events_updated": self.updated,
            "duplicates": self.duplicates,
            "unclassified": self.unclassified,
            "rejected": self.rejected,
            "unchanged": self.unchanged,
            "error": self.error,
            "error_code": self.error_code,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }


@dataclass
class IngestReport:
    """Totals for one refresh run (one or many sources)."""

    results: list[SourceRefresh] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def count(self, attribute: str) -> int:
        return sum(int(getattr(result, attribute)) for result in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sources": [result.to_dict() for result in self.results],
            "sources_checked": len(self.results),
            "sources_ok": sum(1 for item in self.results if item.status == "ok"),
            "sources_failed": sum(
                1 for item in self.results if item.status == "error"
            ),
            "entries_seen": self.count("entries_seen"),
            "events_created": self.count("created"),
            "events_updated": self.count("updated"),
            "duplicates": self.count("duplicates"),
            "unclassified": self.count("unclassified"),
            "rejected": self.count("rejected"),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class MarketEventRadar:
    """Evidence-backed market event discovery for NovaMarket."""

    def __init__(
        self,
        store: EventStore,
        *,
        catalog: SourceCatalog,
        client: EventFeedClient | None = None,
        instruments: Callable[[], Sequence[mapping_module.InstrumentView]] | None = None,
        publisher: EventPublisher | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._client = client or EventFeedClient()
        self._instruments = instruments or (lambda: ())
        self._publisher = publisher
        self._clock = clock or utcnow
        self._verifier = verification_module.VerificationEngine()
        self._last_error = ""
        self._last_event_error = ""
        self._refreshes = 0
        self._last_refresh: datetime | None = None
        self._store.ensure_sources(
            self._catalog.ids, enabled=[item.source_id for item in self._catalog.enabled()]
        )

    # -- access ---------------------------------------------------------------

    @property
    def store(self) -> EventStore:
        return self._store

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def last_event_error(self) -> str:
        return self._last_event_error

    def set_publisher(self, publisher: EventPublisher | None) -> None:
        self._publisher = publisher

    def instruments(self) -> tuple[mapping_module.InstrumentView, ...]:
        return tuple(self._instruments())

    # -- sources --------------------------------------------------------------

    def sources(self) -> tuple[dict[str, Any], ...]:
        """The catalog merged with what was actually observed."""
        rows: list[dict[str, Any]] = []
        for definition in self._catalog.definitions():
            health = self._store.source(definition.source_id)
            body = definition.to_dict()
            body.update(
                {
                    "enabled": self._catalog.is_enabled(definition.source_id),
                    "status": health.status,
                    "last_attempt": health.last_attempt.isoformat()
                    if health.last_attempt
                    else None,
                    "last_success": health.last_success.isoformat()
                    if health.last_success
                    else None,
                    "consecutive_failures": health.consecutive_failures,
                    "entries_seen": health.entries_seen,
                    "entries_new": health.entries_new,
                    "last_error": health.last_error,
                    "last_error_code": health.last_error_code,
                    "unchanged": health.unchanged,
                }
            )
            rows.append(body)
        return tuple(rows)

    def set_source_enabled(self, source_id: str, enabled: bool) -> dict[str, Any]:
        definition = self._catalog.set_enabled(source_id, enabled)
        self._store.set_enabled(definition.source_id, enabled)
        _LOGGER.info(
            "novamarket.market_event.source_enabled source=%s enabled=%s",
            definition.source_id,
            enabled,
        )
        return {"source_id": definition.source_id, "enabled": enabled}

    # -- refresh --------------------------------------------------------------

    async def refresh(self, source_id: str | None = None) -> dict[str, Any]:
        """Fetch and ingest; returns what happened, never a bare ``ok``."""
        started = self._clock()
        targets = (
            (self._catalog.get(source_id),)
            if source_id
            else self._catalog.enabled()
        )
        report = IngestReport(started_at=started)
        for definition in targets:
            try:
                report.results.append(await self._refresh_one(definition))
            except Exception as exc:  # noqa: BLE001 - one source never stops another
                self._last_error = f"{definition.source_id}: {type(exc).__name__}: {exc}"
                _LOGGER.error(
                    "novamarket.market_event.source_failed source=%s error=%s",
                    definition.source_id,
                    type(exc).__name__,
                )
                self._store.record_attempt(
                    definition.source_id,
                    status="error",
                    error=str(exc),
                    error_code="source_error",
                )
                report.results.append(
                    SourceRefresh(
                        source_id=definition.source_id,
                        status="error",
                        error=str(exc),
                        error_code="source_error",
                    )
                )
        report.finished_at = self._clock()
        self._refreshes += 1
        self._last_refresh = report.finished_at
        return report.to_dict()

    async def _refresh_one(self, definition: EventSourceDefinition) -> SourceRefresh:
        health = self._store.source(definition.source_id)
        try:
            result: FeedResult = await self._client.fetch(
                definition, etag=health.etag
            )
        except EventSourceError as exc:
            self._last_error = f"{definition.source_id}: {exc.code}: {exc}"
            self._store.record_attempt(
                definition.source_id,
                status="error",
                error=str(exc),
                error_code=exc.code,
            )
            _LOGGER.warning(
                "novamarket.market_event.source_unavailable source=%s code=%s",
                definition.source_id,
                exc.code,
            )
            await self._publish(
                EventType.NOVAMARKET_MARKET_EVENT_SOURCE_UNAVAILABLE,
                source_id=definition.source_id,
                code=exc.code,
            )
            return SourceRefresh(
                source_id=definition.source_id,
                status="error",
                error=str(exc),
                error_code=exc.code,
            )
        if result.unchanged:
            self._store.record_attempt(
                definition.source_id, status="ok", unchanged=True, etag=result.etag
            )
            return SourceRefresh(
                source_id=definition.source_id,
                status="unchanged",
                unchanged=True,
                fetched_at=result.fetched_at,
            )

        counts = await self.ingest(
            definition, result.entries, fetched_at=result.fetched_at
        )
        self._store.record_attempt(
            definition.source_id,
            status="ok",
            etag=result.etag,
            entries_seen=len(result.entries),
            entries_new=counts["created"] + counts["updated"],
        )
        return SourceRefresh(
            source_id=definition.source_id,
            status="ok",
            entries_seen=len(result.entries),
            created=counts["created"],
            updated=counts["updated"],
            duplicates=counts["duplicates"],
            unclassified=counts["unclassified"],
            rejected=counts["rejected"],
            fetched_at=result.fetched_at,
        )

    async def ingest(
        self,
        definition: EventSourceDefinition,
        entries: Sequence[SourceEntry],
        *,
        fetched_at: datetime | None = None,
    ) -> dict[str, int]:
        """Run the pipeline for each entry; one bad entry cannot stop the rest."""
        counts = {"created": 0, "updated": 0, "duplicates": 0, "unclassified": 0, "rejected": 0}
        for entry in entries:
            try:
                outcome = await self._ingest_entry(
                    definition, entry, fetched_at or self._clock()
                )
            except (EventValidationError, EventStateError) as exc:
                counts["rejected"] += 1
                self._last_error = f"{definition.source_id}: {type(exc).__name__}: {exc}"
                _LOGGER.warning(
                    "novamarket.market_event.entry_rejected source=%s error=%s",
                    definition.source_id,
                    type(exc).__name__,
                )
                continue
            except Exception as exc:  # noqa: BLE001 - isolate a single bad entry
                counts["rejected"] += 1
                self._last_error = f"{definition.source_id}: {type(exc).__name__}: {exc}"
                _LOGGER.error(
                    "novamarket.market_event.entry_failed source=%s error=%s",
                    definition.source_id,
                    type(exc).__name__,
                )
                continue
            counts[outcome] += 1
        return counts

    async def _ingest_entry(
        self,
        definition: EventSourceDefinition,
        entry: SourceEntry,
        fetched_at: datetime,
    ) -> str:
        instruments = self.instruments()
        extraction = extraction_module.extract(
            entry,
            entities=entity_hints(instruments),
            publisher=definition.publisher,
        )
        if extraction.unclassified:
            _LOGGER.info(
                "novamarket.market_event.unclassified source=%s title=%s",
                definition.source_id,
                clip(entry.title, 80),
            )
            return "unclassified"

        event = extraction.event
        mappings = mapping_module.map_event(
            title=entry.title,
            summary=entry.summary,
            entity_names=event.entity_names,
            instruments=instruments,
            sector_relevance=event.category.value in {"sector", "macroeconomic"},
        )
        subject = subject_of(event)
        digest = entry.digest
        candidate = dedupe_module.Candidate(
            subject=subject,
            subcategory=event.subcategory,
            title=entry.title,
            digest=digest,
            instrument_ids=tuple(mapping.instrument_id for mapping in mappings),
            published_at=entry.published_at,
        )
        existing = tuple(
            dedupe_module.Existing(
                event_id=str(row["event_id"]),
                event_key=str(row["event_key"]),
                subject=str(row["subject"]),
                subcategory=str(row["subcategory"]),
                title=str(row["title"]),
                digest=str(row["digest"]),
                instrument_ids=tuple(row["instrument_ids"]),
                published_at=row["published_at"],
                version=int(row["version"]),
            )
            for row in self._store.candidates(subcategory=event.subcategory)
        )
        decision = dedupe_module.decide(
            candidate,
            existing,
            retracting=bool(verification_module.retraction_frame(
                f"{entry.title}. {entry.summary}"
            )
            ),
        )
        if decision.is_duplicate:
            _LOGGER.info(
                "novamarket.market_event.duplicate source=%s event=%s",
                definition.source_id,
                decision.event_id,
            )
            return "duplicates"

        now = self._clock()
        previous: MarketEvent | None = None
        prior_evidence: tuple[EvidenceRecord, ...] = ()
        if decision.is_update:
            previous = self._store.get_event(decision.event_id)
            prior_evidence = self._store.evidence_for(decision.event_id)
            version = previous.version + 1
            event_id = previous.event_id
            event_key_value = previous.event_key
            supersedes = previous.version
        else:
            version = 1
            event_key_value = event_key(
                subject=subject,
                subcategory=event.subcategory,
                published_at=entry.published_at,
            )
            event_id = event_id_for(event_key_value)
            supersedes = None

        evidence = EvidenceRecord(
            evidence_id=f"evd_{content_hash(event_id, definition.source_id, digest)[:20]}",
            event_id=event_id,
            source_id=definition.source_id,
            url=entry.url,
            publisher=definition.publisher,
            reliability=definition.reliability,
            title=entry.title,
            excerpt=event.description or entry.title,
            content_hash=digest,
            independence_key=independence_key_for(definition, entry.url),
            retrieved_at=fetched_at,
            published_at=entry.published_at,
            status=EvidenceStatus.RETRIEVED,
            reasons=extraction.notes,
        )
        all_evidence = tuple(prior_evidence) + (evidence,)
        decision_verification = self._verifier.decision_for(
            verification_module.VerificationInput(
                subject=subject,
                subcategory=event.subcategory,
                evidence=all_evidence,
                sources={
                    item.source_id: item for item in self._catalog.definitions()
                },
                current=previous.verification if previous is not None else None,
                published_at=entry.published_at,
                now=now,
            )
        )
        classification = classification_module.classify(
            event=event,
            verification=decision_verification,
            evidence=all_evidence,
            mappings=mappings,
            published_at=entry.published_at,
            now=now,
        )
        sectors = mapping_module.sectors_from(mappings)
        markets = mapping_module.markets_from(mappings, instruments)
        market = markets[0] if markets else definition.market
        status = (
            EventStatus.RETRACTED
            if decision_verification.state is VerificationState.RETRACTED
            else EventStatus.ACTIVE
        )
        transitions = self._transitions_for(previous, status, decision_verification)
        stored = MarketEvent(
            event_id=event_id,
            event_key=event_key_value,
            version=version,
            title=entry.title,
            description=event.description,
            category=event.category,
            subcategory=event.subcategory,
            status=status,
            verification=decision_verification.state,
            importance=classification.importance,
            confidence=classification.confidence,
            discovered_at=now,
            retrieved_at=fetched_at,
            updated_at=now,
            published_at=entry.published_at,
            occurred_at=event.occurred_at,
            market=market,
            instruments=tuple(mapping.instrument_id for mapping in mappings),
            sectors=sectors,
            evidence_count=len(all_evidence),
            independent_sources=decision_verification.independent_sources,
            fingerprint=content_hash(event_id, subject, event.subcategory),
            classification=classification,
            verification_detail=decision_verification,
            mappings=mappings,
            supersedes=supersedes,
            expiry_days=expiry_for(event.subcategory),
        )
        self._store.insert_event(
            stored,
            evidence=(evidence,),
            mappings=mappings,
            transitions=transitions,
            subject=subject,
            digest=digest,
        )
        _LOGGER.info(
            "novamarket.market_event.stored event=%s version=%d verification=%s "
            "importance=%s source=%s",
            event_id,
            version,
            decision_verification.state.value,
            classification.importance.value,
            definition.source_id,
        )
        await self._publish_series(
            stored, decision, decision_verification, source_id=definition.source_id
        )
        return "updated" if decision.is_update else "created"

    def _transitions_for(
        self,
        previous: MarketEvent | None,
        status: EventStatus,
        decision: VerificationDecision,
    ) -> list[tuple[str, str, str, str, str]]:
        if previous is None:
            path = [
                EventStatus.DISCOVERED,
                EventStatus.EXTRACTED,
                EventStatus.NORMALIZED,
                EventStatus.VERIFICATION,
                EventStatus.CLASSIFIED,
                EventStatus.ACTIVE,
            ]
            if status is EventStatus.RETRACTED:
                path.append(EventStatus.RETRACTED)
            rows = [
                (path[index].value, path[index + 1].value, "", "", "pipeline")
                for index in range(len(path) - 1)
            ]
            rows.append(("", "", "", decision.state.value, "verification"))
            return rows
        rows = [
            (
                previous.status.value,
                EventStatus.UPDATED.value,
                "",
                "",
                "a further report of this event",
            ),
            (
                EventStatus.UPDATED.value,
                status.value,
                "",
                decision.state.value,
                "new evidence re-evaluated the event",
            ),
        ]
        return rows

    async def _publish_series(
        self,
        event: MarketEvent,
        decision: dedupe_module.Decision,
        verification: VerificationDecision,
        *,
        source_id: str,
    ) -> None:
        if decision.creates_event:
            await self._publish(
                EventType.NOVAMARKET_MARKET_EVENT_DISCOVERED,
                event_id=event.event_id,
                source_id=source_id,
            )
        await self._publish(
            EventType.NOVAMARKET_MARKET_EVENT_EXTRACTED,
            event_id=event.event_id,
            category=event.category.value,
        )
        await self._publish(
            EventType.NOVAMARKET_MARKET_EVENT_NORMALIZED,
            event_id=event.event_id,
            instruments=",".join(event.instruments),
        )
        await self._publish(
            EventType.NOVAMARKET_MARKET_EVENT_VERIFIED,
            event_id=event.event_id,
            state=event.verification.value,
        )
        await self._publish(
            EventType.NOVAMARKET_MARKET_EVENT_CLASSIFIED,
            event_id=event.event_id,
            importance=event.importance.value,
        )
        if decision.is_update:
            await self._publish(
                EventType.NOVAMARKET_MARKET_EVENT_UPDATED,
                event_id=event.event_id,
                version=str(event.version),
            )
        if event.verification is VerificationState.RETRACTED:
            await self._publish(
                EventType.NOVAMARKET_MARKET_EVENT_RETRACTED,
                event_id=event.event_id,
                reason="; ".join(verification.reasons) or "retracted by its source",
            )
        if verification.conflicts:
            await self._publish(
                EventType.NOVAMARKET_MARKET_EVENT_EVIDENCE_CONFLICT,
                event_id=event.event_id,
                detail="; ".join(verification.conflicts),
            )

    async def _publish(self, event_type: EventType, **payload: Any) -> None:
        if self._publisher is None:
            return
        try:
            await self._publisher.publish(event_type, **payload)
        except Exception as exc:  # noqa: BLE001 - a stored event stands
            self._last_event_error = f"{type(exc).__name__}: {exc}"
            _LOGGER.warning(
                "novamarket.market_event.event_not_published name=%s error=%s",
                event_type,
                type(exc).__name__,
            )

    # -- expiry ---------------------------------------------------------------

    async def expire_due(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Expire events past their category's review window; keep the history."""
        moment = now or self._clock()
        expired: list[dict[str, Any]] = []
        for event in self._store.due_for_expiry(now=moment):
            if event.verification is VerificationState.EXPIRED:
                continue
            updated = MarketEvent(
                event_id=event.event_id,
                event_key=event.event_key,
                version=event.version + 1,
                title=event.title,
                description=event.description,
                category=event.category,
                subcategory=event.subcategory,
                status=EventStatus.EXPIRED,
                verification=VerificationState.EXPIRED,
                importance=event.importance,
                confidence=event.confidence,
                discovered_at=event.discovered_at,
                retrieved_at=event.retrieved_at,
                updated_at=moment,
                published_at=event.published_at,
                occurred_at=event.occurred_at,
                market=event.market,
                instruments=event.instruments,
                sectors=event.sectors,
                evidence_count=event.evidence_count,
                independent_sources=event.independent_sources,
                fingerprint=event.fingerprint,
                classification=event.classification,
                verification_detail=event.verification_detail,
                mappings=event.mappings,
                supersedes=event.version,
                expiry_days=event.expiry_days,
            )
            self._store.insert_event(
                updated,
                transitions=[
                    (
                        event.status.value,
                        EventStatus.EXPIRED.value,
                        event.verification.value,
                        VerificationState.EXPIRED.value,
                        "outside the category review window",
                    )
                ],
                subject=event.event_key,
                digest="",
            )
            await self._publish(
                EventType.NOVAMARKET_MARKET_EVENT_EXPIRED,
                event_id=event.event_id,
                version=str(updated.version),
            )
            expired.append({"event_id": event.event_id, "version": updated.version})
        return {"expired": expired, "count": len(expired), "at": moment.isoformat()}

    # -- reads ----------------------------------------------------------------

    def search(self, query: EventQuery | None = None, **filters: Any) -> dict[str, Any]:
        resolved = query or EventQuery(**filters)
        events, total = self._store.search(resolved)
        return {
            "ok": True,
            "events": [event.to_dict() for event in events],
            "total": total,
            "limit": resolved.limit,
            "offset": resolved.offset,
        }

    def event(self, event_id: str, *, version: int | None = None) -> dict[str, Any]:
        event = self._store.get_event(event_id, version=version)
        body = event.to_dict()
        body["ok"] = True
        body["evidence"] = [
            record.to_dict()
            for record in self._store.evidence_for(event_id, version=event.version)
        ]
        body["transitions"] = list(self._store.transitions(event_id))
        return body

    def evidence(self, event_id: str, *, version: int | None = None) -> dict[str, Any]:
        self._store.get_event(event_id, version=version)
        records = self._store.evidence_for(event_id, version=version)
        return {
            "ok": True,
            "event_id": event_id,
            "evidence": [record.to_dict() for record in records],
            "total": len(records),
        }

    def history(self, event_id: str) -> dict[str, Any]:
        self._store.get_event(event_id)
        versions = self._store.history(event_id)
        return {
            "ok": True,
            "event_id": event_id,
            "versions": [event.to_dict(include_history=True) for event in versions],
            "total": len(versions),
            "transitions": list(self._store.transitions(event_id)),
        }

    def instrument_events(self, instrument_id: str, *, limit: int = 50) -> dict[str, Any]:
        query = EventQuery(instrument_id=instrument_id, limit=limit)
        return self.search(query)

    # -- observability --------------------------------------------------------

    def metrics(self) -> dict[str, Any]:
        body = dict(self._store.metrics())
        body["refreshes"] = self._refreshes
        body["last_refresh"] = self._last_refresh.isoformat() if self._last_refresh else None
        health = {row["source_id"]: row for row in self.sources()}
        body["sources_total"] = len(health)
        body["sources_enabled"] = sum(1 for item in health.values() if item["enabled"])
        body["sources_ok"] = sum(1 for item in health.values() if item["status"] == "ok")
        body["sources_failing"] = sum(1 for item in health.values() if item["status"] == "error")
        body["sources_never_run"] = sum(
            1 for item in health.values() if item["status"] == "never_run"
        )
        return body

    def status(self) -> dict[str, Any]:
        return {
            "initialised": True,
            "repository": self._store.path,
            "schema_version": self._store.schema_version,
            "health": self._store.health(),
            "metrics": self.metrics(),
            "last_error": self._last_error,
            "last_event_error": self._last_event_error,
        }

    def contract(self) -> dict[str, Any]:
        return {
            "implemented": True,
            "phase": "NM-04",
            "categories": [
                {
                    "category": category.value,
                    "subcategories": [
                        item.id for item in SUBCATEGORIES if item.category is category
                    ],
                }
                for category in EventCategory
            ],
            "verification_states": {
                state.value: verification_module.describe_state(state)
                for state in VerificationState
            },
            "verification_transitions": {
                state.value: sorted(item.value for item in targets)
                for state, targets in VERIFICATION_TRANSITIONS.items()
            },
            "importance": {
                "bands": {
                    "critical": 75.0,
                    "high": 55.0,
                    "moderate": 35.0,
                    "low": 0.0,
                },
                "weights": dict(classification_module.FACTOR_WEIGHTS),
                "is_price_prediction": False,
            },
            "dedupe": {
                "update_similarity": dedupe_module.UPDATE_SIMILARITY,
                "distinct_similarity": dedupe_module.DISTINCT_SIMILARITY,
                "window_hours": dedupe_module.DEFAULT_WINDOW_HOURS,
                "same_title_is_not_enough": True,
            },
            "timestamps": ["occurred_at", "published_at", "retrieved_at", "discovered_at"],
            "sources": {
                "implemented": len(self._catalog),
                "enabled": len(self._catalog.enabled()),
                "feed_formats": ["rss", "atom", "json"],
            },
            "limits": [
                "No price prediction, price target, expected return or trading recommendation exists.",
                "Only the declared feeds are fetched; nothing is scraped beyond them.",
                "Unclassified entries are stored as counts, not as market events.",
                "No NM-05+ behaviour: no watchlists, forecasts, portfolios or execution.",
            ],
        }

    def close(self) -> None:
        self._store.close()


__all__ = [
    "EventPublisher",
    "IngestReport",
    "MarketEventRadar",
    "SourceRefresh",
    "entity_hints",
    "independence_key_for",
    "subject_of",
]
