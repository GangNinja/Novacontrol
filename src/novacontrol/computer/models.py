"""Device and operation models for universal computer operation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class DeviceKind(StrEnum):
    DESKTOP = "desktop"
    DISPLAY = "display"
    KEYBOARD_MOUSE = "keyboard_mouse"
    BROWSER = "browser"
    PHONE = "phone"


class OperationClass(StrEnum):
    """The safety classes Phase 29 distinguishes.

    This is a *description*, not a second policy engine.  The authoritative
    allow/deny decision comes from ``reliability.permissions.PermissionManager``;
    this classification is what that layer and the operation history are told
    about an action so a read and a deletion cannot be recorded as equivalent.
    """

    READ_ONLY = "read_only"
    REVERSIBLE = "reversible"
    SENSITIVE = "sensitive"
    DESTRUCTIVE = "destructive"
    PRIVILEGED = "privileged"


@dataclass(frozen=True, slots=True)
class CapabilitySupport:
    """One operation a device can genuinely perform, and its limits."""

    name: str
    available: bool
    detail: str = ""
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "detail": self.detail,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """A device the registry has probed, including what it cannot do."""

    id: str
    kind: DeviceKind
    name: str
    available: bool
    connected: bool
    capabilities: tuple[CapabilitySupport, ...] = ()
    limitations: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "name": self.name,
            "available": self.available,
            "connected": self.connected,
            "capabilities": [capability.to_dict() for capability in self.capabilities],
            "limitations": list(self.limitations),
            "metadata": self.metadata,
        }

    @property
    def supported_operations(self) -> tuple[str, ...]:
        return tuple(capability.name for capability in self.capabilities if capability.available)


@dataclass(frozen=True, slots=True)
class DeviceReport:
    """The full live answer: what exists, what is connected, what is unknown."""

    devices: tuple[DeviceInfo, ...]
    platform: str
    probed_at: str
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "probed_at": self.probed_at,
            "devices": [device.to_dict() for device in self.devices],
            "notes": list(self.notes),
        }

    def device(self, device_id: str) -> DeviceInfo | None:
        for device in self.devices:
            if device.id == device_id:
                return device
        return None

    @property
    def supported_operations(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                operation
                for device in self.devices
                for operation in device.supported_operations
            )
        )


#: Destructive verbs are checked before everything else.  "Remove a file" is
#: not made reversible by also containing "open", so the ordering is deliberate.
#: Read-only verbs must be recognized before the reversible table: a request
#: to observe, inspect or screenshot is not consent to change anything, and a
#: history record must be able to say so.
_OPERATION_RULES: tuple[tuple[OperationClass, tuple[str, ...]], ...] = (
    (
        OperationClass.READ_ONLY,
        (
            "observe", "inspect", "read ", "screenshot", "capture screen",
            "list ", "enumerate", "describe screen", "find text",
        ),
    ),
    (
        OperationClass.PRIVILEGED,
        (
            "install", "uninstall", "elevate", "administrator", "registry",
            "group policy", "service", "driver", "firewall",
        ),
    ),
    (
        OperationClass.DESTRUCTIVE,
        (
            "delete", "remove", "unlink", "wipe", "erase", "format", "overwrite",
            "move file", "rename", "organize", "close application", "kill",
            "shutdown", "restart",
        ),
    ),
    (
        OperationClass.SENSITIVE,
        (
            "send", "submit", "purchase", "buy", "order", "pay", "checkout",
            "transfer", "publish", "upload", "download", "login", "password",
            "account", "permission",
        ),
    ),
    (
        OperationClass.REVERSIBLE,
        (
            "click", "type", "scroll", "drag", "drop", "press", "navigate",
            "focus", "minimize", "maximize", "open", "launch", "copy", "paste",
            "save", "write", "create", "mkdir", "move",
        ),
    ),
)


def classify_operation(name: str) -> OperationClass:
    """Classify an action by its own words.

    Unknown actions are ``SENSITIVE`` rather than ``REVERSIBLE``: an action the
    build cannot describe must not be treated as freely repeatable.
    """
    lowered = " ".join(str(name or "").strip().lower().split())
    if not lowered:
        return OperationClass.SENSITIVE
    for operation_class, markers in _OPERATION_RULES:
        if any(marker in lowered for marker in markers):
            return operation_class
    return OperationClass.SENSITIVE
