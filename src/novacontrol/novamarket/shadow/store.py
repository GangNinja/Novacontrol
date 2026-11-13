"""The isolated NM-12 SQLite boundary.

Shadow data lives in its own database.  Decisions, projections and comparisons
are append-only (database triggers refuse UPDATE/DELETE); outcomes are the one
mutable record because a later observation is expected to refine them.  No table
here is an execution, wallet or ledger table.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from novacontrol.novamarket.shadow.models import (
    ShadowComparison,
    ShadowDecision,
    ShadowOutcome,
    ShadowProjection,
    ShadowSession,
    ShadowSessionStatus,
    utcnow,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_sessions (
    session_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_decisions (
    decision_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    decision_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS shadow_decisions_session
    ON shadow_decisions(session_id, decision_at);
CREATE TABLE IF NOT EXISTS shadow_projections (
    projection_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS shadow_projections_session
    ON shadow_projections(session_id);
CREATE TABLE IF NOT EXISTS shadow_outcomes (
    outcome_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    interval INTEGER NOT NULL,
    observed_at TEXT,
    payload TEXT NOT NULL,
    UNIQUE (decision_id, interval)
);
CREATE INDEX IF NOT EXISTS shadow_outcomes_session
    ON shadow_outcomes(session_id, decision_id);
CREATE TABLE IF NOT EXISTS shadow_errors (
    error_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS shadow_errors_session
    ON shadow_errors(session_id, at);
CREATE TABLE IF NOT EXISTS shadow_comparisons (
    comparison_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS shadow_comparisons_session
    ON shadow_comparisons(session_id);

CREATE TRIGGER IF NOT EXISTS shadow_decisions_no_update
BEFORE UPDATE ON shadow_decisions
BEGIN SELECT RAISE(ABORT, 'shadow decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS shadow_decisions_no_delete
BEFORE DELETE ON shadow_decisions
BEGIN SELECT RAISE(ABORT, 'shadow decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS shadow_projections_no_update
BEFORE UPDATE ON shadow_projections
BEGIN SELECT RAISE(ABORT, 'shadow projections are immutable'); END;
CREATE TRIGGER IF NOT EXISTS shadow_projections_no_delete
BEFORE DELETE ON shadow_projections
BEGIN SELECT RAISE(ABORT, 'shadow projections are immutable'); END;
CREATE TRIGGER IF NOT EXISTS shadow_comparisons_no_update
BEFORE UPDATE ON shadow_comparisons
BEGIN SELECT RAISE(ABORT, 'shadow comparisons are immutable'); END;
CREATE TRIGGER IF NOT EXISTS shadow_comparisons_no_delete
BEFORE DELETE ON shadow_comparisons
BEGIN SELECT RAISE(ABORT, 'shadow comparisons are immutable'); END;
"""


class ShadowStore:
    """Persistence for the shadow-only boundary."""

    def __init__(
        self, path: str | Path, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.path = str(path)
        self._clock = clock or utcnow
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path, check_same_thread=False, timeout=30.0
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 30000")
        self._connection.execute("PRAGMA foreign_keys = ON")
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._connection.commit()

    # -- sessions -------------------------------------------------------------
    def save_session(self, session: ShadowSession) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO shadow_sessions"
                " (session_id, status, payload, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    session.session_id,
                    session.status.value,
                    json.dumps(session.to_dict(), sort_keys=True, default=str),
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                ),
            )
            self._connection.commit()

    def session(self, session_id: str) -> ShadowSession | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM shadow_sessions WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
        return None if row is None else ShadowSession.from_dict(json.loads(row["payload"]))

    def sessions(self, *, limit: int = 100) -> list[ShadowSession]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM shadow_sessions ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [ShadowSession.from_dict(json.loads(row["payload"])) for row in rows]

    def status_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT status, COUNT(*) AS total FROM shadow_sessions GROUP BY status"
            ).fetchall()
        counts = {status.value: 0 for status in ShadowSessionStatus}
        for row in rows:
            counts[str(row["status"])] = int(row["total"])
        return counts

    # -- decisions ------------------------------------------------------------
    def save_decision(self, decision: ShadowDecision) -> ShadowDecision:
        with self._lock:
            try:
                self._connection.execute(
                    "INSERT INTO shadow_decisions"
                    " (decision_id, session_id, decision_at, idempotency_key, payload)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        decision.decision_id,
                        decision.session_id,
                        decision.decision_at.isoformat(),
                        decision.idempotency_key,
                        json.dumps(decision.to_dict(), sort_keys=True, default=str),
                    ),
                )
                self._connection.commit()
                return decision
            except sqlite3.IntegrityError:
                existing = self.find_decision(
                    decision.session_id, decision.idempotency_key
                )
                if existing is None:
                    raise
                return existing

    def find_decision(self, session_id: str, key: str) -> ShadowDecision | None:
        if not key:
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM shadow_decisions"
                " WHERE session_id = ? AND idempotency_key = ?",
                (str(session_id), str(key)),
            ).fetchone()
        return None if row is None else ShadowDecision.from_dict(json.loads(row["payload"]))

    def decision(self, decision_id: str) -> ShadowDecision | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM shadow_decisions WHERE decision_id = ?",
                (str(decision_id),),
            ).fetchone()
        return None if row is None else ShadowDecision.from_dict(json.loads(row["payload"]))

    def decisions(
        self,
        session_id: str,
        *,
        limit: int = 200,
        instrument_id: str = "",
        strategy_id: str = "",
        signal_type: str = "",
        status: str = "",
    ) -> list[ShadowDecision]:
        sql = "SELECT payload FROM shadow_decisions WHERE session_id = ?"
        params: list[Any] = [str(session_id)]
        if instrument_id:
            sql += " AND json_extract(payload, '$.instrument_id') = ?"
            params.append(instrument_id)
        if strategy_id:
            sql += " AND json_extract(payload, '$.strategy_id') = ?"
            params.append(strategy_id)
        if signal_type:
            sql += " AND json_extract(payload, '$.signal_type') = ?"
            params.append(signal_type)
        if status:
            sql += " AND json_extract(payload, '$.status') = ?"
            params.append(status)
        sql += " ORDER BY decision_at ASC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._connection.execute(sql, tuple(params)).fetchall()
        return [ShadowDecision.from_dict(json.loads(row["payload"])) for row in rows]

    # -- projections ----------------------------------------------------------
    def save_projection(self, projection: ShadowProjection) -> ShadowProjection:
        with self._lock:
            self._connection.execute(
                "INSERT INTO shadow_projections"
                " (projection_id, decision_id, session_id, payload)"
                " VALUES (?, ?, ?, ?)",
                (
                    projection.projection_id,
                    projection.decision_id,
                    projection.session_id,
                    json.dumps(projection.to_dict(), sort_keys=True, default=str),
                ),
            )
            self._connection.commit()
        return projection

    def projection(self, projection_id: str) -> ShadowProjection | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM shadow_projections WHERE projection_id = ?",
                (str(projection_id),),
            ).fetchone()
        return (
            None if row is None else ShadowProjection.from_dict(json.loads(row["payload"]))
        )

    def projection_for(self, decision_id: str) -> ShadowProjection | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM shadow_projections WHERE decision_id = ?",
                (str(decision_id),),
            ).fetchone()
        return (
            None if row is None else ShadowProjection.from_dict(json.loads(row["payload"]))
        )

    def projections(self, session_id: str, *, limit: int = 200) -> list[ShadowProjection]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM shadow_projections WHERE session_id = ?"
                " ORDER BY rowid ASC LIMIT ?",
                (str(session_id), max(1, min(int(limit), 1000))),
            ).fetchall()
        return [ShadowProjection.from_dict(json.loads(row["payload"])) for row in rows]

    # -- outcomes -------------------------------------------------------------
    def save_outcome(self, outcome: ShadowOutcome) -> ShadowOutcome:
        with self._lock:
            existing = self._connection.execute(
                "SELECT outcome_id FROM shadow_outcomes"
                " WHERE decision_id = ? AND interval = ?",
                (outcome.decision_id, outcome.interval),
            ).fetchone()
            outcome_id = str(existing["outcome_id"]) if existing else outcome.outcome_id
            payload = outcome.to_dict()
            payload["outcome_id"] = outcome_id
            self._connection.execute(
                "INSERT OR REPLACE INTO shadow_outcomes"
                " (outcome_id, decision_id, session_id, interval, observed_at, payload)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    outcome_id,
                    outcome.decision_id,
                    outcome.session_id,
                    outcome.interval,
                    outcome.observed_at.isoformat() if outcome.observed_at else None,
                    json.dumps(payload, sort_keys=True, default=str),
                ),
            )
            self._connection.commit()
        return outcome

    def outcomes(
        self, session_id: str, *, decision_id: str = "", limit: int = 1000
    ) -> list[ShadowOutcome]:
        sql = "SELECT payload FROM shadow_outcomes WHERE session_id = ?"
        params: list[Any] = [str(session_id)]
        if decision_id:
            sql += " AND decision_id = ?"
            params.append(str(decision_id))
        sql += " ORDER BY decision_id ASC, interval ASC LIMIT ?"
        params.append(max(1, min(int(limit), 5000)))
        with self._lock:
            rows = self._connection.execute(sql, tuple(params)).fetchall()
        return [ShadowOutcome.from_dict(json.loads(row["payload"])) for row in rows]

    # -- errors ---------------------------------------------------------------
    def save_error(
        self,
        *,
        error_id: str,
        session_id: str,
        kind: str,
        at: datetime,
        payload: dict[str, Any],
    ) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO shadow_errors"
                " (error_id, session_id, at, kind, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    error_id,
                    session_id,
                    at.isoformat(),
                    kind,
                    json.dumps(payload, sort_keys=True, default=str),
                ),
            )
            self._connection.commit()

    def errors(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM shadow_errors WHERE session_id = ?"
                " ORDER BY at DESC LIMIT ?",
                (str(session_id), max(1, min(int(limit), 500))),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    # -- comparisons ----------------------------------------------------------
    def save_comparison(self, comparison: ShadowComparison) -> ShadowComparison:
        with self._lock:
            self._connection.execute(
                "INSERT INTO shadow_comparisons"
                " (comparison_id, session_id, payload) VALUES (?, ?, ?)",
                (
                    comparison.comparison_id,
                    comparison.session_id,
                    json.dumps(comparison.to_dict(), sort_keys=True, default=str),
                ),
            )
            self._connection.commit()
        return comparison

    def comparisons(self, session_id: str, *, limit: int = 100) -> list[ShadowComparison]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM shadow_comparisons WHERE session_id = ?"
                " ORDER BY rowid DESC LIMIT ?",
                (str(session_id), max(1, min(int(limit), 500))),
            ).fetchall()
        return [ShadowComparison.from_dict(json.loads(row["payload"])) for row in rows]

    # -- lifecycle ------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                self._connection.close()
            except Exception:  # noqa: BLE001 - shutdown must continue
                pass

    def ping(self) -> bool:
        with self._lock:
            try:
                self._connection.execute("SELECT 1").fetchone()
                return True
            except Exception:  # noqa: BLE001
                return False


__all__ = ["ShadowStore"]
