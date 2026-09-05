"""Local environment doctor."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class DoctorReport:
    ok: bool
    checks: tuple[DoctorCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": [check.to_dict() for check in self.checks]}


class EnvironmentDoctor:
    """Checks whether the local NovaControl workspace is ready to run."""

    required_files = (
        "pyproject.toml",
        "src/novacontrol/__main__.py",
        "src/novacontrol/application.py",
        "scripts/run_dashboard.cmd",
        "scripts/run_tests.cmd",
        "scripts/run_api.cmd",
    )
    optional_imports = ("fastapi", "PySide6", "pydantic", "sqlalchemy", "loguru")

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def run(self) -> DoctorReport:
        required_checks = [
            self._check_python(),
            self._check_files(),
            self._check_data_dir(),
        ]
        checks = [
            *required_checks,
            *self._check_imports(),
        ]
        return DoctorReport(ok=all(check.ok for check in required_checks), checks=tuple(checks))

    def _check_python(self) -> DoctorCheck:
        version = sys.version_info
        return DoctorCheck(
            "python",
            version >= (3, 12),
            f"{version.major}.{version.minor}.{version.micro}",
        )

    def _check_files(self) -> DoctorCheck:
        missing = [path for path in self.required_files if not (self.root / path).exists()]
        return DoctorCheck(
            "workspace-files",
            not missing,
            "ok" if not missing else "Missing: " + ", ".join(missing),
        )

    def _check_data_dir(self) -> DoctorCheck:
        data_dir = self.root / "data"
        try:
            data_dir.mkdir(exist_ok=True)
            probe = data_dir / f".doctor-{uuid4().hex}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return DoctorCheck("data-dir", True, str(data_dir))
        except OSError as exc:
            return DoctorCheck("data-dir", False, str(exc))

    def _check_imports(self) -> tuple[DoctorCheck, ...]:
        checks = []
        for module in self.optional_imports:
            found = importlib.util.find_spec(module) is not None
            checks.append(
                DoctorCheck(
                    f"import:{module}",
                    found,
                    "installed" if found else "not installed",
                )
            )
        return tuple(checks)
