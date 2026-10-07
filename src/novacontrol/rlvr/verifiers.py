"""The verifiers: deterministic checks of observable facts.

A verifier answers a question that has a checkable answer — did the file exist,
did the process exit zero, did the tests pass, did the response carry the
required fields — and it answers it from an OBSERVATION, not from an opinion.
That is the whole point of verifiable rewards: when a deterministic check can
decide, no model is consulted, and the reward that follows rests on a fact a
reader can re-check.

Design rules every verifier here follows:

  * **The expectation is frozen before the run.** A
    :class:`~novacontrol.rlvr.models.VerificationRequest` carries ``expected``
    and its digest; a verifier never accepts an expectation that arrives after
    the observation, because that is how a policy could move the goalposts.
  * **Read-only.** A verifier reads a file, a report or a recorded state. It
    never writes, never deletes, and never executes a command unless a caller
    opted into execution explicitly (the test verifier's ``run`` mode), which is
    off by default.
  * **Honest statuses.** Missing evidence is ``INCONCLUSIVE``, not a failure;
    an unreadable workspace is ``ERROR``; only a checked fact can PASS or FAIL.
  * **Evidence is named.** Every verdict carries the observable facts it read,
    so a reward built on it can cite what it saw instead of asking anyone to
    trust a number.

The adapters cover the surfaces the phase requires: files, processes, tests,
HTTP responses, databases, git state, raw output and schemas — plus a custom
verifier so a plugin can register its own domain check through the existing
plugin SDK rather than a second extension mechanism.
"""

from __future__ import annotations

import abc
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from novacontrol.rlvr.models import (
    EVIDENCE_CUSTOM_CHECK,
    EVIDENCE_DATABASE_ROWS,
    EVIDENCE_EXIT_CODE,
    EVIDENCE_FILE_CONTENT,
    EVIDENCE_FILE_EXISTS,
    EVIDENCE_FILE_HASH,
    EVIDENCE_GIT_STATE,
    EVIDENCE_HTTP_STATUS,
    EVIDENCE_OUTPUT_MATCH,
    EVIDENCE_PROCESS_STATE,
    EVIDENCE_RESPONSE_SCHEMA,
    EVIDENCE_SCHEMA_VALID,
    EVIDENCE_TEST_RESULT,
    EVIDENCE_TESTS_PASSED,
    RLVR_VERSION,
    CritiqueCategory,
    RiskLevel,
    VerificationRequest,
    VerificationResult,
    VerificationStatus,
    VerifierCategory,
    VerifierMetadata,
    _mapping,
    _text,
    _texts,
)

#: How many characters of observed text are quoted into evidence.
QUOTE_LIMIT = 200


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _quote(value: Any, limit: int = QUOTE_LIMIT) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _safe_join(root: str, relative: str) -> Path | None:
    """Resolve ``relative`` under ``root`` without ever escaping it.

    A verifier must not be usable as a file-read gadget: a policy that could ask
    ``FileVerifier`` to read ``../../secrets`` would have found a way around the
    permission layer. Escaping the workspace is refused, not clamped.
    """
    if not root:
        return None
    base = Path(root).resolve()
    target = (base / relative).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


class Verifier(abc.ABC):
    """One checkable question, with a deterministic answer when it has one."""

    #: Stable identity: what a registry keys on and a reward cites.
    verifier_id = "verifier"
    #: Human name and one-sentence description.
    name = "Verifier"
    description = ""
    category: str = VerifierCategory.CUSTOM.value
    version: str = RLVR_VERSION
    risk_level: str = RiskLevel.LOW.value
    deterministic: bool = True
    confidence_characteristics: str = ""
    task_types: tuple[str, ...] = ()
    #: The expected keys this verifier understands, as a small JSON-schema-like
    #: mapping. Documentation, not enforcement — ``validate`` is enforcement.
    input_schema: Mapping[str, Any] = {}
    output_schema: Mapping[str, Any] = {}

    # -- metadata ---------------------------------------------------------------

    def metadata(self) -> VerifierMetadata:
        """What this verifier says about itself, for the registry and reports."""
        return VerifierMetadata(
            verifier_id=self.verifier_id,
            name=self.name,
            description=self.description,
            category=self.category,
            version=self.version,
            risk_level=self.risk_level,
            deterministic=self.deterministic,
            confidence_characteristics=self.confidence_characteristics,
            task_types=self.task_types,
            input_schema=dict(self.input_schema),
            output_schema=dict(self.output_schema),
            enabled=True,
        )

    def validate(self, request: VerificationRequest) -> tuple[str, ...]:
        """Every reason this verifier cannot answer this request (empty is good).

        The base rule is the one that matters for every category: an
        expectation must be present, and its digest must match the content, so a
        request whose expected result was edited after the fact is refused
        before any check runs.
        """
        problems: list[str] = []
        if not request.expected and not request.observation:
            problems.append("the request carries neither an expectation nor an observation")
        if request.expected_digest:
            current = expected_digest_of(request.expected)
            if current != request.expected_digest:
                problems.append(
                    "the expected result changed after this request was frozen "
                    f"(digest {request.expected_digest} != {current})"
                )
        return tuple(problems)

    # -- checking ---------------------------------------------------------------

    @abc.abstractmethod
    def verify(self, request: VerificationRequest) -> VerificationResult:
        """Check the observation against the expectation. Never raises."""

    # -- explaining -------------------------------------------------------------

    def explain(self, result: VerificationResult) -> str:
        """One sentence a report can print about this verdict."""
        if result.status == VerificationStatus.PASS.value:
            return (
                f"{self.verifier_id} verified the outcome "
                f"({', '.join(result.evidence) or 'no evidence named'})"
            )
        if result.status == VerificationStatus.FAIL.value:
            return (
                f"{self.verifier_id} rejected the outcome: "
                f"{result.detail or 'the expectation was not met'}"
            )
        if result.status == VerificationStatus.PARTIAL.value:
            return (
                f"{self.verifier_id} partially verified the outcome "
                f"(score {result.reading():.2f}): {result.detail or 'some checks passed'}"
            )
        return f"{self.verifier_id} could not decide: {result.detail or 'no reading'}"

    def confidence(self, result: VerificationResult) -> float | None:
        """How much this verdict should be trusted. Deterministic checks say 1."""
        if result.confidence is not None:
            return max(0.0, min(1.0, float(result.confidence)))
        if self.deterministic and result.factual:
            return 1.0
        return None

    def describe(self) -> dict[str, Any]:
        return dict(self.metadata().to_dict())

    # -- shared result building --------------------------------------------------

    def _problem(self, request: VerificationRequest, problems: Sequence[str]) -> VerificationResult:
        return self._result(
            request,
            VerificationStatus.ERROR,
            detail="; ".join(problems),
            error_category=CritiqueCategory.OTHER.value,
        )

    def _result(
        self,
        request: VerificationRequest,
        status: str,
        *,
        detail: str = "",
        evidence: Sequence[str] = (),
        observed: Mapping[str, Any] | None = None,
        score: float | None = None,
        confidence: float | None = None,
        error_category: str = "",
    ) -> VerificationResult:
        return VerificationResult(
            task_id=request.task_id,
            trajectory_id=request.trajectory_id,
            step_id=request.step_id,
            verifier_id=self.verifier_id,
            verifier_version=self.version,
            status=status,
            passed=status == VerificationStatus.PASS.value,
            score=score,
            scope=request.scope,
            evidence=tuple(evidence),
            expected=dict(request.expected),
            observed=dict(observed if observed is not None else request.observation),
            expected_digest=request.expected_digest,
            error_category=error_category,
            confidence=confidence,
            detail=detail,
        )

    def _verdict(
        self,
        request: VerificationRequest,
        checks: Sequence[tuple[bool, str]],
        *,
        evidence: Sequence[str],
        observed: Mapping[str, Any],
        error_category: str = "",
        pass_detail: str = "",
    ) -> VerificationResult:
        """Turn a list of (passed, description) checks into one honest verdict."""
        if not checks:
            return self._result(
                request,
                VerificationStatus.INCONCLUSIVE,
                detail="no check could be evaluated from the observation",
                evidence=evidence,
                observed=observed,
            )
        passed = sum(1 for ok, _ in checks if ok)
        failed = [detail for ok, detail in checks if not ok]
        ratio = passed / len(checks)
        if passed == len(checks):
            return self._result(
                request,
                VerificationStatus.PASS,
                detail=pass_detail or f"all {len(checks)} check(s) passed",
                evidence=evidence,
                observed=observed,
                score=1.0,
            )
        if passed == 0:
            return self._result(
                request,
                VerificationStatus.FAIL,
                detail="; ".join(failed),
                evidence=evidence,
                observed=observed,
                score=0.0,
                error_category=error_category,
            )
        return self._result(
            request,
            VerificationStatus.PARTIAL,
            detail=f"{passed}/{len(checks)} check(s) passed; failed: " + "; ".join(failed),
            evidence=evidence,
            observed=observed,
            score=round(ratio, 6),
            error_category=error_category,
        )


def expected_digest_of(expected: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(expected), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class FileVerifier(Verifier):
    """Checks a file: existence, content, hash, size, structure and JSON body.

    The expectation names what must be true (``exists``, ``content``,
    ``contains``, ``sha256``, ``size``, ``entries``, ``json``); the file is read
    from the request's workspace when the observation does not already carry its
    facts. A path outside the workspace is refused — reading arbitrary files on
    request would be a permission bypass, not a verification.
    """

    verifier_id = "file"
    name = "File verifier"
    description = "Checks that a file exists and its content, hash and structure match."
    category = VerifierCategory.FILE.value
    task_types = ("file", "write", "build", "artifact")
    input_schema = {
        "expected": {
            "path": "str (required)",
            "exists": "bool",
            "content": "str (exact)",
            "contains": "str | [str]",
            "sha256": "hex digest",
            "size": "int",
            "entries": "[str] (relative paths)",
            "json": "mapping (parsed content)",
        },
        "observation": {"file": {"exists": "bool", "content": "str", "sha256": "str"}},
    }
    output_schema = {"status": "pass | fail | partial | inconclusive | error"}

    def validate(self, request: VerificationRequest) -> tuple[str, ...]:
        problems = list(super().validate(request))
        expected = request.expected
        path = _text(expected.get("path")) or _text(request.action.get("path"))
        if not path:
            problems.append("expected.path is required")
        if not any(
            key in expected
            for key in ("exists", "content", "contains", "sha256", "size", "entries", "json")
        ):
            problems.append(
                "the expectation names no file property (exists/content/contains/"
                "sha256/size/entries/json)"
            )
        return tuple(problems)

    def verify(self, request: VerificationRequest) -> VerificationResult:
        problems = self.validate(request)
        if problems:
            return self._problem(request, problems)
        expected = request.expected
        path = _text(expected.get("path")) or _text(request.action.get("path"))
        observed = self._read(path, request)
        if observed.get("unreadable"):
            return self._result(
                request,
                VerificationStatus.ERROR,
                detail=str(observed.get("reason") or "the file could not be read"),
                evidence=(EVIDENCE_FILE_EXISTS,),
                observed=observed,
                error_category=CritiqueCategory.CONTEXT_ERROR.value,
            )

        checks: list[tuple[bool, str]] = []
        evidence: list[str] = []
        want_exists = expected.get("exists", True)
        if isinstance(want_exists, bool):
            checks.append((bool(observed.get("exists")) == want_exists, f"exists={want_exists}"))
            evidence.append(EVIDENCE_FILE_EXISTS)
        if "exists" in expected and not want_exists and not observed.get("exists"):
            return self._result(
                request,
                VerificationStatus.PASS,
                detail="the file is absent, as expected",
                evidence=(EVIDENCE_FILE_EXISTS,),
                observed=observed,
                score=1.0,
            )
        if not observed.get("exists"):
            return self._result(
                request,
                VerificationStatus.FAIL,
                detail=f"file {path!r} does not exist",
                evidence=(EVIDENCE_FILE_EXISTS,),
                observed=observed,
                score=0.0,
                error_category=CritiqueCategory.EXECUTION_ERROR.value,
            )
        content = observed.get("content")
        if "content" in expected and isinstance(content, str):
            want = str(expected.get("content"))
            checks.append((content == want, "content differs"))
            evidence.append(EVIDENCE_FILE_CONTENT)
        if "contains" in expected and isinstance(content, str):
            wanted = expected.get("contains")
            parts = (
                [str(item) for item in wanted]
                if isinstance(wanted, (list, tuple))
                else [str(wanted)]
            )
            for part in parts:
                checks.append((part in content, f"content does not contain {_quote(part)}"))
            evidence.append(EVIDENCE_FILE_CONTENT)
        if "sha256" in expected and observed.get("sha256"):
            checks.append(
                (
                    str(observed.get("sha256")) == str(expected.get("sha256")).lower(),
                    "sha256 differs",
                )
            )
            evidence.append(EVIDENCE_FILE_HASH)
        if "size" in expected and isinstance(observed.get("size"), int):
            checks.append(
                (int(observed["size"]) == int(expected["size"]), "size differs")
            )
            evidence.append(EVIDENCE_FILE_EXISTS)
        if "entries" in expected and isinstance(observed.get("entries"), (list, tuple)):
            wanted = {str(item) for item in expected.get("entries") or []}
            present = {str(item) for item in observed.get("entries") or []}
            missing = sorted(wanted - present)
            checks.append((not missing, f"missing entries: {', '.join(missing)}"))
            evidence.append(EVIDENCE_FILE_EXISTS)
        if "json" in expected and isinstance(observed.get("json"), Mapping):
            checks.append(
                (dict(observed["json"]) == dict(expected["json"]), "parsed JSON differs")
            )
            evidence.append(EVIDENCE_FILE_CONTENT)
        return self._verdict(
            request,
            checks,
            evidence=tuple(dict.fromkeys(evidence)),
            observed=observed,
            error_category=CritiqueCategory.EXECUTION_ERROR.value,
            pass_detail=f"file {path!r} matches every expected property",
        )

    def _read(self, relative: str, request: VerificationRequest) -> dict[str, Any]:
        """The observation's file facts, or the file read from the workspace."""
        facts = _mapping(request.observation.get("file"))
        if facts:
            return facts
        root = request.workspace
        if not root:
            return {
                "unreadable": True,
                "reason": (
                    "no workspace is set and the observation carries no file facts"
                ),
            }
        target = _safe_join(root, relative)
        if target is None:
            return {
                "unreadable": True,
                "reason": f"path {relative!r} escapes the workspace",
            }
        try:
            exists = target.exists()
            raw = target.read_bytes() if target.is_file() else b""
        except OSError as exc:
            return {"unreadable": True, "reason": f"{type(exc).__name__}: {exc}"}
        content = ""
        parsed: Mapping[str, Any] | None = None
        if raw:
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                content = raw.decode("utf-8", errors="replace")
            try:
                loaded = json.loads(content)
            except (ValueError, TypeError):
                loaded = None
            if isinstance(loaded, Mapping):
                parsed = loaded
        entries: list[str] = []
        if target.is_dir():
            try:
                entries = sorted(
                    str(item.relative_to(target)) for item in target.rglob("*")
                )
            except OSError:
                entries = []
        observed: dict[str, Any] = {
            "path": relative,
            "exists": exists,
            "content": content,
            "sha256": _digest_bytes(raw) if raw else None,
            "size": len(raw) if target.is_file() else None,
            "entries": entries,
        }
        if parsed is not None:
            observed["json"] = dict(parsed)
        return observed


class ProcessVerifier(Verifier):
    """Checks a process: running, exited, and with which exit code and output.

    The process facts come from the observation — the runner that executed the
    command recorded what happened — rather than from spawning anything here. A
    caller that wants a live probe can set ``probe_live`` and provide a pid; the
    probe is used only when psutil is installed, and its absence is
    INCONCLUSIVE, never a guessed answer.
    """

    verifier_id = "process"
    name = "Process verifier"
    description = "Checks that a process is running or exited with the expected code and output."
    category = VerifierCategory.PROCESS.value
    task_types = ("process", "command", "execution")
    input_schema = {
        "expected": {
            "state": "running | exited",
            "exit_code": "int",
            "stdout_contains": "str | [str]",
            "stderr_contains": "str | [str]",
            "pid": "int (for a live probe)",
            "probe_live": "bool",
        },
        "observation": {
            "process": {
                "running": "bool",
                "exit_code": "int",
                "stdout": "str",
                "stderr": "str",
            }
        },
    }
    output_schema = {"status": "pass | fail | partial | inconclusive | error"}

    def validate(self, request: VerificationRequest) -> tuple[str, ...]:
        problems = list(super().validate(request))
        expected = request.expected
        if not any(
            key in expected
            for key in ("state", "exit_code", "stdout_contains", "stderr_contains", "pid")
        ):
            problems.append(
                "the expectation names no process property (state/exit_code/"
                "stdout_contains/stderr_contains/pid)"
            )
        return tuple(problems)

    def verify(self, request: VerificationRequest) -> VerificationResult:
        problems = self.validate(request)
        if problems:
            return self._problem(request, problems)
        expected = request.expected
        facts = _mapping(request.observation.get("process"))
        if not facts and bool(expected.get("probe_live")):
            facts = self.probe_live(expected.get("pid"))
        if not facts:
            return self._result(
                request,
                VerificationStatus.INCONCLUSIVE,
                detail=(
                    "the observation carries no process facts and no live probe "
                    "was requested or possible"
                ),
            )
        checks: list[tuple[bool, str]] = []
        evidence: list[str] = []
        state = _text(expected.get("state")).lower()
        if state:
            running = bool(facts.get("running"))
            checks.append((running == (state == "running"), f"state is not {state!r}"))
            evidence.append(EVIDENCE_PROCESS_STATE)
        if "exit_code" in expected:
            observed_code = facts.get("exit_code")
            checks.append(
                (
                    observed_code is not None
                    and int(observed_code) == int(expected.get("exit_code", 0)),
                    f"exit code is {observed_code!r}, not {expected.get('exit_code')!r}",
                )
            )
            evidence.append(EVIDENCE_EXIT_CODE)
        for key, stream in (("stdout_contains", "stdout"), ("stderr_contains", "stderr")):
            if key not in expected:
                continue
            wanted = expected.get(key)
            parts = (
                [str(item) for item in wanted]
                if isinstance(wanted, (list, tuple))
                else [str(wanted)]
            )
            text = str(facts.get(stream) or "")
            for part in parts:
                checks.append((part in text, f"{stream} does not contain {_quote(part)}"))
            evidence.append(EVIDENCE_PROCESS_STATE)
        return self._verdict(
            request,
            checks,
            evidence=tuple(dict.fromkeys(evidence)),
            observed=facts,
            error_category=CritiqueCategory.EXECUTION_ERROR.value,
        )

    def probe_live(self, pid: Any) -> dict[str, Any]:
        """A live process reading when psutil is installed; empty otherwise.

        Returning an empty mapping is the honest answer: this verifier will not
        report a process as running because a caller hoped it was.
        """
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return {}
        try:
            import psutil
        except Exception:  # noqa: BLE001 - absent psutil is a normal condition
            return {}
        try:
            process = psutil.Process(pid)
            running = process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except Exception:  # noqa: BLE001 - a probe failure is not a verdict
            return {}
        return {"pid": pid, "running": bool(running), "exit_code": None}


class TestVerifier(Verifier):
    """Checks that tests passed, and how many ran, failed or were skipped.

    The report comes from the observation (the runner recorded the counts), or
    from a JSON report file the run wrote — those are facts. A caller may also
    opt into running the command itself (``allow_execution``), in which case
    only the exit code is a fact and an expectation about COUNTS is answered
    INCONCLUSIVE rather than guessed from stdout parsing.
    """

    verifier_id = "test"
    name = "Test verifier"
    description = "Checks that a test run passed, with its counts and failures."
    category = VerifierCategory.TEST.value
    task_types = ("test", "pytest", "verification")
    input_schema = {
        "expected": {
            "result": "pass | fail",
            "min_passed": "int",
            "max_failed": "int",
            "total": "int",
            "command": "str (run mode)",
            "report": "path to a JSON report",
            "run": "bool (execute the command)",
            "timeout_s": "float",
        },
        "observation": {
            "tests": {
                "passed": "int",
                "failed": "int",
                "total": "int",
                "skipped": "int",
                "errors": "int",
                "failures": "[str]",
                "exit_code": "int",
            }
        },
    }
    output_schema = {"status": "pass | fail | partial | inconclusive | error"}

    def __init__(self, *, allow_execution: bool = False) -> None:
        #: Execution is off by default: a verifier that runs arbitrary commands
        #: is a capability, and a caller must ask for it explicitly.
        self.allow_execution = bool(allow_execution)

    def validate(self, request: VerificationRequest) -> tuple[str, ...]:
        problems = list(super().validate(request))
        expected = request.expected
        if not any(
            key in expected
            for key in ("result", "min_passed", "max_failed", "total", "command", "report")
        ):
            problems.append(
                "the expectation names no test property (result/min_passed/"
                "max_failed/total/command/report)"
            )
        if bool(expected.get("run")) and not self.allow_execution:
            problems.append(
                "expected.run is set but this verifier was built without "
                "allow_execution: refusing to run a command on request"
            )
        return tuple(problems)

    def verify(self, request: VerificationRequest) -> VerificationResult:
        problems = self.validate(request)
        if problems:
            return self._problem(request, problems)
        expected = request.expected
        report = self._report(request)
        if report is None:
            return self._result(
                request,
                VerificationStatus.INCONCLUSIVE,
                detail=(
                    "no test report was observed and none could be read; a "
                    "count-based expectation is not answered by a guess"
                ),
            )
        checks: list[tuple[bool, str]] = []
        evidence: list[str] = [EVIDENCE_TEST_RESULT]
        failed_count = report.get("failed")
        errors = report.get("errors")
        total_failed = (
            int(failed_count) + int(errors)
            if isinstance(failed_count, int) and isinstance(errors, int)
            else failed_count
        )
        result_wanted = _text(expected.get("result")).lower()
        if result_wanted:
            passed = bool(report.get("passed")) and total_failed in (0, None)
            checks.append((passed == (result_wanted == "pass"), f"result is not {result_wanted!r}"))
        if "min_passed" in expected:
            passed_n = report.get("passed")
            checks.append(
                (
                    isinstance(passed_n, int) and passed_n >= int(expected.get("min_passed", 0)),
                    f"{report.get('passed')!r} tests passed, expected at least "
                    f"{expected.get('min_passed')}",
                )
            )
            evidence.append(EVIDENCE_TESTS_PASSED)
        if "max_failed" in expected:
            limit = int(expected.get("max_failed", 0))
            checks.append(
                (
                    isinstance(total_failed, int) and total_failed <= limit,
                    f"{total_failed!r} tests failed, expected at most {limit}",
                )
            )
        if "total" in expected:
            checks.append(
                (
                    report.get("total") == int(expected.get("total", 0)),
                    f"{report.get('total')!r} tests ran, expected {expected.get('total')}",
                )
            )
        return self._verdict(
            request,
            checks,
            evidence=tuple(dict.fromkeys(evidence)),
            observed=report,
            error_category=CritiqueCategory.VERIFICATION_ERROR.value,
            pass_detail="the test run matches the expectation",
        )

    def _report(self, request: VerificationRequest) -> dict[str, Any] | None:
        facts = _mapping(request.observation.get("tests"))
        if facts:
            return facts
        report_path = _text(request.expected.get("report"))
        if report_path and request.workspace:
            target = _safe_join(request.workspace, report_path)
            if target is None or not target.is_file():
                return None
            try:
                loaded = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return None
            if isinstance(loaded, Mapping):
                return dict(loaded)
            return None
        if bool(request.expected.get("run")) and self.allow_execution:
            return self._run(request)
        return None

    def _run(self, request: VerificationRequest) -> dict[str, Any] | None:
        import subprocess

        command = _text(request.expected.get("command"))
        if not command or not request.workspace:
            return None
        target = Path(request.workspace).resolve()
        try:
            completed = subprocess.run(  # noqa: S603 - execution was explicitly enabled
                command,
                shell=True,
                cwd=str(target),
                capture_output=True,
                text=True,
                timeout=max(1.0, float(request.timeout_s)),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return {
            "exit_code": completed.returncode,
            "passed": completed.returncode == 0,
            "failed": 0 if completed.returncode == 0 else None,
            "command": command,
        }


class HTTPVerifier(Verifier):
    """Checks a recorded HTTP response: status, schema, fields, deterministic parts.

    The response is the observation — the run that made the request recorded it.
    This verifier deliberately makes no network calls of its own: a verifier
    that re-issues the request would be a second, uncontrolled execution path,
    and the point is to check what actually happened.
    """

    verifier_id = "http"
    name = "HTTP verifier"
    description = "Checks a recorded response's status, schema, required fields and values."
    category = VerifierCategory.HTTP.value
    task_types = ("http", "api", "network")
    input_schema = {
        "expected": {
            "status": "int",
            "status_in": "[int]",
            "schema": "mapping (required fields -> type name)",
            "fields": "mapping (field -> exact value)",
            "contains": "str",
            "max_latency_ms": "float",
        },
        "observation": {
            "http": {
                "status": "int",
                "body": "str | mapping",
                "headers": "mapping",
                "latency_ms": "float",
            }
        },
    }
    output_schema = {"status": "pass | fail | partial | inconclusive | error"}

    def validate(self, request: VerificationRequest) -> tuple[str, ...]:
        problems = list(super().validate(request))
        expected = request.expected
        if not any(
            key in expected
            for key in ("status", "status_in", "schema", "fields", "contains", "max_latency_ms")
        ):
            problems.append(
                "the expectation names no response property (status/status_in/"
                "schema/fields/contains/max_latency_ms)"
            )
        return tuple(problems)

    def verify(self, request: VerificationRequest) -> VerificationResult:
        problems = self.validate(request)
        if problems:
            return self._problem(request, problems)
        expected = request.expected
        facts = _mapping(request.observation.get("http"))
        if not facts or "status" not in facts:
            return self._result(
                request,
                VerificationStatus.INCONCLUSIVE,
                detail="the observation carries no recorded HTTP response",
            )
        body = facts.get("body")
        parsed = _mapping(body) if isinstance(body, Mapping) else {}
        checks: list[tuple[bool, str]] = []
        evidence: list[str] = [EVIDENCE_HTTP_STATUS]
        if "status" in expected:
            checks.append(
                (int(facts.get("status", 0)) == int(expected.get("status", 0)),
                 f"status is {facts.get('status')!r}, expected {expected.get('status')!r}")
            )
        if "status_in" in expected:
            wanted = expected.get("status_in")
            allowed = {int(item) for item in wanted} if isinstance(wanted, (list, tuple)) else set()
            checks.append(
                (int(facts.get("status", 0)) in allowed,
                 f"status {facts.get('status')!r} is not in {sorted(allowed)}")
            )
        if "schema" in expected and isinstance(expected.get("schema"), Mapping):
            schema = _mapping(expected.get("schema"))
            missing = [name for name in schema if name not in parsed]
            wrong = [
                name
                for name, kind in schema.items()
                if name in parsed and not _is_type(parsed[name], str(kind))
            ]
            checks.append(
                (not missing and not wrong, f"schema: missing {missing}, wrong type {wrong}")
            )
            evidence.append(EVIDENCE_RESPONSE_SCHEMA)
        if "fields" in expected and isinstance(expected.get("fields"), Mapping):
            for name, value in _mapping(expected.get("fields")).items():
                checks.append(
                    (
                        parsed.get(name) == value,
                        f"field {name!r} is {parsed.get(name)!r}, expected {value!r}",
                    )
                )
            evidence.append(EVIDENCE_RESPONSE_SCHEMA)
        if "contains" in expected:
            text = body if isinstance(body, str) else json.dumps(_jsonable(body), default=str)
            checks.append(
                (str(expected.get("contains")) in text,
                 f"body does not contain {_quote(expected.get('contains'))}")
            )
        if "max_latency_ms" in expected and isinstance(facts.get("latency_ms"), (int, float)):
            checks.append(
                (float(facts["latency_ms"]) <= float(expected.get("max_latency_ms", 0.0)),
                 f"latency {facts.get('latency_ms')}ms exceeds {expected.get('max_latency_ms')}ms")
            )
        return self._verdict(
            request,
            checks,
            evidence=tuple(dict.fromkeys(evidence)),
            observed=facts,
            error_category=CritiqueCategory.OUTPUT_FORMAT_ERROR.value,
        )


class DatabaseVerifier(Verifier):
    """Checks a database state: expected records, values and state changes.

    The state is observed, not queried live: the run recorded the rows it read
    or wrote, and the verifier checks those against the expectation. That keeps
    the check deterministic and free of a second connection to a live database.
    """

    verifier_id = "database"
    name = "Database verifier"
    description = "Checks observed rows, field values and state changes against expectations."
    category = VerifierCategory.DATABASE.value
    task_types = ("database", "record", "state")
    input_schema = {
        "expected": {
            "rows": "[mapping] (each must match a field subset of some observed row)",
            "values": "mapping (field -> value, checked on the first row)",
            "count": "int (exact row count)",
            "changes": "[mapping] (applied changes -> field subset)",
        },
        "observation": {
            "database": {"rows": "[mapping]", "changes": "[mapping]", "state": "mapping"}
        },
    }
    output_schema = {"status": "pass | fail | partial | inconclusive | error"}

    def validate(self, request: VerificationRequest) -> tuple[str, ...]:
        problems = list(super().validate(request))
        expected = request.expected
        if not any(key in expected for key in ("rows", "values", "count", "changes")):
            problems.append(
                "the expectation names no database property (rows/values/count/changes)"
            )
        return tuple(problems)

    def verify(self, request: VerificationRequest) -> VerificationResult:
        problems = self.validate(request)
        if problems:
            return self._problem(request, problems)
        expected = request.expected
        facts = _mapping(request.observation.get("database"))
        if not facts:
            return self._result(
                request,
                VerificationStatus.INCONCLUSIVE,
                detail="the observation carries no database state",
            )
        rows = facts.get("rows")
        observed_rows = (
            [dict(item) for item in rows if isinstance(item, Mapping)]
            if isinstance(rows, (list, tuple))
            else []
        )
        changes = facts.get("changes")
        observed_changes = (
            [dict(item) for item in changes if isinstance(item, Mapping)]
            if isinstance(changes, (list, tuple))
            else []
        )
        checks: list[tuple[bool, str]] = []
        evidence: list[str] = [EVIDENCE_DATABASE_ROWS]
        if "count" in expected:
            checks.append(
                (len(observed_rows) == int(expected.get("count", 0)),
                 f"{len(observed_rows)} row(s) observed, expected {expected.get('count')}")
            )
        if "rows" in expected and isinstance(expected.get("rows"), (list, tuple)):
            for wanted in expected.get("rows") or []:
                if not isinstance(wanted, Mapping):
                    continue
                checks.append(
                    (
                        _row_matches(dict(wanted), observed_rows),
                        f"no observed row matches {dict(wanted)!r}",
                    )
                )
        if "changes" in expected and isinstance(expected.get("changes"), (list, tuple)):
            for wanted in expected.get("changes") or []:
                if not isinstance(wanted, Mapping):
                    continue
                checks.append(
                    (_row_matches(dict(wanted), observed_changes),
                     f"no recorded change matches {dict(wanted)!r}")
                )
        if "values" in expected and isinstance(expected.get("values"), Mapping):
            wanted_values = _mapping(expected.get("values"))
            if not observed_rows:
                checks.append((False, "no rows were observed to read values from"))
            else:
                for name, value in wanted_values.items():
                    checks.append(
                        (observed_rows[0].get(name) == value,
                         f"value {name!r} is {observed_rows[0].get(name)!r}, expected {value!r}")
                    )
        return self._verdict(
            request,
            checks,
            evidence=evidence,
            observed=facts,
            error_category=CritiqueCategory.EXECUTION_ERROR.value,
        )


def _row_matches(wanted: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> bool:
    """Whether some observed row carries every field/value the expectation names."""
    return any(all(row.get(name) == value for name, value in wanted.items()) for row in rows)


def _is_type(value: Any, kind: str) -> bool:
    """A small, explicit type vocabulary for schema checks."""
    name = kind.strip().lower()
    if name in {"any", ""}:
        return True
    if name in {"str", "string", "text"}:
        return isinstance(value, str)
    if name in {"int", "integer"}:
        return isinstance(value, int) and not isinstance(value, bool)
    if name in {"float", "number"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name in {"bool", "boolean"}:
        return isinstance(value, bool)
    if name in {"list", "array"}:
        return isinstance(value, (list, tuple))
    if name in {"dict", "object", "mapping"}:
        return isinstance(value, Mapping)
    if name in {"null", "none"}:
        return value is None
    return True


class GitVerifier(Verifier):
    """Checks a recorded repository state: branch, head, cleanliness, changes.

    The repository facts are observed (the run recorded what its git operations
    produced) or read from a JSON snapshot the run wrote beside the workspace.
    A verifier does not run ``git`` itself by default: the point is to check
    the state that was actually produced, not to generate a second one.
    """

    verifier_id = "git"
    name = "Git verifier"
    description = "Checks branch, head commit, cleanliness, tracked files and diffs."
    category = VerifierCategory.GIT.value
    task_types = ("git", "repository", "commit")
    input_schema = {
        "expected": {
            "branch": "str",
            "head_commit": "str (sha or prefix)",
            "clean": "bool",
            "tracked": "[str]",
            "modified": "[str]",
            "untracked": "[str]",
            "diff_contains": "str | [str]",
            "commit_message_contains": "str",
            "snapshot": "path to a JSON snapshot under the workspace",
        },
        "observation": {
            "git": {
                "branch": "str",
                "head_commit": "str",
                "clean": "bool",
                "tracked": "[str]",
                "modified": "[str]",
                "untracked": "[str]",
                "diff": "str",
                "last_commit": {"sha": "str", "message": "str"},
            }
        },
    }
    output_schema = {"status": "pass | fail | partial | inconclusive | error"}

    def validate(self, request: VerificationRequest) -> tuple[str, ...]:
        problems = list(super().validate(request))
        expected = request.expected
        if not any(
            key in expected
            for key in (
                "branch",
                "head_commit",
                "clean",
                "tracked",
                "modified",
                "untracked",
                "diff_contains",
                "commit_message_contains",
            )
        ):
            problems.append(
                "the expectation names no repository property (branch/head_commit/"
                "clean/tracked/modified/untracked/diff_contains/commit_message_contains)"
            )
        return tuple(problems)

    def verify(self, request: VerificationRequest) -> VerificationResult:
        problems = self.validate(request)
        if problems:
            return self._problem(request, problems)
        expected = request.expected
        facts = self._state(request)
        if not facts:
            return self._result(
                request,
                VerificationStatus.INCONCLUSIVE,
                detail="no repository state was observed and no snapshot could be read",
            )
        checks: list[tuple[bool, str]] = []
        evidence: list[str] = [EVIDENCE_GIT_STATE]
        if "branch" in expected:
            checks.append(
                (_text(facts.get("branch")) == _text(expected.get("branch")),
                 f"branch is {facts.get('branch')!r}, expected {expected.get('branch')!r}")
            )
        if "head_commit" in expected:
            head = _text(facts.get("head_commit")) or _text(
                _mapping(facts.get("last_commit")).get("sha")
            )
            wanted = _text(expected.get("head_commit"))
            checks.append((head.startswith(wanted) if head else False,
                           f"head commit is {head!r}, expected prefix {wanted!r}"))
        if "clean" in expected:
            checks.append(
                (bool(facts.get("clean")) == bool(expected.get("clean")),
                 f"clean={facts.get('clean')!r}, expected {expected.get('clean')!r}")
            )
        checks.extend(_set_checks("tracked", expected.get("tracked"), _texts(facts.get("tracked"))))
        checks.extend(
            _set_checks("modified", expected.get("modified"), _texts(facts.get("modified")))
        )
        checks.extend(
            _set_checks("untracked", expected.get("untracked"), _texts(facts.get("untracked")))
        )
        if "diff_contains" in expected:
            diff = str(facts.get("diff") or "")
            wanted_diff = expected.get("diff_contains")
            parts = (
                [str(item) for item in wanted_diff]
                if isinstance(wanted_diff, (list, tuple))
                else [str(wanted_diff)]
            )
            for part in parts:
                checks.append((part in diff, f"diff does not contain {_quote(part)}"))
        if "commit_message_contains" in expected:
            message = _text(_mapping(facts.get("last_commit")).get("message"))
            checks.append(
                (str(expected.get("commit_message_contains")) in message,
                 "the last commit message does not contain "
                 f"{_quote(expected.get('commit_message_contains'))}")
            )
        return self._verdict(
            request,
            checks,
            evidence=evidence,
            observed=facts,
            error_category=CritiqueCategory.EXECUTION_ERROR.value,
        )

    def _state(self, request: VerificationRequest) -> dict[str, Any]:
        facts = _mapping(request.observation.get("git"))
        if facts:
            return facts
        snapshot = _text(request.expected.get("snapshot"))
        if snapshot and request.workspace:
            target = _safe_join(request.workspace, snapshot)
            if target is None or not target.is_file():
                return {}
            try:
                loaded = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return {}
            if isinstance(loaded, Mapping):
                return dict(loaded)
        return {}


class OutputVerifier(Verifier):
    """Checks raw or structured output: exact, normalised or field-by-field."""

    verifier_id = "output"
    name = "Output verifier"
    description = "Checks an observed output against an exact, normalised or structured form."
    category = VerifierCategory.OUTPUT.value
    task_types = ("output", "answer", "response")
    input_schema = {
        "expected": {
            "exact": "str",
            "normalized": "str (whitespace-collapsed)",
            "contains": "str | [str]",
            "structured": "mapping (field -> value)",
            "ignore_case": "bool",
        },
        "observation": {"output": "str | mapping"},
    }
    output_schema = {"status": "pass | fail | partial | inconclusive | error"}

    def validate(self, request: VerificationRequest) -> tuple[str, ...]:
        problems = list(super().validate(request))
        expected = request.expected
        if not any(key in expected for key in ("exact", "normalized", "contains", "structured")):
            problems.append(
                "the expectation names no output property (exact/normalized/contains/structured)"
            )
        return tuple(problems)

    def verify(self, request: VerificationRequest) -> VerificationResult:
        problems = self.validate(request)
        if problems:
            return self._problem(request, problems)
        expected = request.expected
        raw = request.observation.get("output")
        if raw is None:
            return self._result(
                request,
                VerificationStatus.INCONCLUSIVE,
                detail="the observation carries no output",
            )
        ignore_case = bool(expected.get("ignore_case"))
        text: str = (
            raw
            if isinstance(raw, str)
            else json.dumps(_jsonable(raw), sort_keys=True, default=str)
        )
        checks: list[tuple[bool, str]] = []
        evidence: list[str] = [EVIDENCE_OUTPUT_MATCH]
        if "exact" in expected:
            wanted = str(expected.get("exact"))
            checks.append((_compare(text, wanted, ignore_case=ignore_case),
                           f"output differs from the exact expectation {_quote(wanted)}"))
        if "normalized" in expected:
            wanted = str(expected.get("normalized"))
            checks.append(
                (
                    _compare(_normalized(text), _normalized(wanted), ignore_case=ignore_case),
                    "output differs after normalisation from "
                    f"{_quote(_normalized(wanted))}",
                )
            )
        if "contains" in expected:
            wanted_contains = expected.get("contains")
            parts = (
                [str(item) for item in wanted_contains]
                if isinstance(wanted_contains, (list, tuple))
                else [str(wanted_contains)]
            )
            haystack = text.lower() if ignore_case else text
            for part in parts:
                needle = part.lower() if ignore_case else part
                checks.append((needle in haystack, f"output does not contain {_quote(part)}"))
        if "structured" in expected and isinstance(expected.get("structured"), Mapping):
            parsed = _mapping(raw) if isinstance(raw, Mapping) else None
            if parsed is None:
                checks.append((False, "the observation is not structured output"))
            else:
                for name, value in _mapping(expected.get("structured")).items():
                    checks.append((parsed.get(name) == value,
                                   f"field {name!r} is {parsed.get(name)!r}, expected {value!r}"))
        return self._verdict(
            request,
            checks,
            evidence=evidence,
            observed={"output": raw} if not isinstance(raw, Mapping) else dict(raw),
            error_category=CritiqueCategory.OUTPUT_FORMAT_ERROR.value,
        )


class SchemaVerifier(Verifier):
    """Checks a value against a declarative schema: fields, types, constraints.

    The schema vocabulary is small and explicit (type, required, enum, min, max,
    min_length, max_length, pattern) so a stored expectation explains itself
    without a schema library, and a plugin can extend it through a custom
    verifier when its domain needs more.
    """

    verifier_id = "schema"
    name = "Schema verifier"
    description = "Validates required fields, types and constraints of structured output."
    category = VerifierCategory.SCHEMA.value
    task_types = ("schema", "json", "validation", "structured_output")
    input_schema = {
        "expected": {
            "schema": "mapping (field -> type name | spec mapping)",
            "required": "[str] (extra required fields)",
            "value": "mapping (defaults to the observation's value/output)",
        },
    }
    output_schema = {"status": "pass | fail | partial | inconclusive | error"}

    def validate(self, request: VerificationRequest) -> tuple[str, ...]:
        problems = list(super().validate(request))
        schema = request.expected.get("schema")
        if not isinstance(schema, Mapping) or not schema:
            problems.append("expected.schema must be a non-empty mapping")
        return tuple(problems)

    def verify(self, request: VerificationRequest) -> VerificationResult:
        problems = self.validate(request)
        if problems:
            return self._problem(request, problems)
        value = request.expected.get("value")
        if not isinstance(value, Mapping):
            value = request.observation.get("value")
        if not isinstance(value, Mapping):
            value = request.observation.get("output")
        if not isinstance(value, Mapping):
            return self._result(
                request,
                VerificationStatus.INCONCLUSIVE,
                detail="no structured value was supplied to validate",
            )
        schema = _mapping(request.expected.get("schema"))
        checks: list[tuple[bool, str]] = []
        evidence: list[str] = [EVIDENCE_SCHEMA_VALID]
        required = set(_texts(request.expected.get("required")))
        for name, spec in schema.items():
            rules = _mapping(spec) if isinstance(spec, Mapping) else {"type": str(spec)}
            if bool(rules.get("required", True)):
                required.add(name)
            if name not in value:
                continue
            checks.extend(_constraint_checks(name, value.get(name), rules))
        for name in sorted(required):
            checks.append((name in value, f"required field {name!r} is missing"))
        return self._verdict(
            request,
            checks,
            evidence=evidence,
            observed=dict(value),
            error_category=CritiqueCategory.OUTPUT_FORMAT_ERROR.value,
        )


class CustomVerifier(Verifier):
    """A domain check a plugin registers, through the existing plugin SDK.

    The callable receives the :class:`VerificationRequest` and returns either a
    :class:`VerificationResult`, a boolean, or a mapping with the standard keys
    (``status``, ``score``, ``passed``, ``evidence``, ``detail``, ``expected``,
    ``observed``, ``error_category``, ``confidence``). A mapping is normalised
    into a result, so a plugin cannot smuggle a free-form object into a reward.
    """

    category = VerifierCategory.CUSTOM.value

    def __init__(
        self,
        verifier_id: str,
        check: Callable[[VerificationRequest], Any],
        *,
        name: str = "",
        description: str = "",
        version: str = RLVR_VERSION,
        risk_level: str = RiskLevel.LOW.value,
        deterministic: bool = True,
        plugin_name: str = "",
        task_types: Sequence[str] = (),
    ) -> None:
        self.verifier_id = _text(verifier_id) or "custom"
        self._check = check
        self.name = _text(name) or self.verifier_id
        self.description = _text(description) or "A custom domain verifier."
        self.version = _text(version, RLVR_VERSION)
        self.risk_level = _text(risk_level, RiskLevel.LOW.value)
        self.deterministic = bool(deterministic)
        self.plugin_name = _text(plugin_name)
        self.task_types = tuple(str(item).strip() for item in task_types if str(item).strip())

    @classmethod
    def from_callable(
        cls,
        verifier_id: str,
        check: Callable[[VerificationRequest], Any],
        **kwargs: Any,
    ) -> CustomVerifier:
        return cls(verifier_id, check, **kwargs)

    def validate(self, request: VerificationRequest) -> tuple[str, ...]:
        problems = list(super().validate(request))
        if not callable(self._check):
            problems.append("the custom check is not callable")
        return tuple(problems)

    def verify(self, request: VerificationRequest) -> VerificationResult:
        problems = self.validate(request)
        if problems:
            return self._problem(request, problems)
        try:
            answer = self._check(replace(request))
        except Exception as exc:  # noqa: BLE001 - a broken plugin check is an ERROR verdict
            return self._result(
                request,
                VerificationStatus.ERROR,
                detail=f"the custom check raised {type(exc).__name__}: {exc}",
                error_category=CritiqueCategory.OTHER.value,
            )
        if isinstance(answer, VerificationResult):
            if answer.verifier_id and answer.verifier_id != self.verifier_id:
                return self._result(
                    request,
                    VerificationStatus.ERROR,
                    detail=(
                        "the custom check returned a result attributed to "
                        f"{answer.verifier_id!r}, not {self.verifier_id!r}"
                    ),
                    error_category=CritiqueCategory.OTHER.value,
                )
            return answer
        if isinstance(answer, bool):
            return self._result(
                request,
                VerificationStatus.PASS if answer else VerificationStatus.FAIL,
                detail="the custom check " + ("passed" if answer else "failed"),
                evidence=(EVIDENCE_CUSTOM_CHECK,),
                score=1.0 if answer else 0.0,
                error_category="" if answer else CritiqueCategory.OTHER.value,
            )
        if isinstance(answer, Mapping):
            status = _text(answer.get("status"))
            if status not in {member.value for member in VerificationStatus}:
                passed = answer.get("passed")
                status = (
                    VerificationStatus.PASS.value
                    if passed is True
                    else VerificationStatus.FAIL.value
                    if passed is False
                    else VerificationStatus.INCONCLUSIVE.value
                )
            return VerificationResult(
                task_id=request.task_id,
                trajectory_id=request.trajectory_id,
                step_id=request.step_id,
                verifier_id=self.verifier_id,
                verifier_version=self.version,
                status=status,
                passed=status == VerificationStatus.PASS.value,
                score=_real_or_none(answer.get("score")),
                scope=request.scope,
                evidence=_texts(answer.get("evidence")) or (EVIDENCE_CUSTOM_CHECK,),
                expected=_mapping(answer.get("expected")) or dict(request.expected),
                observed=_mapping(answer.get("observed")) or dict(request.observation),
                expected_digest=request.expected_digest,
                error_category=_text(answer.get("error_category")),
                confidence=_real_or_none(answer.get("confidence")),
                detail=_text(answer.get("detail")),
            )
        return self._result(
            request,
            VerificationStatus.INCONCLUSIVE,
            detail=f"the custom check returned {type(answer).__name__}, which is not a verdict",
        )


def default_verifiers(
    *, allow_execution: bool = False, extra: Sequence[Verifier] = ()
) -> tuple[Verifier, ...]:
    """The verifier set a fresh registry starts with, plus any extras."""
    built: list[Verifier] = [
        FileVerifier(),
        ProcessVerifier(),
        TestVerifier(allow_execution=allow_execution),
        HTTPVerifier(),
        DatabaseVerifier(),
        GitVerifier(),
        OutputVerifier(),
        SchemaVerifier(),
    ]
    built.extend(extra)
    return tuple(built)


def _set_checks(name: str, wanted: Any, observed: Sequence[str]) -> list[tuple[bool, str]]:
    if not isinstance(wanted, (list, tuple)):
        return []
    expected = {str(item) for item in wanted}
    present = set(observed)
    missing = sorted(expected - present)
    unexpected = sorted(present - expected)
    if not expected and not unexpected:
        return [(True, "")]
    checks: list[tuple[bool, str]] = []
    checks.append((not missing, f"{name} is missing {missing}"))
    if expected:
        checks.append((not unexpected, f"{name} has unexpected entries {unexpected}"))
    return checks


def _compare(text: str, wanted: str, *, ignore_case: bool) -> bool:
    if ignore_case:
        return text.lower() == wanted.lower()
    return text == wanted


def _normalized(text: str) -> str:
    lines = (" ".join(line.split()) for line in text.strip().splitlines())
    return " ".join(line for line in lines if line)


def _constraint_checks(name: str, value: Any, rules: Mapping[str, Any]) -> list[tuple[bool, str]]:
    checks: list[tuple[bool, str]] = []
    kind = rules.get("type")
    if kind is not None:
        checks.append((_is_type(value, str(kind)), f"field {name!r} is not {kind!r}"))
    if "enum" in rules and isinstance(rules.get("enum"), (list, tuple)):
        allowed = list(rules.get("enum") or [])
        checks.append((value in allowed, f"field {name!r} is not one of {allowed!r}"))
    if "min" in rules and isinstance(value, (int, float)) and not isinstance(value, bool):
        checks.append((float(value) >= float(rules.get("min", 0.0)),
                       f"field {name!r} is below {rules.get('min')!r}"))
    if "max" in rules and isinstance(value, (int, float)) and not isinstance(value, bool):
        checks.append((float(value) <= float(rules.get("max", 0.0)),
                       f"field {name!r} is above {rules.get('max')!r}"))
    if "min_length" in rules and isinstance(value, (str, list, tuple, Mapping)):
        checks.append((len(value) >= int(rules.get("min_length", 0)),
                       f"field {name!r} is shorter than {rules.get('min_length')!r}"))
    if "max_length" in rules and isinstance(value, (str, list, tuple, Mapping)):
        checks.append((len(value) <= int(rules.get("max_length", 0)),
                       f"field {name!r} is longer than {rules.get('max_length')!r}"))
    if "pattern" in rules and isinstance(value, str):
        try:
            matched = re.search(str(rules.get("pattern")), value) is not None
        except re.error:
            matched = False
        checks.append((matched, f"field {name!r} does not match {rules.get('pattern')!r}"))
    return checks


def _real_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
