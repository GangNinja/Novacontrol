"""Desktop automation subsystem."""

# Audit names are re-exported here for backward compatibility: the log itself
# is shared with the browser controller and lives in core/audit.py.
from novacontrol.core.audit import AutomationAuditLog, InMemoryAutomationAuditLog
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
