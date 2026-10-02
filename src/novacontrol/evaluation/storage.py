"""Storage for trajectories, evaluations, rewards and datasets.

This extends the persistence layer NovaControl already has rather than adding a
second one. A row is a line of JSON in a file inside the application's data
directory, written through a temporary file and an atomic replace — the exact
shape ``optimization/benchmark.py`` and the audit trail's sink already use. No
database, no server, no schema migration: the deployment target is a Windows
desktop, and evidence kept in a plain file that can be read with any editor is
worth more there than a database that has to be running.

Every repository is REPLACEABLE: it takes a store, and the store is a tiny
protocol (``append``/``read``/``replace``) with an in-memory implementation for
tests and a JSONL implementation for a real installation. Nothing above this
layer knows which one it got.

Saving is an UPSERT by id, because the same row can legitimately arrive twice:
a trajectory is delivered once when the task ends and again if a late
annotation (the request's measured latency, its memory delta) reaches it
afterwards. The second write replaces the first instead of competing with it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from novacontrol.audit.models import json_safe
from novacontrol.evaluation.datasets import GoldenDataset
from novacontrol.evaluation.evaluator import EvaluationResult
from novacontrol.evaluation.models import (
    AgentTrajectory,
    json_safe_trajectory,
    parse_trajectories,
)
from novacontrol.evaluation.reward import RewardResult

#: Where the evaluation layer keeps its rows inside an application's data
#: directory. One folder, three files, no cross-references to resolve.
EVALUATION_DIRECTORY = "evaluation"
TRAJECTORY_FILENAME = "trajectories.jsonl"
EVALUATION_FILENAME = "evaluations.jsonl"
REWARD_FILENAME = "rewards.jsonl"
DATASET_FILENAME = "datasets.jsonl"

#: How many rows a repository keeps by default, per file. Bounded so an
#: installation that has been running for months cannot fill the disk with
#: evidence; configurable for an operator who wants a longer history.
DEFAULT_REPOSITORY_CAP = 2000


@runtime_checkable
class RecordStore(Protocol):
    """Storage for rows. ``local`` is declared, like the audit sink's."""

    name: str
    local: bool

    def append(self, record: Mapping[str, Any]) -> None: ...

    def read(self, limit: int = 0) -> list[Mapping[str, Any]]: ...

    def replace(self, records: Sequence[Mapping[str, Any]]) -> None: ...


class InMemoryRecordStore:
    """In-process store: tests, and a run with no data directory."""

    name = "memory"
    local = True

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    def append(self, record: Mapping[str, Any]) -> None:
        self._rows.append(dict(record))

    def read(self, limit: int = 0) -> list[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = list(self._rows)
        if limit and limit > 0:
            return rows[-limit:]
        return rows

    def replace(self, records: Sequence[Mapping[str, Any]]) -> None:
        self._rows = [dict(row) for row in records]

    def __len__(self) -> int:
        return len(self._rows)


class JsonlRecordStore:
    """Append-only JSONL, durable and local.

    Rewrites go through a temporary file and an atomic replace, exactly like the
    audit sink: a crash mid-prune leaves the previous history intact rather than
    half of a new one. Reading tolerates a damaged line by skipping it, because
    a record store that refuses to load is a record store nobody can analyse.
    """

    name = "jsonl"
    local = True

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, record: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(_dump(record) + "\n")

    def read(self, limit: int = 0) -> list[Mapping[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[Mapping[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                row = _parse(line)
                if row is not None:
                    rows.append(row)
        if limit and limit > 0:
            return rows[-limit:]
        return rows

    def replace(self, records: Sequence[Mapping[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            for row in records:
                handle.write(_dump(row) + "\n")
        os.replace(temporary, self.path)


def _dump(row: Mapping[str, Any]) -> str:
    return json.dumps(json_safe(row), ensure_ascii=False)


def _parse(line: str) -> Mapping[str, Any] | None:
    text = line.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _timestamp(value: Any) -> str:
    return str(value or "")


def _cutoff(days: int, *, now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    return (moment - timedelta(days=max(0, int(days)))).isoformat()


class JsonlRepository:
    """Upsert-by-id, bounded, prunable storage for one kind of row."""

    #: The field that identifies a row. Saving a row whose id is already stored
    #: REPLACES it; that is what makes a late annotation an update.
    id_field = "record_id"

    def __init__(
        self, store: RecordStore | None = None, *, cap: int = DEFAULT_REPOSITORY_CAP
    ) -> None:
        self._store: RecordStore = store if store is not None else InMemoryRecordStore()
        self.cap = max(0, int(cap))

    @property
    def store(self) -> RecordStore:
        return self._store

    def rows(self, limit: int = 0) -> list[Mapping[str, Any]]:
        return list(self._store.read(limit))

    def save_row(self, row: Mapping[str, Any]) -> None:
        """Append, then reconcile: the newest copy of an id wins, capped by ``cap``."""
        record_id = _timestamp(row.get(self.id_field))
        if not record_id:
            raise ValueError(f"a {type(self).__name__} row needs a {self.id_field}")
        self._store.append(row)
        rows = list(self._store.read())
        positions = [
            index
            for index, existing in enumerate(rows)
            if _timestamp(existing.get(self.id_field)) == record_id
        ]
        over_cap = bool(self.cap) and len(rows) > self.cap
        if len(positions) < 2 and not over_cap:
            return
        # Keep the LAST copy of an id (the most recent write) and drop every
        # earlier one, then drop the oldest rows while the file is over its cap.
        if len(positions) > 1:
            stale = set(positions[:-1])
            rows = [row_ for index, row_ in enumerate(rows) if index not in stale]
        if over_cap:
            rows = rows[-self.cap :]
        self._store.replace(rows)

    def prune(
        self, *, max_records: int = 0, older_than_days: int = 0, now: datetime | None = None
    ) -> int:
        """Drop rows that are too old and/or beyond a cap. Returns how many went."""
        rows = list(self._store.read())
        if not rows:
            return 0
        kept = rows
        if older_than_days:
            cutoff = _cutoff(older_than_days, now=now)
            kept = [row for row in kept if _timestamp(row.get("timestamp")) >= cutoff]
        if max_records:
            kept = kept[-max(1, int(max_records)) :]
        removed = len(rows) - len(kept)
        if removed:
            self._store.replace(kept)
        return removed

    def clear(self) -> int:
        removed = len(self._store.read())
        self._store.replace([])
        return removed

    def count(self) -> int:
        return len(self._store.read())


class TrajectoryRepository(JsonlRepository):
    """Trajectories, with the filters the inspection surface needs."""

    id_field = "trajectory_id"

    def save(self, trajectory: AgentTrajectory) -> AgentTrajectory:
        self.save_row(json_safe_trajectory(trajectory))
        return trajectory

    def get(self, trajectory_id: str) -> AgentTrajectory | None:
        wanted = str(trajectory_id or "")
        for row in reversed(self._store.read()):
            if _timestamp(row.get("trajectory_id")) == wanted:
                return AgentTrajectory.from_dict(dict(row))
        return None

    def list(
        self,
        *,
        task_id: str = "",
        model: str = "",
        status: str = "",
        success: bool | None = None,
        since: str = "",
        until: str = "",
        limit: int = 0,
        newest_first: bool = True,
    ) -> tuple[AgentTrajectory, ...]:
        """Filter by task, model, date and status — the four the phase asks for."""
        wanted_task = str(task_id or "").strip()
        wanted_model = str(model or "").strip().lower()
        wanted_status = str(status or "").strip().lower()
        found: list[AgentTrajectory] = []
        for trajectory in parse_trajectories(self._store.read()):
            if wanted_task and wanted_task not in {trajectory.task_id, trajectory.parent_task_id}:
                continue
            if wanted_status and trajectory.status.lower() != wanted_status:
                continue
            if success is not None and trajectory.success is not success:
                continue
            if wanted_model and not _mentions_model(trajectory, wanted_model):
                continue
            if since and trajectory.timestamp < since:
                continue
            if until and trajectory.timestamp > until:
                continue
            found.append(trajectory)
        if newest_first:
            found.reverse()
        if limit and limit > 0:
            return tuple(found[:limit])
        return tuple(found)

    def remove(self, trajectory_id: str) -> int:
        wanted = str(trajectory_id or "")
        rows = list(self._store.read())
        kept = [row for row in rows if _timestamp(row.get("trajectory_id")) != wanted]
        if len(kept) != len(rows):
            self._store.replace(kept)
        return len(rows) - len(kept)


class EvaluationRepository(JsonlRepository):
    """Evaluation results, keyed by ``evaluation_id`` (one per trajectory)."""

    id_field = "evaluation_id"

    def save(self, result: EvaluationResult) -> EvaluationResult:
        self.save_row(json_safe(result.to_dict()))
        return result

    def get(self, evaluation_id: str) -> EvaluationResult | None:
        wanted = str(evaluation_id or "")
        for row in reversed(self._store.read()):
            if _timestamp(row.get("evaluation_id")) == wanted:
                return EvaluationResult.from_dict(dict(row))
        return None

    def for_trajectory(self, trajectory_id: str) -> EvaluationResult | None:
        wanted = str(trajectory_id or "")
        for row in reversed(self._store.read()):
            if _timestamp(row.get("trajectory_id")) == wanted:
                return EvaluationResult.from_dict(dict(row))
        return None

    def list(
        self,
        *,
        trajectory_id: str = "",
        task_id: str = "",
        status: str = "",
        dimension: str = "",
        min_score: float | None = None,
        since: str = "",
        until: str = "",
        limit: int = 0,
        newest_first: bool = True,
    ) -> tuple[EvaluationResult, ...]:
        found: list[EvaluationResult] = []
        for result in self._parsed_rows():
            if trajectory_id and result.trajectory_id != trajectory_id:
                continue
            if task_id and result.task_id != task_id:
                continue
            if status and result.overall_status.lower() != status.lower():
                continue
            if since and result.timestamp < since:
                continue
            if until and result.timestamp > until:
                continue
            if dimension and min_score is not None:
                value = result.score(dimension)
                if value is None or value < min_score:
                    continue
            found.append(result)
        if newest_first:
            found.reverse()
        if limit and limit > 0:
            return tuple(found[:limit])
        return tuple(found)

    def _parsed_rows(self) -> tuple[EvaluationResult, ...]:
        parsed: list[EvaluationResult] = []
        for row in self._store.read():
            try:
                parsed.append(EvaluationResult.from_dict(dict(row)))
            except (TypeError, ValueError):
                continue
        return tuple(parsed)


class RewardRepository(JsonlRepository):
    """Reward results, keyed by ``reward_id`` (one per trajectory)."""

    id_field = "reward_id"

    def save(self, result: RewardResult) -> RewardResult:
        self.save_row(json_safe(result.to_dict()))
        return result

    def get(self, reward_id: str) -> RewardResult | None:
        wanted = str(reward_id or "")
        for row in reversed(self._store.read()):
            if _timestamp(row.get("reward_id")) == wanted:
                return RewardResult.from_dict(dict(row))
        return None

    def for_trajectory(self, trajectory_id: str) -> RewardResult | None:
        wanted = str(trajectory_id or "")
        for row in reversed(self._store.read()):
            if _timestamp(row.get("trajectory_id")) == wanted:
                return RewardResult.from_dict(dict(row))
        return None

    def list(
        self,
        *,
        trajectory_id: str = "",
        min_total: float | None = None,
        max_total: float | None = None,
        since: str = "",
        until: str = "",
        limit: int = 0,
        newest_first: bool = True,
    ) -> tuple[RewardResult, ...]:
        found: list[RewardResult] = []
        for row in self._store.read():
            try:
                result = RewardResult.from_dict(dict(row))
            except (TypeError, ValueError):
                continue
            if trajectory_id and result.trajectory_id != trajectory_id:
                continue
            if min_total is not None and result.total_reward < min_total:
                continue
            if max_total is not None and result.total_reward > max_total:
                continue
            if since and result.timestamp < since:
                continue
            if until and result.timestamp > until:
                continue
            found.append(result)
        if newest_first:
            found.reverse()
        if limit and limit > 0:
            return tuple(found[:limit])
        return tuple(found)


class DatasetRepository(JsonlRepository):
    """Golden dataset versions.

    Every version is kept, so a measurement taken against ``1.0.0`` still makes
    sense after ``1.1.0`` exists. ``latest`` answers with the highest version
    string, which is why dataset versions are dotted numbers rather than dates
    with a human in the middle of them.

    A row's identity is ``dataset_id@version``: re-saving a version replaces
    THAT version, while a new version is a new row — which is what "versions
    accumulate" has to mean for a stored measurement to still resolve.
    """

    id_field = "dataset_version_id"

    @staticmethod
    def version_key(dataset_id: str, version: str) -> str:
        return f"{dataset_id}@{version}"

    def save(self, dataset: GoldenDataset) -> GoldenDataset:
        row = json_safe(dataset.to_dict())
        row["version"] = dataset.version
        row[self.id_field] = self.version_key(dataset.dataset_id, dataset.version)
        self.save_row(row)
        return dataset

    def versions(self, dataset_id: str, *, limit: int = 0) -> tuple[GoldenDataset, ...]:
        wanted = str(dataset_id or "")
        parsed: list[GoldenDataset] = []
        for row in self._store.read():
            if _timestamp(row.get("dataset_id")) != wanted:
                continue
            try:
                parsed.append(GoldenDataset.from_dict(dict(row)))
            except (TypeError, ValueError):
                continue
        parsed.sort(key=lambda dataset: dataset.version)
        if limit and limit > 0:
            return tuple(parsed[-limit:])
        return tuple(parsed)

    def latest(self, dataset_id: str) -> GoldenDataset | None:
        versions = self.versions(dataset_id)
        return versions[-1] if versions else None

    def list(self, *, limit: int = 0) -> tuple[GoldenDataset, ...]:
        latest: dict[str, GoldenDataset] = {}
        for row in self._store.read():
            try:
                dataset = GoldenDataset.from_dict(dict(row))
            except (TypeError, ValueError):
                continue
            current = latest.get(dataset.dataset_id)
            if current is None or dataset.version > current.version:
                latest[dataset.dataset_id] = dataset
        ordered = sorted(latest.values(), key=lambda dataset: dataset.dataset_id)
        if limit and limit > 0:
            return tuple(ordered[:limit])
        return tuple(ordered)


def _mentions_model(trajectory: AgentTrajectory, wanted: str) -> bool:
    """Whether any model field on the trajectory names (or contains) ``wanted``."""
    for key, value in trajectory.model_information.items():
        text = str(value).lower()
        if wanted in text or wanted in str(key).lower():
            return True
    return False


def build_repositories(
    data_dir: str | Path | None,
    *,
    cap: int = DEFAULT_REPOSITORY_CAP,
) -> tuple[TrajectoryRepository, EvaluationRepository, RewardRepository, DatasetRepository]:
    """The four repositories for a data directory (in-memory when it is None)."""
    if data_dir is None:
        return (
            TrajectoryRepository(InMemoryRecordStore(), cap=cap),
            EvaluationRepository(InMemoryRecordStore(), cap=cap),
            RewardRepository(InMemoryRecordStore(), cap=cap),
            DatasetRepository(InMemoryRecordStore(), cap=0),
        )
    root = Path(data_dir) / EVALUATION_DIRECTORY
    return (
        TrajectoryRepository(JsonlRecordStore(root / TRAJECTORY_FILENAME), cap=cap),
        EvaluationRepository(JsonlRecordStore(root / EVALUATION_FILENAME), cap=cap),
        RewardRepository(JsonlRecordStore(root / REWARD_FILENAME), cap=cap),
        DatasetRepository(JsonlRecordStore(root / DATASET_FILENAME), cap=0),
    )


__all__ = [
    "DATASET_FILENAME",
    "DEFAULT_REPOSITORY_CAP",
    "EVALUATION_DIRECTORY",
    "EVALUATION_FILENAME",
    "REWARD_FILENAME",
    "TRAJECTORY_FILENAME",
    "DatasetRepository",
    "EvaluationRepository",
    "InMemoryRecordStore",
    "JsonlRecordStore",
    "JsonlRepository",
    "RecordStore",
    "RewardRepository",
    "TrajectoryRepository",
    "build_repositories",
]
