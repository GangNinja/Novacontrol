"""The embodied agent's contracts — observations, actions, sessions, trajectories.

Phase 25 adds one capability the earlier phases do not have: acting INSIDE an
environment that answers back. Everything here is built to keep four things
apart, because collapsing them is how an embodied agent lies to its operator:

1. **What the environment actually said** (``structured`` observation, taken
   from an interface the adapter owns) versus what a screenshot SUGGESTS
   (``visual``) versus what a model GUESSED (``predicted``) versus what nobody
   knows (``unknown``). :class:`ObservationBasis` names them, and only
   ``STRUCTURED`` facts are treated as measurements.
2. **What was asked for** (an :class:`EnvironmentAction` with its declared
   preconditions and expected effect) versus what actually happened (the
   :class:`ActionResult`, and the FRESH observation taken afterwards).
3. **The objective** (``Objective``) versus the session's own progress
   (:class:`SessionState`), because "the agent is still working" and "the goal
   is met" are different claims.
4. **A bounded input** (every action carries a duration ceiling and a release
   obligation) versus an input that is held and forgotten.

The vocabulary is deliberately small: it is the vocabulary of
observe → decide → act → verify → remember, and nothing else.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

#: The phase this module belongs to, and the schema version of its records.
PHASE = "25"
SCHEMA_VERSION = "phase25.1"


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _as_mapping(value: Any) -> dict[str, Any]:
    return {str(key): item for key, item in dict(value or {}).items()}


def _as_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    return tuple(str(item) for item in (value or ()) if str(item).strip())


def _as_count(value: Any) -> int:
    """A stored count, read back without inventing one.

    A missing or unreadable count is ``0`` (nothing was counted), and a negative
    one cannot be a count, so it is clamped rather than carried.
    """
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _as_figure(value: Any) -> float | None:
    """A stored MEASURED figure — ``None`` stays ``None``, never ``0.0``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class EnvironmentKind(StrEnum):
    """What an adapter actually drives.

    The kinds exist so a status surface can say WHICH claim it is making. A
    ``SIMULATION`` is a real environment even though no game is running: it is
    deterministic, owned by this build, and answers through a real interface.
    ``GAME`` is reserved for an adapter talking to a real game's supported
    interface, and ``SCREEN`` for one driving a window by input alone.
    """

    SIMULATION = "simulation"
    GAME = "game"
    SCREEN = "screen"


class EnvironmentStatus(StrEnum):
    """Whether an adapter can be used — the honest four-way answer.

    ``UNSUPPORTED`` — this build has no adapter for it at all.
    ``UNAVAILABLE`` — an adapter exists but cannot reach its environment now.
    ``MOCK`` — an adapter exists for tests only; never claimed as working.
    ``OPERATIONAL`` — connected and answering through its real interface.
    """

    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    MOCK = "mock"
    OPERATIONAL = "operational"


class ObservationBasis(StrEnum):
    """WHERE a fact about the environment came from — never rounded up."""

    #: Read from the environment's own interface. A measurement.
    STRUCTURED = "structured"
    #: Derived from a screenshot or other pixels. An inference.
    VISUAL = "visual"
    #: Produced by a model projecting forward. A guess, labelled as one.
    PREDICTED = "predicted"
    #: Nothing supports it. Kept as a first-class value rather than dropped.
    UNKNOWN = "unknown"


class ObservationFreshness(StrEnum):
    """How much the observation can be leaned on right now."""

    FRESH = "fresh"
    #: Older than the adapter's own freshness policy. Usable to REPORT, not to
    #: authorize a state-dependent action.
    STALE = "stale"
    #: The adapter could not read the environment at all this time.
    FAILED = "failed"


class ActionKind(StrEnum):
    """The bounded input vocabulary every adapter must answer for."""

    #: Move within the environment (a direction, a cell, a waypoint).
    MOVE = "move"
    #: Interact with something already located (an object, an item, a control).
    INTERACT = "interact"
    #: Advance time without doing anything else; lets the state settle.
    WAIT = "wait"
    #: Take a fresh observation — an action with no side effect for the agent.
    OBSERVE = "observe"
    #: Release anything currently held. Always legal, never refused.
    RELEASE = "release"


class ActionStatus(StrEnum):
    """What sending an input actually did.

    ``REJECTED`` is the adapter REFUSING (an unsupported input, a policy
    refusal) and ``FAILED`` an input that was accepted but errored — the
    distinction matters because one is a bug in the agent and the other in the
    environment.
    """

    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class SessionStatus(StrEnum):
    """A session's life cycle — one value at a time, and each means something."""

    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    #: The objective's own conditions were observed holding.
    COMPLETED = "completed"
    #: The agent ran out of a stated budget while the objective was unmet.
    BUDGET_EXHAUSTED = "budget_exhausted"
    #: The controller or the environment failed in a way the agent could not
    #: recover from inside its budgets.
    ENVIRONMENT_FAILED = "environment_failed"
    #: The agent itself errored (a defect, not an outcome).
    AGENT_ERROR = "agent_error"
    #: The permission or approval layer refused an attempt, so nothing was sent
    #: and nothing else will be: a refusal is an answer, not a speed bump to
    #: retry through.
    DENIED = "denied"
    #: The agent has nothing left it can attempt from a fresh reading — every
    #: route from here is known blocked. Reported as its own ending rather than
    #: as an exhausted budget, because no budget ran out: the environment did.
    STUCK = "stuck"
    CANCELLED = "cancelled"
    #: Cancelled by a restart rather than by a person, and NOT resumable until
    #: the environment and the session have both been revalidated.
    INTERRUPTED = "interrupted"


#: Statuses a session can be restarted from without re-planning a live input:
#: everything else is either finished or needs revalidation first.
RESUMABLE_STATUSES: frozenset[SessionStatus] = frozenset(
    {SessionStatus.CREATED, SessionStatus.PAUSED, SessionStatus.RUNNING}
)


@dataclass(frozen=True, slots=True)
class EnvironmentCapability:
    """One thing an adapter can do, and how well it was verified.

    ``status`` is the honest four-way :class:`EnvironmentStatus`; ``verified``
    says whether it was exercised end to end. An adapter may declare a
    capability it has never run — it just may not call that WORKING.
    """

    name: str
    description: str
    status: EnvironmentStatus = EnvironmentStatus.UNAVAILABLE
    verified: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "status": self.status.value,
            "verified": self.verified,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class EnvironmentInfo:
    """What an adapter says about itself — identity and the honest status."""

    environment_id: str
    name: str
    kind: EnvironmentKind = EnvironmentKind.SIMULATION
    status: EnvironmentStatus = EnvironmentStatus.UNAVAILABLE
    reason: str = ""
    #: An environment a person explicitly allowed. Only allowlisted
    #: environments may be driven; the registry refuses the rest by name.
    allowed: bool = False
    actions: tuple[str, ...] = ()
    observations: tuple[str, ...] = ()
    capabilities: tuple[EnvironmentCapability, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment_id": self.environment_id,
            "name": self.name,
            "kind": self.kind.value,
            "status": self.status.value,
            "reason": self.reason,
            "allowed": self.allowed,
            "actions": list(self.actions),
            "observations": list(self.observations),
            "capabilities": [item.to_dict() for item in self.capabilities],
        }


@dataclass(frozen=True, slots=True)
class EntityRead:
    """One thing the observation mentions, with its support.

    ``label`` may be EMPTY on purpose: a region detected in a screenshot with no
    semantic identity is reported as a region with a position and a confidence,
    and never given a name the evidence does not support. ``basis`` records
    where it came from; ``confidence`` is ``None`` when nothing measured it.
    """

    entity_id: str = ""
    label: str = ""
    kind: str = ""
    position: tuple[float, float] | None = None
    bbox: tuple[float, float, float, float] | None = None
    confidence: float | None = None
    basis: ObservationBasis = ObservationBasis.UNKNOWN
    attributes: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()

    @property
    def named(self) -> bool:
        """Whether the evidence supports calling this a thing at all."""
        return bool(self.label.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "label": self.label,
            "kind": self.kind,
            "position": list(self.position) if self.position is not None else None,
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "confidence": self.confidence,
            "basis": self.basis.value,
            "attributes": dict(self.attributes),
            "evidence": list(self.evidence),
            "named": self.named,
        }


@dataclass(frozen=True, slots=True)
class Observation:
    """One reading of the environment — with provenance and freshness attached.

    ``structured`` is the adapter's own state, when it has one. It is the only
    part a state-dependent decision may lean on; ``entities`` from vision carry
    ``basis=VISUAL`` and are reported, not acted on as if measured.
    """

    observation_id: str = field(default_factory=lambda: _new_id("obs"))
    session_id: str = ""
    environment_id: str = ""
    taken_at: str = ""
    basis: ObservationBasis = ObservationBasis.UNKNOWN
    freshness: ObservationFreshness = ObservationFreshness.FRESH
    age_ms: float = 0.0
    #: The adapter's own state, JSON-safe. Empty when the adapter has none.
    structured: Mapping[str, Any] = field(default_factory=dict)
    entities: tuple[EntityRead, ...] = ()
    changes: tuple[str, ...] = ()
    objective_progress: Mapping[str, Any] = field(default_factory=dict)
    confidence: float | None = None
    reason: str = ""
    #: Where this came from — a session, an adapter, a fixture.
    provenance: str = ""
    evidence: tuple[str, ...] = ()

    @property
    def usable_for_decision(self) -> bool:
        """Whether a state-dependent action may be based on this reading.

        Stale or failed readings can still be REPORTED — refusing to show them
        would hide what the agent saw — but they must not authorize an action.
        """
        return self.freshness is ObservationFreshness.FRESH

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "session_id": self.session_id,
            "environment_id": self.environment_id,
            "taken_at": self.taken_at,
            "basis": self.basis.value,
            "freshness": self.freshness.value,
            "age_ms": round(self.age_ms, 3),
            "structured": dict(self.structured),
            "entities": [item.to_dict() for item in self.entities],
            "changes": list(self.changes),
            "objective_progress": dict(self.objective_progress),
            "confidence": self.confidence,
            "reason": self.reason,
            "provenance": self.provenance,
            "evidence": list(self.evidence),
            "usable_for_decision": self.usable_for_decision,
        }


@dataclass(frozen=True, slots=True)
class EnvironmentAction:
    """One bounded input, with everything needed to check it afterwards.

    ``reversible`` and ``destructive`` travel with the action because they
    decide whether a retry is safe: an input that cannot be undone is never
    re-sent on a timeout, because the first one may already have landed.
    """

    action_id: str = field(default_factory=lambda: _new_id("act"))
    session_id: str = ""
    kind: ActionKind = ActionKind.WAIT
    #: The adapter's own input name — ``move``, ``interact``, ``wait``.
    input_name: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    timeout_ms: int = 5000
    preconditions: tuple[str, ...] = ()
    #: What the agent expects to observe afterwards, in words it can check.
    expected_effect: str = ""
    #: The facts the action CLAIMS it will make true (``key=value``), so a
    #: dependent step can be ordered against them.
    effects: tuple[str, ...] = ()
    effect: str = "read_only"
    permission: str = ""
    reversible: bool = True
    destructive: bool = False
    release_after: bool = True
    rationale: str = ""
    attempt: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "session_id": self.session_id,
            "kind": self.kind.value,
            "input_name": self.input_name,
            "parameters": dict(self.parameters),
            "duration_ms": self.duration_ms,
            "timeout_ms": self.timeout_ms,
            "preconditions": list(self.preconditions),
            "expected_effect": self.expected_effect,
            "effects": list(self.effects),
            "effect": self.effect,
            "permission": self.permission,
            "reversible": self.reversible,
            "destructive": self.destructive,
            "release_after": self.release_after,
            "rationale": self.rationale,
            "attempt": self.attempt,
        }


@dataclass(frozen=True, slots=True)
class ActionResult:
    """What sending the input actually did, and how it was verified."""

    action_id: str = ""
    session_id: str = ""
    status: ActionStatus = ActionStatus.FAILED
    input_name: str = ""
    detail: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_ms: float = 0.0
    #: What the agent observed AFTER the input, when the adapter could read it.
    observed: Observation | None = None
    #: Whether the expected effect was actually seen. ``None`` means the check
    #: could not be made — which is not the same as False and never a success.
    verified: bool | None = None
    verification: str = ""
    error: str = ""
    released: bool = True
    evidence: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status is ActionStatus.COMPLETED

    @property
    def confirmed(self) -> bool:
        """Completed AND the expected change was observed."""
        return self.ok and self.verified is True

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "session_id": self.session_id,
            "status": self.status.value,
            "input_name": self.input_name,
            "detail": self.detail,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": round(self.duration_ms, 3),
            "observed": self.observed.to_dict() if self.observed is not None else None,
            "verified": self.verified,
            "verification": self.verification,
            "error": self.error,
            "released": self.released,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class EmbodiedActionResult:
    """One sent input, in the shape the SHARED automation audit trail takes.

    The desktop, browser and phone controllers each hand their audit log a
    result with these five fields (``AuditableResult``), and an environment
    input is the same kind of thing: one consequential action a person may have
    to account for. Declaring them here rather than inventing a second row shape
    is what keeps "what did automation do" answerable from one place.
    """

    action_id: str
    status: ActionStatus
    output: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
    approval_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "status": self.status.value,
            "output": dict(self.output),
            "error": self.error,
            "approval_id": self.approval_id,
        }


@dataclass(frozen=True, slots=True)
class Objective:
    """What the session is trying to achieve, with a checkable finish line.

    A goal with no measurable completion criterion is refused by
    :func:`validate_objective` rather than run until the budget dies: "get
    better at the game" is not an objective this phase can verify.
    """

    objective_id: str = field(default_factory=lambda: _new_id("obj"))
    request: str = ""
    #: The terminal condition as ``key=value`` facts the agent must observe.
    success_conditions: tuple[str, ...] = ()
    initial_conditions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    failure_conditions: tuple[str, ...] = ()
    cancellation_conditions: tuple[str, ...] = ()
    max_actions: int = 40
    max_seconds: float = 120.0
    max_replans: int = 3
    max_input_ms: int = 500

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective_id": self.objective_id,
            "request": self.request,
            "success_conditions": list(self.success_conditions),
            "initial_conditions": list(self.initial_conditions),
            "constraints": list(self.constraints),
            "failure_conditions": list(self.failure_conditions),
            "cancellation_conditions": list(self.cancellation_conditions),
            "max_actions": self.max_actions,
            "max_seconds": self.max_seconds,
            "max_replans": self.max_replans,
            "max_input_ms": self.max_input_ms,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Objective:
        payload = dict(data or {})
        return cls(
            objective_id=str(payload.get("objective_id") or _new_id("obj")),
            request=str(payload.get("request") or "").strip(),
            success_conditions=_as_tuple(payload.get("success_conditions")),
            initial_conditions=_as_tuple(payload.get("initial_conditions")),
            constraints=_as_tuple(payload.get("constraints")),
            failure_conditions=_as_tuple(payload.get("failure_conditions")),
            cancellation_conditions=_as_tuple(payload.get("cancellation_conditions")),
            max_actions=max(1, int(payload.get("max_actions") or 40)),
            max_seconds=max(0.1, float(payload.get("max_seconds") or 120.0)),
            max_replans=max(0, int(payload.get("max_replans") or 3)),
            max_input_ms=max(0, int(payload.get("max_input_ms") or 500)),
        )


@dataclass(frozen=True, slots=True)
class ObjectiveVerdict:
    """Whether the objective's conditions were OBSERVED holding.

    ``met`` is only true when a fresh observation confirmed every condition.
    ``reason`` names what was still missing otherwise — an unmet objective
    reports why, it does not report ``False`` and stop talking.
    """

    met: bool = False
    verified: bool = False
    reason: str = ""
    missing: tuple[str, ...] = ()
    observed: tuple[str, ...] = ()
    observation_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "met": self.met,
            "verified": self.verified,
            "reason": self.reason,
            "missing": list(self.missing),
            "observed": list(self.observed),
            "observation_id": self.observation_id,
        }


@dataclass(frozen=True, slots=True)
class TrajectoryEntry:
    """One step of a session's story, as OPERATIONAL metadata.

    No prompt, no reasoning trace and no frame body: an entry names the action,
    where it came from, what was expected and what was seen. Screenshots are
    referenced by id, never embedded, and never kept past the session.
    """

    index: int = 0
    at: str = ""
    phase: str = ""
    summary: str = ""
    action_id: str = ""
    action: str = ""
    expected: str = ""
    observed: str = ""
    status: str = ""
    verified: bool | None = None
    basis: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "at": self.at,
            "phase": self.phase,
            "summary": self.summary,
            "action_id": self.action_id,
            "action": self.action,
            "expected": self.expected,
            "observed": self.observed,
            "status": self.status,
            "verified": self.verified,
            "basis": self.basis,
            "detail": {str(key): value for key, value in dict(self.detail).items()},
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class Evaluation:
    """What the session's own record says about it — the honest scoreboard.

    Every rate is ``None`` when nothing measured it, and every count is a
    count. ``terminal`` distinguishes an objective that failed from an agent
    that crashed from an environment that broke: three different outcomes that
    a single ``success`` boolean flattens into one.

    What each counter counts, since the difference between them is the whole
    point: ``actions_attempted`` is every decision the loop tried to carry out
    (it can exceed the inputs sent, because an attempt may be refused before it
    reaches the door); ``actions_rejected`` is an attempt a layer REFUSED — the
    environment refused the input, or the action policy refused the step — and a
    refusal ends the session rather than being retried through;
    ``actions_failed`` is an attempt that was accepted and did not work;
    ``invalid_actions`` is an attempt that was not a well-formed action at all
    (the door validates before it sends, so the shipping path keeps this at 0);
    ``verification_failures`` counts attempts whose effect could not be
    confirmed, and ``input_failures`` the inputs that never reported back.
    """

    session_id: str = ""
    objective_met: bool = False
    verified_completion: bool = False
    terminal: str = ""
    actions_attempted: int = 0
    actions_completed: int = 0
    actions_rejected: int = 0
    actions_failed: int = 0
    invalid_actions: int = 0
    input_failures: int = 0
    observations: int = 0
    stale_observations: int = 0
    recoveries: int = 0
    recoveries_succeeded: int = 0
    replans: int = 0
    repeated_actions: int = 0
    verification_failures: int = 0
    safety_violations: int = 0
    seconds: float | None = None
    observation_freshness_ms: float | None = None
    progress: float | None = None

    @property
    def objective_completion_rate(self) -> float | None:
        """1.0 or 0.0 — or ``None`` when no objective was ever evaluated."""
        if not self.terminal:
            return None
        return 1.0 if self.verified_completion else 0.0

    @property
    def actions_per_success(self) -> float | None:
        """How many inputs one verified objective cost; ``None`` if none."""
        if not self.verified_completion:
            return None
        return float(self.actions_attempted)

    @property
    def recovery_success_rate(self) -> float | None:
        if not self.recoveries:
            return None
        return self.recoveries_succeeded / self.recoveries

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "objective_met": self.objective_met,
            "verified_completion": self.verified_completion,
            "terminal": self.terminal,
            "actions_attempted": self.actions_attempted,
            "actions_completed": self.actions_completed,
            "actions_rejected": self.actions_rejected,
            "actions_failed": self.actions_failed,
            "invalid_actions": self.invalid_actions,
            "input_failures": self.input_failures,
            "observations": self.observations,
            "stale_observations": self.stale_observations,
            "recoveries": self.recoveries,
            "recoveries_succeeded": self.recoveries_succeeded,
            "replans": self.replans,
            "repeated_actions": self.repeated_actions,
            "verification_failures": self.verification_failures,
            "safety_violations": self.safety_violations,
            "seconds": self.seconds,
            "observation_freshness_ms": self.observation_freshness_ms,
            "progress": self.progress,
            "objective_completion_rate": self.objective_completion_rate,
            "actions_per_success": self.actions_per_success,
            "recovery_success_rate": self.recovery_success_rate,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Evaluation:
        """Read back what :meth:`to_dict` wrote — the counts, not the derived rates.

        The three rates are properties over these counts, so they are
        deliberately NOT read back: restoring a stored rate would let a figure
        drift away from the counts it is supposed to be computed from. Every
        unmeasured figure stays ``None``; a count that is missing reads ``0``,
        because a count that was never incremented is zero rather than unknown.
        """
        payload = dict(data or {})
        return cls(
            session_id=str(payload.get("session_id") or ""),
            objective_met=bool(payload.get("objective_met")),
            verified_completion=bool(payload.get("verified_completion")),
            terminal=str(payload.get("terminal") or ""),
            actions_attempted=_as_count(payload.get("actions_attempted")),
            actions_completed=_as_count(payload.get("actions_completed")),
            actions_rejected=_as_count(payload.get("actions_rejected")),
            actions_failed=_as_count(payload.get("actions_failed")),
            invalid_actions=_as_count(payload.get("invalid_actions")),
            input_failures=_as_count(payload.get("input_failures")),
            observations=_as_count(payload.get("observations")),
            stale_observations=_as_count(payload.get("stale_observations")),
            recoveries=_as_count(payload.get("recoveries")),
            recoveries_succeeded=_as_count(payload.get("recoveries_succeeded")),
            replans=_as_count(payload.get("replans")),
            repeated_actions=_as_count(payload.get("repeated_actions")),
            verification_failures=_as_count(payload.get("verification_failures")),
            safety_violations=_as_count(payload.get("safety_violations")),
            seconds=_as_figure(payload.get("seconds")),
            observation_freshness_ms=_as_figure(payload.get("observation_freshness_ms")),
            progress=_as_figure(payload.get("progress")),
        )


@dataclass(frozen=True, slots=True)
class SessionState:
    """A session's whole record — what a status surface may publish.

    The record is deliberately bounded: the last observation, the last action
    and its result, the entries the trajectory already holds, and the counts.
    A session never publishes a frame body, a prompt or a reasoning trace.
    """

    session_id: str = field(default_factory=lambda: _new_id("sess"))
    environment_id: str = ""
    #: The named configuration the environment was started with ("open-east",
    #: "drifting"). Reported because a session in a faulted world is a different
    #: session, and a reader must not have to infer that from the counters.
    scenario: str = ""
    status: SessionStatus = SessionStatus.CREATED
    reason: str = ""
    created_at: str = ""
    updated_at: str = ""
    objective: Objective = field(default_factory=Objective)
    verdict: ObjectiveVerdict = field(default_factory=ObjectiveVerdict)
    last_observation: Observation | None = None
    last_action: EnvironmentAction | None = None
    last_result: ActionResult | None = None
    goal_id: str = ""
    actions_taken: int = 0
    observations_taken: int = 0
    replans_used: int = 0
    recoveries: int = 0
    trajectory: tuple[TrajectoryEntry, ...] = ()
    evaluation: Evaluation = field(default_factory=Evaluation)
    #: True when the session was restored after a restart and has NOT yet been
    #: revalidated against a live environment. No input may be sent until it is.
    needs_revalidation: bool = False
    released: bool = True
    started_at: str = ""
    finished_at: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in {
            SessionStatus.COMPLETED,
            SessionStatus.BUDGET_EXHAUSTED,
            SessionStatus.ENVIRONMENT_FAILED,
            SessionStatus.AGENT_ERROR,
            SessionStatus.DENIED,
            SessionStatus.STUCK,
            SessionStatus.CANCELLED,
        }

    def with_(self, **changes: Any) -> SessionState:
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "environment_id": self.environment_id,
            "scenario": self.scenario,
            "status": self.status.value,
            "reason": self.reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "objective": self.objective.to_dict(),
            "verdict": self.verdict.to_dict(),
            "last_observation": (
                self.last_observation.to_dict() if self.last_observation is not None else None
            ),
            "last_action": (self.last_action.to_dict() if self.last_action is not None else None),
            "last_result": (self.last_result.to_dict() if self.last_result is not None else None),
            "goal_id": self.goal_id,
            "actions_taken": self.actions_taken,
            "observations_taken": self.observations_taken,
            "replans_used": self.replans_used,
            "recoveries": self.recoveries,
            "trajectory": [item.to_dict() for item in self.trajectory],
            "evaluation": self.evaluation.to_dict(),
            "needs_revalidation": self.needs_revalidation,
            "released": self.released,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "terminal": self.terminal,
        }


class ObjectiveInvalid(ValueError):
    """An objective that cannot be verified — refused before anything runs."""


def validate_objective(objective: Objective) -> tuple[bool, str]:
    """Whether an objective is measurable, and why not when it is not.

    A pure predicate so the API and the agent agree on what "measurable" means
    instead of each deciding for itself. It never raises: the caller decides
    whether a refusal is a 422 or a session status.
    """
    if not objective.request.strip():
        return False, "an objective needs a request describing what to achieve"
    if not objective.success_conditions:
        return (
            False,
            "an objective needs at least one success condition that can be OBSERVED; "
            "without one, completion cannot be verified and the session would run "
            "until its budget died",
        )
    for condition in objective.success_conditions:
        if "=" not in str(condition):
            return (
                False,
                f"success condition {condition!r} is not a 'key=value' check, so it "
                "cannot be observed",
            )
        key, _, value = str(condition).partition("=")
        if not key.strip() or not value.strip():
            return False, f"success condition {condition!r} is missing a key or a value"
    return True, ""
