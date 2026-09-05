"""Release hardening checks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from novacontrol.release.doctor import EnvironmentDoctor
from novacontrol.release.runtime_package import RuntimePackageBuilder
from novacontrol.self_improvement import SelfImprovementEngine


@dataclass(frozen=True, slots=True)
class HardeningCheck:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class HardeningReport:
    ready: bool
    checks: tuple[HardeningCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"ready": self.ready, "checks": [check.to_dict() for check in self.checks]}


class ReleaseHardeningChecker:
    """Checks the minimum conditions for a locally usable NovaControl build."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def run(self) -> HardeningReport:
        doctor = EnvironmentDoctor(self.root).run()
        package = RuntimePackageBuilder(self.root).build()
        improvement = SelfImprovementEngine(self.root).plan("release hardening")
        syntax_errors = tuple(finding for finding in improvement.findings if finding.severity == "error")
        checks = (
            HardeningCheck("environment", doctor.ok, "doctor ok" if doctor.ok else "doctor failed"),
            HardeningCheck("launch-scripts", package.ready, "scripts ready" if package.ready else "missing scripts"),
            HardeningCheck(
                "syntax",
                not syntax_errors,
                "no syntax errors" if not syntax_errors else f"{len(syntax_errors)} syntax errors",
            ),
            HardeningCheck(
                "tests",
                improvement.profile.test_files > 0,
                f"{improvement.profile.test_files} test files",
            ),
        )
        return HardeningReport(ready=all(check.ok for check in checks), checks=checks)
