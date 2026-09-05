"""Plugin marketplace manager."""

from __future__ import annotations

from pathlib import Path

from novacontrol.core.security import (
    ApprovalGateway,
    ApprovalRequest,
    DenyByDefaultApprovalGateway,
    PermissionScope,
    RiskLevel,
)
from novacontrol.plugins.models import PluginInstallRecord, PluginManifest, PluginStatus
from novacontrol.plugins.repository import InMemoryPluginRepository, PluginRepository


class PluginMarketplace:
    """Discovers, installs, trusts, enables, and disables plugins."""

    def __init__(
        self,
        *,
        repository: PluginRepository | None = None,
        approval_gateway: ApprovalGateway | None = None,
    ) -> None:
        self.repository = repository or InMemoryPluginRepository()
        self.approval_gateway = approval_gateway or DenyByDefaultApprovalGateway()

    async def discover(self, directory: str | Path) -> tuple[PluginManifest, ...]:
        root = Path(directory)
        manifests: list[PluginManifest] = []
        for path in root.rglob("plugin.json"):
            manifests.append(PluginManifest.from_file(path))
        for manifest in manifests:
            await self.repository.save(
                PluginInstallRecord(manifest=manifest, status=PluginStatus.DISCOVERED)
            )
        return tuple(manifests)

    async def install(self, manifest: PluginManifest) -> PluginInstallRecord:
        permissions = _permission_scopes(manifest.permissions)
        if permissions:
            approval = await self.approval_gateway.request_approval(
                ApprovalRequest(
                    action=f"Install plugin {manifest.name}",
                    reason="Plugin installation requests permissions.",
                    permissions=permissions,
                    risk=RiskLevel.HIGH,
                    metadata=manifest.to_dict(),
                )
            )
            if not approval.approved:
                record = PluginInstallRecord(
                    manifest=manifest,
                    status=PluginStatus.DENIED,
                    trusted=False,
                    approval_id=approval.request_id,
                )
                await self.repository.save(record)
                return record

        record = PluginInstallRecord(
            manifest=manifest,
            status=PluginStatus.INSTALLED,
            trusted=not permissions,
        )
        await self.repository.save(record)
        return record

    async def trust(self, plugin_name: str) -> PluginInstallRecord:
        record = await self.repository.get(plugin_name)
        updated = record.with_updates(trusted=True)
        await self.repository.save(updated)
        return updated

    async def enable(self, plugin_name: str) -> PluginInstallRecord:
        record = await self.repository.get(plugin_name)
        if not record.trusted and record.manifest.permissions:
            raise PermissionError(f"Plugin must be trusted before enabling: {plugin_name}")
        updated = record.with_updates(status=PluginStatus.ENABLED)
        await self.repository.save(updated)
        return updated

    async def disable(self, plugin_name: str) -> PluginInstallRecord:
        record = await self.repository.get(plugin_name)
        updated = record.with_updates(status=PluginStatus.DISABLED)
        await self.repository.save(updated)
        return updated

    async def list(self) -> tuple[PluginInstallRecord, ...]:
        return tuple(await self.repository.list())


def _permission_scopes(values: tuple[str, ...]) -> tuple[PermissionScope, ...]:
    scopes: list[PermissionScope] = []
    for value in values:
        try:
            scopes.append(PermissionScope(value))
        except ValueError:
            scopes.append(PermissionScope.NETWORK_ACCESS)
    return tuple(scopes)
