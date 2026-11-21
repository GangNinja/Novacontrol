"""Persistence for PI-14 reward configurations, results and policy runs.

The tables live in the same isolated ``novamarket_rl.sqlite3`` file as PI-13
and are all prefixed ``rl_``.  They never share a table or connection with
NM-07 portfolios, NM-10 orders/fills or the wallet ledger.  Reward results are
append-only; changing a configuration produces a different result id.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import (
    MAX_POLICY_COMPARISONS,
    MAX_POLICY_EVALUATIONS,
    MAX_REWARD_CONFIGS,
    MAX_REWARD_RESULTS,
    PolicyEvaluationResult,
    RewardConfig,
    RewardResult,
    RewardValidationError,
)


class RewardStoreError(RuntimeError):
    """The experimental reward store refused or failed a read/write."""


class RewardStore:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        if self._path != ":memory:":
            Path(self._path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        try:
            self._connection = sqlite3.connect(self._path, check_same_thread=False, timeout=30.0)
            self._connection.row_factory = sqlite3.Row
            if self._path != ":memory:":
                self._connection.execute("PRAGMA journal_mode=WAL")
                self._connection.execute("PRAGMA synchronous=NORMAL")
            self._create_schema()
        except sqlite3.Error as exc:
            raise RewardStoreError(f"cannot open reward store: {exc}") from exc

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS rl_reward_configs (
                    config_id TEXT PRIMARY KEY,
                    config_hash TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rl_reward_results (
                    reward_id TEXT PRIMARY KEY,
                    transition_id TEXT NOT NULL,
                    episode_id TEXT NOT NULL,
                    config_id TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    result_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(transition_id, config_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_rl_reward_results_episode
                    ON rl_reward_results(episode_id, created_at);
                CREATE TABLE IF NOT EXISTS rl_policy_evaluations (
                    evaluation_id TEXT PRIMARY KEY,
                    config_hash TEXT NOT NULL,
                    split_name TEXT NOT NULL,
                    policy_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rl_policy_evaluations_config
                    ON rl_policy_evaluations(config_hash, created_at DESC);
                CREATE TABLE IF NOT EXISTS rl_policy_comparisons (
                    comparison_id TEXT PRIMARY KEY,
                    config_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )

    def close(self) -> None:
        with self._lock, contextlib.suppress(sqlite3.Error):
            self._connection.close()

    # -- reward configurations ------------------------------------------------
    def save_config(
        self, config: RewardConfig, *, created_at: datetime | None = None
    ) -> dict[str, Any]:
        payload = config.to_dict()
        now = (created_at or datetime.now(UTC)).isoformat()
        try:
            with self._lock, self._connection:
                existing = self._connection.execute(
                    "SELECT payload_json FROM rl_reward_configs WHERE config_id=?",
                    (config.config_id,),
                ).fetchone()
                if existing:
                    return dict(json.loads(existing["payload_json"]))
                count = int(
                    self._connection.execute("SELECT COUNT(*) FROM rl_reward_configs").fetchone()[0]
                )
                if count >= MAX_REWARD_CONFIGS:
                    raise RewardStoreError(
                        f"reward configuration storage limit reached ({MAX_REWARD_CONFIGS})"
                    )
                self._connection.execute(
                    "INSERT INTO rl_reward_configs"
                    "(config_id, config_hash, name, version, created_at, payload_json)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        config.config_id,
                        config.config_hash,
                        config.name,
                        config.version,
                        now,
                        _json(payload),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise RewardStoreError("reward configuration hash already exists") from exc
        except sqlite3.Error as exc:
            raise RewardStoreError(f"cannot save reward configuration: {exc}") from exc
        return payload

    def config(self, config_id: str) -> dict[str, Any] | None:
        try:
            with self._lock:
                row = self._connection.execute(
                    "SELECT payload_json FROM rl_reward_configs WHERE config_id=?",
                    (str(config_id),),
                ).fetchone()
        except sqlite3.Error as exc:
            raise RewardStoreError(f"cannot read reward configuration: {exc}") from exc
        return dict(json.loads(row["payload_json"])) if row else None

    def configs(self, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(int(limit), 100))
        try:
            with self._lock:
                total = int(
                    self._connection.execute("SELECT COUNT(*) FROM rl_reward_configs").fetchone()[0]
                )
                rows = self._connection.execute(
                    "SELECT payload_json FROM rl_reward_configs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise RewardStoreError(f"cannot list reward configurations: {exc}") from exc
        return {
            "configurations": [dict(json.loads(row["payload_json"])) for row in rows],
            "total": total,
            "limit": limit,
            "default_experimental": True,
        }

    # -- reward results -------------------------------------------------------
    def save_result(self, result: RewardResult) -> dict[str, Any]:
        payload = result.to_dict()
        try:
            with self._lock, self._connection:
                existing = self._connection.execute(
                    "SELECT payload_json FROM rl_reward_results WHERE reward_id=?",
                    (result.reward_id,),
                ).fetchone()
                if existing:
                    return dict(json.loads(existing["payload_json"]))
                # A repeated evaluation can recreate the same transition and
                # reward configuration with a new calculation timestamp.  The
                # evidence identity is (transition_id, config_hash), so reuse
                # the original append-only result instead of raising an
                # IntegrityError on a duplicate deterministic input.
                existing_identity = self._connection.execute(
                    "SELECT payload_json FROM rl_reward_results "
                    "WHERE transition_id=? AND config_hash=?",
                    (result.transition_id, result.config_hash),
                ).fetchone()
                if existing_identity:
                    return dict(json.loads(existing_identity["payload_json"]))
                count = int(
                    self._connection.execute("SELECT COUNT(*) FROM rl_reward_results").fetchone()[0]
                )
                if count >= MAX_REWARD_RESULTS:
                    raise RewardStoreError(
                        f"reward result storage limit reached ({MAX_REWARD_RESULTS})"
                    )
                self._connection.execute(
                    "INSERT INTO rl_reward_results"
                    "(reward_id, transition_id, episode_id, config_id, config_hash,"
                    " result_hash, created_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        result.reward_id,
                        result.transition_id,
                        result.episode_id,
                        result.config_id,
                        result.config_hash,
                        result.result_hash,
                        result.calculated_at.isoformat(),
                        _json(payload),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise RewardStoreError("reward result identity already exists") from exc
        except sqlite3.Error as exc:
            raise RewardStoreError(f"cannot save reward result: {exc}") from exc
        return payload

    def results(self, episode_id: str, *, limit: int = 200) -> dict[str, Any]:
        limit = max(1, min(int(limit), 1_000))
        try:
            with self._lock:
                total = int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM rl_reward_results WHERE episode_id=?",
                        (str(episode_id),),
                    ).fetchone()[0]
                )
                rows = self._connection.execute(
                    "SELECT payload_json FROM rl_reward_results WHERE episode_id=?"
                    " ORDER BY created_at ASC LIMIT ?",
                    (str(episode_id), limit),
                ).fetchall()
        except sqlite3.Error as exc:
            raise RewardStoreError(f"cannot read reward results: {exc}") from exc
        return {
            "episode_id": str(episode_id),
            "rewards": [dict(json.loads(row["payload_json"])) for row in rows],
            "total": total,
            "limit": limit,
            "append_only": True,
        }

    def result(self, reward_id: str) -> dict[str, Any] | None:
        try:
            with self._lock:
                row = self._connection.execute(
                    "SELECT payload_json FROM rl_reward_results WHERE reward_id=?",
                    (str(reward_id),),
                ).fetchone()
        except sqlite3.Error as exc:
            raise RewardStoreError(f"cannot read reward result: {exc}") from exc
        return dict(json.loads(row["payload_json"])) if row else None

    # -- policy evaluation runs ----------------------------------------------
    def save_evaluation(
        self, result: PolicyEvaluationResult, *, updated_at: datetime | None = None
    ) -> dict[str, Any]:
        payload = result.to_dict()
        now = (updated_at or datetime.now(UTC)).isoformat()
        try:
            with self._lock, self._connection:
                exists = self._connection.execute(
                    "SELECT 1 FROM rl_policy_evaluations WHERE evaluation_id=?",
                    (result.evaluation_id,),
                ).fetchone()
                if exists is None:
                    count = int(
                        self._connection.execute(
                            "SELECT COUNT(*) FROM rl_policy_evaluations"
                        ).fetchone()[0]
                    )
                    if count >= MAX_POLICY_EVALUATIONS:
                        raise RewardStoreError(
                            f"policy evaluation storage limit reached ({MAX_POLICY_EVALUATIONS})"
                        )
                self._connection.execute(
                    "INSERT INTO rl_policy_evaluations"
                    "(evaluation_id, config_hash, split_name, policy_name, status,"
                    " created_at, updated_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(evaluation_id) DO UPDATE SET"
                    " status=excluded.status, updated_at=excluded.updated_at,"
                    " payload_json=excluded.payload_json",
                    (
                        result.evaluation_id,
                        result.config_hash,
                        result.split,
                        result.policy_name,
                        result.status,
                        result.created_at.isoformat(),
                        now,
                        _json(payload),
                    ),
                )
        except sqlite3.Error as exc:
            raise RewardStoreError(f"cannot save policy evaluation: {exc}") from exc
        return payload

    def evaluation(self, evaluation_id: str) -> dict[str, Any] | None:
        try:
            with self._lock:
                row = self._connection.execute(
                    "SELECT payload_json FROM rl_policy_evaluations WHERE evaluation_id=?",
                    (str(evaluation_id),),
                ).fetchone()
        except sqlite3.Error as exc:
            raise RewardStoreError(f"cannot read policy evaluation: {exc}") from exc
        return dict(json.loads(row["payload_json"])) if row else None

    def evaluations(self, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        try:
            with self._lock:
                total = int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM rl_policy_evaluations"
                    ).fetchone()[0]
                )
                rows = self._connection.execute(
                    "SELECT payload_json FROM rl_policy_evaluations"
                    " ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise RewardStoreError(f"cannot list policy evaluations: {exc}") from exc
        return {
            "evaluations": [dict(json.loads(row["payload_json"])) for row in rows],
            "total": total,
            "limit": limit,
        }

    def save_comparison(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(payload)
        comparison_id = str(data.get("comparison_id") or "")
        if not comparison_id:
            raise RewardValidationError("comparison_id is required")
        try:
            with self._lock, self._connection:
                existing = self._connection.execute(
                    "SELECT 1 FROM rl_policy_comparisons WHERE comparison_id=?",
                    (comparison_id,),
                ).fetchone()
                if existing is None:
                    count = int(
                        self._connection.execute(
                            "SELECT COUNT(*) FROM rl_policy_comparisons"
                        ).fetchone()[0]
                    )
                    if count >= MAX_POLICY_COMPARISONS:
                        raise RewardStoreError(
                            f"policy comparison storage limit reached ({MAX_POLICY_COMPARISONS})"
                        )
                self._connection.execute(
                    "INSERT OR IGNORE INTO rl_policy_comparisons"
                    "(comparison_id, config_hash, created_at, payload_json) VALUES (?, ?, ?, ?)",
                    (
                        comparison_id,
                        str(data.get("config_hash") or ""),
                        str(data.get("created_at") or datetime.now(UTC).isoformat()),
                        _json(data),
                    ),
                )
        except sqlite3.Error as exc:
            raise RewardStoreError(f"cannot save policy comparison: {exc}") from exc
        return data

    def comparisons(self, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        try:
            with self._lock:
                rows = self._connection.execute(
                    "SELECT payload_json FROM rl_policy_comparisons"
                    " ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise RewardStoreError(f"cannot list policy comparisons: {exc}") from exc
        return {"comparisons": [dict(json.loads(row["payload_json"])) for row in rows]}

    # -- health ---------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        try:
            with self._lock:
                configs = int(
                    self._connection.execute("SELECT COUNT(*) FROM rl_reward_configs").fetchone()[0]
                )
                results = int(
                    self._connection.execute("SELECT COUNT(*) FROM rl_reward_results").fetchone()[0]
                )
                evaluations = int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM rl_policy_evaluations"
                    ).fetchone()[0]
                )
                comparisons = int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM rl_policy_comparisons"
                    ).fetchone()[0]
                )
        except sqlite3.Error as exc:
            raise RewardStoreError(f"cannot read reward store status: {exc}") from exc
        return {
            "path": self._path,
            "reward_configurations": configs,
            "reward_results": results,
            "policy_evaluations": evaluations,
            "policy_comparisons": comparisons,
            "max_reward_configurations": MAX_REWARD_CONFIGS,
            "max_reward_results": MAX_REWARD_RESULTS,
            "max_policy_evaluations": MAX_POLICY_EVALUATIONS,
            "max_policy_comparisons": MAX_POLICY_COMPARISONS,
            "isolated_experimental_database": True,
            "append_only_reward_results": True,
            "production_financial_tables": 0,
        }


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


__all__ = ["RewardStore", "RewardStoreError"]
