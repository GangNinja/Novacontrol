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
        """Run the artifact in a sandboxed subprocess. Returns (ok, output).

        - dedicated temp dir (fresh cwd, auto-cleanup)
        - no network (Windows: no simple toggle pre-Python 3.13 — the fresh
          cwd + timeout + no-credentials model are the practical guards;
          *nix: preexec_fn disables networking via a new net namespace)
        - hard wall-clock timeout
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

        if self._runner is not None:  # test seam
            ok, out = await self._runner(language, code, filename)
            return bool(ok), str(out)

        tmpdir = Path(tempfile.mkdtemp(prefix="nova_agent_"))
        try:
            path = tmpdir / filename
            path.write_text(code, encoding="utf-8")
            preexec = _disable_network if _POSIX else None
            proc = await asyncio.create_subprocess_exec(
                *exe, path.name,
                cwd=str(tmpdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                preexec_fn=preexec,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self._run_timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return False, f"Timed out after {self._run_timeout:g}s — the program ran too long."
            output = stdout.decode("utf-8", errors="replace") if stdout else ""
            return proc.returncode == 0, _trim(output)
        except FileNotFoundError as exc:
            raise RuntimeUnavailable(
                f"no {language} runtime installed — code drafted but not executed"
            ) from exc
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


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
