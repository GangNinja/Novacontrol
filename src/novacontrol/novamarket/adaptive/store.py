"""Isolated persistence for PI-16 adaptive-intelligence evidence.

The store uses the same isolated research SQLite file as PI-13/PI-14/PI-15,
but every table is prefixed ``adp_`` and is append-only except for explicitly
stateful recommendation/reassessment rows.  It never opens a portfolio, order,
wallet, ledger, paper-fill or autonomy table.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import MAX_CONFIGS, MAX_RECORDS, AdaptiveStoreError

_SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS adp_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS adp_configs (
    config_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS adp_regime_evaluations (
    record_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adp_regime_eval_subject
    ON adp_regime_evaluations(subject, created_at);
CREATE TABLE IF NOT EXISTS adp_suitability (
    record_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adp_suitability_subject
    ON adp_suitability(subject, created_at);
CREATE TABLE IF NOT EXISTS adp_changes (
    record_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adp_changes_subject
    ON adp_changes(subject, created_at);
CREATE TABLE IF NOT EXISTS adp_drift (
    record_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adp_drift_subject
    ON adp_drift(subject, created_at);
CREATE TABLE IF NOT EXISTS adp_degradation (
    record_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adp_degradation_subject
    ON adp_degradation(subject, created_at);
CREATE TABLE IF NOT EXISTS adp_recommendations (
    record_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adp_recommendations_subject
    ON adp_recommendations(subject, updated_at);
CREATE TABLE IF NOT EXISTS adp_retraining (
    record_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adp_retraining_subject
    ON adp_retraining(subject, created_at);
CREATE TABLE IF NOT EXISTS adp_reassessments (
    record_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adp_reassessments_subject
    ON adp_reassessments(subject, updated_at);
CREATE TABLE IF NOT EXISTS adp_annotations (
    record_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adp_annotations_subject
    ON adp_annotations(subject, created_at);
CREATE TABLE IF NOT EXISTS adp_state (
    state_key TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_RECORD_TABLES = {
    "regime_evaluation": "adp_regime_evaluations",
    "suitability": "adp_suitability",
    "change": "adp_changes",
    "drift": "adp_drift",
    "degradation": "adp_degradation",
    "recommendation": "adp_recommendations",
    "retraining": "adp_retraining",
    "reassessment": "adp_reassessments",
    "annotation": "adp_annotations",
}


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).isoformat()


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str)


class AdaptiveStore:
    """Bounded, isolated persistence for adaptive research evidence."""

    def __init__(self, path: str | Path, *, max_records: int = MAX_RECORDS) -> None:
        self._path = str(path)
        self.max_records = max(1, min(int(max_records), MAX_RECORDS))
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._connection = sqlite3.connect(self._path, timeout=30.0, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.executescript(_SCHEMA)
            self._connection.execute(
                "INSERT OR IGNORE INTO adp_schema_meta(key, value) VALUES('schema_version', ?)",
                (str(_SCHEMA_VERSION),),
            )
            self._connection.commit()
        except sqlite3.Error as exc:
            raise AdaptiveStoreError(f"could not open adaptive store: {exc}") from exc

    @property
    def path(self) -> str:
        return self._path

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM adp_schema_meta WHERE key='schema_version'"
            ).fetchone()
        return int(row["value"]) if row is not None else _SCHEMA_VERSION

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def storage_info(self) -> dict[str, Any]:
        size = 0
        if self._path != ":memory:":
            try:
                size = Path(self._path).stat().st_size
            except OSError:
                size = 0
        return {
            "path": self._path,
            "size_bytes": size,
            "schema_version": self.schema_version,
            "isolated_research_tables": True,
        }

    def _count(self, table: str) -> int:
        return int(self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                "configs": self._count("adp_configs"),
                "regime_evaluations": self._count("adp_regime_evaluations"),
                "suitability": self._count("adp_suitability"),
                "changes": self._count("adp_changes"),
                "drift": self._count("adp_drift"),
                "degradation": self._count("adp_degradation"),
                "recommendations": self._count("adp_recommendations"),
                "retraining": self._count("adp_retraining"),
                "reassessments": self._count("adp_reassessments"),
                "annotations": self._count("adp_annotations"),
            }

    def save_config(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        config_id = str(payload.get("config_id") or "")
        digest = str(payload.get("digest") or "")
        if not config_id or not digest:
            raise AdaptiveStoreError("an adaptive config needs an id and digest")
        with self._lock:
            existing = self._connection.execute(
                "SELECT digest, payload_json FROM adp_configs WHERE config_id=?",
                (config_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["digest"]) != digest:
                    raise AdaptiveStoreError(f"adaptive config {config_id!r} is immutable")
                return dict(json.loads(existing["payload_json"]))
            if self._count("adp_configs") >= MAX_CONFIGS:
                raise AdaptiveStoreError(
                    f"adaptive configuration limit reached ({MAX_CONFIGS})"
                )
            self._connection.execute(
                "INSERT INTO adp_configs(config_id, digest, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (config_id, digest, _json(payload), _iso()),
            )
            self._connection.commit()
        return dict(payload)

    def config(self, config_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM adp_configs WHERE config_id=?",
                (str(config_id),),
            ).fetchone()
        return dict(json.loads(row["payload_json"])) if row else None

    def configs(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM adp_configs "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(json.loads(row["payload_json"])) for row in rows]

    def _append(
        self,
        kind: str,
        record_id: str,
        subject: str,
        status: str,
        payload: Mapping[str, Any],
        *,
        created_at: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        table = _RECORD_TABLES[kind]
        timestamp = created_at or _iso()
        with self._lock:
            existing = self._connection.execute(
                f"SELECT payload_json FROM {table} WHERE record_id=?", (record_id,)
            ).fetchone()
            if existing is not None:
                return dict(json.loads(existing["payload_json"])), False
            if self._count(table) >= self.max_records:
                raise AdaptiveStoreError(
                    f"adaptive {kind} storage limit reached ({self.max_records})"
                )
            cursor = self._connection.execute(
                f"INSERT OR IGNORE INTO {table}"
                "(record_id, subject, status, payload_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (record_id, subject, status, _json(payload), timestamp, timestamp),
            )
            self._connection.commit()
            inserted = cursor.rowcount > 0
            row = self._connection.execute(
                f"SELECT payload_json FROM {table} WHERE record_id=?", (record_id,)
            ).fetchone()
        return (dict(json.loads(row["payload_json"])) if row else {}, inserted)

    def _upsert(
        self,
        kind: str,
        record_id: str,
        subject: str,
        status: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        table = _RECORD_TABLES[kind]
        timestamp = _iso()
        with self._lock:
            existing = self._connection.execute(
                f"SELECT 1 FROM {table} WHERE record_id=?", (record_id,)
            ).fetchone()
            if existing is None and self._count(table) >= self.max_records:
                raise AdaptiveStoreError(
                    f"adaptive {kind} storage limit reached ({self.max_records})"
                )
            self._connection.execute(
                f"INSERT INTO {table}"
                "(record_id, subject, status, payload_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(record_id) DO UPDATE SET "
                "status=excluded.status, payload_json=excluded.payload_json, "
                "updated_at=excluded.updated_at",
                (record_id, subject, status, _json(payload), timestamp, timestamp),
            )
            self._connection.commit()
        return dict(payload)

    def save_regime_evaluation(
        self, record_id: str, policy_id: str, status: str, payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        return self._append("regime_evaluation", record_id, policy_id, status, payload)

    def save_suitability(
        self, record_id: str, policy_id: str, status: str, payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        return self._append("suitability", record_id, policy_id, status, payload)

    def save_change(
        self, record_id: str, instrument_id: str, status: str, payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        return self._append("change", record_id, instrument_id, status, payload)

    def save_drift(
        self, record_id: str, subject: str, status: str, payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        return self._append("drift", record_id, subject, status, payload)

    def save_degradation(
        self, record_id: str, policy_id: str, status: str, payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        return self._append("degradation", record_id, policy_id, status, payload)

    def save_recommendation(
        self, record_id: str, policy_id: str, status: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._upsert("recommendation", record_id, policy_id, status, payload)

    def save_retraining(
        self, record_id: str, policy_id: str, status: str, payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        return self._append("retraining", record_id, policy_id, status, payload)

    def save_reassessment(
        self, record_id: str, policy_id: str, status: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._upsert("reassessment", record_id, policy_id, status, payload)

    def save_annotation(
        self, record_id: str, subject: str, status: str, payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        return self._append("annotation", record_id, subject, status, payload)

    def _list(
        self,
        kind: str,
        *,
        subject: str = "",
        status: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        table = _RECORD_TABLES[kind]
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        clauses = ["1=1"]
        params: list[Any] = []
        if subject:
            clauses.append("subject=?")
            params.append(str(subject))
        if status:
            clauses.append("status=?")
            params.append(str(status))
        where = " AND ".join(clauses)
        with self._lock:
            total = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {where}", params
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                f"SELECT payload_json FROM {table} WHERE {where} "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {
            "items": [dict(json.loads(row["payload_json"])) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
        }

    def _get(self, kind: str, record_id: str) -> dict[str, Any] | None:
        table = _RECORD_TABLES[kind]
        with self._lock:
            row = self._connection.execute(
                f"SELECT payload_json FROM {table} WHERE record_id=?",
                (str(record_id),),
            ).fetchone()
        return dict(json.loads(row["payload_json"])) if row else None

    def regime_evaluations(
        self, policy_id: str = "", *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return self._list("regime_evaluation", subject=policy_id, limit=limit, offset=offset)

    def suitability(
        self, policy_id: str = "", *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return self._list("suitability", subject=policy_id, limit=limit, offset=offset)

    def changes(
        self, instrument_id: str = "", *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return self._list("change", subject=instrument_id, limit=limit, offset=offset)

    def drift(self, subject: str = "", *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        return self._list("drift", subject=subject, limit=limit, offset=offset)

    def degradation(
        self, policy_id: str = "", *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return self._list("degradation", subject=policy_id, limit=limit, offset=offset)

    def recommendations(
        self,
        policy_id: str = "",
        *,
        status: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self._list(
            "recommendation", subject=policy_id, status=status, limit=limit, offset=offset
        )

    def recommendation(self, record_id: str) -> dict[str, Any] | None:
        return self._get("recommendation", record_id)

    def retraining(
        self, policy_id: str = "", *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return self._list("retraining", subject=policy_id, limit=limit, offset=offset)

    def reassessments(
        self,
        policy_id: str = "",
        *,
        status: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self._list(
            "reassessment",
            subject=policy_id,
            status=status,
            limit=limit,
            offset=offset,
        )

    def reassessment(self, record_id: str) -> dict[str, Any] | None:
        return self._get("reassessment", record_id)

    def annotations(
        self, instrument_id: str = "", *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return self._list("annotation", subject=instrument_id, limit=limit, offset=offset)

    def state(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM adp_state WHERE state_key=?", (str(key),)
            ).fetchone()
        return dict(json.loads(row["payload_json"])) if row else None

    def set_state(self, key: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._connection.execute(
                "INSERT INTO adp_state(state_key, payload_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(state_key) DO UPDATE SET "
                "payload_json=excluded.payload_json, updated_at=excluded.updated_at",
                (str(key), _json(payload), _iso()),
            )
            self._connection.commit()
        return dict(payload)

    def reset(self) -> None:
        """Clear research records only; used by deterministic test fixtures."""
        with self._lock:
            for table in _RECORD_TABLES.values():
                self._connection.execute(f"DELETE FROM {table}")
            self._connection.execute("DELETE FROM adp_configs")
            self._connection.execute("DELETE FROM adp_state")
            self._connection.commit()
