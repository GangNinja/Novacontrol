"""SQLite persistence for NM-06 forecasts, models and evaluation history.

Forecast rows are immutable: database triggers abort ``UPDATE`` and ``DELETE``,
so a later outcome can never rewrite what was predicted. The lifecycle state
lives beside the forecast in ``forecast_outcomes`` (pending/evaluated/unresolved),
which keeps the prediction itself untouched.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from novacontrol.novamarket.forecasting.models import (
    ForecastOutcome,
    ForecastRecord,
    ModelRecord,
    ModelState,
    ensure_model_transition,
    utcnow,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forecast_records (
    forecast_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data_cutoff TEXT NOT NULL,
    target_at TEXT NOT NULL,
    horizon TEXT NOT NULL,
    horizon_sessions INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    feature_set_version TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_forecast_records_instrument
    ON forecast_records(instrument_id, horizon, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_forecast_records_target
    ON forecast_records(target_at);
CREATE TRIGGER IF NOT EXISTS forecast_records_no_update
BEFORE UPDATE ON forecast_records
BEGIN SELECT RAISE(ABORT, 'forecasts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS forecast_records_no_delete
BEFORE DELETE ON forecast_records
BEGIN SELECT RAISE(ABORT, 'forecasts are immutable'); END;

CREATE TABLE IF NOT EXISTS forecast_outcomes (
    forecast_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_forecast_outcomes_status
    ON forecast_outcomes(status, evaluated_at);

CREATE TABLE IF NOT EXISTS forecast_models (
    model_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    version TEXT NOT NULL,
    state TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    trained_at TEXT,
    updated_at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    metrics TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (model_id, instrument_id, horizon)
);

CREATE TABLE IF NOT EXISTS forecast_selection (
    instrument_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    model_id TEXT NOT NULL,
    selected_at TEXT NOT NULL,
    metrics TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (instrument_id, horizon)
);

CREATE TABLE IF NOT EXISTS forecast_training_runs (
    run_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    model_id TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    samples INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_forecast_training
    ON forecast_training_runs(instrument_id, horizon, finished_at DESC);

CREATE TABLE IF NOT EXISTS forecast_performance (
    snapshot_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    samples INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    metrics TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forecast_calibration (
    calibration_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forecast_reliability (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
"""


class ForecastStore:
    """Append-only forecasts, mutable model state, and evaluation history."""

    def __init__(
        self, path: str = ":memory:", *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.path = str(path)
        self.schema_version = SCHEMA_VERSION
        self._clock = clock or utcnow
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._connection.execute(
                "INSERT OR IGNORE INTO forecast_meta(key, value) VALUES(?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._connection.close()
            except sqlite3.Error:  # pragma: no cover - closing twice
                pass

    def __enter__(self) -> "ForecastStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _commit(self) -> None:
        self._connection.commit()

    # -- forecasts ------------------------------------------------------------

    def save_forecast(self, record: ForecastRecord) -> None:
        payload = json.dumps(record.to_dict(), sort_keys=True)
        with self._lock:
            self._connection.execute(
                "INSERT INTO forecast_records(forecast_id, instrument_id, created_at, "
                "data_cutoff, target_at, horizon, horizon_sessions, model_id, "
                "model_version, feature_set_version, score, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.forecast_id,
                    record.instrument_id,
                    record.created_at.isoformat(),
                    record.data_cutoff.isoformat(),
                    record.target_at.isoformat(),
                    record.horizon.value,
                    record.horizon_sessions,
                    record.model_id,
                    record.model_version,
                    record.feature_set_version,
                    record.predicted_return,
                    payload,
                ),
            )
            self._commit()

    @staticmethod
    def _forecast_row(row: sqlite3.Row) -> dict[str, Any]:
        body: dict[str, Any] = json.loads(row["payload"])
        body["status"] = row["status"] if "status" in row.keys() else "pending"
        if "outcome" in row.keys() and row["outcome"]:
            body["outcome"] = json.loads(row["outcome"])
        return body

    def forecast(self, forecast_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT r.*, COALESCE(o.status, 'pending') AS status, "
                "o.payload AS outcome FROM forecast_records r "
                "LEFT JOIN forecast_outcomes o ON o.forecast_id=r.forecast_id "
                "WHERE r.forecast_id=?",
                (forecast_id,),
            ).fetchone()
        return self._forecast_row(row) if row is not None else None

    def forecasts(
        self,
        *,
        instrument_id: str | None = None,
        horizon: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if instrument_id:
            clauses.append("r.instrument_id=?")
            params.append(instrument_id)
        if horizon:
            clauses.append("r.horizon=?")
            params.append(horizon)
        if status:
            clauses.append("COALESCE(o.status, 'pending')=?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                "SELECT r.*, COALESCE(o.status, 'pending') AS status, "
                "o.payload AS outcome FROM forecast_records r "
                "LEFT JOIN forecast_outcomes o ON o.forecast_id=r.forecast_id"
                f"{where} ORDER BY r.created_at DESC LIMIT ?",
                (*params, max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._forecast_row(row) for row in rows]

    def pending_due(self, at: datetime, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT r.*, 'pending' AS status FROM forecast_records r "
                "LEFT JOIN forecast_outcomes o ON o.forecast_id=r.forecast_id "
                "WHERE o.forecast_id IS NULL AND r.target_at<=? "
                "ORDER BY r.target_at LIMIT ?",
                (at.isoformat(), max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._forecast_row(row) for row in rows]

    # -- outcomes -------------------------------------------------------------

    def record_outcome(self, outcome: ForecastOutcome) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO forecast_outcomes("
                "forecast_id, status, evaluated_at, payload) VALUES (?,?,?,?)",
                (
                    outcome.forecast_id,
                    outcome.status.value,
                    outcome.evaluated_at.isoformat(),
                    json.dumps(outcome.to_dict(), sort_keys=True),
                ),
            )
            self._commit()
            return cursor.rowcount > 0

    def outcome(self, forecast_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM forecast_outcomes WHERE forecast_id=?",
                (forecast_id,),
            ).fetchone()
        if row is None:
            return None
        body: dict[str, Any] = json.loads(row["payload"])
        body["status"] = row["status"]
        return body

    def outcomes(
        self,
        *,
        model_id: str | None = None,
        instrument_id: str | None = None,
        horizon: str | None = None,
        status: str = "evaluated",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses = ["o.status=?"]
        params: list[Any] = [status]
        if model_id:
            clauses.append("r.model_id=?")
            params.append(model_id)
        if instrument_id:
            clauses.append("r.instrument_id=?")
            params.append(instrument_id)
        if horizon:
            clauses.append("r.horizon=?")
            params.append(horizon)
        where = " AND ".join(clauses)
        with self._lock:
            rows = self._connection.execute(
                "SELECT o.payload AS outcome, r.payload AS forecast FROM "
                "forecast_outcomes o JOIN forecast_records r "
                f"ON r.forecast_id=o.forecast_id WHERE {where} "
                "ORDER BY o.evaluated_at DESC LIMIT ?",
                (*params, max(1, min(int(limit), 1000))),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            body = json.loads(row["outcome"])
            forecast = json.loads(row["forecast"])
            body["model_id"] = forecast.get("model_id")
            body["horizon"] = forecast.get("horizon")
            result.append(body)
        return result

    # -- models ---------------------------------------------------------------

    def upsert_model(self, record: ModelRecord) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO forecast_models(model_id, instrument_id, horizon, version, "
                "state, kind, description, trained_at, updated_at, reason, metrics) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(model_id, instrument_id, horizon) DO UPDATE SET "
                "version=excluded.version, state=excluded.state, kind=excluded.kind, "
                "description=excluded.description, trained_at=excluded.trained_at, "
                "updated_at=excluded.updated_at, reason=excluded.reason, "
                "metrics=excluded.metrics",
                (
                    record.model_id,
                    record.instrument_id,
                    record.horizon,
                    record.version,
                    record.state.value,
                    record.kind,
                    record.description,
                    record.trained_at.isoformat() if record.trained_at else None,
                    record.updated_at.isoformat(),
                    record.reason,
                    json.dumps(record.metrics, sort_keys=True),
                ),
            )
            self._commit()

    @staticmethod
    def _model_row(row: sqlite3.Row) -> ModelRecord:
        return ModelRecord(
            model_id=row["model_id"],
            version=row["version"],
            state=ModelState(row["state"]),
            kind=row["kind"],
            description=row["description"],
            trained_at=(
                datetime.fromisoformat(row["trained_at"]) if row["trained_at"] else None
            ),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            instrument_id=row["instrument_id"],
            horizon=row["horizon"],
            metrics=json.loads(row["metrics"] or "{}"),
            reason=row["reason"],
        )

    def model(
        self, model_id: str, instrument_id: str, horizon: str
    ) -> ModelRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM forecast_models WHERE model_id=? AND "
                "instrument_id=? AND horizon=?",
                (model_id, instrument_id, horizon),
            ).fetchone()
        return self._model_row(row) if row is not None else None

    def models(
        self, *, instrument_id: str | None = None, horizon: str | None = None
    ) -> list[ModelRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if instrument_id:
            clauses.append("instrument_id=?")
            params.append(instrument_id)
        if horizon:
            clauses.append("horizon=?")
            params.append(horizon)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM forecast_models{where} ORDER BY model_id, horizon",
                params,
            ).fetchall()
        return [self._model_row(row) for row in rows]

    def set_model_state(
        self,
        model_id: str,
        instrument_id: str,
        horizon: str,
        state: ModelState,
        reason: str,
        at: datetime,
    ) -> ModelRecord | None:
        current = self.model(model_id, instrument_id, horizon)
        if current is None:
            return None
        # The lifecycle table is the only authority on a move; an illegal one
        # raises before anything is written.
        ensure_model_transition(current.state, state)
        with self._lock:
            self._connection.execute(
                "UPDATE forecast_models SET state=?, updated_at=?, reason=? "
                "WHERE model_id=? AND instrument_id=? AND horizon=?",
                (state.value, at.isoformat(), reason, model_id, instrument_id, horizon),
            )
            self._connection.execute(
                "INSERT INTO forecast_reliability(model_id, instrument_id, horizon, "
                "from_state, to_state, reason, at) VALUES (?,?,?,?,?,?,?)",
                (
                    model_id,
                    instrument_id,
                    horizon,
                    current.state.value,
                    state.value,
                    reason,
                    at.isoformat(),
                ),
            )
            self._commit()
        return self.model(model_id, instrument_id, horizon)

    # -- selection, training, performance, calibration ------------------------

    def save_selection(
        self,
        instrument_id: str,
        horizon: str,
        model_id: str,
        metrics: dict[str, Any],
        at: datetime,
    ) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO forecast_selection(instrument_id, horizon, "
                "model_id, selected_at, metrics) VALUES (?,?,?,?,?)",
                (
                    instrument_id,
                    horizon,
                    model_id,
                    at.isoformat(),
                    json.dumps(metrics, sort_keys=True),
                ),
            )
            self._commit()

    def selection(self, instrument_id: str, horizon: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT model_id FROM forecast_selection WHERE instrument_id=? "
                "AND horizon=?",
                (instrument_id, horizon),
            ).fetchone()
        return str(row["model_id"]) if row is not None else None

    def selection_metrics(self, instrument_id: str, horizon: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT metrics FROM forecast_selection WHERE instrument_id=? "
                "AND horizon=?",
                (instrument_id, horizon),
            ).fetchone()
        return json.loads(row["metrics"]) if row is not None else {}

    def save_training_run(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO forecast_training_runs(run_id, instrument_id, horizon, "
                "model_id, version, started_at, finished_at, samples, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    payload["run_id"],
                    payload["instrument_id"],
                    payload["horizon"],
                    payload.get("model_id", ""),
                    payload.get("version", ""),
                    payload["started_at"],
                    payload["finished_at"],
                    int(payload.get("samples", 0)),
                    json.dumps(payload, sort_keys=True),
                ),
            )
            self._commit()

    def training_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM forecast_training_runs "
                "ORDER BY finished_at DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_performance(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO forecast_performance(snapshot_id, model_id, instrument_id, "
                "horizon, samples, created_at, metrics) VALUES (?,?,?,?,?,?,?)",
                (
                    payload["snapshot_id"],
                    payload["model_id"],
                    payload["instrument_id"],
                    payload["horizon"],
                    int(payload.get("samples", 0)),
                    payload["created_at"],
                    json.dumps(payload.get("metrics", {}), sort_keys=True),
                ),
            )
            self._commit()

    def performance(
        self,
        *,
        model_id: str | None = None,
        instrument_id: str | None = None,
        horizon: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("model_id", model_id),
            ("instrument_id", instrument_id),
            ("horizon", horizon),
        ):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM forecast_performance{where} "
                "ORDER BY created_at DESC LIMIT ?",
                (*params, max(1, min(int(limit), 200))),
            ).fetchall()
        result = []
        for row in rows:
            body = dict(row)
            body["metrics"] = json.loads(body["metrics"])
            result.append(body)
        return result

    def save_calibration(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO forecast_calibration(calibration_id, model_id, horizon, "
                "status, created_at, payload) VALUES (?,?,?,?,?,?)",
                (
                    payload["calibration_id"],
                    payload["model_id"],
                    payload["horizon"],
                    payload["status"],
                    payload["created_at"],
                    json.dumps(payload, sort_keys=True),
                ),
            )
            self._commit()

    def calibration(self, model_id: str, horizon: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM forecast_calibration WHERE model_id=? AND "
                "horizon=? ORDER BY created_at DESC LIMIT 1",
                (model_id, horizon),
            ).fetchone()
        return json.loads(row["payload"]) if row is not None else None

    def reliability(self, *, model_id: str | None = None) -> list[dict[str, Any]]:
        clause = " WHERE model_id=?" if model_id else ""
        params = (model_id,) if model_id else ()
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM forecast_reliability{clause} ORDER BY id DESC LIMIT 100",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    # -- observability --------------------------------------------------------

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            forecasts = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM forecast_records"
                ).fetchone()[0]
            )
            evaluated = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM forecast_outcomes WHERE status='evaluated'"
                ).fetchone()[0]
            )
            unresolved = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM forecast_outcomes WHERE status='unresolved'"
                ).fetchone()[0]
            )
            models = {
                row["state"]: int(row["count"])
                for row in self._connection.execute(
                    "SELECT state, COUNT(*) AS count FROM forecast_models GROUP BY state"
                ).fetchall()
            }
            training_runs = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM forecast_training_runs"
                ).fetchone()[0]
            )
            calibration_reports = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM forecast_calibration"
                ).fetchone()[0]
            )
        return {
            "forecasts": forecasts,
            "evaluated": evaluated,
            "pending": max(0, forecasts - evaluated - unresolved),
            "unresolved": unresolved,
            "models": models,
            "training_runs": training_runs,
            "calibration_reports": calibration_reports,
        }


__all__ = ["SCHEMA_VERSION", "ForecastStore"]
