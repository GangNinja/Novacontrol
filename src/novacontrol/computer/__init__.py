"""Universal computer and device operation (Phase 29).

This package is a thin capability layer over the systems NovaControl already
owns: ``agentcore`` runs the closed loop, ``desktop`` injects input through the
DPI-aware worker and input guard, ``browser`` drives Playwright, ``vision`` and
the desktop OCR reader provide perception, ``reliability.permissions`` is the
risk layer, and the event bus carries progress.  It adds the pieces those
systems did not have: an honest device/capability registry, a persisted
operation history, context/reference resolution, and a cancellation-controlled
operator entry point.
"""

from novacontrol.computer.context import ReferenceResolution, resolve_reference
from novacontrol.computer.devices import DeviceRegistry, probe_displays
from novacontrol.computer.history import ComputerOperationHistory, ComputerOperationRecord
from novacontrol.computer.models import (
    CapabilitySupport,
    DeviceInfo,
    DeviceKind,
    DeviceReport,
    OperationClass,
    classify_operation,
)
from novacontrol.computer.operator import ComputerOperator, ComputerRunResult, ComputerTaskBusy

__all__ = [
    "CapabilitySupport",
    "ComputerOperationHistory",
    "ComputerOperationRecord",
    "ComputerOperator",
    "ComputerRunResult",
    "ComputerTaskBusy",
    "DeviceInfo",
    "DeviceKind",
    "DeviceRegistry",
    "DeviceReport",
    "OperationClass",
    "ReferenceResolution",
    "classify_operation",
    "probe_displays",
    "resolve_reference",
]
