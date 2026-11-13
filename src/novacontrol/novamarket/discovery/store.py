"""SQLite persistence for NM-05 discovery, evaluations and watchlists.

The store lives in the same NovaMarket database file as NM-02's market data and
NM-04's events, under its own schema version. It is append-only where history
matters: an evaluation is never overwritten, and every membership state change is
written to a history table, so "why is this here" can be answered months later.

A membership row is unique per ``(watchlist, instrument)``. Removal keeps the row
(``status='removed'``) and its history; re-adding reinstates that row and records
the reinstatement, so a second active membership can never exist.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from novacontrol.novamarket.discovery.models import (
    CandidateEvaluation,
    CycleReport,
    EntryStatus,
    WatchlistEntry,
    WatchlistKind,
    WatchlistRecord,
    entry_id_for,
    utcnow,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS discovery_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discovery_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    cycle_id TEXT NOT NULL DEFAULT '',
    evaluated_at TEXT NOT NULL,
    methodology_version TEXT NOT NULL,
    score REAL NOT NULL,
    data_quality TEXT NOT NULL,
    discovery_reason TEXT NOT NULL,
    digest TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_discovery_eval_instrument
    ON discovery_evaluations(instrument_id, evaluated_at DESC);
CREATE TRIGGER IF NOT EXISTS discovery_evaluations_no_update
BEFORE UPDATE ON discovery_evaluations
BEGIN SELECT RAISE(ABORT, 'discovery evaluations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS discovery_evaluations_no_delete
BEFORE DELETE ON discovery_evaluations
BEGIN SELECT RAISE(ABORT, 'discovery evaluations are append-only'); END;

CREATE TABLE IF NOT EXISTS discovery_candidates (
    instrument_id TEXT PRIMARY KEY,
    first_discovered_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    discovery_reason TEXT NOT NULL,
    last_score REAL NOT NULL,
    last_quality TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery_watchlists (
    watchlist_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    custom INTEGER NOT NULL DEFAULT 0,
    UNIQUE(name)
);

CREATE TABLE IF NOT EXISTS discovery_entries (
    entry_id TEXT PRIMARY KEY,
    watchlist_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    status TEXT NOT NULL,
    added_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    score REAL NOT NULL DEFAULT 0,
    last_evaluated_at TEXT,
    status_reason TEXT NOT NULL DEFAULT '',
    event_ids TEXT NOT NULL DEFAULT '[]',
    below_threshold_count INTEGER NOT NULL DEFAULT 0,
    removal_reason TEXT NOT NULL DEFAULT '',
    UNIQUE(watchlist_id, instrument_id)
);
CREATE INDEX IF NOT EXISTS idx_discovery_entries_status
    ON discovery_entries(watchlist_id, status);

CREATE TABLE IF NOT EXISTS discovery_entry_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    watchlist_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    from_status TEXT NOT NULL DEFAULT '',
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    score REAL NOT NULL DEFAULT 0,
    at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS discovery_history_no_update
BEFORE UPDATE ON discovery_entry_history
BEGIN SELECT RAISE(ABORT, 'watchlist history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS discovery_history_no_delete
BEFORE DELETE ON discovery_entry_history
BEGIN SELECT RAISE(ABORT, 'watchlist history is append-only'); END;

CREATE TABLE IF NOT EXISTS discovery_cycles (
    cycle_id TEXT PRIMARY KEY,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    scanned INTEGER NOT NULL DEFAULT 0,
    eligible INTEGER NOT NULL DEFAULT 0,
    evaluated INTEGER NOT NULL DEFAULT 0,
    created INTEGER NOT NULL DEFAULT 0,
    added INTEGER NOT NULL DEFAULT 0,
    upgraded INTEGER NOT NULL DEFAULT 0,
    downgraded INTEGER NOT NULL DEFAULT 0,
    removed INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    failed_sources INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    errors TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS discovery_sources (
    source_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'never_run',
    last_attempt TEXT,
    last_success TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT ''
);
"""


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _moment(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class DiscoveryStore:
    """Append-only discovery history plus mutable membership rows."""

    def __init__(
        self, path: str = ":memory:", *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.path = str(path)
        self.schema_version = SCHEMA_VERSION
        self._clock = clock or utcnow
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._connection.execute(
                "INSERT OR IGNORE INTO discovery_meta(key, value) VALUES(?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            self._connection.commit()

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            try:
                self._connection.close()
            except sqlite3.Error:  # pragma: no cover - closing twice
                pass

    def __enter__(self) -> "DiscoveryStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _commit(self) -> None:
        self._connection.commit()

    # -- evaluations ----------------------------------------------------------

    def record_evaluation(
        self, evaluation: CandidateEvaluation, *, cycle_id: str = ""
    ) -> str:
        """Insert one immutable evaluation.

        The digest identifies the inputs; the id also carries the evaluation
        moment and its cycle, so re-running the same data later appends a new
        record instead of silently disappearing into a digest collision.
        """
        identity = (
            f"{evaluation.digest}|{evaluation.evaluated_at.isoformat()}|{cycle_id}"
        )
        evaluation_id = "evl_" + hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()[:20]
        payload = json.dumps(evaluation.to_dict(), sort_keys=True)
        with self._lock:
            self._connection.execute(
                "INSERT OR IGNORE INTO discovery_evaluations("
                "evaluation_id, instrument_id, cycle_id, evaluated_at, "
                "methodology_version, score, data_quality, discovery_reason, "
                "digest, payload) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    evaluation_id,
                    evaluation.instrument_id,
                    cycle_id,
                    evaluation.evaluated_at.isoformat(),
                    evaluation.methodology_version,
                    evaluation.score,
                    evaluation.data_quality.value,
                    evaluation.discovery_reason.value,
                    evaluation.digest,
                    payload,
                ),
            )
            self._commit()
        return evaluation_id

    def latest_evaluation(self, instrument_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM discovery_evaluations WHERE instrument_id=? "
                "ORDER BY evaluated_at DESC LIMIT 1",
                (instrument_id,),
            ).fetchone()
        return self._evaluation_row(row) if row is not None else None

    def evaluations(self, instrument_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM discovery_evaluations WHERE instrument_id=? "
                "ORDER BY evaluated_at DESC LIMIT ?",
                (instrument_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [self._evaluation_row(row) for row in rows]

    @staticmethod
    def _evaluation_row(row: sqlite3.Row) -> dict[str, Any]:
        body: dict[str, Any] = json.loads(row["payload"])
        body["evaluation_id"] = row["evaluation_id"]
        body["cycle_id"] = row["cycle_id"]
        body["score"] = row["score"]
        body["data_quality"] = row["data_quality"]
        body["methodology_version"] = row["methodology_version"]
        body["digest"] = row["digest"]
        return body

    # -- candidates -----------------------------------------------------------

    def upsert_candidate(
        self,
        instrument_id: str,
        *,
        at: datetime,
        reason: str,
        score: float,
        quality: str,
    ) -> bool:
        """Register a candidate once; later sightings only move ``last_seen_at``."""
        with self._lock:
            row = self._connection.execute(
                "SELECT instrument_id FROM discovery_candidates WHERE instrument_id=?",
                (instrument_id,),
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO discovery_candidates(instrument_id, first_discovered_at, "
                    "last_seen_at, discovery_reason, last_score, last_quality) "
                    "VALUES (?,?,?,?,?,?)",
                    (instrument_id, at.isoformat(), at.isoformat(), reason, score, quality),
                )
                self._commit()
                return True
            self._connection.execute(
                "UPDATE discovery_candidates SET last_seen_at=?, last_score=?, "
                "last_quality=? WHERE instrument_id=?",
                (at.isoformat(), score, quality, instrument_id),
            )
            self._commit()
            return False

    def candidate(self, instrument_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM discovery_candidates WHERE instrument_id=?",
                (instrument_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def candidates(self, *, limit: int = 50, min_score: float = 0.0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM discovery_candidates WHERE last_score>=? "
                "ORDER BY last_score DESC LIMIT ?",
                (float(min_score), max(1, min(int(limit), 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def prune_candidates_except(self, instrument_ids: Sequence[str]) -> int:
        """Retire candidate rows absent from a data-backed cycle.

        A catalogue entry without a stored series is not a candidate: keeping it
        with ``last_score=0`` would present a data gap as a market observation.
        Evaluation rows stay append-only; only the current candidate index moves.
        """
        ids = sorted({str(item).strip() for item in instrument_ids if str(item).strip()})
        with self._lock:
            if ids:
                placeholders = ",".join("?" for _ in ids)
                cursor = self._connection.execute(
                    f"DELETE FROM discovery_candidates WHERE instrument_id NOT IN ({placeholders})",
                    tuple(ids),
                )
            else:
                cursor = self._connection.execute("DELETE FROM discovery_candidates")
            self._commit()
            return max(0, int(cursor.rowcount))

    # -- watchlists -----------------------------------------------------------

    def ensure_watchlist(
        self,
        kind: WatchlistKind,
        name: str,
        *,
        description: str = "",
        custom: bool = False,
        at: datetime | None = None,
    ) -> WatchlistRecord:
        from novacontrol.novamarket.discovery.models import watchlist_id_for

        moment = at or self._clock()
        watchlist_id = watchlist_id_for(kind, name)
        with self._lock:
            self._connection.execute(
                "INSERT OR IGNORE INTO discovery_watchlists("
                "watchlist_id, name, kind, created_at, updated_at, description, custom) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    watchlist_id,
                    name,
                    kind.value,
                    moment.isoformat(),
                    moment.isoformat(),
                    description,
                    1 if custom else 0,
                ),
            )
            self._commit()
        record = self.watchlist(watchlist_id)
        assert record is not None
        return record

    def rename_watchlist(
        self, watchlist_id: str, name: str, *, at: datetime | None = None
    ) -> WatchlistRecord | None:
        """Rename in place; the stable watchlist id and all history are retained."""
        moment = at or self._clock()
        with self._lock:
            try:
                cursor = self._connection.execute(
                    "UPDATE discovery_watchlists SET name=?, updated_at=? "
                    "WHERE watchlist_id=?",
                    (name, moment.isoformat(), watchlist_id),
                )
                self._commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError("a watchlist with that name already exists") from exc
        if cursor.rowcount == 0:
            return None
        return self.watchlist(watchlist_id)

    def watchlist(self, watchlist_id: str) -> WatchlistRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT w.*, "
                "(SELECT COUNT(*) FROM discovery_entries e WHERE e.watchlist_id=w.watchlist_id "
                "AND e.status!='removed') AS entries "
                "FROM discovery_watchlists w WHERE w.watchlist_id=?",
                (watchlist_id,),
            ).fetchone()
        return self._watchlist_row(row) if row is not None else None

    def watchlists(self) -> list[WatchlistRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT w.*, "
                "(SELECT COUNT(*) FROM discovery_entries e WHERE e.watchlist_id=w.watchlist_id "
                "AND e.status!='removed') AS entries "
                "FROM discovery_watchlists w ORDER BY w.created_at, w.watchlist_id"
            ).fetchall()
        return [self._watchlist_row(row) for row in rows]

    @staticmethod
    def _watchlist_row(row: sqlite3.Row) -> WatchlistRecord:
        return WatchlistRecord(
            watchlist_id=row["watchlist_id"],
            name=row["name"],
            kind=WatchlistKind(row["kind"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            description=row["description"],
            custom=bool(row["custom"]),
            entries=int(row["entries"]),
        )

    # -- entries --------------------------------------------------------------

    def entry(self, watchlist_id: str, instrument_id: str) -> WatchlistEntry | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM discovery_entries WHERE watchlist_id=? AND instrument_id=?",
                (watchlist_id, instrument_id),
            ).fetchone()
        return self._entry_row(row) if row is not None else None

    def entries(
        self, watchlist_id: str, *, include_removed: bool = False, limit: int = 200
    ) -> list[WatchlistEntry]:
        clause = "" if include_removed else " AND status!='removed'"
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM discovery_entries WHERE watchlist_id=?{clause} "
                "ORDER BY score DESC, instrument_id LIMIT ?",
                (watchlist_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._entry_row(row) for row in rows]

    def active_entries(
        self, *, watchlist_id: str | None = None, limit: int = 500
    ) -> list[WatchlistEntry]:
        clause = " AND watchlist_id=?" if watchlist_id else ""
        params: list[Any] = [watchlist_id] if watchlist_id else []
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM discovery_entries WHERE status!='removed'{clause} "
                "ORDER BY score DESC LIMIT ?",
                (*params, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [self._entry_row(row) for row in rows]

    def entries_for_instrument(
        self, instrument_id: str, *, include_removed: bool = False
    ) -> list[WatchlistEntry]:
        """Every membership of one instrument, across all watchlists."""
        clause = "" if include_removed else " AND status!='removed'"
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM discovery_entries WHERE instrument_id=?{clause} "
                "ORDER BY watchlist_id",
                (instrument_id,),
            ).fetchall()
        return [self._entry_row(row) for row in rows]

    def save_entry(self, entry: WatchlistEntry, *, history_reason: str = "") -> WatchlistEntry:
        """Insert or update one membership; history records every status move."""
        with self._lock:
            existing = self._connection.execute(
                "SELECT * FROM discovery_entries WHERE entry_id=?",
                (entry.entry_id,),
            ).fetchone()
            previous = existing["status"] if existing is not None else ""
            removal_reason = "" if entry.status is not EntryStatus.REMOVED else history_reason
            if existing is None:
                self._connection.execute(
                    "INSERT INTO discovery_entries(entry_id, watchlist_id, instrument_id, "
                    "status, added_at, updated_at, reason, score, last_evaluated_at, "
                    "status_reason, event_ids, below_threshold_count, removal_reason) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        entry.entry_id,
                        entry.watchlist_id,
                        entry.instrument_id,
                        entry.status.value,
                        entry.added_at.isoformat(),
                        entry.updated_at.isoformat(),
                        entry.reason,
                        entry.score,
                        _iso(entry.last_evaluated_at),
                        entry.status_reason,
                        json.dumps(list(entry.event_ids)),
                        entry.below_threshold_count,
                        removal_reason,
                    ),
                )
            else:
                self._connection.execute(
                    "UPDATE discovery_entries SET status=?, updated_at=?, reason=?, score=?, "
                    "last_evaluated_at=?, status_reason=?, event_ids=?, "
                    "below_threshold_count=?, removal_reason=? WHERE entry_id=?",
                    (
                        entry.status.value,
                        entry.updated_at.isoformat(),
                        entry.reason,
                        entry.score,
                        _iso(entry.last_evaluated_at),
                        entry.status_reason,
                        json.dumps(list(entry.event_ids)),
                        entry.below_threshold_count,
                        removal_reason,
                        entry.entry_id,
                    ),
                )
            if previous != entry.status.value:
                self._connection.execute(
                    "INSERT INTO discovery_entry_history(watchlist_id, instrument_id, "
                    "from_status, to_status, reason, score, at) VALUES (?,?,?,?,?,?,?)",
                    (
                        entry.watchlist_id,
                        entry.instrument_id,
                        previous,
                        entry.status.value,
                        history_reason or entry.status_reason or entry.reason,
                        entry.score,
                        entry.updated_at.isoformat(),
                    ),
                )
            self._connection.execute(
                "UPDATE discovery_watchlists SET updated_at=? WHERE watchlist_id=?",
                (entry.updated_at.isoformat(), entry.watchlist_id),
            )
            self._commit()
        return entry

    @staticmethod
    def _entry_row(row: sqlite3.Row) -> WatchlistEntry:
        return WatchlistEntry(
            entry_id=row["entry_id"],
            watchlist_id=row["watchlist_id"],
            instrument_id=row["instrument_id"],
            status=EntryStatus(row["status"]),
            added_at=datetime.fromisoformat(row["added_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            reason=row["reason"],
            score=float(row["score"]),
            last_evaluated_at=_moment(row["last_evaluated_at"]),
            status_reason=row["status_reason"],
            event_ids=tuple(json.loads(row["event_ids"] or "[]")),
            below_threshold_count=int(row["below_threshold_count"]),
        )

    def history(
        self,
        *,
        watchlist_id: str | None = None,
        instrument_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if watchlist_id:
            clauses.append("watchlist_id=?")
            params.append(watchlist_id)
        if instrument_id:
            clauses.append("instrument_id=?")
            params.append(instrument_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM discovery_entry_history{where} "
                "ORDER BY history_id DESC LIMIT ?",
                (*params, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- cycles and sources ---------------------------------------------------

    def record_cycle(self, report: CycleReport) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO discovery_cycles(cycle_id, trigger, status, "
                "started_at, finished_at, scanned, eligible, evaluated, created, added, "
                "upgraded, downgraded, removed, skipped, failed_sources, duration_ms, errors) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    report.cycle_id,
                    report.trigger.value,
                    report.status,
                    report.started_at.isoformat(),
                    report.finished_at.isoformat(),
                    report.scanned,
                    report.eligible,
                    report.evaluated,
                    report.created,
                    report.added,
                    report.upgraded,
                    report.downgraded,
                    report.removed,
                    report.skipped,
                    report.failed_sources,
                    report.duration_ms,
                    json.dumps(list(report.errors)),
                ),
            )
            self._commit()

    def cycles(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM discovery_cycles ORDER BY started_at DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        result = []
        for row in rows:
            body = dict(row)
            body["errors"] = json.loads(body["errors"] or "[]")
            result.append(body)
        return result

    def latest_cycle(self) -> dict[str, Any] | None:
        rows = self.cycles(limit=1)
        return rows[0] if rows else None

    def record_source(
        self,
        source_id: str,
        *,
        status: str,
        at: datetime | None = None,
        error: str = "",
    ) -> None:
        moment = (at or self._clock()).isoformat()
        success = status == "ok"
        with self._lock:
            row = self._connection.execute(
                "SELECT consecutive_failures FROM discovery_sources WHERE source_id=?",
                (source_id,),
            ).fetchone()
            failures = 0 if success else int(row["consecutive_failures"] if row else 0) + 1
            self._connection.execute(
                "INSERT INTO discovery_sources(source_id, status, last_attempt, last_success, "
                "consecutive_failures, last_error) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(source_id) DO UPDATE SET status=excluded.status, "
                "last_attempt=excluded.last_attempt, "
                "last_success=COALESCE(excluded.last_success, discovery_sources.last_success), "
                "consecutive_failures=excluded.consecutive_failures, "
                "last_error=excluded.last_error",
                (source_id, status, moment, moment if success else None, failures, error),
            )
            self._commit()

    def sources(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM discovery_sources ORDER BY source_id"
            ).fetchall()
        return [dict(row) for row in rows]

    # -- observability --------------------------------------------------------

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            evaluations = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM discovery_evaluations"
                ).fetchone()[0]
            )
            candidates = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM discovery_candidates"
                ).fetchone()[0]
            )
            watchlists = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM discovery_watchlists"
                ).fetchone()[0]
            )
            entries = {
                row["status"]: int(row["count"])
                for row in self._connection.execute(
                    "SELECT status, COUNT(*) AS count FROM discovery_entries GROUP BY status"
                ).fetchall()
            }
            cycles = int(
                self._connection.execute("SELECT COUNT(*) FROM discovery_cycles").fetchone()[0]
            )
        return {
            "evaluations": evaluations,
            "candidates": candidates,
            "watchlists": watchlists,
            "entries": entries,
            "cycles": cycles,
        }


__all__ = ["SCHEMA_VERSION", "DiscoveryStore"]
