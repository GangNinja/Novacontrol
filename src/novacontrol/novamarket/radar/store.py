"""The event radar's durable store.

It shares NM-02's ``novamarket.sqlite3`` and does **not** touch
``PRAGMA user_version`` (NM-02 owns that in this file), so its schema version
lives in ``radar_meta`` — the same arrangement NM-03 uses with ``wallet_meta``.
A newer schema is refused rather than read optimistically.

Everything historical is append-only:

* an event **version** is a new row; the previous version is never edited;
* evidence rows are immutable;
* instrument mappings are per version and immutable;
* lifecycle and verification moves are appended to ``market_event_transitions``.

SQLite triggers abort ``UPDATE``/``DELETE`` on those four tables, so "history is
preserved" is enforced by the database and not only by this module's discipline.
Source health is the one mutable table: it is an observation about a feed, not a
market record.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from novacontrol.novamarket.contracts import (
    EventNotFoundError,
    EventStateError,
    EventStoreError,
    EventValidationError,
)
from novacontrol.novamarket.radar.models import (
    Classification,
    EventCategory,
    EventMapping,
    EventQuery,
    EventStatus,
    EvidenceRecord,
    EvidenceStatus,
    Factor,
    Importance,
    MarketEvent,
    Relationship,
    SourceHealth,
    SourceReliability,
    VerificationDecision,
    VerificationState,
    _aware,
    clip,
)

RADAR_SCHEMA_VERSION = 1

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS radar_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_sources (
    source_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'never_run',
    last_attempt TEXT,
    last_success TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    entries_seen INTEGER NOT NULL DEFAULT 0,
    entries_new INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    last_error_code TEXT NOT NULL DEFAULT '',
    etag TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    CHECK (enabled IN (0, 1))
);

CREATE TABLE IF NOT EXISTS market_events (
    event_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    event_key TEXT NOT NULL,
    subject TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL,
    subcategory TEXT NOT NULL,
    status TEXT NOT NULL,
    verification TEXT NOT NULL,
    importance TEXT NOT NULL,
    confidence REAL NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    classification TEXT NOT NULL DEFAULT '{}',
    verification_detail TEXT NOT NULL DEFAULT '{}',
    occurred_at TEXT,
    published_at TEXT,
    discovered_at TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    market TEXT NOT NULL DEFAULT 'GLOBAL',
    instruments TEXT NOT NULL DEFAULT '[]',
    sectors TEXT NOT NULL DEFAULT '[]',
    evidence_count INTEGER NOT NULL DEFAULT 0,
    independent_sources INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT NOT NULL DEFAULT '',
    digest TEXT NOT NULL DEFAULT '',
    supersedes INTEGER,
    expiry_days INTEGER NOT NULL DEFAULT 30,
    PRIMARY KEY (event_id, version),
    CHECK (version >= 1),
    CHECK (confidence >= 0 AND confidence <= 1),
    CHECK (category IN ('company', 'macroeconomic', 'sector', 'market_structure', 'unclassified')),
    CHECK (verification IN ('unverified', 'single_source', 'multi_source_corroborated', 'officially_confirmed', 'conflicting_evidence', 'retracted', 'expired')),
    CHECK (importance IN ('low', 'moderate', 'high', 'critical'))
);

CREATE INDEX IF NOT EXISTS ix_market_events_key ON market_events(event_key, version DESC);
CREATE INDEX IF NOT EXISTS ix_market_events_published ON market_events(published_at);
CREATE INDEX IF NOT EXISTS ix_market_events_category ON market_events(category, subcategory);
CREATE INDEX IF NOT EXISTS ix_market_events_verification ON market_events(verification);
CREATE INDEX IF NOT EXISTS ix_market_events_importance ON market_events(importance);
CREATE INDEX IF NOT EXISTS ix_market_events_status ON market_events(status);

CREATE TABLE IF NOT EXISTS market_event_evidence (
    evidence_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    event_version INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    publisher TEXT NOT NULL DEFAULT '',
    reliability TEXT NOT NULL,
    title TEXT NOT NULL,
    excerpt TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    independence_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'retrieved',
    reasons TEXT NOT NULL DEFAULT '[]',
    published_at TEXT,
    retrieved_at TEXT NOT NULL,
    CHECK (status IN ('retrieved', 'unavailable', 'malformed', 'rejected'))
);

CREATE INDEX IF NOT EXISTS ix_event_evidence_event ON market_event_evidence(event_id, event_version);
CREATE INDEX IF NOT EXISTS ix_event_evidence_source ON market_event_evidence(source_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_event_evidence_digest
    ON market_event_evidence(event_id, source_id, content_hash);

CREATE TABLE IF NOT EXISTS market_event_mappings (
    event_id TEXT NOT NULL,
    event_version INTEGER NOT NULL,
    instrument_id TEXT NOT NULL,
    relationship TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    sector TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (event_id, event_version, instrument_id),
    CHECK (relationship IN ('direct', 'indirect', 'uncertain'))
);

CREATE INDEX IF NOT EXISTS ix_event_mappings_instrument
    ON market_event_mappings(instrument_id, event_id);

CREATE TABLE IF NOT EXISTS market_event_transitions (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    verification_from TEXT NOT NULL DEFAULT '',
    verification_to TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT 'radar',
    at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_event_transitions_event
    ON market_event_transitions(event_id, version);

CREATE TRIGGER IF NOT EXISTS trg_market_events_no_update
BEFORE UPDATE ON market_events
BEGIN
    SELECT RAISE(ABORT, 'market event versions are append-only; store a new version');
END;

CREATE TRIGGER IF NOT EXISTS trg_market_events_no_delete
BEFORE DELETE ON market_events
BEGIN
    SELECT RAISE(ABORT, 'market event history is never deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_event_evidence_no_update
BEFORE UPDATE ON market_event_evidence
BEGIN
    SELECT RAISE(ABORT, 'event evidence is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_event_evidence_no_delete
BEFORE DELETE ON market_event_evidence
BEGIN
    SELECT RAISE(ABORT, 'event evidence is never deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_event_mappings_no_update
BEFORE UPDATE ON market_event_mappings
BEGIN
    SELECT RAISE(ABORT, 'event mappings are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_event_mappings_no_delete
BEFORE DELETE ON market_event_mappings
BEGIN
    SELECT RAISE(ABORT, 'event mappings are never deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_event_transitions_no_update
BEFORE UPDATE ON market_event_transitions
BEGIN
    SELECT RAISE(ABORT, 'event transitions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_event_transitions_no_delete
BEFORE DELETE ON market_event_transitions
BEGIN
    SELECT RAISE(ABORT, 'event transitions are history, never deleted');
END;
"""


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _moment(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    return _aware(datetime.fromisoformat(str(value)), field_name="stored timestamp")


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class EventStore:
    """SQLite-backed event versions, evidence, mappings and transitions."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        clock: Any | None = None,
    ) -> None:
        self._path = str(path)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        try:
            # isolation_level=None puts sqlite3 in autocommit mode, so the ONLY
            # transactions are the explicit BEGIN IMMEDIATE below. Python's
            # default implicit transaction made that BEGIN fail with "cannot
            # start a transaction within a transaction".
            self._connection = sqlite3.connect(
                self._path, check_same_thread=False, timeout=5.0, isolation_level=None
            )
        except sqlite3.Error as exc:  # pragma: no cover - host dependent
            raise EventStoreError(f"cannot open the event store: {exc}") from exc
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=5000")
            if self._path != ":memory:":
                self._connection.execute("PRAGMA journal_mode=WAL")
            self._ensure_schema()
        except Exception:
            # A refused schema must not keep the file locked (Windows keeps the
            # handle alive until the connection is closed).
            self._connection.close()
            raise

    # -- lifecycle ------------------------------------------------------------

    @property
    def path(self) -> str:
        return self._path

    def close(self) -> None:
        with self._lock:
            try:
                self._connection.close()
            except sqlite3.Error:  # pragma: no cover - closing twice
                pass

    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _write(self) -> Iterator[None]:
        """One write transaction; a nested call joins the outer one."""
        with self._lock:
            nested = bool(self._connection.in_transaction)
            if not nested:
                try:
                    self._connection.execute("BEGIN IMMEDIATE")
                except sqlite3.Error as exc:
                    raise EventStoreError(f"cannot begin a write: {exc}") from exc
            try:
                yield
            except Exception:
                if not nested:
                    self._connection.rollback()
                raise
            else:
                if not nested:
                    self._connection.commit()

    def _ensure_schema(self) -> None:
        with self._lock:
            tables = {
                str(row[0])
                for row in self._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "radar_meta" not in tables:
                self._connection.executescript(_SCHEMA_V1)
                self._connection.execute(
                    "INSERT OR REPLACE INTO radar_meta (key, value) VALUES (?, ?)",
                    ("schema_version", str(RADAR_SCHEMA_VERSION)),
                )
                self._connection.commit()
                return
            row = self._connection.execute(
                "SELECT value FROM radar_meta WHERE key = ?", ("schema_version",)
            ).fetchone()
            version = int(row["value"]) if row is not None else 0
            if version > RADAR_SCHEMA_VERSION:
                raise EventStoreError(
                    f"event-radar schema is version {version} but this build "
                    f"speaks {RADAR_SCHEMA_VERSION}; upgrade NovaControl instead of "
                    "reading a newer store optimistically"
                )
            if version < RADAR_SCHEMA_VERSION:
                raise EventStoreError(
                    f"event-radar schema {version} predates this build; a migration "
                    "is required"
                )

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM radar_meta WHERE key = 'schema_version'"
            ).fetchone()
        return int(row["value"]) if row is not None else RADAR_SCHEMA_VERSION

    # -- sources --------------------------------------------------------------

    def ensure_sources(self, source_ids: Iterable[str], *, enabled: Iterable[str] = ()) -> None:
        enabled_set = {str(item) for item in enabled}
        now = _iso(self._clock())
        with self._write():
            for source_id in source_ids:
                self._connection.execute(
                    "INSERT OR IGNORE INTO event_sources "
                    "(source_id, enabled, updated_at) VALUES (?, ?, ?)",
                    (str(source_id), 1 if str(source_id) in enabled_set else 0, now),
                )

    def set_enabled(self, source_id: str, enabled: bool) -> None:
        self.ensure_sources([source_id])
        with self._write():
            self._connection.execute(
                "UPDATE event_sources SET enabled = ?, updated_at = ? WHERE source_id = ?",
                (1 if enabled else 0, _iso(self._clock()), str(source_id)),
            )

    def source(self, source_id: str) -> SourceHealth:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM event_sources WHERE source_id = ?", (str(source_id),)
            ).fetchone()
        if row is None:
            return SourceHealth(source_id=str(source_id))
        return SourceHealth(
            source_id=str(row["source_id"]),
            status=str(row["status"]),
            enabled=bool(row["enabled"]),
            last_attempt=_moment(row["last_attempt"]),
            last_success=_moment(row["last_success"]),
            consecutive_failures=int(row["consecutive_failures"]),
            entries_seen=int(row["entries_seen"]),
            entries_new=int(row["entries_new"]),
            last_error=str(row["last_error"]),
            last_error_code=str(row["last_error_code"]),
            etag=str(row["etag"]),
        )

    def sources(self) -> tuple[SourceHealth, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM event_sources ORDER BY source_id"
            ).fetchall()
        return tuple(
            SourceHealth(
                source_id=str(row["source_id"]),
                status=str(row["status"]),
                enabled=bool(row["enabled"]),
                last_attempt=_moment(row["last_attempt"]),
                last_success=_moment(row["last_success"]),
                consecutive_failures=int(row["consecutive_failures"]),
                entries_seen=int(row["entries_seen"]),
                entries_new=int(row["entries_new"]),
                last_error=str(row["last_error"]),
                last_error_code=str(row["last_error_code"]),
                etag=str(row["etag"]),
            )
            for row in rows
        )

    def record_attempt(
        self,
        source_id: str,
        *,
        status: str,
        error_code: str = "",
        error: str = "",
        etag: str = "",
        entries_seen: int = 0,
        entries_new: int = 0,
        unchanged: bool = False,
    ) -> SourceHealth:
        now = self._clock()
        self.ensure_sources([source_id])
        previous = self.source(source_id)
        failures = 0 if status == "ok" else previous.consecutive_failures + 1
        with self._write():
            if status == "ok":
                self._connection.execute(
                    "UPDATE event_sources SET status = ?, last_attempt = ?, "
                    "last_success = ?, consecutive_failures = 0, entries_seen = ?, "
                    "entries_new = ?, last_error = '', last_error_code = '', "
                    "etag = ?, updated_at = ? WHERE source_id = ?",
                    (
                        "unchanged" if unchanged else "ok",
                        _iso(now),
                        _iso(now),
                        int(entries_seen),
                        int(entries_new),
                        etag or previous.etag,
                        _iso(now),
                        str(source_id),
                    ),
                )
            else:
                self._connection.execute(
                    "UPDATE event_sources SET status = ?, last_attempt = ?, "
                    "consecutive_failures = ?, last_error = ?, "
                    "last_error_code = ?, updated_at = ? WHERE source_id = ?",
                    (
                        status,
                        _iso(now),
                        failures,
                        clip(error, 500),
                        error_code,
                        _iso(now),
                        str(source_id),
                    ),
                )
        return self.source(source_id)

    # -- events ---------------------------------------------------------------

    def candidates(self, *, subcategory: str = "", limit: int = 200) -> tuple[dict[str, Any], ...]:
        """Latest version per event, as the comparison fields dedupe needs."""
        clause = " WHERE m.version = (SELECT MAX(version) FROM market_events v WHERE v.event_id = m.event_id)"
        params: list[Any] = []
        if subcategory:
            clause += " AND m.subcategory = ?"
            params.append(str(subcategory))
        clause += " ORDER BY m.updated_at DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._connection.execute(
                "SELECT m.event_id, m.version, m.event_key, m.subject, m.subcategory, "
                "m.title, m.digest, m.instruments, m.published_at FROM market_events m"
                + clause,
                params,
            ).fetchall()
        return tuple(
            {
                "event_id": str(row["event_id"]),
                "version": int(row["version"]),
                "event_key": str(row["event_key"]),
                "subject": str(row["subject"]),
                "subcategory": str(row["subcategory"]),
                "title": str(row["title"]),
                "digest": str(row["digest"]),
                "instrument_ids": tuple(json.loads(row["instruments"] or "[]")),
                "published_at": _moment(row["published_at"]),
            }
            for row in rows
        )

    def latest_version(self, event_id: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT MAX(version) FROM market_events WHERE event_id = ?",
                (str(event_id),),
            ).fetchone()
        if row is None or row[0] is None:
            raise EventNotFoundError(f"no market event with id {event_id!r}")
        return int(row[0])

    def insert_event(
        self,
        event: MarketEvent,
        *,
        evidence: Sequence[EvidenceRecord] = (),
        mappings: Sequence[EventMapping] = (),
        transitions: Sequence[tuple[str, str, str, str, str]] = (),
        subject: str = "",
        digest: str = "",
    ) -> None:
        """Persist one event version and everything that justifies it, atomically."""
        with self._write():
            self._connection.execute(
                "INSERT INTO market_events (event_id, version, event_key, subject, "
                "title, description, category, subcategory, status, verification, "
                "importance, confidence, score, classification, verification_detail, "
                "occurred_at, published_at, discovered_at, retrieved_at, updated_at, "
                "market, instruments, sectors, evidence_count, independent_sources, "
                "fingerprint, digest, supersedes, expiry_days) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event.event_id,
                    event.version,
                    event.event_key,
                    subject or event.event_key,
                    event.title,
                    event.description,
                    event.category.value,
                    event.subcategory,
                    event.status.value,
                    event.verification.value,
                    event.importance.value,
                    float(event.confidence),
                    float(event.classification.score if event.classification else 0.0),
                    _json(event.classification.to_dict() if event.classification else {}),
                    _json(event.verification_detail.to_dict() if event.verification_detail else {}),
                    _iso(event.occurred_at) if event.occurred_at else None,
                    _iso(event.published_at) if event.published_at else None,
                    _iso(event.discovered_at),
                    _iso(event.retrieved_at),
                    _iso(event.updated_at),
                    event.market,
                    _json(list(event.instruments)),
                    _json(list(event.sectors)),
                    int(event.evidence_count),
                    int(event.independent_sources),
                    event.fingerprint,
                    digest,
                    event.supersedes,
                    int(event.expiry_days),
                ),
            )
            for record in evidence:
                self._connection.execute(
                    "INSERT OR IGNORE INTO market_event_evidence (evidence_id, "
                    "event_id, event_version, source_id, url, publisher, reliability, "
                    "title, excerpt, content_hash, independence_key, status, reasons, "
                    "published_at, retrieved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record.evidence_id,
                        event.event_id,
                        event.version,
                        record.source_id,
                        record.url,
                        record.publisher,
                        record.reliability.value,
                        record.title,
                        record.excerpt,
                        record.content_hash,
                        record.independence_key,
                        record.status.value,
                        _json(list(record.reasons)),
                        _iso(record.published_at) if record.published_at else None,
                        _iso(record.retrieved_at),
                    ),
                )
            for mapping in mappings:
                self._connection.execute(
                    "INSERT OR REPLACE INTO market_event_mappings (event_id, "
                    "event_version, instrument_id, relationship, reason, sector) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        event.event_id,
                        event.version,
                        mapping.instrument_id,
                        mapping.relationship.value,
                        mapping.reason,
                        mapping.sector,
                    ),
                )
            for from_status, to_status, from_verification, to_verification, reason in transitions:
                self._connection.execute(
                    "INSERT INTO market_event_transitions (event_id, version, "
                    "from_status, to_status, verification_from, verification_to, "
                    "reason, at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        event.event_id,
                        event.version,
                        from_status,
                        to_status,
                        from_verification,
                        to_verification,
                        reason,
                        _iso(self._clock()),
                    ),
                )

    def get_event(self, event_id: str, *, version: int | None = None) -> MarketEvent:
        with self._lock:
            if version is None:
                row = self._connection.execute(
                    "SELECT * FROM market_events WHERE event_id = ? "
                    "ORDER BY version DESC LIMIT 1",
                    (str(event_id),),
                ).fetchone()
            else:
                row = self._connection.execute(
                    "SELECT * FROM market_events WHERE event_id = ? AND version = ?",
                    (str(event_id), int(version)),
                ).fetchone()
            if row is None:
                raise EventNotFoundError(
                    f"no market event with id {event_id!r}"
                    + (f" and version {version}" if version is not None else "")
                )
            mappings = self._mappings(str(event_id), int(row["version"]))
        return self._event_from_row(row, mappings)

    def history(self, event_id: str) -> tuple[MarketEvent, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM market_events WHERE event_id = ? ORDER BY version",
                (str(event_id),),
            ).fetchall()
            if not rows:
                raise EventNotFoundError(f"no market event with id {event_id!r}")
            return tuple(
                self._event_from_row(row, self._mappings(str(event_id), int(row["version"])))
                for row in rows
            )

    def _mappings(self, event_id: str, version: int) -> tuple[EventMapping, ...]:
        rows = self._connection.execute(
            "SELECT * FROM market_event_mappings WHERE event_id = ? AND event_version = ? "
            "ORDER BY relationship, instrument_id",
            (event_id, version),
        ).fetchall()
        return tuple(
            EventMapping(
                instrument_id=str(row["instrument_id"]),
                relationship=Relationship(str(row["relationship"])),
                reason=str(row["reason"]),
                sector=str(row["sector"]),
            )
            for row in rows
        )

    def _event_from_row(
        self, row: sqlite3.Row, mappings: Sequence[EventMapping]
    ) -> MarketEvent:
        classification_body = json.loads(row["classification"] or "{}")
        classification = None
        if classification_body:
            classification = Classification(
                importance=Importance(classification_body.get("importance", "low")),
                score=float(classification_body.get("score", 0.0)),
                confidence=float(classification_body.get("confidence", 0.0)),
                factors=tuple(
                    Factor(
                        name=str(item.get("name", "")),
                        value=float(item.get("value", 0.0)),
                        weight=float(item.get("weight", 0.0)),
                        contribution=float(item.get("contribution", 0.0)),
                        reason=str(item.get("reason", "")),
                    )
                    for item in classification_body.get("factors", [])
                ),
                explanation=str(classification_body.get("explanation", "")),
            )
        verification_body = json.loads(row["verification_detail"] or "{}")
        decision = None
        if verification_body:
            decision = VerificationDecision(
                state=VerificationState(verification_body.get("state", "unverified")),
                reasons=tuple(verification_body.get("reasons", [])),
                evidence_ids=tuple(verification_body.get("evidence_ids", [])),
                independent_sources=int(verification_body.get("independent_sources", 0)),
                conflicts=tuple(verification_body.get("conflicts", [])),
            )
        return MarketEvent(
            event_id=str(row["event_id"]),
            event_key=str(row["event_key"]),
            version=int(row["version"]),
            title=str(row["title"]),
            description=str(row["description"]),
            category=EventCategory(str(row["category"])),
            subcategory=str(row["subcategory"]),
            status=EventStatus(str(row["status"])),
            verification=VerificationState(str(row["verification"])),
            importance=Importance(str(row["importance"])),
            confidence=float(row["confidence"]),
            discovered_at=_aware(datetime.fromisoformat(str(row["discovered_at"])), field_name="discovered_at"),
            retrieved_at=_aware(datetime.fromisoformat(str(row["retrieved_at"])), field_name="retrieved_at"),
            updated_at=_aware(datetime.fromisoformat(str(row["updated_at"])), field_name="updated_at"),
            published_at=_moment(row["published_at"]),
            occurred_at=_moment(row["occurred_at"]),
            market=str(row["market"]),
            instruments=tuple(json.loads(row["instruments"] or "[]")),
            sectors=tuple(json.loads(row["sectors"] or "[]")),
            evidence_count=int(row["evidence_count"]),
            independent_sources=int(row["independent_sources"]),
            fingerprint=str(row["fingerprint"]),
            classification=classification,
            verification_detail=decision,
            mappings=tuple(mappings),
            supersedes=int(row["supersedes"]) if row["supersedes"] is not None else None,
            expiry_days=int(row["expiry_days"]),
        )

    def evidence_for(self, event_id: str, *, version: int | None = None) -> tuple[EvidenceRecord, ...]:
        clause = " WHERE event_id = ?"
        params: list[Any] = [str(event_id)]
        if version is not None:
            clause += " AND event_version = ?"
            params.append(int(version))
        clause += " ORDER BY retrieved_at, evidence_id"
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM market_event_evidence" + clause, params
            ).fetchall()
            if not rows:
                if not self._event_exists(str(event_id)):
                    raise EventNotFoundError(f"no market event with id {event_id!r}")
        return tuple(self._evidence_from_row(row) for row in rows)

    def _event_exists(self, event_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM market_events WHERE event_id = ? LIMIT 1", (event_id,)
        ).fetchone()
        return row is not None

    def _evidence_from_row(self, row: sqlite3.Row) -> EvidenceRecord:
        return EvidenceRecord(
            evidence_id=str(row["evidence_id"]),
            event_id=str(row["event_id"]),
            source_id=str(row["source_id"]),
            url=str(row["url"]),
            publisher=str(row["publisher"]),
            reliability=SourceReliability(str(row["reliability"])),
            title=str(row["title"]),
            excerpt=str(row["excerpt"]),
            content_hash=str(row["content_hash"]),
            independence_key=str(row["independence_key"]),
            retrieved_at=_aware(datetime.fromisoformat(str(row["retrieved_at"])), field_name="retrieved_at"),
            published_at=_moment(row["published_at"]),
            status=EvidenceStatus(str(row["status"])),
            reasons=tuple(json.loads(row["reasons"] or "[]")),
        )

    def transitions(self, event_id: str) -> tuple[dict[str, Any], ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM market_event_transitions WHERE event_id = ? "
                "ORDER BY transition_id",
                (str(event_id),),
            ).fetchall()
        return tuple(
            {
                "event_id": str(row["event_id"]),
                "version": int(row["version"]),
                "from_status": str(row["from_status"]),
                "to_status": str(row["to_status"]),
                "verification_from": str(row["verification_from"]),
                "verification_to": str(row["verification_to"]),
                "reason": str(row["reason"]),
                "actor": str(row["actor"]),
                "at": str(row["at"]),
            }
            for row in rows
        )

    def search(self, query: EventQuery) -> tuple[tuple[MarketEvent, ...], int]:
        clause, params = self._filter(query)
        if query.latest_only:
            newest = (
                "m.version = (SELECT MAX(version) FROM market_events v "
                "WHERE v.event_id = m.event_id)"
            )
            # The clause may be empty (no filters), so the connector has to be
            # chosen rather than assumed.
            latest = f" AND {newest}" if clause else f" WHERE {newest}"
        else:
            latest = ""
        with self._lock:
            total = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM market_events m{clause}", params
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                f"SELECT m.* FROM market_events m{clause}{latest} "
                "ORDER BY COALESCE(m.published_at, m.discovered_at) DESC, "
                "m.event_id DESC LIMIT ? OFFSET ?",
                [*params, query.limit, query.offset],
            ).fetchall()
            events = tuple(
                self._event_from_row(row, self._mappings(str(row["event_id"]), int(row["version"])))
                for row in rows
            )
        return events, total

    def _filter(self, query: EventQuery) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        joins = ""
        if query.category is not None:
            clauses.append("m.category = ?")
            params.append(query.category.value)
        if query.subcategory:
            clauses.append("m.subcategory = ?")
            params.append(query.subcategory)
        if query.verification is not None:
            clauses.append("m.verification = ?")
            params.append(query.verification.value)
        if query.importance is not None:
            clauses.append("m.importance = ?")
            params.append(query.importance.value)
        if query.market:
            clauses.append("m.market = ?")
            params.append(query.market.upper())
        if query.sector:
            clauses.append("EXISTS (SELECT 1 FROM market_event_mappings x WHERE x.event_id = m.event_id AND x.event_version = m.version AND lower(x.sector) = ?)")
            params.append(query.sector.strip().lower())
        if query.instrument_id:
            clauses.append("EXISTS (SELECT 1 FROM market_event_mappings x WHERE x.event_id = m.event_id AND x.event_version = m.version AND x.instrument_id = ?)")
            params.append(query.instrument_id)
        if query.source_id:
            clauses.append("EXISTS (SELECT 1 FROM market_event_evidence e WHERE e.event_id = m.event_id AND e.source_id = ?)")
            params.append(query.source_id)
        if query.text:
            clauses.append("(lower(m.title) LIKE ? OR lower(m.description) LIKE ? OR lower(m.subject) LIKE ?)")
            needle = f"%{query.text.strip().lower()}%"
            params.extend([needle, needle, needle])
        if query.since is not None:
            clauses.append("COALESCE(m.published_at, m.discovered_at) >= ?")
            params.append(_iso(query.since))
        if query.until is not None:
            clauses.append("COALESCE(m.published_at, m.discovered_at) <= ?")
            params.append(_iso(query.until))
        return (
            (" " + joins + " WHERE " + " AND ".join(clauses)) if clauses else joins,
            params,
        )

    def due_for_expiry(self, *, now: datetime) -> tuple[MarketEvent, ...]:
        clause = (
            " WHERE m.version = (SELECT MAX(version) FROM market_events v "
            "WHERE v.event_id = m.event_id) AND m.status IN ('active', 'resolved') "
            "AND COALESCE(m.published_at, m.discovered_at) <= ?"
        )
        with self._lock:
            rows = self._connection.execute(
                "SELECT m.* FROM market_events m" + clause, (_iso(now),)
            ).fetchall()
            events = []
            for row in rows:
                event = self._event_from_row(
                    row, self._mappings(str(row["event_id"]), int(row["version"]))
                )
                deadline = (event.published_at or event.discovered_at) + timedelta(
                    days=event.expiry_days
                )
                if deadline <= now:
                    events.append(event)
        return tuple(events)

    # -- metrics --------------------------------------------------------------

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM market_events) AS versions,
                    (SELECT COUNT(DISTINCT event_id) FROM market_events) AS events,
                    (SELECT COUNT(*) FROM market_event_evidence) AS evidence,
                    (SELECT COUNT(*) FROM market_event_mappings) AS mappings,
                    (SELECT COUNT(*) FROM market_event_transitions) AS transitions,
                    (SELECT COUNT(*) FROM event_sources) AS sources
                """
            ).fetchone()
            verification = {
                str(item["verification"]): int(item["count"])
                for item in self._connection.execute(
                    "SELECT verification, COUNT(*) AS count FROM market_events m "
                    "WHERE m.version = (SELECT MAX(version) FROM market_events v "
                    "WHERE v.event_id = m.event_id) GROUP BY verification"
                )
            }
            importance = {
                str(item["importance"]): int(item["count"])
                for item in self._connection.execute(
                    "SELECT importance, COUNT(*) AS count FROM market_events m "
                    "WHERE m.version = (SELECT MAX(version) FROM market_events v "
                    "WHERE v.event_id = m.event_id) GROUP BY importance"
                )
            }
            categories = {
                str(item["category"]): int(item["count"])
                for item in self._connection.execute(
                    "SELECT category, COUNT(*) AS count FROM market_events m "
                    "WHERE m.version = (SELECT MAX(version) FROM market_events v "
                    "WHERE v.event_id = m.event_id) GROUP BY category"
                )
            }
        return {
            "event_versions": int(row["versions"]),
            "events": int(row["events"]),
            "evidence_records": int(row["evidence"]),
            "instrument_mappings": int(row["mappings"]),
            "transitions": int(row["transitions"]),
            "sources": int(row["sources"]),
            "by_verification": verification,
            "by_importance": importance,
            "by_category": categories,
        }

    def health(self) -> str:
        try:
            with self._lock:
                value = self._connection.execute("PRAGMA quick_check").fetchone()[0]
            return "ok" if str(value).lower() == "ok" else "degraded"
        except sqlite3.Error:
            return "failing"


__all__ = ["EventStore", "RADAR_SCHEMA_VERSION"]
