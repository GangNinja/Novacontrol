"""Append-only persistence for PI-11 forecast-evaluation reports.

This is a separate SQLite file from the wallet, market and backtest stores.  It
keeps evaluation evidence reproducible without touching any financial table.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Mapping

from novacontrol.novamarket.evaluation.models import EvaluationError, report_hash

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pi11_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    report_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    samples INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pi11_evaluations_created
    ON pi11_evaluations (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pi11_evaluations_config
    ON pi11_evaluations (config_hash);
CREATE TRIGGER IF NOT EXISTS pi11_evaluations_no_update
BEFORE UPDATE ON pi11_evaluations
BEGIN
    SELECT RAISE(ABORT, 'PI-11 evaluation reports are immutable');
END;
CREATE TRIGGER IF NOT EXISTS pi11_evaluations_no_delete
BEFORE DELETE ON pi11_evaluations
BEGIN
    SELECT RAISE(ABORT, 'PI-11 evaluation reports are immutable');
END;
"""


class EvaluationStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock, self._connection:
            self._connection.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def save(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        body["report_hash"] = report_hash(body)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO pi11_evaluations"
                "(evaluation_id, created_at, config_hash, report_hash, status, samples, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(body["evaluation_id"]),
                    str(body["created_at"]),
                    str(body.get("config_hash") or ""),
                    str(body["report_hash"]),
                    str(body.get("status") or "completed"),
                    int(body.get("samples") or 0),
                    json.dumps(body, sort_keys=True, default=str),
                ),
            )
        return body

    def get(self, evaluation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM pi11_evaluations WHERE evaluation_id = ?",
                (str(evaluation_id),),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM pi11_evaluations ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            body = json.loads(row["payload"])
            result.append(
                {
                    key: body.get(key)
                    for key in (
                        "evaluation_id",
                        "created_at",
                        "name",
                        "status",
                        "samples",
                        "config_hash",
                        "report_hash",
                        "source_summary",
                    )
                }
            )
        return result

    def counts(self) -> dict[str, Any]:
        with self._lock:
            total = int(
                self._connection.execute("SELECT COUNT(*) AS n FROM pi11_evaluations").fetchone()["n"]
            )
            completed = int(
                self._connection.execute(
                    "SELECT COUNT(*) AS n FROM pi11_evaluations WHERE status='completed'"
                ).fetchone()["n"]
            )
            degraded = int(
                self._connection.execute(
                    "SELECT COUNT(*) AS n FROM pi11_evaluations WHERE status='degraded'"
                ).fetchone()["n"]
            )
        return {"total": total, "completed": completed, "degraded": degraded}

    def find_by_config_hash(self, value: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM pi11_evaluations WHERE config_hash = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (str(value),),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def verify(self, evaluation_id: str) -> dict[str, Any]:
        body = self.get(evaluation_id)
        if body is None:
            raise EvaluationError(f"unknown evaluation {evaluation_id!r}")
        expected = str(body.get("report_hash") or "")
        actual = report_hash(body)
        return {
            "ok": expected == actual,
            "evaluation_id": evaluation_id,
            "stored_report_hash": expected,
            "recomputed_report_hash": actual,
            "verification": "canonical payload hash; no source record is regenerated",
        }


__all__ = ["EvaluationStore"]
