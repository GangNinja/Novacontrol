"""Isolated append-only persistence for PI-24 event evidence.

NM-04 already owns normalized event history, evidence and mappings. This store
keeps PI-24's own relevance assessments, feature snapshots, warnings, outcomes
and analysis runs so they can be replayed without rewriting the radar.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from novacontrol.novamarket.event_intelligence.models import (
    PI24_SCHEMA_VERSION,
    EventIntelligenceConfig,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ei_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ei_records (
    record_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    event_id TEXT NOT NULL DEFAULT '',
    instrument_id TEXT NOT NULL DEFAULT '',
    horizon TEXT NOT NULL DEFAULT '',
    relevance TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    digest TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ei_records_kind_created
    ON ei_records (kind, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ei_records_event
    ON ei_records (event_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ei_records_scope
    ON ei_records (instrument_id, horizon, kind, created_at DESC);
CREATE TABLE IF NOT EXISTS ei_config (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS ei_records_immutable_update
BEFORE UPDATE ON ei_records
BEGIN
    SELECT RAISE(ABORT, 'PI-24 evidence is immutable');
END;
CREATE TRIGGER IF NOT EXISTS ei_records_immutable_delete
BEFORE DELETE ON ei_records
BEGIN
    SELECT RAISE(ABORT, 'PI-24 evidence is immutable');
END;
"""


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class EventIntelligenceStore:
    def __init__(self, path: str | Path = ":memory:", *, clock: Any = None) -> None:
        self.path = str(path)
        self.schema_version = PI24_SCHEMA_VERSION
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._connection.execute(
                "INSERT OR IGNORE INTO ei_meta(key, value) VALUES(?, ?)",
                ("schema_version", str(PI24_SCHEMA_VERSION)),
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock, suppress(sqlite3.Error):
            self._connection.close()

    def __enter__(self) -> EventIntelligenceStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def save(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        record_id: str = "",
        event_id: str = "",
        instrument_id: str = "",
        horizon: str = "",
        relevance: str = "",
        status: str = "",
        created_at: str = "",
    ) -> dict[str, Any]:
        body = dict(payload)
        created = created_at or str(body.get("created_at") or self._now().isoformat())
        digest = _digest(body)
        identifier = record_id or str(
            body.get("record_id")
            or body.get("assessment_id")
            or body.get("snapshot_id")
            or body.get("outcome_id")
            or body.get("analysis_id")
            or ""
        )
        if not identifier:
            identifier = (
                f"ei_{kind}_"
                + hashlib.sha256(
                    f"{kind}|{event_id}|{instrument_id}|{horizon}|{created}|{digest}".encode()
                ).hexdigest()[:24]
            )
        with self._lock:
            existing = self._connection.execute(
                "SELECT payload FROM ei_records WHERE record_id=?", (identifier,)
            ).fetchone()
            if existing is not None:
                return cast(dict[str, Any], json.loads(existing["payload"]))
            self._connection.execute(
                "INSERT INTO ei_records(record_id, kind, event_id, instrument_id, horizon, "
                "relevance, status, created_at, digest, payload) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    str(kind),
                    str(event_id),
                    str(instrument_id),
                    str(horizon),
                    str(relevance),
                    str(status or body.get("status") or ""),
                    created,
                    digest,
                    _canonical(body),
                ),
            )
            self._connection.commit()
        return body

    def get(self, record_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM ei_records WHERE record_id=?", (record_id,)
            ).fetchone()
        return cast(dict[str, Any], json.loads(row["payload"])) if row is not None else None

    def find_by_digest(self, kind: str, digest_value: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM ei_records WHERE kind=? AND digest=? "
                "ORDER BY created_at DESC LIMIT 1",
                (kind, digest_value),
            ).fetchone()
        return json.loads(row["payload"]) if row is not None else None

    def list(
        self,
        kind: str | None = None,
        *,
        event_id: str = "",
        instrument_id: str = "",
        horizon: str = "",
        relevance: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("kind", kind),
            ("event_id", event_id),
            ("instrument_id", instrument_id),
            ("horizon", horizon),
            ("relevance", relevance),
            ("status", status),
        ):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT payload FROM ei_records{where} "
                "ORDER BY created_at DESC, record_id DESC LIMIT ? OFFSET ?",
                (*params, max(1, min(int(limit), 500)), max(0, int(offset))),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def total(
        self,
        kind: str | None = None,
        *,
        event_id: str = "",
        instrument_id: str = "",
        horizon: str = "",
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("kind", kind),
            ("event_id", event_id),
            ("instrument_id", instrument_id),
            ("horizon", horizon),
        ):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            row = self._connection.execute(
                f"SELECT COUNT(*) AS n FROM ei_records{where}", params
            ).fetchone()
        return int(row["n"]) if row is not None else 0

    def latest(self, kind: str, **filters: Any) -> dict[str, Any] | None:
        rows = self.list(kind, limit=1, **filters)
        return rows[0] if rows else None

    def counts(self) -> dict[str, Any]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT kind, COUNT(*) AS n FROM ei_records GROUP BY kind"
            ).fetchall()
        by_kind = {str(row["kind"]): int(row["n"]) for row in rows}
        return {"total": sum(by_kind.values()), "by_kind": by_kind}

    def save_config(
        self, config: EventIntelligenceConfig, *, updated_at: str = ""
    ) -> dict[str, Any]:
        body = config.to_dict()
        with self._lock:
            self._connection.execute(
                "INSERT INTO ei_config(singleton, updated_at, payload) VALUES(1,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "updated_at=excluded.updated_at, payload=excluded.payload",
                (updated_at or self._now().isoformat(), _canonical(body)),
            )
            self._connection.commit()
        return body

    def load_config(self) -> EventIntelligenceConfig | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM ei_config WHERE singleton=1"
            ).fetchone()
        if row is None:
            return None
        return EventIntelligenceConfig.from_mapping(json.loads(row["payload"]))

    def integrity(self) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute("PRAGMA integrity_check").fetchone()
        return {"ok": str(row[0]).lower() == "ok", "result": str(row[0])}


__all__ = ["EventIntelligenceStore"]
