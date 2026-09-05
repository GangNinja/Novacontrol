"""Desktop automation subsystem."""

from novacontrol.desktop.audit import AutomationAuditLog, InMemoryAutomationAuditLog
from novacontrol.desktop.controller import (
    DesktopAutomationController,
    DesktopCommandRunner,
    LocalDesktopRunner,
    NoopDesktopRunner,
)
from novacontrol.desktop.models import (
    DesktopAction,
    DesktopActionResult,
    DesktopActionStatus,
    DesktopActionType,
    DesktopWorkflow,
)
from novacontrol.desktop.runtime import DesktopAutomationModule

__all__ = [
    "AutomationAuditLog",
    "DesktopAction",
    "DesktopActionResult",
    "DesktopActionStatus",
    "DesktopActionType",
    "DesktopAutomationController",
    "DesktopAutomationModule",
    "DesktopCommandRunner",
    "DesktopWorkflow",
    "InMemoryAutomationAuditLog",
    "LocalDesktopRunner",
    "NoopDesktopRunner",
]
