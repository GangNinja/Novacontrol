"""VerificationEngine: the door every action's result is checked through.

This is the specification's Phase 8.1 engine, and it is deliberately NOT a
second verifier. ``planning.verification.DeterministicVerifier`` already knows
how to check a file, an artifact, an exit code, a captured output, a process,
an observed change and a registered callable. What was missing is the three
things that decide WHICH check applies:

    a step that says how it will be checked      -> the step's own spec wins
    a TOOL that knows how to check itself        -> a registered tool strategy
    a step that said nothing                     -> a check derived from the
                                                    action it names

and the ordering matters, because it is the difference between verification and
a formality. A tool strategy may not overrule a step that names its own check,
and nothing is invented for an action whose effect cannot be observed from
here — such a step is reported SKIPPED, which is a real answer.

Two rules come with the design:

  * **A call is not a completion.** ``verify`` is where that is enforced: the
    result of running a tool is checked against the state it claims to have
    changed, and an inconclusive check is reported as an unknown rather than
    rounded up (see ``planning.models.VerificationResult.success``).
  * **A report is not an observation.** A tool's own "success: true" is the
    claim under test, so it is only read by the REPORTED method and is stamped
    with the weaker confidence it deserves.

Because this class SUBCLASSES the deterministic verifier, every existing call
site keeps working unchanged: ``WorkflowExecutor(verifier=...)``, the
``CALLABLE`` checks the application registers, and the injected process probe
all behave exactly as before.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from novacontrol.planning.models import (
    PlanStep,
    VerificationMethod,
    VerificationResult,
    VerificationSpec,
    VerificationStatus,
)
from novacontrol.planning.verification import (
    DeterministicVerifier,
    NetworkProbe,
    ProcessProbe,
    VerifyCallable,
    WindowProbe,
    default_verification_for,
    interpret_result,
    stamp,
)

#: What a check may return: a verdict, a reason for a reason-only answer, or a
#: whole result when the check knows more than pass/fail.
VerificationVerdict = bool | str | tuple[bool, str] | VerificationResult

#: A tool's own check, as a function of the step and its output.
ToolCheck = Callable[[PlanStep, Mapping[str, Any]], VerificationVerdict]


class ToolObjectVerifier(Protocol):
    """A tool check expressed as an object — a strategy that carries its state."""

    def verify(self, step: PlanStep, output: Mapping[str, Any]) -> VerificationVerdict: ...


#: What may be registered for a tool: a plain callable, or an object exposing
#: ``verify``. Both are read the same way (see :meth:`VerificationEngine.run_strategy`).
ToolVerifier = ToolCheck | ToolObjectVerifier


class VerificationEngine(DeterministicVerifier):
    """Checks a step's result with the strongest check that applies."""

    #: Stamped on every result, so a report can tell which engine produced it.
    name = "verification-engine"

    def __init__(
        self,
        *,
        callables: Mapping[str, VerifyCallable] | None = None,
        process_probe: ProcessProbe | None = None,
        window_probe: WindowProbe | None = None,
        network_probe: NetworkProbe | None = None,
        tool_strategies: Mapping[str, ToolVerifier] | None = None,
        derive_defaults: bool = True,
    ) -> None:
        super().__init__(
            callables=callables,
            process_probe=process_probe,
            window_probe=window_probe,
            network_probe=network_probe,
        )
        self._tool_strategies: dict[str, Any] = dict(tool_strategies or {})
        #: Whether a step that attached no check is checked by the one its
        #: action implies. On by default: it is the whole point of the engine,
        #: and a caller that wants the old behaviour says so explicitly.
        self.derive_defaults = derive_defaults

    # -- custom strategies, per tool -------------------------------------------

    def register_tool_strategy(self, tool: str, strategy: ToolVerifier) -> None:
        """Teach the engine how to check one tool's result.

        Registering replaces any previous strategy for that tool: two checks
        for one tool would leave the winner decided by dictionary order.
        """
        name = tool.strip()
        if not name:
            raise ValueError("A tool strategy needs the tool's name.")
        self._tool_strategies[name] = strategy

    def strategy_for(self, tool: str) -> Any | None:
        """The strategy registered for ``tool``, if this build has one."""
        return self._tool_strategies.get(tool.strip())

    def tool_strategies(self) -> tuple[str, ...]:
        """Which tools have a strategy, in a stable order (for status output)."""
        return tuple(sorted(self._tool_strategies))

    # -- checking -------------------------------------------------------------

    def verify(self, step: PlanStep, output: Mapping[str, Any]) -> VerificationResult:
        """The check that applies to THIS step, in order of authority.

        The step's own ``verification`` comes first and is not overridable: a
        plan that states how a step is checked has said what success means, and
        a default that outranked it would be the engine making the plan's
        decision for it.
        """
        if step.verification.required:
            return super().verify(step, output)

        strategy = self.strategy_for(step.tool)
        if strategy is not None:
            return self.run_strategy(step.tool, strategy, step=step, output=output)

        if self.derive_defaults:
            spec = self.default_spec_for(step)
            if spec.required:
                # Stamped in one pass: who ran the check has to be known when
                # the result is BUILT, because a stamp never overwrites one.
                return self.verify_spec(spec, output, verifier=f"{self.name}:derived")

        # Nothing claims to know how to check this step. SKIPPED says exactly
        # that, and the executor reports the step as unverified rather than
        # inventing a pass for it.
        return super().verify(step, output)

    def verify_tool(
        self,
        tool: str,
        output: Mapping[str, Any],
        *,
        action: str = "",
        parameters: Mapping[str, Any] | None = None,
        spec: VerificationSpec | None = None,
    ) -> VerificationResult:
        """Verify a TOOL's result without a plan step — the direct-action door.

        The same order as :meth:`verify`, minus the step: an explicit spec, then
        the tool's own strategy, then the check the action implies. This is what
        a caller uses for work that was dispatched outside a plan and still
        needs to be checked rather than believed.
        """
        if spec is not None and spec.required:
            return self.verify_spec(spec, output)
        strategy = self.strategy_for(tool)
        step = PlanStep(
            title=tool or "tool",
            description=f"Verify the result of {tool or 'the tool'}.",
            action=action or tool,
            tool=tool,
            parameters=dict(parameters or {}),
        )
        if strategy is not None:
            return self.run_strategy(tool, strategy, step=step, output=output)
        if self.derive_defaults:
            derived = self.default_spec_for(step)
            if derived.required:
                return self.verify_spec(derived, output, verifier=f"{self.name}:derived")
        return stamp(
            VerificationResult(
                status=VerificationStatus.SKIPPED,
                method=VerificationMethod.NONE,
                expectation=f"No check is attached to {tool!r}.",
                reason=(
                    "No verification is available for this tool: it has no registered "
                    "strategy and its action does not imply an observable result, so "
                    "its success cannot be confirmed from here."
                ),
            ),
            verifier=self.name,
        )

    def run_strategy(
        self,
        tool: str,
        strategy: ToolVerifier,
        *,
        step: PlanStep,
        output: Mapping[str, Any],
    ) -> VerificationResult:
        """Run one tool strategy and turn whatever it returned into a result.

        A strategy that raises is an UNKNOWN, never a pass: the check did not
        run, and a check that did not run proves nothing.
        """
        verifier_name = f"{self.name}:{tool}"
        check: ToolCheck = strategy if callable(strategy) else strategy.verify
        try:
            produced = check(step, output)
        except Exception as exc:  # noqa: BLE001 - reported as an unknown result
            return stamp(
                VerificationResult(
                    status=VerificationStatus.INCONCLUSIVE,
                    method=VerificationMethod.CALLABLE,
                    expectation=f"the check registered for {tool!r}",
                    reason=f"Verification for {tool!r} raised {type(exc).__name__}: {exc}",
                    error=f"{type(exc).__name__}: {exc}",
                ),
                verifier=verifier_name,
            )
        if isinstance(produced, VerificationResult):
            return stamp(produced, verifier=verifier_name)
        passed, reason = interpret_result(produced)
        if passed is None:
            return stamp(
                VerificationResult(
                    status=VerificationStatus.INCONCLUSIVE,
                    method=VerificationMethod.CALLABLE,
                    expectation=f"the check registered for {tool!r}",
                    reason=reason or f"The check registered for {tool!r} returned no verdict.",
                ),
                verifier=verifier_name,
            )
        return stamp(
            VerificationResult(
                status=VerificationStatus.PASS if passed else VerificationStatus.FAIL,
                method=VerificationMethod.CALLABLE,
                expectation=f"the check registered for {tool!r}",
                observed=f"passed={passed}",
                reason=reason
                or (
                    f"{tool!r} passed its own check."
                    if passed
                    else f"{tool!r} failed its own check."
                ),
                evidence={"tool": tool},
            ),
            verifier=verifier_name,
        )

    def default_spec_for(self, step: PlanStep) -> VerificationSpec:
        """The check the step's action implies (NONE when nothing is implied)."""
        return default_verification_for(step.action, step.tool, step.parameters)

    # -- reporting ------------------------------------------------------------

    def report(self) -> dict[str, Any]:
        """What this engine can check, for a status readout."""
        return {
            "verifier": self.name,
            "derive_defaults": self.derive_defaults,
            "callables": sorted(self._callables),
            "tool_strategies": list(self.tool_strategies()),
            "methods": sorted(method.value for method in _ENGINE_METHODS),
        }


#: The methods this engine can run — reported, never used to decide anything.
_ENGINE_METHODS = (
    VerificationMethod.FILE_EXISTS,
    VerificationMethod.ARTIFACT_EXISTS,
    VerificationMethod.EXIT_CODE,
    VerificationMethod.OUTPUT_CONTAINS,
    VerificationMethod.PROCESS_RUNNING,
    VerificationMethod.STATE_OBSERVED,
    VerificationMethod.CALLABLE,
    VerificationMethod.WINDOW_EXISTS,
    VerificationMethod.NETWORK_REACHABLE,
    VerificationMethod.RESULT_REPORTED,
)

__all__ = [
    "ToolCheck",
    "ToolObjectVerifier",
    "ToolVerifier",
    "VerificationEngine",
    "VerificationVerdict",
]
