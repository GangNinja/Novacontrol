"""Shared interface contracts for NovaControl modules."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from novacontrol.core.events import Event, EventBus


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    description: str
    permissions: Sequence[str] = field(default_factory=tuple)


@runtime_checkable
class RuntimeModule(Protocol):
    """Lifecycle contract implemented by every module registered with the core."""

    @property
    def name(self) -> str:
        """Stable module name."""

    @property
    def capabilities(self) -> Sequence[Capability]:
        """Capabilities exposed by the module."""

    async def start(self, event_bus: EventBus) -> None:
        """Start the module and subscribe to relevant events."""

    async def stop(self) -> None:
        """Stop the module and release resources."""


@runtime_checkable
class Agent(Protocol):
    @property
    def name(self) -> str:
        """Stable agent name."""

    async def handle(self, event: Event) -> Event | None:
        """Handle an event and optionally emit a follow-up event."""


@runtime_checkable
class Tool(Protocol):
    @property
    def name(self) -> str:
        """Stable tool name."""

    @property
    def required_permissions(self) -> Sequence[str]:
        """Permission scopes required to run this tool."""

    async def run(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        """Run the tool with structured arguments."""


@runtime_checkable
class Skill(Protocol):
    @property
    def name(self) -> str:
        """Stable skill name."""

    async def invoke(self, event: Event) -> Event | None:
        """Invoke a skill in response to an event."""


@runtime_checkable
class MemoryStore(Protocol):
    async def put(self, namespace: str, key: str, value: Mapping[str, Any]) -> None:
        """Store a memory record."""

    async def get(self, namespace: str, key: str) -> Mapping[str, Any] | None:
        """Retrieve a memory record."""

    async def search(self, namespace: str, query: str, limit: int = 10) -> Sequence[Mapping[str, Any]]:
        """Search memory records."""


@runtime_checkable
class LLMProvider(Protocol):
    @property
    def name(self) -> str:
        """Provider name."""

    def complete(self, messages: Sequence[Mapping[str, str]], **kwargs: Any) -> Awaitable[str]:
        """Return a text completion."""
