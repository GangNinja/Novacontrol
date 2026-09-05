"""In-memory scheduler."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from novacontrol.scheduler.models import ScheduledTask, ScheduledTaskStatus

ScheduledTaskHandler = Callable[[ScheduledTask], Awaitable[None]]


class InMemoryScheduler:
    def __init__(self) -> None:
        self._tasks: dict[str, ScheduledTask] = {}

    def schedule_once(
        self,
        name: str,
        *,
        delay_seconds: float = 0,
        payload: dict[str, Any] | None = None,
    ) -> ScheduledTask:
        task = ScheduledTask(
            name=name,
            run_at=datetime.now(UTC) + timedelta(seconds=delay_seconds),
            payload=payload or {},
        )
        self._tasks[task.id] = task
        return task

    def tasks(self) -> tuple[ScheduledTask, ...]:
        return tuple(self._tasks[key] for key in sorted(self._tasks))

    def due_tasks(self, now: datetime | None = None) -> tuple[ScheduledTask, ...]:
        return tuple(task for task in self.tasks() if task.due(now))

    def cancel(self, task_id: str) -> ScheduledTask:
        task = self._tasks[task_id]
        updated = replace(task, status=ScheduledTaskStatus.CANCELLED)
        self._tasks[task_id] = updated
        return updated

    async def run_due(self, handler: ScheduledTaskHandler) -> tuple[ScheduledTask, ...]:
        completed = []
        for task in self.due_tasks():
            running = replace(task, status=ScheduledTaskStatus.RUNNING)
            self._tasks[task.id] = running
            try:
                await handler(running)
                updated = replace(running, status=ScheduledTaskStatus.COMPLETED)
            except Exception:
                updated = replace(running, status=ScheduledTaskStatus.FAILED)
            self._tasks[task.id] = updated
            completed.append(updated)
        return tuple(completed)

    def to_dict(self) -> dict[str, Any]:
        return {"tasks": [task.to_dict() for task in self._tasks.values()]}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "InMemoryScheduler":
        scheduler = cls()
        for item in payload.get("tasks", []):
            if isinstance(item, dict):
                task = ScheduledTask.from_dict(item)
                scheduler._tasks[task.id] = task
        return scheduler
