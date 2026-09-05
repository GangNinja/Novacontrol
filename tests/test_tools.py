"""Tests for tool registry, executor, and module."""

from __future__ import annotations

from collections.abc import Mapping
import unittest

from novacontrol.core.events import Event, EventBus
from novacontrol.core.security import PermissionScope
from novacontrol.tools import (
    FunctionTool,
    ToolExecutor,
    ToolModule,
    ToolParameter,
    ToolRegistry,
    ToolRequest,
    ToolSchema,
    ToolStatus,
)
from conftest import AllowGateway, collect_events


def make_echo_tool() -> FunctionTool:
    return FunctionTool(
        "echo",
        ToolSchema(
            name="echo", description="Echo a message",
            parameters=(ToolParameter("message", "string", required=True),),
        ),
        lambda args: {"echo": args["message"]},
    )


class ToolTests(unittest.IsolatedAsyncioTestCase):

    async def test_executor_runs_safe_tool(self) -> None:
        registry = ToolRegistry()
        registry.register(make_echo_tool())
        result = await ToolExecutor(registry).execute(ToolRequest("echo", {"message": "hello"}))
        self.assertEqual(result.status, ToolStatus.COMPLETED)
        self.assertEqual(result.output["echo"], "hello")

    async def test_executor_validates_schema(self) -> None:
        registry = ToolRegistry()
        registry.register(make_echo_tool())
        result = await ToolExecutor(registry).execute(ToolRequest("echo", {}))
        self.assertEqual(result.status, ToolStatus.FAILED)
        self.assertIn("Missing required argument", result.error or "")

    async def test_sensitive_tool_denied_without_approval(self) -> None:
        registry = ToolRegistry()
        registry.register(FunctionTool(
            "write_file", ToolSchema("write_file", "Write a file"),
            lambda args: {"ok": True},
            required_permissions=(PermissionScope.FILESYSTEM_WRITE,),
        ))
        result = await ToolExecutor(registry).execute(ToolRequest("write_file"))
        self.assertEqual(result.status, ToolStatus.DENIED)

    async def test_sensitive_tool_runs_when_approved(self) -> None:
        registry = ToolRegistry()
        registry.register(FunctionTool(
            "network", ToolSchema("network", "Use the network"),
            lambda args: {"ok": True},
            required_permissions=(PermissionScope.NETWORK_ACCESS,),
        ))
        result = await ToolExecutor(registry, approval_gateway=AllowGateway()).execute(
            ToolRequest("network")
        )
        self.assertEqual(result.status, ToolStatus.COMPLETED)

    async def test_tool_module_emits_event(self) -> None:
        bus = EventBus()
        registry = ToolRegistry()
        registry.register(make_echo_tool())
        module = ToolModule(ToolExecutor(registry))
        seen = await collect_events(
            bus, "tool.execution_completed",
            "tool.execute_requested",
            {"tool_name": "echo", "arguments": {"message": "hello"}},
            start_fn=module.start,
        )
        self.assertEqual(seen[0].payload["output"]["echo"], "hello")

    async def test_registry_rejects_duplicates(self) -> None:
        registry = ToolRegistry()
        registry.register(make_echo_tool())
        with self.assertRaises(ValueError):
            registry.register(make_echo_tool())


if __name__ == "__main__":
    unittest.main()
