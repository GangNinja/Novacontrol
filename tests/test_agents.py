from __future__ import annotations

import unittest

from novacontrol.agents import (
    AgentMessage,
    AgentMessageBus,
    AgentModule,
    AgentRegistry,
    AgentRole,
    AgentTask,
    AgentTaskStatus,
    CoordinatorAgent,
    build_default_agents,
)
from novacontrol.core.events import EventBus
from conftest import collect_events


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_coordinator_routes_coding_tasks(self) -> None:
        coordinator = CoordinatorAgent()

        role = coordinator.route_task("Implement the memory API")

        self.assertEqual(role, AgentRole.CODING)

    async def test_registry_delegates_to_specialized_agent(self) -> None:
        registry = AgentRegistry()
        for agent in build_default_agents():
            registry.register(agent)
        coordinator = CoordinatorAgent()

        response = await coordinator.delegate(AgentTask("Research vector databases"), registry)

        self.assertEqual(response.role, AgentRole.RESEARCH)
        self.assertEqual(response.status, AgentTaskStatus.COMPLETED)

    async def test_message_bus_records_and_delivers_messages(self) -> None:
        bus = AgentMessageBus()
        seen: list[AgentMessage] = []

        async def capture(message: AgentMessage) -> None:
            seen.append(message)

        await bus.subscribe("coding-agent", capture)
        await bus.send(
            AgentMessage(
                from_agent="coordinator-agent",
                to_agent="coding-agent",
                content="Build the feature",
            )
        )

        history = await bus.history(agent_name="coding-agent")

        self.assertEqual(seen[0].content, "Build the feature")
        self.assertEqual(history[0].to_agent, "coding-agent")

    async def test_agent_module_handles_task_events(self) -> None:
        event_bus = EventBus()
        registry = AgentRegistry()
        for agent in build_default_agents():
            registry.register(agent)
        module = AgentModule(registry)

        seen = await collect_events(
            event_bus, "agent.task_completed",
            "agent.task_requested",
            {"goal": "Create tests for the tool registry"},
            start_fn=module.start,
        )

        self.assertEqual(seen[0].payload["role"], AgentRole.TESTING.value)

    async def test_registry_rejects_duplicate_agents(self) -> None:
        registry = AgentRegistry()
        agent = build_default_agents()[0]
        registry.register(agent)

        with self.assertRaises(ValueError):
            registry.register(agent)


if __name__ == "__main__":
    unittest.main()
