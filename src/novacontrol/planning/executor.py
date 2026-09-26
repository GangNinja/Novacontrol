"""Executing a plan: dependency order, bounded retries, and honest outcomes.

The executor walks the step graph a wave at a time. Everything whose
dependencies are met becomes ready together; ready steps run CONCURRENTLY only
when every one of them is read-only and each has its own tool, because that is
the only case where running two things at once cannot change what the other one
sees. A step that changes something runs alone, in order, and waits for the
previous changing step — "may execute concurrently" is a statement about
readings, never about writes.

Three rules shape what a step's result is allowed to mean:

  * a step is only COMPLETED after its verification PASSED — a step that ran and
    was never checked is UNVERIFIED, which is reported, not rounded up;
  * a retry needs a reason from the :class:`RecoveryAdvisor`, is bounded by the
    plan's :class:`RetryPolicy`, and never happens for a destructive or external
    action;
  * a step that needs confirmation runs only when a confirmation channel says
    yes. With no channel wired, it is DENIED rather than attempted, because
    "nobody said no" is not permission.

The executor knows nothing about tools, approvals, or the machine: it calls the
step handler it was given and believes only the verification. That is what keeps
the permission layer where it already is — the handler the application supplies
dispatches through the approval-gated tool executor, and this file cannot
short-circuit it even by mistake.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from inspect import Parameter, isawaitable, signature
from typing import Any

from novacontrol.core.events import EventType
from novacontrol.planning.models import (
    EFFECTS_NEVER_RETRIED,
    FailureKind,
    Plan,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    RetryPolicy,
    StepError,
    StepOutcome,
    VerificationPolicy,
    VerificationResult,
    VerificationStatus,
    WorkflowResult,
)
from novacontrol.planning.recovery import RecoveryAction, RecoveryAdvisor
from novacontrol.planning.verification import DeterministicVerifier

#: Runs one step. May return a mapping (its output) or a full StepOutcome.
#: A handler may take ``(step)`` or ``(step, context)`` — a plan's later steps
#: (collect the output, analyse it, summarise it) need what earlier steps
#: produced, and handlers written before that existed still work.
StepHandler = Callable[..., "Mapping[str, Any] | StepOutcome | Awaitable[Any]"]


@dataclass(frozen=True, slots=True)
class StepContext:
    """What a step is allowed to know about the run around it.

    Deliberately tiny: the outputs of finished steps (so a collector can read
    the run it is collecting) and how many attempts this step has had. No plan
    state, no application handle, nothing a step could use to change the plan
    it is part of.
    """

    plan_id: str = ""
    outputs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    attempts: int = 1

    def output(self, step_id: str) -> Mapping[str, Any]:
        """What one earlier step produced (empty when it produced nothing)."""
        return dict(self.outputs.get(step_id, {}))

    def output_of(self, step_ids: Sequence[str]) -> Mapping[str, Any]:
        """The first non-empty output among the named steps."""
        for step_id in step_ids:
            found = self.outputs.get(step_id)
            if found:
                return dict(found)
        return {}

    def merge(self, **changes: Any) -> StepContext:
        return replace(self, **changes)

#: Asked before a step that needs confirmation. True = authorized to run.
ConfirmationChannel = Callable[[PlanStep], "bool | Awaitable[bool]"]

#: Told about every finished step, in completion order (progress, telemetry).
StepObserver = Callable[[StepOutcome], Awaitable[None] | None]

#: Told what the executor is DOING as it happens — a check starting, what a
#: check said, which recovery was chosen — as opposed to ``StepObserver``, which
#: hears about finished steps. Phase 9's event bus plugs in here, and the
#: executor never learns what is on the other end.
StepAnnouncer = Callable[[str, Mapping[str, Any]], Awaitable[None] | None]

#: The most steps that may share one wave. A cap keeps a wide plan from
#: stampeding the machine it is running on.
MAX_WAVE_SIZE = 4


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    """Kept for callers that predate the retry policy: attempts, nothing else."""

    max_step_attempts: int = 1

    def __post_init__(self) -> None:
        if self.max_step_attempts < 1:
            raise ValueError("max_step_attempts must be at least 1.")

    def as_retry_policy(self) -> RetryPolicy:
        return RetryPolicy(max_attempts=self.max_step_attempts)


class WorkflowExecutor:
    """Executes a plan while respecting dependencies, policies and approvals."""

    def __init__(
        self,
        *,
        step_handler: StepHandler | None = None,
        recovery_policy: RecoveryPolicy | None = None,
        retry_policy: RetryPolicy | None = None,
        verification_policy: VerificationPolicy | None = None,
        verifier: DeterministicVerifier | None = None,
        advisor: RecoveryAdvisor | None = None,
        confirmation: ConfirmationChannel | None = None,
        observer: StepObserver | None = None,
        announcer: StepAnnouncer | None = None,
        max_wave_size: int = MAX_WAVE_SIZE,
    ) -> None:
        self.step_handler = step_handler or _default_step_handler
        #: Whether an executor was actually BOUND. The default handler plans
        #: without executing anything, and the result says so rather than
        #: reporting a completed workflow that never touched the machine.
        self.executor_bound = step_handler is not None
        self.retry_policy = retry_policy or (
            recovery_policy.as_retry_policy()
            if recovery_policy is not None
            else RetryPolicy()
        )
        self.verification_policy = verification_policy or VerificationPolicy()
        self.verifier = verifier or DeterministicVerifier()
        self.advisor = advisor or RecoveryAdvisor(retry_policy=self.retry_policy)
        self.confirmation = confirmation
        self.observer = observer
        self.announcer = announcer
        self.max_wave_size = max(1, max_wave_size)

    async def _announce(self, type_: str, **payload: Any) -> None:
        """Say what is happening, never letting the saying fail the doing."""
        if self.announcer is None:
            return
        try:
            said = self.announcer(type_, payload)
            if isawaitable(said):
                await said
        except Exception:  # noqa: BLE001 - an announcement is not the work
            return

    # -- public ---------------------------------------------------------------

    async def execute(self, plan: Plan) -> WorkflowResult:
        """Run a plan to a conclusion, or to the first unrecoverable failure."""
        if plan.needs_clarification:
            return WorkflowResult(
                plan_id=plan.id,
                status=PlanStatus.NEEDS_CLARIFICATION,
                step_outputs={},
                errors={"plan": "Clarification required before execution."},
                summary="The plan was not executed: the request needs clarifying first.",
            )

        steps: tuple[PlanStep, ...] = tuple(plan.steps)
        policy = plan.retry_policy or self.retry_policy
        verification_policy = plan.verification_policy or self.verification_policy

        outcomes: dict[str, StepOutcome] = {}
        statuses: dict[str, PlanStepStatus] = {step.id: PlanStepStatus.PENDING for step in steps}

        while len(outcomes) < len(steps):
            wave = _ready_wave(steps, statuses, outcomes)
            if not wave:
                # Nothing can move: everything left is waiting on something that
                # will never finish. Reported as blocked, never as complete.
                for step_id, status in statuses.items():
                    if status is PlanStepStatus.PENDING:
                        statuses[step_id] = PlanStepStatus.BLOCKED
                        outcomes[step_id] = _blocked_outcome(step_id, steps, statuses)
                break

            if _may_run_concurrently(wave):
                snapshot = _outputs_snapshot(outcomes)
                results = await asyncio.gather(
                    *(
                        self._run_step(step, policy, verification_policy, plan.id, snapshot)
                        for step in wave[: self.max_wave_size]
                    )
                )
            else:
                results = []
                for step in wave:
                    outcome = await self._run_step(
                        step, policy, verification_policy, plan.id, _outputs_snapshot(outcomes)
                    )
                    results.append(outcome)
                    if outcome.status in (PlanStepStatus.FAILED, PlanStepStatus.DENIED):
                        # A failed write invalidates the steps behind it; the
                        # rest of the wave is not attempted, because its
                        # premises are gone. Independent branches still run:
                        # the outer loop picks up whatever else is ready.
                        break

            for outcome in results:
                outcomes[outcome.step_id] = outcome
                statuses[outcome.step_id] = outcome.status
                if self.observer is not None:
                    observed = self.observer(outcome)
                    if isawaitable(observed):
                        await observed

            if any(
                outcome.status in (PlanStepStatus.FAILED, PlanStepStatus.DENIED)
                for outcome in results
            ):
                _block_dependents(steps, statuses, outcomes, results)

        return self._summarise(plan, steps, statuses, outcomes)

    # -- one step -------------------------------------------------------------

    async def _run_step(
        self,
        step: PlanStep,
        policy: RetryPolicy,
        verification_policy: VerificationPolicy,
        plan_id: str = "",
        outputs: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> StepOutcome:
        started = time.perf_counter()
        recovery: list[str] = []
        attempts = 0
        effective = step
        error: StepError | None = None

        if step.needs_confirmation and self.confirmation is None:
            # Fail closed. A destructive or external step with no approval
            # channel is not "probably fine" — it is not attempted at all.
            return StepOutcome(
                step_id=step.id,
                status=PlanStepStatus.DENIED,
                summary=f"{step.title} needs confirmation and no approval channel is wired.",
                error=StepError(
                    FailureKind.PERMISSION_DENIED,
                    "No confirmation channel is available, so this action was not attempted.",
                    tool=step.tool,
                ),
                tool=step.tool,
                latency_ms=_ms(started),
            )

        if step.needs_confirmation and self.confirmation is not None:
            try:
                granted = self.confirmation(step)
                if isawaitable(granted):
                    granted = await granted
            except Exception as exc:
                return StepOutcome(
                    step_id=step.id,
                    status=PlanStepStatus.DENIED,
                    summary=f"{step.title} was not approved: {type(exc).__name__}.",
                    error=StepError(
                        FailureKind.PERMISSION_DENIED,
                        f"The approval check failed: {type(exc).__name__}: {exc}",
                        tool=step.tool,
                    ),
                    tool=step.tool,
                    latency_ms=_ms(started),
                )
            if not granted:
                return StepOutcome(
                    step_id=step.id,
                    status=PlanStepStatus.DENIED,
                    summary=f"{step.title} was not approved.",
                    error=StepError(
                        FailureKind.PERMISSION_DENIED,
                        "The action was not approved, so it was not attempted.",
                        tool=step.tool,
                    ),
                    tool=step.tool,
                    latency_ms=_ms(started),
                )

        context = StepContext(plan_id=plan_id, outputs=dict(outputs or {}), attempts=attempts)
        #: The check the LAST attempt produced, when it got as far as making one.
        #: A failure that was a FAILED CHECK must carry the check's evidence: an
        #: outcome reporting "not_run" for a step whose verification ran and said
        #: no is not a gap in reporting, it is a false statement about the run.
        #: Cleared by an attempt that raised, because a check that was never made
        #: proves nothing about the attempt that actually failed.
        last_verification: VerificationResult | None = None
        #: The recovery action the LAST attempt was taken under, so its outcome
        #: can be reported against the recovery that produced it.
        recovering: str = ""
        while True:
            attempts += 1
            context = context.merge(attempts=attempts)
            try:
                output = await _call_handler(self.step_handler, effective, context)
                if isinstance(output, StepOutcome):
                    return _with_latency(output, started, attempts, recovery, step.tool)
                payload = dict(output)
                last_verification = None
            except PermissionError as exc:
                # A refusal is not a failure: it is a decision, and the plan
                # stops on it without retrying or rephrasing the question.
                if recovering:
                    await self._announce(
                        EventType.RECOVERY_COMPLETED.value,
                        step_id=step.id,
                        outcome="refused",
                        action=recovering,
                    )
                return StepOutcome(
                    step_id=step.id,
                    status=PlanStepStatus.DENIED,
                    summary=f"{step.title} was refused: {exc}",
                    error=self.advisor.classify(step, str(exc), denied=True),
                    attempts=attempts,
                    latency_ms=_ms(started),
                    recovery=tuple(recovery),
                    tool=step.tool,
                )
            except Exception as exc:
                error = self.advisor.classify(
                    step, f"{type(exc).__name__}: {exc}", denied=isinstance(exc, PermissionError)
                )
                if recovering:
                    await self._announce(
                        EventType.RECOVERY_COMPLETED.value,
                        step_id=step.id,
                        outcome="still_failing",
                        action=recovering,
                    )
                    recovering = ""
                payload = {}
                last_verification = None
            else:
                await self._announce(
                    EventType.VERIFICATION_STARTED.value,
                    step_id=step.id,
                    attempt=attempts,
                )
                result = self.verifier.verify(effective, payload)
                await self._announce(
                    EventType.VERIFICATION_COMPLETED.value,
                    step_id=step.id,
                    status=result.status.value,
                    method=result.method.value,
                    confidence=result.confidence,
                    attempt=attempts,
                )
                if recovering:
                    await self._announce(
                        EventType.RECOVERY_COMPLETED.value,
                        step_id=step.id,
                        outcome="recovered" if result.verified else "still_failing",
                        action=recovering,
                    )
                    recovering = ""
                failure = _failure_from(result, step, verification_policy)
                if failure is None:
                    return _completed(step, payload, started, attempts, recovery, result)
                error = failure
                last_verification = result
                payload = {}

            decision = self.advisor.advise(
                step, error, attempts_made=attempts, retry_policy=policy
            )
            if decision.action is RecoveryAction.STOP:
                if recovering:
                    await self._announce(
                        EventType.RECOVERY_COMPLETED.value,
                        step_id=step.id,
                        outcome="exhausted",
                        action=recovering,
                    )
                return _failed(
                    step, error, started, attempts, recovery, decision.reason,
                    verification=last_verification,
                )
            await self._announce(
                EventType.RECOVERY_STARTED.value,
                step_id=step.id,
                action=decision.action.value,
                attempt=attempts,
                reason=decision.reason,
            )
            if decision.action is RecoveryAction.ESCALATE:
                await self._announce(
                    EventType.RECOVERY_COMPLETED.value,
                    step_id=step.id,
                    outcome="escalated",
                    action=decision.action.value,
                )
                recovery.append(RecoveryAction.ESCALATE.value)
                return _failed(
                    step,
                    StepError(
                        FailureKind.UNSUPPORTED
                        if error.kind in (FailureKind.MISSING_DEPENDENCY, FailureKind.UNSUPPORTED)
                        else error.kind,
                        error.message,
                        retryable=False,
                        tool=step.tool,
                    ),
                    started,
                    attempts,
                    recovery,
                    decision.reason,
                    escalated=True,
                    verification=last_verification,
                )
            if step.effect in EFFECTS_NEVER_RETRIED:
                # Belt and braces: the advisor already refuses this, and the
                # executor refuses it again, because this is the rule whose
                # violation deletes something.
                return _failed(
                    step, error, started, attempts, recovery, decision.reason,
                    verification=last_verification,
                )
            if decision.action is RecoveryAction.MODIFY_PARAMETERS and decision.adjustments:
                recovery.append(RecoveryAction.MODIFY_PARAMETERS.value)
                repaired = {**effective.parameters, **decision.adjustments}
                effective = effective.with_(parameters=repaired)
                recovering = RecoveryAction.MODIFY_PARAMETERS.value
            else:
                recovery.append(RecoveryAction.RETRY.value)
                recovering = RecoveryAction.RETRY.value
            if decision.backoff_seconds:
                await asyncio.sleep(decision.backoff_seconds)

    # -- result ---------------------------------------------------------------

    def _summarise(
        self,
        plan: Plan,
        steps: Sequence[PlanStep],
        statuses: Mapping[str, PlanStepStatus],
        outcomes: Mapping[str, StepOutcome],
    ) -> WorkflowResult:
        failed = [
            step_id
            for step_id, status in statuses.items()
            if status in (PlanStepStatus.FAILED, PlanStepStatus.DENIED, PlanStepStatus.BLOCKED)
        ]
        verified = [
            step_id for step_id, outcome in outcomes.items() if outcome.verified
        ]
        required = [
            step.id
            for step in steps
            if step.verification.required or self.verification_policy.requires(step.effect)
        ]
        # "required but not proven": a step that ran and was not verified says
        # so, whether the executor marked it UNVERIFIED or a check came back
        # inconclusive. Rounded up to COMPLETED is exactly the dishonest case.
        unverified = tuple(
            step_id
            for step_id in required
            if step_id in outcomes
            and outcomes[step_id].status
            in (PlanStepStatus.COMPLETED, PlanStepStatus.UNVERIFIED)
            and not outcomes[step_id].verified
        )
        executed = self.executor_bound and any(
            outcome.executed for outcome in outcomes.values()
        )
        all_verified = bool(required) and all(
            step_id in verified for step_id in required
        )

        # BLOCKED rather than FAILED whenever no step actually failed: a denial
        # is a decision and a step that can never become ready is a defect in the
        # plan, and neither one is a failure to report as one. What did NOT
        # happen is reported as what it was.
        any_failed = any(value is PlanStepStatus.FAILED for value in statuses.values())
        denied = any(value is PlanStepStatus.DENIED for value in statuses.values())

        if failed:
            status = PlanStatus.FAILED if any_failed else PlanStatus.BLOCKED
        elif not outcomes:
            status = PlanStatus.PENDING
        else:
            status = PlanStatus.COMPLETED

        errors = {
            step_id: outcome.error.message
            for step_id, outcome in outcomes.items()
            if outcome.error is not None
        }
        return WorkflowResult(
            plan_id=plan.id,
            status=status,
            step_outputs={
                step_id: dict(outcome.output) for step_id, outcome in outcomes.items()
            },
            errors=errors,
            step_results=dict(outcomes),
            attempts={step_id: outcome.attempts for step_id, outcome in outcomes.items()},
            recovery={step_id: outcome.recovery for step_id, outcome in outcomes.items()},
            executed=executed,
            verified=all_verified,
            unverified_steps=unverified,
            summary=_summary_sentence(
                status, outcomes, executed, all_verified, unverified, denied=denied
            ),
        )


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #


def _failure_from(
    result: VerificationResult, step: PlanStep, policy: VerificationPolicy
) -> StepError | None:
    """A verification only blocks a step when it FAILED, or when the policy says
    an inconclusive result is not acceptable. Unverified is not failed."""
    if result.status is VerificationStatus.FAIL:
        return StepError(
            FailureKind.VERIFICATION_FAILED,
            result.reason or "The step's result did not match what was expected.",
            retryable=True,
            tool=step.tool,
        )
    if result.status is VerificationStatus.INCONCLUSIVE and not policy.allow_inconclusive:
        return StepError(
            FailureKind.VERIFICATION_FAILED,
            result.reason or "The step could not be verified.",
            tool=step.tool,
        )
    return None


def _ready_wave(
    steps: Sequence[PlanStep],
    statuses: Mapping[str, PlanStepStatus],
    outcomes: Mapping[str, StepOutcome],
) -> list[PlanStep]:
    """The steps whose dependencies are satisfied, in declared order."""
    done = {
        step_id
        for step_id, status in statuses.items()
        if status in (PlanStepStatus.COMPLETED, PlanStepStatus.UNVERIFIED)
        and step_id in outcomes
    }
    return [
        step
        for step in steps
        if statuses.get(step.id) is PlanStepStatus.PENDING
        and all(dependency in done for dependency in step.depends_on)
    ]


def _outputs_snapshot(outcomes: Mapping[str, StepOutcome]) -> dict[str, dict[str, Any]]:
    """What finished steps produced, for the steps that read their results."""
    return {step_id: dict(outcome.output) for step_id, outcome in outcomes.items()}


def _may_run_concurrently(wave: Sequence[PlanStep]) -> bool:
    """Concurrency is allowed only when nothing in the wave can interfere.

    Three conditions, all necessary:

      * two steps at least — a wave of one is just a step;
      * EVERY step declares itself parallel-safe, which the model only permits
        on a read-only step, so no write can reach this point;
      * no two steps name the same explicit RESOURCE. This is the conflict
        guard, and it is about the thing being touched rather than the tool
        doing the touching: one read-only tool (the system monitor) legitimately
        serves CPU, RAM and GPU readings at the same time, while two steps that
        name the same file are not independent even if both only read it.
    """
    if len(wave) < 2:
        return False
    if not all(step.parallel_safe for step in wave):
        return False
    resources = [
        str(step.parameters.get("resource", "")) for step in wave if step.parameters.get("resource")
    ]
    return len(set(resources)) == len(resources)


def _block_dependents(
    steps: Sequence[PlanStep],
    statuses: dict[str, PlanStepStatus],
    outcomes: dict[str, StepOutcome],
    results: Sequence[StepOutcome],
) -> None:
    """Anything downstream of a failure is blocked, and never attempted."""
    dead = {
        outcome.step_id
        for outcome in results
        if outcome.status in (PlanStepStatus.FAILED, PlanStepStatus.DENIED)
    }
    changed = True
    while changed:
        changed = False
        for step in steps:
            if statuses.get(step.id) is not PlanStepStatus.PENDING:
                continue
            if any(dependency in dead for dependency in step.depends_on):
                statuses[step.id] = PlanStepStatus.BLOCKED
                outcomes[step.id] = _blocked_outcome(step.id, steps)
                dead.add(step.id)
                changed = True


def _blocked_outcome(
    step_id: str,
    steps: Sequence[PlanStep],
    statuses: Mapping[str, PlanStepStatus] | None = None,
) -> StepOutcome:
    """A step that was never attempted, saying precisely why.

    "A dependency did not complete" covers two different situations that a
    person reading the report needs to tell apart: a dependency that ran and
    failed, and a dependency that is not in the plan at all (a compiler defect,
    not a runtime one). Naming which dependency, and which of the two it is,
    is the difference between a report and a shrug.
    """
    step = next((item for item in steps if item.id == step_id), None)
    title = step.title if step is not None else step_id
    dependencies = step.depends_on if step is not None else ()
    known = {item.id for item in steps}
    absent = [name for name in dependencies if name not in known]
    if absent:
        summary = f"{title} was not attempted: it depends on a step that is not in this plan."
        message = (
            "This step depends on "
            + ", ".join(repr(name) for name in absent)
            + ", which is not part of this plan, so it can never become ready."
        )
        kind = FailureKind.MISSING_DEPENDENCY
    else:
        unfinished = [
            name
            for name in dependencies
            if (statuses or {}).get(name)
            in (
                PlanStepStatus.FAILED,
                PlanStepStatus.DENIED,
                PlanStepStatus.BLOCKED,
                PlanStepStatus.SKIPPED,
            )
        ]
        named = f" ({', '.join(unfinished)})" if unfinished else ""
        summary = f"{title} was not attempted: a step it depends on did not complete."
        message = (
            f"Dependencies{named} failed or were denied, so this step was never attempted."
        )
        kind = FailureKind.UNKNOWN
    return StepOutcome(
        step_id=step_id,
        status=PlanStepStatus.BLOCKED,
        summary=summary,
        error=StepError(
            kind,
            message,
            tool=step.tool if step is not None else "",
        ),
        tool=step.tool if step is not None else "",
    )


# --------------------------------------------------------------------------- #
# Outcomes
# --------------------------------------------------------------------------- #


async def _call_handler(handler: StepHandler, step: PlanStep, context: StepContext) -> Any:
    """Call a step handler, with or without the run context.

    The two-argument form is what a collecting or analysing step needs. The
    one-argument form is what every handler written before plans had context
    uses, and it is detected from the signature rather than guessed from a
    swallowed TypeError, so a genuine error inside a handler is still an error.
    """
    result = handler(step, context) if _accepts_context(handler) else handler(step)
    if isawaitable(result):
        result = await result
    return result


def _accepts_context(handler: StepHandler) -> bool:
    try:
        parameters = signature(handler).parameters.values()
    except (TypeError, ValueError):
        return False
    positional = 0
    for parameter in parameters:
        if parameter.kind is Parameter.VAR_POSITIONAL:
            return True
        if parameter.kind in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD):
            positional += 1
    return positional >= 2


def _completed(
    step: PlanStep,
    payload: Mapping[str, Any],
    started: float,
    attempts: int,
    recovery: Sequence[str],
    verification: VerificationResult,
) -> StepOutcome:
    """A step that ran. Verified only when the check actually passed."""
    status = (
        PlanStepStatus.COMPLETED if verification.verified else PlanStepStatus.UNVERIFIED
    )
    return StepOutcome(
        step_id=step.id,
        status=status,
        summary=str(payload.get("summary") or step.expected_result or step.description),
        output={key: value for key, value in payload.items() if key != "verification"},
        verification=verification,
        attempts=attempts,
        latency_ms=_ms(started),
        recovery=tuple(recovery),
        tool=step.tool,
    )


def _failed(
    step: PlanStep,
    error: StepError | None,
    started: float,
    attempts: int,
    recovery: Sequence[str],
    reason: str,
    *,
    escalated: bool = False,
    verification: VerificationResult | None = None,
) -> StepOutcome:
    """A step that did not succeed. ``verification`` is passed when a check
    actually ran and is what failed; it stays None when the step never got as
    far as being checked, which is a different fact."""
    message = error.message if error is not None else "The step failed without a reported reason."
    return StepOutcome(
        step_id=step.id,
        status=PlanStepStatus.FAILED,
        summary=reason,
        error=error
        or StepError(
            FailureKind.UNKNOWN,
            message,
            tool=step.tool,
        ),
        verification=verification,
        attempts=attempts,
        latency_ms=_ms(started),
        recovery=tuple(recovery),
        tool=step.tool,
        output={"escalate": True} if escalated else {},
    )


def _with_latency(
    outcome: StepOutcome,
    started: float,
    attempts: int,
    recovery: Sequence[str],
    tool: str,
) -> StepOutcome:
    return StepOutcome(
        step_id=outcome.step_id,
        status=outcome.status,
        summary=outcome.summary,
        output=outcome.output,
        error=outcome.error,
        verification=outcome.verification,
        attempts=attempts,
        latency_ms=_ms(started),
        recovery=tuple(dict.fromkeys((*recovery, *outcome.recovery))),
        tool=outcome.tool or tool,
    )


def _summary_sentence(
    status: PlanStatus,
    outcomes: Mapping[str, StepOutcome],
    executed: bool,
    verified: bool,
    unverified: Sequence[str],
    *,
    denied: bool = False,
) -> str:
    if status is PlanStatus.NEEDS_CLARIFICATION:
        return "Nothing ran: the request needs clarifying first."
    if not outcomes:
        return "Nothing ran: the plan had no steps."
    if status is PlanStatus.FAILED:
        first = next(
            (
                outcome
                for outcome in outcomes.values()
                if outcome.status
                in (
                    PlanStepStatus.FAILED,
                    PlanStepStatus.DENIED,
                    PlanStepStatus.BLOCKED,
                )
            ),
            None,
        )
        detail = first.summary if first is not None else "nothing could be attempted"
        return f"The plan stopped: {detail}"
    if status is PlanStatus.BLOCKED:
        if denied:
            return "The plan did not run: a required action was not authorized."
        blocked = next(
            (
                outcome
                for outcome in outcomes.values()
                if outcome.status is PlanStepStatus.BLOCKED
            ),
            None,
        )
        if blocked is not None and blocked.summary:
            return f"The plan did not run: {blocked.summary}"
        return "The plan did not run: nothing in it could be attempted."
    if not executed:
        return (
            "The plan was built but nothing was executed, because no executor is bound "
            "on this path — the steps are a preview, not a result."
        )
    if unverified:
        return (
            f"All steps completed, but {len(unverified)} of them could not be verified, "
            "so their results are reported as unconfirmed."
        )
    if verified:
        return "Every step completed and was verified against the state it changed."
    return "All steps completed."


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


async def _default_step_handler(step: PlanStep) -> Mapping[str, Any]:
    """The plan-only runner: reports the step as planned and claims nothing.

    A plan executed with no bound executor is a PREVIEW. Returning a status that
    reads like success is exactly the blind assumption the specification warns
    about, so this handler returns no verification at all, and the workflow
    result says nothing was executed.
    """
    return {
        "summary": step.expected_result or step.description,
        "planned": True,
        "executed": False,
    }
