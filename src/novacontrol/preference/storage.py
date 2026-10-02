"""Storage for preference datasets and the review queue.

Two more files in the directory the training layer already writes to — one for
immutable preference dataset versions, one for pairs waiting on a person — read
and written through the same row format and the same repository base as Phase 16
(:class:`~novacontrol.training.storage.ParsedRepository`). There is no second
persistence architecture here: a row is still a line of JSON, upserted by id,
written atomically.

The one rule that carries over unchanged: A DATASET VERSION IS IMMUTABLE. A run
stores ``name@version`` as the only reference to the pairs it learned from, so
saving that string again with different content is refused rather than allowed to
silently change what a finished run meant.

The review rows are the exception to "rows are history": a reviewer's decision
REPLACES the queued row, because a pair waiting for review and the same pair
after the review are one object with one id — keeping both would leave the queue
showing a decision that was already made.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from novacontrol.audit.models import json_safe
from novacontrol.evaluation.storage import (
    DEFAULT_REPOSITORY_CAP,
    InMemoryRecordStore,
    JsonlRecordStore,
    RecordStore,
)
from novacontrol.preference.models import (
    PreferenceDatasetVersion,
    PreferenceExample,
    PreferenceQualityStatus,
)
from novacontrol.training.storage import TRAINING_DIRECTORY, ParsedRepository

#: Inside ``<data>/training/``: one file per kind of preference row.
PREFERENCE_DATASET_FILENAME = "preference_datasets.jsonl"
PREFERENCE_REVIEW_FILENAME = "preference_reviews.jsonl"


def _text(value: Any) -> str:
    return "" if value is None else str(value)


class PreferenceDatasetRepository(ParsedRepository):
    """Immutable preference dataset versions, keyed by ``name@version``."""

    id_field = "dataset_version_id"
    parser = staticmethod(PreferenceDatasetVersion.from_dict)

    def save(self, dataset: PreferenceDatasetVersion) -> PreferenceDatasetVersion:
        row = json_safe(dataset.to_dict())
        existing = self.get(dataset.dataset_version_id)
        if existing is not None and existing.fingerprint() != dataset.fingerprint():
            raise ValueError(
                f"preference dataset version {dataset.dataset_version_id!r} already "
                "exists with different pairs: a dataset version is immutable, "
                "publish a new version"
            )
        self.save_row({str(key): value for key, value in row.items()})
        return dataset

    def get(self, dataset_version_id: str) -> PreferenceDatasetVersion | None:
        wanted = _text(dataset_version_id)
        for row in reversed(self._store.read()):
            if _text(row.get(self.id_field)) == wanted:
                try:
                    return PreferenceDatasetVersion.from_dict(dict(row))
                except (TypeError, ValueError):
                    return None
        return None

    def versions(self, name: str) -> tuple[PreferenceDatasetVersion, ...]:
        wanted = _text(name)
        found = [dataset for dataset in self._parsed_rows() if dataset.name == wanted]
        found.sort(key=lambda dataset: dataset.created_at)
        return tuple(found)

    def latest(self, name: str) -> PreferenceDatasetVersion | None:
        versions = self.versions(name)
        return versions[-1] if versions else None

    def list(
        self,
        *,
        dataset_type: str = "",
        name: str = "",
        limit: int = 0,
        newest_first: bool = True,
    ) -> tuple[PreferenceDatasetVersion, ...]:
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


class PreferenceReviewRepository(ParsedRepository):
    """The pairs a person has to decide about, upserted by ``preference_id``.

    A pair enters here as ``needs_review`` and leaves as ``accepted`` or
    ``rejected``: the same row, rewritten, plus the decision that settled it.
    ``remove`` exists for a pair a reviewer discarded entirely.
    """

    id_field = "preference_id"
    parser = staticmethod(PreferenceExample.from_dict)

    def save(self, pair: PreferenceExample) -> PreferenceExample:
        self.save_row(json_safe(pair.to_dict()))
        return pair

    def get(self, preference_id: str) -> PreferenceExample | None:
        wanted = _text(preference_id)
        for row in reversed(self._store.read()):
            if _text(row.get(self.id_field)) == wanted:
                try:
                    return PreferenceExample.from_dict(dict(row))
                except (TypeError, ValueError):
                    return None
        return None

    def remove(self, preference_id: str) -> int:
        wanted = _text(preference_id)
        rows = list(self._store.read())
        kept = [row for row in rows if _text(row.get(self.id_field)) != wanted]
        if len(kept) != len(rows):
            self._store.replace(kept)
        return len(rows) - len(kept)

    def list(
        self,
        *,
        status: str = "",
        decision: str = "",
        pending_only: bool = False,
        limit: int = 0,
        newest_first: bool = False,
    ) -> tuple[PreferenceExample, ...]:
        wanted_status = _text(status).lower()
        wanted_decision = _text(decision).lower()
        found = [
            pair
            for pair in self._parsed_rows()
            if (not wanted_status or pair.quality_status.lower() == wanted_status)
            and (
                not wanted_decision
                or _text(pair.review.get("decision")).lower() == wanted_decision
            )
            and (not pending_only or not pair.reviewed)
        ]
        found.sort(key=lambda pair: pair.created_at)
        if newest_first:
            found.reverse()
        if limit and limit > 0:
            return tuple(found[:limit])
        return tuple(found)

    def pending(self, limit: int = 0) -> tuple[PreferenceExample, ...]:
        """The pairs a person has not decided about yet (oldest first)."""
        return self.list(pending_only=True, limit=limit)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for pair in self._parsed_rows():
            key = pair.quality_status or "unknown"
            counts[key] = counts.get(key, 0) + 1
        counts["pending"] = sum(
            1
            for pair in self._parsed_rows()
            if pair.quality_status == PreferenceQualityStatus.NEEDS_REVIEW.value
            and not pair.reviewed
        )
        return counts


def build_preference_repositories(
    data_dir: str | Path | None,
    *,
    cap: int = DEFAULT_REPOSITORY_CAP,
) -> tuple[PreferenceDatasetRepository, PreferenceReviewRepository]:
    """The two preference repositories for a data directory (in-memory if None)."""
    if data_dir is None:
        return (
            PreferenceDatasetRepository(InMemoryRecordStore(), cap=cap),
            PreferenceReviewRepository(InMemoryRecordStore(), cap=cap),
        )
    root = Path(data_dir) / TRAINING_DIRECTORY

    def store(filename: str) -> RecordStore:
        return JsonlRecordStore(root / filename)

    return (
        PreferenceDatasetRepository(store(PREFERENCE_DATASET_FILENAME), cap=cap),
        PreferenceReviewRepository(store(PREFERENCE_REVIEW_FILENAME), cap=cap),
    )


__all__ = [
    "PREFERENCE_DATASET_FILENAME",
    "PREFERENCE_REVIEW_FILENAME",
    "PreferenceDatasetRepository",
    "PreferenceReviewRepository",
    "build_preference_repositories",
]
