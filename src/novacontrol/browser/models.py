"""Browser automation domain models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class BrowserActionType(StrEnum):
    NAVIGATE = "navigate"
    EXTRACT = "extract"
    FILL_FORM = "fill_form"
    DOWNLOAD = "download"
    TEST_WEB_APP = "test_web_app"
    SCREENSHOT = "screenshot"
    CLICK = "click"
    TYPE = "type"
    SCROLL = "scroll"
    GO_BACK = "go_back"
    PRESS_KEY = "press_key"
    UPLOAD = "upload"
    TAB_NEW = "tab_new"
    TAB_SWITCH = "tab_switch"
    TAB_CLOSE = "tab_close"
    TAB_LIST = "tab_list"
    WAIT_FOR = "wait_for"
    EVALUATE_JS = "evaluate_js"


class BrowserActionStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class BrowserAction:
    type: BrowserActionType
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
class BrowserWorkflow:
    name: str
    actions: tuple[BrowserAction, ...]
    id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "actions": [action.to_dict() for action in self.actions],
        }


@dataclass(frozen=True, slots=True)
class BrowserActionResult:
    action_id: str
    status: BrowserActionStatus
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
