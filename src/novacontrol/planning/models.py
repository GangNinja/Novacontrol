"""Planning domain models: the structured plan, and what became of it.

A plan is a GOAL plus ordered STEPS that say what to do, with what, and how the
result will be checked. The models here are deliberately data-only — they hold
no behaviour beyond validation and serialization — because three separate
components need to agree on this shape:

    the compiler    builds it from a decision and a goal;
    the executor    walks it in dependency order and publishes its state;
    the agent loop  reads it back to decide continue / retry / escalate.

Two vocabularies carry the safety rules, and they are the reason a step is not
just a string of text. ``StepEffect`` describes what a step WOULD do to the
world — which is what makes parallel execution safe or unsafe, what makes
verification required rather than optional, and what makes a retry forbidden
rather than merely unwise. ``FailureKind`` describes why a step failed, which is
what decides whether retrying can possibly help.

Nothing here executes anything, and nothing here assumes a step succeeded: a
step's ``status`` is only ever set from an observed outcome, and an outcome that
was never verified says so.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #


class PlanStatus(StrEnum):
    """What happened to the plan as a whole."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_CLARIFICATION = "needs_clarification"
    #: Could not run to a conclusion — a step was denied, or a dependency could
    #: not be satisfied. Distinct from FAILED: nothing was proven wrong, the
    #: plan simply never got permission to be right.
    BLOCKED = "blocked"


class PlanStepStatus(StrEnum):
    """What happened to one step. Only an observed outcome may set these."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    #: The approval layer refused the action. Not a failure — a decision.
    DENIED = "denied"
    #: It ran and reported success, but nothing verified the result.
    UNVERIFIED = "unverified"
    #: A dependency never completed, so this step was never attempted.
    BLOCKED = "blocked"


class StepEffect(StrEnum):
    """What a step does to the world — the input to every safety rule."""

    #: Reads, looks, locates, analyses. Safe to run concurrently, and the only
    #: kind of step that may declare itself parallel-safe.
    READ_ONLY = "read_only"
    #: Creates or modifies local state (files, windows, an open project).
    LOCAL_WRITE = "local_write"
    #: Deletes or overwrites something a person would miss. Never auto-retried.
    DESTRUCTIVE = "destructive"
    #: Leaves the machine: messages, purchases, uploads. Never auto-retried.
    EXTERNAL = "external"
    #: Changes machine settings or process control.
    SYSTEM = "system"


#: Effects whose actions are worth confirming before they run. The compiler
#: raises ``requires_confirmation`` for these, and the approval layer — not the
#: planner — is what ultimately decides whether they execute.
EFFECTS_NEEDING_CONFIRMATION: frozenset[StepEffect] = frozenset(
    {StepEffect.DESTRUCTIVE, StepEffect.EXTERNAL, StepEffect.SYSTEM}
)

#: Effects that must never be retried automatically, whatever the error said.
#: A delete that half-succeeded, run again, deletes something else.
EFFECTS_NEVER_RETRIED: frozenset[StepEffect] = frozenset(
    {StepEffect.DESTRUCTIVE, StepEffect.EXTERNAL}
)

#: Effects the default verification policy insists on checking.
EFFECTS_NEEDING_VERIFICATION: frozenset[StepEffect] = frozenset(
    {
        StepEffect.LOCAL_WRITE,
        StepEffect.DESTRUCTIVE,
        StepEffect.EXTERNAL,
        StepEffect.SYSTEM,
    }
)


class FailureKind(StrEnum):
    """WHY a step failed — the input to the recovery decision."""

    #: Timing, focus, a flaky network. Retrying is the whole remedy.
    TRANSIENT = "transient"
    #: The approval layer said no, or a permission is missing. A human decides
    #: this one; a retry loop cannot.
    PERMISSION_DENIED = "permission_denied"
    #: The step asked for something the tool will never accept as written.
    INVALID_PARAMETERS = "invalid_parameters"
    #: A prerequisite is missing (no such tool, no such file). Retrying cannot
    #: install it — this needs the model or a person.
    MISSING_DEPENDENCY = "missing_dependency"
    #: Nothing in this installation can carry the step out.
    UNSUPPORTED = "unsupported"
    #: The action reported success and the check disagreed.
    VERIFICATION_FAILED = "verification_failed"
    #: A destructive or external action failed. Never retried.
    UNSAFE = "unsafe"
    UNKNOWN = "unknown"


class VerificationMethod(StrEnum):
    """How a step's result is checked."""

    #: Nothing to check (read-only observations, human summaries).
    NONE = "none"
    FILE_EXISTS = "file_exists"
    PROCESS_RUNNING = "process_running"
    EXIT_CODE = "exit_code"
    OUTPUT_CONTAINS = "output_contains"
    ARTIFACT_EXISTS = "artifact_exists"
    STATE_OBSERVED = "state_observed"
    #: A callable registered with the verifier, named by ``target``.
    CALLABLE = "callable"
    #: Is the window/app actually open? "Open VS Code" is only done when the
    #: window can be observed, which a successful launch call does not prove.
    WINDOW_EXISTS = "window_exists"
    #: Can the endpoint be reached? For anything that leaves the machine.
    NETWORK_REACHABLE = "network_reachable"
    #: What the tool itself REPORTED, read from its output. The weakest evidence
    #: there is — a report is the claim under test — but it is the only thing
    #: available for a tool whose effect is not observable from here, and it is
    #: reported as a reported result rather than promoted to an observation.
    RESULT_REPORTED = "result_reported"


class VerificationStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    #: Nothing could be checked. Reported, never rounded up to PASS.
    INCONCLUSIVE = "inconclusive"
    SKIPPED = "skipped"


# --------------------------------------------------------------------------- #
# Policies
# --------------------------------------------------------------------------- #

#: A hard ceiling on attempts. Retry limits are configurable, but a
#: configuration mistake must not be able to create an infinite loop, so the
#: dataclass refuses to be built above this.
MAX_STEP_ATTEMPTS = 10

#: Failures worth trying again by default: transient trouble, and a plain
#: Unknown, because most one-off failures on this kind of machinery are timing.
DEFAULT_RETRYABLE: frozenset[FailureKind] = frozenset(
    {FailureKind.TRANSIENT, FailureKind.UNKNOWN, FailureKind.VERIFICATION_FAILED}
)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many times a step may run again, and which failures justify it.

    ``max_attempts`` counts TOTAL attempts, so the default of 1 is "run once,
    never retry" — the configuration that cannot surprise anyone. Retrying is
    permitted only for failure kinds that are worth retrying; the executor
    refuses outright for :data:`EFFECTS_NEVER_RETRIED`.
    """

    max_attempts: int = 1
    backoff_seconds: float = 0.0
    retryable: frozenset[FailureKind] = DEFAULT_RETRYABLE

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")
        if self.max_attempts > MAX_STEP_ATTEMPTS:
            raise ValueError(
                f"max_attempts must not exceed {MAX_STEP_ATTEMPTS}; "
                "a larger value risks an unbounded retry loop."
            )
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds must not be negative.")

    def may_retry(self, kind: FailureKind, *, attempts_made: int) -> bool:
        """Whether one more attempt is allowed for this failure."""
        return attempts_made < self.max_attempts and kind in self.retryable

    def attempts_left(self, *, attempts_made: int) -> bool:
        """Whether the budget allows another attempt at all.

        Separate from :meth:`may_retry` on purpose. ``retryable`` answers "is
        repeating this as-is worth it?" — a question a REPAIRED attempt has
        already answered by being different. What a repair needs is only the
        budget, which is this.
        """
        return attempts_made < self.max_attempts

    @property
    def attempts_allowed(self) -> int:
        return self.max_attempts

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "backoff_seconds": self.backoff_seconds,
            "retryable": sorted(kind.value for kind in self.retryable),
        }


@dataclass(frozen=True, slots=True)
class VerificationPolicy:
    """When a step's result must be checked rather than believed.

    ``required_effects`` is the default set: every action that changes something
    must be verified, while a read-only observation needs no proof. A step may
    name its own verification regardless — that is the "optional verification
    step" of the specification — and a policy that finds no way to check a
    required step reports INCONCLUSIVE rather than pretending.
    """

    required_effects: frozenset[StepEffect] = EFFECTS_NEEDING_VERIFICATION
    #: When False, an unverifiable required step counts as a failure instead of
    #: an honest unknown. Off by default: unknown is information.
    allow_inconclusive: bool = True

    def requires(self, effect: StepEffect) -> bool:
        return effect in self.required_effects

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_effects": sorted(effect.value for effect in self.required_effects),
            "allow_inconclusive": bool(self.allow_inconclusive),
        }


@dataclass(frozen=True, slots=True)
class VerificationSpec:
    """How THIS step will be checked, if it is to be checked at all."""

    method: VerificationMethod = VerificationMethod.NONE
    target: str = ""
    description: str = ""
    #: What the check expects — an exit code, a substring, a boolean. Left as
    #: Any because each method reads it differently, and a coerced guess here
    #: would be the verifier's decision made in the wrong place.
    expect: Any = None

    @property
    def required(self) -> bool:
        """Whether a check is attached (the specification's ``verification_required``)."""
        return self.method is not VerificationMethod.NONE

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method.value,
            "target": self.target,
            "description": self.description,
            "expect": _plain(self.expect),
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """What the check actually found. Never rounded up.

    ``expectation``/``observed`` are the original pair and are unchanged; the
    fields after ``evidence`` are the structured view of the same check, added
    so a caller that needs to REPORT (a UI row, an audit line, a recovery
    decision) does not have to parse prose back out of a reason string.

    ``success`` is an alias of ``passed``/``verified`` on purpose, and it is
    still only true for a definite PASS: an inconclusive result is a real
    outcome, and rounding it up is the one thing this class exists to prevent.
    """

    status: VerificationStatus
    method: VerificationMethod
    expectation: str = ""
    observed: str = ""
    reason: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    #: WHICH verifier produced this (a method name, a strategy name, a tool's
    #: own check). Empty for a result nobody has stamped, never guessed.
    verifier: str = ""
    #: What the check looked FOR, and what it FOUND, as values rather than as
    #: the sentence in ``expectation``/``observed``.
    expected_state: str = ""
    actual_state: str = ""
    #: How much the verdict may be relied on (0.0-1.0). An inconclusive result
    #: carries 0.0 rather than "medium": a check that could not be made has no
    #: confidence to report.
    confidence: float = 0.0
    #: The refusal text when the CHECK itself could not run (a probe that
    #: raised, a malformed expectation). Distinct from a failed check.
    error: str = ""
    #: Anything else the check learned, for the report — never for the verdict.
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status is VerificationStatus.PASS

    @property
    def success(self) -> bool:
        """The specification's name for :attr:`passed` — still only a PASS."""
        return self.status is VerificationStatus.PASS

    @property
    def verified(self) -> bool:
        """True only for a definite pass: inconclusive is not a pass."""
        return self.status is VerificationStatus.PASS

    def with_(self, **changes: Any) -> VerificationResult:
        from dataclasses import replace

        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "method": self.method.value,
            "expectation": self.expectation,
            "observed": self.observed,
            "reason": self.reason,
            "evidence": dict(self.evidence),
            "success": self.success,
            "verifier": self.verifier,
            "expected_state": self.expected_state,
            "actual_state": self.actual_state,
            "confidence": round(self.confidence, 3),
            "error": self.error,
            "metadata": dict(self.metadata),
        }


# --------------------------------------------------------------------------- #
# Outcomes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StepError:
    """A structured failure: what went wrong, and whether retrying can help."""

    kind: FailureKind
    message: str
    retryable: bool = False
    tool: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "message": self.message,
            "retryable": self.retryable,
            "tool": self.tool,
        }


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """One step's observed result, with its verification and its retries.

    ``verified`` is the only field that claims success *with evidence*: an
    outcome whose verification never ran is reported as unverified, which is a
    different thing from a failure and a very different thing from a success.
    """

    step_id: str
    status: PlanStepStatus
    summary: str = ""
    output: Mapping[str, Any] = field(default_factory=dict)
    error: StepError | None = None
    verification: VerificationResult | None = None
    attempts: int = 1
    latency_ms: float = 0.0
    #: Recovery actions taken on this step, in order (e.g. ``("retry",)``).
    recovery: tuple[str, ...] = ()
    #: Which tool ran it. Empty when nothing was dispatched.
    tool: str = ""

    @property
    def verified(self) -> bool:
        return self.verification is not None and self.verification.verified

    @property
    def executed(self) -> bool:
        """Whether an executor was actually behind this step."""
        return self.status not in (PlanStepStatus.SKIPPED, PlanStepStatus.BLOCKED)

    @property
    def verification_result(self) -> str:
        """The check's verdict in one word, including "no check ran".

        The specification asks a step to expose ``verification_required``,
        ``verification_method`` and ``verification_result``. The first two live
        on the step; this is the third, and it borrows the verifier's own
        vocabulary (``pass``/``fail``/``inconclusive``/``skipped``) rather than
        inventing a second one. ``not_run`` is deliberately NOT a pass: a step
        nobody checked has no result, which is a different claim from a good
        one.
        """
        return self.verification.status.value if self.verification else "not_run"

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "status": self.status.value,
            "summary": self.summary,
            "output": dict(self.output),
            "error": self.error.to_dict() if self.error else None,
            "verification": self.verification.to_dict() if self.verification else None,
            "verification_result": self.verification_result,
            "verified": self.verified,
            "attempts": self.attempts,
            "latency_ms": round(self.latency_ms, 3),
            "recovery": list(self.recovery),
            "tool": self.tool,
        }


# --------------------------------------------------------------------------- #
# Steps and plans
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One executable step.

    The first five fields are the original plan shape and are unchanged; the
    rest are what the specification asks a step to carry — the action, the tool
    or capability that performs it, its parameters, what a good result looks
    like, how that will be checked, and what it does to the world.
    """

    title: str
    description: str
    depends_on: tuple[str, ...] = ()
    assigned_role: str | None = None
    id: str = field(default_factory=lambda: "")
    #: The verb, machine-readable: ``open_application``, ``run_tests``.
    action: str = ""
    #: The tool or capability that would carry it out. Empty means "nothing
    #: registered for this yet", which the executor reports rather than guesses.
    tool: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)
    #: What a successful result looks like, in words a person can check.
    expected_result: str = ""
    verification: VerificationSpec = field(default_factory=VerificationSpec)
    effect: StepEffect = StepEffect.READ_ONLY
    #: Only a READ_ONLY step may declare this; writes are never run in
    #: parallel, because two steps that both change the same thing conflict
    #: whether or not they were planned together.
    parallel_safe: bool = False
    status: PlanStepStatus = PlanStepStatus.PENDING

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("Plan step title is required.")
        if not self.description.strip():
            raise ValueError("Plan step description is required.")
        if self.parallel_safe and self.effect is not StepEffect.READ_ONLY:
            raise ValueError(
                f"Step {self.title!r} is {self.effect.value} and cannot be parallel_safe: "
                "only read-only steps may run concurrently."
            )
        if self.id == "":
            object.__setattr__(self, "id", _slug(self.title))

    @property
    def step_id(self) -> str:
        """The specification's name for ``id``."""
        return self.id

    @property
    def verification_required(self) -> bool:
        return self.verification.required

    @property
    def needs_confirmation(self) -> bool:
        return self.effect in EFFECTS_NEEDING_CONFIRMATION

    def with_(self, **changes: Any) -> PlanStep:
        from dataclasses import replace

        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.id,
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "action": self.action,
            "tool": self.tool,
            "parameters": {key: _plain(value) for key, value in self.parameters.items()},
            "depends_on": list(self.depends_on),
            "dependencies": list(self.depends_on),
            "assigned_role": self.assigned_role,
            "expected_result": self.expected_result,
            "effect": self.effect.value,
            "parallel_safe": self.parallel_safe,
            "requires_confirmation": self.needs_confirmation,
            "verification_required": self.verification_required,
            "verification_method": self.verification.method.value,
            "verification": self.verification.to_dict(),
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class Plan:
    """A goal, the steps that reach it, and the rules they run under.

    ``dependencies`` is DERIVED from the steps (see the property) so the graph
    cannot disagree with the steps it came from, and ``state`` is the executor's
    published progress — the plan and its live state travel together, which is
    what lets a caller resume, resume-explain or render a partially-run plan.
    """

    goal: str
    steps: tuple[PlanStep, ...]
    needs_clarification: bool = False
    id: str = field(default_factory=lambda: uuid4().hex)
    status: PlanStatus = PlanStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: ``None`` means "inherit the executor's policy", which is what a hand-built
    #: plan should do. A COMPILED plan states its policies explicitly, so a plan
    #: that cares says so in its own data rather than relying on a default that
    #: is indistinguishable from a choice.
    retry_policy: RetryPolicy | None = None
    verification_policy: VerificationPolicy | None = None
    #: Published execution state (per-step status, attempts, verification).
    state: Mapping[str, Any] = field(default_factory=dict)
    #: The decision this plan was compiled from, when there was one.
    decision: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Refuse a plan whose steps cannot be told apart.

        Two steps with the same id are not two steps as far as execution is
        concerned: one outcome overwrites the other, and the plan can report
        COMPLETED while a step it listed was never accounted for. Catching that
        at construction makes the defect unrepresentable instead of silent.
        """
        seen: set[str] = set()
        duplicates: list[str] = []
        for step in self.steps:
            if step.id in seen and step.id not in duplicates:
                duplicates.append(step.id)
            seen.add(step.id)
        if duplicates:
            raise ValueError(
                "Plan step ids must be unique; repeated: "
                + ", ".join(sorted(duplicates))
                + ". Two steps sharing an id would share one outcome."
            )

    @property
    def dependencies(self) -> tuple[tuple[str, str], ...]:
        """The graph edges as ``(step_id, depends_on_id)`` pairs."""
        return tuple(
            (step.id, dependency) for step in self.steps for dependency in step.depends_on
        )

    @property
    def parallel_groups(self) -> tuple[tuple[str, ...], ...]:
        """Steps that may share a wave: the same dependency depth, all read-only.

        Reported for inspection — the executor computes the same thing while
        scheduling, and this is what makes a surprising parallel wave visible.
        """
        depth: dict[str, int] = {}
        by_id = {step.id: step for step in self.steps}
        for step in self.steps:
            _depth(step, by_id, depth, ())
        groups: dict[int, list[str]] = {}
        for step in self.steps:
            if not step.parallel_safe:
                continue
            groups.setdefault(depth.get(step.id, 0), []).append(step.id)
        return tuple(
            tuple(groups[level]) for level in sorted(groups) if len(groups[level]) > 1
        )

    def with_(self, **changes: Any) -> Plan:
        from dataclasses import replace

        return replace(self, **changes)

    def with_state(self, state: Mapping[str, Any], *, status: PlanStatus | None = None) -> Plan:
        changes: dict[str, Any] = {"state": dict(state)}
        if status is not None:
            changes["status"] = status
        return self.with_(**changes)

    def step_for(self, step_id: str) -> PlanStep | None:
        for step in self.steps:
            if step.id == step_id:
                return step
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "status": self.status.value,
            "needs_clarification": self.needs_clarification,
            "steps": [step.to_dict() for step in self.steps],
            "dependencies": [list(edge) for edge in self.dependencies],
            "parallel_groups": [list(group) for group in self.parallel_groups],
            "retry_policy": (
                self.retry_policy.to_dict() if self.retry_policy else {"inherited": True}
            ),
            "verification_policy": (
                self.verification_policy.to_dict()
                if self.verification_policy
                else {"inherited": True}
            ),
            "state": dict(self.state),
            "decision": dict(self.decision),
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class WorkflowResult:
    """What executing a plan produced, including what was never checked.

    ``executed`` and ``verified`` exist so that "the workflow completed" cannot
    be read as "the work was done": a plan walked with no executor bound
    completes with ``executed=False``, and a step that ran without a check
    leaves ``verified=False``. Both are reported, never inferred.
    """

    plan_id: str
    status: PlanStatus
    step_outputs: dict[str, dict[str, Any]]
    errors: dict[str, str] = field(default_factory=dict)
    step_results: dict[str, StepOutcome] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)
    recovery: dict[str, tuple[str, ...]] = field(default_factory=dict)
    executed: bool = False
    verified: bool = False
    unverified_steps: tuple[str, ...] = ()
    summary: str = ""

    def state(self) -> dict[str, Any]:
        """The plan state this result publishes."""
        return {
            "steps": {
                step_id: {
                    "status": outcome.status.value,
                    "attempts": outcome.attempts,
                    "verified": outcome.verified,
                    "tool": outcome.tool,
                    "latency_ms": round(outcome.latency_ms, 3),
                    "error": outcome.error.to_dict() if outcome.error else None,
                    "verification": outcome.verification.to_dict()
                    if outcome.verification
                    else None,
                    "verification_result": outcome.verification_result,
                    "recovery": list(outcome.recovery),
                }
                for step_id, outcome in self.step_results.items()
            },
            "executed": self.executed,
            "verified": self.verified,
            "unverified_steps": list(self.unverified_steps),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "status": self.status.value,
            "step_outputs": {key: dict(value) for key, value in self.step_outputs.items()},
            "errors": dict(self.errors),
            "attempts": dict(self.attempts),
            "recovery": {key: list(value) for key, value in self.recovery.items()},
            "executed": self.executed,
            "verified": self.verified,
            "unverified_steps": list(self.unverified_steps),
            "summary": self.summary,
            "state": self.state(),
            "steps": [outcome.to_dict() for outcome in self.step_results.values()],
        }


def _depth(
    step: PlanStep,
    by_id: Mapping[str, PlanStep],
    depth: dict[str, int],
    seen: tuple[str, ...],
) -> int:
    """Dependency depth of a step, tolerant of a cycle (which cannot be run)."""
    if step.id in depth:
        return depth[step.id]
    if step.id in seen:
        return 0
    parents = [by_id[parent] for parent in step.depends_on if parent in by_id]
    level = (
        0
        if not parents
        else 1 + max(_depth(parent, by_id, depth, seen + (step.id,)) for parent in parents)
    )
    depth[step.id] = level
    return level


def _plain(value: Any) -> Any:
    """Make a value safe to serialize without dropping information."""
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _slug(value: str) -> str:
    slug = "-".join(part for part in value.strip().lower().split() if part)
    return slug or uuid4().hex
