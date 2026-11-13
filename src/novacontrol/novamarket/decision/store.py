"""AT-03 persistence in the existing autonomy SQLite database.

The decision store shares the AT-01/AT-02 connection so a monitoring cycle and
its decisions are one transactional view.  It only adds tables; no financial
table is read or written here.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from novacontrol.novamarket.autonomy.store import AutonomyStore
from novacontrol.novamarket.decision.models import (
    DecisionErrorCode,
    DecisionEvent,
    DecisionEvaluation,
    DecisionNotFound,
    DecisionProposal,
    DecisionProposalStatus,
    DecisionRun,
    DecisionRunStatus,
    DecisionSignal,
    DecisionStateError,
    RiskAssessment,
    digest_payload,
    utcnow,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decision_runs (
    run_id TEXT PRIMARY KEY,
    selection_key TEXT NOT NULL,
    monitoring_cycle_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_code TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decision_runs_cycle
    ON decision_runs(monitoring_cycle_id, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_decision_runs_started
    ON decision_runs(started_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_decision_runs_one_running
    ON decision_runs(status) WHERE status = 'running';
CREATE TABLE IF NOT EXISTS decision_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    evaluation_key TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    monitoring_cycle_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    portfolio_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    signal_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decision_evaluations_run
    ON decision_evaluations(run_id, created_at);
CREATE TABLE IF NOT EXISTS decision_signals (
    signal_id TEXT PRIMARY KEY,
    evaluation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    monitoring_cycle_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    portfolio_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    validation TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decision_signals_run
    ON decision_signals(run_id, created_at);
CREATE TABLE IF NOT EXISTS decision_proposals (
    proposal_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    portfolio_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    status TEXT NOT NULL,
    risk_decision_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decision_proposals_run
    ON decision_proposals(run_id, created_at);
CREATE INDEX IF NOT EXISTS ix_decision_proposals_status
    ON decision_proposals(status, created_at);
CREATE TABLE IF NOT EXISTS decision_risk_assessments (
    proposal_id TEXT PRIMARY KEY,
    risk_decision_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decision_risk_decision
    ON decision_risk_assessments(risk_decision_id);
CREATE TABLE IF NOT EXISTS decision_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    at TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    proposal_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decision_events_run
    ON decision_events(run_id, id);
CREATE TRIGGER IF NOT EXISTS decision_evaluations_immutable_update
BEFORE UPDATE ON decision_evaluations
BEGIN
    SELECT RAISE(ABORT, 'decision evaluations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS decision_evaluations_immutable_delete
BEFORE DELETE ON decision_evaluations
BEGIN
    SELECT RAISE(ABORT, 'decision evaluations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS decision_signals_immutable_update
BEFORE UPDATE ON decision_signals
BEGIN
    SELECT RAISE(ABORT, 'decision signals are immutable');
END;
CREATE TRIGGER IF NOT EXISTS decision_signals_immutable_delete
BEFORE DELETE ON decision_signals
BEGIN
    SELECT RAISE(ABORT, 'decision signals are immutable');
END;
CREATE TRIGGER IF NOT EXISTS decision_risk_immutable_update
BEFORE UPDATE ON decision_risk_assessments
BEGIN
    SELECT RAISE(ABORT, 'decision risk assessments are immutable');
END;
CREATE TRIGGER IF NOT EXISTS decision_risk_immutable_delete
BEFORE DELETE ON decision_risk_assessments
BEGIN
    SELECT RAISE(ABORT, 'decision risk assessments are immutable');
END;
"""


class DecisionStore:
    """Tables and reads for AT-03, using the autonomy store's connection."""

    def __init__(
        self,
        autonomy: AutonomyStore,
        *,
        clock: Any | None = None,
    ) -> None:
        self._autonomy = autonomy
        self._connection = autonomy.connection
        self._lock = autonomy.lock
        self._clock = clock or utcnow
        self.schema_version = SCHEMA_VERSION
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._connection.execute(
                "INSERT OR IGNORE INTO decision_meta(key, value) VALUES(?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            self._connection.commit()

    @property
    def autonomy(self) -> AutonomyStore:
        return self._autonomy

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            try:
                yield
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    # -- recovery -------------------------------------------------------------
    def recover(self, now: datetime) -> int:
        """Interrupt a run left by a dead owner; it is never completed."""

        with self.transaction():
            cursor = self._connection.execute(
                "UPDATE decision_runs SET status = ?, finished_at = ?, "
                "error_code = ? WHERE status = ?",
                (
                    DecisionRunStatus.FAILED.value,
                    now.isoformat(),
                    DecisionErrorCode.CYCLE_CANCELLED.value,
                    DecisionRunStatus.RUNNING.value,
                ),
            )
            rows = self._connection.execute(
                "SELECT run_id, payload FROM decision_runs "
                "WHERE status = ? AND error_code = ?",
                (
                    DecisionRunStatus.FAILED.value,
                    DecisionErrorCode.CYCLE_CANCELLED.value,
                ),
            ).fetchall()
            for row in rows:
                body = json.loads(row["payload"])
                body["status"] = DecisionRunStatus.FAILED.value
                body["finished_at"] = now.isoformat()
                body["error_code"] = DecisionErrorCode.CYCLE_CANCELLED.value
                body["error"] = "decision run interrupted by process restart"
                self._connection.execute(
                    "UPDATE decision_runs SET payload = ? WHERE run_id = ?",
                    (json.dumps(body, sort_keys=True), row["run_id"]),
                )
            return int(cursor.rowcount or 0)

    # -- runs -----------------------------------------------------------------
    def create_run(self, run: DecisionRun) -> None:
        body = json.dumps(run.to_dict(), sort_keys=True)
        try:
            with self.transaction():
                self._connection.execute(
                    "INSERT INTO decision_runs(run_id, selection_key, "
                    "monitoring_cycle_id, status, started_at, finished_at, "
                    "error_code, payload) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        run.run_id,
                        run.selection_key,
                        run.monitoring_cycle_id,
                        run.status.value,
                        run.started_at.isoformat(),
                        run.finished_at.isoformat() if run.finished_at else None,
                        run.error_code,
                        body,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DecisionStateError(
                DecisionErrorCode.CYCLE_ALREADY_ACTIVE.value,
                "another decision run is already active",
            ) from exc

    def update_run(self, run: DecisionRun) -> None:
        with self.transaction():
            cursor = self._connection.execute(
                "UPDATE decision_runs SET status = ?, finished_at = ?, "
                "error_code = ?, payload = ? WHERE run_id = ?",
                (
                    run.status.value,
                    run.finished_at.isoformat() if run.finished_at else None,
                    run.error_code,
                    json.dumps(run.to_dict(), sort_keys=True),
                    run.run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise DecisionNotFound(f"unknown decision run {run.run_id!r}")

    def run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM decision_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise DecisionNotFound(f"unknown decision run {run_id!r}")
        return dict(json.loads(row["payload"]))

    def runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        capped = max(1, min(int(limit), 200))
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM decision_runs ORDER BY started_at DESC, rowid DESC "
                "LIMIT ?",
                (capped,),
            ).fetchall()
        return [dict(json.loads(row["payload"])) for row in rows]

    def runs_for_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM decision_runs WHERE monitoring_cycle_id = ? "
                "ORDER BY started_at DESC, rowid DESC",
                (cycle_id,),
            ).fetchall()
        return [dict(json.loads(row["payload"])) for row in rows]

    # -- evaluations ----------------------------------------------------------
    def save_evaluation(self, evaluation: DecisionEvaluation) -> dict[str, Any]:
        body = json.dumps(evaluation.to_dict(), sort_keys=True)
        with self.transaction():
            self._connection.execute(
                "INSERT OR IGNORE INTO decision_evaluations("
                "evaluation_id, evaluation_key, run_id, monitoring_cycle_id, "
                "strategy_id, portfolio_id, instrument_id, outcome, signal_id, "
                "created_at, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evaluation.evaluation_id,
                    evaluation.evaluation_key,
                    evaluation.run_id,
                    evaluation.monitoring_cycle_id,
                    evaluation.strategy_id,
                    evaluation.portfolio_id,
                    evaluation.instrument_id,
                    evaluation.outcome.value,
                    evaluation.signal_id,
                    evaluation.created_at.isoformat(),
                    body,
                ),
            )
        existing = self.evaluation_by_key(evaluation.evaluation_key)
        return existing if existing is not None else evaluation.to_dict()

    def evaluation(self, evaluation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM decision_evaluations WHERE evaluation_id = ?",
                (evaluation_id,),
            ).fetchone()
        return dict(json.loads(row["payload"])) if row else None

    def evaluation_by_key(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM decision_evaluations WHERE evaluation_key = ?",
                (key,),
            ).fetchone()
        return dict(json.loads(row["payload"])) if row else None

    def evaluations(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM decision_evaluations WHERE run_id = ? "
                "ORDER BY created_at ASC, rowid ASC",
                (run_id,),
            ).fetchall()
        return [dict(json.loads(row["payload"])) for row in rows]

    # -- signals --------------------------------------------------------------
    def save_signal(self, signal: DecisionSignal) -> dict[str, Any]:
        with self.transaction():
            self._connection.execute(
                "INSERT OR IGNORE INTO decision_signals("
                "signal_id, evaluation_id, run_id, monitoring_cycle_id, strategy_id, "
                "portfolio_id, instrument_id, direction, validation, created_at, "
                "payload) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    signal.signal_id,
                    signal.evaluation_id,
                    signal.run_id,
                    signal.monitoring_cycle_id,
                    signal.strategy_id,
                    signal.portfolio_id,
                    signal.instrument_id,
                    signal.direction,
                    signal.validation.value,
                    signal.created_at.isoformat(),
                    json.dumps(signal.to_dict(), sort_keys=True),
                ),
            )
        return self.signal(signal.signal_id) or signal.to_dict()

    def signal(self, signal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM decision_signals WHERE signal_id = ?",
                (signal_id,),
            ).fetchone()
        return dict(json.loads(row["payload"])) if row else None

    def signals(
        self,
        *,
        run_id: str = "",
        strategy_id: str = "",
        portfolio_id: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("run_id", run_id),
            ("strategy_id", strategy_id),
            ("portfolio_id", portfolio_id),
        ):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM decision_signals"
                + where
                + " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [dict(json.loads(row["payload"])) for row in rows]

    # -- proposals ------------------------------------------------------------
    def save_proposal(self, proposal: DecisionProposal) -> dict[str, Any]:
        body = json.dumps(proposal.to_dict(), sort_keys=True)
        try:
            with self.transaction():
                self._connection.execute(
                    "INSERT INTO decision_proposals("
                    "proposal_id, signal_id, run_id, portfolio_id, instrument_id, "
                    "status, risk_decision_id, created_at, updated_at, payload) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        proposal.proposal_id,
                        proposal.signal_id,
                        proposal.run_id,
                        proposal.portfolio_id,
                        proposal.instrument_id,
                        proposal.status.value,
                        proposal.risk_decision_id,
                        proposal.created_at.isoformat(),
                        (proposal.updated_at or proposal.created_at).isoformat(),
                        body,
                    ),
                )
        except sqlite3.IntegrityError:
            existing = self.proposal_for_signal(proposal.signal_id)
            if existing is not None:
                return existing
            existing = self.proposal(proposal.proposal_id)
            if existing is not None:
                return existing
            raise
        return proposal.to_dict()

    def update_proposal(self, proposal: DecisionProposal) -> dict[str, Any]:
        body = json.dumps(proposal.to_dict(), sort_keys=True)
        with self.transaction():
            cursor = self._connection.execute(
                "UPDATE decision_proposals SET status = ?, risk_decision_id = ?, "
                "updated_at = ?, payload = ? WHERE proposal_id = ?",
                (
                    proposal.status.value,
                    proposal.risk_decision_id,
                    (proposal.updated_at or proposal.created_at).isoformat(),
                    body,
                    proposal.proposal_id,
                ),
            )
            if cursor.rowcount != 1:
                raise DecisionNotFound(f"unknown decision proposal {proposal.proposal_id!r}")
        return proposal.to_dict()

    def proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM decision_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        return dict(json.loads(row["payload"])) if row else None

    def proposal_for_signal(self, signal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM decision_proposals WHERE signal_id = ?",
                (signal_id,),
            ).fetchone()
        return dict(json.loads(row["payload"])) if row else None

    def proposals(
        self,
        *,
        run_id: str = "",
        portfolio_id: str = "",
        instrument_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("run_id", run_id),
            ("portfolio_id", portfolio_id),
            ("instrument_id", instrument_id),
            ("status", status),
        ):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM decision_proposals"
                + where
                + " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [dict(json.loads(row["payload"])) for row in rows]

    # -- risk assessments -----------------------------------------------------
    def save_risk_assessment(self, assessment: RiskAssessment) -> dict[str, Any]:
        with self.transaction():
            self._connection.execute(
                "INSERT OR IGNORE INTO decision_risk_assessments("
                "proposal_id, risk_decision_id, outcome, created_at, payload) "
                "VALUES (?,?,?,?,?)",
                (
                    assessment.proposal_id,
                    assessment.risk_decision_id,
                    assessment.outcome,
                    assessment.assessed_at.isoformat(),
                    json.dumps(assessment.to_dict(), sort_keys=True),
                ),
            )
        return self.risk_assessment(assessment.proposal_id) or assessment.to_dict()

    def risk_assessment(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM decision_risk_assessments WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        return dict(json.loads(row["payload"])) if row else None

    def risk_assessment_by_decision(
        self, decision_id: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM decision_risk_assessments "
                "WHERE risk_decision_id = ? ORDER BY created_at DESC LIMIT 1",
                (decision_id,),
            ).fetchone()
        return dict(json.loads(row["payload"])) if row else None

    # -- events ---------------------------------------------------------------
    def append_event(self, event: DecisionEvent) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT OR IGNORE INTO decision_events("
                "event_id, at, run_id, proposal_id, kind, message, payload) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    event.event_id,
                    event.at.isoformat(),
                    event.run_id,
                    event.proposal_id,
                    event.kind,
                    event.message,
                    json.dumps(event.payload, sort_keys=True, default=str),
                ),
            )

    def events(
        self,
        *,
        run_id: str = "",
        proposal_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("run_id", run_id), ("proposal_id", proposal_id)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._connection.execute(
                "SELECT event_id, at, run_id, proposal_id, kind, message, payload "
                "FROM decision_events"
                + where
                + " ORDER BY id ASC LIMIT ?",
                tuple(params),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            body = dict(row)
            body["payload"] = json.loads(body["payload"])
            result.append(body)
        return result

    # -- metrics --------------------------------------------------------------
    def metrics(self) -> dict[str, Any]:
        with self._lock:
            runs = self._connection.execute(
                "SELECT status, COUNT(*) AS n FROM decision_runs GROUP BY status"
            ).fetchall()
            proposals = self._connection.execute(
                "SELECT status, COUNT(*) AS n FROM decision_proposals GROUP BY status"
            ).fetchall()
            evaluations = self._connection.execute(
                "SELECT COUNT(*) AS n FROM decision_evaluations"
            ).fetchone()["n"]
            signals = self._connection.execute(
                "SELECT COUNT(*) AS n FROM decision_signals"
            ).fetchone()["n"]
            assessments = self._connection.execute(
                "SELECT COUNT(*) AS n FROM decision_risk_assessments"
            ).fetchone()["n"]
        return {
            "runs": sum(row["n"] for row in runs),
            "runs_by_status": {row["status"]: row["n"] for row in runs},
            "evaluations": evaluations,
            "signals": signals,
            "proposals": sum(row["n"] for row in proposals),
            "proposals_by_status": {row["status"]: row["n"] for row in proposals},
            "risk_assessments": assessments,
        }

    def status(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "metrics": self.metrics(),
            "last_run": self.runs(limit=1)[0] if self.runs(limit=1) else None,
        }

    def close(self) -> None:
        """The autonomy runtime owns the shared connection; nothing to close."""


def _not_found(kind: str, value: str) -> DecisionNotFound:
    return DecisionNotFound(f"unknown decision {kind} {value!r}")


__all__ = ["SCHEMA_VERSION", "DecisionStore"]
