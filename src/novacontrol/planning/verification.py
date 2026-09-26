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

import socket
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

#: A window probe: True open, False not open, None "cannot tell".
WindowProbe = Callable[[str], "bool | None"]

#: A network probe: True reachable, False not, None "cannot tell".
NetworkProbe = Callable[[str], "bool | None"]

_HONEST_UNKNOWN = (
    "A step that changed something was left unverified: nothing was available to "
    "check it against, so the result is unknown rather than assumed."
)

#: How far a PASS may be relied on, by the strength of the evidence behind the
#: method. A file that exists either exists or does not (0.95); text found in
#: output proves the text was there (0.85); a tool's own reported success is the
#: claim under test, not evidence of it (0.6). Stamped only when the check did
#: not state a confidence of its own, and never for an unknown result — a check
#: that could not be made has no confidence to report.
_CONFIDENCE_BY_METHOD: dict[VerificationMethod, float] = {
    VerificationMethod.FILE_EXISTS: 0.95,
    VerificationMethod.ARTIFACT_EXISTS: 0.95,
    VerificationMethod.EXIT_CODE: 0.95,
    VerificationMethod.PROCESS_RUNNING: 0.9,
    VerificationMethod.WINDOW_EXISTS: 0.85,
    VerificationMethod.NETWORK_REACHABLE: 0.9,
    VerificationMethod.STATE_OBSERVED: 0.7,
    VerificationMethod.OUTPUT_CONTAINS: 0.85,
    VerificationMethod.CALLABLE: 0.9,
    VerificationMethod.RESULT_REPORTED: 0.6,
    VerificationMethod.NONE: 0.0,
}


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
        window_probe: WindowProbe | None = None,
        network_probe: NetworkProbe | None = None,
    ) -> None:
        self._callables: dict[str, VerifyCallable] = dict(callables or {})
        self._process_probe = process_probe or _default_process_probe
        self._window_probe = window_probe or _default_window_probe
        self._network_probe = network_probe or _default_network_probe

    #: The name this verifier stamps on the results it produces. A subclass that
    #: is a different engine says so, so a report can tell which one ran.
    name = "deterministic"

    def register(self, name: str, check: VerifyCallable) -> None:
        """Register a named check a step may ask for via ``CALLABLE``."""
        self._callables[name] = check

    def check_for(self, name: str) -> VerifyCallable | None:
        """The registered check named ``name``, if this machine has one."""
        return self._callables.get(name)

    def probe_process(self, name: str) -> bool | None:
        """True/False whether a process is running, or None when unknowable."""
        return self._process_probe(name)

    def probe_window(self, name: str) -> bool | None:
        """True/False whether a window is open, or None when unknowable."""
        return self._window_probe(name)

    def probe_network(self, target: str) -> bool | None:
        """True/False whether an endpoint is reachable, or None when unknowable."""
        return self._network_probe(target)

    def verify(self, step: PlanStep, output: Mapping[str, Any]) -> VerificationResult:
        """Check the verification the STEP attached."""
        return self.verify_spec(step.verification, output)

    def verify_spec(
        self,
        spec: VerificationSpec,
        output: Mapping[str, Any],
        *,
        verifier: str | None = None,
    ) -> VerificationResult:
        """Check ONE spec against an outcome — the method dispatch on its own.

        Split out from :meth:`verify` so a caller that has a check but no step
        (a tool with a registered strategy, a check derived from an action) can
        run it through the SAME methods and the same honesty rules, rather than
        growing a second, thinner way to verify things.

        ``verifier`` names who is running the check, for the report. It is a
        parameter rather than a field a subclass writes afterwards because a
        result is stamped ONCE: a second stamp cannot correct the first, so the
        name has to be known at the point the result is built.
        """
        method = spec.method
        if method is VerificationMethod.NONE:
            result = VerificationResult(
                status=VerificationStatus.SKIPPED,
                method=method,
                expectation=spec.description or "No check was attached.",
                reason="No verification was requested for this step.",
            )
            return stamp(result, verifier=verifier or self.name)
        handler = _METHODS.get(method)
        if handler is None:  # pragma: no cover - exhaustive over the enum
            result = VerificationResult(
                status=VerificationStatus.INCONCLUSIVE,
                method=method,
                expectation=spec.description,
                reason=f"Verification method {method.value!r} has no implementation.",
            )
            return stamp(result, verifier=verifier or self.name)
        return stamp(handler(self, spec, output), verifier=verifier or self.name)


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
    passed, reason = interpret_result(result)
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


def _window_exists(
    verifier: DeterministicVerifier, spec: VerificationSpec, output: Mapping[str, Any]
) -> VerificationResult:
    """Is the window actually open?

    The check the specification's own example asks for: a launch call returning
    is not a window on screen, and "Open VS Code" is only done when the window
    can be OBSERVED. Where no window probe is wired the answer is "cannot
    tell" — never "not open", which would report a failure nobody measured.
    """
    target = spec.target or str(
        _first(output, ("window", "title", "application", "app", "name")) or ""
    )
    if not target:
        return _inconclusive(spec, "The step names no window to look for.")
    open_now = verifier.probe_window(target)
    if open_now is None:
        return _inconclusive(
            spec,
            f"Cannot tell whether a window for {target!r} is open on this machine, "
            "so the application state is unverified.",
        )
    expect = True if spec.expect is None else bool(spec.expect)
    passed = open_now == expect
    return VerificationResult(
        status=VerificationStatus.PASS if passed else VerificationStatus.FAIL,
        method=spec.method,
        expectation=spec.description or f"window for {target!r} open == {expect}",
        observed=f"open={open_now}",
        reason=(
            f"A window for {target!r} is open as expected."
            if passed
            else f"Expected a window for {target!r} (open={expect}), observed open={open_now}."
        ),
        evidence={"window": target},
    )


def _network_reachable(
    verifier: DeterministicVerifier, spec: VerificationSpec, output: Mapping[str, Any]
) -> VerificationResult:
    """Can the endpoint actually be reached?

    For the actions whose effect leaves the machine, reachability is the one
    honest observation available from here: a request that returned is not the
    same claim as a host that answers.
    """
    target = spec.target or str(
        _first(output, ("url", "host", "endpoint", "address", "target")) or ""
    )
    if not target:
        return _inconclusive(spec, "The step names no endpoint to reach.")
    reachable = verifier.probe_network(target)
    if reachable is None:
        return _inconclusive(
            spec,
            f"{target!r} is not a target this machine can probe, so its reachability "
            "is unverified rather than assumed.",
        )
    expect = True if spec.expect is None else bool(spec.expect)
    passed = reachable == expect
    return VerificationResult(
        status=VerificationStatus.PASS if passed else VerificationStatus.FAIL,
        method=spec.method,
        expectation=spec.description or f"{target!r} reachable == {expect}",
        observed=f"reachable={reachable}",
        reason=(
            f"{target!r} is reachable as expected."
            if passed
            else f"Expected {target!r} reachable={expect}, observed reachable={reachable}."
        ),
        evidence={"endpoint": target},
    )


#: What a tool's own report says when it succeeded. Anything else — including a
#: status this list has never seen — is read as "not a success", because a
#: result-based check that accepts unknown words is a check that always passes.
_SUCCESS_WORDS: frozenset[str] = frozenset(
    {"true", "1", "ok", "pass", "passed", "success", "succeeded", "successful",
     "complete", "completed", "done", "finished"}
)


def _result_reported(
    verifier: DeterministicVerifier, spec: VerificationSpec, output: Mapping[str, Any]
) -> VerificationResult:
    """Check what the TOOL said about itself.

    The weakest evidence there is — the report IS the claim under test — so it
    is only ever used where nothing observable exists, and the result is
    stamped as a reported result (see ``_CONFIDENCE_BY_METHOD``) rather than
    presented as an observation. A missing report is INCONCLUSIVE, not a
    failure: a tool that does not describe its own outcome has told us nothing.
    """
    reported = _first(output, ("success", "succeeded", "ok", "status", "state"))
    if reported is None:
        return _inconclusive(
            spec,
            "The tool reported no success field, so its own result could not be read.",
        )
    if isinstance(reported, bool):
        said_ok = reported
    else:
        said_ok = str(reported).strip().lower() in _SUCCESS_WORDS
    expect = True if spec.expect is None else bool(spec.expect)
    passed = said_ok == expect
    return VerificationResult(
        status=VerificationStatus.PASS if passed else VerificationStatus.FAIL,
        method=spec.method,
        expectation=spec.description or f"the tool reports success == {expect}",
        observed=f"reported={reported!r}",
        reason=(
            f"The tool reported success ({reported!r}). This is the tool's own "
            "account, not an observed state."
            if passed
            else f"The tool did not report success (it reported {reported!r})."
        ),
        evidence={"reported": _plain_value(reported)},
        metadata={"evidence_kind": "reported"},
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
    VerificationMethod.WINDOW_EXISTS: _window_exists,
    VerificationMethod.NETWORK_REACHABLE: _network_reachable,
    VerificationMethod.RESULT_REPORTED: _result_reported,
}


# --------------------------------------------------------------------------- #
# Structured results
# --------------------------------------------------------------------------- #


def stamp(result: VerificationResult, *, verifier: str) -> VerificationResult:
    """Give a result the structured fields, WITHOUT inventing a verdict.

    The verdict is untouched: only the fields that describe it are filled in
    (who checked, what was expected, what was seen, how strong the evidence is)
    and only where the check did not state them itself. A result that could not
    be determined keeps confidence 0.0 — putting a comfortable number on an
    unknown is the soft PASS this module exists to refuse.
    """
    confidence = result.confidence
    if confidence == 0.0 and result.status in (
        VerificationStatus.PASS,
        VerificationStatus.FAIL,
    ):
        confidence = _CONFIDENCE_BY_METHOD.get(result.method, 0.0)
    if (
        result.expected_state
        and result.actual_state
        and result.confidence == confidence
    ):
        return result
    return result.with_(
        verifier=result.verifier or verifier,
        expected_state=result.expected_state or result.expectation,
        actual_state=result.actual_state or result.observed,
        confidence=confidence,
    )


# --------------------------------------------------------------------------- #
# Deriving the check an action implies
# --------------------------------------------------------------------------- #

#: What a step's ACTION implies about how to check it, tried in order. Only
#: verbs whose effect is observable from this process appear here: an action
#: nobody can check derives nothing, and SKIPPED is the honest answer rather
#: than a check constructed only to pass.
#:
#: The expectation is deliberately the cheapest observation that can FALSIFY
#: the action — a launch that produced no process did not launch — which is why
#: a wrong answer here is a FAIL that names what it looked for rather than a
#: silent success.
_DERIVATIONS: tuple[
    tuple[tuple[str, ...], VerificationMethod, tuple[str, ...], Any, str], ...
] = (
    (
        ("delete", "remove", "unlink", "trash", "erase"),
        VerificationMethod.FILE_EXISTS,
        ("path", "file", "target", "folder"),
        False,
        "the file is gone",
    ),
    (
        ("write", "create", "save", "mkdir", "touch", "export", "generate", "download"),
        VerificationMethod.FILE_EXISTS,
        ("path", "file", "target", "destination", "output", "folder"),
        True,
        "the file exists",
    ),
    (
        ("open", "launch", "start", "focus"),
        VerificationMethod.PROCESS_RUNNING,
        ("application", "app", "process", "target", "name"),
        True,
        "the application is running",
    ),
    (
        (
            "send", "upload", "post", "publish", "message", "notify", "order",
            "purchase", "fetch", "request", "http", "browse", "visit", "url", "ping",
            "download",
        ),
        VerificationMethod.NETWORK_REACHABLE,
        ("url", "host", "endpoint", "address", "target"),
        True,
        "the endpoint was reached",
    ),
    (
        ("run", "execute", "command", "shell", "test", "build", "install", "compile"),
        VerificationMethod.EXIT_CODE,
        ("command", "cmd", "script", "args"),
        None,
        "the command reported an exit code",
    ),
)


def default_verification_for(
    action: str = "", tool: str = "", parameters: Mapping[str, Any] | None = None
) -> VerificationSpec:
    """The check a step's action implies when the step attached none.

    This is the answer to "never assume a successful CALL means a successful
    ACTION": a step that says ``open_application`` is checked against the
    process being there, a write against the file being there, a command
    against a reported exit code. Anything with no observable effect derives
    nothing, and the verifier then reports SKIPPED rather than a pass.

    The target is taken from the step's own parameters, so the expectation is
    about the thing the step named. Nothing here guesses a value into
    existence: with no parameter to check, no spec is derived.
    """
    words = f"{action} {tool}".strip().lower()
    if not words:
        return VerificationSpec()
    given = dict(parameters or {})
    # Every matching verb is tried, not just the first: one action can imply
    # more than one kind of effect (a download puts a file somewhere OR reaches
    # an endpoint), and WHICH of them can be checked is decided by which
    # argument the step actually carries. A verb whose argument is absent is
    # skipped rather than forced, because a check with nothing to check is how
    # a verification layer starts reporting inconclusive noise.
    for markers, method, keys, expect, phrased in _DERIVATIONS:
        if not any(marker in words for marker in markers):
            continue
        target = _first_present(given, keys)
        if not isinstance(target, str) or not target.strip():
            continue
        return VerificationSpec(
            method=method,
            target=target.strip(),
            description=f"{action or tool} is checked against {phrased}: {target.strip()}",
            expect=expect,
        )
    return VerificationSpec()


def _first_present(source: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    """The first NAMED value that is not empty — for reading a step's PARAMETERS.

    Distinct from :func:`_first` on purpose. An outcome's ``exit_code=0`` and
    ``changed=False`` are answers, so the output reader must return them; a
    parameter that is blank names nothing to check, so this one skips it.
    """
    for key in keys:
        if key in source:
            value = source[key]
            if value not in (None, "", (), [], {}):
                return value
    return None


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


def _plain_value(value: Any) -> Any:
    """A value safe to put in evidence without losing what it was."""
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def unverified_step_reason() -> str:
    """The sentence an unverified side-effecting step is reported with."""
    return _HONEST_UNKNOWN


def interpret_result(result: Any) -> tuple[bool | None, str]:
    """Read whatever a verification produced as ``(passed, reason)``.

    ``None`` means "no verdict" — the shape a plain reason string has, which is
    reported as an unknown result rather than as a pass or a failure. Shared
    with the reliability engine so a tool strategy and a registered callable
    are read the same way.
    """
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


def _default_window_probe(name: str) -> bool | None:
    """Is a window for ``name`` open? "Cannot tell" unless a probe is wired.

    Window enumeration belongs to the desktop layer that already knows how to
    ask the platform for its visible windows, and a verifier that shelled out
    to PowerShell on every check would make verification cost more than the
    action. With no probe injected the honest answer is "cannot tell" — the
    check reports INCONCLUSIVE, never "not open", because no window was
    looked for and none was missed.
    """
    return None


def _default_network_probe(target: str) -> bool | None:
    """Whether a TCP connection to ``target`` succeeds, else None.

    A connection that completes is a measurement; a name that is not a host and
    port is not, and says so. No HTTP request is made: reachability is the
    observation, and a GET would be a side effect of a check.
    """
    host, port = _split_endpoint(target)
    if not host or port is None:
        return None
    try:
        with socket.create_connection((host, port), timeout=_NETWORK_TIMEOUT_S):
            return True
    except OSError:
        return False


#: How long a reachability check may take before it counts as unreachable.
_NETWORK_TIMEOUT_S = 2.0


#: Port assumed when the target does not name one, by scheme then by default.
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def _split_endpoint(target: str) -> tuple[str, int | None]:
    """``host`` and ``port`` from a URL, a ``host:port`` pair, or a bare host."""
    text = target.strip()
    if not text:
        return "", None
    scheme = ""
    if "://" in text:
        scheme, _, text = text.partition("://")
        text = text.split("/", 1)[0]
    text = text.split("@")[-1]
    host, sep, port_text = text.rpartition(":")
    if sep and port_text.isdigit():
        return host.strip("[]"), int(port_text)
    host = text.strip("[]")
    if not host:
        return "", None
    return host, _DEFAULT_PORTS.get(scheme.lower(), _DEFAULT_PORTS["https"])


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
