"""Durable AT-01 autonomy state in the existing NovaMarket SQLite file.

The schema is additive and separately versioned through ``autonomy_meta``;
the market-data store's ``PRAGMA user_version`` is never touched.  Only the
tables needed by the runtime shell are created.  Decision/exit tables are
deliberately absent until AT-03/AT-05.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from novacontrol.novamarket.autonomy.models import (
    ACTIVE_MONITORING_STATUSES,
    AutonomyConfig,
    AutonomyLeaseError,
    AutonomyStoreError,
    AutonomyValidationError,
    MonitoringConfig,
)

_SCHEMA_VERSION = 5

#: Additive v5 schema: the append-only operator/control audit.  Every statement
#: uses IF NOT EXISTS, so an existing v4 store gains the table without touching
#: its runtime, lease, cycle or configuration rows.
_CONTROL_SCHEMA = """
CREATE TABLE IF NOT EXISTS autonomy_control_events (
    seq INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL,
    operation TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    from_mode TEXT NOT NULL DEFAULT '',
    requested_mode TEXT NOT NULL DEFAULT '',
    resulting_mode TEXT NOT NULL DEFAULT '',
    desired_mode TEXT NOT NULL DEFAULT '',
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_valid INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_autonomy_control_events_occurred
    ON autonomy_control_events(occurred_at DESC);
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS autonomy_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS autonomy_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    config_version INTEGER NOT NULL,
    portfolio_ids_json TEXT NOT NULL DEFAULT '[]',
    paused_exit_policy TEXT NOT NULL DEFAULT 'protective',
    lease_seconds INTEGER NOT NULL DEFAULT 60,
    max_cycle_seconds INTEGER NOT NULL DEFAULT 30,
    auto_start INTEGER NOT NULL DEFAULT 1,
    auto_start_mode TEXT NOT NULL DEFAULT 'autonomous',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS autonomy_runtime (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL DEFAULT 'disabled',
    desired_mode TEXT NOT NULL DEFAULT 'disabled',
    runtime_state TEXT NOT NULL DEFAULT 'stopped',
    owner_id TEXT NOT NULL DEFAULT '',
    lease_token TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    active_cycle_id TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    last_reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS autonomy_cycles (
    cycle_id TEXT PRIMARY KEY,
    cycle_key TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_autonomy_cycles_one_running
    ON autonomy_cycles(status) WHERE status = 'running';
CREATE TABLE IF NOT EXISTS autonomy_monitoring_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 1,
    exchange TEXT NOT NULL DEFAULT 'XNSE',
    interval_seconds INTEGER NOT NULL DEFAULT 900,
    max_instruments_per_cycle INTEGER NOT NULL DEFAULT 20,
    retry_max INTEGER NOT NULL DEFAULT 2,
    cycle_timeout_seconds INTEGER NOT NULL DEFAULT 120,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS autonomy_monitoring_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    rotation_cursor INTEGER NOT NULL DEFAULT 0,
    last_cycle_id TEXT,
    last_completed_at TEXT,
    last_attempt_at TEXT,
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS autonomy_monitoring_cycles (
    cycle_id TEXT PRIMARY KEY,
    autonomy_cycle_id TEXT NOT NULL DEFAULT '',
    cycle_key TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    exchange TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    calendar_verified INTEGER NOT NULL DEFAULT 0,
    calendar_source TEXT NOT NULL DEFAULT '',
    session_state TEXT NOT NULL DEFAULT 'unknown',
    session_eligible INTEGER NOT NULL DEFAULT 0,
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    trigger TEXT NOT NULL DEFAULT 'scheduled',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    eligible_count INTEGER NOT NULL DEFAULT 0,
    processed_count INTEGER NOT NULL DEFAULT 0,
    deferred_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    rotation_start INTEGER NOT NULL DEFAULT 0,
    rotation_end INTEGER NOT NULL DEFAULT 0,
    error_code TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_autonomy_monitoring_cycles_started
    ON autonomy_monitoring_cycles(started_at DESC);
CREATE TABLE IF NOT EXISTS autonomy_monitoring_candidates (
    cycle_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT '',
    exchange TEXT NOT NULL DEFAULT '',
    disposition TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    rank INTEGER NOT NULL DEFAULT 0,
    data_quality TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (cycle_id, instrument_id)
);
"""


def _utc(value: datetime | None = None) -> datetime:
    moment = value or datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("autonomy timestamps must be timezone-aware")
    return moment.astimezone(UTC)


def _iso(value: datetime | None = None) -> str:
    return _utc(value).isoformat()


def _parse(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


_CONTROL_FIELDS = (
    "event_id",
    "occurred_at",
    "operation",
    "actor",
    "reason",
    "from_mode",
    "requested_mode",
    "resulting_mode",
    "desired_mode",
    "lease_owner",
    "lease_valid",
    "outcome",
    "error",
    "details_json",
)


def _control_values(control: dict[str, Any]) -> tuple[Any, ...]:
    """Normalise one control event for the append-only insert."""

    occurred = control.get("occurred_at")
    if not isinstance(occurred, datetime):
        raise ValueError("a control event needs a timezone-aware occurred_at")
    operation = str(control.get("operation") or "").strip().lower()
    outcome = str(control.get("outcome") or "").strip().lower()
    if not operation or not outcome:
        raise ValueError("a control event needs an operation and an outcome")
    details = control.get("details")
    return (
        str(control.get("event_id") or uuid4().hex),
        _iso(occurred),
        operation,
        str(control.get("actor") or "system"),
        str(control.get("reason") or ""),
        str(control.get("from_mode") or ""),
        str(control.get("requested_mode") or ""),
        str(control.get("resulting_mode") or ""),
        str(control.get("desired_mode") or ""),
        str(control.get("lease_owner") or ""),
        1 if control.get("lease_valid") else 0,
        outcome,
        str(control.get("error") or ""),
        json.dumps(
            details if isinstance(details, dict) else {},
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _insert_control_event(conn: sqlite3.Connection, control: dict[str, Any]) -> None:
    """Insert one immutable control event on the caller's transaction."""

    placeholders = ", ".join("?" for _ in _CONTROL_FIELDS)
    conn.execute(
        f"INSERT INTO autonomy_control_events ({', '.join(_CONTROL_FIELDS)}) "
        f"VALUES ({placeholders})",
        _control_values(control),
    )


class AutonomyStore:
    """A small, transactionally safe SQLite store for AT-01 state."""

    def __init__(self, path: str | Path, *, busy_timeout: float = 5.0) -> None:
        self.path = str(path) if str(path) == ":memory:" else str(Path(path))
        self._lock = RLock()
        try:
            self._conn = sqlite3.connect(
                self.path, timeout=busy_timeout, check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout * 1000)}")
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
            self._initialize()
        except sqlite3.Error as exc:
            raise AutonomyStoreError(
                f"could not open the autonomy store: {type(exc).__name__}: {exc}"
            ) from exc

    def _initialize(self) -> None:
        with self._lock:
            try:
                self._conn.executescript(_SCHEMA)
                self._conn.executescript(_CONTROL_SCHEMA)
                # Additive v3 migration: older v2 databases gain the persisted
                # NM-05 data-quality label without rewriting existing rows.
                columns = {
                    item["name"]
                    for item in self._conn.execute(
                        "PRAGMA table_info(autonomy_monitoring_candidates)"
                    )
                }
                if "data_quality" not in columns:
                    self._conn.execute(
                        "ALTER TABLE autonomy_monitoring_candidates "
                        "ADD COLUMN data_quality TEXT NOT NULL DEFAULT ''"
                    )
                # Additive v4 migration: existing stores gain the application
                # auto-start policy without changing any persisted mode or lease.
                config_columns = {
                    item["name"]
                    for item in self._conn.execute(
                        "PRAGMA table_info(autonomy_config)"
                    )
                }
                if "auto_start" not in config_columns:
                    self._conn.execute(
                        "ALTER TABLE autonomy_config "
                        "ADD COLUMN auto_start INTEGER NOT NULL DEFAULT 1"
                    )
                if "auto_start_mode" not in config_columns:
                    self._conn.execute(
                        "ALTER TABLE autonomy_config ADD COLUMN auto_start_mode "
                        "TEXT NOT NULL DEFAULT 'autonomous'"
                    )
                row = self._conn.execute(
                    "SELECT value FROM autonomy_meta WHERE key = 'schema_version'"
                ).fetchone()
                if row is None:
                    self._conn.execute(
                        "INSERT INTO autonomy_meta(key, value) VALUES(?, ?)",
                        ("schema_version", str(_SCHEMA_VERSION)),
                    )
                elif int(row["value"]) > _SCHEMA_VERSION:
                    raise AutonomyStoreError(
                        "unsupported autonomy schema version "
                        f"{row['value']}; this build supports {_SCHEMA_VERSION}"
                    )
                elif int(row["value"]) < _SCHEMA_VERSION:
                    # Additive migration: every table above was created with
                    # IF NOT EXISTS, so the version row is the only marker.
                    self._conn.execute(
                        "UPDATE autonomy_meta SET value = ? WHERE key = "
                        "'schema_version'",
                        (str(_SCHEMA_VERSION),),
                    )
                now = _iso()
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO autonomy_config
                        (id, config_version, portfolio_ids_json, paused_exit_policy,
                         lease_seconds, max_cycle_seconds, auto_start,
                         auto_start_mode, updated_at)
                    VALUES (1, 2, '[]', 'protective', 60, 30, 1, 'autonomous', ?)
                    """,
                    (now,),
                )
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO autonomy_runtime
                        (id, mode, desired_mode, runtime_state, owner_id,
                         lease_token, lease_expires_at, heartbeat_at,
                         active_cycle_id, last_error, last_reason, updated_at)
                    VALUES (1, 'disabled', 'disabled', 'stopped', '', '', NULL,
                            NULL, NULL, '', '', ?)
                    """,
                    (now,),
                )
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO autonomy_monitoring_config
                        (id, enabled, exchange, interval_seconds,
                         max_instruments_per_cycle, retry_max,
                         cycle_timeout_seconds, updated_at)
                    VALUES (1, 1, 'XNSE', 900, 20, 2, 120, ?)
                    """,
                    (now,),
                )
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO autonomy_monitoring_state
                        (id, rotation_cursor, last_cycle_id, last_completed_at,
                         last_attempt_at, last_error_code, last_error,
                         consecutive_failures, updated_at)
                    VALUES (1, 0, NULL, NULL, NULL, '', '', 0, ?)
                    """,
                    (now,),
                )
                self._conn.commit()
            except AutonomyStoreError:
                raise
            except (sqlite3.Error, ValueError) as exc:
                self._conn.rollback()
                raise AutonomyStoreError(
                    f"could not initialise the autonomy store: {type(exc).__name__}: {exc}"
                ) from exc

    # -- shared connection access for later autonomy phases -----------------

    @property
    def connection(self) -> sqlite3.Connection:
        """The one SQLite connection for additive AT-03 decision tables."""

        return self._conn

    @property
    def lock(self) -> RLock:
        """The connection's re-entrant lock, shared by additive tables."""

        return self._lock

    # -- reads ---------------------------------------------------------------

    def config(self) -> AutonomyConfig:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM autonomy_config WHERE id = 1"
            ).fetchone()
        if row is None:
            raise AutonomyStoreError("autonomy configuration row is missing")
        try:
            portfolio_ids = tuple(json.loads(row["portfolio_ids_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AutonomyStoreError("stored autonomy portfolio_ids are invalid") from exc
        return AutonomyConfig(
            config_version=int(row["config_version"]),
            portfolio_ids=portfolio_ids,
            paused_exit_policy=str(row["paused_exit_policy"]),
            lease_seconds=int(row["lease_seconds"]),
            max_cycle_seconds=int(row["max_cycle_seconds"]),
            auto_start=bool(row["auto_start"]),
            auto_start_mode=str(row["auto_start_mode"]),
        )

    def save_auto_start(
        self, *, enabled: bool, mode: str, now: datetime
    ) -> AutonomyConfig:
        """Persist the application-start policy without touching the live mode."""

        normalized = str(mode or "").strip().lower()
        if normalized not in ("monitoring", "autonomous"):
            raise AutonomyValidationError(
                "auto_start_mode must be 'monitoring' or 'autonomous'"
            )
        with self._lock:
            try:
                self._conn.execute(
                    """
                    UPDATE autonomy_config
                    SET auto_start = ?, auto_start_mode = ?, config_version = 2,
                        updated_at = ?
                    WHERE id = 1
                    """,
                    (1 if enabled else 0, normalized, _iso(_utc(now))),
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise AutonomyStoreError(
                    f"could not persist the autonomy auto-start policy: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        return self.config()

    def snapshot(self, now: datetime) -> dict[str, Any]:
        """Read the runtime row, computed lease validity and active cycle."""

        moment = _utc(now)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM autonomy_runtime WHERE id = 1"
            ).fetchone()
            active = self._conn.execute(
                "SELECT * FROM autonomy_cycles WHERE status = 'running' "
                "ORDER BY started_at LIMIT 1"
            ).fetchone()
        if row is None:
            raise AutonomyStoreError("autonomy runtime row is missing")
        expires = _parse(row["lease_expires_at"])
        holder = str(row["owner_id"] or "")
        valid = bool(holder and expires is not None and expires > moment)
        return {
            "mode": str(row["mode"]),
            "desired_mode": str(row["desired_mode"]),
            "runtime_state": str(row["runtime_state"]),
            "owner_id": holder,
            "lease_token": str(row["lease_token"] or ""),
            "lease_expires_at": expires.isoformat() if expires else None,
            "heartbeat_at": row["heartbeat_at"],
            "active_cycle_id": row["active_cycle_id"],
            "last_error": str(row["last_error"] or ""),
            "last_reason": str(row["last_reason"] or ""),
            "updated_at": row["updated_at"],
            "lease": {
                "owner": holder,
                "expires_at": expires.isoformat() if expires else None,
                "valid": valid,
                "held": valid,
            },
            "active_cycle": dict(active) if active is not None else None,
        }

    # -- lifecycle/recovery --------------------------------------------------

    def recover(self, now: datetime) -> dict[str, Any]:
        """Clear stale ownership, preserving a live foreign lease.

        A process that starts while another process holds a live lease must not
        erase that owner's state.  It reports a conflict and stays safe.
        """

        moment = _utc(now)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM autonomy_runtime WHERE id = 1"
                ).fetchone()
                if row is None:
                    raise AutonomyStoreError("autonomy runtime row is missing")
                expires = _parse(row["lease_expires_at"])
                holder = str(row["owner_id"] or "")
                if holder and expires is not None and expires > moment:
                    self._conn.commit()
                    return self.snapshot(moment)
                # No live lease: any remaining cycle owner is interrupted, not
                # completed.  The runtime does not auto-submit or auto-resume.
                self._conn.execute(
                    "UPDATE autonomy_cycles SET status = 'interrupted', "
                    "finished_at = ?, last_error = ? WHERE status = 'running'",
                    (
                        _iso(moment),
                        "runtime ownership ended without a completed cycle",
                    ),
                )
                self._interrupt_monitoring_cycles(
                    moment,
                    "runtime ownership ended before the monitoring cycle completed",
                )
                self._conn.execute(
                    """
                    UPDATE autonomy_runtime
                    SET mode = 'disabled', desired_mode = 'disabled',
                        runtime_state = 'stopped', owner_id = '',
                        lease_token = '', lease_expires_at = NULL,
                        heartbeat_at = NULL, active_cycle_id = NULL,
                        last_error = '', last_reason = 'safe restart',
                        updated_at = ?
                    WHERE id = 1
                    """,
                    (_iso(moment),),
                )
                self._conn.commit()
            except AutonomyStoreError:
                self._conn.rollback()
                raise
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise AutonomyStoreError(
                    f"could not recover the autonomy store: {type(exc).__name__}: {exc}"
                ) from exc
        return self.snapshot(moment)

    def acquire_lease(
        self,
        owner_id: str,
        lease_token: str,
        *,
        lease_seconds: int,
        now: datetime,
    ) -> bool:
        """Atomically acquire/renew the one runtime lease."""

        moment = _utc(now)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM autonomy_runtime WHERE id = 1"
                ).fetchone()
                if row is None:
                    raise AutonomyStoreError("autonomy runtime row is missing")
                expires = _parse(row["lease_expires_at"])
                holder = str(row["owner_id"] or "")
                if holder and holder != owner_id and expires and expires > moment:
                    self._conn.rollback()
                    return False
                if holder and expires and expires <= moment:
                    self._conn.execute(
                        "UPDATE autonomy_cycles SET status = 'interrupted', "
                        "finished_at = ?, last_error = ? WHERE status = 'running'",
                        (_iso(moment), "lease expired before the cycle completed"),
                    )
                self._conn.execute(
                    """
                    UPDATE autonomy_runtime
                    SET owner_id = ?, lease_token = ?, lease_expires_at = ?,
                        heartbeat_at = ?, runtime_state = 'running',
                        last_error = '', updated_at = ?
                    WHERE id = 1
                    """,
                    (
                        owner_id,
                        lease_token,
                        _iso(moment + timedelta(seconds=lease_seconds)),
                        _iso(moment),
                        _iso(moment),
                    ),
                )
                self._conn.commit()
                return True
            except AutonomyStoreError:
                self._conn.rollback()
                raise
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise AutonomyStoreError(
                    f"could not acquire the autonomy lease: {type(exc).__name__}: {exc}"
                ) from exc

    def heartbeat(
        self,
        owner_id: str,
        lease_token: str,
        *,
        lease_seconds: int,
        now: datetime,
    ) -> bool:
        moment = _utc(now)
        with self._lock:
            try:
                cursor = self._conn.execute(
                    """
                    UPDATE autonomy_runtime
                    SET lease_expires_at = ?, heartbeat_at = ?, updated_at = ?
                    WHERE id = 1 AND owner_id = ? AND lease_token = ?
                      AND (lease_expires_at IS NOT NULL AND lease_expires_at > ?)
                    """,
                    (
                        _iso(moment + timedelta(seconds=lease_seconds)),
                        _iso(moment),
                        _iso(moment),
                        owner_id,
                        lease_token,
                        _iso(moment),
                    ),
                )
                self._conn.commit()
                return cursor.rowcount == 1
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise AutonomyStoreError(
                    f"could not renew the autonomy lease: {type(exc).__name__}: {exc}"
                ) from exc

    def save_control(
        self,
        owner_id: str,
        lease_token: str,
        *,
        mode: str,
        state: str,
        reason: str,
        now: datetime,
        desired_mode: str | None = None,
        control: dict[str, Any] | None = None,
    ) -> None:
        desired = mode if desired_mode is None else desired_mode
        with self._lock:
            try:
                cursor = self._conn.execute(
                    """
                    UPDATE autonomy_runtime
                    SET mode = ?, desired_mode = ?, runtime_state = ?,
                        last_reason = ?, updated_at = ?
                    WHERE id = 1 AND owner_id = ? AND lease_token = ?
                    """,
                    (
                        mode,
                        desired,
                        state,
                        reason,
                        _iso(now),
                        owner_id,
                        lease_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise AutonomyLeaseError("the runtime lease is not owned by this instance")
                if control is not None:
                    _insert_control_event(self._conn, dict(control))
                self._conn.commit()
            except AutonomyLeaseError:
                self._conn.rollback()
                raise
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise AutonomyStoreError(
                    f"could not persist the autonomy mode: {type(exc).__name__}: {exc}"
                ) from exc

    def append_control_event(
        self,
        *,
        operation: str,
        outcome: str,
        actor: str = "system",
        reason: str = "",
        now: datetime | None = None,
        from_mode: str = "",
        requested_mode: str = "",
        resulting_mode: str = "",
        desired_mode: str = "",
        lease_owner: str = "",
        lease_valid: bool = False,
        error: str = "",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one control event; it never updates the runtime row.

        Refused and no-op control attempts use this method so the attempt
        is durable without pretending the mode changed.  Successful
        transitions write their event through ``save_control`` or
        ``release_lease`` inside the same transaction as the state change.
        """

        moment = now or datetime.now(UTC)
        control = {
            "operation": operation,
            "outcome": outcome,
            "actor": actor,
            "reason": reason,
            "occurred_at": moment,
            "from_mode": from_mode,
            "requested_mode": requested_mode,
            "resulting_mode": resulting_mode,
            "desired_mode": desired_mode,
            "lease_owner": lease_owner,
            "lease_valid": lease_valid,
            "error": error,
            "details": details or {},
        }
        with self._lock, self._conn:
            _insert_control_event(self._conn, control)
        stored = dict(control)
        stored["occurred_at"] = _iso(moment)
        stored.pop("details", None)
        return stored

    def control_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """The newest control events first; the history is append-only."""

        bounded = max(1, min(int(limit), 200))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM autonomy_control_events ORDER BY seq DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [dict(row) for row in rows]

    def release_lease(
        self,
        owner_id: str,
        lease_token: str,
        *,
        reason: str,
        now: datetime,
        control: dict[str, Any] | None = None,
    ) -> None:
        moment = _utc(now)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "UPDATE autonomy_cycles SET status = 'interrupted', "
                    "finished_at = ?, last_error = ? WHERE status = 'running' "
                    "AND owner_id = ? AND lease_token = ?",
                    (_iso(moment), reason, owner_id, lease_token),
                )
                self._interrupt_monitoring_cycles(
                    moment, reason or "runtime lease released"
                )
                self._conn.execute(
                    """
                    UPDATE autonomy_runtime
                    SET mode = 'disabled', desired_mode = 'disabled',
                        runtime_state = 'stopped', owner_id = '',
                        lease_token = '', lease_expires_at = NULL,
                        heartbeat_at = NULL, active_cycle_id = NULL,
                        last_reason = ?, updated_at = ?
                    WHERE id = 1 AND owner_id = ? AND lease_token = ?
                    """,
                    (reason, _iso(moment), owner_id, lease_token),
                )
                if control is not None:
                    _insert_control_event(self._conn, dict(control))
                self._conn.commit()
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise AutonomyStoreError(
                    f"could not release the autonomy lease: {type(exc).__name__}: {exc}"
                ) from exc

    # -- one active cycle ----------------------------------------------------

    def claim_cycle(
        self,
        cycle_id: str,
        cycle_key: str,
        *,
        owner_id: str,
        lease_token: str,
        now: datetime,
    ) -> dict[str, Any]:
        moment = _utc(now)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM autonomy_runtime WHERE id = 1"
                ).fetchone()
                if row is None:
                    raise AutonomyStoreError("autonomy runtime row is missing")
                expires = _parse(row["lease_expires_at"])
                if (
                    str(row["owner_id"] or "") != owner_id
                    or str(row["lease_token"] or "") != lease_token
                    or expires is None
                    or expires <= moment
                ):
                    raise AutonomyLeaseError("an active runtime lease is required")
                if self._conn.execute(
                    "SELECT 1 FROM autonomy_cycles WHERE status = 'running' LIMIT 1"
                ).fetchone():
                    raise AutonomyLeaseError("another autonomy cycle is already active")
                self._conn.execute(
                    """
                    INSERT INTO autonomy_cycles
                        (cycle_id, cycle_key, owner_id, lease_token, status,
                         started_at, finished_at, last_error)
                    VALUES (?, ?, ?, ?, 'running', ?, NULL, '')
                    """,
                    (
                        cycle_id,
                        cycle_key,
                        owner_id,
                        lease_token,
                        _iso(moment),
                    ),
                )
                self._conn.execute(
                    "UPDATE autonomy_runtime SET active_cycle_id = ?, updated_at = ? "
                    "WHERE id = 1",
                    (cycle_id, _iso(moment)),
                )
                self._conn.commit()
            except (AutonomyLeaseError, AutonomyStoreError):
                self._conn.rollback()
                raise
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise AutonomyLeaseError(
                    "cycle_key or the single-active-cycle constraint was violated"
                ) from exc
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise AutonomyStoreError(
                    f"could not claim an autonomy cycle: {type(exc).__name__}: {exc}"
                ) from exc
        return self.snapshot(moment)["active_cycle"] or {}

    def finish_cycle(
        self,
        cycle_id: str,
        *,
        owner_id: str,
        status: str,
        error: str = "",
        now: datetime,
    ) -> None:
        moment = _utc(now)
        if status not in {"completed", "failed", "interrupted"}:
            raise AutonomyValidationError("a finished cycle must be completed, failed or interrupted")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cursor = self._conn.execute(
                    """
                    UPDATE autonomy_cycles
                    SET status = ?, finished_at = ?, last_error = ?
                    WHERE cycle_id = ? AND owner_id = ? AND status = 'running'
                    """,
                    (status, _iso(moment), error, cycle_id, owner_id),
                )
                if cursor.rowcount != 1:
                    raise AutonomyLeaseError("this runtime does not own the active cycle")
                self._conn.execute(
                    "UPDATE autonomy_runtime SET active_cycle_id = NULL, updated_at = ? "
                    "WHERE id = 1 AND active_cycle_id = ?",
                    (_iso(moment), cycle_id),
                )
                self._conn.commit()
            except (AutonomyLeaseError, AutonomyStoreError):
                self._conn.rollback()
                raise
            except AutonomyValidationError:
                self._conn.rollback()
                raise
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise AutonomyStoreError(
                    f"could not finish an autonomy cycle: {type(exc).__name__}: {exc}"
                ) from exc

    # -- AT-02 monitoring configuration and state ---------------------------

    def monitoring_config(self) -> MonitoringConfig:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM autonomy_monitoring_config WHERE id = 1"
            ).fetchone()
        if row is None:
            raise AutonomyStoreError("monitoring configuration row is missing")
        return MonitoringConfig(
            enabled=bool(row["enabled"]),
            exchange=str(row["exchange"]),
            interval_seconds=int(row["interval_seconds"]),
            max_instruments_per_cycle=int(row["max_instruments_per_cycle"]),
            retry_max=int(row["retry_max"]),
            cycle_timeout_seconds=int(row["cycle_timeout_seconds"]),
        )

    def save_monitoring_config(
        self, config: MonitoringConfig, *, now: datetime
    ) -> MonitoringConfig:
        with self._lock:
            try:
                cursor = self._conn.execute(
                    """
                    UPDATE autonomy_monitoring_config
                    SET enabled = ?, exchange = ?, interval_seconds = ?,
                        max_instruments_per_cycle = ?, retry_max = ?,
                        cycle_timeout_seconds = ?, updated_at = ?
                    WHERE id = 1
                    """,
                    (
                        1 if config.enabled else 0,
                        config.exchange,
                        config.interval_seconds,
                        config.max_instruments_per_cycle,
                        config.retry_max,
                        config.cycle_timeout_seconds,
                        _iso(now),
                    ),
                )
                if cursor.rowcount != 1:
                    raise AutonomyStoreError(
                        "monitoring configuration row is missing"
                    )
                self._conn.commit()
            except AutonomyStoreError:
                self._conn.rollback()
                raise
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise AutonomyStoreError(
                    f"could not save the monitoring configuration: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        return self.monitoring_config()

    def monitoring_state(self) -> dict[str, Any]:
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO autonomy_monitoring_state
                        (id, rotation_cursor, last_cycle_id, last_completed_at,
                         last_attempt_at, last_error_code, last_error,
                         consecutive_failures, updated_at)
                    VALUES (1, 0, NULL, NULL, NULL, '', '', 0, ?)
                    """,
                    (_iso(),),
                )
                self._conn.commit()
                row = self._conn.execute(
                    "SELECT * FROM autonomy_monitoring_state WHERE id = 1"
                ).fetchone()
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise AutonomyStoreError(
                    f"could not read the monitoring state: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        if row is None:
            raise AutonomyStoreError("monitoring state row is missing")
        return {
            "rotation_cursor": max(0, int(row["rotation_cursor"])),
            "last_cycle_id": row["last_cycle_id"],
            "last_completed_at": row["last_completed_at"],
            "last_attempt_at": row["last_attempt_at"],
            "last_error_code": str(row["last_error_code"] or ""),
            "last_error": str(row["last_error"] or ""),
            "consecutive_failures": int(row["consecutive_failures"]),
            "updated_at": row["updated_at"],
        }

    def _update_monitoring_state(self, sets: str, values: tuple[Any, ...]) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    f"UPDATE autonomy_monitoring_state SET {sets} WHERE id = 1",
                    values,
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise AutonomyStoreError(
                    f"could not update the monitoring state: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

    def set_monitoring_attempt(self, *, now: datetime) -> None:
        self._update_monitoring_state(
            "last_attempt_at = ?, updated_at = ?", (_iso(now), _iso(now))
        )

    def set_rotation(self, cursor: int, *, now: datetime) -> None:
        self._update_monitoring_state(
            "rotation_cursor = ?, updated_at = ?",
            (max(0, int(cursor)), _iso(now)),
        )

    def note_monitoring_success(self, cycle_id: str, *, now: datetime) -> None:
        self._update_monitoring_state(
            "last_cycle_id = ?, last_completed_at = ?, consecutive_failures = 0, "
            "last_error_code = '', last_error = '', updated_at = ?",
            (cycle_id, _iso(now), _iso(now)),
        )

    def note_monitoring_failure(
        self, code: str, error: str, *, now: datetime
    ) -> None:
        self._update_monitoring_state(
            "consecutive_failures = consecutive_failures + 1, "
            "last_error_code = ?, last_error = ?, updated_at = ?",
            (str(code or ""), str(error or "")[:500], _iso(now)),
        )

    # -- AT-02 monitoring cycles ---------------------------------------------

    def _interrupt_monitoring_cycles(self, moment: datetime, reason: str) -> int:
        """Mark every non-terminal monitoring cycle failed, never completed."""

        active = tuple(sorted(ACTIVE_MONITORING_STATUSES))
        placeholders = ",".join("?" for _ in active)
        cursor = self._conn.execute(
            "UPDATE autonomy_monitoring_cycles SET status = 'failed', "
            "phase = 'cancelled', finished_at = ?, error_code = 'CYCLE_CANCELLED', "
            f"error = ? WHERE status IN ({placeholders})",
            (_iso(moment), reason, *active),
        )
        return int(cursor.rowcount or 0)

    def create_monitoring_cycle(
        self, record: dict[str, Any], *, now: datetime
    ) -> None:
        cycle_id = str(record.get("cycle_id") or "").strip()
        cycle_key = str(record.get("cycle_key") or "").strip()
        if not cycle_id or not cycle_key:
            raise AutonomyValidationError(
                "a monitoring cycle needs a cycle_id and cycle_key"
            )
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO autonomy_monitoring_cycles
                        (cycle_id, autonomy_cycle_id, cycle_key, owner_id, mode,
                         exchange, trading_date, calendar_verified,
                         calendar_source, session_state, session_eligible,
                         phase, status, trigger, started_at, finished_at,
                         discovered_count, eligible_count, processed_count,
                         deferred_count, rejected_count, rotation_start,
                         rotation_end, error_code, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cycle_id,
                        str(record.get("autonomy_cycle_id") or ""),
                        cycle_key,
                        str(record.get("owner_id") or ""),
                        str(record.get("mode") or ""),
                        str(record.get("exchange") or ""),
                        str(record.get("trading_date") or ""),
                        1 if record.get("calendar_verified") else 0,
                        str(record.get("calendar_source") or ""),
                        str(record.get("session_state") or "unknown"),
                        1 if record.get("session_eligible") else 0,
                        str(record.get("phase") or "created"),
                        str(record.get("status") or "created"),
                        str(record.get("trigger") or "scheduled"),
                        str(record.get("started_at") or _iso(now)),
                        int(record.get("discovered_count") or 0),
                        int(record.get("eligible_count") or 0),
                        int(record.get("processed_count") or 0),
                        int(record.get("deferred_count") or 0),
                        int(record.get("rejected_count") or 0),
                        int(record.get("rotation_start") or 0),
                        int(record.get("rotation_end") or 0),
                        str(record.get("error_code") or ""),
                        str(record.get("error") or "")[:1000],
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise AutonomyValidationError(
                    "a monitoring cycle with this key already exists"
                ) from exc
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise AutonomyStoreError(
                    f"could not create a monitoring cycle: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

    _MONITORING_UPDATE_COLUMNS = frozenset(
        {
            "autonomy_cycle_id",
            "phase",
            "status",
            "session_state",
            "session_eligible",
            "calendar_verified",
            "calendar_source",
            "discovered_count",
            "eligible_count",
            "processed_count",
            "deferred_count",
            "rejected_count",
            "rotation_start",
            "rotation_end",
            "error_code",
            "error",
        }
    )

    def update_monitoring_cycle(
        self,
        cycle_id: str,
        *,
        now: datetime,
        finished: bool = False,
        **fields: Any,
    ) -> None:
        unknown = sorted(set(fields) - self._MONITORING_UPDATE_COLUMNS)
        if unknown:
            raise AutonomyValidationError(
                f"unknown monitoring cycle fields: {', '.join(unknown)}"
            )
        if not fields and not finished:
            return
        sets: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            sets.append(f"{key} = ?")
            if key in ("session_eligible", "calendar_verified"):
                values.append(1 if value else 0)
            elif key == "error":
                values.append(str(value or "")[:1000])
            else:
                values.append(value)
        if finished:
            sets.append("finished_at = ?")
            values.append(_iso(now))
        values.append(cycle_id)
        with self._lock:
            try:
                cursor = self._conn.execute(
                    f"UPDATE autonomy_monitoring_cycles SET {', '.join(sets)} "
                    "WHERE cycle_id = ?",
                    tuple(values),
                )
                if cursor.rowcount != 1:
                    raise AutonomyStoreError(
                        f"unknown monitoring cycle {cycle_id!r}"
                    )
                self._conn.commit()
            except AutonomyStoreError:
                self._conn.rollback()
                raise
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise AutonomyStoreError(
                    f"could not update a monitoring cycle: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

    def record_monitoring_candidates(
        self, cycle_id: str, rows: list[dict[str, Any]]
    ) -> None:
        if not rows:
            return
        with self._lock:
            try:
                self._conn.executemany(
                    """
                    INSERT OR REPLACE INTO autonomy_monitoring_candidates
                        (cycle_id, instrument_id, symbol, exchange,
                         disposition, reason, rank, data_quality)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            cycle_id,
                            str(row.get("instrument_id") or ""),
                            str(row.get("symbol") or ""),
                            str(row.get("exchange") or ""),
                            str(row.get("disposition") or ""),
                            str(row.get("reason") or "")[:500],
                            int(row.get("rank") or 0),
                            str(row.get("data_quality") or "")[:40],
                        )
                        for row in rows
                    ],
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise AutonomyStoreError(
                    f"could not record monitoring candidates: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

    def monitoring_cycles(self, *, limit: int = 20) -> list[dict[str, Any]]:
        capped = max(1, min(int(limit), 200))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM autonomy_monitoring_cycles "
                "ORDER BY started_at DESC LIMIT ?",
                (capped,),
            ).fetchall()
        return [dict(row) for row in rows]

    def monitoring_cycle(self, cycle_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM autonomy_monitoring_cycles WHERE cycle_id = ?",
                (cycle_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def latest_monitoring_cycle(self) -> dict[str, Any] | None:
        rows = self.monitoring_cycles(limit=1)
        return rows[0] if rows else None

    def monitoring_candidates(
        self, cycle_id: str, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        capped = max(1, min(int(limit), 2000))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM autonomy_monitoring_candidates "
                "WHERE cycle_id = ? ORDER BY rank, instrument_id LIMIT ?",
                (cycle_id, capped),
            ).fetchall()
        return [dict(row) for row in rows]

    def recover_monitoring(self, now: datetime) -> int:
        with self._lock:
            try:
                count = self._interrupt_monitoring_cycles(
                    now, "monitoring cycle interrupted by process restart"
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise AutonomyStoreError(
                    f"could not recover monitoring cycles: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        return count

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


__all__ = ["AutonomyStore"]
