"""SQLite persistence for PI-21 management research records.

These tables are management research, not accounting.  No portfolio, position,
cash, ledger, risk or execution table is written here.  Cycles and their child
records are append-only apart from the cycle's own execution/reconciliation
status, which is replaced with the latest reconciled status while preserving the
original decision payload.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from novacontrol.novamarket.portfolio_management.models import (
    CycleStatus,
    ManagementConfig,
    PortfolioManagementConflict,
    PortfolioManagementNotFound,
    parse_time,
)

ACTIVE_LEASE = timedelta(hours=1)
SCHEMA_VERSION = 1


def _json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))


def _load(text: object) -> Any:
    if not text:
        return {}
    try:
        return json.loads(str(text))
    except (TypeError, ValueError):
        return {}


class PortfolioManagementStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            self._connection.execute("PRAGMA journal_mode=WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS pm_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pm_configs (
                    version INTEGER PRIMARY KEY,
                    digest TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS pm_cycles (
                    cycle_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    portfolio_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    config_version TEXT NOT NULL,
                    snapshot_version TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pm_cycles_portfolio
                    ON pm_cycles(portfolio_id, started_at DESC);
                CREATE TABLE IF NOT EXISTS pm_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    cycle_id TEXT NOT NULL,
                    portfolio_id TEXT NOT NULL,
                    snapshot_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(cycle_id) REFERENCES pm_cycles(cycle_id)
                );
                CREATE TABLE IF NOT EXISTS pm_positions (
                    assessment_id TEXT PRIMARY KEY,
                    cycle_id TEXT NOT NULL,
                    portfolio_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    assessment TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(cycle_id) REFERENCES pm_cycles(cycle_id)
                );
                CREATE INDEX IF NOT EXISTS idx_pm_positions_cycle
                    ON pm_positions(cycle_id);
                CREATE TABLE IF NOT EXISTS pm_opportunities (
                    opportunity_id TEXT PRIMARY KEY,
                    cycle_id TEXT NOT NULL,
                    portfolio_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    eligible INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(cycle_id) REFERENCES pm_cycles(cycle_id)
                );
                CREATE INDEX IF NOT EXISTS idx_pm_opportunities_cycle
                    ON pm_opportunities(cycle_id);
                CREATE TABLE IF NOT EXISTS pm_decisions (
                    decision_id TEXT PRIMARY KEY,
                    cycle_id TEXT NOT NULL,
                    portfolio_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(cycle_id) REFERENCES pm_cycles(cycle_id)
                );
                CREATE INDEX IF NOT EXISTS idx_pm_decisions_portfolio
                    ON pm_decisions(portfolio_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS pm_rebalances (
                    rebalance_id TEXT PRIMARY KEY,
                    cycle_id TEXT NOT NULL,
                    portfolio_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(cycle_id) REFERENCES pm_cycles(cycle_id)
                );
                CREATE TABLE IF NOT EXISTS pm_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pm_events_cycle
                    ON pm_events(cycle_id, event_id);
                """
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO pm_meta(key, value, updated_at) VALUES(?,?,?)",
                ("schema_version", str(SCHEMA_VERSION), _now().isoformat()),
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO pm_meta(key, value, updated_at) VALUES(?,?,?)",
                ("active_cycle_id", "", _now().isoformat()),
            )
            has_config = self._connection.execute("SELECT COUNT(*) AS n FROM pm_configs").fetchone()
            if not has_config or int(has_config["n"]) == 0:
                config = ManagementConfig()
                self._connection.execute(
                    "INSERT INTO pm_configs(version, digest, payload, created_at, active) "
                    "VALUES(?,?,?,?,1)",
                    (1, config.digest, _json(config.to_dict()), _now().isoformat()),
                )

    # -- config --------------------------------------------------------------
    def config(self) -> tuple[int, ManagementConfig]:
        row = self._connection.execute(
            "SELECT version, payload FROM pm_configs WHERE active=1 ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return 1, ManagementConfig()
        return int(row["version"]), ManagementConfig.from_mapping(_load(row["payload"]))

    def config_history(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT version, digest, payload, created_at, active "
            "FROM pm_configs ORDER BY version DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
        return [
            {
                "version": int(row["version"]),
                "digest": str(row["digest"]),
                "active": bool(row["active"]),
                "created_at": str(row["created_at"]),
                "config": _load(row["payload"]),
            }
            for row in rows
        ]

    def save_config(self, config: ManagementConfig) -> dict[str, Any]:
        row = self._connection.execute("SELECT MAX(version) AS version FROM pm_configs").fetchone()
        version = int(row["version"] or 0) + 1
        now = _now().isoformat()
        with self._connection:
            self._connection.execute("UPDATE pm_configs SET active=0")
            self._connection.execute(
                "INSERT INTO pm_configs(version, digest, payload, created_at, active) "
                "VALUES(?,?,?,?,1)",
                (version, config.digest, _json(config.to_dict()), now),
            )
            self._connection.execute(
                "INSERT INTO pm_meta(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                ("config_version", config.version, now),
            )
        return {
            "version": version,
            "digest": config.digest,
            "active": True,
            "created_at": now,
            "config": config.to_dict(),
        }

    # -- metadata ------------------------------------------------------------
    def meta(self, key: str, default: str = "") -> str:
        """Read one small coordinator value (scheduler cadence state)."""

        row = self._connection.execute(
            "SELECT value FROM pm_meta WHERE key=?", (str(key),)
        ).fetchone()
        return str(row["value"]) if row is not None else default

    def set_meta(self, key: str, value: str, *, at: datetime | None = None) -> None:
        """Persist one small coordinator value; never a financial record."""

        moment = (at or _now()).isoformat()
        with self._connection:
            self._connection.execute(
                "INSERT INTO pm_meta(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (str(key), str(value), moment),
            )

    # -- cycle lease/idempotency --------------------------------------------
    def cycle_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM pm_cycles WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        return self._cycle_row(row) if row else None

    def claim_cycle(
        self,
        *,
        cycle_id: str,
        idempotency_key: str,
        portfolio_id: str,
        trigger: str,
        config_version: str,
        snapshot_version: str,
        payload: Mapping[str, Any],
        started_at: datetime,
    ) -> tuple[dict[str, Any], bool]:
        """Claim the one active slot and insert the RUNNING cycle atomically."""

        now = started_at.astimezone(UTC)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._connection.execute(
                "SELECT * FROM pm_cycles WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                self._connection.commit()
                return self._cycle_row(existing), True
            active = self._connection.execute(
                "SELECT value, updated_at FROM pm_meta WHERE key='active_cycle_id'"
            ).fetchone()
            active_id = str(active["value"] or "") if active else ""
            if active_id:
                active_row = self._connection.execute(
                    "SELECT * FROM pm_cycles WHERE cycle_id=?", (active_id,)
                ).fetchone()
                if active_row is not None and active_row["status"] == CycleStatus.RUNNING.value:
                    active_at = parse_time(active_row["started_at"])
                    if now - active_at < ACTIVE_LEASE:
                        self._connection.rollback()
                        raise PortfolioManagementConflict(
                            f"portfolio-management cycle {active_id} is already active; "
                            "one review cycle at a time is enforced"
                        )
                    self._connection.execute(
                        "UPDATE pm_cycles SET status=?, completed_at=?, updated_at=? "
                        "WHERE cycle_id=?",
                        (
                            CycleStatus.INTERRUPTED.value,
                            now.isoformat(),
                            now.isoformat(),
                            active_id,
                        ),
                    )
                    self._connection.execute(
                        "INSERT INTO pm_events(cycle_id, kind, message, payload, created_at) "
                        "VALUES(?,?,?,?,?)",
                        (
                            active_id,
                            "cycle.interrupted",
                            "A stale running cycle was interrupted before a new cycle started.",
                            _json({"new_cycle_id": cycle_id}),
                            now.isoformat(),
                        ),
                    )
            self._connection.execute(
                "INSERT INTO pm_cycles(cycle_id, idempotency_key, portfolio_id, status, "
                "trigger, started_at, completed_at, config_version, snapshot_version, "
                "payload, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cycle_id,
                    idempotency_key,
                    portfolio_id,
                    CycleStatus.RUNNING.value,
                    trigger,
                    now.isoformat(),
                    None,
                    config_version,
                    snapshot_version,
                    _json(payload),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            self._connection.execute(
                "UPDATE pm_meta SET value=?, updated_at=? WHERE key='active_cycle_id'",
                (cycle_id, now.isoformat()),
            )
            self._connection.commit()
        except Exception:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        row = self._connection.execute(
            "SELECT * FROM pm_cycles WHERE cycle_id=?", (cycle_id,)
        ).fetchone()
        return self._cycle_row(row), False

    def finish_cycle(
        self,
        cycle_id: str,
        *,
        status: str,
        payload: Mapping[str, Any],
        completed_at: datetime,
    ) -> dict[str, Any]:
        completed = completed_at.astimezone(UTC).isoformat()
        with self._connection:
            changed = self._connection.execute(
                "UPDATE pm_cycles SET status=?, completed_at=?, updated_at=?, payload=? "
                "WHERE cycle_id=?",
                (status, completed, completed, _json(payload), cycle_id),
            )
            if changed.rowcount != 1:
                raise PortfolioManagementNotFound(f"unknown cycle {cycle_id!r}")
            self._connection.execute(
                "UPDATE pm_meta SET value='', updated_at=? WHERE key='active_cycle_id' AND value=?",
                (completed, cycle_id),
            )
        return self.get_cycle(cycle_id)

    def update_cycle_payload(
        self, cycle_id: str, *, payload: Mapping[str, Any], status: str = ""
    ) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT status FROM pm_cycles WHERE cycle_id=?", (cycle_id,)
        ).fetchone()
        if row is None:
            raise PortfolioManagementNotFound(f"unknown cycle {cycle_id!r}")
        target_status = status or str(row["status"])
        now = _now().isoformat()
        with self._connection:
            self._connection.execute(
                "UPDATE pm_cycles SET payload=?, status=?, updated_at=? WHERE cycle_id=?",
                (_json(payload), target_status, now, cycle_id),
            )
        return self.get_cycle(cycle_id)

    def get_cycle(self, cycle_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM pm_cycles WHERE cycle_id=?", (cycle_id,)
        ).fetchone()
        if row is None:
            raise PortfolioManagementNotFound(f"unknown portfolio-management cycle {cycle_id!r}")
        return self._cycle_row(row)

    def latest_cycle(self, portfolio_id: str = "") -> dict[str, Any] | None:
        if portfolio_id:
            row = self._connection.execute(
                "SELECT * FROM pm_cycles WHERE portfolio_id=? ORDER BY started_at DESC LIMIT 1",
                (portfolio_id,),
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT * FROM pm_cycles ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        return self._cycle_row(row) if row else None

    def cycles(
        self,
        *,
        portfolio_id: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        where: list[str] = []
        params: list[Any] = []
        if portfolio_id:
            where.append("portfolio_id=?")
            params.append(portfolio_id)
        if status:
            where.append("status=?")
            params.append(status)
        clause = " WHERE " + " AND ".join(where) if where else ""
        total = int(
            self._connection.execute(
                "SELECT COUNT(*) AS n FROM pm_cycles" + clause, tuple(params)
            ).fetchone()["n"]
        )
        rows = self._connection.execute(
            "SELECT * FROM pm_cycles" + clause + " ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (*params, max(1, min(int(limit), 100)), max(0, int(offset))),
        ).fetchall()
        return {
            "items": [self._cycle_row(row) for row in rows],
            "total": total,
            "limit": max(1, min(int(limit), 100)),
            "offset": max(0, int(offset)),
            "has_more": max(0, int(offset)) + len(rows) < total,
        }

    def active_cycle(self) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT value FROM pm_meta WHERE key='active_cycle_id'"
        ).fetchone()
        active_id = str(row["value"] or "") if row else ""
        if not active_id:
            return None
        cycle = self._connection.execute(
            "SELECT * FROM pm_cycles WHERE cycle_id=?", (active_id,)
        ).fetchone()
        return self._cycle_row(cycle) if cycle else None

    def recover(self, *, at: datetime | None = None) -> dict[str, Any]:
        moment = (at or _now()).astimezone(UTC)
        rows = self._connection.execute(
            "SELECT cycle_id FROM pm_cycles WHERE status=?",
            (CycleStatus.RUNNING.value,),
        ).fetchall()
        ids = [str(row["cycle_id"]) for row in rows]
        with self._connection:
            self._connection.execute(
                "UPDATE pm_cycles SET status=?, completed_at=?, updated_at=? WHERE status=?",
                (
                    CycleStatus.INTERRUPTED.value,
                    moment.isoformat(),
                    moment.isoformat(),
                    CycleStatus.RUNNING.value,
                ),
            )
            self._connection.execute(
                "UPDATE pm_meta SET value='', updated_at=? WHERE key='active_cycle_id'",
                (moment.isoformat(),),
            )
            for cycle_id in ids:
                self._connection.execute(
                    "INSERT INTO pm_events(cycle_id, kind, message, payload, created_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        cycle_id,
                        "cycle.interrupted",
                        "Recovery marked an interrupted management cycle; it was not replayed.",
                        "{}",
                        moment.isoformat(),
                    ),
                )
        return {"interrupted": ids, "count": len(ids), "auto_resumed": False}

    # -- child records -------------------------------------------------------
    def record_cycle_data(
        self,
        *,
        cycle_id: str,
        snapshot: Mapping[str, Any],
        positions: Iterable[Mapping[str, Any]],
        opportunities: Iterable[Mapping[str, Any]],
        decisions: Iterable[Mapping[str, Any]],
        capital: Mapping[str, Any],
        exposure: Mapping[str, Any],
        cash: Mapping[str, Any],
        rebalance: Mapping[str, Any],
        created_at: datetime,
    ) -> None:
        now = created_at.astimezone(UTC).isoformat()
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO pm_snapshots(snapshot_id, cycle_id, portfolio_id, "
                "snapshot_version, created_at, payload) VALUES(?,?,?,?,?,?)",
                (
                    str(snapshot.get("snapshot_id") or ""),
                    cycle_id,
                    str(snapshot.get("portfolio_id") or ""),
                    str(snapshot.get("snapshot_version") or ""),
                    now,
                    _json(snapshot),
                ),
            )
            for row in positions:
                self._connection.execute(
                    "INSERT OR IGNORE INTO pm_positions(assessment_id, cycle_id, portfolio_id, "
                    "instrument_id, assessment, action, created_at, payload) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        str(row.get("assessment_id") or ""),
                        cycle_id,
                        str(row.get("portfolio_id") or ""),
                        str(row.get("instrument_id") or ""),
                        str(row.get("assessment") or ""),
                        str(row.get("action") or ""),
                        now,
                        _json(row),
                    ),
                )
            for row in opportunities:
                self._connection.execute(
                    "INSERT OR IGNORE INTO pm_opportunities(opportunity_id, cycle_id, "
                    "portfolio_id, instrument_id, eligible, created_at, payload) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        str(row.get("opportunity_id") or ""),
                        cycle_id,
                        str(row.get("portfolio_id") or ""),
                        str(row.get("instrument_id") or ""),
                        1 if row.get("eligible") else 0,
                        now,
                        _json(row),
                    ),
                )
            for row in decisions:
                self._connection.execute(
                    "INSERT OR IGNORE INTO pm_decisions(decision_id, cycle_id, portfolio_id, "
                    "instrument_id, action, created_at, payload) VALUES(?,?,?,?,?,?,?)",
                    (
                        str(row.get("decision_id") or ""),
                        cycle_id,
                        str(row.get("portfolio_id") or ""),
                        str(row.get("instrument_id") or ""),
                        str(row.get("action") or ""),
                        now,
                        _json(row),
                    ),
                )
            self._connection.execute(
                "INSERT OR IGNORE INTO pm_rebalances(rebalance_id, cycle_id, portfolio_id, "
                "payload, created_at) VALUES(?,?,?,?,?)",
                (
                    str(rebalance.get("rebalance_id") or ""),
                    cycle_id,
                    str(rebalance.get("portfolio_id") or ""),
                    _json(rebalance),
                    now,
                ),
            )
            for name, body in (
                ("capital", capital),
                ("exposure", exposure),
                ("cash", cash),
            ):
                self._connection.execute(
                    "INSERT INTO pm_events(cycle_id, kind, message, payload, created_at) "
                    "VALUES(?,?,?,?,?)",
                    (cycle_id, f"{name}.assessed", f"{name} assessment recorded", _json(body), now),
                )

    def latest_snapshot(self, portfolio_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload FROM pm_snapshots WHERE portfolio_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (portfolio_id,),
        ).fetchone()
        return dict(_load(row["payload"])) if row else None

    def _latest_child(self, table: str, portfolio_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            f"SELECT payload FROM {table} WHERE portfolio_id=? ORDER BY created_at DESC LIMIT 1",
            (portfolio_id,),
        ).fetchone()
        return dict(_load(row["payload"])) if row else None

    def latest_positions(self, portfolio_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT payload FROM pm_positions WHERE portfolio_id=? AND cycle_id=("
            "SELECT cycle_id FROM pm_positions WHERE portfolio_id=? "
            "ORDER BY created_at DESC LIMIT 1) ORDER BY instrument_id",
            (portfolio_id, portfolio_id),
        ).fetchall()
        return [dict(_load(row["payload"])) for row in rows]

    def latest_opportunities(self, portfolio_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT payload FROM pm_opportunities WHERE portfolio_id=? AND cycle_id=("
            "SELECT cycle_id FROM pm_opportunities WHERE portfolio_id=? "
            "ORDER BY created_at DESC LIMIT 1) ORDER BY instrument_id",
            (portfolio_id, portfolio_id),
        ).fetchall()
        return [dict(_load(row["payload"])) for row in rows]

    def latest_decisions(self, portfolio_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT payload FROM pm_decisions WHERE portfolio_id=? "
            "ORDER BY created_at DESC LIMIT ?",
            (portfolio_id, max(1, min(int(limit), 500))),
        ).fetchall()
        return [dict(_load(row["payload"])) for row in rows]

    def latest_rebalance(self, portfolio_id: str) -> dict[str, Any] | None:
        return self._latest_child("pm_rebalances", portfolio_id)

    def latest_capital(self, portfolio_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload FROM pm_events WHERE kind='capital.assessed' AND cycle_id IN "
            "(SELECT cycle_id FROM pm_cycles WHERE portfolio_id=?) ORDER BY event_id DESC LIMIT 1",
            (portfolio_id,),
        ).fetchone()
        return dict(_load(row["payload"])) if row else None

    def latest_exposure(self, portfolio_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload FROM pm_events WHERE kind='exposure.assessed' AND cycle_id IN "
            "(SELECT cycle_id FROM pm_cycles WHERE portfolio_id=?) ORDER BY event_id DESC LIMIT 1",
            (portfolio_id,),
        ).fetchone()
        return dict(_load(row["payload"])) if row else None

    def latest_cash(self, portfolio_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload FROM pm_events WHERE kind='cash.assessed' AND cycle_id IN "
            "(SELECT cycle_id FROM pm_cycles WHERE portfolio_id=?) ORDER BY event_id DESC LIMIT 1",
            (portfolio_id,),
        ).fetchone()
        return dict(_load(row["payload"])) if row else None

    def append_event(
        self,
        cycle_id: str,
        kind: str,
        message: str,
        *,
        payload: Mapping[str, Any] | None = None,
        at: datetime | None = None,
    ) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO pm_events(cycle_id, kind, message, payload, created_at) "
                "VALUES(?,?,?,?,?)",
                (
                    cycle_id,
                    kind,
                    message,
                    _json(payload or {}),
                    (at or _now()).astimezone(UTC).isoformat(),
                ),
            )

    def events(self, cycle_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM pm_events WHERE cycle_id=? ORDER BY event_id LIMIT ?",
            (cycle_id, max(1, min(int(limit), 1000))),
        ).fetchall()
        return [
            {
                "kind": str(row["kind"]),
                "message": str(row["message"]),
                "payload": _load(row["payload"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def status(self) -> dict[str, Any]:
        counts = {
            "cycles": int(
                self._connection.execute("SELECT COUNT(*) n FROM pm_cycles").fetchone()["n"]
            ),
            "running": int(
                self._connection.execute(
                    "SELECT COUNT(*) n FROM pm_cycles WHERE status=?",
                    (CycleStatus.RUNNING.value,),
                ).fetchone()["n"]
            ),
            "positions": int(
                self._connection.execute("SELECT COUNT(*) n FROM pm_positions").fetchone()["n"]
            ),
            "decisions": int(
                self._connection.execute("SELECT COUNT(*) n FROM pm_decisions").fetchone()["n"]
            ),
            "opportunities": int(
                self._connection.execute("SELECT COUNT(*) n FROM pm_opportunities").fetchone()["n"]
            ),
        }
        active = self.active_cycle()
        try:
            integrity = str(self._connection.execute("PRAGMA integrity_check").fetchone()[0])
        except Exception as exc:  # noqa: BLE001
            integrity = f"unavailable: {type(exc).__name__}: {exc}"
        return {
            "ok": True,
            "schema_version": SCHEMA_VERSION,
            "path": self.path,
            "counts": counts,
            "active_cycle_id": (active or {}).get("cycle_id", ""),
            "integrity": integrity,
            "financial_tables": [],
            "read_only_financial": True,
        }

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._connection.close()

    @staticmethod
    def _cycle_row(row: sqlite3.Row) -> dict[str, Any]:
        body = dict(_load(row["payload"]))
        return {
            "cycle_id": str(row["cycle_id"]),
            "idempotency_key": str(row["idempotency_key"]),
            "portfolio_id": str(row["portfolio_id"]),
            "status": str(row["status"]),
            "trigger": str(row["trigger"]),
            "started_at": str(row["started_at"]),
            "completed_at": row["completed_at"],
            "config_version": str(row["config_version"]),
            "snapshot_version": str(row["snapshot_version"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            **body,
        }


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = ["PortfolioManagementStore", "SCHEMA_VERSION"]
