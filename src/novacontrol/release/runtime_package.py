"""Runtime package manifest for local launches."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeCommand:
    name: str
    description: str
    command: tuple[str, ...]
    script: str | None = None
    available: bool = True

    def shell_command(self) -> str:
        return " ".join(_quote(part) for part in self.command)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "command": list(self.command),
            "shell_command": self.shell_command(),
            "script": self.script,
            "available": self.available,
        }


@dataclass(frozen=True, slots=True)
class RuntimePackage:
    root: str
    python: str
    ready: bool
    commands: tuple[RuntimeCommand, ...]
    missing_scripts: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "python": self.python,
            "ready": self.ready,
            "commands": [command.to_dict() for command in self.commands],
            "missing_scripts": self.missing_scripts,
        }


class RuntimePackageBuilder:
    """Builds launch metadata for the current local workspace."""

    command_specs = (
        ("dashboard", "Launch the desktop dashboard.", "scripts/run_dashboard.cmd"),
        ("api", "Launch the local API server.", "scripts/run_api.cmd"),
        ("tests", "Run the full test suite.", "scripts/run_tests.cmd"),
        ("doctor", "Check local environment readiness.", "scripts/run_doctor.cmd"),
        ("package", "Show this runtime package manifest.", "scripts/run_package.cmd"),
    )

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def build(self) -> RuntimePackage:
        python = self._python()
        commands = tuple(self._build_command(name, description, script) for name, description, script in self.command_specs)
        missing = tuple(command.script or "" for command in commands if command.script and not command.available)
        return RuntimePackage(
            root=str(self.root),
            python=str(python),
            ready=not missing,
            commands=commands,
            missing_scripts=missing,
        )

    def _build_command(self, name: str, description: str, script: str) -> RuntimeCommand:
        path = self.root / script
        return RuntimeCommand(
            name=name,
            description=description,
            command=("cmd", "/c", script) if sys.platform.startswith("win") else (str(path),),
            script=script,
            available=path.exists(),
        )

    def _python(self) -> Path:
        if sys.platform.startswith("win"):
            venv_python = self.root / ".venv" / "Scripts" / "python.exe"
        else:
            venv_python = self.root / ".venv" / "bin" / "python"
        return venv_python if venv_python.exists() else Path(sys.executable)


def _quote(value: str) -> str:
    if not value or re.search(r"\s", value):
        return f'"{value}"'
    return value
