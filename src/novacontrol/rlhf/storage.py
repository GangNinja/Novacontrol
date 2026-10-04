"""Storage for Phase 18's four kinds of row, in the directory training already uses.

No new persistence architecture: a row is a line of JSON, upserted by id, in a
file under ``<data>/training/``, read and written through Phase 16's
:class:`~novacontrol.training.storage.ParsedRepository`. Four files:

  * ``human_feedback.jsonl`` — what people said, including what a filter rejected.
  * ``ai_ratings.jsonl`` — what evaluators said, with their criterion scores.
  * ``reward_datasets.jsonl`` — immutable built versions, keyed ``name@version``.
  * ``feedback_disagreements.jsonl`` — where two readings contradicted each other.

Two rules carry over from earlier phases because they are load-bearing:

  * A DATASET VERSION IS IMMUTABLE. A run stores ``name@version``; saving that
    string again with different content would silently change what a finished
    run meant, so it is refused.
  * FEEDBACK AND RATINGS ARE HISTORY. A review decision annotates a row; it does
    not create a second one, and nothing is deleted by the filter.
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
from novacontrol.rlhf.models import (
    AIRating,
    Disagreement,
    HumanFeedback,
    RewardDatasetVersion,
)
from novacontrol.training.storage import TRAINING_DIRECTORY, ParsedRepository

#: Inside ``<data>/training/``: one file per kind of Phase 18 row.
FEEDBACK_FILENAME = "human_feedback.jsonl"
RATINGS_FILENAME = "ai_ratings.jsonl"
REWARD_DATASET_FILENAME = "reward_datasets.jsonl"
DISAGREEMENT_FILENAME = "feedback_disagreements.jsonl"


def _text(value: Any) -> str:
    return "" if value is None else str(value)


class FeedbackRepository(ParsedRepository):
    """Human feedback rows, upserted by ``feedback_id``, never dropped by a filter."""

    id_field = "feedback_id"
    parser = staticmethod(HumanFeedback.from_dict)

    def save(self, feedback: HumanFeedback) -> HumanFeedback:
        self.save_row(json_safe(feedback.to_dict()))
        return feedback

    def get(self, feedback_id: str) -> HumanFeedback | None:
        wanted = _text(feedback_id)
        for row in reversed(self._store.read()):
            if _text(row.get(self.id_field)) == wanted:
                try:
                    return HumanFeedback.from_dict(dict(row))
                except (TypeError, ValueError):
                    return None
        return None

    def remove(self, feedback_id: str) -> int:
        wanted = _text(feedback_id)
        rows = list(self._store.read())
        kept = [row for row in rows if _text(row.get(self.id_field)) != wanted]
        if len(kept) != len(rows):
            self._store.replace(kept)
        return len(rows) - len(kept)

    def list(
        self,
        *,
        status: str = "",
        feedback_type: str = "",
        trajectory_id: str = "",
        pending_only: bool = False,
        limit: int = 0,
        newest_first: bool = False,
    ) -> tuple[HumanFeedback, ...]:
        wanted_status = _text(status).lower()
        wanted_type = _text(feedback_type).lower()
        wanted_trajectory = _text(trajectory_id)
        found = [
            row
            for row in self._parsed_rows()
            if (not wanted_status or row.status.lower() == wanted_status)
            and (not wanted_type or row.feedback_type.lower() == wanted_type)
            and (not wanted_trajectory or row.trajectory_id == wanted_trajectory)
            and (not pending_only or not row.reviewed)
        ]
        found.sort(key=lambda row: row.timestamp)
        if newest_first:
            found.reverse()
        if limit and limit > 0:
            return tuple(found[:limit])
        return tuple(found)

    def pending(self, limit: int = 0) -> tuple[HumanFeedback, ...]:
        """Rows a person has not decided about yet (oldest first)."""
        found = [
            row
            for row in self._parsed_rows()
            if not row.reviewed
            and row.status
            in {"needs_review", ""}
            and row.feedback_type
        ]
        found.sort(key=lambda row: row.timestamp)
        if limit and limit > 0:
            return tuple(found[:limit])
        return tuple(found)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self._parsed_rows():
            key = row.status or "unfiltered"
            counts[key] = counts.get(key, 0) + 1
        return counts


class RatingRepository(ParsedRepository):
    """AI ratings, upserted by ``rating_id``, with their evaluator identity."""

    id_field = "rating_id"
    parser = staticmethod(AIRating.from_dict)

    def save(self, rating: AIRating) -> AIRating:
        self.save_row(json_safe(rating.to_dict()))
        return rating

    def get(self, rating_id: str) -> AIRating | None:
        wanted = _text(rating_id)
        for row in reversed(self._store.read()):
            if _text(row.get(self.id_field)) == wanted:
                try:
                    return AIRating.from_dict(dict(row))
                except (TypeError, ValueError):
                    return None
        return None

    def list(
        self,
        *,
        trajectory_id: str = "",
        evaluator_id: str = "",
        limit: int = 0,
        newest_first: bool = False,
    ) -> tuple[AIRating, ...]:
        wanted_trajectory = _text(trajectory_id)
        wanted_evaluator = _text(evaluator_id)
        found = [
            row
            for row in self._parsed_rows()
            if (not wanted_trajectory or row.trajectory_id == wanted_trajectory)
            and (not wanted_evaluator or row.evaluator_id == wanted_evaluator)
        ]
        found.sort(key=lambda row: row.created_at)
        if newest_first:
            found.reverse()
        if limit and limit > 0:
            return tuple(found[:limit])
        return tuple(found)

    def for_trajectory(self, trajectory_id: str) -> tuple[AIRating, ...]:
        return self.list(trajectory_id=trajectory_id)


class RewardDatasetRepository(ParsedRepository):
    """Immutable reward dataset versions, keyed by ``name@version``."""

    id_field = "dataset_version_id"
    parser = staticmethod(RewardDatasetVersion.from_dict)

    def save(self, dataset: RewardDatasetVersion) -> RewardDatasetVersion:
        row = json_safe(dataset.to_dict())
        existing = self.get(dataset.dataset_version_id)
        if existing is not None and existing.fingerprint() != dataset.fingerprint():
            raise ValueError(
                f"reward dataset version {dataset.dataset_version_id!r} already "
                "exists with different rows: a dataset version is immutable, "
                "publish a new version"
            )
        self.save_row({str(key): value for key, value in row.items()})
        return dataset

    def get(self, dataset_version_id: str) -> RewardDatasetVersion | None:
        wanted = _text(dataset_version_id)
        for row in reversed(self._store.read()):
            if _text(row.get(self.id_field)) == wanted:
                try:
                    return RewardDatasetVersion.from_dict(dict(row))
                except (TypeError, ValueError):
                    return None
        return None

    def versions(self, name: str) -> tuple[RewardDatasetVersion, ...]:
        wanted = _text(name)
        found = [dataset for dataset in self._parsed_rows() if dataset.name == wanted]
        found.sort(key=lambda dataset: dataset.created_at)
        return tuple(found)

    def latest(self, name: str) -> RewardDatasetVersion | None:
        versions = self.versions(name)
        return versions[-1] if versions else None

    def list(
        self,
        *,
        mode: str = "",
        name: str = "",
        limit: int = 0,
        newest_first: bool = True,
    ) -> tuple[RewardDatasetVersion, ...]:
        wanted_mode = _text(mode).lower()
        wanted_name = _text(name)
        found = [
            dataset
            for dataset in self._parsed_rows()
            if (not wanted_mode or dataset.mode.lower() == wanted_mode)
            and (not wanted_name or dataset.name == wanted_name)
        ]
        found.sort(key=lambda dataset: dataset.created_at)
        if newest_first:
            found.reverse()
        if limit and limit > 0:
            return tuple(found[:limit])
        return tuple(found)


class DisagreementRepository(ParsedRepository):
    """Recorded disagreements between sources, upserted by ``disagreement_id``."""

    id_field = "disagreement_id"
    parser = staticmethod(Disagreement.from_dict)

    def save(self, disagreement: Disagreement) -> Disagreement:
        self.save_row(json_safe(disagreement.to_dict()))
        return disagreement

    def save_many(self, rows: Any) -> tuple[Disagreement, ...]:
        return tuple(self.save(row) for row in rows)

    def get(self, disagreement_id: str) -> Disagreement | None:
        wanted = _text(disagreement_id)
        for row in reversed(self._store.read()):
            if _text(row.get(self.id_field)) == wanted:
                try:
                    return Disagreement.from_dict(dict(row))
                except (TypeError, ValueError):
                    return None
        return None

    def list(
        self,
        *,
        kind: str = "",
        subject_id: str = "",
        limit: int = 0,
        newest_first: bool = True,
    ) -> tuple[Disagreement, ...]:
        wanted_kind = _text(kind).lower()
        wanted_subject = _text(subject_id)
        found = [
            row
            for row in self._parsed_rows()
            if (not wanted_kind or row.kind.lower() == wanted_kind)
            and (not wanted_subject or row.subject_id == wanted_subject)
        ]
        found.sort(key=lambda row: row.detected_at)
        if newest_first:
            found.reverse()
        if limit and limit > 0:
            return tuple(found[:limit])
        return tuple(found)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self._parsed_rows():
            key = row.kind or "unknown"
            counts[key] = counts.get(key, 0) + 1
        return counts


@dataclass(frozen=True, slots=True)
class RLHFRepositories:
    """The four Phase 18 repositories, built for one data directory."""

    feedback: FeedbackRepository
    ratings: RatingRepository
    datasets: RewardDatasetRepository
    disagreements: DisagreementRepository


def build_rlhf_repositories(
    data_dir: str | Path | None,
    *,
    cap: int = DEFAULT_REPOSITORY_CAP,
) -> RLHFRepositories:
    """The four repositories for a data directory (in-memory when ``None``)."""

    def store(filename: str) -> RecordStore:
        if data_dir is None:
            return InMemoryRecordStore()
        return JsonlRecordStore(Path(data_dir) / TRAINING_DIRECTORY / filename)

    return RLHFRepositories(
        feedback=FeedbackRepository(store(FEEDBACK_FILENAME), cap=cap),
        ratings=RatingRepository(store(RATINGS_FILENAME), cap=cap),
        datasets=RewardDatasetRepository(store(REWARD_DATASET_FILENAME), cap=cap),
        disagreements=DisagreementRepository(store(DISAGREEMENT_FILENAME), cap=cap),
    )


__all__ = [
    "DISAGREEMENT_FILENAME",
    "FEEDBACK_FILENAME",
    "RATINGS_FILENAME",
    "REWARD_DATASET_FILENAME",
    "DisagreementRepository",
    "FeedbackRepository",
    "RatingRepository",
    "RLHFRepositories",
    "RewardDatasetRepository",
    "build_rlhf_repositories",
]
