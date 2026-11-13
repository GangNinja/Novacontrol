"""NM-09 persistence: strategies, versions, signals, proposals, conflicts.

Signals and lifecycle rows are append-only. A proposal's status is the only
mutable field, and it moves only through the engine's explicit transitions.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

from novacontrol.novamarket.strategies.models import (
    ConflictOutcome,
    ConflictRecord,
    ConflictType,
    LifecycleRecord,
    ProposalStatus,
    ProposedAction,
    SignalType,
    StrategyCategory,
    StrategyRecord,
    StrategySignal,
    StrategyStatus,
    utcnow,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategies (
    strategy_id TEXT PRIMARY KEY,
    catalog_id TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strategies_status ON strategies(status, updated_at);
CREATE TABLE IF NOT EXISTS strategy_versions (
    strategy_id TEXT NOT NULL,
    version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL,
    PRIMARY KEY (strategy_id, version)
);
CREATE TABLE IF NOT EXISTS strategy_signals (
    signal_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    portfolio_id TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strategy_signals_strategy
    ON strategy_signals(strategy_id, created_at);
CREATE TABLE IF NOT EXISTS strategy_proposals (
    proposal_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    portfolio_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strategy_proposals_status
    ON strategy_proposals(status, created_at);
CREATE TABLE IF NOT EXISTS strategy_conflicts (
    conflict_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    portfolio_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_lifecycle (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    at TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_strategy_lifecycle_strategy
    ON strategy_lifecycle(strategy_id, id);
CREATE TRIGGER IF NOT EXISTS strategy_signals_immutable_update
BEFORE UPDATE ON strategy_signals
BEGIN
    SELECT RAISE(ABORT, 'strategy signals are immutable');
END;
CREATE TRIGGER IF NOT EXISTS strategy_signals_immutable_delete
BEFORE DELETE ON strategy_signals
BEGIN
    SELECT RAISE(ABORT, 'strategy signals are immutable');
END;
"""


class StrategyStore:
    def __init__(
        self, path: str = ":memory:", *, clock: Callable[[], datetime] | None = None
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
                "INSERT OR IGNORE INTO strategy_meta(key, value) VALUES(?, ?)",
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

    # -- strategies -----------------------------------------------------------
    def save_strategy(self, record: StrategyRecord, *, actor: str = "") -> None:
        body = json.dumps(record.to_dict(), sort_keys=True)
        with self.transaction():
            self._connection.execute(
                "INSERT INTO strategies(strategy_id, catalog_id, name, category, status, "
                "version, created_at, updated_at, payload) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(strategy_id) DO UPDATE SET name=excluded.name, "
                "status=excluded.status, version=excluded.version, "
                "updated_at=excluded.updated_at, payload=excluded.payload",
                (
                    record.strategy_id,
                    record.catalog_id,
                    record.name,
                    record.category.value,
                    record.status.value,
                    record.version,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                    body,
                ),
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO strategy_versions(strategy_id, version, created_at, "
                "actor, payload) VALUES (?,?,?,?,?)",
                (
                    record.strategy_id,
                    record.version,
                    self._clock().isoformat(),
                    actor,
                    body,
                ),
            )

    def strategy(self, strategy_id: str) -> StrategyRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM strategies WHERE strategy_id=?", (strategy_id,)
            ).fetchone()
        return _strategy(json.loads(row["payload"])) if row else None

    def strategies(
        self, *, status: str = "", category: str = "", limit: int = 100
    ) -> list[StrategyRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if category:
            clauses.append("category=?")
            params.append(category)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM strategies" + where + " ORDER BY created_at ASC, rowid ASC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [_strategy(json.loads(row["payload"])) for row in rows]

    def versions(self, strategy_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM strategy_versions WHERE strategy_id=? "
                "ORDER BY created_at ASC, rowid ASC",
                (strategy_id,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    # -- signals --------------------------------------------------------------
    def save_signal(self, signal: StrategySignal) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO strategy_signals(signal_id, strategy_id, strategy_version, "
                "instrument_id, portfolio_id, signal_type, created_at, payload) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    signal.signal_id,
                    signal.strategy_id,
                    signal.strategy_version,
                    signal.instrument_id,
                    signal.portfolio_id,
                    signal.signal_type.value,
                    signal.created_at.isoformat(),
                    json.dumps(signal.to_dict(), sort_keys=True),
                ),
            )

    def signal(self, signal_id: str) -> StrategySignal | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM strategy_signals WHERE signal_id=?", (signal_id,)
            ).fetchone()
        return _signal(json.loads(row["payload"])) if row else None

    def signals(
        self,
        *,
        strategy_id: str = "",
        portfolio_id: str = "",
        signal_type: str = "",
        limit: int = 200,
    ) -> list[StrategySignal]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("strategy_id", strategy_id),
            ("portfolio_id", portfolio_id),
            ("signal_type", signal_type),
        ):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM strategy_signals"
                + where
                + " ORDER BY created_at ASC, rowid ASC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [_signal(json.loads(row["payload"])) for row in rows]

    # -- proposals ------------------------------------------------------------
    def save_proposal(self, proposal: ProposedAction) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO strategy_proposals(proposal_id, strategy_id, signal_id, "
                "portfolio_id, instrument_id, status, created_at, updated_at, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    proposal.proposal_id,
                    proposal.strategy_id,
                    proposal.signal_id,
                    proposal.portfolio_id,
                    proposal.instrument_id,
                    proposal.status.value,
                    proposal.created_at.isoformat(),
                    (proposal.updated_at or proposal.created_at).isoformat(),
                    json.dumps(proposal.to_dict(), sort_keys=True),
                ),
            )

    def update_proposal(self, proposal: ProposedAction) -> None:
        with self.transaction():
            self._connection.execute(
                "UPDATE strategy_proposals SET status=?, updated_at=?, payload=? "
                "WHERE proposal_id=?",
                (
                    proposal.status.value,
                    (proposal.updated_at or self._clock()).isoformat(),
                    json.dumps(proposal.to_dict(), sort_keys=True),
                    proposal.proposal_id,
                ),
            )

    def proposal(self, proposal_id: str) -> ProposedAction | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM strategy_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
        return _proposal(json.loads(row["payload"])) if row else None

    def proposals(
        self,
        *,
        strategy_id: str = "",
        portfolio_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> list[ProposedAction]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("strategy_id", strategy_id),
            ("portfolio_id", portfolio_id),
            ("status", status),
        ):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM strategy_proposals"
                + where
                + " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [_proposal(json.loads(row["payload"])) for row in rows]

    # -- conflicts / lifecycle ------------------------------------------------
    def save_conflict(self, conflict: ConflictRecord) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT OR IGNORE INTO strategy_conflicts(conflict_id, created_at, "
                "portfolio_id, instrument_id, payload) VALUES (?,?,?,?,?)",
                (
                    conflict.conflict_id,
                    conflict.created_at.isoformat(),
                    conflict.portfolio_id,
                    conflict.instrument_id,
                    json.dumps(conflict.to_dict(), sort_keys=True),
                ),
            )

    def conflicts(self, limit: int = 100) -> list[ConflictRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM strategy_conflicts ORDER BY created_at DESC, "
                "rowid DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [_conflict(json.loads(row["payload"])) for row in rows]

    def save_lifecycle(self, record: LifecycleRecord) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO strategy_lifecycle(strategy_id, at, from_status, to_status, "
                "reason, actor) VALUES (?,?,?,?,?,?)",
                (
                    record.strategy_id,
                    record.at.isoformat(),
                    record.from_status,
                    record.to_status,
                    record.reason,
                    record.actor,
                ),
            )

    def lifecycle(self, strategy_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT strategy_id, at, from_status, to_status, reason, actor "
                "FROM strategy_lifecycle WHERE strategy_id=? ORDER BY id ASC",
                (strategy_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            strategies = self._connection.execute(
                "SELECT status, COUNT(*) AS n FROM strategies GROUP BY status"
            ).fetchall()
            signals = self._connection.execute(
                "SELECT signal_type, COUNT(*) AS n FROM strategy_signals GROUP BY signal_type"
            ).fetchall()
            proposals = self._connection.execute(
                "SELECT status, COUNT(*) AS n FROM strategy_proposals GROUP BY status"
            ).fetchall()
            conflicts = self._connection.execute(
                "SELECT COUNT(*) AS n FROM strategy_conflicts"
            ).fetchone()["n"]
        return {
            "strategies": sum(row["n"] for row in strategies),
            "by_status": {row["status"]: row["n"] for row in strategies},
            "signals": sum(row["n"] for row in signals),
            "signals_by_type": {row["signal_type"]: row["n"] for row in signals},
            "proposals": sum(row["n"] for row in proposals),
            "proposals_by_status": {row["status"]: row["n"] for row in proposals},
            "conflicts": conflicts,
        }


def _strategy(body: dict[str, Any]) -> StrategyRecord:
    return StrategyRecord(
        strategy_id=str(body["strategy_id"]),
        catalog_id=str(body["catalog_id"]),
        name=str(body["name"]),
        category=StrategyCategory(str(body["category"])),
        status=StrategyStatus(str(body["status"])),
        version=str(body["version"]),
        config=dict(body.get("config") or {}),
        horizons=tuple(str(item) for item in body.get("horizons", ())),
        asset_classes=tuple(str(item) for item in body.get("asset_classes", ())),
        priority=int(body.get("priority", 50)),
        created_at=datetime.fromisoformat(str(body["created_at"])),
        updated_at=datetime.fromisoformat(str(body["updated_at"])),
        last_evaluated_at=(
            datetime.fromisoformat(str(body["last_evaluated_at"]))
            if body.get("last_evaluated_at")
            else None
        ),
        status_reason=str(body.get("status_reason") or ""),
    )


def _signal(body: dict[str, Any]) -> StrategySignal:
    return StrategySignal(
        signal_id=str(body["signal_id"]),
        strategy_id=str(body["strategy_id"]),
        strategy_version=str(body["strategy_version"]),
        instrument_id=str(body["instrument_id"]),
        portfolio_id=str(body["portfolio_id"]),
        signal_type=SignalType(str(body["signal_type"])),
        created_at=datetime.fromisoformat(str(body["created_at"])),
        data_cutoff=datetime.fromisoformat(str(body["market_data_cutoff"])),
        evaluation_horizon=str(body.get("evaluation_horizon") or ""),
        indicators=dict(body.get("indicators") or {}),
        forecast_refs=tuple(str(item) for item in body.get("forecast_references", ())),
        event_refs=tuple(str(item) for item in body.get("event_references", ())),
        conditions=tuple(str(item) for item in body.get("triggered_conditions", ())),
        confidence=Decimal(str(body.get("confidence") or "0")),
        explanation=str(body.get("explanation") or ""),
        expires_at=datetime.fromisoformat(str(body["expires_at"])),
        reference_price=(
            Decimal(str(body["reference_price"])) if body.get("reference_price") else None
        ),
        data_quality=str(body.get("data_quality") or ""),
        horizon_days=int(body.get("horizon_days") or 5),
    )


def _proposal(body: dict[str, Any]) -> ProposedAction:
    return ProposedAction(
        proposal_id=str(body["proposal_id"]),
        signal_id=str(body["signal_id"]),
        strategy_id=str(body["strategy_id"]),
        strategy_version=str(body["strategy_version"]),
        instrument_id=str(body["instrument_id"]),
        portfolio_id=str(body["portfolio_id"]),
        action=str(body["action"]),
        side=str(body["side"]),
        notional=Decimal(str(body["notional"])),
        reference_price=(
            Decimal(str(body["reference_price"])) if body.get("reference_price") else None
        ),
        price_at=(
            datetime.fromisoformat(str(body["price_at"])) if body.get("price_at") else None
        ),
        created_at=datetime.fromisoformat(str(body["created_at"])),
        expires_at=datetime.fromisoformat(str(body["expires_at"])),
        reason=str(body.get("reason") or ""),
        forecast_refs=tuple(str(item) for item in body.get("forecast_references", ())),
        event_refs=tuple(str(item) for item in body.get("event_references", ())),
        status=ProposalStatus(str(body["status"])),
        risk_decision_id=str(body.get("risk_decision_id") or ""),
        risk_outcome=str(body.get("risk_outcome") or ""),
        risk_policy_version=str(body.get("risk_policy_version") or ""),
        risk_state_version=str(body.get("risk_state_version") or ""),
        risk_restrictions=tuple(str(item) for item in body.get("risk_restrictions", ())),
        risk_error=str(body.get("risk_error") or ""),
        conflict_id=str(body.get("conflict_id") or ""),
        updated_at=(
            datetime.fromisoformat(str(body["updated_at"]))
            if body.get("updated_at")
            else None
        ),
        execution_order_id=str(body.get("execution_order_id") or ""),
        execution_status=str(body.get("execution_status") or ""),
    )


def _conflict(body: dict[str, Any]) -> ConflictRecord:
    return ConflictRecord(
        conflict_id=str(body["conflict_id"]),
        portfolio_id=str(body["portfolio_id"]),
        instrument_id=str(body["instrument_id"]),
        conflict_type=ConflictType(str(body["conflict_type"])),
        strategy_ids=tuple(str(item) for item in body.get("strategy_ids", ())),
        signal_ids=tuple(str(item) for item in body.get("signal_ids", ())),
        outcome=ConflictOutcome(str(body["outcome"])),
        rationale=str(body.get("rationale") or ""),
        created_at=datetime.fromisoformat(str(body["created_at"])),
        selected_signal_id=str(body.get("selected_signal_id") or ""),
        selected_strategy_id=str(body.get("selected_strategy_id") or ""),
    )


__all__ = ["SCHEMA_VERSION", "StrategyStore"]
