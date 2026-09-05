"""Release readiness checklist."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


REQUIRED_FILES = (
    "README.md",
    "pyproject.toml",
    "docs/HOW_TO_RUN.md",
    "docs/ARCHITECTURE.md",
    "docs/DEVELOPMENT.md",
    "docs/PHASES.md",
    "docs/CORE_ENGINE.md",
    "docs/MEMORY.md",
    "docs/TOOLS.md",
    "docs/AGENTS.md",
    "docs/PLANNING.md",
    "docs/DESKTOP_AUTOMATION.md",
    "docs/BROWSER_AUTOMATION.md",
    "docs/VISION.md",
    "docs/EXPLORE.md",
    "docs/VOICE.md",
    "docs/GUI.md",
    "docs/API.md",
    "docs/PLUGIN_MARKETPLACE.md",
    "docs/PERFORMANCE.md",
    "docs/DEPLOYMENT.md",
    "docs/RELEASE.md",
    "src/novacontrol/application.py",
    "src/novacontrol/__main__.py",
)


@dataclass(frozen=True, slots=True)
class ReleaseReadinessReport:
    ready: bool
    checked_files: tuple[str, ...]
    missing_files: tuple[str, ...]
    test_command: str
    run_command: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "checked_files": self.checked_files,
            "missing_files": self.missing_files,
            "test_command": self.test_command,
            "run_command": self.run_command,
        }


class ReleaseReadinessChecker:
    """Checks whether release-critical documentation and entrypoints exist."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def check(self) -> ReleaseReadinessReport:
        missing = tuple(path for path in REQUIRED_FILES if not (self.root / path).exists())
        return ReleaseReadinessReport(
            ready=not missing,
            checked_files=REQUIRED_FILES,
            missing_files=missing,
            test_command="python -m unittest discover -s tests",
            run_command="python -m novacontrol demo all",
        )
