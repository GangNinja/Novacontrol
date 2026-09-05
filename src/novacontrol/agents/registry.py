"""Agent registry."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from novacontrol.agents.models import AgentResponse, AgentRole, AgentTask


@runtime_checkable
class RunnableAgent(Protocol):
    @property
    def name(self) -> str:
        """Stable agent name."""

    role: AgentRole

    async def handle_task(self, task: AgentTask) -> AgentResponse:
        """Handle an assigned agent task."""


class AgentRegistry:
    """Stores runnable agents by name and role."""

    def __init__(self) -> None:
        self._agents: dict[str, RunnableAgent] = {}

    def register(self, agent: RunnableAgent) -> None:
        if agent.name in self._agents:
            raise ValueError(f"Agent already registered: {agent.name}")
        self._agents[agent.name] = agent

    def get(self, name: str) -> RunnableAgent:
        try:
            return self._agents[name]
        except KeyError as exc:
            raise KeyError(f"Agent is not registered: {name}") from exc

    def by_role(self, role: AgentRole) -> Sequence[RunnableAgent]:
        return tuple(agent for agent in self._agents.values() if agent.role == role)

    def first_by_role(self, role: AgentRole) -> RunnableAgent | None:
        agents = self.by_role(role)
        return agents[0] if agents else None

    def list(self) -> tuple[RunnableAgent, ...]:
        return tuple(self._agents[name] for name in sorted(self._agents))
