"""The isolated NM-13 SQLite boundary.

Learning records live in their own database.  Evidence, versions, approvals and
rollbacks are append-only (triggers refuse UPDATE/DELETE); experiments,
candidates and alerts are mutable because their status is the workflow.  No
table here is a strategy, risk, execution, wallet or shadow table.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from novacontrol.novamarket.learning.models import (
    EvidenceRecord,
    LearningAlert,
    LearningCandidate,
    LearningExperiment,
    RollbackRecord,
    StrategyVersionRecord,
    utcnow,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS learning_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learning_experiments (
    experiment_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS learning_experiments_strategy
    ON learning_experiments(strategy_id, created_at);
CREATE TABLE IF NOT EXISTS learning_candidates (
    candidate_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    status TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (experiment_id, config_digest)
);
CREATE INDEX IF NOT EXISTS learning_candidates_strategy
    ON learning_candidates(strategy_id, created_at);
CREATE TABLE IF NOT EXISTS learning_versions (
    version_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    parent_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS learning_versions_strategy
    ON learning_versions(strategy_id, created_at);
CREATE TABLE IF NOT EXISTS learning_active (
    strategy_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learning_evidence (
    evidence_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_digest TEXT NOT NULL UNIQUE,
    received_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS learning_evidence_strategy
    ON learning_evidence(strategy_id, kind, received_at);
CREATE TABLE IF NOT EXISTS learning_alerts (
    alert_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS learning_alerts_strategy
    ON learning_alerts(strategy_id, created_at);
CREATE TABLE IF NOT EXISTS learning_rollbacks (
    rollback_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    from_version TEXT NOT NULL,
    to_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learning_approvals (
    approval_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS learning_approvals_candidate
    ON learning_approvals(candidate_id, at);
CREATE TABLE IF NOT EXISTS learning_shadow_validations (
    validation_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS learning_shadow_validations_candidate
    ON learning_shadow_validations(candidate_id, started_at);
CREATE TRIGGER IF NOT EXISTS learning_versions_no_update
BEFORE UPDATE ON learning_versions
BEGIN SELECT RAISE(ABORT, 'learning versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS learning_versions_no_delete
BEFORE DELETE ON learning_versions
BEGIN SELECT RAISE(ABORT, 'learning versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS learning_evidence_no_update
BEFORE UPDATE ON learning_evidence
BEGIN SELECT RAISE(ABORT, 'learning evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS learning_evidence_no_delete
BEFORE DELETE ON learning_evidence
BEGIN SELECT RAISE(ABORT, 'learning evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS learning_approvals_no_update
BEFORE UPDATE ON learning_approvals
BEGIN SELECT RAISE(ABORT, 'learning approvals are immutable'); END;
CREATE TRIGGER IF NOT EXISTS learning_approvals_no_delete
BEFORE DELETE ON learning_approvals
BEGIN SELECT RAISE(ABORT, 'learning approvals are immutable'); END;
CREATE TRIGGER IF NOT EXISTS learning_rollbacks_no_update
BEFORE UPDATE ON learning_rollbacks
BEGIN SELECT RAISE(ABORT, 'learning rollbacks are immutable'); END;
CREATE TRIGGER IF NOT EXISTS learning_rollbacks_no_delete
BEFORE DELETE ON learning_rollbacks
BEGIN SELECT RAISE(ABORT, 'learning rollbacks are immutable'); END;
"""


class LearningStore:
    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = str(path)
        self.schema_version = SCHEMA_VERSION
        self._clock = clock or utcnow
        self._lock = threading.RLock()
        self._depth = 0
        self._connection = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._connection.execute(
                "INSERT OR IGNORE INTO learning_meta(key, value) VALUES(?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self._connection.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            finally:
                self._depth = 0

    # -- experiments ----------------------------------------------------------
    def save_experiment(self, record: LearningExperiment) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO learning_experiments(experiment_id, strategy_id, status, "
                "idempotency_key, created_at, payload) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(experiment_id) DO UPDATE SET status=excluded.status, "
                "payload=excluded.payload",
                (
                    record.experiment_id,
                    record.strategy_id,
                    record.status.value,
                    record.idempotency_key,
                    record.created_at.isoformat(),
                    json.dumps(record.to_dict(), sort_keys=True),
                ),
            )

    def experiment(self, experiment_id: str) -> LearningExperiment | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM learning_experiments WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
        return LearningExperiment.from_dict(json.loads(row["payload"])) if row else None

    def experiment_by_key(self, key: str) -> LearningExperiment | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM learning_experiments WHERE idempotency_key=?",
                (key,),
            ).fetchone()
        return LearningExperiment.from_dict(json.loads(row["payload"])) if row else None

    def experiments(self, *, limit: int = 50) -> list[LearningExperiment]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM learning_experiments "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [LearningExperiment.from_dict(json.loads(row["payload"])) for row in rows]

    # -- candidates -----------------------------------------------------------
    def save_candidate(self, record: LearningCandidate) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO learning_candidates(candidate_id, experiment_id, strategy_id, "
                "status, config_digest, created_at, payload) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(candidate_id) DO UPDATE SET status=excluded.status, "
                "payload=excluded.payload",
                (
                    record.candidate_id,
                    record.experiment_id,
                    record.strategy_id,
                    record.status.value,
                    record.config_digest,
                    record.created_at.isoformat(),
                    json.dumps(record.to_dict(), sort_keys=True),
                ),
            )

    def candidate(self, candidate_id: str) -> LearningCandidate | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM learning_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
        return LearningCandidate.from_dict(json.loads(row["payload"])) if row else None

    def candidates(
        self, *, experiment_id: str = "", strategy_id: str = "", limit: int = 200
    ) -> list[LearningCandidate]:
        clauses: list[str] = []
        params: list[Any] = []
        if experiment_id:
            clauses.append("experiment_id=?")
            params.append(experiment_id)
        if strategy_id:
            clauses.append("strategy_id=?")
            params.append(strategy_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM learning_candidates" + where +
                " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [LearningCandidate.from_dict(json.loads(row["payload"])) for row in rows]

    def candidate_for_digest(
        self, experiment_id: str, config_digest: str
    ) -> LearningCandidate | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM learning_candidates "
                "WHERE experiment_id=? AND config_digest=?",
                (experiment_id, config_digest),
            ).fetchone()
        return LearningCandidate.from_dict(json.loads(row["payload"])) if row else None

    # -- versions and the active pointer -------------------------------------
    def save_version(self, record: StrategyVersionRecord) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO learning_versions(version_id, strategy_id, parent_version, "
                "created_at, payload) VALUES (?,?,?,?,?)",
                (
                    record.version_id,
                    record.strategy_id,
                    record.parent_version,
                    record.created_at.isoformat(),
                    json.dumps(record.to_dict(), sort_keys=True),
                ),
            )

    def version(self, version_id: str) -> StrategyVersionRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM learning_versions WHERE version_id=?",
                (version_id,),
            ).fetchone()
        return StrategyVersionRecord.from_dict(json.loads(row["payload"])) if row else None

    def versions(self, strategy_id: str) -> list[StrategyVersionRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM learning_versions WHERE strategy_id=? "
                "ORDER BY created_at ASC, rowid ASC",
                (strategy_id,),
            ).fetchall()
        return [StrategyVersionRecord.from_dict(json.loads(row["payload"])) for row in rows]

    def active_version(self, strategy_id: str) -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT version_id FROM learning_active WHERE strategy_id=?",
                (strategy_id,),
            ).fetchone()
        return str(row["version_id"]) if row else ""

    def strategy_ids(self) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT DISTINCT strategy_id FROM learning_versions ORDER BY strategy_id"
            ).fetchall()
        return [str(row["strategy_id"]) for row in rows]

    def active_rows(self) -> dict[str, str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT strategy_id, version_id FROM learning_active ORDER BY strategy_id"
            ).fetchall()
        return {str(row["strategy_id"]): str(row["version_id"]) for row in rows}

    def set_active(self, strategy_id: str, version_id: str) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO learning_active(strategy_id, version_id, updated_at) "
                "VALUES (?,?,?) ON CONFLICT(strategy_id) DO UPDATE SET "
                "version_id=excluded.version_id, updated_at=excluded.updated_at",
                (strategy_id, version_id, self._clock().isoformat()),
            )

    # -- evidence -------------------------------------------------------------
    def save_evidence(self, record: EvidenceRecord) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO learning_evidence(evidence_id, kind, strategy_id, status, "
                "evidence_digest, received_at, payload) VALUES (?,?,?,?,?,?,?)",
                (
                    record.evidence_id,
                    record.kind.value,
                    record.strategy_id,
                    record.status.value,
                    record.evidence_digest,
                    record.received_at.isoformat(),
                    json.dumps(record.to_dict(), sort_keys=True),
                ),
            )

    def evidence_by_digest(self, value: str) -> EvidenceRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM learning_evidence WHERE evidence_digest=?",
                (value,),
            ).fetchone()
        return EvidenceRecord.from_dict(json.loads(row["payload"])) if row else None

    def evidence(self, evidence_id: str) -> EvidenceRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM learning_evidence WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()
        return EvidenceRecord.from_dict(json.loads(row["payload"])) if row else None

    def evidence_rows(
        self,
        *,
        strategy_id: str = "",
        kind: str = "",
        status: str = "",
        limit: int = 200,
    ) -> list[EvidenceRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("strategy_id", strategy_id),
            ("kind", kind),
            ("status", status),
        ):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM learning_evidence" + where +
                " ORDER BY received_at DESC, rowid DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [EvidenceRecord.from_dict(json.loads(row["payload"])) for row in rows]

    # -- alerts ---------------------------------------------------------------
    def save_alert(self, record: LearningAlert) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO learning_alerts(alert_id, strategy_id, kind, severity, "
                "status, created_at, payload) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(alert_id) DO UPDATE SET status=excluded.status, "
                "payload=excluded.payload",
                (
                    record.alert_id,
                    record.strategy_id,
                    record.kind.value,
                    record.severity.value,
                    record.status,
                    record.created_at.isoformat(),
                    json.dumps(record.to_dict(), sort_keys=True),
                ),
            )

    def alert(self, alert_id: str) -> LearningAlert | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM learning_alerts WHERE alert_id=?",
                (alert_id,),
            ).fetchone()
        return LearningAlert.from_dict(json.loads(row["payload"])) if row else None

    def alerts(
        self, *, strategy_id: str = "", status: str = "", limit: int = 100
    ) -> list[LearningAlert]:
        clauses: list[str] = []
        params: list[Any] = []
        if strategy_id:
            clauses.append("strategy_id=?")
            params.append(strategy_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM learning_alerts" + where +
                " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [LearningAlert.from_dict(json.loads(row["payload"])) for row in rows]

    def acknowledge_alert(self, alert_id: str, actor: str) -> LearningAlert | None:
        record = self.alert(alert_id)
        if record is None:
            return None
        updated = LearningAlert(
            alert_id=record.alert_id,
            strategy_id=record.strategy_id,
            kind=record.kind,
            severity=record.severity,
            message=record.message,
            metrics=dict(record.metrics),
            evidence_refs=record.evidence_refs,
            status="acknowledged",
            created_at=record.created_at,
            acknowledged_at=self._clock(),
            acknowledged_by=actor,
        )
        self.save_alert(updated)
        return updated

    # -- rollbacks and approvals ---------------------------------------------
    def save_rollback(self, record: RollbackRecord) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO learning_rollbacks(rollback_id, strategy_id, from_version, "
                "to_version, created_at, payload) VALUES (?,?,?,?,?,?)",
                (
                    record.rollback_id,
                    record.strategy_id,
                    record.from_version,
                    record.to_version,
                    record.at.isoformat(),
                    json.dumps(record.to_dict(), sort_keys=True),
                ),
            )

    def rollbacks(self, strategy_id: str = "", limit: int = 50) -> list[RollbackRecord]:
        where = " WHERE strategy_id=?" if strategy_id else ""
        params: tuple[Any, ...] = (
            (strategy_id, max(1, min(int(limit), 500)))
            if strategy_id
            else (max(1, min(int(limit), 500)),)
        )
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM learning_rollbacks" + where +
                " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                params,
            ).fetchall()
        return [RollbackRecord.from_dict(json.loads(row["payload"])) for row in rows]

    def save_approval(
        self, approval_id: str, candidate_id: str, decision: str, payload: dict[str, Any]
    ) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO learning_approvals(approval_id, candidate_id, decision, "
                "at, payload) VALUES (?,?,?,?,?)",
                (
                    approval_id,
                    candidate_id,
                    decision,
                    self._clock().isoformat(),
                    json.dumps(payload, sort_keys=True, default=str),
                ),
            )

    def approvals(self, candidate_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM learning_approvals WHERE candidate_id=? "
                "ORDER BY at ASC, rowid ASC",
                (candidate_id,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    # -- shadow validation ----------------------------------------------------
    def save_shadow_validation(self, record: Mapping[str, Any]) -> None:
        """Persist one shadow-validation attempt before it can complete.

        This is a separate row, not a field on the mutable candidate: a process
        that dies while NM-12 is running leaves a visible ``running`` row rather
        than a candidate whose state silently reverts.
        """
        payload = dict(record)
        with self.transaction():
            self._connection.execute(
                "INSERT INTO learning_shadow_validations(validation_id, candidate_id, "
                "status, started_at, completed_at, payload) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(validation_id) DO UPDATE SET status=excluded.status, "
                "completed_at=excluded.completed_at, payload=excluded.payload",
                (
                    str(payload["validation_id"]),
                    str(payload["candidate_id"]),
                    str(payload["status"]),
                    str(payload["started_at"]),
                    payload.get("completed_at"),
                    json.dumps(payload, sort_keys=True, default=str),
                ),
            )

    def shadow_validations(
        self, *, candidate_id: str = "", limit: int = 50
    ) -> list[dict[str, Any]]:
        with self._lock:
            if candidate_id:
                rows = self._connection.execute(
                    "SELECT payload FROM learning_shadow_validations "
                    "WHERE candidate_id=? ORDER BY started_at DESC, rowid DESC LIMIT ?",
                    (candidate_id, max(1, min(int(limit), 500))),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT payload FROM learning_shadow_validations "
                    "ORDER BY started_at DESC, rowid DESC LIMIT ?",
                    (max(1, min(int(limit), 500)),),
                ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    # -- counts ---------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        tables = {
            "experiments": "learning_experiments",
            "candidates": "learning_candidates",
            "versions": "learning_versions",
            "evidence": "learning_evidence",
            "alerts": "learning_alerts",
            "rollbacks": "learning_rollbacks",
            "shadow_validations": "learning_shadow_validations",
        }
        result: dict[str, int] = {}
        with self._lock:
            for name, table in tables.items():
                row = self._connection.execute(
                    f"SELECT COUNT(*) AS total FROM {table}"
                ).fetchone()
                result[name] = int(row["total"]) if row else 0
        return result


__all__ = ["LearningStore", "SCHEMA_VERSION"]
