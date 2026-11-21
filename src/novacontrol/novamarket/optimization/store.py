"""Isolated SQLite persistence for PI-18 recommendations.

Only ``po_*`` tables live in this file.  No NM-07 portfolio, NM-08 risk, NM-10
order/fill or NM-03 ledger table is opened.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import (
    MAX_BACKTESTS,
    MAX_CONFIGS,
    MAX_RUNS,
    MAX_SCENARIOS_PER_RUN,
    OPTIMIZATION_SCHEMA_VERSION,
    OptimizationNotFoundError,
    OptimizationResourceError,
    OptimizationStateError,
    OptimizationStatus,
    OptimizationStoreError,
)


def _json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise OptimizationStoreError("record is not JSON serialisable") from exc


def _load(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise OptimizationStoreError("stored optimization record is unreadable") from exc
    return parsed if isinstance(parsed, dict) else {}


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(tz=UTC)).isoformat()


class OptimizationStore:
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Any | None = None,
        max_configs: int = MAX_CONFIGS,
        max_runs: int = MAX_RUNS,
        max_backtests: int = MAX_BACKTESTS,
    ) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            try:
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise OptimizationStoreError(
                    f"the optimization store directory is not writable: {exc}"
                ) from exc
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self.max_configs = max(1, min(int(max_configs), MAX_CONFIGS))
        self.max_runs = max(1, min(int(max_runs), MAX_RUNS))
        self.max_backtests = max(1, min(int(max_backtests), MAX_BACKTESTS))
        self._lock = threading.RLock()
        try:
            self._connection = sqlite3.connect(self._path, check_same_thread=False, timeout=15)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._migrate()
        except sqlite3.Error as exc:
            raise OptimizationStoreError(f"optimization store could not open: {exc}") from exc

    @property
    def schema_version(self) -> int:
        return OPTIMIZATION_SCHEMA_VERSION

    def _migrate(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS po_configs(
                    config_id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS po_configs_portfolio
                    ON po_configs(portfolio_id, created_at);
                CREATE TABLE IF NOT EXISTS po_runs(
                    run_id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    status TEXT NOT NULL,
                    config_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS po_runs_portfolio
                    ON po_runs(portfolio_id, created_at);
                CREATE INDEX IF NOT EXISTS po_runs_status
                    ON po_runs(status, created_at);
                CREATE TABLE IF NOT EXISTS po_scenarios(
                    scenario_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS po_scenarios_run
                    ON po_scenarios(run_id, created_at);
                CREATE TABLE IF NOT EXISTS po_backtests(
                    backtest_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS po_backtests_run
                    ON po_backtests(run_id, created_at);
                CREATE TABLE IF NOT EXISTS po_events(
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS po_events_run
                    ON po_events(run_id, created_at);
                """
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _count(self, table: str, where: str = "", params: tuple[Any, ...] = ()) -> int:
        query = f"SELECT COUNT(*) FROM {table}" + (f" WHERE {where}" if where else "")
        return int(self._connection.execute(query, params).fetchone()[0])

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "store": Path(self._path).name if self._path != ":memory:" else ":memory:",
                "schema_version": self.schema_version,
                "configs": self._count("po_configs"),
                "runs": self._count("po_runs"),
                "scenarios": self._count("po_scenarios"),
                "backtests": self._count("po_backtests"),
                "events": self._count("po_events"),
                "limits": {
                    "configs": self.max_configs,
                    "runs": self.max_runs,
                    "backtests": self.max_backtests,
                    "scenarios_per_run": MAX_SCENARIOS_PER_RUN,
                },
                "isolated_research_tables": True,
                "portfolio_state_access": False,
                "ledger_access": False,
                "execution_access": False,
            }

    def save_config(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        config_id = str(body.get("config_id") or "")
        if not config_id:
            raise OptimizationStoreError("config_id is required")
        with self._lock:
            existing = self._connection.execute(
                "SELECT payload_json FROM po_configs WHERE config_id=?", (config_id,)
            ).fetchone()
            if existing:
                return _load(existing["payload_json"])
            if self._count("po_configs") >= self.max_configs:
                raise OptimizationResourceError("optimization configuration limit reached")
            self._connection.execute(
                "INSERT INTO po_configs("
                "config_id, portfolio_id, horizon, payload_json, created_at"
                ") VALUES(?, ?, ?, ?, ?)",
                (
                    config_id,
                    str(body.get("portfolio_id") or ""),
                    str(body.get("horizon") or ""),
                    _json(body),
                    _iso(_time(body.get("created_at"))),
                ),
            )
            self._connection.commit()
        return body

    def config(self, config_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM po_configs WHERE config_id=?", (config_id,)
            ).fetchone()
        if row is None:
            raise OptimizationNotFoundError(
                f"optimization configuration {config_id!r} was not found"
            )
        return _load(row["payload_json"])

    def configs(
        self, *, portfolio_id: str = "", limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if portfolio_id:
            clauses.append("portfolio_id=?")
            params.append(portfolio_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT payload_json FROM po_configs{where} "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, max(1, min(int(limit), 200)), max(0, int(offset))),
            ).fetchall()
        return [_load(row["payload_json"]) for row in rows]

    def save_run(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        run_id = str(body.get("run_id") or "")
        if not run_id:
            raise OptimizationStoreError("run_id is required")
        status = str(body.get("status") or OptimizationStatus.PENDING.value)
        created = _iso(_time(body.get("created_at")))
        finished = body.get("finished_at")
        with self._lock:
            existing = self._connection.execute(
                "SELECT run_id FROM po_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing is None and self._count("po_runs") >= self.max_runs:
                raise OptimizationResourceError("optimization run limit reached")
            self._connection.execute(
                "INSERT INTO po_runs(run_id, portfolio_id, horizon, status, config_id, "
                "payload_json, created_at, finished_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, "
                "payload_json=excluded.payload_json, finished_at=excluded.finished_at",
                (
                    run_id,
                    str(body.get("portfolio_id") or ""),
                    str(body.get("horizon") or ""),
                    status,
                    str(body.get("config_id") or ""),
                    _json(body),
                    created,
                    None if finished is None else str(finished),
                ),
            )
            self._connection.commit()
        return body

    def update_run(self, run_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.save_run({**dict(payload), "run_id": run_id})

    def run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM po_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise OptimizationNotFoundError(f"optimization run {run_id!r} was not found")
        return _load(row["payload_json"])

    def runs(
        self,
        *,
        portfolio_id: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if portfolio_id:
            clauses.append("portfolio_id=?")
            params.append(portfolio_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT payload_json FROM po_runs{where} "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, max(1, min(int(limit), 200)), max(0, int(offset))),
            ).fetchall()
        return [_load(row["payload_json"]) for row in rows]

    def count_runs(self, status: str = "") -> int:
        with self._lock:
            return (
                self._count("po_runs", "status=?", (status,)) if status else self._count("po_runs")
            )

    def recover_stale_running(
        self, *, older_than_seconds: int = 3_600, now: datetime | None = None
    ) -> int:
        reference = now or self._clock()
        cutoff = reference - timedelta(seconds=max(0, older_than_seconds))
        with self._lock:
            rows = self._connection.execute(
                "SELECT run_id, payload_json FROM po_runs WHERE status=? AND created_at<?",
                (OptimizationStatus.RUNNING.value, _iso(cutoff)),
            ).fetchall()
            for row in rows:
                payload = _load(row["payload_json"])
                payload["status"] = OptimizationStatus.FAILED.value
                payload["error"] = "interrupted optimization recovered after restart"
                payload["finished_at"] = _iso(reference)
                self._connection.execute(
                    "UPDATE po_runs SET status=?, payload_json=?, finished_at=? WHERE run_id=?",
                    (
                        OptimizationStatus.FAILED.value,
                        _json(payload),
                        _iso(reference),
                        row["run_id"],
                    ),
                )
                self._connection.execute(
                    "INSERT OR REPLACE INTO po_events("
                    "event_id, run_id, event_type, payload_json, created_at"
                    ") VALUES(?, ?, ?, ?, ?)",
                    (
                        f"poev_{row['run_id']}_recovered",
                        row["run_id"],
                        "optimization.recovered",
                        _json({"status": "failed", "detail": "interrupted"}),
                        _iso(reference),
                    ),
                )
            self._connection.commit()
        return len(rows)

    def save_scenarios(self, run_id: str, rows: Sequence[Mapping[str, Any]]) -> None:
        with self._lock:
            existing_ids = {
                str(row[0])
                for row in self._connection.execute(
                    "SELECT scenario_id FROM po_scenarios WHERE run_id=?", (run_id,)
                ).fetchall()
            }
            incoming_ids = {str(row.get("scenario_id") or "") for row in rows}
            if len(existing_ids | incoming_ids) > MAX_SCENARIOS_PER_RUN:
                raise OptimizationResourceError("scenario limit reached for this run")
            for row in rows:
                scenario_id = str(row.get("scenario_id") or "")
                if not scenario_id:
                    raise OptimizationStoreError("scenario_id is required")
                self._connection.execute(
                    "INSERT OR REPLACE INTO po_scenarios(scenario_id, run_id, kind, name, "
                    "payload_json, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        scenario_id,
                        run_id,
                        str(row.get("kind") or "hypothetical"),
                        str(row.get("name") or ""),
                        _json(row),
                        _iso(),
                    ),
                )
            self._connection.commit()

    def scenarios(self, run_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM po_scenarios WHERE run_id=? "
                "ORDER BY created_at ASC LIMIT ?",
                (run_id, max(1, min(int(limit), MAX_SCENARIOS_PER_RUN))),
            ).fetchall()
        return [_load(row["payload_json"]) for row in rows]

    def save_backtest(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        backtest_id = str(body.get("backtest_id") or "")
        if not backtest_id:
            raise OptimizationStoreError("backtest_id is required")
        with self._lock:
            existing = self._connection.execute(
                "SELECT backtest_id FROM po_backtests WHERE backtest_id=?", (backtest_id,)
            ).fetchone()
            if existing is None and self._count("po_backtests") >= self.max_backtests:
                raise OptimizationResourceError("optimization backtest limit reached")
            self._connection.execute(
                "INSERT INTO po_backtests(backtest_id, run_id, status, payload_json, created_at) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(backtest_id) DO UPDATE SET "
                "status=excluded.status, payload_json=excluded.payload_json",
                (
                    backtest_id,
                    str(body.get("run_id") or ""),
                    str(body.get("status") or "completed"),
                    _json(body),
                    _iso(_time(body.get("created_at"))),
                ),
            )
            self._connection.commit()
        return body

    def latest_completed(self, portfolio_id: str, *, horizon: str = "") -> dict[str, Any] | None:
        clauses = [
            "portfolio_id=?",
            "status IN (?, ?)",
        ]
        params: list[Any] = [
            portfolio_id,
            OptimizationStatus.COMPLETED.value,
            OptimizationStatus.COMPLETED_WITH_WARNINGS.value,
        ]
        if horizon:
            clauses.append("horizon=?")
            params.append(horizon)
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM po_runs WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC LIMIT 1",
                tuple(params),
            ).fetchone()
        return _load(row["payload_json"]) if row is not None else None

    def backtest(self, backtest_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM po_backtests WHERE backtest_id=?", (backtest_id,)
            ).fetchone()
        if row is None:
            raise OptimizationNotFoundError(f"optimization backtest {backtest_id!r} was not found")
        return _load(row["payload_json"])

    def backtests(
        self, *, run_id: str = "", limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        where = " WHERE run_id=?" if run_id else ""
        params: tuple[Any, ...] = (run_id,) if run_id else ()
        with self._lock:
            rows = self._connection.execute(
                f"SELECT payload_json FROM po_backtests{where} "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, max(1, min(int(limit), 200)), max(0, int(offset))),
            ).fetchall()
        return [_load(row["payload_json"]) for row in rows]

    def event(self, run_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        event_id = f"poev_{run_id}_{event_type}_{_iso()}"
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO po_events("
                "event_id, run_id, event_type, payload_json, created_at"
                ") VALUES(?, ?, ?, ?, ?)",
                (event_id, run_id, event_type, _json(dict(payload)), _iso()),
            )
            self._connection.commit()

    def events(self, run_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT event_type, payload_json, created_at FROM po_events "
                "WHERE run_id=? ORDER BY created_at ASC LIMIT ?",
                (run_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [
            {
                "event_type": row["event_type"],
                "at": row["created_at"],
                "payload": _load(row["payload_json"]),
            }
            for row in rows
        ]

    def assert_transition(self, run_id: str, *, target: OptimizationStatus) -> None:
        current = OptimizationStatus(str(self.run(run_id).get("status")))
        if current in {OptimizationStatus.CANCELLED, OptimizationStatus.FAILED} and target not in {
            OptimizationStatus.CANCELLED,
            OptimizationStatus.FAILED,
        }:
            raise OptimizationStateError(
                f"optimization run cannot move from {current.value} to {target.value}"
            )


def _time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = ["OptimizationStore"]
