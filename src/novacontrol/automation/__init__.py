"""Automation: reusable workflows, and scheduled requests.

Two layers, deliberately separate:

* :class:`AutomationManager` stores reusable WORKFLOWS — a named sequence of
  steps a caller replays.
* :class:`AutomationEngine` stores SCHEDULED REQUESTS — one request plus a
  schedule, with the state of scheduling it. It executes nothing itself: it
  hands the request text to a runner, which is the application's request path,
  so a scheduled task travels intent detection → decision → plan → permission →
  execution exactly as a spoken one does.
"""

from novacontrol.automation.engine import (
    AUTOMATION_RUN_SCOPE,
    AutomationEngine,
    AutomationOutcome,
    AutomationRunner,
)
from novacontrol.automation.manager import AutomationManager
from novacontrol.automation.models import (
    AutomationCondition,
    AutomationKind,
    AutomationRun,
    AutomationRunStatus,
    AutomationStatus,
    AutomationStep,
    AutomationTask,
    AutomationWorkflow,
    AutomationWorkflowStatus,
    ConditionKind,
)
from novacontrol.automation.schedule import (
    MIN_INTERVAL_SECONDS,
    WEEKDAY_NAMES,
    Schedule,
    ScheduleKind,
    parse_schedule,
    strip_schedule,
)

__all__ = [
    "AUTOMATION_RUN_SCOPE",
    "MIN_INTERVAL_SECONDS",
    "WEEKDAY_NAMES",
    "AutomationCondition",
    "AutomationEngine",
    "AutomationKind",
    "AutomationManager",
    "AutomationOutcome",
    "AutomationRun",
    "AutomationRunStatus",
    "AutomationRunner",
    "AutomationStatus",
    "AutomationStep",
    "AutomationTask",
    "AutomationWorkflow",
    "AutomationWorkflowStatus",
    "ConditionKind",
    "Schedule",
    "ScheduleKind",
    "parse_schedule",
    "strip_schedule",
]
