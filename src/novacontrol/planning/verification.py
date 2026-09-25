"""Verification: checking that a step did what it claimed.

The distinction this module exists to keep is the one every automation system
gets wrong eventually:

    the action ran          (something returned)
    the action reported ok  (nothing raised)
    the result is real      (the state we expected is observable)

Only the third is success. A verifier therefore never *believes* an executor: it
looks at evidence — a file, an exit code, a captured output, a probe — and
returns PASS, FAIL or, when it genuinely cannot tell, INCONCLUSIVE.

INCONCLUSIVE is not a soft PASS. It says "the check could not be made", which is
different information and is reported as such, because an unverifiable step
counted as a verified one is exactly how a pipeline starts lying to its user.

Everything here is side-effect free and injectable: the process probe and the
callable registry are supplied by the caller, so tests (and machines without
psutil) get honest answers without installing anything new.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from novacontrol.planning.models import (
    PlanStep,
    VerificationMethod,
    VerificationResult,
    VerificationSpec,
    VerificationStatus,
)

#: A verification callable: gets the step's verification spec and its output,
#: and returns True/False, a reason string, or (passed, reason).
VerifyCallable = Callable[
    [VerificationSpec, Mapping[str, Any]], "bool | str | tuple[bool, str]"
]

#: A process probe: True running, False not running, None "cannot tell".
ProcessProbe = Callable[[str], "bool | None"]

_HONEST_UNKNOWN = (
    "A step that changed something was left unverified: nothing was available to "
    "check it against, so the result is unknown rather than assumed."
)


class DeterministicVerifier:
    """Checks the evidence a step's outcome carries, and nothing else.

    No method here reaches for a language model. Verification that can be
    argued with is not verification, and a model asked "did that work?" will
    answer from the same optimism the executor just expressed.
    """

    def __init__(
        self,
        *,
        callables: Mapping[str, VerifyCallable] | None = None,
        process_probe: ProcessProbe | None = None,
    ) -> None:
        self._callables: dict[str, VerifyCallable] = dict(callables or {})
        self._process_probe = process_probe or _default_process_probe

    def register(self, name: str, check: VerifyCallable) -> None:
        """Register a named check a step may ask for via ``CALLABLE``."""
        self._callables[name] = check

    def check_for(self, name: str) -> VerifyCallable | None:
        """The registered check named ``name``, if this machine has one."""
        return self._callables.get(name)

    def probe_process(self, name: str) -> bool | None:
        """True/False whether a process is running, or None when unknowable."""
        return self._process_probe(name)

    def verify(self, step: PlanStep, output: Mapping[str, Any]) -> VerificationResult:
        spec = step.verification
        method = spec.method
        if method is VerificationMethod.NONE:
            return VerificationResult(
                status=VerificationStatus.SKIPPED,
                method=method,
                expectation=spec.description or f"No check attached to {step.title!r}.",
                reason="No verification was requested for this step.",
            )
        handler = _METHODS.get(method)
        if handler is None:  # pragma: no cover - exhaustive over the enum
            return VerificationResult(
                status=VerificationStatus.INCONCLUSIVE,
                method=method,
                expectation=spec.description,
                reason=f"Verification method {method.value!r} has no implementation.",
            )
        return handler(self, spec, output)


# --------------------------------------------------------------------------- #
# Methods
# --------------------------------------------------------------------------- #


def _file_exists(
    verifier: DeterministicVerifier, spec: VerificationSpec, output: Mapping[str, Any]
) -> VerificationResult:
    target = _target(spec, output, keys=("path", "file", "target"))
    if not target:
        return _inconclusive(spec, "The step names no path, so no file could be checked.")
    exists = Path(target).exists()
    expect = True if spec.expect is None else bool(spec.expect)
    passed = exists == expect
    return VerificationResult(
        status=VerificationStatus.PASS if passed else VerificationStatus.FAIL,
        method=spec.method,
        expectation=spec.description or f"path {target!r} exists == {expect}",
        observed=f"exists={exists}",
        reason=(
            f"{target!r} {'exists' if exists else 'does not exist'}."
            if passed
            else f"Expected exists={expect} for {target!r}, observed exists={exists}."
        ),
        evidence={"path": str(Path(target))},
    )


def _artifact_exists(
    verifier: DeterministicVerifier, spec: VerificationSpec, output: Mapping[str, Any]
) -> VerificationResult:
    artifacts = _artifacts(spec, output)
    if not artifacts:
        return _inconclusive(spec, "The step reported no artifacts to check.")
    missing = [item for item in artifacts if not Path(item).exists()]
    return VerificationResult(
        status=VerificationStatus.PASS if not missing else VerificationStatus.FAIL,
        method=spec.method,
        expectation=spec.description or "every reported artifact exists",
        observed=f"{len(artifacts) - len(missing)}/{len(artifacts)} present",
        reason=(
            f"All {len(artifacts)} artifact(s) exist."
            if not missing
            else f"Missing artifact(s): {missing!r}"
        ),
        evidence={"artifacts": list(artifacts), "missing": missing},
    )


def _exit_code(
    verifier: DeterministicVerifier, spec: VerificationSpec, output: Mapping[str, Any]
) -> VerificationResult:
    """Did the command finish, and did it finish with what this step expects?

    Three readings of ``expect``, all of them needed in practice:

      * an int, an expected code — a build must exit 0;
      * a collection, an acceptable set — a suite may fail 0 or 1;
      * ``None``, any code — "run the tests and tell me what failed" expects a
        FAILURE. A non-zero exit there is the result, not a broken step, so the
        only thing worth checking is that a code was actually reported.
    """
    code = _first(output, ("exit_code", "returncode", "status_code"))
    if code is None:
        return _inconclusive(
            spec, "The step reported no exit code, so the command's outcome is unknown."
        )
    try:
        numeric = int(code)
    except (TypeError, ValueError):
        return _inconclusive(spec, f"Exit code {code!r} is not a number.")

    expected = spec.expect
    if expected is None:
        return VerificationResult(
            status=VerificationStatus.PASS,
            method=spec.method,
            expectation=spec.description or "the command reported an exit code",
            observed=f"exit_code={numeric}",
            reason=f"The command ran and reported exit code {numeric}.",
            evidence={"exit_code": numeric, "expected": "any"},
        )
    if isinstance(expected, tuple | list | set | frozenset):
        allowed = [int(item) for item in expected]
        passed = numeric in allowed
        reason = (
            f"Command exited {numeric}, which is one of {allowed}."
            if passed
            else f"Command exited {numeric}, which is not one of {allowed}."
        )
    else:
        try:
            allowed_code = int(expected)
        except (TypeError, ValueError):
            return _inconclusive(spec, f"Expected exit code {expected!r} is not a number.")
        passed = numeric == allowed_code
        reason = (
            f"Command exited {numeric} as expected."
            if passed
            else f"Command exited {numeric}, expected {allowed_code}."
        )
    return VerificationResult(
        status=VerificationStatus.PASS if passed else VerificationStatus.FAIL,
        method=spec.method,
        expectation=spec.description or f"exit code in {expected!r}",
        observed=f"exit_code={numeric}",
        reason=reason,
        evidence={"exit_code": numeric, "expected": expected},
    )


def _output_contains(
    verifier: DeterministicVerifier, spec: VerificationSpec, output: Mapping[str, Any]
) -> VerificationResult:
    needle = str(spec.expect if spec.expect is not None else spec.target)
    if not needle:
        return _inconclusive(spec, "No expected text was provided to look for.")
    haystack = _flatten(output).lower()
    if not haystack:
        return _inconclusive(
            spec, "The step produced no output to search, so the expectation is unverified."
        )
    found = needle.lower() in haystack
    return VerificationResult(
        status=VerificationStatus.PASS if found else VerificationStatus.FAIL,
        method=spec.method,
        expectation=spec.description or f"output contains {needle!r}",
        observed=f"output_length={len(haystack)}",
        reason=(
            f"Output contains {needle!r}."
            if found
            else f"Output does not contain {needle!r}."
        ),
        evidence={"needle": needle},
    )


def _process_running(
    verifier: DeterministicVerifier, spec: VerificationSpec, output: Mapping[str, Any]
) -> VerificationResult:
    """Is the application actually up?

    Evidence first: a step that reported a pid it just started is checked
    against the probe, and the reported flag is never trusted on its own —
    otherwise the check would just echo the claim it is supposed to test.
    """
    target = spec.target or str(_first(output, ("process", "application", "name")) or "")
    if not target:
        return _inconclusive(spec, "The step names no process to look for.")
    running = verifier.probe_process(target)
    if running is None:
        return _inconclusive(
            spec,
            f"Cannot tell whether {target!r} is running on this machine, "
            "so the application state is unverified.",
        )
    expect = True if spec.expect is None else bool(spec.expect)
    passed = running == expect
    return VerificationResult(
        status=VerificationStatus.PASS if passed else VerificationStatus.FAIL,
        method=spec.method,
        expectation=spec.description or f"{target!r} running == {expect}",
        observed=f"running={running}",
        reason=(
            f"{target!r} running state is {running} as expected."
            if passed
            else f"Expected {target!r} running={expect}, observed running={running}."
        ),
        evidence={"process": target},
    )


def _state_observed(
    verifier: DeterministicVerifier, spec: VerificationSpec, output: Mapping[str, Any]
) -> VerificationResult:
    changed = _first(output, ("changed", "observed_change"))
    if changed is None:
        return _inconclusive(
            spec,
            "No observed state change was reported, so nothing was verified "
            "(an unobserved action is not a successful one).",
        )
    expect = True if spec.expect is None else bool(spec.expect)
    passed = bool(changed) == expect
    return VerificationResult(
        status=VerificationStatus.PASS if passed else VerificationStatus.FAIL,
        method=spec.method,
        expectation=spec.description or f"observed change == {expect}",
        observed=f"changed={bool(changed)}",
        reason=(
            "The expected state change was observed."
            if passed
            else "No expected state change was observed after the action."
        ),
        evidence={"changed": bool(changed)},
    )


def _callable_method(
    verifier: DeterministicVerifier, spec: VerificationSpec, output: Mapping[str, Any]
) -> VerificationResult:
    check = verifier.check_for(spec.target)
    if check is None:
        return _inconclusive(
            spec, f"No verification named {spec.target!r} is registered on this machine."
        )
    try:
        result = check(spec, output)
    except Exception as exc:  # a broken check is an unknown result, not a pass
        return _inconclusive(
            spec, f"Verification {spec.target!r} raised {type(exc).__name__}: {exc}"
        )
    passed, reason = _interpret(result)
    if passed is None:
        return _inconclusive(spec, reason or "The check returned no verdict.")
    return VerificationResult(
        status=VerificationStatus.PASS if passed else VerificationStatus.FAIL,
        method=spec.method,
        expectation=spec.description or spec.target,
        observed=f"{spec.target}={passed}",
        reason=reason or ("Check passed." if passed else "Check failed."),
        evidence={"check": spec.target},
    )


MethodHandler = Callable[
    [DeterministicVerifier, VerificationSpec, Mapping[str, Any]], VerificationResult
]

_METHODS: dict[VerificationMethod, MethodHandler] = {
    VerificationMethod.FILE_EXISTS: _file_exists,
    VerificationMethod.ARTIFACT_EXISTS: _artifact_exists,
    VerificationMethod.EXIT_CODE: _exit_code,
    VerificationMethod.OUTPUT_CONTAINS: _output_contains,
    VerificationMethod.PROCESS_RUNNING: _process_running,
    VerificationMethod.STATE_OBSERVED: _state_observed,
    VerificationMethod.CALLABLE: _callable_method,
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _inconclusive(spec: VerificationSpec, reason: str) -> VerificationResult:
    return VerificationResult(
        status=VerificationStatus.INCONCLUSIVE,
        method=spec.method,
        expectation=spec.description,
        reason=reason,
    )


def unverified_step_reason() -> str:
    """The sentence an unverified side-effecting step is reported with."""
    return _HONEST_UNKNOWN


def _interpret(result: Any) -> tuple[bool | None, str]:
    if isinstance(result, tuple) and len(result) == 2:
        return bool(result[0]), str(result[1])
    if isinstance(result, bool):
        return result, ""
    if isinstance(result, str):
        return None, result
    return None, ""


def _target(spec: VerificationSpec, output: Mapping[str, Any], *, keys: tuple[str, ...]) -> str:
    if spec.target:
        return spec.target
    for key in keys:
        value = output.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _artifacts(spec: VerificationSpec, output: Mapping[str, Any]) -> list[str]:
    if spec.target:
        return [spec.target]
    raw = output.get("artifacts") or output.get("files") or ()
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, tuple | list):
        return [str(item) for item in raw]
    return []


def _first(output: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in output:
            return output[key]
    return None


def _flatten(value: Any) -> str:
    """All text in an outcome, for substring checks."""
    if isinstance(value, Mapping):
        return " ".join(_flatten(item) for item in value.values())
    if isinstance(value, tuple | list):
        return " ".join(_flatten(item) for item in value)
    if value is None:
        return ""
    return str(value)


def _default_process_probe(name: str) -> bool | None:
    """Is a process matching ``name`` running?

    psutil is not a declared dependency, so it is imported lazily and its
    absence is reported as "cannot tell" rather than as "not running" — the
    honest answer, and one that keeps this verifier installable everywhere.
    """
    psutil = _import_psutil()
    if psutil is None:
        return None
    needle = name.strip().lower()
    if not needle:
        return None
    try:
        for process in psutil.process_iter(["name"]):
            process_name = str(process.info.get("name") or "").lower()
            if needle in process_name:
                return True
    except Exception:
        return None
    return False


def _import_psutil() -> Any:
    """psutil when it happens to be installed, else None (never a requirement)."""
    try:
        import psutil  # noqa: PLC0415 — optional dependency, probed at runtime

        return psutil
    except ImportError:
        return None
