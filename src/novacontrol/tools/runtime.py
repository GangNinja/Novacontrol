"""Event-driven tool manager runtime module."""

from __future__ import annotations

from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import Capability
from novacontrol.tools.executor import ToolExecutor
from novacontrol.tools.models import ToolRequest


class ToolModule:
    """Runtime module that executes tools from events."""

    def __init__(self, executor: ToolExecutor) -> None:
        self.executor = executor
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "tools"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability("tools.registry", "Register and list tools."),
            Capability("tools.execute", "Execute approved tools."),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        await event_bus.subscribe("tool.execute_requested", self._handle_execute)

    async def stop(self) -> None:
        self._event_bus = None

    async def _handle_execute(self, event: Event) -> None:
        payload = event.payload
        request_data = {
            "tool_name": str(payload["tool_name"]),
            "arguments": payload.get("arguments", {}),
            "reason": payload.get("reason"),
        }
        if payload.get("request_id"):
            request_data["id"] = str(payload["request_id"])
        request = ToolRequest(**request_data)
        result = await self.executor.execute(request)
        if self._event_bus is not None:
            await self._event_bus.publish(
                Event(
                    type=f"tool.execution_{result.status.value}",
                    payload=result.to_dict(),
                    source="tools",
                    correlation_id=event.correlation_id,
                    causation_id=event.correlation_id,
                )
            )
