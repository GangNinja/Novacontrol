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
    """Stores runnable agents by name and role.

    Roles can hold more than one agent (many things diagnose), so "which one
    runs?" needs an answer that is stated rather than accidental. It used to be
    registration order, which made a real specialist impossible to install
    without deleting the placeholder it replaces. ``prefer=True`` now puts an
    agent at the front of its role, and everything else keeps registration
    order.
    """

    def __init__(self) -> None:
        self._agents: dict[str, RunnableAgent] = {}
        self._order: list[str] = []

    def register(self, agent: RunnableAgent, *, prefer: bool = False) -> None:
        if agent.name in self._agents:
            raise ValueError(f"Agent already registered: {agent.name}")
        self._agents[agent.name] = agent
        if prefer:
            self._order.insert(0, agent.name)
        else:
            self._order.append(agent.name)

    def unregister(self, name: str) -> RunnableAgent:
        """Remove an agent and return it (a specialist can be swapped out)."""
        try:
            agent = self._agents.pop(name)
        except KeyError as exc:
            raise KeyError(f"Agent is not registered: {name}") from exc
        self._order.remove(name)
        return agent

    def get(self, name: str) -> RunnableAgent:
        try:
            return self._agents[name]
        except KeyError as exc:
            raise KeyError(f"Agent is not registered: {name}") from exc

    def by_role(self, role: AgentRole) -> Sequence[RunnableAgent]:
        return tuple(
            self._agents[name] for name in self._order if self._agents[name].role == role
        )

    def first_by_role(self, role: AgentRole) -> RunnableAgent | None:
        agents = self.by_role(role)
        return agents[0] if agents else None

    def list(self) -> tuple[RunnableAgent, ...]:
        return tuple(self._agents[name] for name in sorted(self._agents))
