"""Storage for training datasets, runs, checkpoints, models and evaluations.

This extends the persistence layer the rest of NovaControl already uses rather
than adding a second one: a row is a line of JSON in a file inside the
application's data directory, written through a temporary file and an atomic
replace, with the same upsert-by-id semantics as the evaluation store. No
database, no server, no migration — the deployment target is a Windows desktop.

One rule is stricter here than elsewhere: a DATASET VERSION IS IMMUTABLE. Saving
an existing ``name@version`` with different content is refused rather than
allowed to overwrite it, because a training run stores that string as the only
reference to the data it learned from. Every other repository upserts by id, so
a run that is paused and resumed replaces its own row instead of competing with
it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from novacontrol.audit.models import json_safe
from novacontrol.evaluation.storage import (
    DEFAULT_REPOSITORY_CAP,
    InMemoryRecordStore,
    JsonlRecordStore,
    JsonlRepository,
    RecordStore,
)
from novacontrol.training.models import (
    CheckpointRecord,
    SFTDatasetVersion,
    TrainingEvaluation,
    TrainingModelRecord,
    TrainingRun,
)

#: Where the training layer keeps its rows inside an application's data
#: directory. One folder, five files, no cross-references to resolve.
TRAINING_DIRECTORY = "training"
DATASET_FILENAME = "datasets.jsonl"
RUN_FILENAME = "runs.jsonl"
CHECKPOINT_FILENAME = "checkpoints.jsonl"
MODEL_FILENAME = "models.jsonl"
EVALUATION_FILENAME = "training_evaluations.jsonl"


def _text(value: Any) -> str:
    return "" if value is None else str(value)


class ParsedRepository(JsonlRepository):
    """A repository whose rows are read back through one parser.

    Public rather than module-private because Phase 17's preference store is a
    sibling of this one: the row format, the durability rules and the
    upsert-by-id semantics are the same idea, and a second copy of this twenty
    lines would be a second place for them to drift.
    """

    parser: Any = None

    def _parsed_rows(self) -> list[Any]:
        rows: list[Any] = []
        for row in self._store.read():
            try:
                rows.append(self.parser(dict(row)))
            except (TypeError, ValueError):
                continue
        return rows


class DatasetVersionRepository(ParsedRepository):
    """Immutable, versioned SFT datasets, keyed by ``name@version``."""

    id_field = "dataset_version_id"
    parser = staticmethod(SFTDatasetVersion.from_dict)

    def save(self, dataset: SFTDatasetVersion) -> SFTDatasetVersion:
        row = json_safe(dataset.to_dict())
        existing = self.get(dataset.dataset_version_id)
        if existing is not None and existing.fingerprint() != dataset.fingerprint():
            raise ValueError(
                f"dataset version {dataset.dataset_version_id!r} already exists with "
                "different content: a dataset version is immutable, publish a new version"
            )
        self.save_row({str(key): value for key, value in row.items()})
        return dataset

    def get(self, dataset_version_id: str) -> SFTDatasetVersion | None:
        wanted = _text(dataset_version_id)
        for row in reversed(self._store.read()):
            if _text(row.get(self.id_field)) == wanted:
                try:
                    return SFTDatasetVersion.from_dict(dict(row))
                except (TypeError, ValueError):
                    return None
        return None

    def versions(self, name: str) -> tuple[SFTDatasetVersion, ...]:
        wanted = _text(name)
        found = [
            dataset
            for dataset in self._parsed_rows()
            if dataset.name == wanted
        ]
        found.sort(key=lambda dataset: dataset.created_at)
        return tuple(found)

    def latest(self, name: str) -> SFTDatasetVersion | None:
        versions = self.versions(name)
        return versions[-1] if versions else None

    def list(
        self,
        *,
        dataset_type: str = "",
        name: str = "",
        limit: int = 0,
        newest_first: bool = True,
    ) -> tuple[SFTDatasetVersion, ...]:
        wanted_type = _text(dataset_type).lower()
        wanted_name = _text(name)
        found = [
            dataset
            for dataset in self._parsed_rows()
            if (not wanted_type or dataset.dataset_type.lower() == wanted_type)
            and (not wanted_name or dataset.name == wanted_name)
        ]
        found.sort(key=lambda dataset: dataset.created_at)
        if newest_first:
            found.reverse()
        if limit and limit > 0:
            return tuple(found[:limit])
        return tuple(found)


class TrainingRunRepository(ParsedRepository):
    """Training runs, upserted by id as their status advances."""

    id_field = "run_id"
    parser = staticmethod(TrainingRun.from_dict)

    def save(self, run: TrainingRun) -> TrainingRun:
        self.save_row(json_safe(run.to_dict()))
        return run

    def get(self, run_id: str) -> TrainingRun | None:
        wanted = _text(run_id)
        for row in reversed(self._store.read()):
            if _text(row.get(self.id_field)) == wanted:
                try:
                    return TrainingRun.from_dict(dict(row))
                except (TypeError, ValueError):
                    return None
        return None

    def list(
        self,
        *,
        status: str = "",
        model: str = "",
        dataset_version: str = "",
        limit: int = 0,
        newest_first: bool = True,
    ) -> tuple[TrainingRun, ...]:
        wanted_status = _text(status).lower()
        found = [
            run
            for run in self._parsed_rows()
            if (not wanted_status or run.status.lower() == wanted_status)
            and (not model or run.model == model)
            and (not dataset_version or run.dataset_version == dataset_version)
        ]
        found.sort(key=lambda run: run.created_at)
        if newest_first:
            found.reverse()
        if limit and limit > 0:
            return tuple(found[:limit])
        return tuple(found)


class CheckpointRepository(ParsedRepository):
    """Checkpoint records, one row per checkpoint (the file is separate)."""

    id_field = "checkpoint_id"
    parser = staticmethod(CheckpointRecord.from_dict)

    def save(self, record: CheckpointRecord) -> CheckpointRecord:
        self.save_row(json_safe(record.to_dict()))
        return record

    def get(self, checkpoint_id: str) -> CheckpointRecord | None:
        wanted = _text(checkpoint_id)
        for row in reversed(self._store.read()):
            if _text(row.get(self.id_field)) == wanted:
                try:
                    return CheckpointRecord.from_dict(dict(row))
                except (TypeError, ValueError):
                    return None
        return None

    def remove(self, checkpoint_id: str) -> int:
        """Drop one checkpoint record (retention deleted its file)."""
        wanted = _text(checkpoint_id)
        rows = list(self._store.read())
        kept = [row for row in rows if _text(row.get(self.id_field)) != wanted]
        if len(kept) != len(rows):
            self._store.replace(kept)
        return len(rows) - len(kept)

    def list(
        self, *, run_id: str = "", kind: str = "", limit: int = 0
    ) -> tuple[CheckpointRecord, ...]:
        found = [
            record
            for record in self._parsed_rows()
            if (not run_id or record.run_id == run_id) and (not kind or record.kind == kind)
        ]
        found.sort(key=lambda record: (record.epoch, record.step, record.created_at))
        if limit and limit > 0:
            return tuple(found[-limit:])
        return tuple(found)


class TrainingModelRepository(ParsedRepository):
    """Trained-model records, upserted as their registry status advances."""

    id_field = "model_id"
    parser = staticmethod(TrainingModelRecord.from_dict)

    def save(self, record: TrainingModelRecord) -> TrainingModelRecord:
        self.save_row(json_safe(record.to_dict()))
        return record

    def get(self, model_id: str) -> TrainingModelRecord | None:
        wanted = _text(model_id)
        for row in reversed(self._store.read()):
            if _text(row.get(self.id_field)) == wanted:
                try:
                    return TrainingModelRecord.from_dict(dict(row))
                except (TypeError, ValueError):
                    return None
        return None

    def list(
        self,
        *,
        status: str = "",
        base_model: str = "",
        training_run_id: str = "",
        limit: int = 0,
        newest_first: bool = True,
    ) -> tuple[TrainingModelRecord, ...]:
        wanted_status = _text(status).lower()
        found = [
            record
            for record in self._parsed_rows()
            if (not wanted_status or record.status.lower() == wanted_status)
            and (not base_model or record.base_model == base_model)
            and (not training_run_id or record.training_run_id == training_run_id)
        ]
        found.sort(key=lambda record: record.created_at)
        if newest_first:
            found.reverse()
        if limit and limit > 0:
            return tuple(found[:limit])
        return tuple(found)


class TrainingEvaluationRepository(ParsedRepository):
    """Base-versus-candidate comparisons, one per run and dataset."""

    id_field = "evaluation_id"
    parser = staticmethod(TrainingEvaluation.from_dict)

    def save(self, evaluation: TrainingEvaluation) -> TrainingEvaluation:
        self.save_row(json_safe(evaluation.to_dict()))
        return evaluation

    def get(self, evaluation_id: str) -> TrainingEvaluation | None:
        wanted = _text(evaluation_id)
        for row in reversed(self._store.read()):
            if _text(row.get(self.id_field)) == wanted:
                try:
                    return TrainingEvaluation.from_dict(dict(row))
                except (TypeError, ValueError):
                    return None
        return None

    def for_run(self, run_id: str) -> TrainingEvaluation | None:
        wanted = _text(run_id)
        for row in reversed(self._store.read()):
            if _text(row.get("run_id")) == wanted:
                try:
                    return TrainingEvaluation.from_dict(dict(row))
                except (TypeError, ValueError):
                    return None
        return None

    def list(self, *, limit: int = 0, newest_first: bool = True) -> tuple[TrainingEvaluation, ...]:
        found = list(self._parsed_rows())
        found.sort(key=lambda evaluation: evaluation.created_at)
        if newest_first:
            found.reverse()
        if limit and limit > 0:
            return tuple(found[:limit])
        return tuple(found)


def build_training_repositories(
    data_dir: str | Path | None,
    *,
    cap: int = DEFAULT_REPOSITORY_CAP,
) -> tuple[
    DatasetVersionRepository,
    TrainingRunRepository,
    CheckpointRepository,
    TrainingModelRepository,
    TrainingEvaluationRepository,
]:
    """The five repositories for a data directory (in-memory when it is None)."""
    if data_dir is None:
        return (
            DatasetVersionRepository(InMemoryRecordStore(), cap=cap),
            TrainingRunRepository(InMemoryRecordStore(), cap=cap),
            CheckpointRepository(InMemoryRecordStore(), cap=cap),
            TrainingModelRepository(InMemoryRecordStore(), cap=cap),
            TrainingEvaluationRepository(InMemoryRecordStore(), cap=cap),
        )
    root = Path(data_dir) / TRAINING_DIRECTORY

    def store(filename: str) -> RecordStore:
        return JsonlRecordStore(root / filename)

    return (
        DatasetVersionRepository(store(DATASET_FILENAME), cap=cap),
        TrainingRunRepository(store(RUN_FILENAME), cap=cap),
        CheckpointRepository(store(CHECKPOINT_FILENAME), cap=cap),
        TrainingModelRepository(store(MODEL_FILENAME), cap=cap),
        TrainingEvaluationRepository(store(EVALUATION_FILENAME), cap=cap),
    )


__all__ = [
    "CHECKPOINT_FILENAME",
    "DATASET_FILENAME",
    "EVALUATION_FILENAME",
    "MODEL_FILENAME",
    "RUN_FILENAME",
    "TRAINING_DIRECTORY",
    "CheckpointRepository",
    "ParsedRepository",
    "DatasetVersionRepository",
    "TrainingEvaluationRepository",
    "TrainingModelRepository",
    "TrainingRunRepository",
    "build_training_repositories",
]
