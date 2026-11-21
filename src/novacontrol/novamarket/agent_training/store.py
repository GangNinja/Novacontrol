"""Isolated SQLite persistence for PI-15 training records.

The database is the same research database used by PI-13/PI-14.  All PI-15
tables are prefixed ``rl_train_`` and no table references a production
portfolio, order, ledger, paper fill or autonomy record.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .models import (
    CheckpointRecord,
    OptimizationTrial,
    PolicyArtifact,
    TrainingConfig,
    TrainingRun,
    TrainingStoreError,
    utcnow,
)


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _loads(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class TrainingStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            self.path, check_same_thread=False, timeout=30.0
        )
        try:
            with self._lock:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute("PRAGMA foreign_keys=ON")
                self._conn.execute("PRAGMA busy_timeout=30000")
                self._create_schema()
        except Exception:
            self._conn.close()
            self._conn = None
            raise

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise TrainingStoreError("training store is closed")
        return self._conn

    def _create_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS rl_train_configs (
                config_id TEXT PRIMARY KEY,
                config_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                algorithm TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rl_train_runs (
                run_id TEXT PRIMARY KEY,
                config_id TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rl_train_runs_status
                ON rl_train_runs(status, updated_at);
            CREATE TABLE IF NOT EXISTS rl_train_checkpoints (
                checkpoint_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rl_train_checkpoints_run
                ON rl_train_checkpoints(run_id, created_at);
            CREATE TABLE IF NOT EXISTS rl_train_policies (
                policy_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                status TEXT NOT NULL,
                artifact_hash TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rl_train_policies_run
                ON rl_train_policies(run_id, created_at);
            CREATE TABLE IF NOT EXISTS rl_train_evaluations (
                evaluation_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                policy_id TEXT NOT NULL,
                split TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rl_train_evaluations_policy
                ON rl_train_evaluations(policy_id, created_at);
            CREATE TABLE IF NOT EXISTS rl_train_trials (
                trial_id TEXT PRIMARY KEY,
                search_id TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rl_train_trials_search
                ON rl_train_trials(search_id, created_at);
            """
        )
        self._db.commit()

    # -- configurations -------------------------------------------------------
    def save_config(self, config: TrainingConfig) -> dict[str, Any]:
        now = utcnow().isoformat()
        payload = config.to_dict()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO rl_train_configs
                    (config_id, config_hash, name, algorithm, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(config_id) DO NOTHING
                """,
                (
                    config.config_id,
                    config.config_hash,
                    config.name,
                    config.algorithm,
                    _json(payload),
                    now,
                ),
            )
            self._db.commit()
        return payload

    def config(self, config_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM rl_train_configs WHERE config_id = ?",
                (str(config_id),),
            ).fetchone()
        return _loads(row[0]) if row else None

    def configs(self, limit: int = 50) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 200))
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM rl_train_configs ORDER BY created_at DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return {"configs": [_loads(row[0]) for row in rows], "count": len(rows)}

    # -- runs -----------------------------------------------------------------
    def save_run(self, run: TrainingRun) -> dict[str, Any]:
        payload = run.to_dict()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO rl_train_runs
                    (run_id, config_id, status, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    config_id = excluded.config_id,
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    run.run_id,
                    run.config_id,
                    run.status,
                    _json(payload),
                    run.created_at,
                    run.updated_at,
                ),
            )
            self._db.commit()
        return payload

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM rl_train_runs WHERE run_id = ?", (str(run_id),)
            ).fetchone()
        return _loads(row[0]) if row else None

    def interrupted_runs(self, limit: int = 500) -> list[dict[str, Any]]:
        """Rows left mid-flight by a previous process, newest first."""
        bounded = max(1, min(int(limit), 2_000))
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM rl_train_runs WHERE status NOT IN "
                "('completed','failed','cancelled') ORDER BY updated_at ASC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [_loads(row[0]) for row in rows]

    def runs(self, limit: int = 50, *, status: str = "") -> dict[str, Any]:
        bounded = max(1, min(int(limit), 200))
        with self._lock:
            if status:
                rows = self._db.execute(
                    "SELECT payload FROM rl_train_runs WHERE status = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (str(status), bounded),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT payload FROM rl_train_runs ORDER BY created_at DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
        return {"runs": [_loads(row[0]) for row in rows], "count": len(rows)}

    # -- checkpoints ----------------------------------------------------------
    def save_checkpoint(self, checkpoint: CheckpointRecord) -> dict[str, Any]:
        payload = checkpoint.to_dict()
        with self._lock:
            count = self._db.execute(
                "SELECT COUNT(*) FROM rl_train_checkpoints"
            ).fetchone()[0]
            if count >= 2_000:
                raise TrainingStoreError("checkpoint record limit reached; archive old runs first")
            self._db.execute(
                """
                INSERT OR REPLACE INTO rl_train_checkpoints
                    (checkpoint_id, run_id, kind, path, integrity_hash, size_bytes,
                     status, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.checkpoint_id,
                    checkpoint.run_id,
                    checkpoint.kind,
                    checkpoint.path,
                    checkpoint.integrity_hash,
                    checkpoint.size_bytes,
                    checkpoint.status,
                    _json(payload),
                    checkpoint.created_at,
                ),
            )
            self._db.commit()
        return payload

    def checkpoint(self, checkpoint_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM rl_train_checkpoints WHERE checkpoint_id = ?",
                (str(checkpoint_id),),
            ).fetchone()
        return _loads(row[0]) if row else None

    def delete_checkpoint(self, checkpoint_id: str) -> bool:
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM rl_train_checkpoints WHERE checkpoint_id = ?",
                (str(checkpoint_id),),
            )
            self._db.commit()
        return cursor.rowcount > 0

    def checkpoints(self, run_id: str = "", limit: int = 100) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 500))
        with self._lock:
            if run_id:
                rows = self._db.execute(
                    "SELECT payload FROM rl_train_checkpoints WHERE run_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (str(run_id), bounded),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT payload FROM rl_train_checkpoints ORDER BY created_at DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
        return {"checkpoints": [_loads(row[0]) for row in rows], "count": len(rows)}

    # -- policies -------------------------------------------------------------
    def save_policy(self, policy: PolicyArtifact) -> dict[str, Any]:
        payload = policy.to_dict()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO rl_train_policies
                    (policy_id, run_id, status, artifact_hash, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    status = excluded.status,
                    artifact_hash = excluded.artifact_hash,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    policy.policy_id,
                    policy.run_id,
                    policy.status,
                    policy.artifact_hash,
                    _json(payload),
                    policy.created_at,
                    policy.updated_at,
                ),
            )
            self._db.commit()
        return payload

    def policy(self, policy_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM rl_train_policies WHERE policy_id = ?",
                (str(policy_id),),
            ).fetchone()
        return _loads(row[0]) if row else None

    def policies(self, limit: int = 50, *, status: str = "") -> dict[str, Any]:
        bounded = max(1, min(int(limit), 200))
        with self._lock:
            if status:
                rows = self._db.execute(
                    "SELECT payload FROM rl_train_policies WHERE status = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (str(status), bounded),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT payload FROM rl_train_policies ORDER BY created_at DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
        return {"policies": [_loads(row[0]) for row in rows], "count": len(rows)}

    # -- validation/test evidence --------------------------------------------
    def save_evaluation(
        self,
        *,
        evaluation_id: str,
        run_id: str,
        policy_id: str,
        split: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        record = dict(payload)
        record.setdefault("evaluation_id", evaluation_id)
        record.setdefault("run_id", run_id)
        record.setdefault("policy_id", policy_id)
        record.setdefault("split", split)
        with self._lock:
            count = self._db.execute(
                "SELECT COUNT(*) FROM rl_train_evaluations"
            ).fetchone()[0]
            if count >= 1_000:
                raise TrainingStoreError("training evaluation record limit reached")
            self._db.execute(
                """
                INSERT OR REPLACE INTO rl_train_evaluations
                    (evaluation_id, run_id, policy_id, split, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    run_id,
                    policy_id,
                    split,
                    _json(record),
                    utcnow().isoformat(),
                ),
            )
            self._db.commit()
        return record

    def evaluations(self, policy_id: str = "", limit: int = 50) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 200))
        with self._lock:
            if policy_id:
                rows = self._db.execute(
                    "SELECT payload FROM rl_train_evaluations WHERE policy_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (str(policy_id), bounded),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT payload FROM rl_train_evaluations ORDER BY created_at DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
        return {"evaluations": [_loads(row[0]) for row in rows], "count": len(rows)}

    def evaluation(self, evaluation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM rl_train_evaluations WHERE evaluation_id = ?",
                (str(evaluation_id),),
            ).fetchone()
        return _loads(row[0]) if row else None

    # -- trials ---------------------------------------------------------------
    def save_trial(self, trial: OptimizationTrial) -> dict[str, Any]:
        payload = trial.to_dict()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO rl_train_trials
                    (trial_id, search_id, status, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(trial_id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    trial.trial_id,
                    trial.search_id,
                    trial.status,
                    _json(payload),
                    trial.created_at,
                    trial.updated_at,
                ),
            )
            self._db.commit()
        return payload

    def trials(self, search_id: str = "", limit: int = 100) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 500))
        with self._lock:
            if search_id:
                rows = self._db.execute(
                    "SELECT payload FROM rl_train_trials WHERE search_id = ? "
                    "ORDER BY created_at ASC LIMIT ?",
                    (str(search_id), bounded),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT payload FROM rl_train_trials ORDER BY created_at DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
        return {"trials": [_loads(row[0]) for row in rows], "count": len(rows)}

    # -- status ---------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                table.removeprefix("rl_train_"): int(
                    self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in (
                    "rl_train_configs",
                    "rl_train_runs",
                    "rl_train_checkpoints",
                    "rl_train_policies",
                    "rl_train_evaluations",
                    "rl_train_trials",
                )
            }

    def storage_info(self) -> dict[str, Any]:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            try:
                total += candidate.stat().st_size
            except OSError:
                continue
        return {
            "path": str(self.path),
            "bytes": total,
            "megabytes": round(total / (1024 * 1024), 3),
            "counts": self.counts(),
        }

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.commit()
                self._conn.close()
                self._conn = None


__all__ = ["TrainingStore"]
