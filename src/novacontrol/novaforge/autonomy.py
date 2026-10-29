"""NF-22 — the first bounded autonomous gameplay session.

    Observe -> Understand -> Identify objective -> Plan -> Approve -> Act
            -> Verify -> Recover or Continue

This module is a **supervisor**. It owns no window, captures no frame, injects no
input and keeps no second session manager: every step travels the interfaces the
preceding phases already built, through the manager that owns them:

* observation is NF-15's ``observe_frame_once`` (the observer's own validated
  frame), never a screenshot taken here;
* interpretation is NF-16's state reader, and the objective is NF-17's reading of
  that state (the manager consumes it on the way through);
* planning is NF-20's ``plan_actions``, execution NF-20's ``execute_plan`` over
  NF-19's control interface, and the verdict is NF-21's ``verify_outcomes`` with
  NF-21's own recovery decisions;
* lifecycle is the session's own ``pause``/``resume``/``stop``.

The rules this module holds itself to, stated so a reader does not have to infer
them from the absence of code:

* **A dispatched action is not a finished objective.** The run ends in
  ``OBJECTIVE_COMPLETED`` only when NF-17's reading of a fresh state says the
  declared objective completed (or NF-17's already-completed reading is what the
  precheck found) — never because a plan finished or the game stayed open.
* **Every terminal state carries its reason, and a limit is not a failure.** The
  coarse phase is COMPLETED/CANCELLED/FAILED; the precise ending is the
  :class:`AutonomyOutcome` vocabulary, where "the action budget ran out" is
  ``RESOURCE_LIMIT_REACHED`` and never ``OBJECTIVE_FAILED``.
* **An illegal phase move RAISES.** :data:`AUTONOMY_TRANSITIONS` is the only legal
  map, and a move outside it is an ``AutonomyTransitionError`` rather than a
  silently overwritten state.
* **An unknown outcome is never retried as an action.** Recovery goes through
  NF-21's recorded decision: the reads (refresh/wait/re-evaluate) and an
  in-budget replan are carried out; a pause, a stop or an escalation ENDS the run
  with its own outcome. No code path here can dispatch input.
* **The run's scope is approved once and never widened.** The objective, the game
  and every budget are recorded before the first cycle; a plan larger than the
  approved per-plan budget stops the run instead of quietly planning more.
* **Simulated evidence is labelled.** ``evidence_source`` is "live" or
  "simulated" and is reported on every surface: a run whose frames come from the
  simulated game is never described as controlling a real game.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from .objectives import ObjectiveStatus

__all__ = [
    "AUTONOMY_SCHEMA_VERSION",
    "AUTONOMY_TERMINAL_PHASES",
    "AUTONOMY_TRANSITIONS",
    "ACTIVE_PHASES",
    "MAX_ACTION_SECONDS",
    "MAX_ACTIONS",
    "MAX_ACTIONS_PER_PLAN",
    "MAX_CYCLES",
    "MAX_OBSERVATION_ATTEMPTS",
    "MAX_RECOVERY_ATTEMPTS",
    "MAX_REPLANS",
    "MAX_RUNS_KEPT",
    "MAX_SESSION_SECONDS",
    "MAX_VERIFICATION_SECONDS",
    "AutonomyConflictError",
    "AutonomyGateway",
    "AutonomyLimits",
    "AutonomyOutcome",
    "AutonomyPhase",
    "AutonomyPolicy",
    "AutonomyRun",
    "AutonomyTransitionError",
    "AutonomousSessionController",
    "objective_from_picture",
    "terminal_phase_for",
]

AUTONOMY_SCHEMA_VERSION = "nf22.1"

#: Hard ceilings. A caller may declare less, never more: an autonomous run's
#: bounds are the difference between "a bounded task" and "leave it running".
MAX_CYCLES = 40
MAX_ACTIONS = 24
MAX_ACTIONS_PER_PLAN = 8
MAX_SESSION_SECONDS = 900
MAX_OBSERVATION_ATTEMPTS = 5
MAX_RECOVERY_ATTEMPTS = 5
MAX_REPLANS = 5
MAX_VERIFICATION_SECONDS = 180
MAX_ACTION_SECONDS = 300
MAX_RUNS_KEPT = 20

#: The states the loop moves through. The names follow the phase's own
#: vocabulary; the underlying SESSION state machine stays the manager's
#: (:class:`~novacontrol.novaforge.models.SessionState`), because this is a
#: supervisor over a session, not a second lifecycle for one.
class AutonomyPhase(StrEnum):
    IDLE = "IDLE"
    INITIALIZING = "INITIALIZING"
    OBSERVING = "OBSERVING"
    UNDERSTANDING_STATE = "UNDERSTANDING_STATE"
    IDENTIFYING_OBJECTIVE = "IDENTIFYING_OBJECTIVE"
    PLANNING = "PLANNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RECOVERING = "RECOVERING"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


PHASES = tuple(item.value for item in AutonomyPhase)

#: Every legal move, and only those. ``PAUSED`` re-enters at ``OBSERVING``
#: because a resumed run must observe again rather than continue from a reading
#: taken before the pause; the three terminal phases have no exit.
AUTONOMY_TRANSITIONS: dict[AutonomyPhase, frozenset[AutonomyPhase]] = {
    AutonomyPhase.IDLE: frozenset({AutonomyPhase.INITIALIZING}),
    AutonomyPhase.INITIALIZING: frozenset(
        {
            AutonomyPhase.OBSERVING,
            # The dependency check pins the objective from the first reading it
            # takes, so this edge belongs to initialization rather than to a cycle.
            AutonomyPhase.IDENTIFYING_OBJECTIVE,
            AutonomyPhase.COMPLETED,
            AutonomyPhase.FAILED,
            AutonomyPhase.CANCELLED,
        }
    ),
    AutonomyPhase.OBSERVING: frozenset(
        {
            AutonomyPhase.UNDERSTANDING_STATE,
            AutonomyPhase.RECOVERING,
            AutonomyPhase.PAUSED,
            AutonomyPhase.CANCELLED,
            AutonomyPhase.FAILED,
        }
    ),
    AutonomyPhase.UNDERSTANDING_STATE: frozenset(
        {
            AutonomyPhase.IDENTIFYING_OBJECTIVE,
            AutonomyPhase.RECOVERING,
            AutonomyPhase.PAUSED,
            AutonomyPhase.CANCELLED,
            AutonomyPhase.FAILED,
        }
    ),
    AutonomyPhase.IDENTIFYING_OBJECTIVE: frozenset(
        {
            AutonomyPhase.PLANNING,
            # Back to a reading: initialization returns to the cycle proper, and a
            # cycle that re-identifies the objective after a recovery observes again.
            AutonomyPhase.OBSERVING,
            AutonomyPhase.RECOVERING,
            AutonomyPhase.COMPLETED,
            AutonomyPhase.PAUSED,
            AutonomyPhase.CANCELLED,
            AutonomyPhase.FAILED,
        }
    ),
    AutonomyPhase.PLANNING: frozenset(
        {
            AutonomyPhase.WAITING_FOR_APPROVAL,
            AutonomyPhase.EXECUTING,
            # NF-20 refuses to plan an objective its own reading already reports
            # complete, and the run ends there: the ending is reached from planning.
            AutonomyPhase.COMPLETED,
            AutonomyPhase.RECOVERING,
            AutonomyPhase.PAUSED,
            AutonomyPhase.CANCELLED,
            AutonomyPhase.FAILED,
        }
    ),
    AutonomyPhase.WAITING_FOR_APPROVAL: frozenset(
        {
            AutonomyPhase.EXECUTING,
            AutonomyPhase.PAUSED,
            AutonomyPhase.CANCELLED,
            AutonomyPhase.FAILED,
        }
    ),
    AutonomyPhase.EXECUTING: frozenset(
        {
            AutonomyPhase.VERIFYING,
            AutonomyPhase.RECOVERING,
            AutonomyPhase.PAUSED,
            AutonomyPhase.CANCELLED,
            AutonomyPhase.FAILED,
        }
    ),
    AutonomyPhase.VERIFYING: frozenset(
        {
            AutonomyPhase.COMPLETED,
            AutonomyPhase.RECOVERING,
            AutonomyPhase.OBSERVING,
            AutonomyPhase.PAUSED,
            AutonomyPhase.CANCELLED,
            AutonomyPhase.FAILED,
        }
    ),
    AutonomyPhase.RECOVERING: frozenset(
        {
            AutonomyPhase.OBSERVING,
            AutonomyPhase.COMPLETED,
            AutonomyPhase.PAUSED,
            AutonomyPhase.CANCELLED,
            AutonomyPhase.FAILED,
        }
    ),
    AutonomyPhase.PAUSED: frozenset(
        {
            AutonomyPhase.OBSERVING,
            AutonomyPhase.CANCELLED,
            AutonomyPhase.FAILED,
        }
    ),
    AutonomyPhase.COMPLETED: frozenset(),
    AutonomyPhase.CANCELLED: frozenset(),
    AutonomyPhase.FAILED: frozenset(),
}

AUTONOMY_TERMINAL_PHASES = frozenset(
    {AutonomyPhase.COMPLETED, AutonomyPhase.CANCELLED, AutonomyPhase.FAILED}
)

#: Observer reason codes that mean the TARGET's own state, not a bad frame.
TARGET_UNSEEN_CODES = frozenset(
    {
        "target_minimized",
        "target_hidden",
        "target_offscreen",
        "target_not_foreground",
        "frame_directory_unusable",
        "target_probe_failed",
    }
)

#: Reason codes that mean the frame is not the pinned window's evidence.
TARGET_IDENTITY_CODES = frozenset(
    {"not_target", "covered", "target_changed", "target_lost", "wrong_target"}
)

#: NF-20/NF-19 refusal codes that mean the run must NOT keep going.
REFUSAL_CODES = frozenset(
    {
        "closed",
        "no_session",
        "not_running",
        "held",
        "pending",
        "unvalidated",
        "budget_exhausted",
        "budget_unenforced",
        "unsupported",
        "wrong_session_state",
        "target_changed",
        "target_lost",
        "gate_refused",
        "service_stopped",
    }
)

#: The reason codes :meth:`AutonomousSessionController._call` uses when a
#: dependency never answered at all (a timeout, or a call that raised). None of
#: these is a verdict about the game: they are a verdict about the stage.
DEPENDENCY_CODES = frozenset({"dependency_timeout", "dependency_failed"})

#: The observer's per-read statuses this supervisor reads (NF-15).
OBSERVED_STATUS = "observed"
OBSERVATION_BUSY_STATUS = "busy"
OBSERVATION_IDLE_STATUS = "not_observing"

#: How long to wait when a read answers ``busy`` — a capture is already in
#: flight. Waiting less than one capture can never observe its completion: the
#: retry then finds the SAME capture still running and the run is stopped for an
#: unavailable observation it never actually lacked (a 2880x1800 window takes
#: over a second per frame). This is a wait for measured work, not a poll delay.
CAPTURE_IN_FLIGHT_WAIT_SECONDS = 1.6

#: How long to wait when the observer reports it is not sampling at all.
OBSERVATION_IDLE_WAIT_SECONDS = 0.25

#: Phases in which the loop is actively working on the objective.
ACTIVE_PHASES = frozenset(
    {
        AutonomyPhase.INITIALIZING,
        AutonomyPhase.OBSERVING,
        AutonomyPhase.UNDERSTANDING_STATE,
        AutonomyPhase.IDENTIFYING_OBJECTIVE,
        AutonomyPhase.PLANNING,
        AutonomyPhase.WAITING_FOR_APPROVAL,
        AutonomyPhase.EXECUTING,
        AutonomyPhase.VERIFYING,
        AutonomyPhase.RECOVERING,
    }
)


class AutonomyTransitionError(RuntimeError):
    """An illegal phase move was attempted; the state is left untouched."""


class AutonomyConflictError(RuntimeError):
    """A second autonomous run would share a game another run is driving."""


#: Why an autonomous run ended. The phase says how it ended; this says why.
class AutonomyOutcome(StrEnum):
    OBJECTIVE_COMPLETED = "objective_completed"
    OBJECTIVE_FAILED = "objective_failed"
    SESSION_CANCELLED = "session_cancelled"
    SESSION_TIMED_OUT = "session_timed_out"
    RESOURCE_LIMIT_REACHED = "resource_limit_reached"
    OBSERVATION_UNAVAILABLE = "observation_unavailable"
    OUTCOME_UNKNOWN = "outcome_unknown"
    DEPENDENCY_FAILURE = "dependency_failure"
    SAFETY_STOP = "safety_stop"


OUTCOMES = tuple(item.value for item in AutonomyOutcome)

#: The coarse phase each ending lands in. A limit that was reached is NOT a
#: failed objective — the objective was never shown to have failed — so it lands
#: in FAILED with its own outcome, which is the distinction the session record
#: already makes between "succeeded" and "ran out of clock".
TERMINAL_PHASE_FOR_OUTCOME: dict[AutonomyOutcome, AutonomyPhase] = {
    AutonomyOutcome.OBJECTIVE_COMPLETED: AutonomyPhase.COMPLETED,
    AutonomyOutcome.SESSION_CANCELLED: AutonomyPhase.CANCELLED,
    AutonomyOutcome.OBJECTIVE_FAILED: AutonomyPhase.FAILED,
    AutonomyOutcome.SESSION_TIMED_OUT: AutonomyPhase.FAILED,
    AutonomyOutcome.RESOURCE_LIMIT_REACHED: AutonomyPhase.FAILED,
    AutonomyOutcome.OBSERVATION_UNAVAILABLE: AutonomyPhase.FAILED,
    AutonomyOutcome.OUTCOME_UNKNOWN: AutonomyPhase.FAILED,
    AutonomyOutcome.DEPENDENCY_FAILURE: AutonomyPhase.FAILED,
    AutonomyOutcome.SAFETY_STOP: AutonomyPhase.FAILED,
}


def terminal_phase_for(outcome: AutonomyOutcome) -> AutonomyPhase:
    return TERMINAL_PHASE_FOR_OUTCOME[outcome]


@dataclass(frozen=True, slots=True)
class AutonomyLimits:
    """The execution limits of ONE autonomous run, all of them ceilings in code.

    Conservative by default and validated at construction: a caller that asks for
    a wider bound than the ceiling above is refused rather than clamped, because a
    silently narrowed budget is as dishonest as a silently widened one.
    """

    #: Decision cycles, and actions across the whole run.
    max_cycles: int = 12
    max_actions: int = 6
    #: The largest plan the run's approval covers.
    max_actions_per_plan: int = 2
    #: Wall clock, measured with the injected monotonic clock.
    session_seconds: float = 180.0
    #: Per-phase bounds. A phase that does not answer inside its bound ends the
    #: run instead of holding the cancel path.
    observation_timeout_seconds: float = 20.0
    planning_timeout_seconds: float = 20.0
    action_timeout_seconds: float = 60.0
    verification_timeout_seconds: float = 45.0
    #: How many times one cycle may re-read the observation, and how many
    #: recoveries and replans the run may take in total.
    observation_attempts: int = 2
    recovery_attempts: int = 2
    replans: int = 2

    def errors(self) -> list[str]:
        problems: list[str] = []
        if not 1 <= int(self.max_cycles) <= MAX_CYCLES:
            problems.append(f"max_cycles must be between 1 and {MAX_CYCLES}.")
        if not 1 <= int(self.max_actions) <= MAX_ACTIONS:
            problems.append(f"max_actions must be between 1 and {MAX_ACTIONS}.")
        if not 1 <= int(self.max_actions_per_plan) <= MAX_ACTIONS_PER_PLAN:
            problems.append(
                f"max_actions_per_plan must be between 1 and {MAX_ACTIONS_PER_PLAN}."
            )
        if int(self.max_actions_per_plan) > int(self.max_actions):
            problems.append("max_actions_per_plan must not exceed max_actions.")
        if not 0 < float(self.session_seconds) <= MAX_SESSION_SECONDS:
            problems.append(f"session_seconds must be between 0 and {MAX_SESSION_SECONDS}.")
        if not 1 <= int(self.observation_attempts) <= MAX_OBSERVATION_ATTEMPTS:
            problems.append(
                f"observation_attempts must be between 1 and {MAX_OBSERVATION_ATTEMPTS}."
            )
        if not 0 <= int(self.recovery_attempts) <= MAX_RECOVERY_ATTEMPTS:
            problems.append(
                f"recovery_attempts must be between 0 and {MAX_RECOVERY_ATTEMPTS}."
            )
        if not 0 <= int(self.replans) <= MAX_REPLANS:
            problems.append(f"replans must be between 0 and {MAX_REPLANS}.")
        for name, ceiling in (
            ("observation_timeout_seconds", 120.0),
            ("planning_timeout_seconds", 120.0),
            ("action_timeout_seconds", MAX_ACTION_SECONDS),
            ("verification_timeout_seconds", MAX_VERIFICATION_SECONDS),
        ):
            value = float(getattr(self, name))
            if not 0 < value <= ceiling:
                problems.append(f"{name} must be between 0 and {ceiling:g}.")
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_cycles": self.max_cycles,
            "max_actions": self.max_actions,
            "max_actions_per_plan": self.max_actions_per_plan,
            "session_seconds": self.session_seconds,
            "observation_timeout_seconds": self.observation_timeout_seconds,
            "planning_timeout_seconds": self.planning_timeout_seconds,
            "action_timeout_seconds": self.action_timeout_seconds,
            "verification_timeout_seconds": self.verification_timeout_seconds,
            "observation_attempts": self.observation_attempts,
            "recovery_attempts": self.recovery_attempts,
            "replans": self.replans,
        }


PLAN_APPROVAL_MODES = ("run", "per_plan")


@dataclass(frozen=True, slots=True)
class AutonomyPolicy:
    """Who approves what, and how much of the run is recorded.

    ``plan_approval`` is the ONE switch that decides whether the run's approval
    covers each plan ("run", the default) or whether the operator approves every
    plan as it is built ("per_plan", which parks the run in
    ``WAITING_FOR_APPROVAL``).
    """

    require_approval: bool = True
    plan_approval: str = "run"
    #: Off by default: an unwired gate means "not measured", which is reported
    #: as such instead of being read as permission. A WIRED gate that refuses
    #: stops the run with ``RESOURCE_LIMIT_REACHED``.
    #: A live run is admitted only through the machine's resource gate. It is ON
    #: by default because the gate is read from the build's own governor: a
    #: CRITICAL machine refuses the run, while an unwired or unreadable gate is
    #: reported as "not measured" and admitted (an absent sensor is neither
    #: permission nor pressure). Turning it off means ignoring the gate, not
    #: discovering one that is not there.
    require_resource_gate: bool = True
    stop_session_on_terminal: bool = True
    max_runs_kept: int = 5

    def errors(self) -> list[str]:
        problems: list[str] = []
        if self.plan_approval not in PLAN_APPROVAL_MODES:
            problems.append(f"plan_approval must be one of {list(PLAN_APPROVAL_MODES)}.")
        if not 1 <= int(self.max_runs_kept) <= MAX_RUNS_KEPT:
            problems.append(f"max_runs_kept must be between 1 and {MAX_RUNS_KEPT}.")
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "require_approval": self.require_approval,
            "plan_approval": self.plan_approval,
            "require_resource_gate": self.require_resource_gate,
            "stop_session_on_terminal": self.stop_session_on_terminal,
            "max_runs_kept": self.max_runs_kept,
            "limits_note": (
                "An autonomous run is bounded by its own declared limits; the policy "
                "decides who approves a plan, never how much the run may do."
            ),
        }


@runtime_checkable
class AutonomyGateway(Protocol):
    """The slice of the session manager an autonomous run may use.

    Every method here is an interface that already existed for a person to drive
    by hand (or that wraps one in a line); nothing in this protocol can capture a
    window, inject input or bypass an approval.
    """

    def get(self, session_id: str) -> Any | None:
        """The live session record, for its pinned target and its state."""

    async def observe_frame_once(self) -> Mapping[str, Any]:
        """NF-15: capture and validate one frame now."""

    async def read_state(self) -> Mapping[str, Any]:
        """NF-16: read one structured state from the newest validated frame."""

    def objective_picture(self, session_id: str) -> Mapping[str, Any]:
        """NF-17: the newest interpretation of that state."""

    async def plan_actions(self, session_id: str) -> Mapping[str, Any]:
        """NF-20: build ONE bounded plan; nothing is sent."""

    async def execute_plan(
        self, session_id: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """NF-20 over NF-19: execute an approved plan, step by step."""

    async def verify_outcomes(
        self, session_id: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """NF-21: verify the plan's dispatched steps against fresh evidence."""

    async def recover_outcome(
        self, session_id: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """NF-21: carry out one outcome's recorded recovery decision."""

    async def pause(self, session_id: str) -> Mapping[str, Any]:
        """The session's own pause path (which releases held inputs)."""

    async def resume(self, session_id: str) -> Mapping[str, Any]:
        """The session's own resume path."""

    async def stop(self, session_id: str, *, reason: str = "") -> Mapping[str, Any]:
        """The session's own stop path (cleanup included)."""


def _stamp() -> str:
    return datetime.now(UTC).isoformat()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in value if isinstance(row, Mapping)] if isinstance(
        value, Sequence
    ) and not isinstance(value, (str, bytes)) else []


def objective_from_picture(picture: Mapping[str, Any]) -> dict[str, Any]:
    """The one objective a run follows out of NF-17's picture.

    ``picture`` is the objective state's own ``to_dict()`` (``objectives`` at the
    top level); the nested spellings are accepted so a caller that wraps it in
    ``{"current": …}`` is not silently read as "no objective".

    An interpretation may hold several objectives (a mission and a sub-task); the
    run follows the FIRST of them, which is the one NF-17 ordered first, and
    reports the rest rather than picking a favourite by status — choosing the
    completed one would make every run finish immediately.
    """
    data = _mapping(picture)
    objectives = _rows(data.get("objectives"))
    if not objectives:
        nested = _mapping(data.get("interpretation") or data.get("current"))
        objectives = _rows(nested.get("objectives"))
    return objectives[0] if objectives else {}


def _objective_completed(objective: Mapping[str, Any]) -> bool:
    return str(objective.get("status") or "") == ObjectiveStatus.COMPLETED.value


def _objective_failed(objective: Mapping[str, Any]) -> bool:
    return str(objective.get("status") or "") == ObjectiveStatus.FAILED.value


def _progress_line(objective: Mapping[str, Any]) -> str:
    progress = _mapping(objective.get("progress"))
    if not progress:
        return ""
    value = progress.get("value")
    target = progress.get("target")
    fraction = progress.get("fraction")
    parts = [f"progress {value if value is not None else 'unread'}"
             f"/{target if target is not None else 'unstated'}"]
    if fraction is not None:
        parts.append(f"{float(fraction) * 100:.0f}%")
    if progress.get("resets"):
        parts.append(f"{progress['resets']} reset(s)")
    return " ".join(parts)


@dataclass(slots=True)
class AutonomyStep:
    """One recorded edge of a run: a phase move, with its clock and its reason."""

    at: str
    phase: str
    reason: str = ""
    cycle: int = 0
    outcome: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "phase": self.phase,
            "reason": self.reason,
            "cycle": self.cycle,
            "outcome": self.outcome,
            "detail": dict(self.detail),
        }


@dataclass(slots=True)
class AutonomyCycle:
    """Everything ONE decision cycle observed, decided, ran and concluded."""

    index: int
    started_at: str
    phase: str = ""
    observation: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    objective: dict[str, Any] = field(default_factory=dict)
    knowledge_refs: list[str] = field(default_factory=list)
    plan: dict[str, Any] = field(default_factory=dict)
    approval: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    recovery: dict[str, Any] = field(default_factory=dict)
    actions_dispatched: int = 0
    ended_at: str = ""
    outcome: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "phase": self.phase,
            "observation": dict(self.observation),
            "state": dict(self.state),
            "objective": dict(self.objective),
            "knowledge_refs": list(self.knowledge_refs),
            "plan": dict(self.plan),
            "approval": dict(self.approval),
            "execution": dict(self.execution),
            "verification": dict(self.verification),
            "recovery": dict(self.recovery),
            "actions_dispatched": self.actions_dispatched,
            "outcome": self.outcome,
            "reason": self.reason,
        }


@dataclass(slots=True)
class AutonomyRun:
    """One autonomous run: its approval, its budget, its cycles and its ending."""

    run_id: str
    session_id: str
    phase: AutonomyPhase
    evidence_source: str
    game: str = ""
    target: dict[str, Any] = field(default_factory=dict)
    objective_id: str = ""
    objective_description: str = ""
    completion_criteria: list[str] = field(default_factory=list)
    failure_criteria: list[str] = field(default_factory=list)
    task: str = ""
    approved: bool = False
    approved_at: str = ""
    approved_note: str = ""
    limits: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    ended_at: str = ""
    elapsed_seconds: float | None = None
    cycles: list[AutonomyCycle] = field(default_factory=list)
    steps: list[AutonomyStep] = field(default_factory=list)
    outcome: str = ""
    ended_reason: str = ""
    actions_dispatched: int = 0
    recoveries: int = 0
    replans: int = 0
    verification_evidence: list[str] = field(default_factory=list)
    resources: dict[str, Any] = field(default_factory=dict)
    session_ended: dict[str, Any] | None = None
    limitations: list[str] = field(default_factory=list)
    #: The reading that was in force when an action's outcome could NOT be
    #: established (``[observation_id, state_id]``). While that same reading is
    #: still current, an identical new plan is refused instead of dispatched:
    #: pressing again after a result nobody could read is the repetition the phase
    #: forbids. A NEW reading clears it, because new information is exactly what
    #: earns another attempt.
    unresolved_evidence: list[str] = field(default_factory=list)

    @property
    def terminal(self) -> bool:
        return self.phase in AUTONOMY_TERMINAL_PHASES

    @property
    def active(self) -> bool:
        return self.phase in ACTIVE_PHASES

    def to_dict(self, *, cycles: int = 20) -> dict[str, Any]:
        return {
            "schema_version": AUTONOMY_SCHEMA_VERSION,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "phase": self.phase.value,
            "active": self.active,
            "terminal": self.terminal,
            "evidence_source": self.evidence_source,
            "game": self.game,
            "target": dict(self.target),
            "objective_id": self.objective_id,
            "objective": self.objective_description,
            "completion_criteria": list(self.completion_criteria),
            "failure_criteria": list(self.failure_criteria),
            "task": self.task,
            "approved": self.approved,
            "approved_at": self.approved_at,
            "approved_note": self.approved_note,
            "limits": dict(self.limits),
            "policy": dict(self.policy),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "elapsed_seconds": self.elapsed_seconds,
            "cycles": [row.to_dict() for row in self.cycles[-max(1, cycles) :]],
            "cycle_count": len(self.cycles),
            "steps": [row.to_dict() for row in self.steps],
            "outcome": self.outcome,
            "reason": self.ended_reason,
            "actions_dispatched": self.actions_dispatched,
            "recoveries": self.recoveries,
            "replans": self.replans,
            "verification_evidence": list(self.verification_evidence),
            "resources": dict(self.resources),
            "session_ended": dict(self.session_ended) if self.session_ended else None,
            "limitations": list(self.limitations),
        }


class AutonomousSessionController:
    """Run ONE bounded autonomous session, cycle by cycle, inside its limits.

    Every collaborator is injected: the gateway is the session manager, the clock
    is a monotonic source, ``sleep`` is awaited through (never ``time.sleep``, so
    the event loop and the cancel path stay live), the resource probe is the
    host's own gate when one exists, and ``publish`` is the application's event
    bus. The controller never imports a desktop, a capture backend or an injector.
    """

    def __init__(
        self,
        *,
        gateway: AutonomyGateway,
        limits: AutonomyLimits | None = None,
        policy: AutonomyPolicy | None = None,
        #: A caller that does not DECLARE its provenance has not earned the live
        #: label: unlabelled evidence is the simulated class, so nothing it feeds
        #: can be counted as real-game evidence. Production passes ``live``
        #: explicitly (manager), and the acceptance harness passes ``simulated``.
        evidence_source: str = "simulated",
        resource_probe: Callable[[], Mapping[str, Any]] | None = None,
        publish: Callable[[str, Mapping[str, Any]], Awaitable[None]] | None = None,
        store: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        #: How long a cancellation waits for the loop to reach its next checkpoint
        #: before the run's own task is stopped. A dependency that never answers
        #: (a wedged observation, a hung planner) must not make cancel hang with
        #: it, and the cancel path may not depend on one.
        cancel_grace_seconds: float = 2.0,
        namespace: str = "novaforge_autonomy",
    ) -> None:
        self.gateway = gateway
        self.limits = limits or AutonomyLimits()
        self.policy = policy or AutonomyPolicy()
        if self.limits.errors():
            raise ValueError("Invalid autonomy limits: " + "; ".join(self.limits.errors()))
        if self.policy.errors():
            raise ValueError("Invalid autonomy policy: " + "; ".join(self.policy.errors()))
        if evidence_source not in {"live", "simulated"}:
            raise ValueError("evidence_source must be 'live' or 'simulated'.")
        self.evidence_source = evidence_source
        self.resource_probe = resource_probe
        self.publish = publish
        self.store = store
        self.clock = clock
        self.sleep = sleep
        self.cancel_grace_seconds = max(0.0, float(cancel_grace_seconds))
        self.namespace = namespace
        self.runs: dict[str, AutonomyRun] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._pause_events: dict[str, asyncio.Event] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._approval_events: dict[str, asyncio.Event] = {}
        self._approvals: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._counts: dict[str, int] = {"runs": 0, "cycles": 0, "actions": 0, "recoveries": 0}
        #: Run ids whose ending has been recorded, so a second terminal report for
        #: the same run neither re-counts it nor stops its session twice.
        self._ended: set[str] = set()
        self._load()

    # ── persistence ────────────────────────────────────────────────────────
    def _load(self) -> None:
        if self.store is None:
            return
        try:
            payload = self.store.read(self.namespace)
        except Exception:  # noqa: BLE001 - an unreadable store is not a crash
            return
        rows = payload.get("runs") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            return
        for row in rows:
            run = self._run_from_dict(row)
            if run is not None:
                self.runs[run.session_id] = run

    def _run_from_dict(self, payload: Any) -> AutonomyRun | None:
        data = _mapping(payload)
        session_id = str(data.get("session_id") or "")
        if not session_id:
            return None
        try:
            phase = AutonomyPhase(str(data.get("phase") or AutonomyPhase.IDLE.value))
        except ValueError:
            phase = AutonomyPhase.FAILED
        run = AutonomyRun(
            run_id=str(data.get("run_id") or uuid4().hex[:12]),
            session_id=session_id,
            phase=phase,
            # A stored run that never recorded a source is restored as simulated,
            # never promoted to live: the missing label is the unmeasured fact.
            evidence_source=str(data.get("evidence_source") or "simulated"),
            game=str(data.get("game") or ""),
            target=_mapping(data.get("target")),
            objective_id=str(data.get("objective_id") or ""),
            objective_description=str(data.get("objective") or ""),
            completion_criteria=[str(item) for item in data.get("completion_criteria") or []],
            failure_criteria=[str(item) for item in data.get("failure_criteria") or []],
            task=str(data.get("task") or ""),
            approved=bool(data.get("approved")),
            approved_at=str(data.get("approved_at") or ""),
            approved_note=str(data.get("approved_note") or ""),
            limits=_mapping(data.get("limits")),
            policy=_mapping(data.get("policy")),
            started_at=str(data.get("started_at") or ""),
            ended_at=str(data.get("ended_at") or ""),
            elapsed_seconds=data.get("elapsed_seconds"),
            outcome=str(data.get("outcome") or ""),
            ended_reason=str(data.get("reason") or ""),
            actions_dispatched=int(data.get("actions_dispatched") or 0),
            recoveries=int(data.get("recoveries") or 0),
            replans=int(data.get("replans") or 0),
            verification_evidence=[str(item) for item in data.get("verification_evidence") or []],
            resources=_mapping(data.get("resources")),
            session_ended=_mapping(data.get("session_ended")) or None,
            limitations=[str(item) for item in data.get("limitations") or []],
        )
        for row in data.get("cycles") or []:
            cycle = _mapping(row)
            run.cycles.append(
                AutonomyCycle(
                    index=int(cycle.get("index") or 0),
                    started_at=str(cycle.get("started_at") or ""),
                    ended_at=str(cycle.get("ended_at") or ""),
                    phase=str(cycle.get("phase") or ""),
                    observation=_mapping(cycle.get("observation")),
                    state=_mapping(cycle.get("state")),
                    objective=_mapping(cycle.get("objective")),
                    knowledge_refs=[str(item) for item in cycle.get("knowledge_refs") or []],
                    plan=_mapping(cycle.get("plan")),
                    approval=_mapping(cycle.get("approval")),
                    execution=_mapping(cycle.get("execution")),
                    verification=_mapping(cycle.get("verification")),
                    recovery=_mapping(cycle.get("recovery")),
                    actions_dispatched=int(cycle.get("actions_dispatched") or 0),
                    outcome=str(cycle.get("outcome") or ""),
                    reason=str(cycle.get("reason") or ""),
                )
            )
        return run

    def _persist(self) -> None:
        """Write the bounded run history; a store failure never fails a run."""
        if self.store is None:
            return
        try:
            rows = [run.to_dict(cycles=8) for run in list(self.runs.values())[-self.policy.max_runs_kept :]]
            self.store.write(self.namespace, {"runs": rows})
        except Exception:  # noqa: BLE001 - an unwritable snapshot is not a run failure
            return

    # ── reading ────────────────────────────────────────────────────────────
    def status(self, session_id: str = "") -> dict[str, Any]:
        """The panel's view: the bounds, the runs, and what each is doing."""
        runs = [
            run.to_dict(cycles=6)
            for run in self.runs.values()
            if not session_id or run.session_id == session_id
        ]
        probe = self._probe_resources()
        return {
            "schema_version": AUTONOMY_SCHEMA_VERSION,
            "available": True,
            "evidence_source": self.evidence_source,
            "limits": self.limits.to_dict(),
            "policy": self.policy.to_dict(),
            "resources": probe,
            "runs": runs,
            "counts": dict(self._counts),
            "can_start": not any(run.active for run in self.runs.values() if run.session_id == session_id)
            if session_id
            else not any(run.active for run in self.runs.values()),
            "limitations": [
                "An autonomous run drives the game through the session's own control "
                "interface: it never captures a frame, injects an input or approves itself.",
                "The objective must be one a reading can check (NF-17); a game whose state "
                "cannot be read has no completion criterion and is refused.",
                f"Evidence source: {self.evidence_source}.",
            ],
        }

    def trace(self, session_id: str = "") -> dict[str, Any]:
        """The complete orchestration history of the runs, newest last."""
        runs = [
            run.to_dict(cycles=40)
            for run in self.runs.values()
            if not session_id or run.session_id == session_id
        ]
        return {
            "schema_version": AUTONOMY_SCHEMA_VERSION,
            "evidence_source": self.evidence_source,
            "runs": runs,
            "counts": dict(self._counts),
        }

    def run_for(self, session_id: str) -> AutonomyRun | None:
        return self.runs.get(session_id)

    # ── control ────────────────────────────────────────────────────────────
    async def start(
        self,
        session_id: str,
        *,
        approved: bool = False,
        note: str = "",
        task: str = "",
        limits: AutonomyLimits | None = None,
    ) -> dict[str, Any]:
        """Begin ONE bounded autonomous run over a live session.

        The approval is the gate: without it (and without a policy that says no
        approval is needed) nothing starts. The run pins the game, the target and
        the objective BEFORE its first cycle, so what was approved is what runs.
        """
        limits = limits or self.limits
        problems = limits.errors()
        if problems:
            raise ValueError("Invalid autonomy limits: " + "; ".join(problems))
        record = self.gateway.get(session_id)
        if record is None:
            raise ValueError(f"No NovaForge session is named {session_id!r}.")
        if not approved and self.policy.require_approval:
            return {
                "started": False,
                "session_id": session_id,
                "code": "approval_required",
                "reason": (
                    "An autonomous run needs the operator's explicit approval: it may send "
                    "bounded input to the game. Nothing was started."
                ),
                "approved": False,
            }
        existing = self.runs.get(session_id)
        if existing is not None and not existing.terminal:
            return {
                "started": False,
                "session_id": session_id,
                "code": "already_running",
                "reason": f"Session {session_id!r} already has an autonomous run in {existing.phase.value}.",
                "approved": True,
            }
        conflict = self._conflicting_run(session_id, record)
        if conflict is not None:
            raise AutonomyConflictError(
                f"Autonomous run {conflict.run_id} is already controlling "
                f"{conflict.game or conflict.target.get('title') or 'this game'}; two runs must "
                "not drive the same game."
            )
        target = self._target_of(record)
        objective = objective_from_picture(self.gateway.objective_picture(session_id))
        resources = self._probe_resources()
        if self.policy.require_resource_gate and resources.get("allow") is False:
            return {
                "started": False,
                "session_id": session_id,
                "code": "resource_gate_refused",
                "reason": str(resources.get("reason") or "The resource gate refused the run."),
                "resources": resources,
                "approved": True,
            }
        run = AutonomyRun(
            run_id=uuid4().hex[:12],
            session_id=session_id,
            phase=AutonomyPhase.IDLE,
            evidence_source=self.evidence_source,
            game=str(target.get("title") or ""),
            target=target,
            objective_id=str(objective.get("objective_id") or ""),
            objective_description=str(objective.get("description") or objective.get("source_text") or ""),
            completion_criteria=[str(item) for item in objective.get("completion_conditions") or []],
            failure_criteria=[str(item) for item in objective.get("failure_conditions") or []],
            task=str(task or ""),
            approved=True,
            approved_at=_stamp(),
            approved_note=str(note or ""),
            limits=limits.to_dict(),
            policy=self.policy.to_dict(),
            started_at=_stamp(),
            resources=resources,
            limitations=[
                "The run stops at its first terminal outcome; nothing restarts it.",
                "The objective, the game and every budget were pinned before the first cycle.",
            ],
            verification_evidence=[],
        )
        self.runs[session_id] = run
        self._counts["runs"] += 1
        self._cancel_events[session_id] = asyncio.Event()
        self._pause_events[session_id] = asyncio.Event()
        self._pause_events[session_id].set()
        self._approval_events[session_id] = asyncio.Event()
        self._record(run, AutonomyPhase.INITIALIZING, "The operator approved this bounded run.")
        self._persist()
        task_handle = asyncio.create_task(self._drive(run))
        self._tasks[session_id] = task_handle
        return {
            "started": True,
            "session_id": session_id,
            "run_id": run.run_id,
            "approved": True,
            "evidence_source": run.evidence_source,
            "objective_id": run.objective_id,
            "objective": run.objective_description,
            "completion_criteria": list(run.completion_criteria),
            "limits": dict(run.limits),
            "resources": resources,
            "status": run.to_dict(cycles=1),
        }

    async def pause(self, session_id: str, *, reason: str = "") -> dict[str, Any]:
        run = self.runs.get(session_id)
        if run is None or run.terminal:
            return {"paused": False, "code": "no_active_run", "reason": "There is no active run to pause."}
        event = self._pause_events.get(session_id)
        if event is not None:
            event.clear()
        # The phase is recorded HERE, not at the next checkpoint: a dependency call
        # may be in flight, and the operator's pause is a fact about the run the
        # moment they asked for it. The loop then parks at its next checkpoint.
        if AutonomyPhase.PAUSED in AUTONOMY_TRANSITIONS.get(run.phase, frozenset()):
            self._record(
                run,
                AutonomyPhase.PAUSED,
                reason or "The operator paused the autonomous run.",
            )
        session = await self._safe(self.gateway.pause(session_id), "pause")
        return {
            "paused": True,
            "session_id": session_id,
            "code": "pause_requested",
            "reason": reason or "The operator paused the autonomous run.",
            "session": session,
        }

    async def resume(self, session_id: str, *, approved: bool | None = None) -> dict[str, Any]:
        run = self.runs.get(session_id)
        if run is None:
            return {"resumed": False, "code": "no_run", "reason": "There is no run to resume."}
        if run.terminal:
            return {
                "resumed": False,
                "code": "run_finished",
                "reason": (
                    f"This run already ended as {run.outcome or run.phase.value}; a finished run "
                    "is never restarted. Start a new one with its own approval."
                ),
            }
        if approved is False:
            return {"resumed": False, "code": "approval_required", "reason": "Resuming needs approval."}
        event = self._pause_events.get(session_id)
        if event is not None:
            event.set()
        if run.phase is AutonomyPhase.PAUSED:
            # A resumed run observes AGAIN rather than continuing from a reading
            # taken before the pause: the screen has had time to change.
            self._record(run, AutonomyPhase.OBSERVING, "The operator resumed the run.")
        session = await self._safe(self.gateway.resume(session_id), "resume")
        return {"resumed": True, "session_id": session_id, "code": "resumed", "session": session}

    async def cancel(self, session_id: str, *, reason: str = "") -> dict[str, Any]:
        """Ask a run to stop before its next stage, then let it finish cleanly."""
        run = self.runs.get(session_id)
        if run is None:
            return {"cancelled": False, "code": "no_run", "reason": "There is no run to cancel."}
        if run.terminal:
            return {
                "cancelled": True,
                "code": "already_finished",
                "reason": f"The run had already ended as {run.outcome or run.phase.value}.",
                "status": run.to_dict(cycles=1),
            }
        cancel_event = self._cancel_events.get(session_id)
        if cancel_event is not None:
            cancel_event.set()
        pause_event = self._pause_events.get(session_id)
        if pause_event is not None:
            pause_event.set()
        approval = self._approval_events.get(session_id)
        if approval is not None:
            approval.set()
        task_handle = self._tasks.get(session_id)
        if task_handle is not None and not task_handle.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(task_handle), timeout=self.cancel_grace_seconds
                )
            except (TimeoutError, asyncio.TimeoutError):
                # The loop is inside a dependency call that has not answered: the
                # task is stopped directly rather than waiting for it, and the
                # ending is recorded by the loop's own cancellation branch.
                task_handle.cancel()
                try:
                    await task_handle
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 - the ending is recorded
                    pass
                if not run.terminal:
                    self._finish(
                        run,
                        AutonomyOutcome.SESSION_CANCELLED,
                        "The run did not reach a checkpoint inside its cancellation grace, so it "
                        "was stopped directly.",
                    )
            except asyncio.CancelledError:
                raise
        return {
            "cancelled": True,
            "session_id": session_id,
            "code": "cancelled",
            "reason": reason or "The operator cancelled the run.",
            "status": run.to_dict(cycles=1),
        }

    async def approve_plan(
        self, session_id: str, *, approved: bool = False, note: str = ""
    ) -> dict[str, Any]:
        """Answer a run parked in ``WAITING_FOR_APPROVAL`` (per-plan policy)."""
        run = self.runs.get(session_id)
        if run is None or run.terminal:
            return {"approved": False, "code": "no_active_run", "reason": "There is no run awaiting approval."}
        if run.phase is not AutonomyPhase.WAITING_FOR_APPROVAL:
            return {
                "approved": False,
                "code": "not_waiting",
                "reason": f"The run is in {run.phase.value}, not waiting for approval.",
            }
        self._approvals[session_id] = {"approved": bool(approved), "note": str(note or ""), "at": _stamp()}
        event = self._approval_events.get(session_id)
        if event is not None:
            event.set()
        return {
            "approved": bool(approved),
            "session_id": session_id,
            "code": "plan_approved" if approved else "plan_rejected",
            "reason": str(note or ("The operator approved this plan." if approved else "The operator refused this plan.")),
        }

    async def wait(self, session_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Await one run's own task, so a caller can follow it to its ending.

        This is the *caller's* convenience, not a second supervisor: the run is
        already driving itself, and a task that vanished (or an unknown session)
        is answered with the run's current record rather than an exception.
        """
        task = self._tasks.get(session_id)
        if task is not None and not task.done():
            try:
                if timeout is None:
                    await asyncio.shield(task)
                else:
                    await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.CancelledError as exc:
                run = self.runs.get(session_id)
                if run is None or not run.terminal:
                    raise
                return {
                    "finished": True,
                    "code": "cancelled",
                    "reason": str(exc) or "The run's task was stopped.",
                    "status": run.to_dict(cycles=1),
                }
            except (TimeoutError, asyncio.TimeoutError):
                return {
                    "finished": False,
                    "code": "still_running",
                    "reason": f"The run did not finish within {timeout:g} s.",
                    "status": self.runs[session_id].to_dict(cycles=1)
                    if session_id in self.runs
                    else None,
                }
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the run records its own faults
                return {"finished": False, "code": "failed", "reason": str(exc)}
        run = self.runs.get(session_id)
        return {
            "finished": bool(run is not None and run.terminal),
            "code": "finished" if run is not None and run.terminal else "unknown_run",
            "status": run.to_dict(cycles=1) if run is not None else None,
        }

    async def shutdown(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Stop every run, then let the session stops they triggered finish.

        ``cancel`` ends the run and queues the session stop on the loop, so a
        caller that returns here and assumes the release already happened is how
        a held input outlives the shutdown. The queued stops are awaited, bounded:
        a dependency that never answers is reported as ``released: False`` rather
        than becoming a shutdown that hangs. An action already in flight is never
        aborted mid-way — a half-applied keystroke is the failure mode "end
        cleanly" rules out.
        """
        stopped: list[str] = []
        for session_id, run in list(self.runs.items()):
            if run.terminal:
                continue
            await self.cancel(session_id, reason="The manager is shutting down.")
            stopped.append(session_id)
        released = await self._await_cleanup(timeout)
        return {"stopped": stopped, "runs": len(self.runs), "released": released}

    async def _await_cleanup(self, timeout: float) -> bool:
        """Wait for the session stops a finished run queued, within a bound."""
        pending = [
            task
            for task in tuple(getattr(self, "_cleanup_tasks", ()))
            if not task.done() and task is not asyncio.current_task()
        ]
        if not pending:
            return True
        _, still = await asyncio.wait(pending, timeout=max(0.05, float(timeout)))
        if not still:
            return True
        # The stop call did not answer inside the bound. Cancelling it is the
        # only way the shutdown returns at all, and saying so beats claiming a
        # release that never happened.
        for task in still:
            task.cancel()
        await asyncio.gather(*still, return_exceptions=True)
        return False

    # ── the loop ───────────────────────────────────────────────────────────
    async def _drive(self, run: AutonomyRun) -> None:
        """One cycle at a time, until a terminal outcome or the operator stops it."""
        session_id = run.session_id
        started = self.clock()
        try:
            while True:
                if self._cancelled(session_id):
                    self._finish(run, AutonomyOutcome.SESSION_CANCELLED, "The operator cancelled the run.")
                    break
                if run.phase in AUTONOMY_TERMINAL_PHASES:
                    break
                if not run.active:
                    await self._wait_or_cancel(session_id, 0.05)
                    continue
                if run.phase in {AutonomyPhase.INITIALIZING, AutonomyPhase.OBSERVING}:
                    ready = await self._initially_ready(run)
                    if ready is not None:
                        self._finish(run, ready[0], ready[1])
                        break
                if self.clock() - started > float(self.limits.session_seconds):
                    self._finish(
                        run,
                        AutonomyOutcome.SESSION_TIMED_OUT,
                        f"The {float(self.limits.session_seconds):g} s session bound was reached.",
                    )
                    break
                if len(run.cycles) >= int(self.limits.max_cycles):
                    self._finish(
                        run,
                        AutonomyOutcome.RESOURCE_LIMIT_REACHED,
                        f"The cycle budget of {int(self.limits.max_cycles)} was spent.",
                    )
                    break
                if run.actions_dispatched >= int(self.limits.max_actions):
                    self._finish(
                        run,
                        AutonomyOutcome.RESOURCE_LIMIT_REACHED,
                        f"The action budget of {int(self.limits.max_actions)} was spent.",
                    )
                    break
                await self._await_pause(session_id, run)
                if run.phase is AutonomyPhase.PAUSED:
                    continue
                outcome, reason = await self._cycle(run)
                if outcome is not None:
                    self._finish(run, outcome, reason)
                    break
        except asyncio.CancelledError:  # pragma: no cover - the cancel path sets the event
            self._finish(run, AutonomyOutcome.SESSION_CANCELLED, "The run task was cancelled.")
            raise
        except Exception as exc:  # noqa: BLE001 - a supervisor fault is a recorded ending
            self._finish(
                run,
                AutonomyOutcome.DEPENDENCY_FAILURE,
                f"The run failed with {type(exc).__name__}: {exc}",
            )
        finally:
            run.elapsed_seconds = round(self.clock() - started, 3)
            self._persist()
            self._tasks.pop(session_id, None)

    async def _initially_ready(self, run: AutonomyRun) -> tuple[AutonomyOutcome, str] | None:
        """Validate the dependencies once, before the first cycle decides anything.

        The completion criteria are pinned HERE, from the first real reading: an
        objective that no rule can interpret means there is nothing to work
        towards, and the run ends before it plans or dispatches anything rather
        than acting without a stated goal.
        """
        if self._cancelled(run.session_id):
            return AutonomyOutcome.SESSION_CANCELLED, "The operator cancelled before the first cycle."
        record = self.gateway.get(run.session_id)
        if record is None:
            return AutonomyOutcome.DEPENDENCY_FAILURE, f"Session {run.session_id!r} no longer exists."
        if not bool(getattr(record, "target_validated", False)):
            return (
                AutonomyOutcome.DEPENDENCY_FAILURE,
                "The session has not validated a target window; an autonomous run never acts on "
                "an unpinned window.",
            )
        if run.phase is AutonomyPhase.INITIALIZING and not run.objective_id:
            observation: dict[str, Any] = {}
            for attempt in range(1, max(1, int(self.limits.observation_attempts)) + 1):
                observation = await self._call(
                    self.gateway.observe_frame_once(),
                    float(self.limits.observation_timeout_seconds),
                    "observation",
                )
                status = str(observation.get("status") or "")
                if status == OBSERVED_STATUS:
                    break
                if status in {OBSERVATION_BUSY_STATUS, OBSERVATION_IDLE_STATUS} and attempt < int(
                    self.limits.observation_attempts
                ):
                    await self._wait_for_observation(run.session_id, status)
                    continue
                failure = self._observation_failure(observation) or (
                    AutonomyOutcome.OBSERVATION_UNAVAILABLE,
                    str(observation.get("reason") or "The dependency check produced no frame."),
                )
                return failure
            mismatch = self._target_mismatch(run, observation)
            if mismatch:
                return AutonomyOutcome.SAFETY_STOP, mismatch
            payload = await self._call(
                self.gateway.read_state(),
                float(self.limits.observation_timeout_seconds),
                "state",
            )
            if not payload.get("analyzed"):
                return (
                    AutonomyOutcome.OBSERVATION_UNAVAILABLE,
                    str(payload.get("reason") or "The first state could not be read."),
                )
            objective = objective_from_picture(self.gateway.objective_picture(run.session_id))
            if not objective:
                return (
                    AutonomyOutcome.DEPENDENCY_FAILURE,
                    "No interpretable objective exists for this game: NF-17 has no rule for its "
                    "state, so there is no completion criterion to run against.",
                )
            run.objective_id = str(objective.get("objective_id") or "")
            run.objective_description = str(
                objective.get("description") or objective.get("source_text") or ""
            )
            run.completion_criteria = [
                str(item) for item in objective.get("completion_conditions") or []
            ]
            run.failure_criteria = [
                str(item) for item in objective.get("failure_conditions") or []
            ]
            run.task = run.task or run.objective_description
            self._record(
                run,
                AutonomyPhase.IDENTIFYING_OBJECTIVE,
                "The objective and its completion criteria were pinned from the first reading: "
                f"{run.objective_description or run.objective_id}",
                detail={
                    "objective_id": run.objective_id,
                    "observation_id": observation.get("observation_id"),
                    "state_id": _mapping(payload.get("state")).get("state_id"),
                },
            )
        elif not run.objective_id:
            return (
                AutonomyOutcome.DEPENDENCY_FAILURE,
                "No interpretable objective exists for this game: NF-17 has no rule for its state, "
                "so there is no completion criterion to run against.",
            )
        if run.phase in {AutonomyPhase.INITIALIZING, AutonomyPhase.IDENTIFYING_OBJECTIVE}:
            self._record(run, AutonomyPhase.OBSERVING, "Dependencies verified; the run begins.")
        return None

    async def _cycle(self, run: AutonomyRun) -> tuple[AutonomyOutcome | None, str]:
        """One observe → understand → objective → plan → approve → act → verify pass.

        Returns a terminal outcome with its reason, or ``(None, "")`` when the run
        should continue (the loop calls this again after a recovery).
        """
        session_id = run.session_id
        cycle = AutonomyCycle(index=len(run.cycles) + 1, started_at=_stamp())
        run.cycles.append(cycle)
        self._counts["cycles"] += 1

        # ── 1/2. observe, and confirm the reading belongs to this session
        observation, failure = await self._observe(run, cycle)
        if failure is not None:
            return failure

        # ── 3. understand the state
        state, failure = await self._understand(run, cycle)
        if failure is not None:
            return failure

        # ── 4/5. the objective and its progress — read ONCE. Reading it twice (once
        # for the transition's wording, once for the decision) let a changed
        # objective slip past the very check that exists to catch it.
        objective = self._refresh_objective(run, cycle)
        self._record(run, AutonomyPhase.IDENTIFYING_OBJECTIVE, cycle.reason)
        changed = self._objective_changed(run, objective)
        if changed:
            return (
                AutonomyOutcome.OUTCOME_UNKNOWN,
                changed,
            )
        if _objective_completed(objective):
            self._record(
                run,
                AutonomyPhase.COMPLETED,
                "NF-17's reading of the fresh state reports the objective completed.",
                # The ending carries its outcome in the trace and in the announced
                # event, exactly as a stop does.
                outcome=AutonomyOutcome.OBJECTIVE_COMPLETED.value,
            )
            cycle.outcome = AutonomyOutcome.OBJECTIVE_COMPLETED.value
            cycle.reason = str(objective.get("reason") or "The objective completed.")
            cycle.ended_at = _stamp()
            return AutonomyOutcome.OBJECTIVE_COMPLETED, cycle.reason
        if _objective_failed(objective):
            self._record(
                run,
                AutonomyPhase.FAILED,
                "NF-17's reading reports the objective failed.",
                outcome=AutonomyOutcome.OBJECTIVE_FAILED.value,
            )
            cycle.outcome = AutonomyOutcome.OBJECTIVE_FAILED.value
            cycle.reason = str(objective.get("reason") or "The objective failed.")
            cycle.ended_at = _stamp()
            return AutonomyOutcome.OBJECTIVE_FAILED, cycle.reason
        if not objective:
            return (
                AutonomyOutcome.OUTCOME_UNKNOWN,
                "The objective could not be interpreted from this state, so there is nothing to "
                "work towards; the run stops rather than guessing.",
            )

        # ── 5b. an action whose outcome was never established is not dispatched
        #        again while the run is still reading the same thing
        repeat = self._unresolved_repeat(run, cycle)
        if repeat:
            return AutonomyOutcome.OUTCOME_UNKNOWN, repeat

        # ── 6. plan
        plan, failure = await self._plan(run, cycle)
        if failure is not None:
            return failure

        # ── 7/8. validate the plan's scope, and get its approval
        approval, failure = await self._approve(run, cycle, plan)
        if failure is not None:
            return failure

        # ── 9. act through NF-19
        executed, failure = await self._act(run, cycle, plan)
        if failure is not None:
            return failure

        # ── 10/11. fresh evidence, then the verdict
        verdict, failure = await self._verify(run, cycle, plan)
        if failure is not None:
            return failure

        # ── 12. continue, recover, or stop
        return await self._recover(run, cycle, verdict)

    async def _observe(
        self, run: AutonomyRun, cycle: AutonomyCycle
    ) -> tuple[dict[str, Any], tuple[AutonomyOutcome, str] | None]:
        session_id = run.session_id
        self._record(run, AutonomyPhase.OBSERVING, "Reading the target before deciding anything.")
        attempts = int(self.limits.observation_attempts)
        last: dict[str, Any] = {}
        for attempt in range(1, attempts + 1):
            if self._cancelled(session_id):
                return {}, (AutonomyOutcome.SESSION_CANCELLED, "The operator cancelled during observation.")
            if attempt > 1:
                self._counts["observation_retries"] = self._counts.get("observation_retries", 0) + 1
                await self._wait_or_cancel(session_id, 0.2)
            last = await self._call(
                self.gateway.observe_frame_once(),
                float(self.limits.observation_timeout_seconds),
                "observation",
            )
            status = str(last.get("status") or "")
            if status == OBSERVED_STATUS:
                break
            if status == OBSERVATION_BUSY_STATUS or status == OBSERVATION_IDLE_STATUS:
                # A frame is already in flight, or the observer is not sampling:
                # waiting is the correct response — never a second capture. The
                # wait must outlast the capture that is running, or every retry
                # meets the same one and reports an absent observation.
                await self._wait_for_observation(session_id, status)
                continue
            if attempt < attempts:
                continue
            failure = self._observation_failure(last) or (
                AutonomyOutcome.OBSERVATION_UNAVAILABLE,
                str(last.get("reason") or "No validated frame was produced."),
            )
            cycle.observation = self._observation_row(last)
            return {}, self._stop_for(run, cycle, failure[0], failure[1])
        if str(last.get("status") or "") != OBSERVED_STATUS:
            reason = (
                f"No validated frame was produced in {attempts} attempt(s); the newest answer was "
                f"{str(last.get('status') or 'nothing')!r} ({str(last.get('reason') or 'no reason given')})"
            )
            cycle.observation = self._observation_row(last)
            return {}, self._stop_for(run, cycle, AutonomyOutcome.OBSERVATION_UNAVAILABLE, reason)
        cycle.observation = self._observation_row(last)
        # The reading must belong to THIS session AND to the window this run was
        # approved for: a frame of another session, or of a replaced window, is
        # not evidence about this run's target.
        session_of_reading = str(last.get("session_id") or "")
        if session_of_reading and session_of_reading != session_id:
            reason = (
                f"The reading belongs to session {session_of_reading!r}, not {session_id!r}; an "
                "autonomous run never acts on another session's evidence."
            )
            return {}, self._stop_for(run, cycle, AutonomyOutcome.SAFETY_STOP, reason)
        mismatch = self._target_mismatch(run, last)
        if mismatch:
            return {}, self._stop_for(run, cycle, AutonomyOutcome.SAFETY_STOP, mismatch)
        return cycle.observation, None

    @staticmethod
    def _observation_row(payload: Mapping[str, Any]) -> dict[str, Any]:
        """The observation facts a trace keeps — never the frame's path."""
        return {
            "observation_id": payload.get("observation_id"),
            "session_id": payload.get("session_id"),
            "seq": payload.get("seq"),
            "captured_at": payload.get("captured_at"),
            "status": payload.get("status"),
            "reason_code": payload.get("reason_code"),
            "reason": payload.get("reason"),
            "width": payload.get("width"),
            "height": payload.get("height"),
            "backend": payload.get("backend"),
            "fresh": payload.get("fresh"),
            "target_valid": payload.get("target_valid"),
            "target": {
                key: _mapping(payload.get("target")).get(key)
                for key in ("title", "handle", "pid")
            },
        }

    @staticmethod
    def _target_mismatch(run: AutonomyRun, payload: Mapping[str, Any]) -> str:
        """Why this frame is not the approved window's, or an empty string."""
        pinned = run.target
        seen = _mapping(payload.get("target"))
        if not pinned or not seen:
            return ""
        for key in ("handle", "pid"):
            expected = str(pinned.get(key) or "")
            actual = str(seen.get(key) or "")
            if expected and actual and expected != actual:
                return (
                    f"The validated frame belongs to {key} {actual!r} and this run was approved "
                    f"for {key} {expected!r}; the run stops rather than acting on another window."
                )
        return ""

    def _observation_failure(self, payload: Mapping[str, Any]) -> tuple[AutonomyOutcome, str] | None:
        """What a refused observation MEANS for the run, using the observer's codes.

        The observer's own vocabulary is the input here: a window that cannot be
        seen, or that is no longer the pinned one, is a safety stop; a frame that
        failed validation is a missing reading; an explicit stop is a cancel.
        """
        code = str(payload.get("reason_code") or payload.get("status") or "")
        reason = str(payload.get("reason") or "The observation did not validate.")
        if code == "stopped":
            return AutonomyOutcome.SESSION_CANCELLED, (
                f"The observation stopped mid-read; the run ends with it: {reason}"
            )
        if code in TARGET_UNSEEN_CODES:
            return AutonomyOutcome.SAFETY_STOP, (
                f"The target window cannot be seen ({code}): {reason} An autonomous run pauses "
                "rather than acting on a frame it cannot trust."
            )
        if code in TARGET_IDENTITY_CODES:
            return AutonomyOutcome.SAFETY_STOP, (
                f"The frame no longer belongs to the pinned target ({code}): {reason}"
            )
        # The observer's own code travels with the message: a reason the operator
        # cannot look up is a reason they cannot act on.
        return AutonomyOutcome.OBSERVATION_UNAVAILABLE, (
            f"The observation produced no usable frame ({code or 'unavailable'}): {reason}"
        )

    async def _understand(
        self, run: AutonomyRun, cycle: AutonomyCycle
    ) -> tuple[dict[str, Any], tuple[AutonomyOutcome, str] | None]:
        self._record(run, AutonomyPhase.UNDERSTANDING_STATE, "Interpreting the reading into a state.")
        payload = await self._call(
            self.gateway.read_state(),
            float(self.limits.observation_timeout_seconds),
            "state",
        )
        cycle.state = {k: v for k, v in payload.items() if k != "state"}
        analyzed = bool(payload.get("analyzed"))
        if not analyzed:
            code = str(payload.get("reason_code") or "")
            reason = str(payload.get("reason") or "The state could not be read.")
            if code in {"no_validated_frame", "frame_unavailable"}:
                return self._pause_for(run, cycle, AutonomyOutcome.OBSERVATION_UNAVAILABLE, reason)
            if code == "observation_stale":
                return self._pause_for(run, cycle, AutonomyOutcome.OBSERVATION_UNAVAILABLE, reason)
            if code in {"observer_unavailable", "analysis_timeout", "analysis_failed"}:
                return self._pause_for(run, cycle, AutonomyOutcome.DEPENDENCY_FAILURE, reason)
            return self._pause_for(run, cycle, AutonomyOutcome.OUTCOME_UNKNOWN, reason)
        state = _mapping(payload.get("state"))
        cycle.state = {
            "state_id": state.get("state_id"),
            "status": state.get("status"),
            "sequence": state.get("sequence"),
            "game": state.get("game"),
            "observation_id": _mapping(state.get("observation")).get("observation_id"),
            # The CHANGES and the fields the reading could NOT establish are kept:
            # they are the difference between "nothing happened" and "nothing was
            # read", which an autonomous run must never confuse.
            "changes": [
                {
                    "kind": row.get("kind"),
                    "element": row.get("element_id"),
                    "before": row.get("before"),
                    "after": row.get("after"),
                }
                for row in _rows(state.get("changes"))
            ],
            "unknown": [
                {
                    "field": row.get("field_name"),
                    "reason": row.get("reason"),
                }
                for row in _rows(state.get("unknown"))
            ],
            "hud": [
                {
                    "element": row.get("element_id"),
                    "status": row.get("status"),
                    "value": row.get("value"),
                    "text": row.get("text"),
                }
                for row in _rows(state.get("hud"))
            ],
            "profile": _mapping(state.get("profile")).get("profile_id"),
            "confidence": state.get("confidence"),
            "reasons": [str(item) for item in state.get("reasons") or []],
        }
        # The SCENE the state layer understood travels with the cycle: NF-25's
        # judge derives its `scene:<kind>` evidence token from this row, and
        # NF-26 records it as the observed transition. Dropping it here made a
        # required `scene:` tag unsatisfiable on a real run with no error. A
        # reading that named no scene records no key, rather than an empty claim.
        scene_row = _mapping(state.get("scene"))
        if scene_row:
            cycle.state["scene"] = scene_row
        return cycle.state, None

    def _pause_for(
        self,
        run: AutonomyRun,
        cycle: AutonomyCycle,
        outcome: AutonomyOutcome,
        reason: str,
    ) -> tuple[dict[str, Any], tuple[AutonomyOutcome, str]]:
        """A reading the run cannot use ENDS it: re-reading a dead pipeline is a loop.

        The phase's rule is explicit — pause on invalid or stale observations — and
        an autonomous run that kept asking would spend its whole budget on a screen
        nobody can read.
        """
        self._record(run, AutonomyPhase.FAILED, reason, outcome=outcome.value)
        cycle.outcome = outcome.value
        cycle.reason = reason
        cycle.ended_at = _stamp()
        return {}, (outcome, reason)

    def _refresh_objective(self, run: AutonomyRun, cycle: AutonomyCycle) -> dict[str, Any]:
        objective = objective_from_picture(self.gateway.objective_picture(run.session_id))
        cycle.objective = objective
        progress = _progress_line(objective)
        cycle.reason = " ".join(
            part for part in (str(objective.get("description") or ""), progress) if part
        ) or "Reading the objective and its progress from the fresh state."
        # The objective's own evidence stays where it is (inside ``cycle.objective``):
        # it is a reference to the READING the objective came from, not to
        # retrieved knowledge. Copying it into ``knowledge_refs`` filled the audit
        # trail with blank entries and named no knowledge at all.
        return objective

    @staticmethod
    def _evidence_fingerprint(cycle: AutonomyCycle) -> tuple[str, str]:
        """What the run was reading when it acted: the observation and the state."""
        observation = _mapping(cycle.observation)
        state = _mapping(cycle.state)
        return (
            str(observation.get("observation_id") or ""),
            str(state.get("state_id") or ""),
        )

    def _unresolved_repeat(self, run: AutonomyRun, cycle: AutonomyCycle) -> str:
        """Why this cycle must not act, or an empty string.

        The phase's rule is that an action whose outcome is unknown is never
        repeated. Repeating it on a DIFFERENT reading is not a repetition: the new
        evidence is what the recovery asked for, and it earns another attempt. On
        the SAME reading there is nothing new to act on, so the run ends as an
        unknown outcome rather than pressing again and calling it progress.
        """
        pending = tuple(str(item) for item in run.unresolved_evidence[:2])
        if not pending:
            return ""
        if len(pending) != 2:
            # A half-written latch from an older record is not evidence of
            # anything: drop it rather than block a healthy run.
            run.unresolved_evidence = []
            return ""
        current = self._evidence_fingerprint(cycle)
        if current != pending:
            run.unresolved_evidence = []
            return ""
        return (
            "The previous action's effect was never established and the run is looking at the "
            f"same reading (observation {current[0] or 'unknown'}, state "
            f"{current[1] or 'unknown'}); it stops rather than repeating an action whose result "
            "it could not read."
        )

    @staticmethod
    def _objective_changed(run: AutonomyRun, objective: Mapping[str, Any]) -> str:
        """Why a DIFFERENT objective ends the run, or an empty string.

        The run's approval is for one objective. A different one is a different
        task: its completion criteria were never approved, so continuing would be
        working towards something nobody agreed to.
        """
        current = str(_mapping(objective).get("objective_id") or "")
        if not current or not run.objective_id or current == run.objective_id:
            return ""
        return (
            f"The objective changed from {run.objective_id!r} to {current!r}; this run was "
            "approved for one objective, so a new plan needs a new approval."
        )

    async def _plan(
        self, run: AutonomyRun, cycle: AutonomyCycle
    ) -> tuple[dict[str, Any], tuple[AutonomyOutcome, str] | None]:
        self._record(run, AutonomyPhase.PLANNING, "Building one bounded plan from fresh evidence.")
        payload = await self._call(
            self.gateway.plan_actions(run.session_id),
            float(self.limits.planning_timeout_seconds),
            "planning",
        )
        plan = _mapping(payload.get("plan"))
        cycle.plan = plan
        # NF-18's retrieved knowledge, as NF-20 itself names it: the verified
        # records this plan's steps were built from. An empty list is the honest
        # answer when no verified record matched — the field says what was USED.
        cycle.knowledge_refs = [
            str(item) for item in plan.get("knowledge_refs") or [] if str(item)
        ]
        status = str(plan.get("status") or "")
        if status not in {"awaiting_approval", "planned"}:
            # NF-20's own refusal vocabulary decides what this run does next. A
            # refusal is not a failure to retry: it is an answer about the evidence.
            # A dependency that never answered leaves no plan at all: its code and
            # its reason travel on the PAYLOAD, and dropping them would report a
            # planner timeout as an unclassifiable unknown.
            code = str(plan.get("code") or payload.get("reason_code") or "")
            reason = str(
                plan.get("reason") or payload.get("reason") or "The planner did not produce a plan."
            )
            if code == "objective_progress_complete":
                reason = (
                    "NF-20 planned nothing because its own reading of the progress counter is "
                    "already at its target, and that is not a verdict about the objective: "
                    f"{reason} The run stops rather than claiming a success nobody verified."
                )
            return {}, self._stop_for(run, cycle, self._plan_outcome(code), reason)
        steps = _rows(plan.get("steps"))
        budget = int(run.limits.get("max_actions_per_plan") or self.limits.max_actions_per_plan)
        if len(steps) > budget:
            # The approved scope is a promise about what may run: a plan above it
            # stops the run instead of quietly widening the budget.
            return {}, self._stop_for(
                run,
                cycle,
                AutonomyOutcome.RESOURCE_LIMIT_REACHED,
                f"The plan holds {len(steps)} step(s) and the approved per-plan budget is "
                f"{budget}; the run stops rather than widening a limit it was approved with.",
            )
        if not steps:
            # Nothing was proposed, so nothing about the objective was established.
            # An empty plan is an unknown outcome, never a verdict on the objective:
            # a run that reports OBJECTIVE_FAILED here claims a failure it has no
            # evidence for, which is the mirror of claiming an unverified success.
            return {}, self._stop_for(
                run,
                cycle,
                AutonomyOutcome.OUTCOME_UNKNOWN,
                "The planner produced a plan with no steps, so nothing was run and nothing "
                "about the objective was established.",
            )
        return plan, None

    @staticmethod
    def _plan_outcome(code: str) -> AutonomyOutcome:
        """What a planner refusal means to an autonomous run.

        The mapping is deliberately conservative: an unreadable or stale reading
        ends the run (nothing safe to act on), a gate refusal is a safety stop, an
        exhausted budget is a limit, an objective whose counter already reached
        its declared target is the one refusal that IS a completion, and anything
        this build cannot act on is an honest unknown rather than a failure.
        """
        if code == "objective_progress_complete":
            # NF-20 refuses because its OWN reading of the counter is at target —
            # and NF-20 says so itself: "completion is still the verifier's
            # verdict, not this planner's". NF-17 is the authority on completion,
            # and by the time a plan is built it has not claimed one (a claimed
            # completion ends the run before planning); so this refusal is an
            # unknown outcome, never a success the run could not verify.
            return AutonomyOutcome.OUTCOME_UNKNOWN
        if code == "no_action_budget":
            return AutonomyOutcome.RESOURCE_LIMIT_REACHED
        if code in {"planning_timeout", "candidate_generation_failed"} | DEPENDENCY_CODES:
            return AutonomyOutcome.DEPENDENCY_FAILURE
        if code.startswith("gate_"):
            # Every gate refusal is one thing: the control interface said no, and
            # the run stops instead of working around it. The gate's own name
            # travels in the reason, not in the outcome.
            return AutonomyOutcome.SAFETY_STOP
        if code == "wrong_session_state":
            return AutonomyOutcome.SAFETY_STOP
        if code in {
            "no_state",
            "stale_state",
            "unknown_state_age",
            "no_objective_interpretation",
            "objective_stale",
        } or code.startswith("state_"):
            return AutonomyOutcome.OBSERVATION_UNAVAILABLE
        return AutonomyOutcome.OUTCOME_UNKNOWN

    async def _approve(
        self, run: AutonomyRun, cycle: AutonomyCycle, plan: Mapping[str, Any]
    ) -> tuple[dict[str, Any], tuple[AutonomyOutcome, str] | None]:
        approval = {
            "mode": self.policy.plan_approval,
            "run_approval": run.approved,
            "approved_at": run.approved_at,
            "plan_id": str(plan.get("plan_id") or ""),
        }
        if self.policy.plan_approval == "run":
            approval["approved"] = True
            approval["reason"] = (
                "The run's approval covers this plan: it is inside the objectives and budgets the "
                "operator approved before the first cycle."
            )
            cycle.approval = approval
            return approval, None
        self._approvals.pop(run.session_id, None)
        event = self._approval_events.setdefault(run.session_id, asyncio.Event())
        event.clear()
        # The answer slot is prepared BEFORE the parking phase is announced: the
        # operator can answer the moment it is visible, and clearing the slot
        # afterwards threw that answer away and parked the run forever.
        self._record(
            run,
            AutonomyPhase.WAITING_FOR_APPROVAL,
            "The policy approves each plan separately; waiting for the operator.",
        )
        while not event.is_set():
            if self._cancelled(run.session_id):
                return {}, self._stop_for(
                    run, cycle, AutonomyOutcome.SESSION_CANCELLED, "The operator cancelled while waiting for approval."
                )
            await self._wait_or_cancel(run.session_id, 0.05)
        answer = dict(self._approvals.pop(run.session_id, {}))
        approval.update(answer)
        cycle.approval = approval
        if not answer.get("approved"):
            # A RECORDED refusal is the operator's own decision to stop the run,
            # which is a cancellation — nothing about it is unknown. An answer
            # that arrived with no decision in it is the genuinely unknown case:
            # silence is never read as permission.
            refused = "approved" in answer
            if refused:
                reason = str(answer.get("note") or "The operator refused this plan; the run stops.")
            else:
                reason = (
                    "The approval channel answered with no decision, so the run stops rather "
                    "than assuming one."
                )
            return {}, self._stop_for(
                run,
                cycle,
                AutonomyOutcome.SESSION_CANCELLED if refused else AutonomyOutcome.OUTCOME_UNKNOWN,
                reason,
            )
        return approval, None

    async def _act(
        self, run: AutonomyRun, cycle: AutonomyCycle, plan: Mapping[str, Any]
    ) -> tuple[dict[str, Any], tuple[AutonomyOutcome, str] | None]:
        self._record(run, AutonomyPhase.EXECUTING, "Executing the approved plan through NF-19.")
        plan_id = str(plan.get("plan_id") or "")
        payload = await self._call(
            self.gateway.execute_plan(run.session_id, {"plan_id": plan_id, "approved": True}),
            float(self.limits.action_timeout_seconds),
            "execution",
        )
        execution = _mapping(payload.get("plan")) or payload
        records = _rows(_mapping(execution.get("execution")).get("steps"))
        cycle.execution = {
            "plan_id": plan_id,
            "status": execution.get("status"),
            "code": execution.get("code"),
            "reason": execution.get("reason"),
            # One record per submitted step; ``ok`` is the INPUT operation's own
            # contract — never a gameplay effect.
            "steps": [
                {
                    "index": row.get("index"),
                    "action_id": row.get("action_id"),
                    "kind": row.get("kind"),
                    "ok": bool(row.get("ok")),
                    "status": row.get("status"),
                    "code": row.get("code"),
                    "reason": row.get("reason"),
                    "duration_ms": row.get("duration_ms"),
                }
                for row in records
            ],
            "effect_verified": bool(payload.get("effect_verified")),
        }
        dispatched = sum(1 for row in records if bool(row.get("ok")))
        cycle.actions_dispatched = dispatched
        run.actions_dispatched += dispatched
        self._counts["actions"] += dispatched
        status = str(execution.get("status") or "")
        code = str(execution.get("code") or "")
        if dispatched == 0:
            reason = str(execution.get("reason") or "The plan did not execute.")
            if code in REFUSAL_CODES:
                return {}, self._stop_for(run, cycle, AutonomyOutcome.SAFETY_STOP, reason)
            if code == "unknown_plan":
                # The layer that owns plans does not know the plan this run holds:
                # that is a stage mismatch, and it says nothing about the game. It
                # is emphatically NOT an objective failure — nothing was sent.
                return {}, self._stop_for(
                    run,
                    cycle,
                    AutonomyOutcome.DEPENDENCY_FAILURE,
                    f"Nothing was sent: {reason}",
                )
            if code == "no_steps":
                return {}, self._stop_for(
                    run,
                    cycle,
                    AutonomyOutcome.OUTCOME_UNKNOWN,
                    f"Nothing was sent and nothing about the objective was established: {reason}",
                )
            return {}, self._stop_for(run, cycle, AutonomyOutcome.DEPENDENCY_FAILURE, reason)
        if status in {"stopped", "cancelled", "replan_required", "invalidated"}:
            # The plan invalidated itself mid-way: the dispatched part still gets
            # verified (the effects are real), then the outcome decides.
            return cycle.execution, None
        return cycle.execution, None

    async def _verify(
        self, run: AutonomyRun, cycle: AutonomyCycle, plan: Mapping[str, Any]
    ) -> tuple[dict[str, Any], tuple[AutonomyOutcome, str] | None]:
        self._record(run, AutonomyPhase.VERIFYING, "Checking the action's effect against fresh evidence.")
        plan_id = str(plan.get("plan_id") or "")
        payload = await self._call(
            self.gateway.verify_outcomes(run.session_id, {"plan_id": plan_id}),
            float(self.limits.verification_timeout_seconds),
            "verification",
        )
        verification = _mapping(payload.get("verification"))
        if not verification and str(payload.get("reason_code") or "") in DEPENDENCY_CODES:
            # The verdict was never produced: that is a failed dependency, not an
            # unknown outcome about the game. Saying so is what keeps the report
            # honest about who did not answer.
            return {}, self._stop_for(
                run,
                cycle,
                AutonomyOutcome.DEPENDENCY_FAILURE,
                str(payload.get("reason") or "The verification stage did not answer."),
            )
        cycle.verification = {
            "status": verification.get("status"),
            "code": verification.get("code"),
            "reason": verification.get("reason"),
            "effect_verified": bool(verification.get("effect_verified")),
            "outcomes": [
                {
                    "outcome_id": row.get("outcome_id"),
                    "action_id": row.get("action_id"),
                    "status": row.get("status"),
                    "expected_effect": row.get("expected_effect"),
                    "observed_effect": row.get("observed_effect"),
                    "code": row.get("code"),
                    "reason": row.get("reason"),
                    "confidence": row.get("confidence"),
                    "confidence_basis": row.get("confidence_basis"),
                    "ambiguity": row.get("ambiguity"),
                    "method": row.get("method"),
                    "observations": row.get("observations"),
                    # The evidence that was ACCEPTED, and the readings that were
                    # rejected with their reason: an audit needs both halves.
                    "evidence": [
                        {
                            "ref": ref.get("ref"),
                            "observation_id": ref.get("observation_id"),
                            "state_id": ref.get("state_id"),
                            "accepted": ref.get("accepted"),
                            "age_seconds": ref.get("age_seconds"),
                            "code": ref.get("code"),
                            "reason": ref.get("reason"),
                        }
                        for ref in _rows(row.get("evidence"))
                    ],
                    "rejected": [
                        {
                            "observation_id": ref.get("observation_id"),
                            "code": ref.get("code"),
                            "reason": ref.get("reason"),
                            "age_seconds": ref.get("age_seconds"),
                        }
                        for ref in _rows(row.get("rejected"))
                    ],
                    "recovery": dict(_mapping(row.get("recovery"))),
                }
                for row in _rows(verification.get("outcomes"))
            ],
        }
        for row in verification.get("evidence_refs") or []:
            if isinstance(row, str) and row:
                run.verification_evidence.append(row)
        for outcome in cycle.verification["outcomes"]:
            for ref in outcome["evidence"]:
                # Both halves travel: the state the verdict was read against AND the
                # observation it was taken from. A reference alone cannot be
                # re-checked, and an observation id alone does not say what was read.
                for token in (ref.get("ref"), ref.get("observation_id"), ref.get("state_id")):
                    text = str(token or "")
                    if text and text not in run.verification_evidence:
                        run.verification_evidence.append(text)
        return cycle.verification, None

    async def _recover(
        self, run: AutonomyRun, cycle: AutonomyCycle, verdict: Mapping[str, Any]
    ) -> tuple[AutonomyOutcome | None, str]:
        """NF-21's decision, carried out inside this run's own bounds."""
        outcomes = _rows(verdict.get("outcomes"))
        if not outcomes:
            return (
                AutonomyOutcome.OUTCOME_UNKNOWN,
                str(verdict.get("reason") or "The verification produced no outcome to act on."),
            )
        if bool(verdict.get("effect_verified")) and all(
            str(_mapping(row).get("status") or "") == "verified_success" for row in outcomes
        ):
            cycle.outcome = "verified_success"
            cycle.reason = "Every dispatched step's declared effect was observed."
            cycle.ended_at = _stamp()
            # The action's effect IS known, so nothing is outstanding: a later
            # action of the same shape is ordinary gameplay, not a repetition.
            run.unresolved_evidence = []
            return None, ""
        # The outcome was NOT established. Remember the reading that was in force
        # while this action ran: a later plan may not repeat it until the world
        # shows something this run has not already seen.
        run.unresolved_evidence = list(self._evidence_fingerprint(cycle))
        # Only the decisions NF-21 recorded as ALLOWED are carried out: a decision
        # that was already blocked (its own bound was reached, or the response is
        # not permitted) is not an instruction, and re-attempting it is the loop
        # this phase forbids.
        decided = [
            (row, _mapping(row.get("recovery")))
            for row in outcomes
            if str(_mapping(row.get("recovery")).get("action") or "none") != "none"
        ]
        actions = {
            str(decision.get("action") or "")
            for _, decision in decided
            if decision.get("allowed") is not False
        }
        reason = "; ".join(
            str(row.get("reason") or row.get("status") or "") for row in outcomes
        )
        if not actions:
            return (
                AutonomyOutcome.OUTCOME_UNKNOWN,
                f"NF-21 recorded no permitted recovery for this outcome: {reason}",
            )
        if run.recoveries >= int(self.limits.recovery_attempts):
            return (
                AutonomyOutcome.OUTCOME_UNKNOWN,
                f"The recovery budget of {int(self.limits.recovery_attempts)} was spent with the "
                f"effect unestablished: {reason}",
            )
        for action in ("stop_operation", "pause_session"):
            if action in actions:
                return (
                    AutonomyOutcome.SAFETY_STOP,
                    f"NF-21's recovery decision says to stop or pause: {reason}",
                )
        if "request_operator" in actions:
            return (
                AutonomyOutcome.OUTCOME_UNKNOWN,
                f"NF-21 escalated the outcome to a person rather than retrying it: {reason}",
            )
        if "replan" in actions:
            if run.replans >= int(self.limits.replans):
                return (
                    AutonomyOutcome.OUTCOME_UNKNOWN,
                    f"The replan budget of {int(self.limits.replans)} was spent: {reason}",
                )
            run.replans += 1
        target = next(
            (
                row
                for row, decision in decided
                if decision.get("allowed") is not False
                and str(decision.get("action") or "")
                in {
                    "refresh_observation",
                    "wait_for_transition",
                    "reevaluate_state",
                    "invalidate_plan",
                    "replan",
                }
            ),
            None,
        )
        if target is None:
            return (
                AutonomyOutcome.OUTCOME_UNKNOWN,
                f"The outcome was not established and no bounded recovery applies: {reason}",
            )
        action = str(_mapping(target.get("recovery")).get("action") or "")
        outcome_id = str(target.get("outcome_id") or "")
        self._record(
            run,
            AutonomyPhase.RECOVERING,
            f"Applying NF-21's bounded recovery ({action}).",
            detail={"outcome_id": outcome_id, "action": action},
        )
        applied = await self._call(
            self.gateway.recover_outcome(
                run.session_id, {"outcome_id": outcome_id, "approve": action == "replan"}
            ),
            float(self.limits.verification_timeout_seconds),
            "recovery",
        )
        run.recoveries += 1
        self._counts["recoveries"] += 1
        cycle.recovery = {
            "action": action,
            "outcome_id": outcome_id,
            "applied": bool(applied.get("applied")),
            "code": applied.get("code"),
            "reason": applied.get("reason"),
            "result": applied.get("result"),
        }
        cycle.outcome = str(applied.get("code") or action)
        cycle.reason = str(applied.get("reason") or "")
        cycle.ended_at = _stamp()
        if not applied.get("applied"):
            return (
                AutonomyOutcome.OUTCOME_UNKNOWN,
                f"NF-21's recovery could not be carried out ({applied.get('code')}): "
                f"{applied.get('reason')}",
            )
        return None, ""

    # ── helpers ────────────────────────────────────────────────────────────
    async def _call(
        self, awaitable: Awaitable[Any], timeout: float, stage: str
    ) -> dict[str, Any]:
        """Await one gateway call inside its own bound, reporting a refusal."""
        try:
            payload = await asyncio.wait_for(awaitable, timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            return {
                "status": "timeout",
                "reason_code": "dependency_timeout",
                "reason": f"The {stage} stage did not answer within {timeout:g} s.",
            }
        except Exception as exc:  # noqa: BLE001 - a dependency fault is a result, not a crash
            return {
                "status": "failed",
                "reason_code": "dependency_failed",
                "reason": f"The {stage} stage raised {type(exc).__name__}: {exc}",
            }
        return _mapping(payload)

    async def _safe(self, awaitable: Awaitable[Any], stage: str) -> dict[str, Any]:
        """A lifecycle call whose failure must not hide the operator's intent."""
        try:
            payload = await awaitable
        except Exception as exc:  # noqa: BLE001 - reported, never raised at the caller
            return {"ok": False, "reason": f"{stage} raised {type(exc).__name__}: {exc}"}
        return _mapping(payload)

    def _probe_resources(self) -> dict[str, Any]:
        if self.resource_probe is None:
            return {
                "available": False,
                "allow": None,
                "reason": "No resource gate is wired on this host; nothing was measured.",
            }
        try:
            payload = _mapping(self.resource_probe())
        except Exception as exc:  # noqa: BLE001 - an unreadable gate is not a refusal
            return {
                "available": False,
                "allow": None,
                "reason": f"The resource probe raised {type(exc).__name__}: {exc}",
            }
        return payload

    def _conflicting_run(self, session_id: str, record: Any) -> AutonomyRun | None:
        """Another ACTIVE run on the same game or the same window, or None."""
        target = self._target_of(record)
        handle = str(target.get("handle") or "")
        pid = str(target.get("pid") or "")
        title = str(target.get("title") or "")
        for run in self.runs.values():
            if run.session_id == session_id or run.terminal:
                continue
            other = run.target
            if handle and str(other.get("handle") or "") == handle:
                return run
            if pid and str(other.get("pid") or "") == pid:
                return run
            if title and str(other.get("title") or "") == title:
                return run
        return None

    @staticmethod
    def _target_of(record: Any) -> dict[str, Any]:
        target = getattr(record, "target", None)
        if target is None:
            return {}
        return {
            "title": str(getattr(target, "title", "") or ""),
            "handle": str(getattr(target, "handle", "") or ""),
            "pid": str(getattr(target, "pid", "") or ""),
        }

    async def _await_pause(self, session_id: str, run: AutonomyRun) -> None:
        """Park at a checkpoint while paused, and re-observe once resumed.

        Both directions live here so a resume that arrives while a dependency call
        was in flight cannot leave the run parked with nothing to wake it.
        """
        event = self._pause_events.get(session_id)
        if event is None or event.is_set():
            if run.phase is AutonomyPhase.PAUSED:
                self._record(run, AutonomyPhase.OBSERVING, "The run was resumed; observing again.")
            return
        if run.phase is not AutonomyPhase.PAUSED:
            self._record(run, AutonomyPhase.PAUSED, "The operator paused the run.")

    async def _wait_for_observation(self, session_id: str, status: str) -> None:
        """Wait the amount of time the observer's own answer implies."""
        await self._wait_or_cancel(
            session_id,
            CAPTURE_IN_FLIGHT_WAIT_SECONDS
            if status == OBSERVATION_BUSY_STATUS
            else OBSERVATION_IDLE_WAIT_SECONDS,
        )

    async def _wait_or_cancel(self, session_id: str, seconds: float) -> None:
        """A bounded wait that a cancel can interrupt (never ``time.sleep``)."""
        if self._cancelled(session_id):
            return
        await self.sleep(seconds)

    @staticmethod
    def _running_loop() -> asyncio.AbstractEventLoop | None:
        """The loop work may be announced on, or ``None`` outside one."""
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def _cancelled(self, session_id: str) -> bool:
        event = self._cancel_events.get(session_id)
        return bool(event is not None and event.is_set())

    def _record(
        self,
        run: AutonomyRun,
        phase: AutonomyPhase,
        reason: str = "",
        *,
        outcome: str = "",
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Move the run to ``phase``, or RAISE if that move is not legal."""
        if phase is not run.phase:
            allowed = AUTONOMY_TRANSITIONS.get(run.phase, frozenset())
            if phase not in allowed:
                raise AutonomyTransitionError(
                    f"{run.phase.value} -> {phase.value} is not a legal autonomous transition."
                )
            run.phase = phase
        step = AutonomyStep(
            at=_stamp(),
            phase=phase.value,
            reason=reason,
            cycle=len(run.cycles),
            outcome=outcome,
            detail=dict(detail or {}),
        )
        run.steps.append(step)
        # A supervisor's announcement must never fail the work it announces, and a
        # step recorded outside a running loop (a synchronous caller) schedules
        # nothing rather than creating a coroutine nobody awaits.
        loop = self._running_loop()
        if loop is not None and self.publish is not None:
            loop.create_task(self._announce(run, step))
        self._persist()

    async def _announce(self, run: AutonomyRun, step: AutonomyStep) -> None:
        if self.publish is None:
            return
        try:
            await self.publish(
                "novaforge.autonomy.phase",
                {
                    "session_id": run.session_id,
                    "run_id": run.run_id,
                    "phase": step.phase,
                    "reason": step.reason,
                    "cycle": step.cycle,
                    "outcome": step.outcome,
                    "evidence_source": run.evidence_source,
                    "actions_dispatched": run.actions_dispatched,
                },
            )
        except Exception:  # noqa: BLE001 - a lost event is not a run failure
            return

    def _stop_for(
        self,
        run: AutonomyRun,
        cycle: AutonomyCycle,
        outcome: AutonomyOutcome,
        reason: str,
    ) -> tuple[AutonomyOutcome, str]:
        self._record(run, terminal_phase_for(outcome), reason, outcome=outcome.value)
        cycle.outcome = outcome.value
        cycle.reason = reason
        cycle.ended_at = _stamp()
        return outcome, reason

    def _finish(self, run: AutonomyRun, outcome: AutonomyOutcome, reason: str) -> None:
        """End the run, then clean up through the session's own interfaces.

        A cycle that already recorded its own ending (a safety stop, an
        unreadable reading, a completed objective) arrives here with the phase
        already terminal. That ending is recorded once — but the reason, the end
        stamp and the session cleanup are never skipped: a terminal run that does
        not say why it ended, and a session whose held keys were never released,
        is exactly what this phase forbids.
        """
        if not run.terminal:
            self._record(run, terminal_phase_for(outcome), reason, outcome=outcome.value)
        if not run.outcome:
            run.outcome = outcome.value
        if not run.ended_reason:
            run.ended_reason = reason
        if not run.ended_at:
            run.ended_at = _stamp()
        self._persist()
        if run.run_id in self._ended:
            return
        known = {item.run_id for item in self.runs.values()}
        self._ended = {run_id for run_id in self._ended if run_id in known} | {run.run_id}
        self._counts["outcomes"] = self._counts.get("outcomes", 0) + 1
        loop = self._running_loop()
        if self.policy.stop_session_on_terminal and loop is not None:
            task = loop.create_task(self._end_session(run))
            self._cleanup_tasks = getattr(self, "_cleanup_tasks", [])
            self._cleanup_tasks.append(task)

    async def _end_session(self, run: AutonomyRun) -> None:
        """Stop the session (which releases held inputs) and record what happened."""
        try:
            payload = await self.gateway.stop(
                run.session_id,
                reason=f"autonomous run ended: {run.outcome}",
            )
        except Exception as exc:  # noqa: BLE001 - reported, never claimed as a cleanup
            run.session_ended = {"ok": False, "reason": f"stop raised {type(exc).__name__}: {exc}"}
            self._persist()
            return
        run.session_ended = _mapping(payload)
        self._persist()
