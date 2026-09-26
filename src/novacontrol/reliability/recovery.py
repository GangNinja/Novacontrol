"""RecoveryEngine: what happens after a failure, decided from evidence.

``planning.recovery.RecoveryAdvisor`` already owns the SAFETY rules — a
destructive or external action is never retried, a denial is a human decision
and not an invitation to ask again, and the attempt budget comes from the
plan's :class:`RetryPolicy` whose ceiling makes an unbounded loop
unrepresentable. This module is the engine around it, and it adds the four
things the advisor deliberately does not do:

    analyse   classify the failure and say whether a retry is SAFE, not just
              whether it is permitted;
    alternate try a DIFFERENT way to reach the same goal when the step's own
              way cannot work (a tool that is not registered here, an action
              nothing in this installation can carry out);
    confirm   ask the user when the only way forward needs their approval, and
              stop when they say no;
    bound     run the whole thing — attempt, verify, recover — under a hard
              attempt ceiling, so recovery can never become the infinite loop
              it exists to prevent.

Every decision is a :class:`RecoveryPlan`: what to do next, why, and whether
what just happened is safe to repeat. Nothing here executes anything on its
own; :meth:`RecoveryEngine.run_with_recovery` does, and it takes the executor
and the verifier as arguments so the permission layer stays exactly where it
already is.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from inspect import isawaitable
from typing import Any

from novacontrol.planning.models import (
    EFFECTS_NEVER_RETRIED,
    MAX_STEP_ATTEMPTS,
    FailureKind,
    PlanStep,
    RetryPolicy,
    StepError,
    VerificationPolicy,
    VerificationResult,
    VerificationStatus,
)
from novacontrol.planning.recovery import (
    RecoveryAction,
    RecoveryAdvisor,
    RecoveryDecision,
    repair_parameters,
)
from novacontrol.planning.verification import DeterministicVerifier


class RecoveryKind(StrEnum):
    """What recovery concluded, in the order it is preferred."""

    #: The step may run again as it is (a transient failure).
    RETRY = "retry"
    #: Run a DIFFERENT step that reaches the same goal.
    ALTERNATIVE = "alternative"
    #: The step needs reasoning this layer does not have; hand it upward.
    ESCALATE = "escalate"
    #: Nothing may proceed without a human's decision.
    ASK_USER = "ask_user"
    #: Stop, safely, and report why.
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class FailureAnalysis:
    """What the failure actually was, before deciding what to do about it."""

    kind: FailureKind
    message: str
    #: Whether repeating this step, as it is, is safe. A permission denial or a
    #: destructive action is not.
    retry_safe: bool
    #: Whether the attempt budget allows another run at all.
    attempts_left: bool
    #: Why — the sentence a report shows next to the failure.
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "message": self.message,
            "retry_safe": self.retry_safe,
            "attempts_left": self.attempts_left,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """The next thing to do about a failure, and the evidence for it."""

    kind: RecoveryKind
    reason: str
    diagnosis: str = ""
    #: The step to run next. Equal to the failed step for RETRY, a rewritten
    #: one for ALTERNATIVE, and the failed one again for ASK_USER.
    step: PlanStep | None = None
    adjustments: Mapping[str, Any] = field(default_factory=dict)
    attempts_made: int = 1
    attempts_left: int = 0
    retry_safe: bool = False
    requires_confirmation: bool = False
    #: True when the engine stopped because continuing was not safe — the
    #: distinction between \"gave up\" and \"stopped before doing harm\".
    stopped_safely: bool = False

    @property
    def will_run_again(self) -> bool:
        return self.kind in (RecoveryKind.RETRY, RecoveryKind.ALTERNATIVE)

    def as_decision(self) -> RecoveryDecision:
        """This plan in the executor's vocabulary, so it can be acted on.

        An ALTERNATIVE with repaired parameters IS the advisor's
        ``MODIFY_PARAMETERS``: a different attempt rather than the same one
        again, which is exactly what the executor already knows how to apply.
        """
        if self.kind is RecoveryKind.RETRY:
            action = RecoveryAction.RETRY
        elif self.kind is RecoveryKind.ALTERNATIVE:
            action = RecoveryAction.MODIFY_PARAMETERS
        elif self.kind is RecoveryKind.ESCALATE:
            action = RecoveryAction.ESCALATE
        else:
            action = RecoveryAction.STOP
        return RecoveryDecision(action, self.reason, adjustments=dict(self.adjustments))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "reason": self.reason,
            "diagnosis": self.diagnosis,
            "adjustments": dict(self.adjustments),
            "attempts_made": self.attempts_made,
            "attempts_left": self.attempts_left,
            "retry_safe": self.retry_safe,
            "requires_confirmation": self.requires_confirmation,
            "stopped_safely": self.stopped_safely,
            "step": self.step.to_dict() if self.step is not None else None,
        }


#: An alternative strategy: given the failed step and the error, return the step
#: to run instead (or None when this alternative does not apply).
AlternativeStrategy = Callable[[PlanStep, StepError], "PlanStep | None"]

#: How a caller checks one step's outcome. May be sync or async, so the engine
#: can be given the verification engine directly or a lightweight probe.
VerifyStep = Callable[
    [PlanStep, Mapping[str, Any]], "VerificationResult | Awaitable[VerificationResult]"
]

#: The alternatives this installation knows for itself: a way to reach the same
#: goal when the tool the plan named cannot do it. Deliberately tiny and
#: explicit — an alternative is a REPAIR with a stated reason, not a guess.
_BUILTIN_ALTERNATIVES: tuple[tuple[tuple[str, ...], str, tuple[str, ...]], ...] = (
    # opening an application: the desktop layer can launch a target the tool
    # registry has no entry for, and vice versa.
    (
        ("open_application", "open_app", "launch_application"),
        "desktop_open_application",
        ("application", "app", "target"),
    ),
    (
        ("desktop_open_application",),
        "open_application",
        ("application", "app", "target"),
    ),
    (
        ("run_command", "execute_command", "shell"),
        "desktop_run_command",
        ("command", "cmd"),
    ),
    (
        ("read_file",),
        "desktop_read_file",
        ("path", "file"),
    ),
    (
        ("write_file",),
        "desktop_write_file",
        ("path", "file"),
    ),
)


@dataclass(frozen=True, slots=True)
class RecoveryRun:
    """What a bounded run-with-recovery produced."""

    step_id: str
    succeeded: bool
    attempts: int
    executed: bool
    #: True only when a check PASSED. A run can be executed and unconfirmed,
    #: which is a different answer from both success and failure.
    verified: bool = False
    output: Mapping[str, Any] = field(default_factory=dict)
    verification: VerificationResult | None = None
    plans: tuple[RecoveryPlan, ...] = ()
    error: StepError | None = None
    stopped_safely: bool = False
    requires_confirmation: bool = False
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "succeeded": self.succeeded,
            "attempts": self.attempts,
            "executed": self.executed,
            "verified": self.verified,
            "output": dict(self.output),
            "verification": self.verification.to_dict() if self.verification else None,
            "recovery": [plan.to_dict() for plan in self.plans],
            "error": self.error.to_dict() if self.error else None,
            "stopped_safely": self.stopped_safely,
            "requires_confirmation": self.requires_confirmation,
            "summary": self.summary,
        }


class RecoveryEngine:
    """Turns a failure into the next safe action, within a hard budget."""

    def __init__(
        self,
        *,
        retry_policy: RetryPolicy | None = None,
        advisor: RecoveryAdvisor | None = None,
        verifier: DeterministicVerifier | None = None,
        verification_policy: VerificationPolicy | None = None,
        known_tools: tuple[str, ...] | Callable[[], tuple[str, ...]] | None = None,
        confirmation_available: bool = False,
        escalation_available: bool = False,
    ) -> None:
        self.retry_policy = retry_policy or RetryPolicy()
        self.verification_policy = verification_policy or VerificationPolicy()
        self.advisor = advisor or RecoveryAdvisor(
            retry_policy=self.retry_policy, escalation_available=escalation_available
        )
        self.verifier = verifier or DeterministicVerifier()
        #: Which tools exist on this installation — a tuple, or a CALLABLE that
        #: returns one, because a registry populated during startup is not the
        #: same registry as the empty one at construction time. An alternative
        #: naming a tool that is not here is not an alternative, so it is never
        #: offered; None means "this build did not say", and the built-ins are
        #: then offered as the honest best knowledge available.
        self.known_tools = known_tools
        self.confirmation_available = confirmation_available
        self.escalation_available = escalation_available
        self._alternatives: dict[str, AlternativeStrategy] = {}

    # -- teaching it new ways --------------------------------------------------

    def register_alternative(self, tool_or_action: str, strategy: AlternativeStrategy) -> None:
        """Register a way to reach a goal whose own step failed."""
        name = tool_or_action.strip()
        if not name:
            raise ValueError("An alternative needs the tool or action it replaces.")
        self._alternatives[name] = strategy

    def alternatives(self) -> tuple[str, ...]:
        return tuple(sorted(self._alternatives))

    # -- analysis --------------------------------------------------------------

    def analyze_failure(
        self,
        step: PlanStep,
        error: StepError,
        *,
        attempts_made: int,
        retry_policy: RetryPolicy | None = None,
    ) -> FailureAnalysis:
        """What went wrong, and whether trying again is SAFE — not merely allowed.

        Two separate questions, and both are asked: ``retryable`` on the error
        says the failure kind is one retrying can fix, ``retry_safe`` says
        repeating this step cannot do harm, and the budget says whether there is
        an attempt left to spend.
        """
        policy = retry_policy or self.retry_policy
        attempts_left = policy.attempts_left(attempts_made=attempts_made)
        if step.effect in EFFECTS_NEVER_RETRIED:
            return FailureAnalysis(
                error.kind,
                error.message,
                retry_safe=False,
                attempts_left=attempts_left,
                reason=(
                    f"{step.effect.value} actions are never repeated automatically: "
                    "a second destructive or external action is a new risk, not a "
                    "second chance."
                ),
            )
        if error.kind is FailureKind.PERMISSION_DENIED:
            return FailureAnalysis(
                error.kind,
                error.message,
                retry_safe=False,
                attempts_left=attempts_left,
                reason=(
                    "The action was not authorized; repeating it would be asking the "
                    "same question twice, so only a person can unblock this."
                ),
            )
        return FailureAnalysis(
            error.kind,
            error.message,
            retry_safe=error.retryable,
            attempts_left=attempts_left,
            reason=(
                f"{error.kind.value} failure with {policy.max_attempts - attempts_made} "
                f"attempt(s) left of {policy.max_attempts}."
            ),
        )

    def classify(self, step: PlanStep, message: str, *, denied: bool = False) -> StepError:
        """Turn a raw failure message into a classified error (delegated)."""
        return self.advisor.classify(step, message, denied=denied)

    # -- the decision ----------------------------------------------------------

    def plan_recovery(
        self,
        step: PlanStep,
        error: StepError,
        *,
        attempts_made: int,
        retry_policy: RetryPolicy | None = None,
    ) -> RecoveryPlan:
        """The next thing to do, in the order that keeps people and data safe."""
        policy = retry_policy or self.retry_policy
        analysis = self.analyze_failure(
            step, error, attempts_made=attempts_made, retry_policy=policy
        )
        attempts_left = max(0, policy.max_attempts - attempts_made)

        # 1. Safety first, in the advisor's own order: some steps are never
        #    repeated whatever went wrong, and a second destructive or external
        #    action is a new risk rather than a second chance. Checked before the
        #    denial rule below, because "who could approve it" must not become a
        #    route to repeating an action that may not be repeated at all.
        if step.effect in EFFECTS_NEVER_RETRIED:
            return RecoveryPlan(
                RecoveryKind.STOP,
                analysis.reason,
                diagnosis=analysis.reason,
                step=step,
                attempts_made=attempts_made,
                attempts_left=attempts_left,
                stopped_safely=True,
            )

        # 2. A refusal is a decision. Ask the person who can change it, and stop
        #    when there is nobody to ask — never retry into the same wall.
        if error.kind is FailureKind.PERMISSION_DENIED:
            if self.confirmation_available:
                return RecoveryPlan(
                    RecoveryKind.ASK_USER,
                    "The action needs a person's approval before it can run.",
                    diagnosis=analysis.reason,
                    step=step,
                    attempts_made=attempts_made,
                    attempts_left=attempts_left,
                    requires_confirmation=True,
                )
            return RecoveryPlan(
                RecoveryKind.STOP,
                "The action was not authorized and there is no one to ask, so recovery stops.",
                diagnosis=analysis.reason,
                step=step,
                attempts_made=attempts_made,
                attempts_left=attempts_left,
                stopped_safely=True,
            )

        # 3. A DIFFERENT way, before another try at the same one. A missing tool
        #    cannot be fixed by repetition, and a repair the step can actually
        #    carry out beats a loop.
        alternative = self.alternative_for(step, error)
        if alternative is not None:
            return RecoveryPlan(
                RecoveryKind.ALTERNATIVE,
                f"Trying a different way to reach the same goal: {alternative.title}.",
                diagnosis=analysis.reason,
                step=alternative,
                adjustments=dict(alternative.parameters),
                attempts_made=attempts_made,
                attempts_left=attempts_left,
                retry_safe=True,
            )

        # 4. Missing machinery: escalate when something can take over, and stop
        #    honestly when nothing can.
        if error.kind in (FailureKind.MISSING_DEPENDENCY, FailureKind.UNSUPPORTED):
            if self.escalation_available:
                return RecoveryPlan(
                    RecoveryKind.ESCALATE,
                    f"A prerequisite is missing or unsupported here: {error.message}",
                    diagnosis=analysis.reason,
                    step=step,
                    attempts_made=attempts_made,
                    attempts_left=attempts_left,
                )
            return RecoveryPlan(
                RecoveryKind.STOP,
                "Nothing here can carry this out and no escalation target is wired: "
                f"{error.message}",
                diagnosis=analysis.reason,
                step=step,
                attempts_made=attempts_made,
                attempts_left=attempts_left,
                stopped_safely=True,
            )

        # 5. Repair before repeating, using the advisor's own rule and repair.
        adjustments = (
            repair_parameters(step, error)
            if error.kind is FailureKind.INVALID_PARAMETERS
            else {}
        )
        if adjustments and policy.attempts_left(attempts_made=attempts_made):
            repaired = replace(step, parameters={**step.parameters, **adjustments})
            return RecoveryPlan(
                RecoveryKind.ALTERNATIVE,
                "Repaired the parameters the tool rejected: "
                + ", ".join(sorted(adjustments)),
                diagnosis=analysis.reason,
                step=repaired,
                adjustments=adjustments,
                attempts_made=attempts_made,
                attempts_left=attempts_left,
                retry_safe=True,
            )

        # 6. Bounded retry, decided by the advisor so the two agree.
        decision = self.advisor.advise(
            step, error, attempts_made=attempts_made, retry_policy=policy
        )
        if decision.action is RecoveryAction.RETRY:
            return RecoveryPlan(
                RecoveryKind.RETRY,
                decision.reason,
                diagnosis=analysis.reason,
                step=step,
                attempts_made=attempts_made,
                attempts_left=attempts_left,
                retry_safe=True,
            )
        if decision.action is RecoveryAction.ESCALATE:
            return RecoveryPlan(
                RecoveryKind.ESCALATE,
                decision.reason,
                diagnosis=analysis.reason,
                step=step,
                attempts_made=attempts_made,
                attempts_left=attempts_left,
            )
        return RecoveryPlan(
            RecoveryKind.STOP,
            decision.reason,
            diagnosis=analysis.reason,
            step=step,
            attempts_made=attempts_made,
            attempts_left=attempts_left,
        )

    def alternative_for(self, step: PlanStep, error: StepError) -> PlanStep | None:
        """A registered or built-in alternative for this failing step.

        Registered strategies win, because they were written for this
        installation. A built-in is only offered when its tool is actually
        present (``known_tools``), and only for the failures a different route
        can plausibly fix — never for a destructive or external step, whose
        failure stops rather than reroutes.
        """
        if step.effect in EFFECTS_NEVER_RETRIED:
            return None
        if error.kind is not FailureKind.MISSING_DEPENDENCY:
            # Rerouting is for the STRUCTURAL case — the tool the plan named is
            # not here. A transient failure needs the same step tried again, and
            # a refusal is about the ACTION rather than the tool, so sending it
            # to another tool would smuggle it past the decision that refused it.
            return None
        for key in (step.tool, step.action):
            strategy = self._alternatives.get(key.strip())
            if strategy is None:
                continue
            produced = strategy(step, error)
            if produced is not None:
                return produced
        return self._builtin_alternative(step)

    def _builtin_alternative(self, step: PlanStep) -> PlanStep | None:
        words = f"{step.tool} {step.action}".strip().lower()
        for markers, replacement, _keys in _BUILTIN_ALTERNATIVES:
            if not any(marker in words for marker in markers):
                continue
            if replacement.strip() == step.tool.strip():
                continue
            known = self._known_tool_names()
            if known is not None and replacement not in known:
                continue
            return replace(
                step,
                tool=replacement,
                title=f"{step.title} (via {replacement})",
                description=(
                    f"{step.description} Tried through {replacement} instead, because "
                    f"{step.tool or step.action or 'the original tool'} could not carry it out."
                ),
            )
        return None

    def _known_tool_names(self) -> tuple[str, ...] | None:
        if self.known_tools is None:
            return None
        if callable(self.known_tools):
            return tuple(self.known_tools())
        return tuple(self.known_tools)

    # -- running with recovery -------------------------------------------------

    async def run_with_recovery(
        self,
        step: PlanStep,
        *,
        execute: Callable[[PlanStep], Mapping[str, Any] | Awaitable[Mapping[str, Any]]],
        verify: VerifyStep | None = None,
        retry_policy: RetryPolicy | None = None,
        max_attempts: int | None = None,
        confirm: Callable[[PlanStep], bool | Awaitable[bool]] | None = None,
    ) -> RecoveryRun:
        """Run one step, verify it, and recover from what fails — bounded.

        The ceiling is the smaller of the policy's ``max_attempts`` and any
        explicit ``max_attempts``, and the policy refuses to be built above
        :data:`~novacontrol.planning.models.MAX_STEP_ATTEMPTS`, so this loop
        cannot be configured into an infinite one. Each pass either succeeds,
        or produces a RecoveryPlan that says what to do next: retry, run a
        different step, ask the user, escalate, or stop.
        """
        policy = retry_policy or self.retry_policy
        limit = (
            policy.max_attempts
            if max_attempts is None
            else min(max_attempts, policy.max_attempts)
        )
        limit = max(1, min(limit, MAX_STEP_ATTEMPTS))

        plans: list[RecoveryPlan] = []
        effective = step
        attempts = 0
        #: Tools already attempted. An alternative that sends the run back to
        #: a tool it has ALREADY failed with is not a different approach, it is
        #: the same one in a circle — and two built-ins can name each other, so
        #: this is what turns that pair into a stop rather than a bounce until
        #: the budget runs out.
        tried: set[str] = {step.tool} if step.tool else set()
        output: Mapping[str, Any] = {}
        verification: VerificationResult | None = None
        error: StepError | None = None

        while attempts < limit:
            attempts += 1
            try:
                produced = execute(effective)
                if isawaitable(produced):
                    produced = await produced
                output = dict(produced or {})
            except Exception as exc:  # noqa: BLE001 - classified, then recovered
                error = self.advisor.classify(effective, f"{type(exc).__name__}: {exc}")
                output = {}
            else:
                verification = await self._verify(effective, output, verify)
                if verification.passed:
                    return RecoveryRun(
                        step_id=step.id,
                        succeeded=True,
                        attempts=attempts,
                        executed=True,
                        verified=True,
                        output=output,
                        verification=verification,
                        plans=tuple(plans),
                        summary=(
                            f"{step.title} completed and was verified on attempt {attempts}."
                            if attempts > 1
                            else f"{step.title} completed and was verified."
                        ),
                    )
                if (
                    verification.status
                    in (VerificationStatus.INCONCLUSIVE, VerificationStatus.SKIPPED)
                    and self.verification_policy.allow_inconclusive
                ):
                    # It ran and nothing could check it (or nobody attached a
                    # check at all). Retrying would produce another run nobody
                    # can check, so the honest answer is "executed, unconfirmed"
                    # rather than a loop or a success — the same reading the
                    # plan executor gives an unverified step.
                    return RecoveryRun(
                        step_id=step.id,
                        succeeded=False,
                        attempts=attempts,
                        executed=True,
                        verified=False,
                        output=output,
                        verification=verification,
                        plans=tuple(plans),
                        summary=(
                            f"{step.title} ran, but nothing could confirm the result: "
                            f"{verification.reason} It is reported as unconfirmed, not as done."
                        ),
                    )
                error = StepError(
                    FailureKind.VERIFICATION_FAILED,
                    verification.reason or "The step's result did not match what was expected.",
                    retryable=True,
                    tool=effective.tool,
                )

            failure = error
            if failure is None:  # pragma: no cover - both branches above set it
                break
            plan = self.plan_recovery(
                effective, failure, attempts_made=attempts, retry_policy=policy
            )
            plans.append(plan)

            if plan.kind is RecoveryKind.ASK_USER:
                approved = await self._confirm(effective, confirm)
                if not approved:
                    return self._stopped(
                        step, attempts, output, verification, error, plans,
                        summary=(
                            f"{step.title} was not approved, so recovery stopped without "
                            "retrying it."
                        ),
                        requires_confirmation=True,
                    )
                # Approved: spend one attempt on the same step, and re-plan after
                # it, which is why this path only continues when budget remains.
                if attempts >= limit:
                    return self._stopped(
                        step, attempts, output, verification, error, plans,
                        summary=(
                            f"{step.title} was approved but the attempt budget is exhausted."
                        ),
                        requires_confirmation=False,
                    )
                continue

            if plan.kind is RecoveryKind.RETRY:
                continue

            if plan.kind is RecoveryKind.ALTERNATIVE and plan.step is not None:
                same_step = (
                    plan.step.tool == effective.tool
                    and plan.step.parameters == effective.parameters
                )
                already_tried = plan.step.tool in tried and plan.step.tool != effective.tool
                if same_step or already_tried:
                    # The alternative is the same step under a new name, or a
                    # tool this run has already failed with. Repeating either
                    # would be a retry wearing a disguise, so it is reported as
                    # "no different way" instead of being attempted.
                    return self._stopped(
                        step, attempts, output, verification, error, plans,
                        summary=(
                            f"{step.title} failed and no different way to try it was found: "
                            f"{plan.reason}"
                        ),
                    )
                tried.add(plan.step.tool)
                effective = plan.step
                continue

            if plan.kind is RecoveryKind.ESCALATE:
                return RecoveryRun(
                    step_id=step.id,
                    succeeded=False,
                    attempts=attempts,
                    executed=True,
                    output=output,
                    verification=verification,
                    plans=tuple(plans),
                    error=error,
                    summary=(
                        f"{step.title} needs reasoning this layer does not have: "
                        f"{error.message}"
                    ),
                )

            return self._stopped(
                step, attempts, output, verification, error, plans,
                summary=f"{step.title} failed and recovery stopped: {plan.reason}",
                stopped_safely=plan.stopped_safely,
            )

        return self._stopped(
            step, attempts, output, verification, error, plans,
            summary=(
                f"{step.title} failed after {attempts} attempt(s); the retry budget is "
                "exhausted, so it is reported rather than repeated."
            ),
        )

    async def _verify(
        self,
        step: PlanStep,
        output: Mapping[str, Any],
        verify: VerifyStep | None,
    ) -> VerificationResult:
        produced = self.verifier.verify(step, output) if verify is None else verify(step, output)
        if isawaitable(produced):
            produced = await produced
        return produced

    async def _confirm(
        self, step: PlanStep, confirm: Callable[[PlanStep], bool | Awaitable[bool]] | None
    ) -> bool:
        if confirm is None:
            return False
        answer = confirm(step)
        if isawaitable(answer):
            answer = await answer
        return bool(answer)

    @staticmethod
    def _stopped(
        step: PlanStep,
        attempts: int,
        output: Mapping[str, Any],
        verification: VerificationResult | None,
        error: StepError | None,
        plans: list[RecoveryPlan],
        *,
        summary: str,
        stopped_safely: bool = False,
        requires_confirmation: bool = False,
    ) -> RecoveryRun:
        return RecoveryRun(
            step_id=step.id,
            succeeded=False,
            attempts=attempts,
            executed=True,
            output=output,
            verification=verification,
            plans=tuple(plans),
            error=error,
            stopped_safely=stopped_safely,
            requires_confirmation=requires_confirmation,
            summary=summary,
        )


__all__ = [
    "AlternativeStrategy",
    "FailureAnalysis",
    "VerifyStep",
    "RecoveryEngine",
    "RecoveryKind",
    "RecoveryPlan",
    "RecoveryRun",
]
