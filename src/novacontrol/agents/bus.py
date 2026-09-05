"""Agent-to-agent message bus."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence

from novacontrol.agents.models import AgentMessage

AgentMessageHandler = Callable[[AgentMessage], Awaitable[None]]


class AgentMessageBus:
    """In-process message bus for agent communication."""

    def __init__(self) -> None:
        self._messages: list[AgentMessage] = []
        self._handlers: dict[str, list[AgentMessageHandler]] = defaultdict(list)

    async def subscribe(self, recipient: str, handler: AgentMessageHandler) -> None:
        self._handlers[recipient].append(handler)

    async def send(self, message: AgentMessage) -> None:
        self._messages.append(message)
        handlers = [*self._handlers.get(message.to_agent, ()), *self._handlers.get("*", ())]
        for handler in handlers:
            await handler(message)

    async def history(self, *, agent_name: str | None = None) -> Sequence[AgentMessage]:
        if agent_name is None:
            return tuple(self._messages)
        return tuple(
            message
            for message in self._messages
            if message.from_agent == agent_name or message.to_agent == agent_name
        )
