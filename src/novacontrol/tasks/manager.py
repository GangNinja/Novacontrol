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
