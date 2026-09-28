"""Plugin domain models.

Phase 13 defined a plugin as a MANIFEST — what a marketplace can install. Phase
10 adds the half a running system needs: the same manifest carrying its SECURITY
DECLARATIONS (risk, required capabilities, external services, filesystem and
network access), the lifecycle vocabulary a plugin manager moves a plugin
through, and :class:`PluginRecord`, the per-plugin runtime state that says where
a plugin currently is and why it is not somewhere further along.

The vocabulary is deliberately one set of names, not two. A ``PluginStatus``
covers both the install-time states the marketplace already used
(DISCOVERED/INSTALLED/ENABLED/DISABLED/DENIED) and the lifecycle states the SDK
adds (LOADED/INITIALIZED/UNLOADED/FAILED), so a status read anywhere means the
same thing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from novacontrol.core.security import RiskLevel


class PluginStatus(StrEnum):
    DISCOVERED = "discovered"
    LOADED = "loaded"
    INITIALIZED = "initialized"
    INSTALLED = "installed"
    ENABLED = "enabled"
    DISABLED = "disabled"
    UNLOADED = "unloaded"
    DENIED = "denied"
    FAILED = "failed"


#: The configuration field types a plugin may declare. Kept small on purpose:
#: every type here is one the manifest can state and the manager can check
#: without guessing, and a plugin that needs something richer can accept a
#: ``string`` and parse it itself.
CONFIGURATION_TYPES: frozenset[str] = frozenset(
    {"string", "integer", "number", "boolean", "list"}
)


@dataclass(frozen=True, slots=True)
class PluginConfigurationField:
    """One declared configuration knob: the plugin's configuration schema.

    A field says what the plugin READS and what it will accept, so a manager can
    refuse to start a plugin whose required settings are missing instead of
    discovering that when the first call arrives.
    """

    name: str
    type: str = "string"
    required: bool = False
    default: Any = None
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("A configuration field needs a name.")
        if self.type not in CONFIGURATION_TYPES:
            raise ValueError(
                f"Configuration field {self.name!r} has unsupported type {self.type!r}. "
                f"Valid types: {', '.join(sorted(CONFIGURATION_TYPES))}"
            )

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> PluginConfigurationField:
        return cls(
            name=str(data["name"]),
            type=str(data.get("type", "string")),
            required=bool(data.get("required", False)),
            default=data.get("default"),
            description=str(data.get("description", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "default": self.default,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class PluginCapability:
    name: str
    type: str
    entrypoint: str | None = None
    description: str = ""

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> PluginCapability:
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
    """A plugin's declaration: identity, capabilities, and what it may touch.

    ``plugin_id`` is the STABLE identity and ``name`` the display label; a
    manifest that declares no id gets one derived from its name, so existing
    ``plugin.json`` files keep working unchanged while a plugin that cares about
    keeping its identity across a rename can say so.
    """

    name: str
    version: str
    description: str = ""
    permissions: tuple[str, ...] = ()
    capabilities: tuple[PluginCapability, ...] = ()
    min_core_version: str = "0.1.0"
    # -- Phase 10: identity and security declarations -------------------------
    plugin_id: str = ""
    author: str = ""
    risk: RiskLevel = RiskLevel.LOW
    required_capabilities: tuple[str, ...] = ()
    external_services: tuple[str, ...] = ()
    filesystem_access: tuple[str, ...] = ()
    network_access: bool = False
    configuration: tuple[PluginConfigurationField, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Plugin name is required.")
        if not self.version.strip():
            raise ValueError("Plugin version is required.")
        if not self.plugin_id.strip():
            object.__setattr__(self, "plugin_id", _slug(self.name))

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> PluginManifest:
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
            plugin_id=str(data.get("plugin_id", "")),
            author=str(data.get("author", "")),
            risk=RiskLevel(str(data.get("risk", RiskLevel.LOW.value))),
            required_capabilities=tuple(
                str(name) for name in data.get("required_capabilities", ())
            ),
            external_services=tuple(
                str(service) for service in data.get("external_services", ())
            ),
            filesystem_access=tuple(str(path) for path in data.get("filesystem_access", ())),
            network_access=bool(data.get("network_access", False)),
            configuration=tuple(
                PluginConfigurationField.from_mapping(field)
                for field in data.get("configuration", ())
            ),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> PluginManifest:
        return cls.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "permissions": self.permissions,
            "capabilities": [capability.to_dict() for capability in self.capabilities],
            "min_core_version": self.min_core_version,
            "plugin_id": self.plugin_id,
            "author": self.author,
            "risk": self.risk.value,
            "required_capabilities": self.required_capabilities,
            "external_services": self.external_services,
            "filesystem_access": self.filesystem_access,
            "network_access": self.network_access,
            "configuration": [field.to_dict() for field in self.configuration],
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
    ) -> PluginInstallRecord:
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


@dataclass(frozen=True, slots=True)
class PluginRecord:
    """Where one plugin currently is, and what stopped it going further.

    Distinct from :class:`PluginInstallRecord` on purpose: that record is the
    marketplace's durable answer to "may this be installed?", and this one is
    the SDK's in-process answer to "is it loaded, enabled, and working right
    now?". ``error`` is empty for a plugin that has not failed, and carries the
    reason — never swallowed — for one that has.
    """

    plugin_id: str
    manifest: PluginManifest
    status: PluginStatus
    error: str = ""
    approval_id: str | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def ok(self) -> bool:
        return self.status is not PluginStatus.FAILED

    @property
    def enabled(self) -> bool:
        return self.status is PluginStatus.ENABLED

    def with_updates(
        self,
        *,
        status: PluginStatus | None = None,
        error: str | None = None,
        approval_id: str | None = None,
    ) -> PluginRecord:
        return replace(
            self,
            status=self.status if status is None else status,
            error=self.error if error is None else error,
            approval_id=self.approval_id if approval_id is None else approval_id,
            updated_at=datetime.now(UTC),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "manifest": self.manifest.to_dict(),
            "status": self.status.value,
            "error": self.error,
            "approval_id": self.approval_id,
            "updated_at": self.updated_at.isoformat(),
        }


def _slug(value: str) -> str:
    """A stable id from a display name: lowercase, punctuation collapsed."""
    chars = [char if char.isalnum() else "-" for char in str(value).strip().lower()]
    collapsed = "".join(chars)
    while "--" in collapsed:
        collapsed = collapsed.replace("--", "-")
    return collapsed.strip("-") or "plugin"


__all__ = [
    "CONFIGURATION_TYPES",
    "PluginCapability",
    "PluginConfigurationField",
    "PluginInstallRecord",
    "PluginManifest",
    "PluginRecord",
    "PluginStatus",
]
