"""System health monitoring for local NovaControl runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from novacontrol.release.doctor import EnvironmentDoctor
from novacontrol.release.runtime_package import RuntimePackageBuilder


class HealthLevel(StrEnum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class HealthCheckResult:
    name: str
    level: HealthLevel
    message: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "level": self.level.value,
            "message": self.message,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SystemHealthReport:
    level: HealthLevel
    generated_at: datetime
    checks: tuple[HealthCheckResult, ...]

    @property
    def ok(self) -> bool:
        return self.level is not HealthLevel.ERROR

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "ok": self.ok,
            "generated_at": self.generated_at.isoformat(),
            "checks": [check.to_dict() for check in self.checks],
        }


class SystemHealthMonitor:
    """Aggregates local environment, launch, and app health."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def run(self, app_status: Mapping[str, Any] | None = None) -> SystemHealthReport:
        doctor = EnvironmentDoctor(self.root).run()
        package = RuntimePackageBuilder(self.root).build()
        checks = [
            HealthCheckResult(
                "environment",
                HealthLevel.OK if doctor.ok else HealthLevel.ERROR,
                "Environment doctor passed." if doctor.ok else "Environment doctor needs attention.",
                {"doctor": doctor.to_dict()},
            ),
            HealthCheckResult(
                "runtime-package",
                HealthLevel.OK if package.ready else HealthLevel.WARNING,
                "Launch manifest is complete." if package.ready else "Some launch scripts are missing.",
                {"missing_scripts": package.missing_scripts},
            ),
            self._app_modules_check(app_status),
            self._adapter_check(app_status),
        ]
        return SystemHealthReport(
            level=_overall_level(checks),
            generated_at=datetime.now(UTC),
            checks=tuple(checks),
        )

    def _app_modules_check(self, app_status: Mapping[str, Any] | None) -> HealthCheckResult:
        if app_status is None:
            return HealthCheckResult(
                "app-modules",
                HealthLevel.WARNING,
                "Application status was not supplied.",
            )
        modules = tuple(app_status.get("modules", ()))
        return HealthCheckResult(
            "app-modules",
            HealthLevel.OK if modules else HealthLevel.ERROR,
            f"{len(modules)} runtime modules registered.",
            {"modules": modules},
        )

    def _adapter_check(self, app_status: Mapping[str, Any] | None) -> HealthCheckResult:
        if app_status is None:
            return HealthCheckResult(
                "adapters",
                HealthLevel.WARNING,
                "Adapter status was not supplied.",
            )
        browser_ready = bool(app_status.get("browser_adapter_available"))
        return HealthCheckResult(
            "adapters",
            HealthLevel.OK if browser_ready else HealthLevel.WARNING,
            "Desktop and browser adapters are configured."
            if browser_ready
            else "Browser adapter package is not available.",
            {
                "desktop_runner": app_status.get("desktop_runner"),
                "browser_runner": app_status.get("browser_runner"),
                "browser_adapter_available": browser_ready,
            },
        )


def _overall_level(checks: list[HealthCheckResult]) -> HealthLevel:
    levels = {check.level for check in checks}
    if HealthLevel.ERROR in levels:
        return HealthLevel.ERROR
    if HealthLevel.WARNING in levels:
        return HealthLevel.WARNING
    return HealthLevel.OK
