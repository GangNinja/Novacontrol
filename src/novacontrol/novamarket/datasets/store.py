"""SQLite persistence for PI-02 datasets, selections, gaps and training sets.

The store uses the existing NovaMarket database file.  It adds only
dataset-owned tables and a small metadata table with its own schema version;
the market, portfolio, risk, execution and prediction tables are untouched.

Three invariants shape the schema:

* **No price duplication.** A dataset member is a ``bar_id`` reference with the
  ordinal that fixes its position.  Prices remain in ``market_bars``.
* **Immutability.** A completed dataset version, its members, gaps and training
  samples are protected by SQLite triggers.  Corrected observations create a
  new version; they never rewrite an old one.
* **Visible failures.** An acquisition/validation run is recorded in
  ``dataset_builds`` and may only become a version when its outcome is known.
  An incomplete version is stored with ``status='incomplete'`` rather than
  being presented as complete.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from novacontrol.novamarket.datasets.models import (
    PI02_SCHEMA_VERSION,
    CorporateAction,
    DatasetMember,
    DatasetStateError,
    DatasetValidationError,
    DatasetVersion,
    GapRecord,
    MarketAnnotation,
    MemberPage,
    SelectionRecord,
    SplitSummary,
    TrainingDataset,
    TrainingSample,
)
from novacontrol.novamarket.marketdata.models import (
    AppendResult,
    AppendStatus,
    DataQuality,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dataset_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_versions (
    dataset_id TEXT NOT NULL,
    version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    quality TEXT NOT NULL,
    resolution TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    normalization_version TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (dataset_id, version)
);
CREATE INDEX IF NOT EXISTS ix_dataset_versions_created
    ON dataset_versions(dataset_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_dataset_versions_digest
    ON dataset_versions(content_digest);
CREATE INDEX IF NOT EXISTS ix_dataset_versions_resolution
    ON dataset_versions(dataset_id, resolution, created_at DESC);

CREATE TABLE IF NOT EXISTS dataset_segments (
    dataset_id TEXT NOT NULL,
    version TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    exchange TEXT NOT NULL,
    bar_count INTEGER NOT NULL,
    first_at TEXT,
    last_at TEXT,
    quality TEXT NOT NULL,
    coverage_ratio REAL,
    payload TEXT NOT NULL,
    PRIMARY KEY (dataset_id, version, instrument_id)
);
CREATE INDEX IF NOT EXISTS ix_dataset_segments_exchange
    ON dataset_segments(exchange, dataset_id, version);

CREATE TABLE IF NOT EXISTS dataset_members (
    dataset_id TEXT NOT NULL,
    version TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    exchange TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    bar_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    quality TEXT NOT NULL,
    PRIMARY KEY (dataset_id, version, instrument_id, ordinal)
);
CREATE INDEX IF NOT EXISTS ix_dataset_members_time
    ON dataset_members(dataset_id, version, instrument_id, observed_at);
CREATE INDEX IF NOT EXISTS ix_dataset_members_quality
    ON dataset_members(dataset_id, version, quality, observed_at);
CREATE INDEX IF NOT EXISTS ix_dataset_members_bar
    ON dataset_members(bar_id);
CREATE INDEX IF NOT EXISTS ix_dataset_members_exchange
    ON dataset_members(dataset_id, version, exchange, observed_at);
CREATE INDEX IF NOT EXISTS ix_dataset_members_provider
    ON dataset_members(dataset_id, version, provider, observed_at);

CREATE TABLE IF NOT EXISTS dataset_selections (
    dataset_id TEXT NOT NULL,
    version TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    selected_bar_id TEXT NOT NULL,
    selected_provider TEXT NOT NULL,
    alternatives_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (dataset_id, version, instrument_id, observed_at)
);

CREATE TABLE IF NOT EXISTS dataset_gaps (
    dataset_id TEXT NOT NULL,
    version TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    count INTEGER NOT NULL,
    detail TEXT NOT NULL,
    verified INTEGER NOT NULL,
    PRIMARY KEY (dataset_id, version, instrument_id, kind, start_at, end_at)
);
CREATE INDEX IF NOT EXISTS ix_dataset_gaps_kind
    ON dataset_gaps(dataset_id, version, kind);
CREATE INDEX IF NOT EXISTS ix_dataset_gaps_time
    ON dataset_gaps(dataset_id, version, start_at);

CREATE TABLE IF NOT EXISTS dataset_corporate_actions (
    action_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    announced_at TEXT,
    source TEXT NOT NULL,
    verified INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (instrument_id, kind, effective_at, source)
);
CREATE INDEX IF NOT EXISTS ix_dataset_actions_instrument
    ON dataset_corporate_actions(instrument_id, effective_at);

CREATE TABLE IF NOT EXISTS dataset_annotations (
    annotation_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    source TEXT NOT NULL,
    verified INTEGER NOT NULL,
    detail TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dataset_annotations_window
    ON dataset_annotations(instrument_id, start_at, end_at);

CREATE TABLE IF NOT EXISTS dataset_builds (
    build_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    checkpoint_json TEXT NOT NULL,
    error TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dataset_builds_dataset
    ON dataset_builds(dataset_id, started_at DESC);

CREATE TABLE IF NOT EXISTS training_versions (
    training_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    horizon TEXT NOT NULL,
    resolution TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    content_digest TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_training_versions_dataset
    ON training_versions(dataset_id, dataset_version, horizon);

CREATE TABLE IF NOT EXISTS training_samples (
    training_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    instrument_id TEXT NOT NULL,
    split TEXT NOT NULL,
    horizon TEXT NOT NULL,
    decision_at TEXT NOT NULL,
    target_at TEXT NOT NULL,
    decision_bar_id TEXT NOT NULL,
    target_bar_id TEXT NOT NULL,
    window_start_at TEXT NOT NULL,
    window_end_at TEXT NOT NULL,
    decision_close TEXT NOT NULL,
    target_close TEXT NOT NULL,
    target_return TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (training_id, ordinal)
);
CREATE INDEX IF NOT EXISTS ix_training_samples_split
    ON training_samples(training_id, split, decision_at);
CREATE INDEX IF NOT EXISTS ix_training_samples_instrument
    ON training_samples(training_id, instrument_id, decision_at);

CREATE TABLE IF NOT EXISTS training_splits (
    training_id TEXT NOT NULL,
    split TEXT NOT NULL,
    start_at TEXT,
    end_at TEXT,
    sample_count INTEGER NOT NULL,
    purged INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (training_id, split)
);

CREATE TRIGGER IF NOT EXISTS dataset_versions_immutable_update
BEFORE UPDATE ON dataset_versions
BEGIN
    SELECT RAISE(ABORT, 'a dataset version is immutable');
END;
CREATE TRIGGER IF NOT EXISTS dataset_versions_immutable_delete
BEFORE DELETE ON dataset_versions
BEGIN
    SELECT RAISE(ABORT, 'a dataset version is immutable');
END;
CREATE TRIGGER IF NOT EXISTS dataset_members_immutable_update
BEFORE UPDATE ON dataset_members
BEGIN
    SELECT RAISE(ABORT, 'dataset membership is immutable');
END;
CREATE TRIGGER IF NOT EXISTS dataset_members_immutable_delete
BEFORE DELETE ON dataset_members
BEGIN
    SELECT RAISE(ABORT, 'dataset membership is immutable');
END;
CREATE TRIGGER IF NOT EXISTS dataset_segments_immutable_update
BEFORE UPDATE ON dataset_segments
BEGIN
    SELECT RAISE(ABORT, 'dataset segment metadata is immutable');
END;
CREATE TRIGGER IF NOT EXISTS dataset_segments_immutable_delete
BEFORE DELETE ON dataset_segments
BEGIN
    SELECT RAISE(ABORT, 'dataset segment metadata is immutable');
END;
CREATE TRIGGER IF NOT EXISTS dataset_selections_immutable_update
BEFORE UPDATE ON dataset_selections
BEGIN
    SELECT RAISE(ABORT, 'dataset selection evidence is immutable');
END;
CREATE TRIGGER IF NOT EXISTS dataset_selections_immutable_delete
BEFORE DELETE ON dataset_selections
BEGIN
    SELECT RAISE(ABORT, 'dataset selection evidence is immutable');
END;
CREATE TRIGGER IF NOT EXISTS dataset_gaps_immutable_update
BEFORE UPDATE ON dataset_gaps
BEGIN
    SELECT RAISE(ABORT, 'dataset gap records are immutable');
END;
CREATE TRIGGER IF NOT EXISTS dataset_gaps_immutable_delete
BEFORE DELETE ON dataset_gaps
BEGIN
    SELECT RAISE(ABORT, 'dataset gap records are immutable');
END;
CREATE TRIGGER IF NOT EXISTS dataset_corporate_actions_immutable_update
BEFORE UPDATE ON dataset_corporate_actions
BEGIN
    SELECT RAISE(ABORT, 'a corporate action is append-only');
END;
CREATE TRIGGER IF NOT EXISTS dataset_corporate_actions_immutable_delete
BEFORE DELETE ON dataset_corporate_actions
BEGIN
    SELECT RAISE(ABORT, 'a corporate action is append-only');
END;
CREATE TRIGGER IF NOT EXISTS dataset_annotations_immutable_update
BEFORE UPDATE ON dataset_annotations
BEGIN
    SELECT RAISE(ABORT, 'an annotation is append-only');
END;
CREATE TRIGGER IF NOT EXISTS dataset_annotations_immutable_delete
BEFORE DELETE ON dataset_annotations
BEGIN
    SELECT RAISE(ABORT, 'an annotation is append-only');
END;
CREATE TRIGGER IF NOT EXISTS training_versions_immutable_update
BEFORE UPDATE ON training_versions
BEGIN
    SELECT RAISE(ABORT, 'a training dataset version is immutable');
END;
CREATE TRIGGER IF NOT EXISTS training_versions_immutable_delete
BEFORE DELETE ON training_versions
BEGIN
    SELECT RAISE(ABORT, 'a training dataset version is immutable');
END;
CREATE TRIGGER IF NOT EXISTS training_samples_immutable_update
BEFORE UPDATE ON training_samples
BEGIN
    SELECT RAISE(ABORT, 'training samples are immutable');
END;
CREATE TRIGGER IF NOT EXISTS training_samples_immutable_delete
BEFORE DELETE ON training_samples
BEGIN
    SELECT RAISE(ABORT, 'training samples are immutable');
END;
CREATE TRIGGER IF NOT EXISTS training_splits_immutable_update
BEFORE UPDATE ON training_splits
BEGIN
    SELECT RAISE(ABORT, 'training split summaries are immutable');
END;
CREATE TRIGGER IF NOT EXISTS training_splits_immutable_delete
BEFORE DELETE ON training_splits
BEGIN
    SELECT RAISE(ABORT, 'training split summaries are immutable');
END;
CREATE TRIGGER IF NOT EXISTS training_versions_immutable_update
BEFORE UPDATE ON training_versions
BEGIN
    SELECT RAISE(ABORT, 'a training dataset version is immutable');
END;
CREATE TRIGGER IF NOT EXISTS training_versions_immutable_delete
BEFORE DELETE ON training_versions
BEGIN
    SELECT RAISE(ABORT, 'a training dataset version is immutable');
END;
CREATE TRIGGER IF NOT EXISTS training_samples_immutable_update
BEFORE UPDATE ON training_samples
BEGIN
    SELECT RAISE(ABORT, 'training samples are immutable');
END;
CREATE TRIGGER IF NOT EXISTS training_samples_immutable_delete
BEFORE DELETE ON training_samples
BEGIN
    SELECT RAISE(ABORT, 'training samples are immutable');
END;
"""


def _json(payload: Mapping[str, Any] | Sequence[Any] | None) -> str:
    return json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str)


def _loads(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DatasetValidationError("dataset timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


class DatasetStore:
    """Thread-safe persistence for dataset versions and training sets."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        clock: Any | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._clock = clock or (lambda: datetime.now(UTC))
        self.path = str(path)
        if self.path != ":memory:":
            try:
                Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise DatasetValidationError(
                    f"dataset directory is not writable: {exc}"
                ) from exc
        try:
            self._connection = sqlite3.connect(
                self.path,
                check_same_thread=False,
                timeout=10.0,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys=OFF")
            self._connection.execute("PRAGMA busy_timeout=10000")
            if self.path != ":memory:":
                self._connection.execute("PRAGMA journal_mode=WAL")
            self._migrate()
        except sqlite3.Error as exc:
            raise DatasetValidationError(
                f"dataset store could not be opened: {exc}"
            ) from exc

    def _migrate(self) -> None:
        with self._connection:
            self._connection.executescript(_SCHEMA)
            row = self._connection.execute(
                "SELECT value FROM dataset_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO dataset_meta(key, value) VALUES ('schema_version', ?)",
                    (str(PI02_SCHEMA_VERSION),),
                )
            elif int(row["value"]) > PI02_SCHEMA_VERSION:
                raise DatasetValidationError(
                    f"dataset schema {row['value']} was written by a newer build; "
                    f"this build speaks {PI02_SCHEMA_VERSION}"
                )

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM dataset_meta WHERE key='schema_version'"
            ).fetchone()
        return int(row["value"]) if row is not None else 0

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # -- versions -------------------------------------------------------------

    def create_version(
        self,
        version: DatasetVersion,
        *,
        members: Sequence[DatasetMember],
        gaps: Sequence[GapRecord],
        selections: Sequence[SelectionRecord],
    ) -> AppendResult:
        expected_members = sum(segment.bar_count for segment in version.segments)
        if len(members) != expected_members:
            raise DatasetValidationError(
                "dataset membership does not match the segment bar counts: "
                f"{len(members)} row(s) for {expected_members} declared bar(s)"
            )
        for member in members:
            if (
                member.dataset_id != version.dataset_id
                or member.version != version.version
            ):
                raise DatasetValidationError(
                    "a dataset member belongs to a different dataset version"
                )
        payload = _json(version.to_dict())
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT content_digest FROM dataset_versions "
                "WHERE dataset_id=? AND version=?",
                (version.dataset_id, version.version),
            ).fetchone()
            if existing is not None:
                if str(existing["content_digest"]) == version.content_digest:
                    return AppendResult(
                        AppendStatus.DUPLICATE,
                        f"{version.dataset_id}:{version.version}",
                        "an identical version already exists",
                    )
                raise DatasetStateError(
                    f"dataset version {version.dataset_id}:{version.version} is "
                    "immutable and already has different content; build a new version"
                )
            self._connection.execute(
                """
                INSERT INTO dataset_versions (
                    dataset_id, version, created_at, status, quality, resolution,
                    start_at, end_at, content_digest, config_digest,
                    normalization_version, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version.dataset_id,
                    version.version,
                    _iso(version.created_at),
                    "complete" if version.complete else "incomplete",
                    version.quality.value,
                    version.resolution.value,
                    _iso(version.start_at),
                    _iso(version.end_at),
                    version.content_digest,
                    _json(version.config),
                    version.normalization_version,
                    payload,
                ),
            )
            for segment in version.segments:
                self._connection.execute(
                    """
                    INSERT INTO dataset_segments (
                        dataset_id, version, instrument_id, exchange, bar_count,
                        first_at, last_at, quality, coverage_ratio, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version.dataset_id,
                        version.version,
                        segment.instrument_id,
                        segment.exchange,
                        segment.bar_count,
                        _iso(segment.first_at) if segment.first_at else None,
                        _iso(segment.last_at) if segment.last_at else None,
                        segment.quality.value,
                        segment.coverage.coverage_ratio,
                        _json(segment.to_dict()),
                    ),
                )
            if members:
                self._connection.executemany(
                    """
                    INSERT INTO dataset_members (
                        dataset_id, version, instrument_id, exchange, ordinal,
                        bar_id, provider, observed_at, quality
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            member.dataset_id,
                            member.version,
                            member.instrument_id,
                            member.exchange,
                            member.ordinal,
                            member.bar_id,
                            member.provider,
                            _iso(member.observed_at),
                            member.quality.value,
                        )
                        for member in members
                    ],
                )
            if selections:
                self._connection.executemany(
                    """
                    INSERT INTO dataset_selections (
                        dataset_id, version, instrument_id, observed_at,
                        selected_bar_id, selected_provider, alternatives_json, reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            version.dataset_id,
                            version.version,
                            item.instrument_id,
                            _iso(item.observed_at),
                            item.selected_bar_id,
                            item.selected_provider,
                            _json(list(item.alternatives)),
                            item.reason,
                        )
                        for item in selections
                    ],
                )
            if gaps:
                self._connection.executemany(
                    """
                    INSERT INTO dataset_gaps (
                        dataset_id, version, instrument_id, kind, start_at,
                        end_at, count, detail, verified
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            version.dataset_id,
                            version.version,
                            gap.instrument_id,
                            gap.kind.value,
                            _iso(gap.start_at),
                            _iso(gap.end_at),
                            gap.count,
                            gap.detail,
                            1 if gap.verified else 0,
                        )
                        for gap in gaps
                    ],
                )
        return AppendResult(
            AppendStatus.INSERTED, f"{version.dataset_id}:{version.version}"
        )

    def get_version(
        self, dataset_id: str, version: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM dataset_versions "
                "WHERE dataset_id=? AND version=?",
                (str(dataset_id), str(version)),
            ).fetchone()
        return _loads(row["payload"]) if row is not None else None

    def version_row(
        self, dataset_id: str, version: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM dataset_versions WHERE dataset_id=? AND version=?",
                (str(dataset_id), str(version)),
            ).fetchone()
        return dict(row) if row is not None else None

    def latest_version(self, dataset_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM dataset_versions WHERE dataset_id=? "
                "ORDER BY created_at DESC, LENGTH(version) DESC, version DESC LIMIT 1",
                (str(dataset_id),),
            ).fetchone()
        return _loads(row["payload"]) if row is not None else None

    def find_by_content_digest(
        self, dataset_id: str, content_digest: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM dataset_versions "
                "WHERE dataset_id=? AND content_digest=? "
                "ORDER BY created_at ASC LIMIT 1",
                (str(dataset_id), str(content_digest)),
            ).fetchone()
        return _loads(row["payload"]) if row is not None else None

    def versions(
        self,
        dataset_id: str | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit, offset = self._page(limit, offset)
        clause = "WHERE dataset_id=?" if dataset_id else ""
        params: tuple[Any, ...] = (str(dataset_id),) if dataset_id else ()
        with self._lock:
            total = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM dataset_versions {clause}", params
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                "SELECT dataset_id, version, created_at, status, quality, "
                "resolution, start_at, end_at, content_digest, normalization_version "
                f"FROM dataset_versions {clause} "
                "ORDER BY created_at DESC, LENGTH(version) DESC, version DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None,
        }

    def dataset_ids(self) -> tuple[str, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT DISTINCT dataset_id FROM dataset_versions ORDER BY dataset_id"
            ).fetchall()
        return tuple(str(row["dataset_id"]) for row in rows)

    # -- members --------------------------------------------------------------

    def member_rows(
        self,
        dataset_id: str,
        version: str,
        *,
        instrument_id: str | None = None,
        exchange: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        quality: str | None = None,
        provider: str | None = None,
        limit: int = 1000,
        offset: int = 0,
        ascending: bool = True,
    ) -> tuple[tuple[dict[str, Any], ...], int]:
        limit, offset = self._page(limit, offset)
        clauses = ["dataset_id=?", "version=?"]
        params: list[Any] = [str(dataset_id), str(version)]
        if instrument_id:
            clauses.append("instrument_id=?")
            params.append(str(instrument_id))
        if exchange:
            clauses.append("UPPER(exchange)=?")
            params.append(str(exchange).strip().upper())
        if start is not None:
            clauses.append("observed_at >= ?")
            params.append(_iso(start))
        if end is not None:
            clauses.append("observed_at <= ?")
            params.append(_iso(end))
        if quality:
            clauses.append("quality=?")
            params.append(str(quality))
        if provider:
            clauses.append("provider=?")
            params.append(str(provider).strip().lower())
        where = " WHERE " + " AND ".join(clauses)
        order = "ASC" if ascending else "DESC"
        with self._lock:
            total = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM dataset_members{where}", params
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                f"SELECT * FROM dataset_members{where} "
                f"ORDER BY observed_at {order}, instrument_id {order}, "
                f"ordinal {order} LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return tuple(dict(row) for row in rows), total

    def members(
        self,
        dataset_id: str,
        version: str,
        **kwargs: Any,
    ) -> MemberPage:
        limit = int(kwargs.get("limit", 1000))
        offset = int(kwargs.get("offset", 0))
        rows, total = self.member_rows(dataset_id, version, **kwargs)
        items = tuple(
            DatasetMember(
                dataset_id=str(row["dataset_id"]),
                version=str(row["version"]),
                instrument_id=str(row["instrument_id"]),
                exchange=str(row["exchange"]),
                ordinal=int(row["ordinal"]),
                bar_id=str(row["bar_id"]),
                provider=str(row["provider"]),
                observed_at=datetime.fromisoformat(str(row["observed_at"])),
                quality=DataQuality(str(row["quality"])),
            )
            for row in rows
        )
        return MemberPage(items, total, limit, offset)

    def all_member_rows(
        self, dataset_id: str, version: str, instrument_id: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        clauses = ["dataset_id=?", "version=?"]
        params: list[Any] = [str(dataset_id), str(version)]
        if instrument_id:
            clauses.append("instrument_id=?")
            params.append(str(instrument_id))
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM dataset_members WHERE "
                + " AND ".join(clauses)
                + " ORDER BY instrument_id ASC, observed_at ASC, ordinal ASC",
                params,
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def segments(self, dataset_id: str, version: str) -> tuple[dict[str, Any], ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM dataset_segments "
                "WHERE dataset_id=? AND version=? ORDER BY instrument_id ASC",
                (str(dataset_id), str(version)),
            ).fetchall()
        return tuple(_loads(row["payload"]) for row in rows)

    def gaps(
        self,
        dataset_id: str,
        version: str,
        *,
        instrument_id: str | None = None,
        kind: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit, offset = self._page(limit, offset)
        clauses = ["dataset_id=?", "version=?"]
        params: list[Any] = [str(dataset_id), str(version)]
        if instrument_id:
            clauses.append("instrument_id=?")
            params.append(str(instrument_id))
        if kind:
            clauses.append("kind=?")
            params.append(str(kind))
        where = " WHERE " + " AND ".join(clauses)
        with self._lock:
            total = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM dataset_gaps{where}", params
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                f"SELECT * FROM dataset_gaps{where} "
                "ORDER BY start_at ASC, kind ASC, instrument_id ASC, end_at ASC "
                "LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None,
        }

    def selections(
        self,
        dataset_id: str,
        version: str,
        *,
        instrument_id: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit, offset = self._page(limit, offset)
        clauses = ["dataset_id=?", "version=?"]
        params: list[Any] = [str(dataset_id), str(version)]
        if instrument_id:
            clauses.append("instrument_id=?")
            params.append(str(instrument_id))
        where = " WHERE " + " AND ".join(clauses)
        with self._lock:
            total = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM dataset_selections{where}", params
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                f"SELECT * FROM dataset_selections{where} "
                "ORDER BY observed_at ASC, instrument_id ASC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {
            "items": [
                {
                    **dict(row),
                    "alternatives": json.loads(row["alternatives_json"] or "[]"),
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
        }

    # -- corporate actions and annotations ------------------------------------

    def record_corporate_action(self, action: CorporateAction) -> AppendResult:
        payload = _json(action.to_dict())
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT payload FROM dataset_corporate_actions WHERE action_id=?",
                (action.action_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload"]) == payload:
                    return AppendResult(
                        AppendStatus.DUPLICATE,
                        action.action_id,
                        "the corporate action is already recorded",
                    )
                return AppendResult(
                    AppendStatus.CONFLICT,
                    action.action_id,
                    "a corporate action with this identity already exists with "
                    "different values; the original is preserved",
                )
            self._connection.execute(
                """
                INSERT INTO dataset_corporate_actions (
                    action_id, instrument_id, kind, effective_at, announced_at,
                    source, verified, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.action_id,
                    action.instrument_id,
                    str(action.kind),
                    _iso(action.effective_at),
                    _iso(action.announced_at) if action.announced_at else None,
                    action.source,
                    1 if action.verified else 0,
                    payload,
                    _iso(self._clock()),
                ),
            )
        return AppendResult(AppendStatus.INSERTED, action.action_id)

    def corporate_actions(
        self,
        *,
        instrument_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        verified_only: bool = False,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit, offset = self._page(limit, offset)
        clauses: list[str] = []
        params: list[Any] = []
        if instrument_id:
            clauses.append("instrument_id=?")
            params.append(str(instrument_id))
        if start is not None:
            clauses.append("effective_at >= ?")
            params.append(_iso(start))
        if end is not None:
            clauses.append("effective_at <= ?")
            params.append(_iso(end))
        if verified_only:
            clauses.append("verified=1")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            total = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM dataset_corporate_actions{where}", params
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                f"SELECT payload FROM dataset_corporate_actions{where} "
                "ORDER BY effective_at ASC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {
            "items": [_loads(row["payload"]) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
        }

    def record_annotation(self, annotation: MarketAnnotation) -> AppendResult:
        payload = _json(annotation.to_dict())
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT payload FROM dataset_annotations WHERE annotation_id=?",
                (annotation.annotation_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload"]) == payload:
                    return AppendResult(
                        AppendStatus.DUPLICATE, annotation.annotation_id
                    )
                return AppendResult(
                    AppendStatus.CONFLICT,
                    annotation.annotation_id,
                    "the annotation already exists with different values",
                )
            self._connection.execute(
                """
                INSERT INTO dataset_annotations (
                    annotation_id, instrument_id, kind, start_at, end_at,
                    source, verified, detail, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    annotation.annotation_id,
                    annotation.instrument_id,
                    str(annotation.kind),
                    _iso(annotation.start_at),
                    _iso(annotation.end_at),
                    annotation.source,
                    1 if annotation.verified else 0,
                    annotation.detail,
                    payload,
                    _iso(self._clock()),
                ),
            )
        return AppendResult(AppendStatus.INSERTED, annotation.annotation_id)

    def annotations(
        self,
        *,
        instrument_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> tuple[MarketAnnotation, ...]:
        limit, offset = self._page(limit, offset)
        clauses: list[str] = []
        params: list[Any] = []
        if instrument_id:
            clauses.append("instrument_id=?")
            params.append(str(instrument_id))
        if start is not None:
            clauses.append("end_at >= ?")
            params.append(_iso(start))
        if end is not None:
            clauses.append("start_at <= ?")
            params.append(_iso(end))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM dataset_annotations{where} "
                "ORDER BY start_at ASC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return tuple(
            MarketAnnotation(
                instrument_id=str(row["instrument_id"]),
                kind=str(row["kind"]),
                start_at=datetime.fromisoformat(str(row["start_at"])),
                end_at=datetime.fromisoformat(str(row["end_at"])),
                source=str(row["source"]),
                verified=bool(row["verified"]),
                detail=str(row["detail"]),
                annotation_id=str(row["annotation_id"]),
            )
            for row in rows
        )

    # -- build journal --------------------------------------------------------

    def start_build(self, dataset_id: str, version: str) -> str:
        build_id = f"build_{uuid.uuid4().hex[:20]}"
        now = _iso(self._clock())
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO dataset_builds (
                    build_id, dataset_id, version, status, started_at,
                    updated_at, checkpoint_json, error
                ) VALUES (?, ?, ?, 'running', ?, ?, '{}', '')
                """,
                (build_id, str(dataset_id), str(version), now, now),
            )
        return build_id

    def update_build(
        self,
        build_id: str,
        *,
        status: str,
        checkpoint: Mapping[str, Any] | None = None,
        error: str = "",
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE dataset_builds SET status=?, updated_at=?, "
                "checkpoint_json=?, error=? WHERE build_id=?",
                (
                    str(status),
                    _iso(self._clock()),
                    _json(dict(checkpoint or {})),
                    str(error),
                    str(build_id),
                ),
            )

    def builds(
        self,
        dataset_id: str | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit, offset = self._page(limit, offset)
        clause = "WHERE dataset_id=?" if dataset_id else ""
        params: tuple[Any, ...] = (str(dataset_id),) if dataset_id else ()
        with self._lock:
            total = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM dataset_builds {clause}", params
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                f"SELECT * FROM dataset_builds {clause} "
                "ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {
            "items": [
                {
                    **dict(row),
                    "checkpoint": _loads(row["checkpoint_json"]),
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
        }

    # -- training datasets ----------------------------------------------------

    def create_training(
        self,
        training: TrainingDataset,
        samples: Sequence[TrainingSample],
    ) -> AppendResult:
        if len(samples) != training.sample_count:
            raise DatasetValidationError(
                "training sample rows do not match the declared sample count"
            )
        if sum(summary.sample_count for summary in training.splits.values()) != (
            training.sample_count
        ):
            raise DatasetValidationError(
                "training split summaries do not sum to the declared sample count"
            )
        payload = _json(training.to_dict())
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT content_digest FROM training_versions WHERE training_id=?",
                (training.training_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["content_digest"]) == training.content_digest:
                    return AppendResult(AppendStatus.DUPLICATE, training.training_id)
                raise DatasetStateError(
                    f"training dataset {training.training_id} is immutable and "
                    "already has different content"
                )
            self._connection.execute(
                """
                INSERT INTO training_versions (
                    training_id, dataset_id, dataset_version, horizon, resolution,
                    created_at, sample_count, content_digest, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    training.training_id,
                    training.dataset_id,
                    training.dataset_version,
                    training.horizon.value,
                    training.resolution.value,
                    _iso(training.created_at),
                    training.sample_count,
                    training.content_digest,
                    payload,
                ),
            )
            for split, summary in training.splits.items():
                self._connection.execute(
                    """
                    INSERT INTO training_splits (
                        training_id, split, start_at, end_at, sample_count,
                        purged, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        training.training_id,
                        split.value,
                        _iso(summary.start_at) if summary.start_at else None,
                        _iso(summary.end_at) if summary.end_at else None,
                        summary.sample_count,
                        summary.purged_target_overlaps,
                        _json(summary.to_dict()),
                    ),
                )
            if samples:
                self._connection.executemany(
                    """
                    INSERT INTO training_samples (
                        training_id, ordinal, instrument_id, split, horizon,
                        decision_at, target_at, decision_bar_id, target_bar_id,
                        window_start_at, window_end_at, decision_close,
                        target_close, target_return, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            training.training_id,
                            ordinal,
                            sample.instrument_id,
                            str(sample.split),
                            str(sample.horizon),
                            _iso(sample.decision_at),
                            _iso(sample.target_at),
                            sample.decision_bar_id,
                            sample.target_bar_id,
                            _iso(sample.window_start_at),
                            _iso(sample.window_end_at),
                            format(sample.decision_close, "f"),
                            format(sample.target_close, "f"),
                            format(sample.target_return or 0, "f"),
                            _json(sample.to_dict()),
                        )
                        for ordinal, sample in enumerate(samples)
                    ],
                )
        return AppendResult(AppendStatus.INSERTED, training.training_id)

    def get_training(self, training_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM training_versions WHERE training_id=?",
                (str(training_id),),
            ).fetchone()
        return _loads(row["payload"]) if row is not None else None

    def training_versions(
        self,
        *,
        dataset_id: str | None = None,
        dataset_version: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit, offset = self._page(limit, offset)
        clauses: list[str] = []
        params: list[Any] = []
        if dataset_id:
            clauses.append("dataset_id=?")
            params.append(str(dataset_id))
        if dataset_version:
            clauses.append("dataset_version=?")
            params.append(str(dataset_version))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            total = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM training_versions{where}", params
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                "SELECT training_id, dataset_id, dataset_version, horizon, "
                "resolution, created_at, sample_count, content_digest "
                f"FROM training_versions{where} "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
        }

    def training_samples(
        self,
        training_id: str,
        *,
        split: str | None = None,
        instrument_id: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit, offset = self._page(limit, offset)
        clauses = ["training_id=?"]
        params: list[Any] = [str(training_id)]
        if split:
            clauses.append("split=?")
            params.append(str(split))
        if instrument_id:
            clauses.append("instrument_id=?")
            params.append(str(instrument_id))
        where = " WHERE " + " AND ".join(clauses)
        with self._lock:
            total = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM training_samples{where}", params
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                f"SELECT payload FROM training_samples{where} "
                "ORDER BY decision_at ASC, instrument_id ASC, ordinal ASC "
                "LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {
            "items": [_loads(row["payload"]) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None,
        }

    def all_training_samples(
        self, training_id: str, *, split: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        clauses = ["training_id=?"]
        params: list[Any] = [str(training_id)]
        if split:
            clauses.append("split=?")
            params.append(str(split))
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM training_samples WHERE "
                + " AND ".join(clauses)
                + " ORDER BY decision_at ASC, instrument_id ASC, ordinal ASC",
                params,
            ).fetchall()
        return tuple(_loads(row["payload"]) for row in rows)

    # -- introspection --------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        with self._lock:
            counts = {
                "versions": int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM dataset_versions"
                    ).fetchone()[0]
                ),
                "segments": int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM dataset_segments"
                    ).fetchone()[0]
                ),
                "members": int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM dataset_members"
                    ).fetchone()[0]
                ),
                "gaps": int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM dataset_gaps"
                    ).fetchone()[0]
                ),
                "corporate_actions": int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM dataset_corporate_actions"
                    ).fetchone()[0]
                ),
                "annotations": int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM dataset_annotations"
                    ).fetchone()[0]
                ),
                "training_versions": int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM training_versions"
                    ).fetchone()[0]
                ),
                "training_samples": int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM training_samples"
                    ).fetchone()[0]
                ),
            }
        return {
            "path": self.path,
            "schema_version": self.schema_version,
            "counts": counts,
            "datasets": list(self.dataset_ids()),
        }

    def check(self) -> dict[str, Any]:
        with self._lock:
            integrity = [
                str(row[0])
                for row in self._connection.execute("PRAGMA quick_check").fetchall()
            ]
            orphans = 0
            has_market_bars = self._connection.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='market_bars'"
            ).fetchone()[0]
            if has_market_bars:
                orphans = int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM dataset_members m "
                        "LEFT JOIN market_bars b ON b.bar_id = m.bar_id "
                        "WHERE b.bar_id IS NULL"
                    ).fetchone()[0]
                )
        return {
            "ok": integrity == ["ok"] and orphans == 0,
            "integrity": integrity,
            "orphan_members": orphans,
            "schema_version": self.schema_version,
        }

    @staticmethod
    def _page(limit: int, offset: int) -> tuple[int, int]:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise DatasetValidationError("limit must be an integer")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise DatasetValidationError("offset must be a non-negative integer")
        if not 1 <= limit <= 5_000:
            raise DatasetValidationError("limit must be between 1 and 5000")
        return limit, offset


__all__ = ["DatasetStore"]
