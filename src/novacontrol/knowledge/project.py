"""Project awareness (Phase 11.3): what "this project" means right now.

Note the name collision this module deliberately avoids: :mod:`novacontrol.projects`
is the PROJECT MANAGER — named projects with milestones and tasks, which is how
a person organises work. A :class:`ProjectContext` is something else: the CODE
project the user is standing in, which is what the question *"fix the
authentication issue"* needs resolved before anything can be suggested.

Everything here is read, never guessed, and never written:

* the **root** is the first directory up the tree that looks like a project
  (``.git``, ``pyproject.toml``, ``package.json``, …), because the file being
  asked about is usually below it;
* the **repository** and **branch** come from ``.git`` files — read directly,
  because running git to answer a question about the workspace is a command the
  user did not ask for;
* the **language** and **framework** come from the manifests that are actually
  present, and the framework only when a dependency names it;
* the **recent files** are the newest ones, not "the ones in the editor" — this
  process cannot see an editor, and pretending otherwise would be a guess.

Nothing in this module modifies a file. The phase says "do not blindly modify
files": project awareness exists so an agent can *ask better questions*, and the
only thing it produces is context.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

#: Directories no project scan should enter: they are either not source or not
#: this project's source.
IGNORED_DIRECTORIES = frozenset(
    {
        ".git", ".hg", ".svn", ".idea", ".vscode", ".venv", "venv", "env",
        "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", "dist", "build", "target", "out", ".next", ".nuxt",
        "coverage", "htmlcov", ".tox", ".gradle", ".terraform", "vendor",
        "site-packages", ".egg-info",
    }
)

#: File names that mark a project root (first match walking upward wins).
ROOT_MARKERS: tuple[str, ...] = (
    ".git", "pyproject.toml", "setup.py", "setup.cfg", "package.json",
    "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "Gemfile",
    "composer.json", "CMakeLists.txt", "requirements.txt", "tox.ini",
)

#: Manifest → the language it implies. Facts, not inference: the file exists or
#: it does not.
MANIFEST_LANGUAGES: dict[str, str] = {
    "pyproject.toml": "python",
    "setup.py": "python",
    "setup.cfg": "python",
    "requirements.txt": "python",
    "Pipfile": "python",
    "package.json": "javascript",
    "tsconfig.json": "typescript",
    "go.mod": "go",
    "Cargo.toml": "rust",
    "pom.xml": "java",
    "build.gradle": "java",
    "Gemfile": "ruby",
    "composer.json": "php",
    "CMakeLists.txt": "cpp",
    "*.csproj": "csharp",
}

#: Manifest → framework names worth reporting, looked for in the manifest text.
FRAMEWORK_HINTS: dict[str, tuple[str, ...]] = {
    "pyproject.toml": ("django", "fastapi", "flask", "pydantic", "pytest", "uvicorn"),
    "requirements.txt": ("django", "fastapi", "flask", "pydantic", "pytest", "uvicorn"),
    "package.json": ("next", "react", "vue", "svelte", "express", "vite", "jest", "playwright"),
    "go.mod": ("gin", "echo", "fiber", "cobra"),
    "Cargo.toml": ("axum", "actix-web", "tokio", "clap"),
    "pom.xml": ("spring",),
    "Gemfile": ("rails", "sinatra"),
    "composer.json": ("laravel", "symfony"),
}

#: Configuration files worth naming in a context — "where is this configured?".
CONFIGURATION_NAMES: tuple[str, ...] = (
    "pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", "mypy.ini",
    "ruff.toml", ".ruff.toml", "package.json", "tsconfig.json", ".eslintrc.json",
    "Dockerfile", "docker-compose.yml", ".env.example", ".github", "Makefile",
    "justfile", "go.mod", "Cargo.toml",
)

#: What counts as a source file for "recent files".
_SOURCE_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go",
        ".rs", ".java", ".kt", ".cs", ".c", ".h", ".cpp", ".hpp", ".rb",
        ".php", ".swift", ".sh", ".ps1", ".sql", ".toml", ".yaml", ".yml",
        ".json", ".md", ".rst", ".txt", ".cfg", ".ini", ".html", ".css",
    }
)

_MAX_SCAN_ENTRIES = 4000


class TestStatusProvider(Protocol):
    """Anything that can say how the project's tests are doing."""

    def __call__(self) -> str: ...  # pragma: no cover - protocol


@dataclass(frozen=True, slots=True)
class ProjectContext:
    """Everything a question about the current code project needs resolved."""

    name: str
    root: str
    repository: str = ""
    branch: str = ""
    language: str = ""
    framework: str = ""
    recent_files: tuple[str, ...] = ()
    configuration: tuple[str, ...] = ()
    recent_errors: tuple[str, ...] = ()
    test_status: str = ""
    active_task: str = ""
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    notes: tuple[str, ...] = ()

    @property
    def is_project(self) -> bool:
        """Whether a project was actually found (a bare directory is not one)."""
        return bool(self.root)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "root": self.root,
            "repository": self.repository,
            "branch": self.branch,
            "language": self.language,
            "framework": self.framework,
            "recent_files": list(self.recent_files),
            "configuration": list(self.configuration),
            "recent_errors": list(self.recent_errors),
            "test_status": self.test_status,
            "active_task": self.active_task,
            "detected_at": self.detected_at.isoformat(),
            "notes": list(self.notes),
            "is_project": self.is_project,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ProjectContext:
        return cls(
            name=str(payload.get("name", "")),
            root=str(payload.get("root", "")),
            repository=str(payload.get("repository", "")),
            branch=str(payload.get("branch", "")),
            language=str(payload.get("language", "")),
            framework=str(payload.get("framework", "")),
            recent_files=tuple(payload.get("recent_files", ())),
            configuration=tuple(payload.get("configuration", ())),
            recent_errors=tuple(payload.get("recent_errors", ())),
            test_status=str(payload.get("test_status", "")),
            active_task=str(payload.get("active_task", "")),
            detected_at=datetime.fromisoformat(str(payload["detected_at"]))
            if payload.get("detected_at")
            else datetime.now(UTC),
            notes=tuple(payload.get("notes", ())),
        )

    def render(self) -> str:
        """The context as the few lines a model (or a person) reads first."""
        if not self.is_project:
            return ""
        lines = [f"Active project: {self.name} ({self.root})"]
        if self.repository:
            lines.append(f"Repository: {self.repository}")
        if self.branch:
            lines.append(f"Branch: {self.branch}")
        if self.language:
            lines.append(f"Language: {self.language}")
        if self.framework:
            lines.append(f"Framework: {self.framework}")
        if self.configuration:
            lines.append(f"Configuration: {', '.join(self.configuration)}")
        if self.recent_files:
            lines.append(f"Recent files: {', '.join(self.recent_files)}")
        if self.recent_errors:
            lines.append("Recent errors:")
            lines.extend(f"  - {error}" for error in self.recent_errors)
        if self.test_status:
            lines.append(f"Tests: {self.test_status}")
        if self.active_task:
            lines.append(f"Active task: {self.active_task}")
        if self.notes:
            lines.extend(f"Note: {note}" for note in self.notes)
        return "\n".join(lines)


class ProjectDetector:
    """Reads a :class:`ProjectContext` off the filesystem, and writes nothing."""

    def __init__(
        self,
        *,
        bug_log: Any | None = None,
        test_status_provider: TestStatusProvider | None = None,
        max_recent_files: int = 8,
        max_errors: int = 5,
        max_scan_entries: int = _MAX_SCAN_ENTRIES,
    ) -> None:
        self.bug_log = bug_log
        self.test_status_provider = test_status_provider
        self.max_recent_files = max(1, int(max_recent_files))
        self.max_errors = max(0, int(max_errors))
        self.max_scan_entries = max(1, int(max_scan_entries))
        self.active_task = ""

    def detect(self, path: str | Path | None = None) -> ProjectContext:
        """Detect the project containing ``path`` (or the process's own directory)."""
        start = Path(path or os.getcwd()).expanduser()
        if start.is_file():
            start = start.parent
        root = find_project_root(start)
        if root is None:
            return ProjectContext(name=start.name or str(start), root="")
        return ProjectContext(
            name=root.name,
            root=str(root),
            repository=read_repository(root),
            branch=read_branch(root),
            language=detect_language(root),
            framework=detect_framework(root),
            recent_files=recent_files(root, limit=self.max_recent_files),
            configuration=configuration_files(root),
            recent_errors=self._recent_errors(),
            test_status=self._test_status(),
            active_task=self.active_task,
        )

    def set_active_task(self, task: str) -> None:
        """Record the task the context should name (the caller owns this)."""
        self.active_task = str(task or "")

    def _recent_errors(self) -> tuple[str, ...]:
        if self.bug_log is None or self.max_errors <= 0:
            return ()
        try:
            records = self.bug_log.all(include_fixed=False)
        except Exception:  # noqa: BLE001 - a log that cannot be read is not an error here
            return ()
        return tuple(
            str(getattr(record, "title", record))[:200] for record in list(records)[-self.max_errors :]
        )

    def _test_status(self) -> str:
        if self.test_status_provider is None:
            return ""
        try:
            return str(self.test_status_provider() or "")
        except Exception:  # noqa: BLE001 - a probe that fails reports nothing
            return ""


def find_project_root(start: Path) -> Path | None:
    """The first directory at or above ``start`` that looks like a project."""
    current = start
    for _ in range(40):
        if any((current / marker).exists() for marker in ROOT_MARKERS):
            return current
        if current.parent == current:
            return None
        current = current.parent
    return None


def read_branch(root: Path) -> str:
    """The current branch, read from ``.git`` — without running git."""
    head = root / ".git" / "HEAD"
    try:
        content = head.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if content.startswith("ref:"):
        ref = content.split(":", 1)[1].strip()
        # The BRANCH is everything after the ref namespace, not the last path
        # segment: a very common branch is `feature/timeouts`, and reporting it
        # as "timeouts" loses the fact that decides what it is for.
        for prefix in ("refs/heads/", "refs/remotes/", "refs/tags/", "refs/"):
            if ref.startswith(prefix):
                name = ref.removeprefix(prefix).strip()
                return name or ref
        return ref
    return f"detached@{content[:8]}" if content else ""


def read_repository(root: Path) -> str:
    """The origin remote, read from ``.git/config`` — without running git."""
    config = root / ".git" / "config"
    try:
        lines = config.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    in_origin = False
    url = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[remote "):
            in_origin = '"origin"' in stripped
            continue
        if in_origin and stripped.lower().startswith("url"):
            url = stripped.split("=", 1)[-1].strip()
    return url


def detect_language(root: Path) -> str:
    """The language this project is written in, from the manifests it has.

    A project with several manifests reports the one whose marker is highest in
    :data:`MANIFEST_LANGUAGES`, so the answer is stable rather than dependent on
    directory listing order.
    """
    for manifest, language in MANIFEST_LANGUAGES.items():
        if manifest.startswith("*"):
            pattern = f"*{manifest[1:]}"
            if any(root.glob(pattern)):
                return language
            continue
        if (root / manifest).exists():
            return language
    return ""


def detect_framework(root: Path) -> str:
    """Framework names the manifests actually mention, in a stable order."""
    found: list[str] = []
    for manifest, hints in FRAMEWORK_HINTS.items():
        candidate = root / manifest
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        for hint in hints:
            if hint in text and hint not in found:
                found.append(hint)
    return ", ".join(found[:3])


def recent_files(root: Path, *, limit: int = 8, max_entries: int = _MAX_SCAN_ENTRIES) -> tuple[str, ...]:
    """The newest source files under ``root``, most recent first, relative paths."""
    newest: list[tuple[float, str]] = []
    considered = 0
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = [
            name for name in subdirectories if name not in IGNORED_DIRECTORIES and not name.startswith(".")
        ]
        for filename in filenames:
            considered += 1
            if considered > max_entries:
                break
            suffix = Path(filename).suffix.lower()
            if suffix not in _SOURCE_SUFFIXES:
                continue
            full = Path(directory) / filename
            try:
                modified = full.stat().st_mtime
            except OSError:
                continue
            newest.append((modified, str(Path(directory).relative_to(root) / filename).replace("\\", "/")))
        if considered > max_entries:
            break
    newest.sort(key=lambda item: (-item[0], item[1]))
    return tuple(path for _modified, path in newest[: max(1, limit)])


def configuration_files(root: Path) -> tuple[str, ...]:
    """The configuration and manifest files present at the project root."""
    return tuple(name for name in CONFIGURATION_NAMES if (root / name).exists())


__all__ = [
    "CONFIGURATION_NAMES",
    "FRAMEWORK_HINTS",
    "IGNORED_DIRECTORIES",
    "MANIFEST_LANGUAGES",
    "ROOT_MARKERS",
    "ProjectContext",
    "ProjectDetector",
    "TestStatusProvider",
    "configuration_files",
    "detect_framework",
    "detect_language",
    "find_project_root",
    "read_branch",
    "read_repository",
    "recent_files",
]
