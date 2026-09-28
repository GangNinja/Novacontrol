"""The Developer specialized agent (Phase 12.1).

Every responsibility the phase lists has one home, and none of them is new
machinery:

    inspect repositories           -> SelfImprovementEngine.inspect()
    understand project structure   -> ProjectDetector + a bounded top level
    search code                    -> a local lexical scan of the workspace
    inspect errors                 -> the bug log / open project errors
    propose changes                -> SelfImprovementEngine.plan()
    modify files when authorized   -> SelfImprovementEngine.apply_changes()
    run tests                      -> a command runner, behind a permission gate
    inspect failures / debug       -> failure parsing + the Recovery vocabulary
    Git operations                 -> read-only by default; arguments are read
                                      back word by word and writes are gated
    verify changes                 -> the written bytes, and the test tally
    report                         -> the run's own evidence

Two rules the phase states are enforced structurally rather than by habit:

* **Never claim success without verification.** The shared pipeline refuses to
  report COMPLETED unless a step was verified PASS, and this agent's `verify`
  only marks PASS what it actually checked — all tests passing, the written
  bytes matching what was proposed.
* **Do not commit/push destructive changes without permission.** Writes and
  repository mutations declare a permission and a risk, so the pipeline's tool
  selection stage — the centralized
  :class:`~novacontrol.reliability.permissions.PermissionManager` plus the
  build's approval gateway — decides before anything runs. With no approval
  flow wired (the default) a write is refused and reported, never attempted.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from novacontrol.agentcore.recovery import RecoveryStrategy
from novacontrol.agentcore.verifier import VerificationResult, VerificationStatus
from novacontrol.agents.models import AgentRole, AgentTask
from novacontrol.agents.pipeline import (
    AgentRun,
    PipelineStep,
    RecoveryPlan,
    SpecialistAgent,
    SpecialistDecision,
    SpecialistPipeline,
    StepOutcome,
    StepStatus,
)
from novacontrol.core.security import (
    ApprovalDecision,
    ApprovalGateway,
    ApprovalRequest,
    PermissionScope,
    RiskLevel,
)
from novacontrol.knowledge.project import (
    IGNORED_DIRECTORIES,
    ProjectContext,
    ProjectDetector,
)
from novacontrol.reliability.permissions import PermissionDeclaration
from novacontrol.self_improvement.engine import SelfImprovementEngine
from novacontrol.self_improvement.models import CodeChange, CodeChangeStatus

__all__ = [
    "CodeMatch",
    "CodeSearchResult",
    "CommandResult",
    "DeveloperAgent",
    "GitCommand",
    "PipelineApprovedGateway",
    "SubprocessRunner",
    "TestCommand",
    "TestReport",
    "diagnose_failure",
    "git_is_mutating",
    "parse_test_output",
    "read_git_command",
    "recognise_test_command",
    "search_code",
]

#: Extensions a code search reads. Deliberately a subset of the knowledge
#: module's list: a search result is source a developer will act on, not prose.
_SEARCH_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".cs",
        ".rb",
        ".php",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".swift",
        ".sql",
        ".sh",
        ".ps1",
        ".toml",
        ".cfg",
        ".ini",
        ".yaml",
        ".yml",
        ".json",
    }
)

#: Goal phrasing -> the one action this agent is being asked for. Order IS the
#: precedence: "apply the fix" is an application, not a proposal, and "why does
#: the test fail" is a diagnosis even though it mentions a test.
_ACTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "git",
        r"\b(git|commit|commits|push|pull request|pull|merge|rebase|stash|checkout|switch"
        r"|branch|clone|fetch|cherry-pick|revert|tag|diff)\b",
    ),
    ("run_tests", r"\b(run|rerun|execute)\b[^.]{0,24}\btests?\b|\bpytest\b|\btest suite\b"),
    (
        "debug",
        r"\b(debug|why (?:is|does|did|are)|traceback|exception|crash|regression"
        r"|failing|failed|fails)\b",
    ),
    # "what errors are open?" is its own request: reading the open errors is
    # cheaper than reproducing a failure, and doing both when only the list was
    # asked for wastes the test suite's runtime.
    ("errors", r"\b(errors?|failures?)\b"),
    ("search", r"\b(search|find|locate|grep|where(?:'s| is| are))\b"),
    ("apply_change", r"\b(apply|make the change|go ahead|do it|implement it|fix it)\b"),
    (
        "plan_change",
        r"\b(fix|implement|refactor|add|update|upgrade|change|modify|edit|rewrite|patch|bug)\b",
    ),
    (
        "inspect",
        r"\b(inspect|structure|overview|repository|repo|project|explain|summarize|describe|status)\b",
    ),
)

#: Git verbs that only read: running one changes nothing, so it needs no
#: approval.
_READ_ONLY_GIT_VERBS = frozenset(
    {
        "blame",
        "count-objects",
        "describe",
        "diff",
        "grep",
        "log",
        "ls-files",
        "reflog",
        "rev-parse",
        "shortlog",
        "show",
        "status",
        "whatchanged",
    }
)

#: Verbs that read or write depending on what follows them: `git branch` lists
#: the branches, `git branch -D x` deletes one, and `git remote` shows the
#: remotes while `git remote add` changes them. The argument decides, so the
#: classification is per argument — calling the verb itself read-only is exactly
#: how a branch deletion becomes an unapproved mutation. The values are the
#: first arguments that mean "just read".
_MULTI_MODE_GIT: dict[str, frozenset[str]] = {
    "branch": frozenset(
        {
            "-a",
            "--all",
            "-r",
            "--remotes",
            "-v",
            "-vv",
            "--verbose",
            "--list",
            "--show-current",
            "--merged",
            "--no-merged",
            "--contains",
            "--points-at",
            "--format",
        }
    ),
    "config": frozenset({"-l", "--list", "--get", "--get-all", "--get-regexp"}),
    "lfs": frozenset({"env", "ls-files", "version", "status"}),
    "notes": frozenset({"list", "show"}),
    "remote": frozenset({"-v", "--verbose", "get-url", "show"}),
    "stash": frozenset({"list", "show"}),
    "submodule": frozenset({"status", "summary"}),
    "tag": frozenset({"-l", "--list", "-n", "--contains", "--points-at"}),
    "worktree": frozenset({"list"}),
}

#: Every git verb this agent recognises. A verb in neither table above is a
#: mutation, because an unrecognised repository command is not something to run
#: without a person saying yes — and a verb nobody classified is exactly what a
#: read-only list gets wrong.
_GIT_VERBS = frozenset(
    {
        "add",
        "am",
        "apply",
        "archive",
        "bisect",
        "blame",
        "branch",
        "bundle",
        "cat-file",
        "check-ignore",
        "checkout",
        "cherry-pick",
        "clean",
        "clone",
        "commit",
        "config",
        "count-objects",
        "describe",
        "diff",
        "fetch",
        "filter-branch",
        "fsck",
        "gc",
        "grep",
        "init",
        "log",
        "ls-files",
        "merge",
        "mv",
        "notes",
        "pull",
        "push",
        "rebase",
        "reflog",
        "remote",
        "reset",
        "restore",
        "revert",
        "rev-parse",
        "rm",
        "shortlog",
        "show",
        "stash",
        "status",
        "submodule",
        "switch",
        "tag",
        "update-ref",
        "whatchanged",
        "worktree",
    }
)

#: How a bare "git" with no verb after it is read, and how a sentence about the
#: repository that names no verb at all is read ("show me git history").
_GIT_SYNONYMS: dict[str, str] = {
    "branches": "branch",
    "changes": "status",
    "commits": "log",
    "history": "log",
}

#: Flags after which the next token is a VALUE the user supplied (`git commit -m
#: 'message'`, `git log --grep widget`). The value is passed through even though
#: it looks like neither a revision nor a path, because the flag says what it is.
_GIT_VALUE_FLAGS = frozenset(
    {
        "-b",
        "-B",
        "-c",
        "-C",
        "-m",
        "-t",
        "-u",
        "--author",
        "--branch",
        "--grep",
        "--max-count",
        "--message",
        "--set-upstream-to",
        "--since",
        "--tag",
        "--until",
    }
)

#: Sentence glue: words that are English rather than git ("git commit these
#: changes"). They are dropped and reported, never treated as arguments.
#: Deliberately small, and free of anything git takes as an argument — a word
#: that only looks like glue becomes an unreadable token, which refuses a
#: mutation instead of reshaping it.
_GIT_GLUE = frozenset(
    {
        "a",
        "all",
        "an",
        "and",
        "any",
        "at",
        "changes",
        "code",
        "everything",
        "file",
        "files",
        "for",
        "from",
        "in",
        "into",
        "it",
        "its",
        "me",
        "my",
        "new",
        "now",
        "of",
        "on",
        "onto",
        "or",
        "our",
        "please",
        "some",
        "that",
        "the",
        "then",
        "these",
        "this",
        "those",
        "to",
        "with",
        "your",
    }
)

#: Characters with no business in an argument. The runner never uses a shell, so
#: this is defence in depth rather than the guard — which is why a token
#: containing one is REFUSED and reported, instead of passed on.
_GIT_UNSAFE = re.compile(r"[;|&<>$`()\\\n\r]")

_GIT_FLAG = re.compile(r"^(?:--?[A-Za-z0-9][-A-Za-z0-9_=]*|--)$")
_GIT_REVISION = re.compile(
    r"^(?:HEAD|FETCH_HEAD|ORIG_HEAD|MERGE_HEAD|@|[0-9a-fA-F]{7,40})(?:[~^][0-9]{0,3})?$"
)
#: A path, a ref, or a URL: something with structure git can act on (`src/x.py`,
#: `origin/main`, `v1.2`, `../lib`, `HEAD~1`, `https://host/repo.git`).
_GIT_OPERAND = re.compile(r"^[A-Za-z0-9_.@-]*[:/.~^][A-Za-z0-9_:./@~^-]*$")
_GIT_MODE = re.compile(r"^[a-z][a-z-]{0,20}$")
_GIT_TOKEN = re.compile(r"'([^']*)'|\"([^\"]*)\"|(\S+)")

#: Failure signatures the debugger recognises, worst-and-most-specific first.
_FAILURE_SIGNATURES: tuple[tuple[str, str], ...] = (
    (
        "missing_dependency",
        r"(?:ModuleNotFoundError|ImportError|No module named)\s*:?\s*"
        r"(?:No module named\s*)?['\"]?([\w.\-]+)",
    ),
    ("syntax_error", r"SyntaxError:\s*(.+)"),
    ("assertion", r"AssertionError:?\s*(.*)"),
    ("type_error", r"TypeError:\s*(.+)"),
    ("name_error", r"NameError:\s*(.+)"),
    ("attribute_error", r"AttributeError:\s*(.+)"),
    ("key_error", r"KeyError:\s*(.+)"),
    ("error", r"(?:^|\n)E\s+(.+)"),
)

_TEST_COMMAND: tuple[str, ...] = ("python", "-m", "pytest", "tests", "-q")

_PYTEST_TALLY = re.compile(
    r"(\d+)\s+(passed|failed|error[s]?|skipped|xfailed|xpassed|deselected|warning[s]?)"
)
_UNITTEST_RAN = re.compile(r"Ran (\d+) tests? in ([\d.]+)s")
_UNITTEST_RESULT = re.compile(r"^(OK|FAILED)\s*(?:\(([^)]*)\))?", re.MULTILINE)

_MAX_OUTPUT_CHARS = 4000


# --- command execution ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What running one command produced."""

    command: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        parts = [self.stdout.strip(), self.stderr.strip()]
        return "\n".join(part for part in parts if part)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "returncode": self.returncode,
            "duration_seconds": round(self.duration_seconds, 3),
            "timed_out": self.timed_out,
            "output": self.output[-_MAX_OUTPUT_CHARS:],
        }


class CommandRunner(Protocol):
    """Anything that can run a command and report what happened."""

    async def run(
        self, command: Sequence[str], *, cwd: str | Path | None = None, timeout: float = 300.0
    ) -> CommandResult: ...


class SubprocessRunner:
    """The real runner: one process, no shell, output captured.

    No shell means no quoting rules to get wrong: the argv is executed as given,
    so a target that contains a space or a semicolon is an argument, not a
    command. Nothing about this class is authorized on its own — it is only ever
    reached through a step the permission layer already allowed.
    """

    def __init__(self, *, max_output_chars: int = 40_000) -> None:
        self.max_output_chars = max_output_chars

    async def run(
        self, command: Sequence[str], *, cwd: str | Path | None = None, timeout: float = 300.0
    ) -> CommandResult:
        argv = tuple(str(part) for part in command)
        if not argv:
            raise ValueError("A command runner needs a command.")
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            timed_out = False
        except TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
            timed_out = True
        return CommandResult(
            command=argv,
            returncode=int(process.returncode or 0),
            stdout=stdout.decode("utf-8", errors="replace")[-self.max_output_chars :],
            stderr=stderr.decode("utf-8", errors="replace")[-self.max_output_chars :],
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
        )


class PipelineApprovedGateway:
    """Carries the pipeline's verdict down to a component's own approval gate.

    The write path has two gates because it has two entry points: the shared
    pipeline's tool-selection stage (which asked the person, or their policy)
    and :meth:`SelfImprovementEngine.apply_changes`, which is callable on its
    own. Within a specialist run the first gate already decided, and asking the
    same person twice for the same write teaches people to approve without
    reading. This gateway therefore approves exactly what the pipeline allowed,
    and is only ever reached for a step that permission layer allowed.
    """

    def __init__(self, decided_by: str = "agents.specialist") -> None:
        self.decided_by = decided_by

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(
            request_id=request.id,
            approved=True,
            decided_by=self.decided_by,
            reason="Authorized during the specialist pipeline's tool-selection stage.",
        )


# --- the project's test runner -------------------------------------------------


@dataclass(frozen=True, slots=True)
class TestCommand:
    """The argv that runs a project's tests, and how it is described."""

    argv: tuple[str, ...]
    label: str

    def to_dict(self) -> dict[str, Any]:
        return {"argv": list(self.argv), "label": self.label}


def recognise_test_command(project: str | Path, *, scope: str = "tests") -> TestCommand | None:
    """The argv that runs ``project``'s tests, or None when none is recognisable.

    Recognising the runner — rather than guessing one — is the difference between
    running the project's own suite and running whatever happens to be installed:
    a Node project gets ``npm test``, a Python project gets pytest from the
    interpreter that owns this process, and anything else is reported as
    unrecognised instead of guessed at. The planning path's test step uses this
    same rule (`NovaControlApplication._test_argv` delegates here), so a project
    cannot be a Node project to the planner and a Python project to this agent.
    """
    root = Path(project)
    npm = shutil.which("npm")
    if (root / "package.json").is_file() and npm:
        return TestCommand(argv=(npm, "test"), label="npm test")
    scope_path = root / scope
    looks_python = (
        (root / "pytest.ini").is_file()
        or (root / "pyproject.toml").is_file()
        or (root / "setup.cfg").is_file()
        or (root / "tests").is_dir()
        or scope_path.exists()
    )
    if not looks_python:
        return None
    argv: tuple[str, ...] = (sys.executable, "-m", "pytest", "-q")
    if scope_path.exists():
        argv = (*argv, scope)
    return TestCommand(argv=argv, label="python -m pytest")


# --- test output ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TestReport:
    """A test run, counted rather than eyeballed."""

    command: tuple[str, ...]
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    duration_seconds: float = 0.0
    returncode: int = 0
    output: str = ""
    recognised: bool = False

    @property
    def ok(self) -> bool:
        """Green means: the runner agreed, nothing failed, and a tally was read.

        A run whose output carried no tally at all is not a pass — it is a run
        that did not do what was asked (a collection error, a missing test
        directory, a wrong command).
        """
        return self.recognised and self.returncode == 0 and not self.failed and not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "skipped": self.skipped,
            "duration_seconds": round(self.duration_seconds, 3),
            "returncode": self.returncode,
            "ok": self.ok,
            "recognised": self.recognised,
            "output": self.output[-_MAX_OUTPUT_CHARS:],
        }


def parse_test_output(result: CommandResult) -> TestReport:
    """Count a pytest or unittest run from its own output.

    Both formats are read because both are what a project here actually runs;
    neither is guessed at, and a run with no recognisable tally is reported as
    unrecognised rather than silently as zero failures.
    """
    text = result.output
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    recognised = False
    for match in _PYTEST_TALLY.finditer(text):
        name = match.group(2).rstrip("s") if match.group(2).startswith("error") else match.group(2)
        key = {"error": "errors"}.get(name, name)
        if key in counts:
            counts[key] = max(counts[key], int(match.group(1)))
            recognised = True
    unittest_result = _UNITTEST_RESULT.search(text)
    ran = _UNITTEST_RAN.search(text)
    if unittest_result is not None and ran is not None:
        total = int(ran.group(1))
        detail = unittest_result.group(2) or ""
        for key, pattern in (
            ("failed", r"failures?=(\d+)"),
            ("errors", r"errors?=(\d+)"),
            ("skipped", r"skipped=(\d+)"),
        ):
            found = re.search(pattern, detail)
            if found:
                counts[key] = int(found.group(1))
        counts["passed"] = max(0, total - counts["failed"] - counts["errors"] - counts["skipped"])
        recognised = True
    duration = result.duration_seconds
    pytest_duration = re.search(r"in ([\d.]+)s", text)
    if pytest_duration is not None:
        try:
            duration = float(pytest_duration.group(1))
        except ValueError:
            duration = result.duration_seconds
    return TestReport(
        command=result.command,
        passed=counts["passed"],
        failed=counts["failed"],
        errors=counts["errors"],
        skipped=counts["skipped"],
        duration_seconds=duration,
        returncode=result.returncode,
        output=text,
        recognised=recognised,
    )


# --- code search ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CodeMatch:
    path: str
    line: int
    text: str
    terms: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "text": self.text, "terms": list(self.terms)}


@dataclass(frozen=True, slots=True)
class CodeSearchResult:
    query: str
    matches: tuple[CodeMatch, ...] = ()
    files_scanned: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "matches": [match.to_dict() for match in self.matches],
            "files_scanned": self.files_scanned,
            "truncated": self.truncated,
        }


def search_code(
    root: Path,
    query: str,
    *,
    max_files: int = 3000,
    max_matches: int = 40,
    max_line_chars: int = 200,
) -> CodeSearchResult:
    """Find the lines that mention ``query`` in the workspace, locally.

    A lexical scan, not an embedding search: it is deterministic, needs no model
    and no index build, and answers the question a developer actually asks
    ("where is this name?") — the project's own knowledge engine is what answers
    the semantic questions.
    """
    terms = tuple(
        dict.fromkeys(term for term in re.split(r"[\s,]+", query.strip()) if len(term) >= 2)
    )
    if not terms:
        return CodeSearchResult(query=query)
    needle = re.compile("|".join(re.escape(term) for term in terms), re.IGNORECASE)
    matches: list[CodeMatch] = []
    scanned = 0
    truncated = False
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if name not in IGNORED_DIRECTORIES and not name.startswith(".")
        )
        for filename in sorted(filenames):
            if Path(filename).suffix.lower() not in _SEARCH_SUFFIXES:
                continue
            if scanned >= max_files:
                truncated = True
                break
            scanned += 1
            path = Path(directory) / filename
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            relative = _relative(path, root)
            for number, line in enumerate(text.splitlines(), start=1):
                found = needle.findall(line)
                if not found:
                    continue
                matches.append(
                    CodeMatch(
                        relative,
                        number,
                        line.strip()[:max_line_chars],
                        tuple(dict.fromkeys(found)),
                    )
                )
                if len(matches) >= max_matches:
                    truncated = True
                    break
            if len(matches) >= max_matches:
                break
        if len(matches) >= max_matches or scanned >= max_files:
            break
    return CodeSearchResult(
        query=query, matches=tuple(matches), files_scanned=scanned, truncated=truncated
    )


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


# --- git ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GitCommand:
    """The git command a goal asks for, and every word that could not be read.

    ``unused`` is sentence glue that carries no meaning ("git commit these
    changes"), and ``unreadable`` is a word that might have been an argument and
    could not be shown to be one. The difference matters: a mutation is only run
    when ``unreadable`` is empty.
    """

    argv: tuple[str, ...]
    unused: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "unused": list(self.unused),
            "unreadable": list(self.unreadable),
        }


def git_is_mutating(argv: Sequence[str]) -> bool:
    """Whether running this git command changes the repository.

    A verb that only reads is a read. A verb that can do both (`branch`,
    `remote`, `config`, `stash`, `worktree`, `tag`, `notes`, `submodule`, `lfs`)
    is judged by its first argument, so `git branch -D feature` is a mutation
    needing approval and `git branch -a` is not. Anything unrecognised is a
    mutation: the safe default for a repository command nobody classified is to
    ask first.
    """
    if not argv:
        return False
    verb = str(argv[0])
    arguments = tuple(str(argument) for argument in argv[1:])
    if verb in _READ_ONLY_GIT_VERBS:
        return False
    reads = _MULTI_MODE_GIT.get(verb)
    if reads is not None:
        return bool(arguments) and arguments[0] not in reads
    return True


def read_git_command(goal: str, *, root: str | Path | None = None) -> GitCommand:
    """Read the git command a request asks for, without inventing one.

    The verb is the one the user named (preferring the word right after "git"),
    and the arguments are the words that can be shown to be git arguments: flags,
    revisions, paths, the values of value-taking flags, and remote or branch
    names this workspace actually has. Quoted text is kept whole and passed as a
    single argument, which is how `git commit -m 'fix the widget'` survives
    having no shell to quote it.

    Words that are neither are reported rather than dropped silently: a caller
    can see that `git commit the widget fix` was read as `git commit` with three
    words unread, which is why that mutation is refused instead of run.
    """
    tokens = _split_goal(goal)
    cores = [_git_core(token) for token, _ in tokens]
    lowered = [core.lower() for core in cores]
    git_at = next((index for index, core in enumerate(lowered) if core == "git"), -1)
    verb_at = -1
    verb = "status"
    searches = (
        (range(git_at + 1, len(lowered)) if git_at >= 0 else range(0), _GIT_VERBS),
        (range(git_at + 1, len(lowered)) if git_at >= 0 else range(0), _GIT_SYNONYMS),
        (range(len(lowered)), _GIT_SYNONYMS),
        (range(len(lowered)), _GIT_VERBS),
    )
    for positions, vocabulary in searches:
        for index in positions:
            if index == git_at or lowered[index] not in vocabulary:
                continue
            verb_at = index
            verb = (
                lowered[index]
                if isinstance(vocabulary, frozenset)
                else vocabulary[lowered[index]]
            )
            break
        if verb_at >= 0:
            break
    if verb_at < 0:
        return GitCommand(argv=(verb,))
    names = _git_reference_names(root)
    arguments: list[str] = []
    unused: list[str] = []
    unreadable: list[str] = []
    value_expected = False
    for token, quoted in tokens[verb_at + 1 :]:
        core = _git_core(token)
        if not core:
            continue
        if value_expected and not _GIT_FLAG.match(core):
            arguments.append(core)
            value_expected = False
            continue
        if _GIT_UNSAFE.search(token):
            unreadable.append(token)
            value_expected = False
            continue
        value_expected = core in _GIT_VALUE_FLAGS
        if _GIT_FLAG.match(core):
            arguments.append(core)
            continue
        if quoted:
            arguments.append(core)
            continue
        if core in _GIT_GLUE:
            unused.append(core)
            continue
        if not arguments and verb in _MULTI_MODE_GIT and _GIT_MODE.match(core):
            # The mode word of a verb that reads or writes (`remote add`).
            arguments.append(core)
            continue
        if _git_operand(core, root, names):
            arguments.append(core)
            continue
        unreadable.append(core)
    return GitCommand(argv=(verb, *arguments), unused=tuple(unused), unreadable=tuple(unreadable))


def _split_goal(goal: str) -> tuple[tuple[str, bool], ...]:
    """The goal's words, with quoted spans kept whole and marked as quoted."""
    tokens: list[tuple[str, bool]] = []
    for match in _GIT_TOKEN.finditer(goal):
        for group in (1, 2):
            if match.group(group) is not None:
                tokens.append((match.group(group), True))
                break
        else:
            tokens.append((match.group(3), False))
    return tuple(tokens)


def _git_core(token: str) -> str:
    return token.strip("\"'`.,;:!?()[]")


def _git_operand(core: str, root: str | Path | None, names: frozenset[str]) -> bool:
    """Whether a word can be shown to be a git argument rather than English."""
    if _GIT_REVISION.match(core) or _GIT_OPERAND.match(core) or core.isdigit():
        return True
    if core in names:
        return True
    if root is not None:
        try:
            return (Path(root) / core).exists()
        except OSError:
            return False
    return False


def _quoted_list(words: Sequence[str]) -> str:
    return ", ".join(repr(str(word)) for word in words)


def _git_reference_names(root: str | Path | None) -> frozenset[str]:
    """Remote and branch names this workspace actually has, read from `.git`.

    Read-only, and never a subprocess: "main" and "origin" are arguments because
    the repository says they are, which is what keeps `git push origin main`
    working while a mutation full of English is refused.
    """
    if root is None:
        return frozenset()
    git_dir = Path(root) / ".git"
    names: set[str] = set()
    try:
        config = (git_dir / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        config = ""
    names.update(match.group(1) for match in re.finditer(r'\[remote\s+"([^"]+)"\]', config))
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        head = ""
    if head.startswith("ref:"):
        names.add(head.split("ref:", 1)[1].strip().removeprefix("refs/heads/"))
    for namespace in ("heads", "remotes"):
        directory = git_dir / "refs" / namespace
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.is_file():
                reference = path.relative_to(directory).as_posix()
                names.update({reference, reference.rsplit("/", 1)[-1]})
    try:
        packed = (git_dir / "packed-refs").read_text(encoding="utf-8", errors="replace")
    except OSError:
        packed = ""
    for line in packed.splitlines():
        reference = line.strip().split(" ")[-1]
        for prefix in ("refs/heads/", "refs/remotes/"):
            if reference.startswith(prefix):
                name = reference.removeprefix(prefix)
                names.update({name, name.rsplit("/", 1)[-1]})
    return frozenset(names)


# --- the agent ------------------------------------------------------------------


class DeveloperAgent(SpecialistAgent):
    """Inspects, plans, and — when authorized — changes and tests the project."""

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        engine: SelfImprovementEngine | None = None,
        detector: ProjectDetector | None = None,
        runner: CommandRunner | None = None,
        pipeline: SpecialistPipeline | None = None,
        change_builder: Callable[[str], tuple[CodeChange, ...]] | None = None,
        apply_gateway: ApprovalGateway | None = None,
        test_command: Sequence[str] | None = None,
        bug_log: Any | None = None,
        max_files_per_search: int = 3000,
    ) -> None:
        super().__init__(
            "developer-agent",
            AgentRole.CODING,
            "Inspects repositories, searches code, plans and (when authorized) applies "
            "and tests changes.",
            pipeline=pipeline,
        )
        self.root = Path(root or os.getcwd()).expanduser().resolve()
        self.engine = engine or SelfImprovementEngine(self.root)
        self.detector = detector or ProjectDetector(bug_log=bug_log)
        self.runner = runner or SubprocessRunner()
        # An explicitly configured command wins; otherwise the property below
        # recognises the project's own runner (see `recognise_test_command`).
        self._test_command: tuple[str, ...] = tuple(test_command) if test_command else ()
        self.bug_log = bug_log
        self.max_files_per_search = max_files_per_search
        # A proposed change set comes from the build's own code generator; with
        # none wired, this agent still plans, and simply has nothing to write.
        self.change_builder = change_builder
        # The pipeline's tool-selection stage is the gate a specialist run passes
        # through, and it is the one that DECIDES: a write step only reaches this
        # agent's `apply_changes` at all if that gate allowed it. Asking the same
        # person a second time for the same write is how people learn to approve
        # without reading, so the engine's own gate mirrors the verdict the
        # pipeline already reached, and is never consulted for a step the
        # pipeline refused. A caller that wants a different verdict passes one.
        self.apply_gateway: ApprovalGateway = apply_gateway or PipelineApprovedGateway()
        self._pending: dict[str, tuple[CodeChange, ...]] = {}

    # -- declarations ---------------------------------------------------------

    def declarations(self) -> Mapping[str, PermissionDeclaration]:
        read = PermissionDeclaration(
            risk_level=RiskLevel.LOW, required_permission=PermissionScope.FILESYSTEM_READ
        )
        return {
            "developer.inspect_repository": read,
            "developer.inspect_structure": read,
            "developer.search_code": read,
            "developer.inspect_errors": read,
            "developer.plan_changes": read,
            "developer.diagnose": read,
            "developer.verify_changes": read,
            "developer.git_read": PermissionDeclaration(
                risk_level=RiskLevel.LOW, required_permission=PermissionScope.FILESYSTEM_READ
            ),
            # Overwriting a source file cannot be undone from here: there is no
            # backup to restore, so it is destructive and needs a person.
            "developer.apply_changes": PermissionDeclaration(
                risk_level=RiskLevel.HIGH,
                required_permission=PermissionScope.FILESYSTEM_WRITE,
                requires_confirmation=True,
                reversible=False,
                destructive=True,
            ),
            # Running the project's tests executes the project's code: arbitrary
            # code, arbitrary imports, whatever its setup does.
            "developer.run_tests": PermissionDeclaration(
                risk_level=RiskLevel.HIGH,
                required_permission=PermissionScope.SHELL_EXECUTE,
                requires_confirmation=True,
                reversible=True,
                external_side_effect=True,
            ),
            # A commit or a push rewrites shared history and cannot be recalled.
            "developer.git_write": PermissionDeclaration(
                risk_level=RiskLevel.HIGH,
                required_permission=PermissionScope.SHELL_EXECUTE,
                requires_confirmation=True,
                reversible=False,
                destructive=True,
                external_side_effect=True,
            ),
        }

    def clone(self, pipeline: SpecialistPipeline) -> DeveloperAgent:
        """This agent on another pipeline — the same workspace, another scope."""
        return DeveloperAgent(
            root=self.root,
            engine=self.engine,
            detector=self.detector,
            runner=self.runner,
            pipeline=pipeline,
            change_builder=self.change_builder,
            apply_gateway=self.apply_gateway,
            test_command=self._test_command or None,
            bug_log=self.bug_log,
            max_files_per_search=self.max_files_per_search,
        )

    @property
    def test_command(self) -> tuple[str, ...]:
        """The command this project's tests run with.

        A caller that names a command gets exactly that command. Otherwise the
        project's own runner is RECOGNISED — ``npm test`` for a Node project,
        pytest from the interpreter that owns this process for a Python one —
        because running pytest inside a JavaScript repository is not running the
        project's tests. Only when nothing is recognisable does the build fall
        back to the interpreter's pytest, and verification will refuse to call
        that run a pass if it finds no test tally in the output.
        """
        if self._test_command:
            return self._test_command
        recognised = recognise_test_command(self.root)
        return recognised.argv if recognised is not None else _TEST_COMMAND

    @test_command.setter
    def test_command(self, command: Sequence[str]) -> None:
        self._test_command = tuple(str(part) for part in command)

    # -- context --------------------------------------------------------------

    def local_context(self, task: AgentTask, interpretation: Any) -> Mapping[str, Any]:
        """The project context: which project, branch, language, recent files."""
        context = self.project_context()
        return {"project": context.to_dict(), "root": str(self.root)}

    def context_query(self, task: AgentTask, interpretation: Any) -> str:
        action = self._route(task.goal)
        if action == "search":
            return self._search_query(task.goal)
        return task.goal

    def project_context(self) -> ProjectContext:
        return self.detector.detect(self.root)

    # -- decision -------------------------------------------------------------

    def decide(
        self, task: AgentTask, interpretation: Any, context: Mapping[str, Any]
    ) -> SpecialistDecision:
        action = self._route(task.goal)
        data: dict[str, Any] = {}
        requires_approval = action in {"apply_change", "git"}
        if action == "search":
            data["query"] = self._search_query(task.goal)
        if action == "git":
            command = read_git_command(task.goal, root=self.root)
            mutating = git_is_mutating(command.argv)
            data["argv"] = list(command.argv)
            data["mutating"] = mutating
            if command.unused:
                data["unused"] = list(command.unused)
            if command.unreadable:
                data["unreadable"] = list(command.unreadable)
            requires_approval = mutating
            action = "git_write" if mutating else "git"
        if action == "apply_change" and not self._has_pending_changes():
            # "apply the fix" with nothing proposed yet is a proposal: the
            # honest reading is to plan the change, not to write one nobody saw.
            action = "plan_change"
            data["downgraded_from"] = "apply_change"
        return SpecialistDecision(
            action=action,
            reason=self._route_reason(task.goal, action),
            confidence=0.7 if action != "inspect" else 0.4,
            requires_approval=requires_approval,
            data=data,
        )

    # -- planning -------------------------------------------------------------

    def plan(
        self,
        task: AgentTask,
        interpretation: Any,
        decision: SpecialistDecision,
        context: Mapping[str, Any],
    ) -> tuple[PipelineStep, ...]:
        action = decision.action
        goal = task.goal
        if action == "search":
            return (
                PipelineStep(
                    action="search_code",
                    description=f"Search the project for {decision.data.get('query', goal)!r}",
                    tool="developer.search_code",
                    target=str(decision.data.get("query", goal)),
                    expected="The matching files and lines are listed",
                ),
            )
        if action == "errors":
            return (
                self._step(
                    "inspect_errors", "Read the project's open errors", "The open errors are listed"
                ),
            )
        if action == "plan_change":
            return (
                self._inspect_step(),
                PipelineStep(
                    action="plan_changes",
                    description=f"Plan the smallest change that addresses: {goal}",
                    tool="developer.plan_changes",
                    target=goal,
                    expected="A plan with findings and the files it touches",
                ),
            )
        if action == "apply_change":
            return (
                self._inspect_step(),
                PipelineStep(
                    action="plan_changes",
                    description=f"Plan the change to apply for: {goal}",
                    tool="developer.plan_changes",
                    target=goal,
                    expected="A plan with the proposed change set",
                ),
                PipelineStep(
                    action="apply_changes",
                    description="Write the proposed changes to disk",
                    tool="developer.apply_changes",
                    target=goal,
                    expected="Every proposed change is written",
                    permission=PermissionScope.FILESYSTEM_WRITE,
                    risk=RiskLevel.HIGH,
                    requires_approval=True,
                ),
                PipelineStep(
                    action="run_tests",
                    description="Run the project's tests after the change",
                    tool="developer.run_tests",
                    target=" ".join(self.test_command),
                    expected="The test suite passes",
                    permission=PermissionScope.SHELL_EXECUTE,
                    risk=RiskLevel.HIGH,
                    requires_approval=True,
                    metadata={"command": list(self.test_command)},
                ),
                PipelineStep(
                    action="verify_changes",
                    description="Confirm the written files hold the proposed content",
                    tool="developer.verify_changes",
                    target=goal,
                    expected="The files on disk match the proposal",
                ),
            )
        if action == "run_tests":
            return (
                PipelineStep(
                    action="run_tests",
                    description="Run the project's test suite",
                    tool="developer.run_tests",
                    target=" ".join(self.test_command),
                    expected="The test suite passes",
                    permission=PermissionScope.SHELL_EXECUTE,
                    risk=RiskLevel.HIGH,
                    requires_approval=True,
                    metadata={"command": list(self.test_command)},
                ),
            )
        if action == "debug":
            return (
                self._step(
                    "inspect_errors", "Read the project's open errors", "The open errors are listed"
                ),
                PipelineStep(
                    action="run_tests",
                    description="Reproduce the failure by running the tests",
                    tool="developer.run_tests",
                    target=" ".join(self.test_command),
                    expected="The failing test is named in the output",
                    permission=PermissionScope.SHELL_EXECUTE,
                    risk=RiskLevel.HIGH,
                    requires_approval=True,
                    # Debugging READS a failure: a suite that fails as reported
                    # is the reproduction this step exists for, so it is not a
                    # failed step, and the run does not recover from it.
                    metadata={"expect_failure": True, "command": list(self.test_command)},
                ),
                self._step("diagnose", "Diagnose the failure", "A named failure with its cause"),
            )
        if action in {"git", "git_write"}:
            argv = tuple(str(part) for part in decision.data.get("argv", ("status",)))
            unused = tuple(str(word) for word in decision.data.get("unused", ()))
            unreadable = tuple(str(word) for word in decision.data.get("unreadable", ()))
            mutating = bool(decision.data.get("mutating", git_is_mutating(argv)))
            # A mutation is only run when the whole command could be read back.
            # Running "git commit" for "git commit the widget fix" is a
            # different repository operation from the one that was asked for,
            # and guessing is how an unattended agent rewrites history nobody
            # asked it to touch.
            refuse = mutating and bool(unreadable)
            command_text = " ".join(argv)
            refusal = (
                f"Not run: git {command_text} — "
                f"{_quoted_list(unreadable)} could not be read as a git argument"
            )
            return (
                PipelineStep(
                    action="git",
                    description=refusal if refuse else f"Run: git {command_text}",
                    tool="developer.git_write" if mutating else "developer.git_read",
                    target=" ".join(argv),
                    expected=(
                        "The command is not run and the words that could not be read are reported"
                        if refuse
                        else "The repository reports the requested result"
                    ),
                    permission=(
                        PermissionScope.SHELL_EXECUTE
                        if mutating
                        else PermissionScope.FILESYSTEM_READ
                    ),
                    risk=RiskLevel.HIGH if mutating else RiskLevel.LOW,
                    requires_approval=mutating,
                    metadata={
                        "argv": list(argv),
                        "mutating": mutating,
                        "unused": list(unused),
                        "unreadable": list(unreadable),
                        "refuse": refuse,
                    },
                ),
            )
        return (
            self._inspect_step(),
            PipelineStep(
                action="inspect_structure",
                description="Read the project's structure",
                tool="developer.inspect_structure",
                target=str(self.root),
                expected="The project's layout is described",
            ),
        )

    def _inspect_step(self) -> PipelineStep:
        return self._step(
            "inspect_repository",
            "Inspect the repository",
            "A profile of the project (files, lines, packages)",
            tool="developer.inspect_repository",
            target=str(self.root),
        )

    @staticmethod
    def _step(
        action: str, description: str, expected: str, *, tool: str = "", target: str = ""
    ) -> PipelineStep:
        return PipelineStep(
            action=action,
            description=description,
            tool=tool or f"developer.{action}",
            target=target,
            expected=expected,
        )

    # -- execution ------------------------------------------------------------

    async def execute(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        handlers: dict[str, Callable[[PipelineStep, AgentRun], Awaitable[StepOutcome]]] = {
            "inspect_repository": self._do_inspect_repository,
            "inspect_structure": self._do_inspect_structure,
            "search_code": self._do_search_code,
            "inspect_errors": self._do_inspect_errors,
            "plan_changes": self._do_plan_changes,
            "apply_changes": self._do_apply_changes,
            "run_tests": self._do_run_tests,
            "diagnose": self._do_diagnose,
            "git": self._do_git,
            "verify_changes": self._do_verify_changes,
        }
        handler = handlers.get(step.action)
        if handler is None:
            return StepOutcome(
                step_id=step.id,
                action=step.action,
                status=StepStatus.SKIPPED,
                detail=f"{step.action} is not an action this agent performs",
                tool=step.tool,
            )
        return await handler(step, run)

    async def _do_inspect_repository(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        profile = self.engine.inspect()
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED,
            detail=(
                f"{profile.source_files} source file(s), {profile.test_files} test file(s), "
                f"{profile.python_lines} line(s), {len(profile.packages)} package(s)"
            ),
            output=profile.to_dict(),
            tool=step.tool,
        )

    async def _do_inspect_structure(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        structure = self.structure()
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED,
            detail=(
                f"{len(structure['directories'])} top-level director(ies), "
                f"packages: {', '.join(structure['packages'][:8]) or 'none'}"
                + (
                    f", {len(structure['modules'])} module(s)"
                    if structure.get("modules")
                    else ""
                )
            ),
            output=structure,
            tool=step.tool,
        )

    async def _do_search_code(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        result = search_code(
            self.root, step.target or run.goal, max_files=self.max_files_per_search
        )
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED,
            detail=(
                f"{len(result.matches)} match(es) across {result.files_scanned} file(s)"
                + (" (truncated)" if result.truncated else "")
            ),
            output=result.to_dict(),
            tool=step.tool,
        )

    async def _do_inspect_errors(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        errors = self.open_errors()
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED,
            detail=f"{len(errors)} open error(s)" if errors else "no open errors on record",
            output={"errors": list(errors), "checked": True},
            tool=step.tool,
        )

    async def _do_plan_changes(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        plan = self.engine.plan(step.target or run.goal)
        changes = self._build_changes(run.goal)
        self._pending[run.id] = changes
        self._trim_pending()
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED,
            detail=(
                f"{len(plan.actions)} planned action(s), {len(changes)} proposed file change(s)"
            ),
            output={
                "plan": plan.to_dict(),
                "changes": [change.to_dict() for change in changes],
                "writes_require_approval": True,
            },
            tool=step.tool,
        )

    async def _do_apply_changes(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        changes = self._pending.get(run.id, ())
        if not changes:
            return StepOutcome(
                step_id=step.id,
                action=step.action,
                status=StepStatus.SKIPPED,
                detail="no proposed change set was available to write",
                tool=step.tool,
            )
        results = await self.engine.apply_changes(changes, approval_gateway=self.apply_gateway)
        applied = [result for result in results if result.status is CodeChangeStatus.APPLIED]
        denied = [result for result in results if result.status is CodeChangeStatus.DENIED]
        output = {"results": [result.to_dict() for result in results], "applied": len(applied)}
        if applied:
            detail = f"{len(applied)} file(s) written"
            status = StepStatus.COMPLETED
        elif denied:
            detail = denied[0].message
            status = StepStatus.FAILED
        else:
            failed = [result.message for result in results if result.message]
            detail = failed[0] if failed else "no change was applied"
            status = StepStatus.FAILED
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=status,
            detail=detail,
            output=output,
            tool=step.tool,
        )

    async def _do_run_tests(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        # The command travels in the step as a list: the target string is for
        # display, and re-splitting it would break a path with a space in it.
        command = tuple(str(part) for part in step.metadata.get("command") or ())
        if not command:
            command = tuple(step.target.split()) if step.target else self.test_command
        result = await self.runner.run(command, cwd=self.root)
        report = parse_test_output(result)
        expect_failure = bool(step.metadata.get("expect_failure"))
        output = {
            "tests": report.to_dict(),
            "command": list(command),
            "returncode": result.returncode,
            "stdout_tail": result.output[-1200:],
            "expect_failure": expect_failure,
        }
        succeeded = self._tests_met_expectation(report, expect_failure)
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED if succeeded else StepStatus.FAILED,
            detail=(
                f"{report.passed} passed, {report.failed} failed, {report.errors} error(s), "
                f"{report.skipped} skipped"
                + ("" if report.recognised else " (no test tally in the output)")
            ),
            output=output,
            tool=step.tool,
        )

    async def _do_diagnose(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        text = self._failure_text(run)
        if not text:
            return StepOutcome(
                step_id=step.id,
                action=step.action,
                status=StepStatus.SKIPPED,
                detail="no failing output was recorded to diagnose",
                tool=step.tool,
            )
        diagnosis = diagnose_failure(text)
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED if diagnosis["kind"] != "unknown" else StepStatus.FAILED,
            detail=(
                f"{diagnosis['kind']}: {diagnosis['message']}"
                if diagnosis["message"]
                else str(diagnosis["kind"])
            ),
            output=diagnosis,
            tool=step.tool,
        )

    async def _do_git(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        argv = tuple(str(part) for part in step.metadata.get("argv", ())) or ("status",)
        unused = tuple(str(word) for word in step.metadata.get("unused") or ())
        unreadable = tuple(str(word) for word in step.metadata.get("unreadable") or ())
        output: dict[str, Any] = {
            "git": argv[0],
            "argv": list(argv),
            "mutating": bool(step.metadata.get("mutating")),
            "unused": list(unused),
            "unreadable": list(unreadable),
        }
        if step.metadata.get("refuse"):
            return StepOutcome(
                step_id=step.id,
                action=step.action,
                status=StepStatus.FAILED,
                detail=(
                    f"git {' '.join(argv)} was not run: "
                    f"{_quoted_list(unreadable)} could not be read as a git argument"
                ),
                output=output,
                tool=step.tool,
            )
        result = await self.runner.run(("git", *argv), cwd=self.root)
        output["returncode"] = result.returncode
        output["stdout_tail"] = result.output[-1200:]
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED if result.ok else StepStatus.FAILED,
            detail=(
                f"git {' '.join(argv)} -> exit {result.returncode}"
                + (
                    f" (not used: {_quoted_list((*unused, *unreadable))})"
                    if unused or unreadable
                    else ""
                )
            ),
            output=output,
            tool=step.tool,
        )

    async def _do_verify_changes(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        changes = self._pending.get(run.id, ())
        applied = {
            result["path"]
            for outcome in run.outcomes
            if outcome.action == "apply_changes"
            for result in outcome.output.get("results", [])
            if result.get("status") == CodeChangeStatus.APPLIED.value
        }
        checked: list[dict[str, Any]] = []
        for change in changes:
            target = (self.root / change.relative_path).resolve()
            if not str(target).startswith(str(self.root)):
                checked.append(
                    {
                        "path": change.relative_path,
                        "matches": False,
                        "reason": "outside the project root",
                    }
                )
                continue
            try:
                on_disk = target.read_text(encoding="utf-8")
            except OSError as exc:
                checked.append({"path": change.relative_path, "matches": False, "reason": str(exc)})
                continue
            checked.append(
                {
                    "path": change.relative_path,
                    "matches": on_disk == change.content,
                    "applied": str(target) in applied,
                }
            )
        tests = self._last_tests(run)
        verified = bool(checked) and all(entry["matches"] for entry in checked)
        if tests is not None:
            verified = verified and bool(tests.get("ok"))
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED if verified else StepStatus.FAILED,
            detail=(
                f"{sum(1 for entry in checked if entry['matches'])}/{len(checked)} "
                "file(s) match the proposal"
                + ("" if tests is None else f"; tests ok={bool(tests.get('ok'))}")
            ),
            output={"verified": verified, "files": checked, "tests": tests},
            tool=step.tool,
        )

    # -- verification ---------------------------------------------------------

    def verify(self, step: PipelineStep, outcome: StepOutcome, run: AgentRun) -> VerificationResult:
        if step.action == "run_tests":
            report = outcome.output.get("tests")
            if not isinstance(report, dict):
                return VerificationResult(
                    status=VerificationStatus.FAIL,
                    expectation=step.expected or "The test suite passes",
                    reason=outcome.detail or "the test run produced no readable result",
                    evidence={},
                    confidence=0.6,
                )
            if bool(outcome.output.get("expect_failure")):
                reproduced = int(report.get("failed", 0)) + int(report.get("errors", 0)) > 0
                return VerificationResult(
                    status=VerificationStatus.PASS if reproduced else VerificationStatus.FAIL,
                    expectation=step.expected or "The failing test is named in the output",
                    reason=(
                        f"the failure was reproduced: {report.get('failed', 0)} failed, "
                        f"{report.get('errors', 0)} error(s)"
                        if reproduced
                        else "the suite passed, so the reported failure was not reproduced"
                    ),
                    evidence={"tests": report},
                    confidence=0.9 if reproduced else 0.85,
                )
            if not report.get("ok"):
                return VerificationResult(
                    status=VerificationStatus.FAIL,
                    expectation=step.expected or "The test suite passes",
                    reason=(
                        f"tests did not pass: {report.get('failed')} failed, "
                        f"{report.get('errors')} error(s)"
                    ),
                    evidence={"tests": report},
                    confidence=0.9,
                )
            return VerificationResult(
                status=VerificationStatus.PASS,
                expectation=step.expected or "The test suite passes",
                reason=f"{report.get('passed', 0)} test(s) passed with no failures",
                evidence={"tests": report, "command": list(self.test_command)},
                confidence=0.95,
            )
        if step.action == "apply_changes":
            applied = int(outcome.output.get("applied", 0) or 0)
            if not outcome.ok or not applied:
                return VerificationResult(
                    status=VerificationStatus.FAIL,
                    expectation=step.expected or "Every proposed change is written",
                    reason=outcome.detail or "no proposed change was written",
                    evidence={"results": outcome.output.get("results", [])},
                    confidence=0.9,
                )
            return VerificationResult(
                status=VerificationStatus.PASS,
                expectation=step.expected or "Every proposed change is written",
                reason=f"{applied} file(s) written",
                evidence={"results": outcome.output.get("results", [])},
                confidence=0.8,
            )
        if step.action == "verify_changes":
            verified = bool(outcome.output.get("verified"))
            return VerificationResult(
                status=VerificationStatus.PASS if verified else VerificationStatus.FAIL,
                expectation=step.expected or "The files on disk match the proposal",
                reason=outcome.detail or self._verify_reason(verified),
                evidence={"files": outcome.output.get("files", [])},
                confidence=0.95 if verified else 0.85,
            )
        if step.action == "diagnose":
            diagnosis = outcome.output
            if not outcome.ok or diagnosis.get("kind") in (None, "", "unknown"):
                return VerificationResult(
                    status=VerificationStatus.INCONCLUSIVE,
                    expectation=step.expected or "A named failure with its cause",
                    reason=f"the failure signature was not recognised: {outcome.detail}",
                    evidence=dict(diagnosis),
                    confidence=0.3,
                )
            return VerificationResult(
                status=VerificationStatus.PASS,
                expectation=step.expected or "A named failure with its cause",
                reason=f"{diagnosis.get('kind')}: {diagnosis.get('message')}",
                evidence=dict(diagnosis),
                confidence=0.7,
            )
        return super().verify(step, outcome, run)

    # -- recovery -------------------------------------------------------------

    def recover(
        self, step: PipelineStep, outcome: StepOutcome, attempt: int, run: AgentRun
    ) -> RecoveryPlan:
        diagnosis = diagnose_failure(self._failure_text(run) or outcome.detail)
        if step.action in {"apply_changes", "verify_changes"}:
            # A write that failed may have half-written; re-running it is how a
            # repository ends up in a state nobody planned.
            return RecoveryPlan(
                diagnosis=f"{diagnosis['kind']}: {diagnosis['message']}",
                strategy=RecoveryStrategy.ABORT,
                text="A file write failed; stop and report it rather than retrying the write.",
                confidence=1.0,
            )
        if step.action == "git":
            mutating = bool(step.metadata.get("mutating"))
            return RecoveryPlan(
                diagnosis=f"{diagnosis['kind']}: {diagnosis['message']}",
                strategy=RecoveryStrategy.ABORT,
                text=(
                    "A repository command failed; report it rather than retrying it."
                    if mutating
                    else "The repository command failed; re-read the state before running it again."
                ),
                confidence=0.9,
            )
        if diagnosis["kind"] == "missing_dependency":
            return RecoveryPlan(
                diagnosis=f"missing dependency: {diagnosis['message']}",
                strategy=RecoveryStrategy.ABORT,
                text="A required dependency is missing — retrying cannot install it.",
                confidence=0.95,
            )
        if step.action == "run_tests" and attempt <= 1:
            return RecoveryPlan(
                diagnosis=f"{diagnosis['kind']}: {diagnosis['message']}",
                strategy=RecoveryStrategy.RETRY,
                steps=(step,),
                text="Re-run the suite once to rule out a flaky or ordering failure.",
                confidence=0.5,
            )
        return super().recover(step, outcome, attempt, run)

    # -- report ---------------------------------------------------------------

    def report(self, run: AgentRun) -> dict[str, Any]:
        report: dict[str, Any] = {
            "action": run.decision.get("action", ""),
            "project": run.context.get("project", {}),
            "root": str(self.root),
            "verified": run.verified,
            "wrote_files": self._wrote_files(run),
            "authorized": [grant.to_dict() for grant in run.grants if grant.allowed],
            "denied": [grant.to_dict() for grant in run.denied],
        }
        for action, key in (
            ("inspect_repository", "profile"),
            ("inspect_structure", "structure"),
            ("search_code", "search"),
            ("inspect_errors", "errors"),
            ("plan_changes", "plan"),
            ("apply_changes", "changes"),
            ("run_tests", "tests"),
            ("diagnose", "diagnosis"),
            ("git", "git"),
            ("verify_changes", "verification"),
        ):
            output = self._output_of(run, action)
            if not output:
                continue
            # The test run is reported as the tally itself, not wrapped in the
            # step's output: a caller asking "did the tests pass?" should not
            # have to know which step produced the answer.
            report[key] = output.get("tests") if action == "run_tests" else output
        return report

    def summarize(self, run: AgentRun) -> str:
        parts = [super().summarize(run)]
        tests = self._last_tests(run)
        if tests is not None:
            parts.append(
                f"Tests: {tests.get('passed', 0)} passed, {tests.get('failed', 0)} failed"
                f"{'' if tests.get('ok') else ' (not green)'}."
            )
        if self._wrote_files(run):
            applied = sum(
                int(outcome.output.get("applied", 0) or 0)
                for outcome in run.outcomes
                if outcome.action == "apply_changes"
            )
            parts.append(f"Wrote {applied} file(s) after authorization.")
        diagnosis = self._output_of(run, "diagnose")
        if diagnosis and diagnosis.get("message"):
            parts.append(f"Diagnosis: {diagnosis.get('kind')} — {diagnosis.get('message')}")
        git = self._output_of(run, "git")
        dropped = [*git.get("unused", ()), *git.get("unreadable", ())]
        if dropped:
            parts.append(f"Words not used in the git command: {_quoted_list(dropped)}.")
        return " ".join(parts)

    def structure(self) -> dict[str, Any]:
        """The project's shape, read shallowly and bounded."""
        context = self.project_context()
        directories: list[str] = []
        if self.root.is_dir():
            directories = sorted(
                path.name
                for path in self.root.iterdir()
                if path.is_dir()
                and path.name not in IGNORED_DIRECTORIES
                and not path.name.startswith(".")
            )[:50]
        # The source layout, read the way this project (and the common
        # src-layout) actually arranges it: `src/<package>` when a src directory
        # exists, otherwise the top level. The package's own subdirectories are
        # the modules, which is the useful answer for a monorepo-style package
        # and an empty list for a single-file one.
        source = self.root / "src" if (self.root / "src").is_dir() else self.root
        packages = (
            [
                path.name
                for path in sorted(source.iterdir())
                if path.is_dir()
                and not path.name.startswith((".", "__"))
                and path.name not in IGNORED_DIRECTORIES
            ][:50]
            if source.is_dir()
            else []
        )
        modules: list[str] = []
        if len(packages) == 1:
            inner = source / packages[0]
            modules = [
                path.name
                for path in sorted(inner.iterdir())
                if path.is_dir() and not path.name.startswith((".", "__"))
            ][:50]
        return {
            "project": context.to_dict(),
            "directories": directories,
            "packages": packages,
            "modules": modules,
            "root": str(self.root),
        }

    def open_errors(self) -> tuple[str, ...]:
        """The project's open errors: the bug log's, plus the context's."""
        errors: list[str] = []
        if self.bug_log is not None:
            try:
                records = self.bug_log.all(include_fixed=False)
                errors.extend(
                    str(getattr(record, "title", record))[:200] for record in list(records)[-5:]
                )
            except Exception:  # noqa: BLE001 - an unreadable log is not an error here
                pass
        for error in self.project_context().recent_errors:
            if error not in errors:
                errors.append(error)
        return tuple(errors)

    # -- helpers --------------------------------------------------------------

    def _route(self, goal: str) -> str:
        text = goal.lower()
        for action, pattern in _ACTION_PATTERNS:
            if re.search(pattern, text):
                return action
        return "inspect"

    @staticmethod
    def _route_reason(goal: str, action: str) -> str:
        return f"'{goal.strip()[:60]}' reads as a {action.replace('_', ' ')} request."

    @staticmethod
    def _search_query(goal: str) -> str:
        cleaned = re.sub(
            r"^\s*(?:please\s+)?(?:can you\s+)?(?:search|find|locate|grep|look)\s+(?:for\s+)?",
            "",
            goal.strip(),
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"\s+(?:in|across|through)\s+(?:the\s+)?(?:code|project|repo(?:sitory)?|codebase)\b.*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        return cleaned.strip(" ?.\"'") or goal.strip()

    def _has_pending_changes(self) -> bool:
        return self.change_builder is not None

    def _build_changes(self, goal: str) -> tuple[CodeChange, ...]:
        if self.change_builder is None:
            return ()
        try:
            return tuple(self.change_builder(goal))
        except Exception:  # noqa: BLE001 - a generator that fails proposes nothing
            return ()

    def _trim_pending(self) -> None:
        while len(self._pending) > 8:
            self._pending.pop(next(iter(self._pending)))

    @staticmethod
    def _output_of(run: AgentRun, action: str) -> dict[str, Any]:
        for outcome in run.outcomes:
            if outcome.action == action and outcome.output:
                return dict(outcome.output)
        return {}

    @staticmethod
    def _last_tests(run: AgentRun) -> dict[str, Any] | None:
        for outcome in reversed(run.outcomes):
            tests = outcome.output.get("tests")
            if isinstance(tests, dict):
                return dict(tests)
        return None

    @staticmethod
    def _verify_reason(verified: bool) -> str:
        return (
            "the written files match the proposal"
            if verified
            else "the files on disk do not match the proposal"
        )

    @staticmethod
    def _tests_met_expectation(report: TestReport, expect_failure: bool) -> bool:
        """Whether a test run gave the specialist what it asked for.

        A normal run wants green. A debugging run wants the failure it was
        asked about — and a suite that could not be read at all satisfies
        neither, which is why `recognised` gates both.
        """
        if not report.recognised:
            return False
        if expect_failure:
            return report.failed + report.errors > 0
        return report.ok

    @staticmethod
    def _failure_text(run: AgentRun) -> str:
        """Ending output from the last step that actually RAN, and nothing else.

        The detail of a denied or skipped step is a refusal or an explanation, not
        a failure to diagnose. Diagnosing one produced the worst kind of report:
        a refusal the agent itself made, presented to the user as the cause of a
        failure nobody ever reproduced.
        """
        for outcome in reversed(run.outcomes):
            text = str(outcome.output.get("stdout_tail") or "")
            if text:
                return text
            if outcome.status is StepStatus.FAILED and outcome.detail:
                return outcome.detail
        return ""

    @staticmethod
    def _wrote_files(run: AgentRun) -> bool:
        return any(
            result.get("status") == CodeChangeStatus.APPLIED.value
            for outcome in run.outcomes
            if outcome.action == "apply_changes"
            for result in outcome.output.get("results", [])
        )


def diagnose_failure(text: str) -> dict[str, Any]:
    """Name a failure from its output, or say that it was not recognised.

    Only signatures that are actually present in the text are reported: an
    unrecognised failure is reported as ``unknown`` rather than as a guess,
    because a wrong diagnosis sends the next attempt in the wrong direction.
    """
    location = re.search(r'File "([^"]+)", line (\d+)', text or "")
    if not text:
        return {"kind": "unknown", "message": "", "file": "", "line": 0}
    for kind, pattern in _FAILURE_SIGNATURES:
        match = re.search(pattern, text, re.MULTILINE)
        if match is None:
            continue
        message = next((group for group in match.groups() if group), "")
        return {
            "kind": kind,
            "message": " ".join(message.split())[:300],
            "file": location.group(1) if location else "",
            "line": int(location.group(2)) if location else 0,
        }
    if location is not None:
        return {
            "kind": "failure",
            "message": "the run failed without a recognised exception signature",
            "file": location.group(1),
            "line": int(location.group(2)),
        }
    return {
        "kind": "unknown",
        "message": " ".join((text or "").split())[-200:],
        "file": "",
        "line": 0,
    }
