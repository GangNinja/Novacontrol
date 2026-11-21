"""Isolated SQLite persistence for PI-20 dashboard preferences.

Only filter presets live here.  The store never opens a portfolio, ledger,
forecast, evaluation, research, risk or execution table; it cannot mutate any
financial state even if a caller tries to reuse it for that purpose.
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
    DASHBOARD_CONFIG_LIMIT,
    DASHBOARD_NAME_LIMIT,
    DASHBOARD_SCHEMA_VERSION,
    DashboardConfig,
    DashboardFilters,
    DashboardStoreError,
    DashboardValidationError,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pd_dashboard_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pd_dashboard_configs (
    name TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class DashboardStore:
    """Bounded, versioned preference storage in its own SQLite file."""

    def __init__(self, path: str | Path) -> None:
        try:
            raw_path = str(path)
            self.path = Path(raw_path)
            if raw_path != ":memory:":
                self.path.parent.mkdir(parents=True, exist_ok=True)
            self._lock = threading.RLock()
            self._connection = sqlite3.connect(
                raw_path if raw_path == ":memory:" else self.path,
                check_same_thread=False,
                timeout=10.0,
            )
            self._connection.row_factory = sqlite3.Row
            with self._lock, self._connection:
                self._connection.executescript(_SCHEMA)
                self._connection.execute(
                    "INSERT OR IGNORE INTO pd_dashboard_meta(key, value) VALUES(?, ?)",
                    ("schema_version", str(DASHBOARD_SCHEMA_VERSION)),
                )
        except (OSError, sqlite3.Error) as exc:
            raise DashboardStoreError(
                f"dashboard preference store unavailable: {type(exc).__name__}: {exc}"
            ) from exc

    def save_config(
        self,
        name: str,
        filters: DashboardFilters | Mapping[str, Any],
        *,
        updated_at: datetime | None = None,
    ) -> dict[str, Any]:
        config = DashboardConfig(
            name=name or "default",
            filters=(
                filters if isinstance(filters, DashboardFilters)
                else DashboardFilters.from_mapping(filters)
            ),
            updated_at=updated_at or datetime.now(tz=UTC),
        )
        if len(config.name) > DASHBOARD_NAME_LIMIT:
            raise DashboardValidationError("dashboard preset name is too long")
        payload = json.dumps(config.to_dict(), sort_keys=True, separators=(",", ":"))
        if len(payload.encode("utf-8")) > DASHBOARD_CONFIG_LIMIT:
            raise DashboardValidationError("dashboard preset is too large")
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    "INSERT INTO pd_dashboard_configs(name, payload, updated_at) "
                    "VALUES(?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                    "payload=excluded.payload, updated_at=excluded.updated_at",
                    (config.name, payload, config.updated_at.isoformat()),
                )
        except sqlite3.Error as exc:
            raise DashboardStoreError(f"could not save dashboard preset: {exc}") from exc
        return config.to_dict()

    def get_config(self, name: str = "default") -> dict[str, Any] | None:
        clean = (name or "default").strip()[:DASHBOARD_NAME_LIMIT]
        try:
            with self._lock:
                row = self._connection.execute(
                    "SELECT payload FROM pd_dashboard_configs WHERE name=?",
                    (clean,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise DashboardStoreError(f"could not read dashboard preset: {exc}") from exc
        if row is None:
            return None
        try:
            payload = json.loads(str(row["payload"]))
        except (TypeError, ValueError) as exc:
            raise DashboardStoreError("stored dashboard preset is invalid") from exc
        if not isinstance(payload, dict):
            raise DashboardStoreError("stored dashboard preset is invalid")
        return payload

    def list_configs(self, *, limit: int = 30) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 100))
        try:
            with self._lock:
                rows = self._connection.execute(
                    "SELECT payload FROM pd_dashboard_configs "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise DashboardStoreError(f"could not list dashboard presets: {exc}") from exc
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload"]))
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                result.append(payload)
        return result

    def health(self) -> dict[str, Any]:
        try:
            with self._lock:
                configs = int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM pd_dashboard_configs"
                    ).fetchone()[0]
                )
        except sqlite3.Error as exc:
            return {"ok": False, "reason": str(exc), "path": str(self.path)}
        return {
            "ok": True,
            "path": str(self.path),
            "configs": configs,
            "schema_version": DASHBOARD_SCHEMA_VERSION,
            "financial_tables": [],
        }

    def close(self) -> None:
        with self._lock, contextlib.suppress(sqlite3.Error):
            self._connection.close()
