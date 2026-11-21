"""Persistence for PI-01 prediction records.

The store uses the same SQLite database as the rest of NovaMarket.  Forecasts,
stage records and decision evidence are append-only: SQLite triggers reject
UPDATE and DELETE, so a T-5 confirmation cannot silently overwrite the T-20
record.  Model metadata is intentionally mutable (a model may be suspended or
degraded), while its identity and version remain part of the primary key.

This is a PI-01 foundation, not an unrelated database migration.  It creates
only prediction-owned tables and records a schema version in its own metadata
table; the existing market, portfolio and execution tables are untouched.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from novacontrol.novamarket.prediction.models import (
    PI_CONTRACT_VERSION,
    PI_SCHEMA_VERSION,
    DecisionForecastEvidence,
    ForecastEvaluation,
    ForecastLifecycle,
    ForecastOutput,
    LifecycleStageRecord,
    MarketDataSnapshot,
    ModelMetadata,
    PredictionRequest,
    PredictionValidationError,
)
from novacontrol.novamarket.scheduling.models import (
    CheckpointState,
    ForecastCheckpoint,
    ForecastCycle,
    ForecastOutcome,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prediction_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prediction_requests (
    request_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    target_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prediction_lifecycles (
    lifecycle_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    target_at TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prediction_lifecycles_instrument
    ON prediction_lifecycles (instrument_id, target_at);
CREATE TABLE IF NOT EXISTS prediction_forecasts (
    forecast_id TEXT PRIMARY KEY,
    lifecycle_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    target_at TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    data_at TEXT NOT NULL,
    validity TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (lifecycle_id, stage)
);
CREATE INDEX IF NOT EXISTS idx_prediction_forecasts_lifecycle
    ON prediction_forecasts (lifecycle_id, stage);
CREATE TABLE IF NOT EXISTS prediction_stages (
    stage_id TEXT PRIMARY KEY,
    lifecycle_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    status TEXT NOT NULL,
    forecast_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (lifecycle_id, stage)
);
CREATE INDEX IF NOT EXISTS idx_prediction_stages_lifecycle
    ON prediction_stages (lifecycle_id, stage);
CREATE TABLE IF NOT EXISTS prediction_models (
    model_id TEXT NOT NULL,
    version TEXT NOT NULL,
    horizon TEXT NOT NULL,
    family TEXT NOT NULL,
    state TEXT NOT NULL,
    calibration TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (model_id, version, horizon)
);
CREATE INDEX IF NOT EXISTS idx_prediction_models_horizon
    ON prediction_models (horizon, state);
CREATE TABLE IF NOT EXISTS prediction_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    source_observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    resolution TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prediction_snapshots_instrument
    ON prediction_snapshots (instrument_id, source_observed_at);
CREATE TABLE IF NOT EXISTS prediction_ensembles (
    ensemble_id TEXT PRIMARY KEY,
    lifecycle_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    target_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prediction_ensemble_configs (
    config_id TEXT PRIMARY KEY,
    ensemble_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prediction_ensemble_configs_scope
    ON prediction_ensemble_configs (instrument_id, horizon, created_at DESC);
CREATE TABLE IF NOT EXISTS prediction_ensemble_calibrations (
    calibration_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prediction_ensemble_calibrations_scope
    ON prediction_ensemble_calibrations (instrument_id, horizon, created_at DESC);
CREATE TABLE IF NOT EXISTS prediction_ensemble_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    config_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prediction_ensemble_evaluations_scope
    ON prediction_ensemble_evaluations (instrument_id, horizon, created_at DESC);
CREATE TABLE IF NOT EXISTS prediction_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    lifecycle_id TEXT NOT NULL,
    forecast_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    target_at TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prediction_evaluations_lifecycle
    ON prediction_evaluations (lifecycle_id, evaluated_at);
CREATE TABLE IF NOT EXISTS prediction_evidence (
    evidence_id TEXT PRIMARY KEY,
    lifecycle_id TEXT NOT NULL,
    forecast_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prediction_evidence_lifecycle
    ON prediction_evidence (lifecycle_id, created_at);

-- PI-09: durable forecast-cycle scheduling.  Cycles and checkpoints are
-- mutable state machines; forecasts, stages, evaluations and outcomes remain
-- immutable evidence.
CREATE TABLE IF NOT EXISTS prediction_forecast_cycles (
    cycle_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    exchange TEXT NOT NULL,
    horizon TEXT NOT NULL,
    resolution TEXT NOT NULL,
    target_at TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prediction_cycles_scope
    ON prediction_forecast_cycles (instrument_id, horizon, target_at DESC);
CREATE INDEX IF NOT EXISTS idx_prediction_cycles_state
    ON prediction_forecast_cycles (state, target_at);
CREATE TABLE IF NOT EXISTS prediction_forecast_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    next_attempt_at TEXT,
    lease_owner TEXT NOT NULL,
    lease_expires_at TEXT,
    executed_at TEXT,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (cycle_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_prediction_checkpoints_due
    ON prediction_forecast_checkpoints (status, scheduled_at, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_prediction_checkpoints_cycle
    ON prediction_forecast_checkpoints (cycle_id, scheduled_at);
CREATE TABLE IF NOT EXISTS prediction_forecast_outcomes (
    outcome_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    forecast_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    target_at TEXT NOT NULL,
    actual_observed_at TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (cycle_id, forecast_id)
);
CREATE INDEX IF NOT EXISTS idx_prediction_outcomes_scope
    ON prediction_forecast_outcomes (cycle_id, stage, target_at DESC);
CREATE TRIGGER IF NOT EXISTS prediction_outcomes_immutable_update
BEFORE UPDATE ON prediction_forecast_outcomes
BEGIN
    SELECT RAISE(ABORT, 'prediction forecast outcomes are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prediction_outcomes_immutable_delete
BEFORE DELETE ON prediction_forecast_outcomes
BEGIN
    SELECT RAISE(ABORT, 'prediction forecast outcomes are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prediction_forecasts_immutable_update
BEFORE UPDATE ON prediction_forecasts
BEGIN
    SELECT RAISE(ABORT, 'prediction forecasts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prediction_forecasts_immutable_delete
BEFORE DELETE ON prediction_forecasts
BEGIN
    SELECT RAISE(ABORT, 'prediction forecasts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prediction_stages_immutable_update
BEFORE UPDATE ON prediction_stages
BEGIN
    SELECT RAISE(ABORT, 'prediction lifecycle stages are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prediction_stages_immutable_delete
BEFORE DELETE ON prediction_stages
BEGIN
    SELECT RAISE(ABORT, 'prediction lifecycle stages are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prediction_evaluations_immutable_update
BEFORE UPDATE ON prediction_evaluations
BEGIN
    SELECT RAISE(ABORT, 'prediction evaluations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prediction_evaluations_immutable_delete
BEFORE DELETE ON prediction_evaluations
BEGIN
    SELECT RAISE(ABORT, 'prediction evaluations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS prediction_evidence_immutable_update
BEFORE UPDATE ON prediction_evidence
BEGIN
    SELECT RAISE(ABORT, 'prediction decision evidence is immutable');
END;
CREATE TRIGGER IF NOT EXISTS prediction_evidence_immutable_delete
BEFORE DELETE ON prediction_evidence
BEGIN
    SELECT RAISE(ABORT, 'prediction decision evidence is immutable');
END;
"""


def _encode(value: Any) -> str:
    return json.dumps(asdict(value), sort_keys=True, separators=(",", ":"), default=str)


def _decode(payload: str) -> dict[str, Any]:
    loaded = json.loads(payload)
    if not isinstance(loaded, dict):
        raise PredictionValidationError("stored prediction payload is not an object")
    return loaded


class PredictionStore:
    """SQLite persistence for prediction contracts, safe for one process."""

    def __init__(self, path: str | Path | None = None, *, connection: sqlite3.Connection | None = None) -> None:
        self._lock = threading.RLock()
        self._owns_connection = connection is None
        if connection is not None:
            self._conn = connection
        else:
            if path is None:
                raise PredictionValidationError(
                    "a prediction store path or an existing SQLite connection is required"
                )
            if str(path) != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute(
                "SELECT value FROM prediction_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO prediction_meta(key, value) VALUES ('schema_version', ?)",
                    (str(PI_SCHEMA_VERSION),),
                )
            else:
                try:
                    found = int(row["value"])
                except (TypeError, ValueError) as exc:
                    raise PredictionValidationError(
                        "prediction schema version is not an integer"
                    ) from exc
                if found > PI_SCHEMA_VERSION:
                    raise PredictionValidationError(
                        f"prediction schema {found} is newer than supported {PI_SCHEMA_VERSION}"
                    )
            self._conn.execute(
                "INSERT OR REPLACE INTO prediction_meta(key, value) VALUES ('contract_version', ?)",
                (PI_CONTRACT_VERSION,),
            )
            self._conn.commit()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM prediction_meta WHERE key = 'schema_version'"
            ).fetchone()
        return int(row["value"]) if row else PI_SCHEMA_VERSION

    # -- requests ---------------------------------------------------------
    def save_request(self, request: PredictionRequest) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO prediction_requests
                    (request_id, instrument_id, horizon, target_at, created_at, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.instrument_id,
                    request.horizon.value,
                    request.target_at.isoformat(),
                    request.created_at.isoformat(),
                    _encode(request),
                ),
            )
            return cursor.rowcount == 1

    def get_request(self, request_id: str) -> PredictionRequest | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM prediction_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return PredictionRequest(**_decode(row["payload"])) if row else None

    # -- lifecycles -------------------------------------------------------
    def save_lifecycle(self, lifecycle: ForecastLifecycle) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO prediction_lifecycles
                    (lifecycle_id, request_id, instrument_id, horizon, target_at,
                     status, created_at, updated_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lifecycle.lifecycle_id,
                    lifecycle.request_id,
                    lifecycle.instrument_id,
                    lifecycle.horizon.value,
                    lifecycle.target_at.isoformat(),
                    lifecycle.status.value,
                    lifecycle.created_at.isoformat(),
                    lifecycle.updated_at.isoformat(),
                    _encode(lifecycle),
                ),
            )
            return cursor.rowcount == 1

    def update_lifecycle(self, lifecycle: ForecastLifecycle) -> None:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE prediction_lifecycles
                SET status = ?, updated_at = ?, payload = ?
                WHERE lifecycle_id = ?
                """,
                (
                    lifecycle.status.value,
                    lifecycle.updated_at.isoformat(),
                    _encode(lifecycle),
                    lifecycle.lifecycle_id,
                ),
            )
            if cursor.rowcount != 1:
                raise PredictionValidationError(
                    f"lifecycle {lifecycle.lifecycle_id!r} does not exist"
                )

    def get_lifecycle(self, lifecycle_id: str) -> ForecastLifecycle | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM prediction_lifecycles WHERE lifecycle_id = ?",
                (lifecycle_id,),
            ).fetchone()
        return ForecastLifecycle(**_decode(row["payload"])) if row else None

    def lifecycles(self, limit: int = 100) -> list[ForecastLifecycle]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM prediction_lifecycles "
                "ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [ForecastLifecycle(**_decode(row["payload"])) for row in rows]

    # -- forecasts and stages --------------------------------------------
    def save_forecast(self, forecast: ForecastOutput) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO prediction_forecasts
                    (forecast_id, lifecycle_id, stage, instrument_id, horizon,
                     target_at, generated_at, data_at, validity, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    forecast.forecast_id,
                    forecast.lifecycle_id,
                    forecast.lifecycle_stage.value,
                    forecast.instrument_id,
                    forecast.forecast_horizon.value,
                    forecast.target_at.isoformat(),
                    forecast.generated_at.isoformat(),
                    forecast.data_observed_at.isoformat(),
                    forecast.validity.value,
                    _encode(forecast),
                ),
            )
            return cursor.rowcount == 1

    def get_forecast(self, forecast_id: str) -> ForecastOutput | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM prediction_forecasts WHERE forecast_id = ?",
                (forecast_id,),
            ).fetchone()
        return ForecastOutput(**_decode(row["payload"])) if row else None

    def forecasts(self, lifecycle_id: str) -> list[ForecastOutput]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM prediction_forecasts WHERE lifecycle_id = ? "
                "ORDER BY generated_at ASC",
                (lifecycle_id,),
            ).fetchall()
        return [ForecastOutput(**_decode(row["payload"])) for row in rows]

    def list_forecasts(
        self,
        *,
        instrument_id: str | None = None,
        horizon: Any | None = None,
        model_id: str | None = None,
        limit: int = 100,
    ) -> list[ForecastOutput]:
        """Read immutable forecast contracts in the order they were generated.

        This is a read surface over the existing ``prediction_forecasts`` table,
        not a second forecast store.  ``model_id`` is filtered in the payload
        because the table deliberately indexes the contract fields rather than
        duplicating the complete model identity in a second schema.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if instrument_id:
            clauses.append("instrument_id = ?")
            params.append(instrument_id)
        if horizon is not None:
            clauses.append("horizon = ?")
            params.append(getattr(horizon, "value", str(horizon)))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT payload FROM prediction_forecasts{where} "
                "ORDER BY generated_at DESC LIMIT ?",
                (*params, max(1, min(int(limit), 500))),
            ).fetchall()
        result: list[ForecastOutput] = []
        for row in rows:
            forecast = ForecastOutput(**_decode(row["payload"]))
            if model_id and forecast.model_id != model_id:
                continue
            result.append(forecast)
        return result

    def forecast_for_stage(self, lifecycle_id: str, stage: Any) -> ForecastOutput | None:
        stage_value = getattr(stage, "value", str(stage))
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM prediction_forecasts "
                "WHERE lifecycle_id = ? AND stage = ?",
                (lifecycle_id, stage_value),
            ).fetchone()
        return ForecastOutput(**_decode(row["payload"])) if row else None

    def save_stage(self, stage: LifecycleStageRecord) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO prediction_stages
                    (stage_id, lifecycle_id, stage, scheduled_at, recorded_at,
                     status, forecast_id, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stage.stage_id,
                    stage.lifecycle_id,
                    stage.stage.value,
                    stage.scheduled_at.isoformat(),
                    stage.recorded_at.isoformat(),
                    stage.status.value,
                    stage.forecast_id,
                    _encode(stage),
                ),
            )
            return cursor.rowcount == 1

    def stages(self, lifecycle_id: str) -> list[LifecycleStageRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM prediction_stages WHERE lifecycle_id = ? "
                "ORDER BY scheduled_at ASC",
                (lifecycle_id,),
            ).fetchall()
        return [LifecycleStageRecord(**_decode(row["payload"])) for row in rows]

    def stage_for(self, lifecycle_id: str, stage: Any) -> LifecycleStageRecord | None:
        stage_value = getattr(stage, "value", str(stage))
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM prediction_stages "
                "WHERE lifecycle_id = ? AND stage = ?",
                (lifecycle_id, stage_value),
            ).fetchone()
        return LifecycleStageRecord(**_decode(row["payload"])) if row else None

    # -- models -----------------------------------------------------------
    def register_model(self, model: ModelMetadata) -> None:
        with self._lock, self._conn:
            for horizon in model.supported_horizons:
                self._conn.execute(
                    """
                    INSERT INTO prediction_models
                        (model_id, version, horizon, family, state, calibration,
                         updated_at, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(model_id, version, horizon) DO UPDATE SET
                        family = excluded.family,
                        state = excluded.state,
                        calibration = excluded.calibration,
                        updated_at = excluded.updated_at,
                        payload = excluded.payload
                    """,
                    (
                        model.model_id,
                        model.version,
                        horizon.value,
                        model.family.value,
                        model.state.value,
                        model.calibration.value,
                        (model.updated_at.isoformat() if model.updated_at else ""),
                        _encode(model),
                    ),
                )
            horizons = tuple(horizon.value for horizon in model.supported_horizons)
            placeholders = ",".join("?" for _ in horizons)
            self._conn.execute(
                f"DELETE FROM prediction_models WHERE model_id = ? AND version = ? "
                f"AND horizon NOT IN ({placeholders})",
                (model.model_id, model.version, *horizons),
            )

    def models(self, horizon: Any | None = None) -> list[ModelMetadata]:
        query = "SELECT payload FROM prediction_models"
        args: tuple[Any, ...] = ()
        if horizon is not None:
            horizon_value = getattr(horizon, "value", str(horizon))
            query += " WHERE horizon = ?"
            args = (horizon_value,)
        query += " ORDER BY model_id, version"
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        seen: set[tuple[str, str, str]] = set()
        result: list[ModelMetadata] = []
        for row in rows:
            model = ModelMetadata(**_decode(row["payload"]))
            key = (model.model_id, model.version, model.updated_at.isoformat() if model.updated_at else "")
            if key in seen:
                continue
            seen.add(key)
            result.append(model)
        return result

    def get_model(self, model_id: str, version: str, horizon: Any) -> ModelMetadata | None:
        horizon_value = getattr(horizon, "value", str(horizon))
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM prediction_models "
                "WHERE model_id = ? AND version = ? AND horizon = ?",
                (model_id, version, horizon_value),
            ).fetchone()
        return ModelMetadata(**_decode(row["payload"])) if row else None

    # -- snapshots, ensembles, evaluations, evidence ---------------------
    def save_snapshot(self, snapshot: MarketDataSnapshot) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO prediction_snapshots
                    (snapshot_id, instrument_id, source_observed_at, received_at,
                     resolution, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.instrument_id,
                    snapshot.source_observed_at.isoformat(),
                    snapshot.received_at.isoformat(),
                    snapshot.resolution.value,
                    _encode(snapshot),
                ),
            )
            return cursor.rowcount == 1

    def get_snapshot(self, snapshot_id: str) -> MarketDataSnapshot | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM prediction_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        return MarketDataSnapshot(**_decode(row["payload"])) if row else None

    def save_ensemble(self, ensemble: Any) -> bool:
        with self._lock, self._conn:
            payload = ensemble.to_dict()
            output = ensemble.output
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO prediction_ensembles
                    (ensemble_id, lifecycle_id, instrument_id, horizon, target_at,
                     created_at, status, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ensemble.ensemble_id,
                    output.lifecycle_id if output else "",
                    output.instrument_id if output else "",
                    output.forecast_horizon.value if output else "",
                    output.target_at.isoformat() if output else "",
                    output.generated_at.isoformat() if output else "",
                    ensemble.status,
                    json.dumps(payload, sort_keys=True, default=str),
                ),
            )
            return cursor.rowcount == 1

    def ensemble(self, ensemble_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM prediction_ensembles WHERE ensemble_id = ?",
                (ensemble_id,),
            ).fetchone()
        return _decode(row["payload"]) if row else None

    # -- PI-08 ensemble configuration/calibration/evaluation evidence --------
    def save_ensemble_config(self, payload: Mapping[str, Any]) -> bool:
        """Persist one immutable ensemble configuration/weight snapshot."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO prediction_ensemble_configs
                    (config_id, ensemble_id, instrument_id, horizon, version,
                     status, created_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(payload["config_id"]),
                    str(payload.get("ensemble_id") or ""),
                    str(payload["instrument_id"]),
                    str(payload["horizon"]),
                    str(payload.get("version") or ""),
                    str(payload.get("status") or "provisional"),
                    str(payload["created_at"]),
                    json.dumps(dict(payload), sort_keys=True, default=str),
                ),
            )
            return cursor.rowcount == 1

    def latest_ensemble_config(
        self,
        instrument_id: str,
        horizon: str,
        *,
        before: datetime | None = None,
    ) -> dict[str, Any] | None:
        clause = " AND created_at <= ?" if before is not None else ""
        params: tuple[Any, ...] = (
            (instrument_id, horizon, before.isoformat())
            if before is not None
            else (instrument_id, horizon)
        )
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM prediction_ensemble_configs "
                "WHERE instrument_id = ? AND horizon = ? "
                + clause
                + " ORDER BY created_at DESC LIMIT 1",
                params,
            ).fetchone()
        return _decode(row["payload"]) if row else None

    def ensemble_config(self, config_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM prediction_ensemble_configs WHERE config_id = ?",
                (config_id,),
            ).fetchone()
        return _decode(row["payload"]) if row else None

    def save_ensemble_calibration(self, payload: Mapping[str, Any]) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO prediction_ensemble_calibrations
                    (calibration_id, instrument_id, horizon, version, created_at,
                     payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(payload["calibration_id"]),
                    str(payload["instrument_id"]),
                    str(payload["horizon"]),
                    str(payload.get("method") or ""),
                    str(payload["created_at"]),
                    json.dumps(dict(payload), sort_keys=True, default=str),
                ),
            )
            return cursor.rowcount == 1

    def latest_ensemble_calibration(
        self,
        instrument_id: str,
        horizon: str,
        *,
        before: datetime | None = None,
    ) -> dict[str, Any] | None:
        clause = " AND created_at <= ?" if before is not None else ""
        params: tuple[Any, ...] = (
            (instrument_id, horizon, before.isoformat())
            if before is not None
            else (instrument_id, horizon)
        )
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM prediction_ensemble_calibrations "
                "WHERE instrument_id = ? AND horizon = ? "
                + clause
                + " ORDER BY created_at DESC LIMIT 1",
                params,
            ).fetchone()
        return _decode(row["payload"]) if row else None

    def save_ensemble_evaluation(self, evaluation: Any) -> bool:
        payload = evaluation.to_dict()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO prediction_ensemble_evaluations
                    (evaluation_id, config_id, instrument_id, horizon,
                     created_at, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(payload["evaluation_id"]),
                    str(payload.get("config_id") or ""),
                    str(payload["instrument_id"]),
                    str(payload["horizon"]),
                    str(payload["created_at"]),
                    json.dumps(payload, sort_keys=True, default=str),
                ),
            )
            return cursor.rowcount == 1

    def ensemble_evaluations(
        self, *, instrument_id: str = "", horizon: str = "", limit: int = 20
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if instrument_id:
            clauses.append("instrument_id = ?")
            params.append(instrument_id)
        if horizon:
            clauses.append("horizon = ?")
            params.append(horizon)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT payload FROM prediction_ensemble_evaluations{where} "
                "ORDER BY created_at DESC LIMIT ?",
                (*params, max(1, min(int(limit), 100))),
            ).fetchall()
        return [_decode(row["payload"]) for row in rows]

    def save_evaluation(self, evaluation: ForecastEvaluation) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO prediction_evaluations
                    (evaluation_id, lifecycle_id, forecast_id, stage, target_at,
                     evaluated_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.lifecycle_id,
                    evaluation.forecast_id,
                    evaluation.stage.value,
                    evaluation.target_at.isoformat(),
                    evaluation.evaluated_at.isoformat(),
                    _encode(evaluation),
                ),
            )
            return cursor.rowcount == 1

    def evaluations(self, lifecycle_id: str) -> list[ForecastEvaluation]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM prediction_evaluations WHERE lifecycle_id = ? "
                "ORDER BY evaluated_at ASC, forecast_id ASC",
                (lifecycle_id,),
            ).fetchall()
        return [ForecastEvaluation(**_decode(row["payload"])) for row in rows]

    def save_evidence(self, evidence: DecisionForecastEvidence) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO prediction_evidence
                    (evidence_id, lifecycle_id, forecast_id, created_at, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_id,
                    evidence.lifecycle_id,
                    evidence.forecast_id,
                    evidence.generated_at.isoformat(),
                    _encode(evidence),
                ),
            )
            return cursor.rowcount == 1

    def evidence(self, lifecycle_id: str) -> list[DecisionForecastEvidence]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM prediction_evidence WHERE lifecycle_id = ? "
                "ORDER BY created_at ASC",
                (lifecycle_id,),
            ).fetchall()
        return [DecisionForecastEvidence(**_decode(row["payload"])) for row in rows]

    # -- PI-09 forecast cycles ---------------------------------------------
    def save_forecast_cycle(self, cycle: ForecastCycle) -> bool:
        """Insert one stable cycle; an existing identity is never replaced."""

        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO prediction_forecast_cycles
                    (cycle_id, request_id, instrument_id, exchange, horizon,
                     resolution, target_at, state, created_at, updated_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle.cycle_id,
                    cycle.request_id,
                    cycle.instrument_id,
                    cycle.exchange,
                    cycle.horizon.value,
                    cycle.resolution,
                    cycle.target_at.isoformat(),
                    cycle.state.value,
                    cycle.created_at.isoformat(),
                    cycle.updated_at.isoformat(),
                    _encode(cycle),
                ),
            )
            return cursor.rowcount == 1

    def update_forecast_cycle(self, cycle: ForecastCycle) -> None:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE prediction_forecast_cycles
                SET state = ?, updated_at = ?, payload = ?
                WHERE cycle_id = ?
                """,
                (
                    cycle.state.value,
                    cycle.updated_at.isoformat(),
                    _encode(cycle),
                    cycle.cycle_id,
                ),
            )
            if cursor.rowcount != 1:
                raise PredictionValidationError(
                    f"forecast cycle {cycle.cycle_id!r} does not exist"
                )

    def get_forecast_cycle(self, cycle_id: str) -> ForecastCycle | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM prediction_forecast_cycles WHERE cycle_id = ?",
                (cycle_id,),
            ).fetchone()
        return ForecastCycle(**_decode(row["payload"])) if row else None

    def forecast_cycles(
        self,
        *,
        instrument_id: str | None = None,
        exchange: str | None = None,
        horizon: str | None = None,
        state: str | None = None,
        cycle_id: str | None = None,
        target_date: str | None = None,
        limit: int = 50,
        newest_first: bool = False,
    ) -> list[ForecastCycle]:
        """Cycles matching the filters, chronological by target by default.

        ``newest_first`` orders by creation, then target, so a cycle scheduled a
        moment ago is on the first page even when older cycles already fill the
        limit.  Live scans that walk this list (the scheduler's own status and
        checkpoint sweeps) keep the default chronological order.
        """
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("instrument_id", instrument_id),
            ("exchange", exchange),
            ("horizon", horizon),
            ("state", state),
            ("cycle_id", cycle_id),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(str(value))
        if target_date:
            clauses.append("substr(target_at, 1, 10) = ?")
            params.append(str(target_date)[:10])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        order = (
            "ORDER BY created_at DESC, target_at DESC"
            if newest_first
            else "ORDER BY target_at ASC, created_at ASC"
        )
        with self._lock:
            rows = self._conn.execute(
                f"SELECT payload FROM prediction_forecast_cycles{where} "
                f"{order} LIMIT ?",
                (*params, max(1, min(int(limit), 200))),
            ).fetchall()
        return [ForecastCycle(**_decode(row["payload"])) for row in rows]

    # -- PI-09 durable checkpoints -------------------------------------------
    def save_checkpoint(self, checkpoint: ForecastCheckpoint) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO prediction_forecast_checkpoints
                    (checkpoint_id, cycle_id, kind, scheduled_at, status,
                     attempts, next_attempt_at, lease_owner, lease_expires_at,
                     executed_at, updated_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.checkpoint_id,
                    checkpoint.cycle_id,
                    checkpoint.kind.value,
                    checkpoint.scheduled_at.isoformat(),
                    checkpoint.status.value,
                    checkpoint.attempts,
                    checkpoint.next_attempt_at.isoformat()
                    if checkpoint.next_attempt_at
                    else None,
                    checkpoint.lease_owner,
                    checkpoint.lease_expires_at.isoformat()
                    if checkpoint.lease_expires_at
                    else None,
                    checkpoint.executed_at.isoformat()
                    if checkpoint.executed_at
                    else None,
                    (checkpoint.executed_at or checkpoint.scheduled_at).isoformat(),
                    _encode(checkpoint),
                ),
            )
            return cursor.rowcount == 1

    def update_checkpoint(self, checkpoint: ForecastCheckpoint) -> None:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE prediction_forecast_checkpoints
                SET status = ?, attempts = ?, next_attempt_at = ?,
                    lease_owner = ?, lease_expires_at = ?, executed_at = ?,
                    updated_at = ?, payload = ?
                WHERE checkpoint_id = ?
                """,
                (
                    checkpoint.status.value,
                    checkpoint.attempts,
                    checkpoint.next_attempt_at.isoformat()
                    if checkpoint.next_attempt_at
                    else None,
                    checkpoint.lease_owner,
                    checkpoint.lease_expires_at.isoformat()
                    if checkpoint.lease_expires_at
                    else None,
                    checkpoint.executed_at.isoformat()
                    if checkpoint.executed_at
                    else None,
                    (checkpoint.executed_at or checkpoint.scheduled_at).isoformat(),
                    _encode(checkpoint),
                    checkpoint.checkpoint_id,
                ),
            )
            if cursor.rowcount != 1:
                raise PredictionValidationError(
                    f"forecast checkpoint {checkpoint.checkpoint_id!r} does not exist"
                )

    def get_checkpoint(self, checkpoint_id: str) -> ForecastCheckpoint | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM prediction_forecast_checkpoints "
                "WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
        return ForecastCheckpoint(**_decode(row["payload"])) if row else None

    def checkpoints(self, cycle_id: str) -> list[ForecastCheckpoint]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM prediction_forecast_checkpoints "
                "WHERE cycle_id = ? ORDER BY scheduled_at ASC",
                (cycle_id,),
            ).fetchall()
        return [ForecastCheckpoint(**_decode(row["payload"])) for row in rows]

    def due_checkpoints(
        self, now: datetime, *, limit: int = 20
    ) -> list[ForecastCheckpoint]:
        """Checkpoints whose planned or retry time has arrived."""

        stamp = now.isoformat()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT payload FROM prediction_forecast_checkpoints
                WHERE status IN ('scheduled', 'pending', 'evaluation_pending')
                  AND COALESCE(next_attempt_at, scheduled_at) <= ?
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                ORDER BY scheduled_at ASC LIMIT ?
                """,
                (stamp, stamp, max(1, min(int(limit), 100))),
            ).fetchall()
        return [ForecastCheckpoint(**_decode(row["payload"])) for row in rows]

    def claim_checkpoint(
        self, checkpoint_id: str, *, owner: str, now: datetime, lease_seconds: int
    ) -> ForecastCheckpoint | None:
        """Atomically take ownership; a second worker gets ``None``."""

        stamp = now.isoformat()
        expiry = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE prediction_forecast_checkpoints
                SET status = 'running', lease_owner = ?, lease_expires_at = ?,
                    updated_at = ?
                WHERE checkpoint_id = ?
                  AND status IN ('scheduled', 'pending', 'evaluation_pending')
                  AND COALESCE(next_attempt_at, scheduled_at) <= ?
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (owner, expiry, stamp, checkpoint_id, stamp, stamp),
            )
            if cursor.rowcount != 1:
                return None
            row = self._conn.execute(
                "SELECT payload FROM prediction_forecast_checkpoints "
                "WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
            if row is None:
                return None
            checkpoint = ForecastCheckpoint(**_decode(row["payload"]))
            claimed = replace(
                checkpoint,
                status=CheckpointState.RUNNING,
                lease_owner=owner,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            self._conn.execute(
                "UPDATE prediction_forecast_checkpoints SET payload = ?, updated_at = ? "
                "WHERE checkpoint_id = ?",
                (_encode(claimed), stamp, checkpoint_id),
            )
            return claimed

    def recover_expired_checkpoint_leases(self, now: datetime) -> int:
        """Return abandoned running checkpoints to the due queue."""

        stamp = now.isoformat()
        recovered = 0
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT payload FROM prediction_forecast_checkpoints "
                "WHERE status = 'running' AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at <= ?",
                (stamp,),
            ).fetchall()
            for row in rows:
                checkpoint = ForecastCheckpoint(**_decode(row["payload"]))
                recovered += 1
                updated = replace(
                    checkpoint,
                    status=CheckpointState.SCHEDULED,
                    lease_owner="",
                    lease_expires_at=None,
                    next_attempt_at=now,
                    reasons=(*checkpoint.reasons, "expired lease recovered"),
                )
                self._conn.execute(
                    """
                    UPDATE prediction_forecast_checkpoints
                    SET status = 'scheduled', lease_owner = '', lease_expires_at = NULL,
                        next_attempt_at = ?, updated_at = ?, payload = ?
                    WHERE checkpoint_id = ? AND status = 'running'
                    """,
                    (stamp, stamp, _encode(updated), checkpoint.checkpoint_id),
                )
            self._conn.commit()
        return recovered

    # -- PI-09 outcomes ------------------------------------------------------
    def save_outcome(self, outcome: ForecastOutcome) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO prediction_forecast_outcomes
                    (outcome_id, cycle_id, forecast_id, stage, target_at,
                     actual_observed_at, evaluated_at, status, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome.outcome_id,
                    outcome.cycle_id,
                    outcome.forecast_id,
                    outcome.stage.value,
                    outcome.target_at.isoformat(),
                    outcome.actual_observed_at.isoformat(),
                    outcome.evaluated_at.isoformat(),
                    outcome.status,
                    _encode(outcome),
                ),
            )
            return cursor.rowcount == 1

    def outcomes(self, cycle_id: str) -> list[ForecastOutcome]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM prediction_forecast_outcomes "
                "WHERE cycle_id = ? ORDER BY evaluated_at ASC",
                (cycle_id,),
            ).fetchall()
        return [ForecastOutcome(**_decode(row["payload"])) for row in rows]

    def forecast_outcomes(
        self,
        *,
        instrument_id: str | None = None,
        horizon: str | None = None,
        stage: str | None = None,
        cycle_id: str | None = None,
        limit: int = 1000,
    ) -> list[ForecastOutcome]:
        clauses: list[str] = []
        params: list[Any] = []
        if instrument_id:
            clauses.append(
                "cycle_id IN (SELECT cycle_id FROM prediction_forecast_cycles "
                "WHERE instrument_id = ?)"
            )
            params.append(instrument_id)
        if horizon:
            clauses.append(
                "cycle_id IN (SELECT cycle_id FROM prediction_forecast_cycles "
                "WHERE horizon = ?)"
            )
            params.append(horizon)
        if stage:
            clauses.append("stage = ?")
            params.append(stage)
        if cycle_id:
            clauses.append("cycle_id = ?")
            params.append(cycle_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT payload FROM prediction_forecast_outcomes{where} "
                "ORDER BY target_at DESC LIMIT ?",
                (*params, max(1, min(int(limit), 10_000))),
            ).fetchall()
        return [ForecastOutcome(**_decode(row["payload"])) for row in rows]

    def counts(self) -> dict[str, int]:
        tables = (
            "prediction_requests",
            "prediction_lifecycles",
            "prediction_forecasts",
            "prediction_stages",
            "prediction_models",
            "prediction_snapshots",
            "prediction_ensembles",
            "prediction_ensemble_configs",
            "prediction_ensemble_calibrations",
            "prediction_ensemble_evaluations",
            "prediction_evaluations",
            "prediction_evidence",
            "prediction_forecast_cycles",
            "prediction_forecast_checkpoints",
            "prediction_forecast_outcomes",
        )
        with self._lock:
            return {
                table: int(
                    self._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                )
                for table in tables
            }

    def close(self) -> None:
        with self._lock:
            if self._owns_connection:
                self._conn.close()
