"""Isolated persistence for PI-17 research cycles.

Tables use the ``ma_`` prefix in their own SQLite file.  They hold research
history, agent messages, evidence provenance and advisory decisions only.  No
portfolio, risk, ledger, order, fill or autonomy table is ever opened.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from novacontrol.novamarket.multiagent.models import (
    MAX_AGENT_RUNS,
    MAX_CYCLES,
    MAX_EVIDENCE,
    MAX_MESSAGES,
    AgentConfiguration,
    AgentHealth,
    AgentMessage,
    AgentPerformance,
    AgentResponse,
    AgentRunStatus,
    ConflictRecord,
    CycleStatus,
    DecisionCycle,
    DecisionRecord,
    EvidenceRecord,
    MultiAgentResourceError,
    MultiAgentStoreError,
)

_SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ma_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ma_configs (
    agent_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ma_config_audit (
    audit_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ma_cycles (
    cycle_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    exchange TEXT NOT NULL,
    horizon TEXT NOT NULL,
    status TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ma_cycles_instrument
    ON ma_cycles(instrument_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ma_cycles_status
    ON ma_cycles(status, created_at);
CREATE TABLE IF NOT EXISTS ma_runs (
    run_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    action TEXT NOT NULL,
    error TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ma_runs_cycle ON ma_runs(cycle_id);
CREATE INDEX IF NOT EXISTS idx_ma_runs_agent ON ma_runs(agent_id, created_at);
CREATE TABLE IF NOT EXISTS ma_messages (
    message_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    sender_agent_id TEXT NOT NULL,
    recipient_agent_id TEXT NOT NULL,
    message_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ma_messages_cycle ON ma_messages(cycle_id);
CREATE TABLE IF NOT EXISTS ma_evidence (
    evidence_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    freshness TEXT NOT NULL,
    validation TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ma_evidence_cycle ON ma_evidence(cycle_id, agent_id);
CREATE TABLE IF NOT EXISTS ma_conflicts (
    conflict_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ma_conflicts_cycle ON ma_conflicts(cycle_id);
CREATE TABLE IF NOT EXISTS ma_decisions (
    decision_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL UNIQUE,
    instrument_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ma_decisions_instrument
    ON ma_decisions(instrument_id, created_at);
CREATE TABLE IF NOT EXISTS ma_events (
    event_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ma_events_cycle ON ma_events(cycle_id, created_at);
"""


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).isoformat()


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str)


def _load(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


class MultiAgentStore:
    """Bounded persistence for PI-17 research records."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_cycles: int = MAX_CYCLES,
        max_messages: int = MAX_MESSAGES,
        max_evidence: int = MAX_EVIDENCE,
        max_runs: int = MAX_AGENT_RUNS,
    ) -> None:
        self._path = str(path)
        self.max_cycles = max(1, min(int(max_cycles), MAX_CYCLES))
        self.max_messages = max(1, min(int(max_messages), MAX_MESSAGES))
        self.max_evidence = max(1, min(int(max_evidence), MAX_EVIDENCE))
        self.max_runs = max(1, min(int(max_runs), MAX_AGENT_RUNS))
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._connection = sqlite3.connect(
                self._path, timeout=30.0, check_same_thread=False
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.executescript(_SCHEMA)
            self._connection.execute(
                "INSERT OR IGNORE INTO ma_schema_meta(key, value) VALUES('schema_version', ?)",
                (str(_SCHEMA_VERSION),),
            )
            self._connection.commit()
        except sqlite3.Error as exc:
            raise MultiAgentStoreError(f"could not open multi-agent store: {exc}") from exc

    @property
    def path(self) -> str:
        return self._path

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM ma_schema_meta WHERE key='schema_version'"
            ).fetchone()
            return int(row["value"]) if row else 0

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _count(self, table: str) -> int:
        row = self._connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"]) if row else 0

    def _guard(self, table: str, limit: int) -> None:
        if self._count(table) >= limit:
            raise MultiAgentResourceError(
                f"multi-agent research store limit reached for {table}"
            )

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "store": Path(self._path).name if self._path != ":memory:" else ":memory:",
                "schema_version": self.schema_version,
                "cycles": self._count("ma_cycles"),
                "agent_runs": self._count("ma_runs"),
                "messages": self._count("ma_messages"),
                "evidence": self._count("ma_evidence"),
                "conflicts": self._count("ma_conflicts"),
                "decisions": self._count("ma_decisions"),
                "configs": self._count("ma_configs"),
                "limits": {
                    "cycles": self.max_cycles,
                    "messages": self.max_messages,
                    "evidence": self.max_evidence,
                    "agent_runs": self.max_runs,
                },
                "isolated_research_tables": True,
                "portfolio_state_access": False,
                "ledger_access": False,
                "execution_access": False,
            }

    def ensure_configs(self, configs: Sequence[AgentConfiguration]) -> None:
        with self._lock:
            for config in configs:
                self._connection.execute(
                    "INSERT OR IGNORE INTO ma_configs(agent_id, version, payload_json, updated_at) "
                    "VALUES(?, ?, ?, ?)",
                    (
                        config.agent_id,
                        config.version,
                        _json(config.to_dict()),
                        _iso(config.updated_at),
                    ),
                )
            self._connection.commit()

    def configs(self) -> dict[str, AgentConfiguration]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT agent_id, payload_json FROM ma_configs ORDER BY agent_id"
            ).fetchall()
        return {
            str(row["agent_id"]): AgentConfiguration.from_dict(_load(row["payload_json"]))
            for row in rows
        }

    def config(self, agent_id: str) -> AgentConfiguration | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM ma_configs WHERE agent_id=?", (agent_id,)
            ).fetchone()
        return AgentConfiguration.from_dict(_load(row["payload_json"])) if row else None

    def save_config(
        self, config: AgentConfiguration, *, audit_id: str
    ) -> AgentConfiguration:
        payload = config.to_dict()
        with self._lock:
            current = self._connection.execute(
                "SELECT version FROM ma_configs WHERE agent_id=?", (config.agent_id,)
            ).fetchone()
            version = (int(current["version"]) + 1) if current else 1
            saved = AgentConfiguration.from_dict({**payload, "version": version})
            self._connection.execute(
                "INSERT INTO ma_configs(agent_id, version, payload_json, updated_at) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(agent_id) DO UPDATE SET "
                "version=excluded.version, payload_json=excluded.payload_json, "
                "updated_at=excluded.updated_at",
                (saved.agent_id, saved.version, _json(saved.to_dict()), _iso(saved.updated_at)),
            )
            self._connection.execute(
                "INSERT INTO ma_config_audit("
                "audit_id, agent_id, version, payload_json, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (audit_id, saved.agent_id, saved.version, _json(saved.to_dict()), _iso()),
            )
            self._connection.commit()
        return saved

    def config_audit(self, agent_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            if agent_id:
                rows = self._connection.execute(
                    "SELECT payload_json FROM ma_config_audit WHERE agent_id=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (agent_id, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT payload_json FROM ma_config_audit ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [_load(row["payload_json"]) for row in rows]

    def save_cycle(self, cycle: DecisionCycle, *, request_payload: Mapping[str, Any]) -> None:
        with self._lock:
            self._guard("ma_cycles", self.max_cycles)
            self._connection.execute(
                "INSERT INTO ma_cycles(cycle_id, instrument_id, exchange, horizon, status, "
                "correlation_id, payload_json, created_at, started_at, finished_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cycle.cycle_id,
                    cycle.instrument_id,
                    cycle.exchange,
                    cycle.horizon,
                    cycle.status.value,
                    cycle.correlation_id,
                    _json(
                        {
                            **cycle.to_dict(include_responses=False),
                            "request": dict(request_payload),
                        }
                    ),
                    _iso(cycle.created_at),
                    _iso(cycle.started_at) if cycle.started_at else None,
                    _iso(cycle.finished_at) if cycle.finished_at else None,
                ),
            )
            self._connection.execute(
                "INSERT INTO ma_events(event_id, cycle_id, event_type, payload_json, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (
                    f"maev_{cycle.cycle_id}_created",
                    cycle.cycle_id,
                    "cycle_created",
                    _json({"status": cycle.status.value}),
                    _iso(cycle.created_at),
                ),
            )
            self._connection.commit()

    def claim_cycle(self, cycle_id: str, *, started_at: datetime) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE ma_cycles SET status=?, started_at=? WHERE cycle_id=? AND status=?",
                (
                    CycleStatus.RUNNING.value,
                    _iso(started_at),
                    cycle_id,
                    CycleStatus.PENDING.value,
                ),
            )
            if cursor.rowcount:
                self._connection.execute(
                    "INSERT INTO ma_events("
                    "event_id, cycle_id, event_type, payload_json, created_at) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (
                        f"maev_{cycle_id}_started",
                        cycle_id,
                        "cycle_started",
                        _json({"started_at": _iso(started_at)}),
                        _iso(started_at),
                    ),
                )
            self._connection.commit()
            return bool(cursor.rowcount)

    def cycle(self, cycle_id: str) -> DecisionCycle | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM ma_cycles WHERE cycle_id=?", (cycle_id,)
            ).fetchone()
        if not row:
            return None
        payload = _load(row["payload_json"])
        responses = self.runs(cycle_id)
        conflicts = self.conflicts(cycle_id)
        return _cycle_from_payload(payload, responses=responses, conflicts=conflicts)

    def cycles(
        self,
        *,
        instrument_id: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[DecisionCycle]:
        clauses: list[str] = []
        params: list[Any] = []
        if instrument_id:
            clauses.append("instrument_id=?")
            params.append(instrument_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM ma_cycles"
                + where
                + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [
            _cycle_from_payload(
                _load(row["payload_json"]),
                responses=self.runs(_load(row["payload_json"])["cycle_id"]),
                conflicts=self.conflicts(_load(row["payload_json"])["cycle_id"]),
            )
            for row in rows
        ]

    def count_cycles(self, *, status: str = "") -> int:
        with self._lock:
            if status:
                row = self._connection.execute(
                    "SELECT COUNT(*) AS n FROM ma_cycles WHERE status=?", (status,)
                ).fetchone()
            else:
                row = self._connection.execute(
                    "SELECT COUNT(*) AS n FROM ma_cycles"
                ).fetchone()
        return int(row["n"]) if row else 0

    def finish_cycle(
        self,
        cycle: DecisionCycle,
        *,
        responses: Sequence[AgentResponse],
        conflicts: Sequence[ConflictRecord],
        decision: DecisionRecord | None,
        error: str = "",
    ) -> bool:
        with self._lock:
            status_row = self._connection.execute(
                "SELECT status FROM ma_cycles WHERE cycle_id=?", (cycle.cycle_id,)
            ).fetchone()
            if not status_row or status_row["status"] != CycleStatus.RUNNING.value:
                return False
            payload = {
                **cycle.to_dict(include_responses=False),
                "status": cycle.status.value,
                "error": error,
                "degraded": cycle.degraded,
                "decision_id": decision.decision_id if decision else "",
            }
            self._connection.execute(
                "UPDATE ma_cycles SET status=?, payload_json=?, finished_at=? WHERE cycle_id=?",
                (
                    cycle.status.value,
                    _json(payload),
                    _iso(cycle.finished_at),
                    cycle.cycle_id,
                ),
            )
            for response in responses:
                self._save_run_locked(response)
            for item in conflicts:
                self._save_conflict_locked(item)
            if decision:
                self._save_decision_locked(decision)
            self._connection.execute(
                "INSERT INTO ma_events(event_id, cycle_id, event_type, payload_json, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (
                    f"maev_{cycle.cycle_id}_finished",
                    cycle.cycle_id,
                    "cycle_finished",
                    _json(
                        {
                            "status": cycle.status.value,
                            "decision": decision.action.value if decision else "",
                            "error": error,
                        }
                    ),
                    _iso(cycle.finished_at),
                ),
            )
            self._connection.commit()
        return True

    def cancel_cycle(self, cycle_id: str, *, reason: str = "cancelled by operator") -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT status, payload_json FROM ma_cycles WHERE cycle_id=?", (cycle_id,)
            ).fetchone()
            if not row:
                return False
            if row["status"] not in {CycleStatus.PENDING.value, CycleStatus.RUNNING.value}:
                return False
            payload = _load(row["payload_json"])
            payload["status"] = CycleStatus.CANCELLED.value
            payload["cancelled"] = True
            payload["error"] = reason
            payload["finished_at"] = _iso()
            self._connection.execute(
                "UPDATE ma_cycles SET status=?, payload_json=?, finished_at=? WHERE cycle_id=?",
                (CycleStatus.CANCELLED.value, _json(payload), _iso(), cycle_id),
            )
            self._connection.execute(
                "INSERT INTO ma_events(event_id, cycle_id, event_type, payload_json, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (
                    f"maev_{cycle_id}_cancelled",
                    cycle_id,
                    "cycle_cancelled",
                    _json({"reason": reason}),
                    _iso(),
                ),
            )
            self._connection.commit()
        return True

    def recover_stale_running(
        self,
        *,
        older_than_seconds: int = 3_600,
        now: datetime | None = None,
    ) -> int:
        """Recover cycles left RUNNING by an interrupted process.

        A stale running cycle is degraded with its history retained; it is never
        silently marked complete and no recommendation is invented.
        """
        reference = now or datetime.now(UTC)
        cutoff = reference - timedelta(seconds=max(0, older_than_seconds))
        with self._lock:
            rows = self._connection.execute(
                "SELECT cycle_id, payload_json FROM ma_cycles "
                "WHERE status=? AND (started_at IS NULL OR started_at<?)",
                (CycleStatus.RUNNING.value, _iso(cutoff)),
            ).fetchall()
            for row in rows:
                payload = _load(row["payload_json"])
                payload["status"] = CycleStatus.DEGRADED.value
                payload["degraded"] = True
                payload["error"] = "interrupted orchestration recovered after restart"
                payload["finished_at"] = _iso()
                self._connection.execute(
                    "UPDATE ma_cycles SET status=?, payload_json=?, finished_at=? "
                    "WHERE cycle_id=?",
                    (
                        CycleStatus.DEGRADED.value,
                        _json(payload),
                        _iso(),
                        row["cycle_id"],
                    ),
                )
                self._connection.execute(
                    "INSERT INTO ma_events(event_id, cycle_id, event_type, payload_json, "
                    "created_at) VALUES(?, ?, ?, ?, ?)",
                    (
                        f"maev_{row['cycle_id']}_recovered",
                        row["cycle_id"],
                        "cycle_recovered",
                        _json({"status": "degraded", "detail": "interrupted"}),
                        _iso(),
                    ),
                )
            self._connection.commit()
        return len(rows)

    def _save_run_locked(self, response: AgentResponse) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO ma_runs(run_id, cycle_id, agent_id, status, action, "
            "error, latency_ms, payload_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                response.message_id,
                response.cycle_id,
                response.agent_id,
                response.status.value,
                response.action.value,
                response.error[:2_000],
                response.latency_ms,
                _json(response.to_dict()),
                _iso(response.started_at),
            ),
        )

    def save_run(self, response: AgentResponse) -> None:
        with self._lock:
            self._guard("ma_runs", self.max_runs)
            self._save_run_locked(response)
            self._connection.commit()

    def runs(self, cycle_id: str) -> list[AgentResponse]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM ma_runs WHERE cycle_id=? ORDER BY created_at",
                (cycle_id,),
            ).fetchall()
        return [_response_from_payload(_load(row["payload_json"])) for row in rows]

    def save_message(self, message: AgentMessage) -> None:
        with self._lock:
            self._guard("ma_messages", self.max_messages)
            self._connection.execute(
                "INSERT INTO ma_messages(message_id, cycle_id, sender_agent_id, "
                "recipient_agent_id, message_type, payload_json, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    message.message_id,
                    message.cycle_id,
                    message.sender_agent_id,
                    message.recipient_agent_id,
                    message.message_type.value,
                    _json(message.to_dict()),
                    _iso(message.created_at),
                ),
            )
            self._connection.commit()

    def messages(self, cycle_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM ma_messages WHERE cycle_id=? "
                "ORDER BY created_at LIMIT ?",
                (cycle_id, limit),
            ).fetchall()
        return [_load(row["payload_json"]) for row in rows]

    def has_message(self, message_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM ma_messages WHERE message_id=?", (message_id,)
            ).fetchone()
        return bool(row)

    def save_evidence(self, item: EvidenceRecord) -> None:
        with self._lock:
            self._guard("ma_evidence", self.max_evidence)
            self._connection.execute(
                "INSERT OR IGNORE INTO ma_evidence(evidence_id, cycle_id, agent_id, "
                "evidence_type, freshness, validation, payload_json, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.evidence_id,
                    item.cycle_id,
                    item.agent_id,
                    item.kind.value,
                    item.freshness.value,
                    item.validation.value,
                    _json(item.to_dict()),
                    _iso(item.retrieved_at),
                ),
            )
            self._connection.commit()

    def evidence(self, cycle_id: str, *, agent_id: str = "") -> list[EvidenceRecord]:
        with self._lock:
            if agent_id:
                rows = self._connection.execute(
                    "SELECT payload_json FROM ma_evidence WHERE cycle_id=? AND agent_id=? "
                    "ORDER BY created_at",
                    (cycle_id, agent_id),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT payload_json FROM ma_evidence WHERE cycle_id=? ORDER BY created_at",
                    (cycle_id,),
                ).fetchall()
        return [EvidenceRecord.from_dict(_load(row["payload_json"])) for row in rows]

    def _save_conflict_locked(self, item: ConflictRecord) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO ma_conflicts(conflict_id, cycle_id, kind, severity, "
            "payload_json, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (
                item.conflict_id,
                item.cycle_id,
                item.kind.value,
                item.severity.value,
                _json(item.to_dict()),
                _iso(),
            ),
        )

    def save_conflicts(self, cycle_id: str, items: Sequence[ConflictRecord]) -> None:
        with self._lock:
            for item in items:
                self._save_conflict_locked(item)
            self._connection.commit()

    def conflicts(self, cycle_id: str) -> list[ConflictRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM ma_conflicts WHERE cycle_id=? ORDER BY created_at",
                (cycle_id,),
            ).fetchall()
        result: list[ConflictRecord] = []
        for row in rows:
            payload = _load(row["payload_json"])
            try:
                result.append(_conflict_from_payload(payload))
            except (KeyError, ValueError):
                continue
        return result

    def _save_decision_locked(self, item: DecisionRecord) -> None:
        self._connection.execute(
            "INSERT INTO ma_decisions(decision_id, cycle_id, instrument_id, horizon, action, "
            "payload_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(cycle_id) DO UPDATE SET payload_json=excluded.payload_json, "
            "action=excluded.action",
            (
                item.decision_id,
                item.cycle_id,
                item.instrument_id,
                item.horizon,
                item.action.value,
                _json(item.to_dict()),
                _iso(item.decided_at),
            ),
        )

    def save_decision(self, item: DecisionRecord) -> None:
        with self._lock:
            self._save_decision_locked(item)
            self._connection.commit()
            self._connection.commit()

    def decision(self, cycle_id: str) -> DecisionRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM ma_decisions WHERE cycle_id=?", (cycle_id,)
            ).fetchone()
        return _decision_from_payload(_load(row["payload_json"])) if row else None

    def decisions(
        self, *, instrument_id: str = "", limit: int = 50, offset: int = 0
    ) -> list[DecisionRecord]:
        with self._lock:
            if instrument_id:
                rows = self._connection.execute(
                    "SELECT payload_json FROM ma_decisions WHERE instrument_id=? "
                    "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (instrument_id, limit, offset),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT payload_json FROM ma_decisions "
                    "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        return [_decision_from_payload(_load(row["payload_json"])) for row in rows]

    def events(self, cycle_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT event_type, payload_json, created_at FROM ma_events "
                "WHERE cycle_id=? ORDER BY created_at",
                (cycle_id,),
            ).fetchall()
        return [
            {
                "event_type": row["event_type"],
                "payload": _load(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def performance(self, agent_id: str = "") -> list[AgentPerformance]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT agent_id, status, action, latency_ms, payload_json, created_at "
                "FROM ma_runs " + ("WHERE agent_id=? " if agent_id else "") + "ORDER BY created_at",
                (agent_id,) if agent_id else (),
            ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(row["agent_id"], []).append(row)
        result: list[AgentPerformance] = []
        for name, items in sorted(grouped.items()):
            total = len(items)
            completed = sum(1 for row in items if row["status"] == AgentRunStatus.COMPLETED.value)
            insufficient = sum(
                1 for row in items if row["status"] == AgentRunStatus.INSUFFICIENT_EVIDENCE.value
            )
            failed = sum(1 for row in items if row["status"] == AgentRunStatus.FAILED.value)
            timeouts = sum(1 for row in items if row["status"] == AgentRunStatus.TIMEOUT.value)
            invalid = sum(1 for row in items if row["status"] == AgentRunStatus.INVALID.value)
            latencies = [float(row["latency_ms"]) for row in items if row["latency_ms"] >= 0]
            actions = [
                row["action"]
                for row in items
                if row["status"]
                in {
                    AgentRunStatus.COMPLETED.value,
                    AgentRunStatus.INSUFFICIENT_EVIDENCE.value,
                }
            ]
            consistency = None
            if len(actions) >= 2:
                most_common = max(actions.count(value) for value in set(actions))
                consistency = most_common / len(actions)
            payload = _load(items[-1]["payload_json"])
            missing_rate = sum(
                1 for row in items if _load(row["payload_json"]).get("missing")
            ) / total
            error_rate = (failed + timeouts + invalid) / total
            health = AgentHealth.HEALTHY
            if total >= 5 and error_rate >= 0.5:
                health = AgentHealth.UNAVAILABLE
            elif total >= 3 and error_rate >= 0.25:
                health = AgentHealth.DEGRADED
            result.append(
                AgentPerformance(
                    agent_id=name,
                    executions=total,
                    completed=completed,
                    insufficient=insufficient,
                    failed=failed,
                    timeouts=timeouts,
                    invalid=invalid,
                    average_latency_ms=round(sum(latencies) / len(latencies), 3)
                    if latencies
                    else None,
                    timeout_rate=timeouts / total,
                    error_rate=error_rate,
                    validation_failure_rate=invalid / total,
                    missing_evidence_rate=missing_rate,
                    recommendation_consistency=consistency,
                    historical_quality=None,
                    last_execution=str(items[-1]["created_at"]),
                    provider=str(payload.get("provider") or "deterministic-local"),
                    model_version=str(payload.get("model_version") or ""),
                    health=health,
                )
            )
        return result

    def health_for(self, agent_id: str) -> AgentHealth:
        rows = self.performance(agent_id)
        return rows[0].health if rows else AgentHealth.HEALTHY


def _cycle_from_payload(
    payload: Mapping[str, Any],
    *,
    responses: Sequence[AgentResponse],
    conflicts: Sequence[ConflictRecord],
) -> DecisionCycle:
    from datetime import datetime as _datetime

    from novacontrol.novamarket.multiagent.models import parse_time

    def opt(value: object) -> _datetime | None:
        return parse_time(value) if value else None

    return DecisionCycle(
        cycle_id=str(payload["cycle_id"]),
        instrument_id=str(payload["instrument_id"]),
        exchange=str(payload["exchange"]),
        horizon=str(payload["horizon"]),
        status=CycleStatus(str(payload.get("status", "pending"))),
        as_of=parse_time(payload["as_of"]),
        requested_agents=tuple(payload.get("requested_agents") or []),
        assigned_agents=tuple(payload.get("assigned_agents") or []),
        required_agents=tuple(payload.get("required_agents") or []),
        correlation_id=str(payload.get("correlation_id") or ""),
        created_at=parse_time(payload["created_at"]),
        started_at=opt(payload.get("started_at")),
        finished_at=opt(payload.get("finished_at")),
        current_agent=str(payload.get("current_agent") or ""),
        decision_id=str(payload.get("decision_id") or ""),
        responses=tuple(responses),
        conflicts=tuple(conflicts),
        error=str(payload.get("error") or ""),
        degraded=bool(payload.get("degraded")),
        cancelled=bool(payload.get("cancelled")),
        metadata=dict(payload.get("metadata") or {}),
    )


def _response_from_payload(payload: Mapping[str, Any]) -> AgentResponse:
    from novacontrol.novamarket.multiagent.models import (
        FinancialAgentRole,
        RecommendationAction,
        parse_time,
    )

    return AgentResponse(
        message_id=str(payload["message_id"]),
        task_id=str(payload["task_id"]),
        cycle_id=str(payload["cycle_id"]),
        agent_id=str(payload["agent_id"]),
        role=FinancialAgentRole(str(payload["role"])),
        status=AgentRunStatus(str(payload["status"])),
        action=RecommendationAction(str(payload.get("action", "abstain"))),
        confidence=payload.get("confidence"),
        confidence_kind=str(payload.get("confidence_kind") or "unavailable"),
        findings=dict(payload.get("findings") or {}),
        supporting=tuple(payload.get("supporting") or []),
        contradicting=tuple(payload.get("contradicting") or []),
        uncertainties=tuple(payload.get("uncertainties") or []),
        missing=tuple(payload.get("missing") or []),
        evidence=tuple(
            EvidenceRecord.from_dict(item) for item in payload.get("evidence") or []
        ),
        source_references=tuple(payload.get("source_references") or []),
        error=str(payload.get("error") or ""),
        latency_ms=float(payload.get("latency_ms") or 0.0),
        config_version=str(payload.get("config_version") or "1"),
        model_version=str(payload.get("model_version") or ""),
        started_at=parse_time(payload["started_at"]),
        finished_at=parse_time(payload["finished_at"]),
    )


def _conflict_from_payload(payload: Mapping[str, Any]) -> ConflictRecord:
    from novacontrol.novamarket.multiagent.models import (
        ConflictKind,
        ConflictSeverity,
    )

    return ConflictRecord(
        conflict_id=str(payload["conflict_id"]),
        cycle_id=str(payload["cycle_id"]),
        kind=ConflictKind(str(payload["kind"])),
        severity=ConflictSeverity(str(payload["severity"])),
        agents=tuple(payload.get("agents") or []),
        description=str(payload.get("description") or ""),
        evidence_ids=tuple(payload.get("evidence_ids") or []),
        rule=str(payload.get("resolution_rule") or ""),
        resolved=bool(payload.get("resolved")),
        resolution=str(payload.get("resolution") or ""),
        preserved=bool(payload.get("preserved", True)),
    )


def _decision_from_payload(payload: Mapping[str, Any]) -> DecisionRecord:
    from novacontrol.novamarket.multiagent.models import (
        DecisionRecord,
        RecommendationAction,
        parse_time,
    )

    return DecisionRecord(
        decision_id=str(payload["decision_id"]),
        cycle_id=str(payload["cycle_id"]),
        instrument_id=str(payload["instrument_id"]),
        exchange=str(payload["exchange"]),
        horizon=str(payload["horizon"]),
        action=RecommendationAction(str(payload["action"])),
        supporting_agents=tuple(payload.get("supporting_agents") or []),
        contradicting_agents=tuple(payload.get("contradicting_agents") or []),
        supporting_evidence=tuple(payload.get("supporting_evidence") or []),
        contradictory_evidence=tuple(payload.get("contradictory_evidence") or []),
        evidence_strength=str(payload.get("evidence_strength") or "insufficient"),
        confidence=payload.get("confidence"),
        confidence_kind=str(payload.get("confidence_kind") or "unavailable"),
        confidence_provenance=dict(payload.get("confidence_provenance") or {}),
        uncertainty=dict(payload.get("uncertainty") or {}),
        portfolio_context=dict(payload.get("portfolio_context") or {}),
        risk_restrictions=tuple(payload.get("risk_restrictions") or []),
        unresolved_conflicts=tuple(payload.get("unresolved_conflicts") or []),
        explanation=str(payload.get("explanation") or ""),
        decided_at=parse_time(payload["decided_at"]),
        degraded=bool(payload.get("degraded")),
        risk_review_required=True,
        handoff_required=True,
        order_submitted=False,
        production_authority=False,
    )
