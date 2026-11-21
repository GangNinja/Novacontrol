"""Isolated SQLite persistence for PI-19 financial research.

Only ``pr_*`` tables are created here.  The research store cannot open or write
an NM-03 ledger, NM-07 portfolio, NM-08 risk-authority or NM-10 execution table.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import (
    MAX_EVIDENCE,
    MAX_RUNS,
    RESEARCH_SCHEMA_VERSION,
    ResearchNotFoundError,
    ResearchResourceError,
    ResearchStateError,
    ResearchStatus,
    ResearchStoreError,
)


def _json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ResearchStoreError("research record is not JSON serialisable") from exc


def _load(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ResearchStoreError("stored research record is unreadable") from exc
    return parsed if isinstance(parsed, dict) else {}


class ResearchStore:
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Any | None = None,
        max_runs: int = MAX_RUNS,
        max_evidence: int = MAX_EVIDENCE,
    ) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            try:
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ResearchStoreError(
                    f"the research store directory is not writable: {exc}"
                ) from exc
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self.max_runs = max(1, min(int(max_runs), MAX_RUNS))
        self.max_evidence = max(1, min(int(max_evidence), MAX_EVIDENCE))
        self._lock = threading.RLock()
        try:
            self._connection = sqlite3.connect(self._path, check_same_thread=False, timeout=15)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._migrate()
        except sqlite3.Error as exc:
            raise ResearchStoreError(f"research store could not open: {exc}") from exc

    @property
    def schema_version(self) -> int:
        return RESEARCH_SCHEMA_VERSION

    def _migrate(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS pr_runs(
                    run_id TEXT PRIMARY KEY,
                    instrument_id TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    parent_run_id TEXT NOT NULL DEFAULT '',
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    as_of TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS pr_runs_instrument
                    ON pr_runs(instrument_id, as_of);
                CREATE INDEX IF NOT EXISTS pr_runs_status
                    ON pr_runs(status, created_at);
                CREATE TABLE IF NOT EXISTS pr_evidence(
                    run_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    eligible INTEGER NOT NULL,
                    published_at TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, evidence_id)
                );
                CREATE INDEX IF NOT EXISTS pr_evidence_run
                    ON pr_evidence(run_id, status);
                CREATE TABLE IF NOT EXISTS pr_claims(
                    run_id TEXT NOT NULL,
                    claim_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, claim_id)
                );
                CREATE TABLE IF NOT EXISTS pr_sections(
                    run_id TEXT NOT NULL,
                    section TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, section)
                );
                CREATE TABLE IF NOT EXISTS pr_theses(
                    run_id TEXT NOT NULL,
                    thesis_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, thesis_id)
                );
                CREATE TABLE IF NOT EXISTS pr_events(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS pr_events_run
                    ON pr_events(run_id, created_at);
                """
            )
            columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(pr_runs)").fetchall()
            }
            if "idempotency_key" not in columns:
                self._connection.execute(
                    "ALTER TABLE pr_runs ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''"
                )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS pr_runs_idempotency ON pr_runs(idempotency_key)"
            )
            self._connection.commit()

    def _count(self, table: str) -> int:
        row = self._connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"]) if row else 0

    def count_runs(self) -> int:
        with self._lock:
            return self._count("pr_runs")

    def save_run(self, run: Mapping[str, Any]) -> dict[str, Any]:
        run_id = str(run.get("run_id") or "").strip()
        if not run_id:
            raise ResearchStoreError("research run needs a run_id")
        with self._lock:
            if self._count("pr_runs") >= self.max_runs:
                raise ResearchResourceError("the research run limit has been reached")
            existing = self._connection.execute(
                "SELECT run_id FROM pr_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing:
                raise ResearchStateError(f"research run {run_id!r} already exists")
            self._connection.execute(
                """
                INSERT INTO pr_runs(
                    run_id, instrument_id, horizon, status, version, parent_run_id,
                    idempotency_key, as_of, payload_json, created_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    str(run.get("instrument_id") or ""),
                    str(run.get("horizon") or ""),
                    str(run.get("status") or ResearchStatus.PENDING.value),
                    int(run.get("version") or 1),
                    str(run.get("parent_run_id") or ""),
                    str(run.get("idempotency_key") or ""),
                    str(run.get("as_of") or self._clock().isoformat()),
                    _json(run),
                    str(run.get("created_at") or self._clock().isoformat()),
                    run.get("finished_at"),
                ),
            )
            self._connection.commit()
        return self.run(run_id)

    def update_run(self, run_id: str, run: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            existing = self._connection.execute(
                "SELECT run_id, status FROM pr_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if not existing:
                raise ResearchNotFoundError(f"research run {run_id!r} was not found")
            _validate_terminal_transition(str(existing["status"]), str(run.get("status") or ""))
            self._connection.execute(
                """
                UPDATE pr_runs SET status=?, payload_json=?, finished_at=?, as_of=?,
                    version=?, parent_run_id=? WHERE run_id=?
                """,
                (
                    str(run.get("status") or ResearchStatus.FAILED.value),
                    _json(run),
                    run.get("finished_at"),
                    str(run.get("as_of") or self._clock().isoformat()),
                    int(run.get("version") or 1),
                    str(run.get("parent_run_id") or ""),
                    run_id,
                ),
            )
            self._connection.commit()
        return self.run(run_id)

    def run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM pr_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if not row:
            raise ResearchNotFoundError(f"research run {run_id!r} was not found")
        return _load(row["payload_json"])

    def runs(
        self,
        *,
        instrument_id: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if instrument_id:
            clauses.append("instrument_id=?")
            params.append(instrument_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.extend([max(1, min(int(limit), 200)), max(0, int(offset))])
        with self._lock:
            rows = self._connection.execute(
                f"SELECT payload_json FROM pr_runs{where} "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [_summary(_load(row["payload_json"])) for row in rows]

    def find_by_idempotency(self, key: str) -> dict[str, Any] | None:
        if not key:
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM pr_runs WHERE idempotency_key=? "
                "ORDER BY created_at DESC LIMIT 1",
                (key,),
            ).fetchone()
        return _load(row["payload_json"]) if row else None

    def latest_completed(self, instrument_id: str, horizon: str = "") -> dict[str, Any] | None:
        params: list[Any] = [instrument_id]
        clause = (
            "instrument_id=? AND status IN "
            "('completed','completed_with_warnings','partial_evidence','stale_data')"
        )
        if horizon:
            clause += " AND horizon=?"
            params.append(horizon)
        with self._lock:
            row = self._connection.execute(
                f"SELECT payload_json FROM pr_runs WHERE {clause} ORDER BY as_of DESC LIMIT 1",
                params,
            ).fetchone()
        return _load(row["payload_json"]) if row else None

    def save_evidence(self, run_id: str, records: Sequence[Mapping[str, Any]]) -> None:
        with self._lock:
            if self._count("pr_evidence") + len(records) > self.max_evidence:
                raise ResearchResourceError("the research evidence limit has been reached")
            for record in records:
                self._connection.execute(
                    """
                    INSERT OR REPLACE INTO pr_evidence(
                        run_id, evidence_id, instrument_id, status, eligible,
                        published_at, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        str(record.get("evidence_id") or ""),
                        str(record.get("instrument_id") or ""),
                        str(record.get("verification") or "unverified"),
                        1 if record.get("eligible", True) else 0,
                        record.get("published_at"),
                        _json(record),
                        self._clock().isoformat(),
                    ),
                )
            self._connection.commit()

    def evidence(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM pr_evidence WHERE run_id=? "
                "ORDER BY published_at, evidence_id",
                (run_id,),
            ).fetchall()
        return [_load(row["payload_json"]) for row in rows]

    def save_claims(self, run_id: str, claims: Sequence[Mapping[str, Any]]) -> None:
        with self._lock:
            for claim in claims:
                self._connection.execute(
                    """
                    INSERT OR REPLACE INTO pr_claims(
                        run_id, claim_id, status, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        str(claim.get("claim_id") or ""),
                        str(claim.get("status") or "unverified"),
                        _json(claim),
                        self._clock().isoformat(),
                    ),
                )
            self._connection.commit()

    def claims(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM pr_claims WHERE run_id=? ORDER BY claim_id",
                (run_id,),
            ).fetchall()
        return [_load(row["payload_json"]) for row in rows]

    def save_section(self, run_id: str, section: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO pr_sections(run_id, section, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, section, _json(payload), self._clock().isoformat()),
            )
            self._connection.commit()

    def save_sections(self, run_id: str, sections: Mapping[str, Any]) -> None:
        for name, payload in sections.items():
            self.save_section(
                run_id, name, payload if isinstance(payload, Mapping) else {"value": payload}
            )

    def section(self, run_id: str, section: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM pr_sections WHERE run_id=? AND section=?",
                (run_id, section),
            ).fetchone()
        if not row:
            raise ResearchNotFoundError(f"section {section!r} was not found for run {run_id!r}")
        return _load(row["payload_json"])

    def save_thesis(self, run_id: str, thesis: Mapping[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO pr_theses(
                    run_id, thesis_id, version, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    str(thesis.get("thesis_id") or ""),
                    int(thesis.get("version") or 1),
                    _json(thesis),
                    self._clock().isoformat(),
                ),
            )
            self._connection.commit()

    def thesis(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM pr_theses WHERE run_id=? ORDER BY version DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if not row:
            raise ResearchNotFoundError(f"thesis was not found for run {run_id!r}")
        return _load(row["payload_json"])

    def event(self, run_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO pr_events(run_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, event_type, _json(payload), self._clock().isoformat()),
            )
            self._connection.commit()

    def events(self, run_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT event_type, payload_json, created_at FROM pr_events "
                "WHERE run_id=? ORDER BY event_id DESC LIMIT ?",
                (run_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [
            {
                "event_type": row["event_type"],
                "payload": _load(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def recover_stale_running(
        self, *, older_than_seconds: int = 3_600, now: datetime | None = None
    ) -> int:
        cutoff = (now or self._clock()).timestamp() - older_than_seconds
        recovered = 0
        with self._lock:
            rows = self._connection.execute(
                "SELECT run_id, payload_json, created_at FROM pr_runs "
                "WHERE status IN ('pending','running')"
            ).fetchall()
            for row in rows:
                try:
                    created = datetime.fromisoformat(str(row["created_at"])).timestamp()
                except ValueError:
                    created = 0
                if created >= cutoff:
                    continue
                payload = _load(row["payload_json"])
                payload["status"] = ResearchStatus.FAILED.value
                payload["error"] = "research run was interrupted and recovered as failed"
                payload["finished_at"] = (now or self._clock()).isoformat()
                self._connection.execute(
                    "UPDATE pr_runs SET status=?, payload_json=?, finished_at=? WHERE run_id=?",
                    (payload["status"], _json(payload), payload["finished_at"], row["run_id"]),
                )
                recovered += 1
            self._connection.commit()
        return recovered

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema_version": RESEARCH_SCHEMA_VERSION,
                "runs": self._count("pr_runs"),
                "evidence": self._count("pr_evidence"),
                "claims": self._count("pr_claims"),
                "theses": self._count("pr_theses"),
                "events": self._count("pr_events"),
                "isolated_tables": True,
                "financial_mutation": False,
            }

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _summary(run: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run.get("run_id"),
        "instrument_id": run.get("instrument_id"),
        "company_name": run.get("company_name"),
        "exchange": run.get("exchange"),
        "horizon": run.get("horizon"),
        "objective": run.get("objective"),
        "as_of": run.get("as_of"),
        "status": run.get("status"),
        "version": run.get("version"),
        "parent_run_id": run.get("parent_run_id"),
        "created_at": run.get("created_at"),
        "finished_at": run.get("finished_at"),
        "thesis_stance": (run.get("thesis") or {}).get("stance")
        if isinstance(run.get("thesis"), Mapping)
        else None,
        "evidence_count": len(run.get("evidence") or []),
        "warning_count": len(run.get("warnings") or []),
        "advisory_only": True,
        "financial_mutation": False,
    }


def _validate_terminal_transition(previous: str, new: str) -> None:
    if previous in {"completed", "cancelled", "failed"} and new in {"pending", "running"}:
        raise ResearchStateError("a terminal research run cannot return to a non-terminal state")
