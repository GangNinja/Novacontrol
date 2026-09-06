"""Evaluation Ledger: measurable agentic metrics.

Records one entry per task run and derives the metrics the improvement loop
optimizes against: success rate, recovery rate, steps per task, verification
accuracy. Persisted through the application's JsonStateStore — no new DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


@dataclass(slots=True)
class EvaluationRecord:
    task_id: str
    goal: str
    completed: bool
    steps_planned: int = 0
    steps_executed: int = 0
    recoveries_attempted: int = 0
    recoveries_succeeded: int = 0
    verifications_run: int = 0
    verifications_passed: int = 0
    research_calls: int = 0
    duration_seconds: float = 0.0
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal[:200],
            "completed": self.completed,
            "steps_planned": self.steps_planned,
            "steps_executed": self.steps_executed,
            "recoveries_attempted": self.recoveries_attempted,
            "recoveries_succeeded": self.recoveries_succeeded,
            "verifications_run": self.verifications_run,
            "verifications_passed": self.verifications_passed,
            "research_calls": self.research_calls,
            "duration_seconds": round(self.duration_seconds, 2),
            "at": self.at,
        }


class EvaluationLedger:
    """Append-only task evaluation records with derived metrics."""

    def __init__(self, *, max_records: int = 500) -> None:
        self._records: list[EvaluationRecord] = []
        self._max_records = max_records

    def record(self, record: EvaluationRecord) -> None:
        self._records.append(record)
        if len(self._records) > self._max_records:
            self._records = self._records[-self._max_records:]

    def record_task(self, state_dict: dict[str, Any], *, duration_seconds: float) -> EvaluationRecord:
        verifications_run = 1 if state_dict.get("verification") else 0
        record = EvaluationRecord(
            task_id=str(state_dict.get("task_id", uuid4().hex)),
            goal=str(state_dict.get("goal", "")),
            completed=str(state_dict.get("status")) == "completed",
            steps_planned=len(state_dict.get("plan", [])),
            steps_executed=len(state_dict.get("actions", [])),
            recoveries_attempted=len(state_dict.get("recoveries", [])),
            recoveries_succeeded=sum(
                1 for r in state_dict.get("recoveries", []) if r.get("succeeded")
            ),
            verifications_run=verifications_run,
            verifications_passed=verifications_run if state_dict.get("verification", {}).get("status") == "pass" else 0,
            research_calls=sum(
                1 for lesson in state_dict.get("learned_information", []) if lesson.get("kind") == "research"
            ),
            duration_seconds=duration_seconds,
        )
        self.record(record)
        return record

    def metrics(self) -> dict[str, Any]:
        total = len(self._records)
        if not total:
            return {"tasks": 0}
        completed = sum(1 for r in self._records if r.completed)
        recoveries = sum(r.recoveries_attempted for r in self._records)
        recoveries_ok = sum(r.recoveries_succeeded for r in self._records)
        verifications = sum(r.verifications_run for r in self._records)
        verifications_ok = sum(r.verifications_passed for r in self._records)
        return {
            "tasks": total,
            "task_success_rate": round(completed / total, 3),
            "recovery_success_rate": round(recoveries_ok / recoveries, 3) if recoveries else None,
            "verification_accuracy": round(verifications_ok / verifications, 3) if verifications else None,
            "avg_steps_per_task": round(sum(r.steps_executed for r in self._records) / total, 2),
            "avg_duration_seconds": round(sum(r.duration_seconds for r in self._records) / total, 2),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"metrics": self.metrics(), "recent": [r.to_dict() for r in self._records[-25:]]}

    def load(self, snapshot: dict[str, Any]) -> None:
        for item in snapshot.get("records", []) or []:
            try:
                record = EvaluationRecord(
                    task_id=str(item.get("task_id", "")),
                    goal=str(item.get("goal", "")),
                    completed=bool(item.get("completed", False)),
                    steps_planned=int(item.get("steps_planned", 0)),
                    steps_executed=int(item.get("steps_executed", 0)),
                    recoveries_attempted=int(item.get("recoveries_attempted", 0)),
                    recoveries_succeeded=int(item.get("recoveries_succeeded", 0)),
                    verifications_run=int(item.get("verifications_run", 0)),
                    verifications_passed=int(item.get("verifications_passed", 0)),
                    research_calls=int(item.get("research_calls", 0)),
                    duration_seconds=float(item.get("duration_seconds", 0.0)),
                    at=str(item.get("at", "")),
                )
                self._records.append(record)
            except Exception:
                continue

    def snapshot(self) -> dict[str, Any]:
        return {"records": [r.to_dict() for r in self._records]}
