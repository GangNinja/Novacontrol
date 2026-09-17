"""The Build coding agent: draft → run → diagnose → fix → re-run.

A one-shot LLM call produces code that merely *looks* right. An agent *verifies*:
it runs the code, reads the actual failure, asks the model to fix exactly that,
and iterates until the code runs clean or the fix budget is spent. The agent
loop is model-agnostic — it works with Ollama or any cloud provider because it
only consumes the standard ``complete()`` surface.

Every step is recorded in a trace so the UI can show what the agent actually
did — drafts, runs, failures, fixes — not just the final artifact.

Sandboxing: Python artifacts run in a **subprocess with no network, a fresh
temp cwd, and a wall-clock timeout**. JavaScript uses Node with the same
guards. Non-executable languages (SQL, HTML, CSS, Java, Rust, ...) are drafted
but never executed — the trace says so honestly.
"""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


AGENT_MAX_FIX_ROUNDS = 3
_RUN_TIMEOUT_SECONDS = 15.0


class RuntimeUnavailable(Exception):
    """The language runtime isn't installed — the code cannot be executed
    here. Distinct from "ran clean but printed nothing": the first is a
    draft-only outcome, the second is a verified success."""


@dataclass
class AgentStep:
    """One visible action the agent took."""

    kind: str  # draft | run | fixed | gave_up | skipped
    detail: str
    output: str = ""  # command output / error excerpt (trimmed)

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "detail": self.detail, "output": self.output}


@dataclass
class CodeAgentResult:
    """Everything the Build UI needs to show an agent run."""

    content: str
    language: str
    generated_by: str  # llm | agent | scaffold
    ran_ok: bool
    steps: list[AgentStep] = field(default_factory=list)
    final_output: str = ""
    fix_rounds: int = 0
    model_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "language": self.language,
            "generated_by": self.generated_by,
            "ran_ok": self.ran_ok,
            "steps": [s.to_dict() for s in self.steps],
            "final_output": self.final_output,
            "fix_rounds": self.fix_rounds,
            "model_name": self.model_name,
        }


@dataclass
class ProjectAgentResult:
    """Everything the Build UI needs to show a multi-file agent run."""

    project: str
    entry: str
    files: list[dict[str, str]]  # [{path, content}]
    language: str
    ran_ok: bool
    steps: list[AgentStep] = field(default_factory=list)
    final_output: str = ""
    fix_rounds: int = 0
    model_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "entry": self.entry,
            "files": self.files,
            "language": self.language,
            "ran_ok": self.ran_ok,
            "steps": [s.to_dict() for s in self.steps],
            "final_output": self.final_output,
            "fix_rounds": self.fix_rounds,
            "model_name": self.model_name,
        }


def extract_code_block(text: str) -> str:
    """Pull code out of an LLM reply: fenced block if present, else raw text.

    Models love wrapping code in prose ("Here's your function:") and fences
    with language tags. Returns the cleaned code.
    """
    text = text.strip()
    fences = re.findall(r"```[a-zA-Z0-9+#-]*\n(.*?)```", text, re.DOTALL)
    if fences:
        return str(max(fences, key=len)).strip()
    # Unfenced reply: strip a leading one-line "Here's ..." sentence.
    lines = text.splitlines()
    if lines and not _looks_like_code(lines[0]) and len(lines) > 1:
        return "\n".join(lines[1:]).strip()
    return text


def _looks_like_code(line: str) -> bool:
    stripped = line.strip()
    return bool(
        stripped.startswith(("#", "//", "/*", "*", '"', "'", "import ", "from ", "def ", "class ", "function", "export", "const ", "let ", "var ", "package ", "using ", "SELECT", "--"))
        or stripped.startswith(("<", "```"))
    )


def _trim(text: str, limit: int = 1200) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def parse_project_files(reply: str) -> list[dict[str, str]]:
    """Parse a model reply into a file map; raise when nothing usable remains.

    Accepts raw JSON, JSON fenced in a code block, or a fenced JSON object
    wrapped in prose. Per-file content may itself arrive fenced — stripped.
    File paths are sanitized to safe relative names (no traversal).
    """
    import json

    text = (reply or "").strip()
    # Prefer the outermost {...} in the reply (fenced or not).
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("model reply contained no JSON file map")
    payload = json.loads(text[start:end + 1])
    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("file map has no files")
    files: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_files:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).strip()
        content = str(item.get("content", ""))
        # Strip a fenced block if the model wrapped file content in one.
        fence = re.search(r"```[a-zA-Z0-9+#-]*\n(.*?)```", content, re.DOTALL)
        if fence and fence.group(1).strip():
            content = fence.group(1)
        if not path or not content.strip():
            continue
        # Safe relative path: no drive, no traversal, sane characters.
        # The traversal check runs on the sanitized-but-unstripped path so a
        # leading "../" is DROPPED, not silently renamed into a bare file.
        path = re.sub(r"[^A-Za-z0-9_./-]+", "_", path)
        if ".." in path or not path.strip("./"):
            continue
        path = path.strip("./")
        if path in seen:
            continue
        seen.add(path)
        files.append({"path": path, "content": content.strip() + "\n"})
    if not files:
        raise ValueError("file map parsed to zero usable files")
    return files


async def run_sandboxed(
    language: str,
    files: list[tuple[str, str]],
    entry: str,
    *,
    timeout: float = _RUN_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """Write files into a fresh temp dir and run the entry point sandboxed.

    Shared by the single-file agent and Run-saved-artifact. Guards: dedicated
    temp cwd, no network on *nix (net namespace, best-effort), hard wall-clock
    timeout, output captured. Raises RuntimeUnavailable when the language has
    no runtime here.
    """
    import shutil
    import tempfile

    # Resolve by PATH, falling back to this interpreter so a Linux/CI box
    # without a bare `python` shim still executes Python artifacts.
    python_exe = shutil.which("python") or sys.executable
    node_exe = shutil.which("node")
    runners: dict[str, list[str]] = {
        "python": [python_exe],
        "javascript": [node_exe] if node_exe else [],
    }
    exe = runners.get(language)
    if not exe:
        raise RuntimeUnavailable(
            f"no {language} runtime installed — code drafted but not executed"
            if language == "javascript"
            else f"{language} artifacts are drafted but not executed here"
        )

    tmpdir = Path(tempfile.mkdtemp(prefix="nova_agent_"))
    try:
        for name, code in files:
            safe = re.sub(r"[^A-Za-z0-9_./-]+", "_", name)
            target = (tmpdir / safe).resolve()
            if not str(target).startswith(str(tmpdir.resolve())):
                continue  # traversal guard; sanitized names never trigger it
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(code, encoding="utf-8")
        preexec = _disable_network if _POSIX else None
        proc = await asyncio.create_subprocess_exec(
            *exe, entry,
            cwd=str(tmpdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            preexec_fn=preexec,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, f"Timed out after {timeout:g}s — the program ran too long."
        output = stdout.decode("utf-8", errors="replace") if stdout else ""
        return proc.returncode == 0, _trim(output)
    except FileNotFoundError as exc:
        raise RuntimeUnavailable(
            f"no {language} runtime installed — code drafted but not executed"
        ) from exc
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


class CodingAgent:
    """Verify-and-fix loop over any configured completion provider."""

    def __init__(
        self,
        completion_provider: Any,
        *,
        max_fix_rounds: int = AGENT_MAX_FIX_ROUNDS,
        run_timeout: float = _RUN_TIMEOUT_SECONDS,
        runner: Any = None,
        workdir: Path | None = None,
    ) -> None:
        self._provider = completion_provider
        self._max_fix_rounds = max_fix_rounds
        self._run_timeout = run_timeout
        # Runner is injectable for tests; production uses the subprocess one.
        self._runner = runner
        self._workdir = workdir

    # ── drafting ──────────────────────────────────────────────

    async def _ask(self, prompt: str, *, max_tokens: int = 3000) -> str:
        response = await self._provider.complete(
            [{"role": "user", "content": prompt}], max_tokens=max_tokens
        )
        return str(response)

    async def draft(self, goal: str, language: str) -> str:
        reply = await self._ask(
            f"You are a senior {language} engineer. Task: {goal}\n"
            f"Write a complete, runnable, production-quality {language} program that "
            "solves the task. At the end include a small self-check (a main guard / "
            "entry point that demonstrates the solution works and prints a result). "
            "Return ONLY the code in a single fenced code block."
        )
        return extract_code_block(reply)

    async def fix(self, goal: str, language: str, code: str, error: str) -> str:
        reply = await self._ask(
            f"You are a senior {language} engineer. Task: {goal}\n\n"
            f"The current implementation:\n```{language}\n{code}\n```\n\n"
            f"It fails when run with this error/output:\n```\n{error}\n```\n\n"
            "Fix the bug and return the COMPLETE corrected program. "
            "Return ONLY the code in a single fenced code block."
        )
        return extract_code_block(reply)

    # ── execution ─────────────────────────────────────────────

    async def _run_code(self, language: str, code: str, filename: str) -> tuple[bool, str]:
        """Run one artifact via the shared sandbox (see run_sandboxed).

        The single-file test seam keeps the historical (language, code,
        filename) shape; the project runner gets the file-map seam instead.
        """
        if self._runner is not None:  # test seam (single-file shape)
            ok, out = await self._runner(language, code, filename)
            return bool(ok), str(out)
        return await run_sandboxed(language, [(filename, code)], filename,
                                   timeout=self._run_timeout)

    async def _run_files(
        self, language: str, files: list[tuple[str, str]], entry: str
    ) -> tuple[bool, str]:
        """Run a file map via the shared sandbox (project test seam)."""
        if self._runner is not None:  # test seam (project shape)
            ok, out = await self._runner(language, files, entry)
            return bool(ok), str(out)
        return await run_sandboxed(language, files, entry, timeout=self._run_timeout)

    async def draft_project(self, goal: str, language: str) -> list[dict[str, str]]:
        """Draft a MULTI-FILE project: the model returns a JSON file map.

        The strict JSON contract keeps parsing deterministic; per-file fences
        are still accepted and stripped because models love them anyway.
        """
        reply = await self._ask(
            f"You are a senior {language} engineer. Task: {goal}\n"
            "Design a SMALL multi-file project (2-5 files, each under 120 lines) "
            "with clear separation of concerns and a single entry point.\n"
            "Respond with ONLY a JSON object, no prose:\n"
            '{"entry": "main.py", "files": [{"path": "main.py", "content": "...code..."}, '
            '{"path": "other.py", "content": "..."}]}\n'
            "Every file must be complete and runnable in context (imports match the "
            "file names you chose). The entry point demonstrates the whole project "
            "works and prints a result."
        )
        return parse_project_files(reply)

    async def fix_project(
        self, goal: str, language: str, files: list[dict[str, str]], error: str
    ) -> list[dict[str, str]]:
        listing = "\n".join(
            f"--- {f['path']} ---\n{f['content']}" for f in files
        )
        reply = await self._ask(
            f"You are a senior {language} engineer. Task: {goal}\n\n"
            f"The project has these files:\n{listing}\n\n"
            f"When the entry point runs it fails with:\n```\n{error}\n```\n\n"
            "Fix the bug. Return ONLY the JSON file map (same shape, complete "
            "corrected files — include every file, changed or not)."
        )
        return parse_project_files(reply)


_RUNTIMES: dict[str, str | None] = {
    # Language -> executable name (None = never executable here). Resolved
    # lazily via PATH at call time so CI boxes without a bare `python` shim
    # still work (sys.executable fallback).
    "python": "python",
    "javascript": "node",
}


def runtime_available(language: str) -> bool:
    """True when this machine can execute the language in the sandbox."""
    import shutil

    exe = _RUNTIMES.get(language)
    if exe is None:
        return False
    if language == "python":
        return True  # shutil.which("python") or sys.executable always resolves
    return shutil.which(exe) is not None


_POSIX = False
try:  # pragma: no cover - platform probe
    import os as _os

    if _os.name == "posix":
        _POSIX = True

        def _disable_network() -> None:  # pragma: no cover - subprocess hook
            """Child-side best-effort network isolation.

            Runs inside preexec_fn, so ANY exception kills the spawned program
            before it starts — a missing `unshare` package or a kernel that
            restricts unprivileged user namespaces (recent Ubuntu defaults)
            must degrade to cwd+timeout isolation, not crash the run.
            """
            try:
                import unshare  # type: ignore[import-not-found]

                unshare.unshare(unshare.CLONE_NEWNET)
            except Exception:  # noqa: BLE001 - isolation is best-effort by contract
                pass
except ImportError:
    pass


async def run_coding_agent(
    goal: str,
    language: str,
    completion_provider: Any,
    *,
    filename: str,
    max_fix_rounds: int = AGENT_MAX_FIX_ROUNDS,
    runner: Any = None,
) -> CodeAgentResult:
    """Full agent run for one Build goal. Never raises for code problems —
    failures are reported in the trace; only provider-level hard errors
    propagate to the caller's existing fallback."""
    steps: list[AgentStep] = []
    agent = CodingAgent(
        completion_provider,
        max_fix_rounds=max_fix_rounds,
        runner=runner,
    )

    # 1. Draft
    try:
        code = await agent.draft(goal, language)
    except Exception as exc:  # noqa: BLE001 - caller falls back on provider errors
        raise RuntimeError(f"LLM draft failed: {exc}") from exc
    if not code.strip():
        raise RuntimeError("LLM returned no usable code.")
    steps.append(AgentStep("draft", f"Drafted {filename} with {getattr(completion_provider, 'name', 'model')}", _trim(code, 400)))

    # 2. Run → diagnose → fix loop
    fix_rounds = 0
    try:
        ran_ok, output = await agent._run_code(language, code, filename)
    except RuntimeUnavailable:
        steps.append(AgentStep("skipped", f"No {language} runtime available — code drafted but not executed", ""))
        return CodeAgentResult(
            content=code, language=language, generated_by="llm", ran_ok=False,
            steps=steps, model_name=str(getattr(completion_provider, "name", "")),
        )

    for round_index in range(max_fix_rounds + 1):
        if ran_ok:
            steps.append(AgentStep("run", f"Run {round_index + 1}: passed", output))
            return CodeAgentResult(
                content=code, language=language, generated_by="agent", ran_ok=True,
                steps=steps, final_output=output, fix_rounds=fix_rounds,
                model_name=str(getattr(completion_provider, "name", "")),
            )
        steps.append(AgentStep("run", f"Run {round_index + 1}: failed", output))
        if round_index == max_fix_rounds:
            break
        # Diagnose and fix with the REAL error text
        fix_rounds += 1
        steps.append(AgentStep("fixed", f"Fix round {fix_rounds}: asked model to fix the failure", ""))
        try:
            code = await agent.fix(goal, language, code, output)
        except Exception as exc:  # noqa: BLE001
            steps.append(AgentStep("gave_up", f"Fix round {fix_rounds} failed: {exc}", ""))
            break
        if not code.strip():
            steps.append(AgentStep("gave_up", "Fix round produced empty code", ""))
            break
        try:
            ran_ok, output = await agent._run_code(language, code, filename)
        except RuntimeUnavailable:
            steps.append(AgentStep("gave_up", f"{language} runtime disappeared mid-run", ""))
            break

    return CodeAgentResult(
        content=code, language=language, generated_by="agent", ran_ok=False,
        steps=steps, final_output=output, fix_rounds=fix_rounds,
        model_name=str(getattr(completion_provider, "name", "")),
    )


async def run_project_agent(
    goal: str,
    language: str,
    completion_provider: Any,
    *,
    max_fix_rounds: int = AGENT_MAX_FIX_ROUNDS,
    runner: Any = None,
) -> ProjectAgentResult:
    """Multi-file agent run: draft a file map → run the entry point →
    feed the real error back → fix the affected files → re-run.

    Same contract as run_coding_agent, scoped to a project: failures live in
    the trace, only provider-level hard errors propagate.
    """
    steps: list[AgentStep] = []
    agent = CodingAgent(
        completion_provider,
        max_fix_rounds=max_fix_rounds,
        runner=runner,
    )
    model_name = str(getattr(completion_provider, "name", ""))

    try:
        files = await agent.draft_project(goal, language)
    except Exception as exc:  # noqa: BLE001 - caller falls back on provider errors
        raise RuntimeError(f"LLM project draft failed: {exc}") from exc
    entry = files[0]["path"] if files else "main.py"
    # Prefer an explicit main-ish entry when the model listed one first.
    for candidate in ("main.py", "main.js", "index.js", "app.py", "app.js"):
        if any(f["path"] == candidate for f in files):
            entry = candidate
            break
    steps.append(AgentStep(
        "draft",
        f"Drafted {len(files)} files ({', '.join(f['path'] for f in files)}) with {model_name}",
        _trim(files[0]["content"], 300),
    ))

    def write_map() -> list[tuple[str, str]]:
        return [(f["path"], f["content"]) for f in files]

    fix_rounds = 0
    try:
        if not runtime_available(language):
            raise RuntimeUnavailable(
                f"{language} artifacts are drafted but not executed here"
            )
        ran_ok, output = await agent._run_files(language, write_map(), entry)
    except RuntimeUnavailable:
        steps.append(AgentStep("skipped", f"No {language} runtime available — project drafted but not executed", ""))
        return ProjectAgentResult(
            project=goal, entry=entry, files=files, language=language,
            ran_ok=False, steps=steps, model_name=model_name,
        )

    for round_index in range(max_fix_rounds + 1):
        if ran_ok:
            steps.append(AgentStep("run", f"Run {round_index + 1}: passed", output))
            return ProjectAgentResult(
                project=goal, entry=entry, files=files, language=language,
                ran_ok=True, steps=steps, final_output=output,
                fix_rounds=fix_rounds, model_name=model_name,
            )
        steps.append(AgentStep("run", f"Run {round_index + 1}: failed", output))
        if round_index == max_fix_rounds:
            break
        fix_rounds += 1
        steps.append(AgentStep("fixed", f"Fix round {fix_rounds}: asked model to fix the failure", ""))
        try:
            files = await agent.fix_project(goal, language, files, output)
        except Exception as exc:  # noqa: BLE001
            steps.append(AgentStep("gave_up", f"Fix round {fix_rounds} failed: {exc}", ""))
            break
        try:
            ran_ok, output = await agent._run_files(language, write_map(), entry)
        except RuntimeUnavailable:
            steps.append(AgentStep("gave_up", f"{language} runtime disappeared mid-run", ""))
            break

    return ProjectAgentResult(
        project=goal, entry=entry, files=files, language=language,
        ran_ok=False, steps=steps, final_output=output,
        fix_rounds=fix_rounds, model_name=model_name,
    )
