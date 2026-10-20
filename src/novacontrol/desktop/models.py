"""Desktop automation domain models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class DesktopActionType(StrEnum):
    OPEN_APPLICATION = "open_application"
    CLOSE_APPLICATION = "close_application"
    ORGANIZE_FILES = "organize_files"
    EXECUTE_SCRIPT = "execute_script"
    TAKE_SCREENSHOT = "take_screenshot"
    LIST_PROCESSES = "list_processes"
    LIST_FILES = "list_files"
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    SYSTEM_INFO = "system_info"
    KEYBOARD_SHORTCUT = "keyboard_shortcut"
    TYPE_TEXT = "type_text"
    OPEN_FOLDER = "open_folder"
    SEARCH_START_MENU = "search_start_menu"
    WEB_SEARCH = "web_search"
    # Vision-guided in-app action: click a labeled element on screen.
    VISION_CLICK = "vision_click"
    # Coordinate-addressed input (Phase C of the game-control audit). These are
    # the explicit, geometry-aware primitives a game needs: move to an exact
    # physical-pixel point, click it, and hold/release a mouse button or a key
    # so a press is guaranteed to be matched by a release. Each travels through
    # the same approval, permission and audit layers as every other action.
    MOUSE_MOVE = "mouse_move"
    MOUSE_CLICK = "mouse_click"
    MOUSE_BUTTON_DOWN = "mouse_button_down"
    MOUSE_BUTTON_UP = "mouse_button_up"
    KEY_DOWN = "key_down"
    KEY_UP = "key_up"
    # Target-window geometry (bounds, client area, DPI scale, foreground state)
    # — the evidence an input action needs BEFORE it addresses a coordinate.
    WINDOW_GEOMETRY = "window_geometry"
    # Direct game launch via platform deep link (steam://rungameid/<appid>).
    GAME_LAUNCH = "game_launch"
    # App deep-link navigation (steam://open/library, ms-settings:<page>, ...).
    APP_NAVIGATE = "app_navigate"
    # Vision-verified stop: close the most recently focused app window.
    STOP_APP = "stop_app"


class DesktopActionStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class DesktopAction:
    type: DesktopActionType
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
class DesktopWorkflow:
    name: str
    actions: tuple[DesktopAction, ...]
    id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "actions": [action.to_dict() for action in self.actions],
        }


@dataclass(frozen=True, slots=True)
class DesktopActionResult:
    action_id: str
    status: DesktopActionStatus
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
