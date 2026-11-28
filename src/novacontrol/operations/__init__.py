"""Phase 30 — persistent autonomy and personal operations.

The package composes what NovaControl already has rather than replacing it:

* scheduling values come from :mod:`novacontrol.automation.schedule`;
* execution is the application's own request pipeline, injected as a callable;
* authority is the shared :class:`~novacontrol.reliability.permissions.PermissionManager`;
* state is persisted through the same ``JsonStateStore`` every subsystem uses.

What is new is the layer that keeps an OUTCOME alive across restarts: objectives
with milestones and dependency-aware tasks, verified checkpoints, a monitoring
pass that derives notifications from stored state, and autonomy levels that are
ceilings rather than grants.
"""

from novacontrol.operations.manager import OperationsManager
from novacontrol.operations.models import (
    AutonomyLevel,
    Milestone,
    MonitoringSignal,
    Notification,
    NotificationKind,
    Objective,
    ObjectiveKind,
    ObjectiveStatus,
    OperationTask,
    SignalKind,
    TaskKind,
    TaskOutcome,
    TaskState,
)
from novacontrol.operations.monitor import NotificationCenter, OperationsMonitor
from novacontrol.operations.service import DEFAULT_TASK_SCOPE, OperationsService

__all__ = [
    "DEFAULT_TASK_SCOPE",
    "AutonomyLevel",
    "Milestone",
    "MonitoringSignal",
    "Notification",
    "NotificationCenter",
    "NotificationKind",
    "Objective",
    "ObjectiveKind",
    "ObjectiveStatus",
    "OperationTask",
    "OperationsManager",
    "OperationsMonitor",
    "OperationsService",
    "SignalKind",
    "TaskKind",
    "TaskOutcome",
    "TaskState",
]
