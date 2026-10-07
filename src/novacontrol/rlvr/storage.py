"""Storage for Phase 19's three kinds of row, in the directory training uses.

No new persistence architecture: a row is a line of JSON, upserted by id, read
through Phase 16's :class:`~novacontrol.training.storage.ParsedRepository`.
Three files under ``<data>/training/``:

  * ``critiques.jsonl`` — structured, evidence-based critiques.
  * ``corrections.jsonl`` — corrected examples with their quality verdict.
  * ``critique_datasets.jsonl`` — immutable built versions, keyed ``name@version``.

The rules carry over because they are load-bearing: a critique is history (it is
never silently rewritten by a later filter), a correction keeps the verdict it
was given (accept/hold/reject), and a dataset version is immutable — saving the
same ``name@version`` with different rows is refused, because a run stores that
string as the only reference to what it learned from.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from novacontrol.audit.models import json_safe
from novacontrol.evaluation.storage import (
    DEFAULT_REPOSITORY_CAP,
    InMemoryRecordStore,
    JsonlRecordStore,
    RecordStore,
)
from novacontrol.rlvr.models import (
    CorrectedExample,
    CritiqueDatasetVersion,
    CritiqueResult,
)
from novacontrol.training.storage import TRAINING_DIRECTORY, ParsedRepository

#: Inside ``<data>/training/``: one file per kind of Phase 19 row.
CRITIQUE_FILENAME = "critiques.jsonl"
CORRECTION_FILENAME = "corrections.jsonl"
CRITIQUE_DATASET_FILENAME = "critique_datasets.jsonl"


def _text(value: Any) -> str:
    return "" if value is None else str(value)


class CritiqueRepository(ParsedRepository):
    """Structured critiques, upserted by ``critique_id``, never rewritten."""

    id_field = "critique_id"
    parser = staticmethod(CritiqueResult.from_dict)

    def save(self, critique: CritiqueResult) -> CritiqueResult:
        self.save_row(json_safe(critique.to_dict()))
        return critique

    def save_many(self, rows: Any) -> tuple[CritiqueResult, ...]:
        return tuple(self.save(row) for row in rows)

    def get(self, critique_id: str) -> CritiqueResult | None:
        wanted = _text(critique_id)
        for row in reversed(self._store.read()):
            if _text(row.get(self.id_field)) == wanted:
                try:
                    return CritiqueResult.from_dict(dict(row))
                except (TypeError, ValueError):
                    return None
        return None

    def list(
        self,
        *,
        trajectory_id: str = "",
        category: str = "",
        severity: str = "",
        source: str = "",
        limit: int = 0,
        newest_first: bool = False,
    ) -> tuple[CritiqueResult, ...]:
        wanted_trajectory = _text(trajectory_id)
        wanted_category = _text(category).lower()
        wanted_severity = _text(severity).lower()
        wanted_source = _text(source).lower()
        found = [
            row
            for row in self._parsed_rows()
            if (not wanted_trajectory or row.trajectory_id == wanted_trajectory)
            and (not wanted_category or row.category.lower() == wanted_category)
            and (not wanted_severity or row.severity.lower() == wanted_severity)
            and (not wanted_source or row.source.lower() == wanted_source)
        ]
        found.sort(key=lambda row: row.timestamp)
        if newest_first:
            found.reverse()
        if limit and limit > 0:
            return tuple(found[:limit])
        return tuple(found)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self._parsed_rows():
            key = row.category or "unknown"
            counts[key] = counts.get(key, 0) + 1
        return counts


class CorrectionRepository(ParsedRepository):
    """Corrected examples, upserted by ``example_id``, with their verdicts."""

    id_field = "example_id"
    parser = staticmethod(CorrectedExample.from_dict)

    def save(self, correction: CorrectedExample) -> CorrectedExample:
        self.save_row(json_safe(correction.to_dict()))
        return correction

    def save_many(self, rows: Any) -> tuple[CorrectedExample, ...]:
        return tuple(self.save(row) for row in rows)

    def get(self, example_id: str) -> CorrectedExample | None:
        wanted = _text(example_id)
        for row in reversed(self._store.read()):
            if _text(row.get(self.id_field)) == wanted:
                try:
                    return CorrectedExample.from_dict(dict(row))
                except (TypeError, ValueError):
                    return None
        return None

    def list(
        self,
        *,
        status: str = "",
        trajectory_id: str = "",
        limit: int = 0,
        newest_first: bool = False,
    ) -> tuple[CorrectedExample, ...]:
        wanted_status = _text(status).lower()
        wanted_trajectory = _text(trajectory_id)
        found = [
            row
            for row in self._parsed_rows()
            if (not wanted_status or row.quality_status.lower() == wanted_status)
            and (
                not wanted_trajectory
                or row.source_trajectory_id == wanted_trajectory
            )
        ]
        found.sort(key=lambda row: row.created_at)
        if newest_first:
            found.reverse()
        if limit and limit > 0:
            return tuple(found[:limit])
        return tuple(found)

    def pending(self, limit: int = 0) -> tuple[CorrectedExample, ...]:
        """Corrections waiting on a person: held, because nothing settled them."""
        found = [
            row
            for row in self._parsed_rows()
            if row.quality_status == "needs_review"
        ]
        found.sort(key=lambda row: row.created_at)
        if limit and limit > 0:
            return tuple(found[:limit])
        return tuple(found)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self._parsed_rows():
            key = row.quality_status or "unknown"
            counts[key] = counts.get(key, 0) + 1
        return counts


class CritiqueDatasetRepository(ParsedRepository):
    """Immutable critique dataset versions, keyed by ``name@version``."""

    id_field = "dataset_version_id"
    parser = staticmethod(CritiqueDatasetVersion.from_dict)

    def save(self, dataset: CritiqueDatasetVersion) -> CritiqueDatasetVersion:
        row = json_safe(dataset.to_dict())
        existing = self.get(dataset.dataset_version_id)
        if existing is not None and existing.fingerprint() != dataset.fingerprint():
            raise ValueError(
                f"critique dataset version {dataset.dataset_version_id!r} already "
                "exists with different rows: a dataset version is immutable, "
                "publish a new version"
            )
        self.save_row({str(key): value for key, value in row.items()})
        return dataset

    def get(self, dataset_version_id: str) -> CritiqueDatasetVersion | None:
        wanted = _text(dataset_version_id)
        for row in reversed(self._store.read()):
            if _text(row.get(self.id_field)) == wanted:
                try:
                    return CritiqueDatasetVersion.from_dict(dict(row))
                except (TypeError, ValueError):
                    return None
        return None

    def versions(self, name: str) -> tuple[CritiqueDatasetVersion, ...]:
        wanted = _text(name)
        found = [item for item in self._parsed_rows() if item.name == wanted]
        found.sort(key=lambda item: item.created_at)
        return tuple(found)

    def latest(self, name: str) -> CritiqueDatasetVersion | None:
        versions = self.versions(name)
        return versions[-1] if versions else None

    def list(
        self, *, name: str = "", limit: int = 0, newest_first: bool = True
    ) -> tuple[CritiqueDatasetVersion, ...]:
        wanted_name = _text(name)
        found = [
            item
            for item in self._parsed_rows()
            if not wanted_name or item.name == wanted_name
        ]
        found.sort(key=lambda item: item.created_at)
        if newest_first:
            found.reverse()
        if limit and limit > 0:
            return tuple(found[:limit])
        return tuple(found)


@dataclass(frozen=True, slots=True)
class RLVRRepositories:
    """The three Phase 19 repositories, built for one data directory."""

    critiques: CritiqueRepository
    corrections: CorrectionRepository
    datasets: CritiqueDatasetRepository


def build_rlvr_repositories(
    data_dir: str | Path | None,
    *,
    cap: int = DEFAULT_REPOSITORY_CAP,
) -> RLVRRepositories:
    """The three repositories for a data directory (in-memory when ``None``)."""

    def store(filename: str) -> RecordStore:
        if data_dir is None:
            return InMemoryRecordStore()
        return JsonlRecordStore(Path(data_dir) / TRAINING_DIRECTORY / filename)

    return RLVRRepositories(
        critiques=CritiqueRepository(store(CRITIQUE_FILENAME), cap=cap),
        corrections=CorrectionRepository(store(CORRECTION_FILENAME), cap=cap),
        datasets=CritiqueDatasetRepository(store(CRITIQUE_DATASET_FILENAME), cap=cap),
    )


__all__ = [
    "CORRECTION_FILENAME",
    "CRITIQUE_DATASET_FILENAME",
    "CRITIQUE_FILENAME",
    "CorrectionRepository",
    "CritiqueDatasetRepository",
    "CritiqueRepository",
    "RLVRRepositories",
    "build_rlvr_repositories",
]
