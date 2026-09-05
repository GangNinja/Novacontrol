"""Tool registry and callable tool adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any

from novacontrol.core.security import PermissionScope
from novacontrol.tools.models import ToolSchema

ToolCallable = Callable[[Mapping[str, Any]], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    tool: FunctionTool
    schema: ToolSchema


class FunctionTool:
    """Adapter that exposes a Python callable through the tool protocol."""

    def __init__(
        self,
        name: str,
        schema: ToolSchema,
        function: ToolCallable,
        *,
        required_permissions: Sequence[PermissionScope] = (),
    ) -> None:
        self._name = name
        self.schema = schema
        self._function = function
        self._required_permissions = tuple(required_permissions)

    @property
    def name(self) -> str:
        return self._name

    @property
    def required_permissions(self) -> tuple[PermissionScope, ...]:
        return self._required_permissions

    async def run(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        result = self._function(arguments)
        if isawaitable(result):
            result = await result
        return dict(result)


class ToolRegistry:
    """Stores tool implementations and schemas."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, tool: FunctionTool, schema: ToolSchema | None = None) -> None:
        name = str(getattr(tool, "name"))
        tool_schema = schema or getattr(tool, "schema", None)
        if not isinstance(tool_schema, ToolSchema):
            raise TypeError(f"Tool {name!r} must provide a ToolSchema.")
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        self._tools[name] = RegisteredTool(tool=tool, schema=tool_schema)

    def get(self, name: str) -> RegisteredTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Tool is not registered: {name}") from exc

    def list(self) -> tuple[RegisteredTool, ...]:
        return tuple(self._tools[name] for name in sorted(self._tools))
