"""SQLite persistence for PI-04 feature sets, regimes and preprocessing."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from novacontrol.novamarket.features.models import (
    FeatureSet,
    PreprocessingFit,
    RegimeResult,
    SelectionDecision,
    SelectionReport,
)

_SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS feature_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO feature_schema_meta(key, value) VALUES ('schema_version', '1');
CREATE TABLE IF NOT EXISTS feature_definitions (
    version TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feature_sets (
    feature_set_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    exchange TEXT NOT NULL,
    as_of TEXT NOT NULL,
    resolution TEXT NOT NULL,
    profile TEXT NOT NULL,
    version TEXT NOT NULL,
    digest TEXT NOT NULL,
    quality TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_feature_sets_instrument_time
    ON feature_sets(instrument_id, as_of, resolution);
CREATE TABLE IF NOT EXISTS regimes (
    regime_id TEXT PRIMARY KEY,
    feature_set_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    resolution TEXT NOT NULL,
    primary_regime TEXT NOT NULL,
    method_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(feature_set_id) REFERENCES feature_sets(feature_set_id)
);
CREATE INDEX IF NOT EXISTS ix_regimes_instrument_time
    ON regimes(instrument_id, as_of, resolution);
CREATE TABLE IF NOT EXISTS preprocessing_fits (
    preprocessing_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    method TEXT NOT NULL,
    training_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS selection_reports (
    report_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feature_generations (
    generation_id TEXT PRIMARY KEY,
    feature_set_id TEXT,
    instrument_id TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_feature_generations_instrument
    ON feature_generations(instrument_id, created_at);
CREATE TRIGGER IF NOT EXISTS feature_definitions_immutable_update
BEFORE UPDATE ON feature_definitions
BEGIN SELECT RAISE(ABORT, 'feature definitions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS feature_definitions_immutable_delete
BEFORE DELETE ON feature_definitions
BEGIN SELECT RAISE(ABORT, 'feature definitions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS feature_sets_immutable_update
BEFORE UPDATE ON feature_sets
BEGIN SELECT RAISE(ABORT, 'feature sets are immutable'); END;
CREATE TRIGGER IF NOT EXISTS feature_sets_immutable_delete
BEFORE DELETE ON feature_sets
BEGIN SELECT RAISE(ABORT, 'feature sets are immutable'); END;
CREATE TRIGGER IF NOT EXISTS regimes_immutable_update
BEFORE UPDATE ON regimes
BEGIN SELECT RAISE(ABORT, 'regimes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS regimes_immutable_delete
BEFORE DELETE ON regimes
BEGIN SELECT RAISE(ABORT, 'regimes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS preprocessing_fits_immutable_update
BEFORE UPDATE ON preprocessing_fits
BEGIN SELECT RAISE(ABORT, 'preprocessing fits are immutable'); END;
CREATE TRIGGER IF NOT EXISTS preprocessing_fits_immutable_delete
BEFORE DELETE ON preprocessing_fits
BEGIN SELECT RAISE(ABORT, 'preprocessing fits are immutable'); END;
CREATE TRIGGER IF NOT EXISTS selection_reports_immutable_update
BEFORE UPDATE ON selection_reports
BEGIN SELECT RAISE(ABORT, 'selection reports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS selection_reports_immutable_delete
BEFORE DELETE ON selection_reports
BEGIN SELECT RAISE(ABORT, 'selection reports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS feature_generations_immutable_update
BEFORE UPDATE ON feature_generations
BEGIN SELECT RAISE(ABORT, 'feature generations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS feature_generations_immutable_delete
BEFORE DELETE ON feature_generations
BEGIN SELECT RAISE(ABORT, 'feature generations are immutable'); END;
"""


class FeatureStoreError(RuntimeError):
    """A persistence operation could not be completed."""


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).isoformat()


class FeatureStore:
    """A bounded, append-only store in the existing NovaMarket SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
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
            self._connection.commit()
        except sqlite3.Error as exc:
            raise FeatureStoreError(f"could not open feature store: {exc}") from exc

    @property
    def path(self) -> str:
        return self._path

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM feature_schema_meta WHERE key='schema_version'"
            ).fetchone()
        return int(row["value"]) if row is not None else _SCHEMA_VERSION

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def save_definitions(self, payload: Mapping[str, Any]) -> bool:
        version = str(payload.get("feature_set_version", ""))
        digest = str(payload.get("definition_digest", ""))
        if not version or not digest:
            raise FeatureStoreError("feature definitions need version and digest")
        with self._lock:
            try:
                existing = self._connection.execute(
                    "SELECT digest FROM feature_definitions WHERE version=?", (version,)
                ).fetchone()
                if existing is not None:
                    if str(existing["digest"]) != digest:
                        raise FeatureStoreError(
                            f"feature definition version {version!r} is immutable"
                        )
                    return False
                self._connection.execute(
                    "INSERT INTO feature_definitions(version, digest, payload_json, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (version, digest, json.dumps(payload, sort_keys=True), _iso()),
                )
                self._connection.commit()
            except sqlite3.Error as exc:
                self._connection.rollback()
                raise FeatureStoreError(f"could not save feature definitions: {exc}") from exc
        return True

    def save(
        self,
        feature_set: FeatureSet,
        regime: RegimeResult,
        *,
        generation_id: str,
        definitions: Mapping[str, Any] | None = None,
    ) -> tuple[bool, bool]:
        """Persist once; return ``(persisted, duplicate)``."""
        if not generation_id:
            raise FeatureStoreError("a feature generation needs an identifier")
        with self._lock:
            try:
                if definitions is not None:
                    self.save_definitions(definitions)
                existing = self._connection.execute(
                    "SELECT digest FROM feature_sets WHERE feature_set_id=?",
                    (feature_set.feature_set_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing["digest"]) != feature_set.digest_value:
                        raise FeatureStoreError(
                            "a feature-set id cannot be reused with different content"
                        )
                    return False, True
                created = _iso()
                self._connection.execute(
                    "INSERT INTO feature_sets(feature_set_id, instrument_id, exchange, as_of, "
                    "resolution, profile, version, digest, quality, payload_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        feature_set.feature_set_id,
                        feature_set.instrument_id,
                        feature_set.exchange,
                        feature_set.as_of.isoformat(),
                        feature_set.resolution,
                        str(feature_set.profile),
                        feature_set.version,
                        feature_set.digest_value,
                        str(feature_set.quality),
                        json.dumps(feature_set.to_dict(), sort_keys=True),
                        created,
                    ),
                )
                self._connection.execute(
                    "INSERT INTO regimes(regime_id, feature_set_id, instrument_id, as_of, "
                    "resolution, primary_regime, method_version, payload_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        regime.regime_id,
                        regime.feature_set_id,
                        regime.instrument_id,
                        regime.as_of.isoformat(),
                        regime.resolution,
                        str(regime.primary),
                        regime.method,
                        json.dumps(regime.to_dict(), sort_keys=True),
                        created,
                    ),
                )
                self._connection.execute(
                    "INSERT INTO feature_generations(generation_id, feature_set_id, "
                    "instrument_id, status, error, created_at) VALUES (?, ?, ?, 'completed', '', ?)",
                    (generation_id, feature_set.feature_set_id, feature_set.instrument_id, created),
                )
                self._connection.commit()
            except sqlite3.Error as exc:
                self._connection.rollback()
                raise FeatureStoreError(f"could not save feature generation: {exc}") from exc
        return True, False

    def save_failure(
        self,
        generation_id: str,
        instrument_id: str,
        error: str,
        *,
        feature_set_id: str = "",
    ) -> None:
        with self._lock:
            try:
                self._connection.execute(
                    "INSERT INTO feature_generations(generation_id, feature_set_id, "
                    "instrument_id, status, error, created_at) "
                    "VALUES (?, ?, ?, 'failed', ?, ?)",
                    (generation_id, feature_set_id or None, instrument_id, error, _iso()),
                )
                self._connection.commit()
            except sqlite3.Error as exc:
                self._connection.rollback()
                raise FeatureStoreError(f"could not record feature failure: {exc}") from exc

    def get_feature_set(self, feature_set_id: str) -> FeatureSet | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM feature_sets WHERE feature_set_id=?",
                (str(feature_set_id),),
            ).fetchone()
        return FeatureSet.from_dict(json.loads(row["payload_json"])) if row else None

    def latest_feature_set(
        self, instrument_id: str, *, resolution: str | None = None
    ) -> FeatureSet | None:
        clauses = ["instrument_id=?"]
        params: list[Any] = [str(instrument_id)]
        if resolution:
            clauses.append("resolution=?")
            params.append(str(resolution))
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM feature_sets WHERE " + " AND ".join(clauses)
                + " ORDER BY as_of DESC, created_at DESC LIMIT 1",
                params,
            ).fetchone()
        return FeatureSet.from_dict(json.loads(row["payload_json"])) if row else None

    def feature_sets(
        self,
        *,
        instrument_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 5_000))
        offset = max(0, int(offset))
        clauses = ["1=1"]
        params: list[Any] = []
        if instrument_id:
            clauses.append("instrument_id=?")
            params.append(str(instrument_id))
        where = " AND ".join(clauses)
        with self._lock:
            total = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM feature_sets WHERE {where}", params
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                f"SELECT feature_set_id, instrument_id, exchange, as_of, resolution, profile, "
                f"version, quality, digest, created_at FROM feature_sets WHERE {where} "
                f"ORDER BY as_of DESC, created_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        items = [dict(row) for row in rows]
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
        }

    def get_regime(self, regime_id: str) -> RegimeResult | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM regimes WHERE regime_id=?", (str(regime_id),)
            ).fetchone()
        return RegimeResult.from_dict(json.loads(row["payload_json"])) if row else None

    def latest_regime(
        self,
        instrument_id: str,
        *,
        resolution: str | None = None,
        before: datetime | None = None,
    ) -> RegimeResult | None:
        clauses = ["instrument_id=?"]
        params: list[Any] = [str(instrument_id)]
        if resolution:
            clauses.append("resolution=?")
            params.append(str(resolution))
        if before is not None:
            clauses.append("as_of < ?")
            params.append(_iso(before))
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM regimes WHERE " + " AND ".join(clauses)
                + " ORDER BY as_of DESC, created_at DESC LIMIT 1",
                params,
            ).fetchone()
        return RegimeResult.from_dict(json.loads(row["payload_json"])) if row else None

    def regimes(
        self,
        *,
        instrument_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 5_000))
        offset = max(0, int(offset))
        clauses = ["1=1"]
        params: list[Any] = []
        if instrument_id:
            clauses.append("instrument_id=?")
            params.append(str(instrument_id))
        where = " AND ".join(clauses)
        with self._lock:
            total = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM regimes WHERE {where}", params
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                f"SELECT regime_id, feature_set_id, instrument_id, as_of, resolution, "
                f"primary_regime, method_version, created_at FROM regimes WHERE {where} "
                f"ORDER BY as_of DESC, created_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        items = [dict(row) for row in rows]
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
        }

    def save_preprocessing(self, fit: PreprocessingFit) -> bool:
        with self._lock:
            try:
                existing = self._connection.execute(
                    "SELECT training_digest FROM preprocessing_fits WHERE preprocessing_id=?",
                    (fit.preprocessing_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing["training_digest"]) != fit.training_digest:
                        raise FeatureStoreError(
                            "a preprocessing id cannot be reused with different training data"
                        )
                    return False
                self._connection.execute(
                    "INSERT INTO preprocessing_fits(preprocessing_id, version, method, "
                    "training_digest, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        fit.preprocessing_id,
                        fit.version,
                        str(fit.method),
                        fit.training_digest,
                        json.dumps(fit.to_dict(), sort_keys=True),
                        _iso(),
                    ),
                )
                self._connection.commit()
            except sqlite3.Error as exc:
                self._connection.rollback()
                raise FeatureStoreError(f"could not save preprocessing: {exc}") from exc
        return True

    def get_preprocessing(self, preprocessing_id: str) -> PreprocessingFit | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM preprocessing_fits WHERE preprocessing_id=?",
                (str(preprocessing_id),),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        return PreprocessingFit(
            preprocessing_id=str(payload["preprocessing_id"]),
            method=payload["method"],
            feature_names=tuple(payload.get("feature_names", ())),
            parameters=dict(payload.get("parameters", {})),
            training_digest=str(payload["training_digest"]),
            version=str(payload.get("version", "")),
            fitted_at=(
                datetime.fromisoformat(str(payload["fitted_at"]))
                if payload.get("fitted_at")
                else None
            ),
        )

    def save_selection(self, report: SelectionReport) -> bool:
        payload = report.to_dict()
        encoded = json.dumps(payload, sort_keys=True)
        with self._lock:
            try:
                existing = self._connection.execute(
                    "SELECT payload_json FROM selection_reports WHERE report_id=?",
                    (report.report_id,),
                ).fetchone()
                if existing is not None:
                    prior = json.loads(str(existing["payload_json"]))
                    comparable = dict(payload)
                    prior.pop("generated_at", None)
                    comparable.pop("generated_at", None)
                    if prior != comparable:
                        raise FeatureStoreError(
                            "a selection report id cannot be reused with different content"
                        )
                    return False
                self._connection.execute(
                    "INSERT INTO selection_reports(report_id, version, payload_json, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        report.report_id,
                        report.version,
                        encoded,
                        _iso(),
                    ),
                )
                self._connection.commit()
            except sqlite3.Error as exc:
                self._connection.rollback()
                raise FeatureStoreError(f"could not save selection report: {exc}") from exc
        return True

    def get_selection(self, report_id: str) -> SelectionReport | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM selection_reports WHERE report_id=?",
                (str(report_id),),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        return SelectionReport(
            report_id=str(payload["report_id"]),
            version=str(payload["version"]),
            feature_names=tuple(payload.get("feature_names", ())),
            decisions=tuple(
                SelectionDecision(
                    feature=item["feature"],
                    issue=item["issue"],
                    detail=item["detail"],
                    retained=bool(item.get("retained", True)),
                    related_to=str(item.get("related_to", "")),
                )
                for item in payload.get("decisions", ())
            ),
            correlation_threshold=float(payload.get("correlation_threshold", 0.95)),
            missing_threshold=float(payload.get("missing_threshold", 0.5)),
            observations=int(payload.get("observations", 0)),
            leakage_safe=bool(payload.get("leakage_safe", True)),
            generated_at=(
                datetime.fromisoformat(str(payload["generated_at"]))
                if payload.get("generated_at")
                else None
            ),
        )

    def health(self) -> dict[str, Any]:
        with self._lock:
            try:
                self._connection.execute("SELECT 1").fetchone()
                return {"ok": True, "path": self.path, "schema_version": self.schema_version}
            except sqlite3.Error as exc:
                return {"ok": False, "path": self.path, "error": str(exc)}

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "feature_definitions": int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM feature_definitions"
                    ).fetchone()[0]
                ),
                "feature_sets": int(
                    self._connection.execute("SELECT COUNT(*) FROM feature_sets").fetchone()[0]
                ),
                "regimes": int(
                    self._connection.execute("SELECT COUNT(*) FROM regimes").fetchone()[0]
                ),
                "preprocessing_fits": int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM preprocessing_fits"
                    ).fetchone()[0]
                ),
                "selection_reports": int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM selection_reports"
                    ).fetchone()[0]
                ),
                "generations": int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM feature_generations"
                    ).fetchone()[0]
                ),
            }


__all__ = ["FeatureStore", "FeatureStoreError"]
