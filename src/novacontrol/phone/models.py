"""Phone control domain models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class PhoneBridgeState(StrEnum):
    NOT_CONFIGURED = "not_configured"
    NO_DEVICE = "no_device"
    DEVICE_CONNECTED = "device_connected"


class PhoneActionType(StrEnum):
    OPEN_APPLICATION = "open_application"


class PhoneActionStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class PhoneDevice:
    id: str
    state: str
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "state": self.state, "model": self.model}


@dataclass(frozen=True, slots=True)
class PhoneBridgeStatus:
    available: bool
    state: PhoneBridgeState
    adapter: str
    devices: tuple[PhoneDevice, ...] = ()
    next_steps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "state": self.state.value,
            "adapter": self.adapter,
            "devices": [device.to_dict() for device in self.devices],
            "next_steps": list(self.next_steps),
        }


@dataclass(frozen=True, slots=True)
class PhoneAction:
    type: PhoneActionType
    target: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "target": self.target,
            "description": self.description,
            "parameters": dict(self.parameters),
        }


@dataclass(frozen=True, slots=True)
class PhoneWorkflow:
    name: str
    actions: tuple[PhoneAction, ...]
    id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "actions": [action.to_dict() for action in self.actions],
        }


@dataclass(frozen=True, slots=True)
class PhoneActionResult:
    action_id: str
    status: PhoneActionStatus
    output: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
    approval_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "status": self.status.value,
            "output": dict(self.output),
            "error": self.error,
            "approval_id": self.approval_id,
        }
