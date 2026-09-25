"""Tool-call validation: nothing invalid reaches the executor.

The pipeline the specification asks for is

    LLM/Planner -> ToolCall -> schema validation -> permission validation -> execution

and the point of it is the arrow that is *missing*: there is no path from a
language model's argument guess straight to a callable. ``ToolCall`` is the value
the planner produces, and :func:`validate_tool_call` is the gate — it answers
what is wrong with the arguments, in the schema's own terms, before anything has
a chance to run. The executor calls the same function, so a caller cannot get a
different answer by asking earlier.

The schema system is the project's own :class:`~novacontrol.tools.models.ToolSchema`
rather than a second framework (the specification allows either; a second one
would mean two places could disagree about what a valid call is). What this
module adds on top of it is the strictness that matters in practice:

  * an UNKNOWN argument is an error, not something to pass along — a model that
    invents ``{"path": ..., "recursive": true}`` for a tool that takes neither
    should not have its extra keys delivered to the implementation;
  * a required argument that is present but EMPTY counts as missing, because
    ``{"command": ""}`` runs nothing and reports success;
  * a type mismatch is reported with what was expected and what arrived.

Every error is a string a person can act on, and they are all reported together
rather than one at a time: a planner fixing three mistakes should not need three
round trips to discover them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from novacontrol.tools.models import ToolRequest, ToolSchema


@dataclass(frozen=True, slots=True)
class ToolCall:
    """What a planner wants to run: a tool name and the arguments for it."""

    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ToolCallValidation:
    """The verdict on one call, with every reason it was not accepted."""

    call: ToolCall
    errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    def request(self) -> ToolRequest:
        """The request to hand the executor — only if the call is valid.

        Raises rather than returning a half-checked request: an invalid call
        turned into a ``ToolRequest`` is exactly the object this module exists to
        keep out of the executor, and making that impossible is cheaper than
        trusting every caller to re-check the flag.
        """
        if self.errors:
            raise ValueError(
                f"Refusing to build a request for an invalid {self.call.tool_name!r} "
                f"call: {'; '.join(self.errors)}"
            )
        return ToolRequest(
            tool_name=self.call.tool_name,
            arguments=dict(self.call.arguments),
            reason=self.call.reason or None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "call": self.call.to_dict(),
            "valid": self.valid,
            "errors": list(self.errors),
        }


def validate_tool_call(schema: ToolSchema | None, call: ToolCall) -> ToolCallValidation:
    """Check one call against one schema (a missing schema validates nothing)."""
    if schema is None:
        return ToolCallValidation(call=call)
    if schema.name and schema.name != call.tool_name:
        return ToolCallValidation(
            call=call,
            errors=(
                f"Tool call names {call.tool_name!r} but the schema describes "
                f"{schema.name!r}.",
            ),
        )
    return ToolCallValidation(call=call, errors=schema.validate(call.arguments))


def describe_arguments(schema: ToolSchema) -> str:
    """The call shape, in one line — for a prompt or an error message."""
    if not schema.parameters:
        return f"{schema.name}()"
    parts = []
    for parameter in schema.parameters:
        label = parameter.name if parameter.required else f"[{parameter.name}]"
        parts.append(f"{label}: {parameter.type}")
    return f"{schema.name}({', '.join(parts)})"
