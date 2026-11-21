"""Bounded, isolated SQLite persistence for PI-13 episodes and experiences.

This database is the RL environment's own store (``novamarket_rl.sqlite3`` by
default).  It never shares a connection or a table with NM-07 portfolios, NM-10
paper orders, NM-08 risk decisions or the wallet ledger.  Transition payloads
carry only the RL environment's observations, actions and simulated book, not
copies of the market database.
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
    MAX_ENVIRONMENTS,
    MAX_EXPERIENCES_PER_EPISODE,
    MAX_STORED_EPISODES,
    EpisodeStatus,
    RLEpisode,
    RLStoreError,
    RLTransition,
    parse_time,
)


class RLExperienceStore:
    """One connection, bounded rows and a single atomic step-write transaction."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_experiences_per_episode: int = MAX_EXPERIENCES_PER_EPISODE,
        max_total_experiences: int = 50_000,
    ) -> None:
        self._path = str(path)
        self._max_per_episode = max(1, min(int(max_experiences_per_episode), 50_000))
        self._max_total = max(self._max_per_episode, min(int(max_total_experiences), 250_000))
        self._lock = threading.RLock()
        if self._path != ":memory:":
            parent = Path(self._path).expanduser().resolve().parent
            parent.mkdir(parents=True, exist_ok=True)
        try:
            self._connection = sqlite3.connect(self._path, check_same_thread=False, timeout=30.0)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys=ON")
            if self._path != ":memory:":
                self._connection.execute("PRAGMA journal_mode=WAL")
                self._connection.execute("PRAGMA synchronous=NORMAL")
            self._create_schema()
        except sqlite3.Error as exc:
            raise RLStoreError(f"cannot open RL experience store: {exc}") from exc

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS rl_environments (
                    environment_id TEXT PRIMARY KEY,
                    config_hash TEXT NOT NULL,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rl_episodes (
                    episode_id TEXT PRIMARY KEY,
                    environment_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    transition_count INTEGER NOT NULL DEFAULT 0,
                    invalid_actions INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rl_episodes_environment
                    ON rl_episodes(environment_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS rl_experiences (
                    transition_id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL,
                    environment_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    simulated_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(episode_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_rl_experiences_episode
                    ON rl_experiences(episode_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_rl_experiences_created
                    ON rl_experiences(created_at);
                """
            )

    def close(self) -> None:
        with self._lock, contextlib.suppress(sqlite3.Error):
            self._connection.close()

    # -- environments ---------------------------------------------------------
    def save_environment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(payload)
        environment_id = str(data.get("environment_id") or "").strip()
        config_hash = str(data.get("config_hash") or "").strip()
        if not environment_id or not config_hash:
            raise RLStoreError("environment id and config hash are required")
        now = datetime.now(UTC).isoformat()
        try:
            with self._lock, self._connection:
                existing = self._connection.execute(
                    "SELECT config_hash FROM rl_environments WHERE environment_id=?",
                    (environment_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing["config_hash"]) != config_hash:
                        raise RLStoreError(
                            "environment id already exists with a different immutable configuration"
                        )
                    return dict(data)
                count = int(
                    self._connection.execute("SELECT COUNT(*) FROM rl_environments").fetchone()[0]
                )
                if count >= MAX_ENVIRONMENTS:
                    raise RLStoreError(f"RL environment storage limit reached ({MAX_ENVIRONMENTS})")
                self._connection.execute(
                    "INSERT INTO rl_environments"
                    "(environment_id, config_hash, name, created_at, updated_at, payload_json)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        environment_id,
                        config_hash,
                        str(data.get("name") or ""),
                        now,
                        now,
                        _json(data),
                    ),
                )
        except sqlite3.Error as exc:
            raise RLStoreError(f"cannot save RL environment: {exc}") from exc
        return dict(data)

    def environment(self, environment_id: str) -> dict[str, Any] | None:
        try:
            with self._lock:
                row = self._connection.execute(
                    "SELECT payload_json FROM rl_environments WHERE environment_id=?",
                    (str(environment_id),),
                ).fetchone()
        except sqlite3.Error as exc:
            raise RLStoreError(f"cannot read RL environment: {exc}") from exc
        return dict(json.loads(row["payload_json"])) if row else None

    def environments(self, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        try:
            with self._lock:
                total = int(
                    self._connection.execute("SELECT COUNT(*) FROM rl_environments").fetchone()[0]
                )
                rows = self._connection.execute(
                    "SELECT payload_json FROM rl_environments ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise RLStoreError(f"cannot list RL environments: {exc}") from exc
        return {
            "environments": [dict(json.loads(row["payload_json"])) for row in rows],
            "total": total,
            "limit": limit,
        }

    # -- episodes -------------------------------------------------------------
    def save_episode(self, episode: RLEpisode | Mapping[str, Any]) -> dict[str, Any]:
        data = episode.to_dict() if isinstance(episode, RLEpisode) else dict(episode)
        episode_id = str(data.get("episode_id") or "").strip()
        if not episode_id:
            raise RLStoreError("episode id is required")
        try:
            with self._lock, self._connection:
                exists = self._connection.execute(
                    "SELECT 1 FROM rl_episodes WHERE episode_id=?", (episode_id,)
                ).fetchone()
                if exists is None:
                    count = int(
                        self._connection.execute("SELECT COUNT(*) FROM rl_episodes").fetchone()[0]
                    )
                    if count >= MAX_STORED_EPISODES:
                        raise RLStoreError(
                            f"RL episode storage limit reached ({MAX_STORED_EPISODES})"
                        )
                self._write_episode_row(data)
        except sqlite3.Error as exc:
            raise RLStoreError(f"cannot save RL episode: {exc}") from exc
        return data

    def _write_episode_row(self, data: Mapping[str, Any]) -> None:
        """Upsert one episode row; the caller owns the transaction."""
        episode_id = str(data.get("episode_id") or "").strip()
        environment_id = str(data.get("environment_id") or "").strip()
        if not episode_id or not environment_id:
            raise RLStoreError("episode id and environment id are required")
        status = str(data.get("status") or "")
        created_at = str(data.get("created_at") or datetime.now(UTC).isoformat())
        updated_at = str(data.get("updated_at") or datetime.now(UTC).isoformat())
        transition_count = int(data.get("transition_count") or 0)
        invalid_actions = int(data.get("invalid_actions") or 0)
        error = str(data.get("error") or "")
        self._connection.execute(
            "INSERT INTO rl_episodes"
            "(episode_id, environment_id, status, created_at, updated_at,"
            " transition_count, invalid_actions, error, payload_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(episode_id) DO UPDATE SET"
            " status=excluded.status, updated_at=excluded.updated_at,"
            " transition_count=excluded.transition_count,"
            " invalid_actions=excluded.invalid_actions, error=excluded.error,"
            " payload_json=excluded.payload_json",
            (
                episode_id,
                environment_id,
                status,
                created_at,
                updated_at,
                transition_count,
                invalid_actions,
                error,
                _json(data),
            ),
        )

    def episode(self, episode_id: str) -> dict[str, Any] | None:
        try:
            with self._lock:
                row = self._connection.execute(
                    "SELECT payload_json FROM rl_episodes WHERE episode_id=?",
                    (str(episode_id),),
                ).fetchone()
        except sqlite3.Error as exc:
            raise RLStoreError(f"cannot read RL episode: {exc}") from exc
        return dict(json.loads(row["payload_json"])) if row else None

    def episodes(
        self, *, environment_id: str | None = None, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        clauses = ["1=1"]
        params: list[Any] = []
        if environment_id:
            clauses.append("environment_id=?")
            params.append(str(environment_id))
        where = " AND ".join(clauses)
        try:
            with self._lock:
                total = int(
                    self._connection.execute(
                        f"SELECT COUNT(*) FROM rl_episodes WHERE {where}", params
                    ).fetchone()[0]
                )
                rows = self._connection.execute(
                    f"SELECT payload_json FROM rl_episodes WHERE {where}"
                    " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (*params, limit, offset),
                ).fetchall()
        except sqlite3.Error as exc:
            raise RLStoreError(f"cannot list RL episodes: {exc}") from exc
        return {
            "episodes": [dict(json.loads(row["payload_json"])) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
        }

    def reset_episode(self, episode: RLEpisode | Mapping[str, Any]) -> dict[str, Any]:
        data = episode.to_dict() if isinstance(episode, RLEpisode) else dict(episode)
        episode_id = str(data.get("episode_id") or "")
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    "DELETE FROM rl_experiences WHERE episode_id=?", (episode_id,)
                )
                self._connection.execute(
                    "INSERT INTO rl_episodes"
                    "(episode_id, environment_id, status, created_at, updated_at,"
                    " transition_count, invalid_actions, error, payload_json)"
                    " VALUES (?, ?, ?, ?, ?, 0, 0, '', ?)"
                    " ON CONFLICT(episode_id) DO UPDATE SET"
                    " status=excluded.status, updated_at=excluded.updated_at,"
                    " transition_count=0, invalid_actions=0, error='',"
                    " payload_json=excluded.payload_json",
                    (
                        episode_id,
                        str(data.get("environment_id") or ""),
                        str(data.get("status") or ""),
                        str(data.get("created_at") or datetime.now(UTC).isoformat()),
                        str(data.get("updated_at") or datetime.now(UTC).isoformat()),
                        _json(data),
                    ),
                )
        except sqlite3.Error as exc:
            raise RLStoreError(f"cannot reset RL episode: {exc}") from exc
        return data

    # -- experiences ----------------------------------------------------------
    def record_step(
        self,
        episode: RLEpisode | Mapping[str, Any],
        transition: RLTransition | Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        episode_data = episode.to_dict() if isinstance(episode, RLEpisode) else dict(episode)
        transition_data = (
            transition.to_dict() if isinstance(transition, RLTransition) else dict(transition)
        )
        episode_id = str(episode_data.get("episode_id") or "")
        transition_id = str(transition_data.get("transition_id") or "")
        sequence = int(transition_data.get("sequence") or 0)
        if not episode_id or not transition_id:
            raise RLStoreError("episode and transition identity are required")
        if str(transition_data.get("episode_id") or "") != episode_id:
            raise RLStoreError("transition does not belong to the supplied episode")
        if sequence < 1:
            raise RLStoreError("transition sequence must be positive")
        now = datetime.now(UTC).isoformat()
        try:
            with self._lock, self._connection:
                last = self._connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM rl_experiences WHERE episode_id=?",
                    (episode_id,),
                ).fetchone()[0]
                if sequence != int(last) + 1:
                    raise RLStoreError(
                        f"transition sequence {sequence} is not the next record after {last}"
                    )
                self._connection.execute(
                    "INSERT INTO rl_experiences (transition_id, episode_id, "
                    "environment_id, sequence, simulated_at, created_at, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        transition_id,
                        episode_id,
                        str(transition_data.get("environment_id") or ""),
                        sequence,
                        str(transition_data.get("timestamp") or now),
                        now,
                        _json(transition_data),
                    ),
                )
                self._write_episode_row(episode_data)
                self._prune(episode_id)
        except sqlite3.IntegrityError as exc:
            raise RLStoreError("duplicate RL transition was refused") from exc
        except sqlite3.Error as exc:
            raise RLStoreError(f"cannot record RL transition: {exc}") from exc
        return episode_data, transition_data

    def experiences(self, episode_id: str, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(int(limit), 1_000))
        offset = max(0, int(offset))
        try:
            with self._lock:
                total = int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM rl_experiences WHERE episode_id=?",
                        (str(episode_id),),
                    ).fetchone()[0]
                )
                rows = self._connection.execute(
                    "SELECT payload_json FROM rl_experiences WHERE episode_id=?"
                    " ORDER BY sequence ASC LIMIT ? OFFSET ?",
                    (str(episode_id), limit, offset),
                ).fetchall()
        except sqlite3.Error as exc:
            raise RLStoreError(f"cannot read RL experiences: {exc}") from exc
        return {
            "episode_id": str(episode_id),
            "experiences": [dict(json.loads(row["payload_json"])) for row in rows],
            "transitions": [dict(json.loads(row["payload_json"])) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
            "bounded_storage": True,
        }

    def experience_count(self, episode_id: str) -> int:
        try:
            with self._lock:
                return int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM rl_experiences WHERE episode_id=?",
                        (str(episode_id),),
                    ).fetchone()[0]
                )
        except sqlite3.Error as exc:
            raise RLStoreError(f"cannot count RL experiences: {exc}") from exc

    def _prune(self, episode_id: str) -> None:
        self._connection.execute(
            "DELETE FROM rl_experiences WHERE episode_id=? AND transition_id NOT IN"
            " (SELECT transition_id FROM rl_experiences WHERE episode_id=?"
            " ORDER BY sequence DESC LIMIT ?)",
            (episode_id, episode_id, self._max_per_episode),
        )
        total = int(self._connection.execute("SELECT COUNT(*) FROM rl_experiences").fetchone()[0])
        if total > self._max_total:
            self._connection.execute(
                "DELETE FROM rl_experiences WHERE transition_id IN"
                " (SELECT transition_id FROM rl_experiences"
                " ORDER BY created_at ASC, sequence ASC LIMIT ?)",
                (total - self._max_total,),
            )

    # -- health ---------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        try:
            with self._lock:
                environments = int(
                    self._connection.execute("SELECT COUNT(*) FROM rl_environments").fetchone()[0]
                )
                episodes = int(
                    self._connection.execute("SELECT COUNT(*) FROM rl_episodes").fetchone()[0]
                )
                experiences = int(
                    self._connection.execute("SELECT COUNT(*) FROM rl_experiences").fetchone()[0]
                )
                last_write = self._connection.execute(
                    "SELECT MAX(created_at) FROM rl_experiences"
                ).fetchone()[0]
        except sqlite3.Error as exc:
            raise RLStoreError(f"cannot read RL store status: {exc}") from exc
        size_bytes = 0
        if self._path != ":memory:":
            # WAL mode keeps recent commits in the sidecar until checkpointing;
            # count it so the health figure reflects durable on-disk bytes.
            for suffix in ("", "-wal", "-shm"):
                try:
                    size_bytes += Path(self._path + suffix).stat().st_size
                except OSError:
                    continue
        return {
            "path": self._path,
            "environments": environments,
            "episodes": episodes,
            "experiences": experiences,
            "last_write_at": last_write,
            "size_bytes": size_bytes,
            "max_experiences_per_episode": self._max_per_episode,
            "max_total_experiences": self._max_total,
            "max_environments": MAX_ENVIRONMENTS,
            "max_stored_episodes": MAX_STORED_EPISODES,
            "isolated_database": True,
        }


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def make_episode(
    *,
    episode_id: str,
    environment_id: str,
    status: str,
    config: Mapping[str, Any],
    state: Mapping[str, Any],
    summary: Mapping[str, Any] | None = None,
    transition_count: int = 0,
    invalid_actions: int = 0,
    error: str = "",
    created_at: datetime | str | None = None,
) -> RLEpisode:
    now = datetime.now(UTC)
    created = parse_time(created_at, "created_at") if created_at else now
    return RLEpisode(
        episode_id=episode_id,
        environment_id=environment_id,
        status=EpisodeStatus(status),
        created_at=created,
        updated_at=now,
        config=dict(config),
        state=dict(state),
        summary=dict(summary or {}),
        transition_count=transition_count,
        invalid_actions=invalid_actions,
        error=error,
    )
