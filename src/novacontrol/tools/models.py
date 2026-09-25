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
    """What a valid call to one tool looks like.

    ``validate`` is the gate the executor runs before anything else, so it is
    strict on purpose: an argument the schema does not describe is an ERROR, not
    something to pass along. A model that invents ``{"recursive": true}`` for a
    tool that takes nothing should be told its argument was refused, rather than
    have it delivered to an implementation that may half-honour it. A tool that
    genuinely takes a free-form mapping says so with
    ``allow_extra_arguments=True``.
    """

    name: str
    description: str
    parameters: tuple[ToolParameter, ...] = ()
    #: Refuse undeclared arguments (off only when a tool really takes anything).
    allow_extra_arguments: bool = False

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for parameter in self.parameters:
            if parameter.name in seen:
                raise ValueError(f"Tool {self.name!r} declares {parameter.name!r} twice.")
            seen.add(parameter.name)
            if parameter.type not in _TYPE_MAP:
                # Caught here rather than at validation time: a schema nobody can
                # satisfy should fail when it is registered, not when a request
                # is already in flight.
                raise ValueError(
                    f"Tool {self.name!r} declares an unsupported type "
                    f"{parameter.type!r} for {parameter.name!r}."
                )

    def validate(self, arguments: Mapping[str, Any]) -> tuple[str, ...]:
        """Everything wrong with these arguments, all of it at once."""
        errors: list[str] = []
        declared = {parameter.name for parameter in self.parameters}
        if not self.allow_extra_arguments:
            for key in arguments:
                if str(key) not in declared:
                    errors.append(f"Unexpected argument: {key}")
        for parameter in self.parameters:
            if parameter.name not in arguments:
                if parameter.required:
                    errors.append(f"Missing required argument: {parameter.name}")
                continue
            value = arguments[parameter.name]
            if parameter.required and _is_empty(value):
                # ``{"command": ""}`` runs nothing and reports success.
                errors.append(
                    f"Required argument {parameter.name!r} was given no value"
                )
                continue
            if not _matches_type(value, parameter.type):
                errors.append(
                    f"Argument {parameter.name!r} must be {parameter.type}, "
                    f"got {type(value).__name__}"
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
    #: True when this result came from the cache. Reported so a surprising
    #: number or a suspiciously fast call is visible rather than mysterious.
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "tool_name": self.tool_name,
            "status": self.status.value,
            "output": dict(self.output),
            "error": self.error,
            "approval_id": self.approval_id,
            "cached": self.cached,
        }


#: Schema type name -> the Python type a value must be. The single table both
#: schema construction and argument validation read.
_TYPE_MAP: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": Mapping,
    "array": list,
}


def _is_empty(value: Any) -> bool:
    """Whether a REQUIRED argument was given no value at all.

    Only ``None`` and blank text count. An empty list is a real answer for a
    tool that takes a collection — "organize these files: none" — so it is not
    treated as a missing value.
    """
    if value is None:
        return True
    return isinstance(value, str) and not value.strip()


def _matches_type(value: Any, expected: str) -> bool:
    expected_type = _TYPE_MAP[expected]
    if expected in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, expected_type)
