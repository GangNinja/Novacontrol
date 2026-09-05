"""Plugin repository implementations."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from novacontrol.plugins.models import PluginInstallRecord


@runtime_checkable
class PluginRepository(Protocol):
    async def save(self, record: PluginInstallRecord) -> None:
        """Store a plugin install record."""

    async def get(self, plugin_name: str) -> PluginInstallRecord:
        """Get a plugin install record."""

    async def list(self) -> Sequence[PluginInstallRecord]:
        """List plugin install records."""


class InMemoryPluginRepository:
    def __init__(self) -> None:
        self._records: dict[str, PluginInstallRecord] = {}

    async def save(self, record: PluginInstallRecord) -> None:
        self._records[record.manifest.name] = record

    async def get(self, plugin_name: str) -> PluginInstallRecord:
        try:
            return self._records[plugin_name]
        except KeyError as exc:
            raise KeyError(f"Plugin is not installed: {plugin_name}") from exc

    async def list(self) -> Sequence[PluginInstallRecord]:
        return tuple(self._records[name] for name in sorted(self._records))
