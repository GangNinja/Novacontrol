"""Plugin marketplace domain models."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
import json
from pathlib import Path
from typing import Any


class PluginStatus(StrEnum):
    DISCOVERED = "discovered"
    INSTALLED = "installed"
    ENABLED = "enabled"
    DISABLED = "disabled"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class PluginCapability:
    name: str
    type: str
    entrypoint: str | None = None
    description: str = ""

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "PluginCapability":
        return cls(
            name=str(data["name"]),
            type=str(data["type"]),
            entrypoint=data.get("entrypoint"),
            description=str(data.get("description", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "entrypoint": self.entrypoint,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class PluginManifest:
    name: str
    version: str
    description: str = ""
    permissions: tuple[str, ...] = ()
    capabilities: tuple[PluginCapability, ...] = ()
    min_core_version: str = "0.1.0"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Plugin name is required.")
        if not self.version.strip():
            raise ValueError("Plugin version is required.")

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "PluginManifest":
        return cls(
            name=str(data["name"]),
            version=str(data["version"]),
            description=str(data.get("description", "")),
            permissions=tuple(str(permission) for permission in data.get("permissions", ())),
            capabilities=tuple(
                PluginCapability.from_mapping(capability)
                for capability in data.get("capabilities", ())
            ),
            min_core_version=str(data.get("min_core_version", "0.1.0")),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "PluginManifest":
        return cls.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "permissions": self.permissions,
            "capabilities": [capability.to_dict() for capability in self.capabilities],
            "min_core_version": self.min_core_version,
        }


@dataclass(frozen=True, slots=True)
class PluginInstallRecord:
    manifest: PluginManifest
    status: PluginStatus
    trusted: bool = False
    approval_id: str | None = None
    installed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def with_updates(
        self,
        *,
        status: PluginStatus | None = None,
        trusted: bool | None = None,
    ) -> "PluginInstallRecord":
        return replace(
            self,
            status=self.status if status is None else status,
            trusted=self.trusted if trusted is None else trusted,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.to_dict(),
            "status": self.status.value,
            "trusted": self.trusted,
            "approval_id": self.approval_id,
            "installed_at": self.installed_at.isoformat(),
        }
