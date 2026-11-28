"""Phase 27 integration: evidence from real operation history.

Continual learning is an OBSERVER of what happened, not a second planner. This
module reads the stored tasks — their outcomes, attempts, durations and failure
reasons — and reports what the evidence does and does not support. Two rules
keep it honest:

* **confidence follows sample size.** One slow run is not a preference; a kind
  with three samples is still low confidence, and the summary says so.
* **the output is advisory.** Nothing here rewrites a schedule, raises a retry
  ceiling or changes an autonomy level. A person (or a later phase with an
  explicit approval path) decides what to do with the evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from novacontrol.operations.models import Objective, OperationTask, TaskState


@dataclass(frozen=True, slots=True)
class KindEvidence:
    kind: str
    completed: int = 0
    failed: int = 0
    blocked: int = 0
    attempts: int = 0
    average_duration_seconds: float = 0.0
    success_rate: float = 0.0
    confidence: str = "none"
    sample_size: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "completed": self.completed,
            "failed": self.failed,
            "blocked": self.blocked,
            "attempts": self.attempts,
            "average_duration_seconds": round(self.average_duration_seconds, 3),
            "success_rate": round(self.success_rate, 4),
            "confidence": self.confidence,
            "sample_size": self.sample_size,
            "notes": list(self.notes),
        }


class OperationsLearning:
    """Summarises stored task history into confidence-labelled evidence."""

    def __init__(self, *, min_samples: int = 3, high_samples: int = 12) -> None:
        self.min_samples = max(1, int(min_samples))
        self.high_samples = max(self.min_samples + 1, int(high_samples))

    def summarise(self, objectives: Iterable[Objective]) -> dict[str, Any]:
        tasks = [task for objective in objectives for task in objective.tasks]
        by_kind: dict[str, list[OperationTask]] = {}
        for task in tasks:
            by_kind.setdefault(task.kind.value, []).append(task)

        kinds = [self._kind(kind, rows) for kind, rows in sorted(by_kind.items())]
        advisory: list[str] = []
        for evidence in kinds:
            advisory.extend(
                f"{evidence.kind}: {note}" for note in evidence.notes
            )
        return {
            "source": "stored operation tasks (state, attempts, timestamps, errors)",
            "samples": len(tasks),
            "kinds": [evidence.to_dict() for evidence in kinds],
            "advisory": advisory,
            "policy": (
                "Advisory only: this evidence never changes a schedule, a retry "
                "ceiling or an autonomy level by itself."
            ),
            "confidence": self._overall(kinds),
        }

    def _kind(self, kind: str, rows: list[OperationTask]) -> KindEvidence:
        completed = [row for row in rows if row.state is TaskState.COMPLETED]
        failed = [row for row in rows if row.state is TaskState.FAILED]
        blocked = [row for row in rows if row.state is TaskState.BLOCKED]
        durations = [
            (row.completed_at - row.started_at).total_seconds()
            for row in completed
            if row.started_at is not None and row.completed_at is not None
        ]
        attempts = sum(row.attempts for row in rows if row.attempts)
        sample = len(completed) + len(failed)
        rate = len(completed) / sample if sample else 0.0
        confidence = self._confidence(sample)
        notes: list[str] = []
        if confidence != "none":
            if rate < 0.5:
                notes.append(
                    f"{len(failed)} of {sample} finished runs failed — add a "
                    "precondition check before repeating this work"
                )
            if attempts and sample and attempts / sample > 1.5:
                notes.append(
                    f"runs average {attempts / sample:.1f} attempts — a smaller "
                    "first step would avoid the retry"
                )
            if durations and sum(durations) / len(durations) > 900:
                notes.append(
                    "runs average over 15 minutes — schedule them as a block "
                    "rather than a quick check"
                )
        return KindEvidence(
            kind=kind,
            completed=len(completed),
            failed=len(failed),
            blocked=len(blocked),
            attempts=attempts,
            average_duration_seconds=(sum(durations) / len(durations)) if durations else 0.0,
            success_rate=rate,
            confidence=confidence,
            sample_size=sample,
            notes=tuple(notes),
        )

    def _confidence(self, sample: int) -> str:
        if sample < self.min_samples:
            return "none"
        if sample < self.min_samples * 2:
            return "low"
        if sample < self.high_samples:
            return "medium"
        return "high"

    def _overall(self, kinds: list[KindEvidence]) -> str:
        rank = {"none": 0, "low": 1, "medium": 2, "high": 3}
        if not kinds:
            return "none"
        return max((item.confidence for item in kinds), key=lambda value: rank[value])
