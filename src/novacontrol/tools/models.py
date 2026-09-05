"""Tool domain models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class ToolStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class ToolParameter:
    name: str
    type: str = "string"
    required: bool = False
    description: str = ""


@dataclass(frozen=True, slots=True)
class ToolSchema:
    name: str
    description: str
    parameters: tuple[ToolParameter, ...] = ()

    def validate(self, arguments: Mapping[str, Any]) -> tuple[str, ...]:
        errors: list[str] = []
        for parameter in self.parameters:
            if parameter.required and parameter.name not in arguments:
                errors.append(f"Missing required argument: {parameter.name}")
                continue
            if parameter.name in arguments and not _matches_type(
                arguments[parameter.name],
                parameter.type,
            ):
                errors.append(
                    f"Argument {parameter.name!r} must be {parameter.type}, "
                    f"got {type(arguments[parameter.name]).__name__}"
                )
        return tuple(errors)


@dataclass(frozen=True, slots=True)
class ToolRequest:
    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True, slots=True)
class ToolResult:
    request_id: str
    tool_name: str
    status: ToolStatus
    output: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
    approval_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "tool_name": self.tool_name,
            "status": self.status.value,
            "output": dict(self.output),
            "error": self.error,
            "approval_id": self.approval_id,
        }


def _matches_type(value: Any, expected: str) -> bool:
    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "object": Mapping,
        "array": list,
    }
    expected_type = type_map.get(expected)
    if expected_type is None:
        raise ValueError(f"Unsupported tool parameter type: {expected}")
    if expected == "integer" and isinstance(value, bool):
        return False
    if expected == "number" and isinstance(value, bool):
        return False
    return isinstance(value, expected_type)  # type: ignore[arg-type]
