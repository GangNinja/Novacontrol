"""External integration registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class IntegrationStatus(StrEnum):
    CONFIGURED = "configured"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class IntegrationDefinition:
    name: str
    provider: str
    status: IntegrationStatus = IntegrationStatus.CONFIGURED
    settings: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "status": self.status.value,
            "settings": self.settings,
        }


class IntegrationRegistry:
    def __init__(self) -> None:
        self._integrations: dict[str, IntegrationDefinition] = {}

    def register(self, integration: IntegrationDefinition) -> None:
        if integration.name in self._integrations:
            raise ValueError(f"Integration already registered: {integration.name}")
        self._integrations[integration.name] = integration

    def get(self, name: str) -> IntegrationDefinition:
        try:
            return self._integrations[name]
        except KeyError as exc:
            raise KeyError(f"Integration not registered: {name}") from exc

    def list(self) -> tuple[IntegrationDefinition, ...]:
        return tuple(self._integrations[name] for name in sorted(self._integrations))
