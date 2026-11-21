"""Isolated, append-only persistence for PI-23 evidence.

The table is intentionally generic: every PI-23 artefact (evaluation, selection,
calibration, drift event, recommendation, reliability assessment or fallback)
has the same immutable envelope and a JSON payload. This avoids duplicating the
existing forecast/model registries and makes version history auditable.
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

from novacontrol.novamarket.forecast_intelligence.models import PI23_SCHEMA_VERSION

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fi_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fi_records (
    record_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    model_id TEXT NOT NULL DEFAULT '',
    instrument_id TEXT NOT NULL DEFAULT '',
    horizon TEXT NOT NULL DEFAULT '',
    regime TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    digest TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fi_records_kind_created
    ON fi_records (kind, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_fi_records_scope
    ON fi_records (kind, instrument_id, horizon, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_fi_records_model
    ON fi_records (model_id, created_at DESC);
CREATE TABLE IF NOT EXISTS fi_config (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS fi_records_immutable_update
BEFORE UPDATE ON fi_records
BEGIN
    SELECT RAISE(ABORT, 'PI-23 evidence is immutable');
END;
CREATE TRIGGER IF NOT EXISTS fi_records_immutable_delete
BEFORE DELETE ON fi_records
BEGIN
    SELECT RAISE(ABORT, 'PI-23 evidence is immutable');
END;
"""


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class ForecastIntelligenceStore:
    def __init__(self, path: str | Path = ":memory:", *, clock: Any = None) -> None:
        self.path = str(path)
        self.schema_version = PI23_SCHEMA_VERSION
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._connection.execute(
                "INSERT OR IGNORE INTO fi_meta(key, value) VALUES(?, ?)",
                ("schema_version", str(PI23_SCHEMA_VERSION)),
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock, suppress(sqlite3.Error):
            self._connection.close()

    def __enter__(self) -> ForecastIntelligenceStore:
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
        model_id: str = "",
        instrument_id: str = "",
        horizon: str = "",
        regime: str = "",
        status: str = "",
        created_at: str = "",
    ) -> dict[str, Any]:
        body = dict(payload)
        created = created_at or str(
            body.get("created_at") or body.get("generated_at") or self._now().isoformat()
        )
        digest = _digest(body)
        identifier = record_id or str(
            body.get("record_id")
            or body.get("evaluation_id")
            or body.get("selection_id")
            or body.get("reliability_id")
            or body.get("fallback_id")
            or body.get("recommendation_id")
            or body.get("assessment_id")
            or body.get("run_id")
            or ""
        )
        if not identifier:
            identifier = (
                f"fi_{kind}_"
                + hashlib.sha256(
                    f"{kind}|{model_id}|{instrument_id}|{horizon}|{created}|{digest}".encode()
                ).hexdigest()[:24]
            )
        with self._lock:
            existing = self._connection.execute(
                "SELECT payload FROM fi_records WHERE record_id=?", (identifier,)
            ).fetchone()
            if existing is not None:
                return cast(dict[str, Any], json.loads(existing["payload"]))
            self._connection.execute(
                "INSERT INTO fi_records(record_id, kind, model_id, instrument_id, horizon, "
                "regime, status, created_at, digest, payload) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    str(kind),
                    str(model_id or ""),
                    str(instrument_id or ""),
                    str(horizon or ""),
                    str(regime or ""),
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
                "SELECT payload FROM fi_records WHERE record_id=?", (record_id,)
            ).fetchone()
        return cast(dict[str, Any], json.loads(row["payload"])) if row is not None else None

    def find_by_digest(self, kind: str, digest: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM fi_records WHERE kind=? AND digest=? "
                "ORDER BY created_at DESC LIMIT 1",
                (kind, digest),
            ).fetchone()
        return json.loads(row["payload"]) if row is not None else None

    def list(
        self,
        kind: str | None = None,
        *,
        model_id: str = "",
        instrument_id: str = "",
        horizon: str = "",
        regime: str = "",
        status: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("kind", kind),
            ("model_id", model_id),
            ("instrument_id", instrument_id),
            ("horizon", horizon),
            ("regime", regime),
            ("status", status),
        ):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT payload FROM fi_records{where} "
                "ORDER BY created_at DESC, record_id DESC LIMIT ?",
                (*params, max(1, min(int(limit), 500))),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def latest(
        self,
        kind: str,
        *,
        instrument_id: str = "",
        horizon: str = "",
        model_id: str = "",
    ) -> dict[str, Any] | None:
        rows = self.list(
            kind,
            instrument_id=instrument_id,
            horizon=horizon,
            model_id=model_id,
            limit=1,
        )
        return rows[0] if rows else None

    def history(
        self,
        *,
        instrument_id: str = "",
        horizon: str = "",
        kind: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("kind", kind),
            ("instrument_id", instrument_id),
            ("horizon", horizon),
        ):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            total = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM fi_records{where}", tuple(params)
                ).fetchone()[0]
            )
        return {
            "ok": True,
            "records": self.list(
                kind or None,
                instrument_id=instrument_id,
                horizon=horizon,
                limit=limit,
            ),
            "total": total,
        }

    def counts(self) -> dict[str, Any]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT kind, COUNT(*) AS count FROM fi_records GROUP BY kind ORDER BY kind"
            ).fetchall()
            total = int(self._connection.execute("SELECT COUNT(*) FROM fi_records").fetchone()[0])
        return {"total": total, "by_kind": {str(row["kind"]): int(row["count"]) for row in rows}}

    def save_config(self, payload: Mapping[str, Any], *, updated_at: str = "") -> dict[str, Any]:
        body = dict(payload)
        with self._lock:
            self._connection.execute(
                "INSERT INTO fi_config(singleton, updated_at, payload) VALUES(1,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "updated_at=excluded.updated_at, payload=excluded.payload",
                (updated_at or self._now().isoformat(), _canonical(body)),
            )
            self._connection.commit()
        return body

    def load_config(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM fi_config WHERE singleton=1"
            ).fetchone()
        return json.loads(row["payload"]) if row is not None else None

    def integrity(self) -> dict[str, Any]:
        with self._lock:
            check = self._connection.execute("PRAGMA integrity_check").fetchone()
        return {"ok": str(check[0]).lower() == "ok", "result": str(check[0])}


__all__ = ["ForecastIntelligenceStore"]
