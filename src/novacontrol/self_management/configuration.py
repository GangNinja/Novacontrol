"""Versioned, validated configuration snapshots and safe rollback.

The application receives its file configuration at boot, but its persisted
user settings are the part that can change at runtime.  Those settings are
validated by the same ``SettingsManager`` the application uses, snapshotted
before risky work, and restored only from a known-good version.  The current
snapshot is never overwritten in place; a rollback always adds a new version.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

from novacontrol.self_management.models import (
    ConfigReport,
    ConfigVersion,
    checksum,
    utcnow,
)
from novacontrol.settings.manager import SettingsManager


class ConfigurationManager:
    """Persisted settings history with integrity checks and known-good restore."""

    NAMESPACE = "self_management_config"
    SCHEMA = "phase34.1"

    def __init__(
        self,
        store: Any | None,
        settings_provider: Callable[[], Mapping[str, Any]],
        *,
        max_versions: int = 20,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self._settings_provider = settings_provider
        self.max_versions = max(3, int(max_versions))
        self._clock = clock or utcnow
        self._versions: list[ConfigVersion] = []
        self._recovered = False
        self._recovery_note = ""
        self._load()

    def _load(self) -> None:
        if self.store is None:
            return
        payload = self.store.read(self.NAMESPACE)
        rows = payload.get("versions", ()) if isinstance(payload, Mapping) else ()
        loaded: list[ConfigVersion] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            try:
                settings = dict(row.get("settings") or {})
                version = ConfigVersion(
                    version=int(row["version"]),
                    created_at=datetime.fromisoformat(str(row["created_at"])),
                    checksum=str(row["checksum"]),
                    settings=settings,
                    note=str(row.get("note", "")),
                    known_good=bool(row.get("known_good", True)),
                )
                # A stored checksum that no longer matches its body is not a
                # known-good version.  Keep it out of the restore set, but do
                # not fail boot over one damaged historical row.
                if version.checksum == checksum(settings):
                    loaded.append(version)
            except (KeyError, TypeError, ValueError):
                continue
        self._versions = sorted(loaded, key=lambda item: item.version)[-self.max_versions :]

    def _save(self) -> None:
        if self.store is None:
            return
        try:
            self.store.write(
                self.NAMESPACE,
                {
                    "schema": self.SCHEMA,
                    "versions": [row.to_dict() for row in self._versions],
                },
            )
        except Exception:
            return

    # -- validation ----------------------------------------------------------
    def validate(self, settings: Mapping[str, Any] | None) -> tuple[bool, tuple[str, ...]]:
        if not isinstance(settings, Mapping):
            return False, ("configuration is not a mapping",)
        try:
            parsed = SettingsManager.from_dict(dict(settings))
        except Exception as exc:
            return False, (f"configuration validation failed: {type(exc).__name__}: {exc}",)
        issues: list[str] = []
        # The manager clamps some values on read; a raw value present but
        # outside its own vocabulary is corruption, not a silent default.
        from novacontrol.optimization.models import ExecutionMode
        from novacontrol.settings.models import BRAIN_MODES, ApprovalMode

        if "brain_mode" in settings and str(settings.get("brain_mode")) not in BRAIN_MODES:
            issues.append("brain_mode is not a supported mode")
        if "execution_mode" in settings and str(settings.get("execution_mode")) not in {
            mode.value for mode in ExecutionMode
        }:
            issues.append("execution_mode is not a supported mode")
        if "approval_mode" in settings and str(settings.get("approval_mode")) not in {
            mode.value for mode in ApprovalMode
        }:
            issues.append("approval_mode is not a supported mode")
        if not parsed.settings.approval_mode.value:
            issues.append("approval_mode is empty")
        return not issues, tuple(issues)

    # -- snapshots -----------------------------------------------------------
    def current(self) -> Mapping[str, Any]:
        # The persisted body is the configuration on disk.  The live provider is
        # the fallback for a first boot with no snapshot yet; preferring the
        # live object would hide a corrupt persisted file behind its in-memory
        # default, which is exactly the failure this layer exists to catch.
        if self.store is not None:
            stored = self.store.read("settings")
            if isinstance(stored, Mapping) and stored:
                return dict(stored)
        return dict(self._settings_provider())

    def checksum_current(self) -> str:
        return checksum(self.current())

    def snapshot(
        self,
        settings: Mapping[str, Any] | None = None,
        *,
        note: str = "",
        known_good: bool | None = None,
    ) -> ConfigVersion:
        payload = dict(settings if settings is not None else self.current())
        valid, issues = self.validate(payload)
        version = ConfigVersion(
            version=(self._versions[-1].version + 1) if self._versions else 1,
            created_at=self._clock(),
            checksum=checksum(payload),
            settings=payload,
            note=note or ("validated runtime settings" if valid else "; ".join(issues)),
            known_good=valid if known_good is None else bool(known_good and valid),
        )
        # A duplicate checksum is still a history event (the operator asked for
        # a snapshot), but it does not need a second identical body.
        self._versions.append(version)
        self._versions = self._versions[-self.max_versions :]
        self._save()
        return version

    def versions(self, limit: int = 20) -> tuple[ConfigVersion, ...]:
        return tuple(reversed(self._versions[-max(1, int(limit)) :]))

    def restore_target(self, version: int) -> ConfigVersion:
        for row in reversed(self._versions):
            if row.version == int(version):
                if not row.known_good:
                    raise ValueError(f"Configuration version {version} is not known-good.")
                valid, issues = self.validate(row.settings)
                if not valid:
                    raise ValueError(
                        f"Configuration version {version} no longer validates: " + "; ".join(issues)
                    )
                return row
        raise KeyError(f"Configuration version not found: {version}")

    def record_restore(self, target: ConfigVersion, *, restored_by: str) -> ConfigVersion:
        """Write the restored body as a NEW version, preserving the bad one."""
        return self.snapshot(
            target.settings,
            note=f"restored from version {target.version} by {restored_by}",
            known_good=True,
        )

    def recover_from_known_good(self) -> tuple[ConfigVersion | None, tuple[str, ...]]:
        current = self.current()
        valid, issues = self.validate(current)
        if valid:
            self._recovered = False
            return None, ()
        for row in reversed(self._versions):
            if not row.known_good:
                continue
            valid_row, row_issues = self.validate(row.settings)
            if valid_row:
                self._recovered = True
                self._recovery_note = (
                    f"restored configuration version {row.version}; previous invalid body was retained in history"
                )
                return row, tuple(issues)
            issues = (*issues, *row_issues)
        return None, tuple(issues)

    def report(self) -> ConfigReport:
        current = self.current()
        valid, issues = self.validate(current)
        return ConfigReport(
            valid=valid,
            checksum=checksum(current),
            issues=issues,
            current=current,
            versions=self.versions(limit=self.max_versions),
            active_version=self._versions[-1].version if self._versions else None,
            recovered=self._recovered,
            recovery_note=self._recovery_note,
        )

    def status(self) -> dict[str, Any]:
        return self.report().to_dict()

    def mark_recovery_applied(self) -> None:
        self._recovered = False


__all__ = ["ConfigurationManager"]
