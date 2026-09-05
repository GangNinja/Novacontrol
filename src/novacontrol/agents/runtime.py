"""Event-driven multi-agent runtime module."""

from __future__ import annotations

from typing import Any

from novacontrol.agents.coordinator import CoordinatorAgent
from novacontrol.agents.models import AgentRole, AgentTask
from novacontrol.agents.registry import AgentRegistry
from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import Capability


class AgentModule:
    """Runtime module that delegates incoming tasks to agents."""

    def __init__(self, registry: AgentRegistry, coordinator: CoordinatorAgent | None = None) -> None:
        self.registry = registry
        self.coordinator = coordinator or CoordinatorAgent()
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "agents"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability("agents.delegate", "Delegate tasks to specialized agents."),
            Capability("agents.progress", "Publish agent task progress."),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        await event_bus.subscribe("agent.task_requested", self._handle_task_requested)

    async def stop(self) -> None:
        self._event_bus = None

    async def _handle_task_requested(self, event: Event) -> None:
        payload = event.payload
        role = payload.get("role")
        task_id = str(payload["task_id"]) if payload.get("task_id") else None
        task_kwargs: dict[str, Any] = {
            "goal": str(payload["goal"]),
            "role": AgentRole(str(role)) if role else None,
            "metadata": dict(payload.get("metadata", {})),
        }
        if task_id is not None:
            task_kwargs["id"] = task_id
        task = AgentTask(**task_kwargs)
        response = await self.coordinator.delegate(task, self.registry)
        if self._event_bus is None:
            return
        await self._event_bus.publish(
            Event(
                type=f"agent.task_{response.status.value}",
                payload=response.to_dict(),
                source="agents",
                correlation_id=event.correlation_id,
                causation_id=event.correlation_id,
            )
        )
