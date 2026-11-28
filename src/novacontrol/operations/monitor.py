"""Monitoring and notifications over REAL stored state.

The monitor answers one question per objective: is there anything a person
should be told, given the state that is actually stored? It reads no model, makes
no network call and performs no action. Its outputs are :class:`MonitoringSignal`
values, and ``NO_CHANGE`` is one of them — a quiet objective is a reported
conclusion, not a missing one.

Notifications are deduplicated by a key that names the state being reported. A
deadline warning is therefore sent once, not once per poll; a new state (the
task failed, the deadline passed) has a new key and is reported. That is the
whole anti-spam design, and it is deliberately not a timer.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from novacontrol.operations.models import (
    AutonomyLevel,
    MonitoringSignal,
    Notification,
    NotificationKind,
    Objective,
    ObjectiveStatus,
    SignalKind,
    TaskState,
    utcnow,
)


class NotificationCenter:
    """Stores notifications, applies preferences, and suppresses duplicates."""

    def __init__(
        self,
        notifications: Sequence[Notification] = (),
        *,
        enabled_kinds: Iterable[str] | None = None,
        dedupe_window_seconds: float = 6 * 60 * 60,
        limit: int = 200,
        muted: bool = False,
        quiet: bool = False,
    ) -> None:
        self._notifications: list[Notification] = list(notifications)
        self.enabled_kinds: set[str] = (
            {kind.value for kind in NotificationKind}
            if enabled_kinds is None
            else {str(kind) for kind in enabled_kinds}
        )
        self.dedupe_window_seconds = max(0.0, float(dedupe_window_seconds))
        self.limit = max(10, int(limit))
        self.muted = bool(muted)
        #: The last time each dedupe key was emitted, for suppression.
        self._seen: dict[str, datetime] = {}
        self._quiet = bool(quiet)

    # -- preferences -----------------------------------------------------------

    def configure(
        self,
        *,
        enabled_kinds: Iterable[str] | None = None,
        dedupe_window_seconds: float | None = None,
        muted: bool | None = None,
    ) -> dict[str, Any]:
        if enabled_kinds is not None:
            valid = {kind.value for kind in NotificationKind}
            unknown = set(map(str, enabled_kinds)) - valid
            if unknown:
                raise ValueError(
                    "Unknown notification kinds: " + ", ".join(sorted(unknown))
                )
            self.enabled_kinds = set(map(str, enabled_kinds))
        if dedupe_window_seconds is not None:
            self.dedupe_window_seconds = max(0.0, float(dedupe_window_seconds))
        if muted is not None:
            self.muted = bool(muted)
        return self.preferences()

    def preferences(self) -> dict[str, Any]:
        return {
            "enabled_kinds": sorted(self.enabled_kinds),
            "dedupe_window_seconds": self.dedupe_window_seconds,
            "muted": self.muted,
            "limit": self.limit,
        }

    # -- writing ---------------------------------------------------------------

    def notify(
        self,
        kind: NotificationKind,
        title: str,
        body: str,
        *,
        objective_id: str = "",
        task_id: str = "",
        severity: str = "info",
        dedupe_key: str = "",
        evidence: Sequence[str] = (),
        now: datetime | None = None,
    ) -> Notification | None:
        """Store a notification unless it is muted, disabled or a duplicate.

        Returns ``None`` when nothing was stored, so a caller can count real
        notifications rather than attempts to create them.
        """
        if self.muted or kind.value not in self.enabled_kinds:
            return None
        clock = now or utcnow()
        key = dedupe_key or f"{kind.value}:{objective_id}:{task_id}"
        last = self._seen.get(key)
        if last is not None and (clock - last).total_seconds() < self.dedupe_window_seconds:
            return None
        notification = Notification(
            kind=kind,
            title=title,
            body=body,
            objective_id=objective_id,
            task_id=task_id,
            severity=severity,
            dedupe_key=key,
            evidence=tuple(str(item) for item in evidence),
            created_at=clock,
        )
        self._notifications.append(notification)
        self._seen[key] = clock
        if len(self._notifications) > self.limit:
            # Bounded history: the oldest read rows go first, and unread rows are
            # only dropped once there is nothing read left to drop.
            unread = [item for item in self._notifications if not item.read]
            read = [item for item in self._notifications if item.read]
            keep = unread[-(self.limit // 2) :] + read[-(self.limit - self.limit // 2) :]
            self._notifications = sorted(keep, key=lambda item: item.created_at)
        return notification

    def acknowledge(self, notification_id: str, *, now: datetime | None = None) -> Notification:
        clock = now or utcnow()
        for index, item in enumerate(self._notifications):
            if item.id == notification_id:
                updated = replace(item, read=True, read_at=clock)
                self._notifications[index] = updated
                return updated
        raise KeyError(f"Notification not found: {notification_id}")

    def acknowledge_all(self, *, now: datetime | None = None) -> int:
        clock = now or utcnow()
        changed = 0
        for index, item in enumerate(self._notifications):
            if not item.read:
                self._notifications[index] = replace(item, read=True, read_at=clock)
                changed += 1
        return changed

    def clear(self) -> int:
        count = len(self._notifications)
        self._notifications.clear()
        self._seen.clear()
        return count

    # -- reading ---------------------------------------------------------------

    def list(self, *, unread_only: bool = False, limit: int = 50) -> tuple[Notification, ...]:
        rows = [item for item in self._notifications if not unread_only or not item.read]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return tuple(rows[: max(1, limit)])

    @property
    def unread_count(self) -> int:
        return sum(1 for item in self._notifications if not item.read)

    def to_dict(self) -> dict[str, Any]:
        return {
            "notifications": [item.to_dict() for item in self._notifications],
            "preferences": self.preferences(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> NotificationCenter:
        data = payload or {}
        notifications: list[Notification] = []
        for item in data.get("notifications", ()):
            if not isinstance(item, Mapping):
                continue
            try:
                notifications.append(Notification.from_dict(item))
            except (KeyError, ValueError, TypeError):
                continue
        preferences = data.get("preferences")
        preferences = preferences if isinstance(preferences, Mapping) else {}
        enabled = preferences.get("enabled_kinds")
        return cls(
            notifications,
            enabled_kinds=enabled if isinstance(enabled, (list, tuple, set)) else None,
            dedupe_window_seconds=float(
                preferences.get("dedupe_window_seconds", 6 * 60 * 60)
            ),
            muted=bool(preferences.get("muted", False)),
            limit=int(preferences.get("limit", 200)),
        )

    def report(self) -> dict[str, Any]:
        return {
            "total": len(self._notifications),
            "unread": self.unread_count,
            **self.preferences(),
        }


class OperationsMonitor:
    """Derives monitoring signals from stored objective state."""

    def __init__(
        self,
        *,
        deadline_window: timedelta = timedelta(hours=24),
        stall_after: timedelta = timedelta(hours=24),
    ) -> None:
        self.deadline_window = deadline_window
        self.stall_after = stall_after

    def scan(
        self,
        objectives: Iterable[Objective],
        *,
        now: datetime | None = None,
    ) -> tuple[MonitoringSignal, ...]:
        clock = now or utcnow()
        signals: list[MonitoringSignal] = []
        for objective in objectives:
            signals.extend(self._scan_one(objective, clock))
        return tuple(signals)

    def _scan_one(self, objective: Objective, clock: datetime) -> list[MonitoringSignal]:
        signals: list[MonitoringSignal] = []

        if objective.status is ObjectiveStatus.COMPLETED:
            return [
                MonitoringSignal(
                    kind=SignalKind.COMPLETED,
                    objective_id=objective.id,
                    detail=(
                        f"Objective {objective.title!r} is complete: all "
                        f"{len(objective.tasks)} tasks verified."
                    ),
                    dedupe_key=f"completed:{objective.id}",
                    severity="success",
                )
            ]
        if objective.status is ObjectiveStatus.CANCELLED:
            return [
                MonitoringSignal(
                    kind=SignalKind.NO_CHANGE,
                    objective_id=objective.id,
                    detail="Objective is cancelled; nothing is monitored.",
                )
            ]
        if objective.status is ObjectiveStatus.PAUSED:
            return [
                MonitoringSignal(
                    kind=SignalKind.NO_CHANGE,
                    objective_id=objective.id,
                    detail=(
                        "Objective is paused"
                        + (f": {objective.paused_reason}" if objective.paused_reason else ".")
                    ),
                )
            ]

        if objective.deadline is not None:
            remaining = objective.deadline - clock
            if remaining.total_seconds() < 0:
                signals.append(
                    MonitoringSignal(
                        kind=SignalKind.OVERDUE,
                        objective_id=objective.id,
                        detail=(
                            f"Objective {objective.title!r} is overdue by "
                            f"{_human(-remaining)} (deadline "
                            f"{objective.deadline.isoformat(timespec='minutes')})."
                        ),
                        severity="warning",
                        actionable=True,
                        dedupe_key=f"overdue:objective:{objective.id}",
                    )
                )
            elif remaining <= self.deadline_window:
                signals.append(
                    MonitoringSignal(
                        kind=SignalKind.DEADLINE_APPROACHING,
                        objective_id=objective.id,
                        detail=(
                            f"Objective {objective.title!r} is due in "
                            f"{_human(remaining)}."
                        ),
                        severity="warning",
                        actionable=True,
                        dedupe_key=(
                            f"deadline:{objective.id}:{objective.deadline.date().isoformat()}"
                        ),
                    )
                )

        for task in objective.tasks:
            if task.state is TaskState.COMPLETED:
                continue
            if task.due_at is not None and task.due_at < clock:
                signals.append(
                    MonitoringSignal(
                        kind=SignalKind.OVERDUE,
                        objective_id=objective.id,
                        task_id=task.id,
                        detail=(
                            f"Task {task.title!r} is overdue by "
                            f"{_human(clock - task.due_at)}."
                        ),
                        severity="warning",
                        actionable=True,
                        dedupe_key=f"overdue:task:{task.id}",
                    )
                )
            if task.state is TaskState.WAITING_APPROVAL:
                signals.append(
                    MonitoringSignal(
                        kind=SignalKind.APPROVAL_REQUIRED,
                        objective_id=objective.id,
                        task_id=task.id,
                        detail=(
                            f"Task {task.title!r} is waiting for approval"
                            + (f": {task.error}" if task.error else ".")
                        ),
                        severity="warning",
                        actionable=True,
                        dedupe_key=f"approval:{task.id}:{task.attempts}",
                    )
                )
            elif task.state is TaskState.FAILED:
                signals.append(
                    MonitoringSignal(
                        kind=SignalKind.FAILED,
                        objective_id=objective.id,
                        task_id=task.id,
                        detail=(
                            f"Task {task.title!r} failed"
                            + (f": {task.error}" if task.error else ".")
                        ),
                        severity="error",
                        actionable=True,
                        dedupe_key=f"failed:{task.id}:{task.attempts}",
                    )
                )
            elif task.state is TaskState.BLOCKED:
                signals.append(
                    MonitoringSignal(
                        kind=SignalKind.BLOCKED,
                        objective_id=objective.id,
                        task_id=task.id,
                        detail=(
                            f"Task {task.title!r} is blocked"
                            + (f": {task.error}" if task.error else ".")
                        ),
                        severity="warning",
                        actionable=True,
                        dedupe_key=f"blocked:{task.id}",
                    )
                )
            elif (
                task.state in (TaskState.READY, TaskState.RUNNING)
                and clock - task.updated_at >= self.stall_after
            ):
                signals.append(
                    MonitoringSignal(
                        kind=SignalKind.STALLED,
                        objective_id=objective.id,
                        task_id=task.id,
                        detail=(
                            f"Task {task.title!r} has shown no progress for "
                            f"{_human(clock - task.updated_at)}."
                        ),
                        severity="warning",
                        actionable=True,
                        dedupe_key=f"stalled:{task.id}:{task.updated_at.date().isoformat()}",
                    )
                )
            elif task.state is TaskState.READY:
                signals.append(
                    MonitoringSignal(
                        kind=SignalKind.ACTIONABLE,
                        objective_id=objective.id,
                        task_id=task.id,
                        detail=(
                        f"Task {task.title!r} is ready to run."
                        if AutonomyLevel(objective.autonomy).runs_work
                        else (
                            f"Task {task.title!r} is ready, but the objective is set "
                            f"to {objective.autonomy.value}; a person must start it."
                        )
                    ),
                    )
                )

        if not signals:
            signals.append(
                MonitoringSignal(
                    kind=SignalKind.NO_CHANGE,
                    objective_id=objective.id,
                    detail="No change detected.",
                )
            )
        return signals

    def notifications_for(
        self,
        signals: Sequence[MonitoringSignal],
        center: NotificationCenter,
        *,
        now: datetime | None = None,
    ) -> tuple[Notification, ...]:
        """Turn signals into notifications, keeping only what was stored."""
        clock = now or utcnow()
        stored: list[Notification] = []
        for signal in signals:
            kind = signal.to_dict().get("notification_kind")
            if not kind:
                continue
            notification = center.notify(
                NotificationKind(str(kind)),
                _title_for(signal),
                signal.detail,
                objective_id=signal.objective_id,
                task_id=signal.task_id,
                severity=signal.severity,
                dedupe_key=signal.dedupe_key,
                now=clock,
            )
            if notification is not None:
                stored.append(notification)
        return tuple(stored)


def _title_for(signal: MonitoringSignal) -> str:
    return {
        SignalKind.DEADLINE_APPROACHING: "Deadline approaching",
        SignalKind.OVERDUE: "Deadline passed",
        SignalKind.STALLED: "Task stalled",
        SignalKind.APPROVAL_REQUIRED: "Approval required",
        SignalKind.FAILED: "Task failed",
        SignalKind.BLOCKED: "Task blocked",
        SignalKind.COMPLETED: "Objective complete",
        SignalKind.ACTIONABLE: "Work ready",
    }.get(signal.kind, "Operations update")


def _human(delta: timedelta) -> str:
    seconds = int(abs(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"
