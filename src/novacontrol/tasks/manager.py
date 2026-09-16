"""Task center for long-running work tracking."""

from __future__ import annotations

from dataclasses import replace

from typing import Any

from novacontrol.tasks.models import TaskRecord, TaskRecordStatus


class TaskCenter:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}

    def create(self, title: str, *, kind: str = "general") -> TaskRecord:
        task = TaskRecord(title=title, kind=kind)
        self._tasks[task.id] = task
        return task

    def update(
        self,
        task_id: str,
        status: TaskRecordStatus,
        *,
        progress: float | None = None,
        result: dict[str, object] | None = None,
        error: str | None = None,
    ) -> TaskRecord:
        task = self._tasks[task_id]
        updated = replace(
            task,
            status=status,
            progress=task.progress if progress is None else progress,
            result=task.result if result is None else result,
            error=error,
        )
        self._tasks[task_id] = updated
        return updated

    def get(self, task_id: str) -> TaskRecord:
        return self._tasks[task_id]

    def delete(self, task_id: str) -> TaskRecord:
        """Remove one task record and return it (raises KeyError when unknown)."""
        return self._tasks.pop(task_id)

    def clear(self) -> int:
        """Remove every task record; returns how many were deleted."""
        count = len(self._tasks)
        self._tasks.clear()
        return count

    def clear_snapshot(self) -> list[TaskRecord]:
        """Remove every task record and RETURN what was removed.

        The undo path for "Delete All Tasks": the caller holds this snapshot
        until the user's undo window closes, then discards it. Unknown
        TaskRecord payload shapes are skipped defensively so a corrupt record
        can never block the wipe it belongs to.
        """
        removed = list(self._tasks.values())
        self._tasks.clear()
        return removed

    def restore(self, records: list[TaskRecord]) -> int:
        """Put previously cleared records back; returns how many were restored.

        Existing ids are left alone (a record re-created after the clear wins),
        so a restore never resurrects duplicates.
        """
        restored = 0
        for record in records:
            if record.id in self._tasks:
                continue
            self._tasks[record.id] = record
            restored += 1
        return restored

    def list(self) -> tuple[TaskRecord, ...]:
        return tuple(self._tasks[key] for key in sorted(self._tasks))

    def to_dict(self) -> dict[str, Any]:
        return {"tasks": [task.to_dict() for task in self._tasks.values()]}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskCenter":
        center = cls()
        for item in payload.get("tasks", []):
            if isinstance(item, dict):
                task = TaskRecord.from_dict(item)
                center._tasks[task.id] = task
        return center
