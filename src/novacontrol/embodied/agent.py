"""The embodied session loop — observe, decide, act, verify, recover, remember.

There is exactly ONE loop in this phase and it is this one. It does not plan:
:class:`~novacontrol.planning.goal_manager.GoalManager` plans. It does not
execute inputs: the application's executor executes them, through the step
handler this package registers. It does not decide what is allowed: the action
policy and the permission layer do. What the loop owns is the SESSION: the order
those things are asked in, and the state that remembers what happened.

Every cycle is the same seven steps, and each one can refuse:

1. **Observe.** A fresh reading, or a re-read when the first was stale. A stale
   reading is REPORTED and never used to authorize an attempt.
2. **Update the world model.** Structured facts become observed facts; regions
   stay regions.
3. **Check the objective.** Only a fresh observation that shows every success
   condition holding ends the session as COMPLETED.
4. **Choose.** The tactical layer names the next subgoal; Phase 24 turns it into
   validated, ordered, authorized tasks.
5. **Act.** Through the executor, with the fresh-state gate re-grounding the
   preconditions immediately before the input goes out.
6. **Verify.** A fresh observation after the input, compared against the
   declared effect. An ambiguous result is NOT a success — and a timed-out input
   is never blindly re-sent, because the first one may already have landed.
7. **Recover or remember.** A blocked attempt is remembered so it is not
   repeated; a failed attempt consumes one of the session's own recovery
   attempts. When those run out the session stops and says why.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from novacontrol.embodied.environment import EnvironmentAdapter, ReleaseReport
from novacontrol.embodied.models import (
    ActionKind,
    EnvironmentAction,
    Objective,
    ObjectiveVerdict,
    Observation,
    SessionState,
    SessionStatus,
    TrajectoryEntry,
    validate_objective,
)
from novacontrol.embodied.perception import (
    FACT_PREFIX,
    effect_observed,
    same_value,
    to_world_observation,
)
from novacontrol.embodied.tactical import (
    TacticalChoice,
    TacticalMemory,
    choose,
    read_position,
    tasks_for,
)

#: The event types this loop publishes through its observer seam.
AGENT_SESSION_STARTED = "embodied.session_started"
AGENT_SESSION_PAUSED = "embodied.session_paused"
AGENT_SESSION_RESUMED = "embodied.session_resumed"
AGENT_SESSION_CANCELLED = "embodied.session_cancelled"
AGENT_SESSION_ENDED = "embodied.session_ended"
AGENT_OBSERVATION = "embodied.observation"
AGENT_ACTION_SELECTED = "embodied.action_selected"
AGENT_ACTION_EXECUTED = "embodied.action_executed"
AGENT_ACTION_FAILED = "embodied.action_failed"
AGENT_RECOVERY = "embodied.recovery"
AGENT_OBJECTIVE_VERIFIED = "embodied.objective_verified"
AGENT_OBJECTIVE_FAILED = "embodied.objective_failed"
AGENT_BLOCKED = "embodied.blocked"

#: How many fresh readings to try before calling the observation stale.
DEFAULT_REFRESH_ATTEMPTS = 2

#: The trajectory is bounded: an agent that runs for an hour must not grow an
#: unbounded record of its own small steps.
DEFAULT_MAX_TRAJECTORY = 200

#: What the loop is willing to attempt before it stops acting on its own.
DEFAULT_MAX_ACTIONS = 40


@dataclass
class EmbodiedAgent:
    """One session's brain: a bounded loop with a memory and a scoreboard."""

    state: SessionState
    adapter: EnvironmentAdapter
    #: Phase 24. ``None`` means this build has no planner wired, which the
    #: session reports as a refusal rather than falling back to a private loop.
    goals: Any | None = None
    #: Phase 22. ``None`` means observations are reported but not remembered.
    world: Any | None = None
    permissions: Any | None = None
    observer: Callable[[str, Mapping[str, Any]], None] | None = None
    #: How many inputs the ONE door has actually sent for this session. The door
    #: is the authority on that figure — an attempt the fresh-state gate refused
    #: never reached the environment — so the loop reads it rather than counting
    #: its own intentions. ``None`` means no door is wired, which is only true in
    #: a unit test that drives the loop directly.
    input_counter: Callable[[], int] | None = None
    #: How many inputs this session's environment REFUSED, told by the door for
    #: the same reason it tells the loop the sent count: a refusal is recorded
    #: where it happened, and the loop cannot tell a refusal from a malfunction
    #: by looking at a run's status. Without it the evaluation's
    #: ``actions_rejected`` stayed a permanent zero while two refused inputs were
    #: reported as failures — the same action, described wrongly.
    refusal_counter: Callable[[], int] | None = None
    #: Where the agent publishes its own state changes. The session REGISTRY is
    #: what the input door, the list surface and the restore read, and the loop
    #: makes safety-relevant transitions on its own — clearing
    #: ``needs_revalidation`` after a revalidation, for one. Without this sink
    #: the registry kept the pre-run copy, so a resumed session stayed marked
    #: "not revalidated" in the only place the door looks and every input was
    #: refused: a session that could never act again. ``None`` means the caller
    #: owns the state, which is only true of a unit test driving the loop alone.
    on_state: Callable[[SessionState], None] | None = None
    refresh_attempts: int = DEFAULT_REFRESH_ATTEMPTS
    max_trajectory: int = DEFAULT_MAX_TRAJECTORY
    max_actions: int = DEFAULT_MAX_ACTIONS
    #: The most plans this session may produce, from configuration. Each cycle
    #: plans once, so this is what stops a loop whose attempts keep succeeding
    #: but whose objective is never met.
    plan_ceiling: int = DEFAULT_MAX_ACTIONS
    clock: Callable[[], float] = time.monotonic
    memory: TacticalMemory = field(default_factory=TacticalMemory)
    last_direction: str = ""
    #: The refusal count this loop has already attributed to an attempt.
    seen_refusals: int = 0
    started_monotonic: float = 0.0
    #: Counters the evaluation reads. Kept as plain integers so the record is
    #: auditable rather than derived from a log nobody can see.
    counts: dict[str, int] = field(
        default_factory=lambda: {
            "actions_attempted": 0,
            "actions_completed": 0,
            "actions_rejected": 0,
            "actions_failed": 0,
            "invalid_actions": 0,
            "input_failures": 0,
            "stale_observations": 0,
            "recoveries": 0,
            "recoveries_succeeded": 0,
            "repeated_actions": 0,
            "verification_failures": 0,
            "safety_violations": 0,
        }
    )

    # -- public surface ---------------------------------------------------------

    @property
    def objective(self) -> Objective:
        return self.state.objective

    @property
    def terminal(self) -> bool:
        return self.state.terminal

    async def run(self, *, max_actions: int | None = None) -> SessionState:
        """Run the loop until the objective is met, a budget dies, or it stops.

        The loop NEVER raises to report an outcome: an environment that breaks,
        a controller that refuses and a planner that declines are all session
        statuses with a reason, because "the run threw" is not a result an
        operator can act on.
        """
        valid, why = validate_objective(self.objective)
        if not valid:
            self._finish(SessionStatus.AGENT_ERROR, f"objective refused: {why}")
            return self.state

        if self.state.needs_revalidation:
            revalidated, reason = self.revalidate()
            if not revalidated:
                self._finish(SessionStatus.ENVIRONMENT_FAILED, reason)
                return self.state

        if self.goals is None:
            self._finish(
                SessionStatus.AGENT_ERROR,
                "no Phase 24 goal planner is wired, so no action can be selected "
                "through it; refusing to run a private loop instead",
            )
            return self.state

        connected = self._connect()
        if not connected:
            self._finish(
                SessionStatus.ENVIRONMENT_FAILED,
                f"the environment {self.state.environment_id!r} could not be connected",
            )
            return self.state

        self.started_monotonic = self.started_monotonic or self.clock()
        self._touch(
            status=SessionStatus.RUNNING,
            started_at=self.state.started_at or _now(),
            updated_at=_now(),
            released=False,
        )
        self._announce(
            AGENT_SESSION_STARTED,
            {
                "session_id": self.state.session_id,
                "environment_id": self.state.environment_id,
                "status": SessionStatus.RUNNING.value,
            },
        )

        try:
            while not self.terminal:
                # The input count is taken from the DOOR before anything is
                # decided, because the two budget checks below are what stop the
                # loop: reading a count the previous cycle left behind let the
                # loop plan one more attempt than the budget allowed and then
                # report a figure lower than what it had actually sent. The door
                # is the authority on what was sent, so it is asked FIRST.
                self._sync_sent_count()
                if self._over_time():
                    self._finish(
                        SessionStatus.BUDGET_EXHAUSTED,
                        f"the session's own {self.objective.max_seconds:.1f}s budget was "
                        "reached before the objective was verified",
                    )
                    break
                if self._action_budget(max_actions) <= 0:
                    break
                await self._cycle()
        finally:
            # The input-release guarantee is a finally block rather than a step
            # in the happy path: an exception, a cancellation and an ordinary
            # return all leave the environment with nothing held.
            self._release()
        if not self.terminal:
            self._finish(
                SessionStatus.BUDGET_EXHAUSTED,
                f"the session's action budget ({self.max_actions}) was reached before the "
                "objective was verified; the environment state is as observed",
            )
        return self.state

    def revalidate(self) -> tuple[bool, str]:
        """Re-establish the environment and the session before any input.

        Called after a restart, and it is deliberately strict: a session is only
        revalidated when the adapter connects AND a fresh structured reading
        arrives. Anything else means the agent does not know where it is, and an
        agent that does not know where it is must not send an input.
        """
        report = self.adapter.connect()
        if not report.connected or report.mock:
            return False, (
                f"the environment could not be revalidated after the restart: "
                f"{report.reason or report.status.value}"
            )
        observation = self.adapter.observe(session_id=self.state.session_id)
        if not observation.usable_for_decision or not observation.structured:
            return False, (
                "the environment connected but returned no fresh structured reading, "
                "so the session cannot be revalidated and no input will be sent"
            )
        self._ingest(observation)
        self._touch(
            needs_revalidation=False,
            last_observation=observation,
            updated_at=_now(),
            reason="revalidated: the environment answered with a fresh reading",
        )
        self._announce(
            AGENT_SESSION_RESUMED,
            {
                "session_id": self.state.session_id,
                "reason": "revalidated against a fresh structured reading",
            },
        )
        return True, ""

    def pause(self, reason: str = "") -> SessionState:
        if self.state.terminal:
            return self.state
        self._release()
        self._touch(
            status=SessionStatus.PAUSED,
            reason=reason or "paused by the operator",
            updated_at=_now(),
        )
        self._announce(
            AGENT_SESSION_PAUSED,
            {"session_id": self.state.session_id, "reason": self.state.reason},
        )
        return self.state

    def resume(self) -> SessionState:
        if self.state.status is not SessionStatus.PAUSED:
            return self.state
        # A pause may have been long enough for the reading to go stale, and a
        # resumed session must not act on a reading taken before the pause.
        self._touch(
            status=SessionStatus.CREATED,
            needs_revalidation=True,
            reason="resumed; the environment must be revalidated before any input",
            updated_at=_now(),
        )
        return self.state

    def cancel(self, reason: str = "") -> SessionState:
        if self.state.terminal:
            return self.state
        self._release()
        self._finish(SessionStatus.CANCELLED, reason or "cancelled by the operator")
        self._announce(
            AGENT_SESSION_CANCELLED,
            {"session_id": self.state.session_id, "reason": reason or "cancelled by the operator"},
        )
        return self.state

    # -- the cycle --------------------------------------------------------------

    async def _cycle(self) -> None:
        """One observe → decide → act → verify → recover pass."""
        observation = self._fresh_observation()
        if observation is None:
            self._finish(
                SessionStatus.ENVIRONMENT_FAILED,
                "the environment stopped answering observations, so the agent cannot "
                "know the state and will not act on the state it had",
            )
            return
        self._ingest(observation)
        self._touch(
            last_observation=observation, observations_taken=self.state.observations_taken + 1
        )
        self._entry(
            phase="observe",
            summary=f"observed {observation.basis.value} state",
            observed=_summarise(observation),
            status=observation.freshness.value,
            basis=observation.basis.value,
            detail={
                "freshness": observation.freshness.value,
                "age_ms": round(observation.age_ms, 3),
            },
        )
        self._announce(
            AGENT_OBSERVATION,
            {
                "session_id": self.state.session_id,
                "basis": observation.basis.value,
                "freshness": observation.freshness.value,
                "entities": len(observation.entities),
            },
        )
        if not observation.usable_for_decision:
            self.counts["stale_observations"] += 1
            self._recover(
                reason=(
                    f"the observation is {observation.freshness.value}; a state-dependent "
                    "attempt would be based on a reading that is not current"
                ),
                direction="",
            )
            return

        self._sync_sent_count()
        verdict = self.verdict(observation)
        if verdict.met:
            self._complete(verdict)
            return

        choice = choose(
            observation,
            want="collect" if _should_collect(observation, self.objective) else "reach",
            memory=self.memory,
            last_direction=self.last_direction,
        )
        self._entry(
            phase="decide",
            summary=choice.reason,
            expected="",
            observed="",
            status=choice.kind,
            basis=observation.basis.value,
            detail={"choice": choice.to_dict()},
        )
        self._announce(
            AGENT_ACTION_SELECTED,
            {
                "session_id": self.state.session_id,
                "kind": choice.kind,
                "direction": choice.direction,
            },
        )
        if choice.kind == "none":
            # The tactical layer found nothing to do from a FRESH reading: every
            # direction is known blocked, or what is left cannot be acted on.
            # Spinning through the recovery budget would not change that — the
            # recovery budget is for attempts that failed, and there was no
            # attempt — so the session stops here and says why.
            self._finish(SessionStatus.STUCK, choice.reason)
            return

        tasks = tasks_for(
            choice,
            session_id=self.state.session_id,
            environment_id=self.state.environment_id,
            position=choice.position,
            max_input_ms=self.objective.max_input_ms,
            attempt=self.state.actions_taken + 1,
        )
        if not tasks:
            self._recover(reason="no task could be derived from the choice", direction="")
            return
        self.counts["actions_attempted"] += 1
        position = choice.position or (0, 0)
        direction = choice.direction or ""
        if self.memory.repeats(position, direction):
            self.counts["repeated_actions"] += 1
            self.memory.remember_blocked(position, direction)
            self._recover(
                reason=(
                    f"the attempt {direction} from {position[0]},{position[1]} was already "
                    "made; repeating it is not a new attempt"
                ),
                direction=direction,
                choice=choice,
                repeat=True,
            )
            return
        self.memory.remember_attempt(position, direction)

        await self._plan_and_act(choice, tasks, observation)

    async def _plan_and_act(
        self,
        choice: TacticalChoice,
        tasks: Sequence[Any],
        observation: Observation,
    ) -> None:
        """Plan the choice through Phase 24, run it, and judge the outcome."""
        goals = self._require_goals()
        goal_id = self.state.goal_id or self._ensure_goal()
        if not goal_id:
            return
        self._goals_decompose(goal_id, tasks)
        report = goals.plan(goal_id)
        if not getattr(report, "planned", False):
            self._finish(
                SessionStatus.BUDGET_EXHAUSTED,
                "Phase 24 refused another plan for this objective: "
                + str(getattr(report, "refusal", "") or "no candidate was selected"),
            )
            return
        self._touch(
            last_action=self._action_preview(choice),
            actions_taken=self.state.actions_taken,
            updated_at=_now(),
        )
        run = await self._execute(goal_id)
        status = str(getattr(run, "status", "") or "")
        reason = str(getattr(run, "reason", "") or "")
        self._touch(last_result=None)

        after = self._fresh_observation()
        if after is not None:
            self._ingest(after)
            self._touch(last_observation=after)
        accepted = dict(getattr(run, "outputs", {}) or {}) if getattr(run, "outputs", None) else {}
        matched, verification = _effect_seen(choice, tasks, after, accepted)

        if status == "denied":
            self._entry(
                phase="act",
                summary=f"the approval layer refused: {reason}",
                observed=_summarise(after) if after is not None else "",
                status="denied",
                verified=False,
                detail={"direction": choice.direction},
            )
            # A policy refusal is a REFUSAL, not a malformed action: the attempt
            # was well formed and a layer said no. Counting it as "invalid" would
            # describe a refusal as a malfunction — the same mis-description the
            # door's own refusal counter exists to prevent.
            self.counts["actions_rejected"] += 1
            self._announce(
                AGENT_ACTION_FAILED,
                {"session_id": self.state.session_id, "status": "denied"},
            )
            self._finish(
                SessionStatus.DENIED,
                "the action policy refused the attempt: " + (reason or "no reason given"),
            )
            return

        if status == "completed" and matched is True:
            self.counts["actions_completed"] += 1
            self.last_direction = choice.direction
            wall = _blocked_by_wall(choice, observation, after)
            if wall:
                # The environment completed the input but the agent did not
                # move: a wall, a boundary, or a controller that ignored it.
                self.memory.remember_blocked(
                    position=choice.position or (0, 0), direction=choice.direction
                )
                self.counts["verification_failures"] += 1
                self._recover(
                    reason=(
                        f"the input was accepted but the agent is still at "
                        f"{_where(observation)}; treating {choice.direction} as blocked"
                    ),
                    direction=choice.direction,
                    choice=choice,
                )
                return
            self._entry(
                phase="act",
                summary=f"ran {choice.kind} {choice.direction}".strip(),
                expected=_expected_of(tasks),
                observed=_summarise(after) if after is not None else "",
                status=status,
                verified=True if matched else None,
                detail={"direction": choice.direction, "verification": verification},
            )
            self._announce(
                AGENT_ACTION_EXECUTED,
                {
                    "session_id": self.state.session_id,
                    "kind": choice.kind,
                    "direction": choice.direction,
                    "verified": bool(matched),
                },
            )
            return

        if matched is True:
            # The attempt WORKED: a fresh reading shows the effect the action
            # declared. That is the check this loop acts on, and it is why the
            # run's own status is not consulted here. A Phase 24 goal carries the
            # SESSION's objective, so a single tactical step legitimately leaves
            # that goal unmet — the run saying "the objective is not met yet" is
            # not a failed attempt, and the session's completion is decided by
            # :meth:`verdict` against a fresh reading, never by this status.
            self.counts["actions_completed"] += 1
            self.last_direction = choice.direction
            timed_out = "timeout" in reason.lower() or "timed out" in reason.lower()
            if timed_out:
                # The input did not report back, yet the effect is observed: the
                # input landed before it timed out. Counted as a completed action
                # AND as an input failure, because both statements are true.
                self.counts["input_failures"] += 1
            self._entry(
                phase="act",
                summary=(
                    f"ran {choice.kind} {choice.direction}".strip()
                    + (
                        " (the environment never reported back, but the effect was observed)"
                        if timed_out
                        else ""
                    )
                ),
                expected=_expected_of(tasks),
                observed=_summarise(after) if after is not None else "",
                status="completed",
                verified=True,
                detail={
                    "direction": choice.direction,
                    "verification": verification,
                    "run_status": status,
                },
            )
            self._announce(
                AGENT_ACTION_EXECUTED,
                {
                    "session_id": self.state.session_id,
                    "kind": choice.kind,
                    "direction": choice.direction,
                    "verified": True,
                },
            )
            return

        # Everything else is a failed attempt: an input the environment refused,
        # an input that never reported back, a step that landed somewhere else,
        # and an effect nothing could confirm. An unverifiable action is reported
        # as failed rather than counted as a success nobody can check.
        if status == "completed":
            reason = "the input was accepted but the effect could not be confirmed: " + (
                verification or "no verification was possible"
            )
        refused = self._refusals_since_last()
        if refused:
            # The environment REFUSED the input. That is an answer, not a
            # malfunction, and it is reported as a rejection rather than folded
            # into the failures — which is how the retry path stays visible.
            self.counts["actions_rejected"] += refused
        else:
            self.counts["actions_failed"] += 1
        self.counts["verification_failures"] += 1
        if "timeout" in reason.lower() or "timed out" in reason.lower():
            self.counts["input_failures"] += 1
        self._entry(
            phase="act",
            summary=f"the attempt did not produce the expected result: {reason or status}",
            expected=_expected_of(tasks),
            observed=_summarise(after) if after is not None else "",
            status=status or "failed",
            verified=False,
            detail={"direction": choice.direction, "verification": verification},
        )
        self._announce(
            AGENT_ACTION_FAILED,
            {
                "session_id": self.state.session_id,
                "status": status or "failed",
                "direction": choice.direction,
            },
        )
        blocked = _blocked_by_wall(choice, observation, after)
        if blocked:
            # The agent is exactly where it was: a wall, a boundary, or a
            # controller that ignored the input. Remembering the DIRECTION is
            # what makes the next choice a different one.
            self.memory.remember_blocked(
                position=choice.position or (0, 0), direction=choice.direction
            )
        self._recover(
            reason=reason
            or (
                f"the attempt ended as {status}; the agent is at {_where(after)}"
                if after is not None
                else f"the attempt ended as {status}"
            ),
            direction=choice.direction,
            choice=choice,
        )

    # -- objectives and verification -------------------------------------------

    def verdict(self, observation: Observation) -> ObjectiveVerdict:
        """Whether the objective's conditions are OBSERVED holding right now.

        The check reads the observation's own structured state rather than the
        plan's belief about it: a plan that says the target is collected proves
        nothing, and this is the one place that decides whether the session is
        over.
        """
        if not observation.usable_for_decision:
            return ObjectiveVerdict(
                met=False,
                verified=False,
                reason=(
                    f"the observation is {observation.freshness.value}, so completion "
                    "cannot be verified from it"
                ),
                observation_id=observation.observation_id,
            )
        state = dict(observation.structured or {})
        missing: list[str] = []
        observed: list[str] = []
        for condition in self.objective.success_conditions:
            key, _, value = str(condition).partition("=")
            name = key.strip()
            # A condition may be written with the environment prefix or without
            # it, and the environment reports its own state UNPREFIXED. Every
            # spelling is tried so a condition cannot fail merely because it was
            # written the other way round — the same tolerance the fact layer
            # applies when it prefixes a name.
            bare = name[len(FACT_PREFIX) :] if name.startswith(FACT_PREFIX) else name
            lookup = next(
                (
                    candidate
                    for candidate in (name, bare, f"{FACT_PREFIX}{name}")
                    if candidate in state
                ),
                "",
            )
            if not lookup:
                missing.append(f"{name} (not observed)")
                continue
            if same_value(state.get(lookup), value.strip()):
                observed.append(str(condition))
            else:
                missing.append(
                    f"{name} (observed {state.get(lookup)!r}, expected {value.strip()!r})"
                )
        if missing:
            return ObjectiveVerdict(
                met=False,
                verified=False,
                reason="the objective is not verified: " + "; ".join(missing[:4]),
                missing=tuple(missing),
                observed=tuple(observed),
                observation_id=observation.observation_id,
            )
        return ObjectiveVerdict(
            met=True,
            verified=True,
            reason="every success condition was observed holding",
            observed=tuple(observed),
            observation_id=observation.observation_id,
        )

    # -- plumbing ---------------------------------------------------------------

    def _fresh_observation(self) -> Observation | None:
        """A fresh reading, re-read when the first attempt came back stale."""
        observation: Observation | None = None
        for _ in range(max(1, self.refresh_attempts)):
            observation = self.adapter.observe(session_id=self.state.session_id)
            if observation.usable_for_decision and observation.structured:
                return observation
        return observation

    def _ingest(self, observation: Observation) -> None:
        """Remember the observation in the world model, when one is wired.

        Ingestion is best-effort by design: a world model that cannot take the
        reading must not stop the session. The META says so rather than
        pretending the fact was recorded.
        """
        if self.world is None:
            return
        try:
            self.world.observe(
                to_world_observation(
                    observation,
                    session_id=self.state.session_id,
                    scope=self.state.environment_id,
                )
            )
        except Exception:  # noqa: BLE001 - remembering is not the work
            self._entry(
                phase="remember",
                summary="the world model could not take this observation",
                status="unrecorded",
                basis=observation.basis.value,
            )

    def _connect(self) -> bool:
        report = self.adapter.connect()
        if not report.connected or report.mock:
            self._entry(
                phase="connect",
                summary=report.reason or report.status.value,
                status="failed",
            )
            return False
        self._entry(
            phase="connect",
            summary=f"connected to {report.environment_id}",
            status=report.status.value,
            detail={"actions": list(report.actions)},
        )
        return True

    def _ensure_goal(self) -> str:
        """Create the ONE Phase 24 goal this session works under.

        One goal, replanned per cycle: that is what makes Phase 24's replan
        ceiling the session's real bound instead of a number nobody reads.
        """
        goals = self._require_goals()
        goal = goals.create(
            self.objective.request,
            success_conditions=success_conditions_from_text(self.objective.success_conditions),
            initial_state={},
            constraints=tuple(self.objective.constraints),
            exclusions=(),
            required_permissions=("environment:control",),
            failure_conditions=tuple(self.objective.failure_conditions),
            cancellation_conditions=tuple(self.objective.cancellation_conditions),
            provenance=f"embodied:{self.state.session_id}",
            correlation_id=self.state.session_id,
            max_steps=max(2, self.objective.max_actions),
            # The PLANNER's ceiling, which is a cost bound: one plan per cycle, so
            # it is derived from the action budget and clamped by configuration.
            # The objective's own ``max_replans`` is the RECOVERY allowance (how
            # many failed attempts to tolerate) and is spent by ``_recover`` —
            # conflating the two made a two-step detour hit "no further plan"
            # while its action budget was still almost untouched.
            max_replans=max(0, min(self.plan_ceiling, max(1, self.objective.max_actions))),
            max_seconds=max(1.0, self.objective.max_seconds),
        )
        self._touch(goal_id=goal.goal_id, updated_at=_now())
        return str(goal.goal_id)

    def _require_goals(self) -> Any:
        """The Phase 24 goal manager, or an honest refusal.

        Unreachable from :meth:`run`, which refuses to start a session with no
        planner wired; the raise is here so a caller that bypasses that gate is
        told why instead of failing on ``None``.
        """
        if self.goals is None:
            raise RuntimeError(
                "no Phase 24 goal planner is wired, so no action can be selected through it"
            )
        return self.goals

    def _goals_decompose(self, goal_id: str, tasks: Sequence[Any]) -> None:
        self._require_goals().decompose(
            goal_id, tuple(tasks), known_capabilities=("environment:control",)
        )

    async def _execute(self, goal_id: str) -> Any:
        try:
            return await self._require_goals().execute(goal_id)
        except Exception as exc:  # noqa: BLE001 - a broken run is a session outcome
            self._finish(
                SessionStatus.AGENT_ERROR,
                f"the execution path raised {type(exc).__name__}: {exc}",
            )
            return _FailedRun(str(exc))

    def _recover(
        self,
        *,
        reason: str,
        direction: str,
        choice: TacticalChoice | None = None,
        repeat: bool = False,
    ) -> None:
        """One recovery attempt, bounded by the session's own ceiling."""
        ceiling = max(1, self.objective.max_replans + 1)
        self.counts["recoveries"] += 1
        used = self.counts["recoveries"]
        self._touch(
            recoveries=used,
            replans_used=min(used, self.objective.max_replans),
            updated_at=_now(),
        )
        self._announce(
            AGENT_RECOVERY,
            {
                "session_id": self.state.session_id,
                "reason": reason,
                "attempt": used,
                "ceiling": ceiling,
            },
        )
        self._entry(
            phase="recover",
            summary=reason,
            status="recover",
            detail={"attempt": used, "ceiling": ceiling, "direction": direction},
        )
        if used >= ceiling:
            self._finish(
                SessionStatus.BUDGET_EXHAUSTED,
                f"the recovery ceiling was reached ({used}/{ceiling}): {reason}",
            )
            return
        # A recovery that got a different attempt out of the memory is counted
        # as a success only when the NEXT cycle actually proceeds; the count is
        # advanced here because the alternative was proven and remembered.
        if not repeat and choice is not None and choice.direction:
            self.counts["recoveries_succeeded"] += 1

    def _refusals_since_last(self) -> int:
        """How many inputs the door saw the environment REFUSE since last asked.

        A delta rather than a total: the door's count spans the whole session,
        and this must attribute a refusal to the attempt that produced it.
        """
        if self.refusal_counter is None:
            return 0
        try:
            total = max(0, int(self.refusal_counter()))
        except Exception:  # noqa: BLE001 - an unreadable count is no refusals
            return 0
        delta = max(0, total - self.seen_refusals)
        self.seen_refusals = max(self.seen_refusals, total)
        return delta

    def _sync_sent_count(self) -> None:
        """Take the door's count of inputs that were actually sent.

        Counting the loop's own plans would overstate the session's cost by every
        attempt the fresh-state gate refused, and understate it by nothing — but
        a session's resource record has to be the first kind of number: what
        really left for the environment.
        """
        if self.input_counter is None:
            return
        try:
            sent = max(0, int(self.input_counter()))
        except Exception:  # noqa: BLE001 - an unreadable count keeps the last one
            return
        if sent != self.state.actions_taken:
            self._touch(actions_taken=sent, updated_at=_now())

    def _action_budget(self, max_actions: int | None) -> int:
        remaining = max(0, self.objective.max_actions - self.state.actions_taken)
        asked = DEFAULT_MAX_ACTIONS if max_actions is None else int(max_actions)
        return min(remaining, max(0, asked))

    def _over_time(self) -> bool:
        if not self.started_monotonic:
            return False
        return (self.clock() - self.started_monotonic) > float(self.objective.max_seconds)

    def _action_preview(self, choice: TacticalChoice) -> EnvironmentAction:
        return EnvironmentAction(
            session_id=self.state.session_id,
            kind=ActionKind.MOVE if choice.kind == "move" else ActionKind.INTERACT,
            input_name=choice.kind if choice.kind != "move" else "move",
            parameters={"direction": choice.direction} if choice.direction else {},
            duration_ms=self.objective.max_input_ms,
            expected_effect=choice.reason,
            rationale=choice.reason,
        )

    def _release(self) -> None:
        report: ReleaseReport = self.adapter.release_inputs()
        self._touch(released=report.released, updated_at=_now())

    def _complete(self, verdict: ObjectiveVerdict) -> None:
        self._touch(verdict=verdict)
        self._entry(
            phase="verify",
            summary="every success condition was observed holding",
            observed="; ".join(verdict.observed),
            status="verified",
            verified=True,
        )
        self._announce(
            AGENT_OBJECTIVE_VERIFIED,
            {"session_id": self.state.session_id, "conditions": len(verdict.observed)},
        )
        self._finish(SessionStatus.COMPLETED, verdict.reason)

    def _finish(self, status: SessionStatus, reason: str) -> None:
        self._release()
        finished = _now()
        elapsed = self.clock() - self.started_monotonic if self.started_monotonic else None
        self._touch(
            status=status,
            reason=reason,
            finished_at=finished,
            updated_at=finished,
            verdict=self.state.verdict,
            evaluation=_evaluation(
                self.state,
                self.counts,
                elapsed,
                verified=status is SessionStatus.COMPLETED,
                # The ending is passed in because THIS call is what records it:
                # the state this helper reads still says ``running`` until the
                # ``_touch`` beside it lands, which would leave every record
                # claiming its outcome was "running".
                terminal=status.value,
            ),
        )
        self._announce(
            AGENT_SESSION_ENDED,
            {
                "session_id": self.state.session_id,
                "status": status.value,
                "reason": reason,
            },
        )
        if status is SessionStatus.COMPLETED:
            return
        if status is SessionStatus.STUCK:
            self._announce(
                AGENT_BLOCKED,
                {"session_id": self.state.session_id, "reason": reason},
            )
        self._announce(
            AGENT_OBJECTIVE_FAILED,
            {"session_id": self.state.session_id, "reason": reason},
        )

    def _touch(self, **changes: Any) -> None:
        self.state = self.state.with_(**changes)
        if self.on_state is not None:
            with suppress(Exception):  # a broken sink is not a session outcome
                self.on_state(self.state)

    def _entry(self, **fields: Any) -> None:
        index = len(self.state.trajectory)
        entry = TrajectoryEntry(
            index=index,
            at=fields.pop("at", "") or _now(),
            phase=str(fields.pop("phase", "")),
            summary=str(fields.pop("summary", "")),
            action_id=str(fields.pop("action_id", "")),
            action=str(fields.pop("action", "")),
            expected=str(fields.pop("expected", "")),
            observed=str(fields.pop("observed", "")),
            status=str(fields.pop("status", "")),
            verified=fields.pop("verified", None),
            basis=str(fields.pop("basis", "")),
            detail=dict(fields.pop("detail", {}) or {}),
            evidence=tuple(fields.pop("evidence", ()) or ()),
        )
        rows = list(self.state.trajectory)
        rows.append(entry)
        if len(rows) > self.max_trajectory:
            rows = rows[-self.max_trajectory :]
        self._touch(trajectory=tuple(rows), updated_at=_now())

    def _announce(self, kind: str, payload: Mapping[str, Any]) -> None:
        if self.observer is None:
            return
        try:
            self.observer(kind, dict(payload))
        except Exception:  # noqa: BLE001 - an announcement is not the work
            return


@dataclass(frozen=True, slots=True)
class _FailedRun:
    """A run that never started, shaped like one so the caller reads one thing."""

    status: str = "failed"
    verified: bool = False
    reason: str = ""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def success_conditions_from_text(texts: Sequence[str]) -> tuple[Any, ...]:
    """A session objective's ``key=value`` conditions as planning conditions.

    An objective states its finish line as text (that is what an operator
    writes, and what travels over the API), while Phase 24's goal takes
    :class:`~novacontrol.planning.goals.SuccessCondition` objects. The VALUE is
    translated by the planning layer's own ``coerce_value`` rather than by a
    second reading of "true" defined here, so the condition the session checks
    and the condition the planner checks cannot disagree.

    A condition with no ``=`` is kept with ``expected=None``: it stays
    UNMEASURABLE, which the planner reports instead of the session inventing an
    expectation for it.
    """
    from novacontrol.planning.goals import SuccessCondition, coerce_value

    rows: list[Any] = []
    for text in texts or ():
        item = str(text).strip()
        if not item:
            continue
        key, sep, raw = item.partition("=")
        rows.append(
            SuccessCondition(
                key=key.strip(),
                expected=coerce_value(raw.strip()) if sep else None,
                description=item,
            )
        )
    return tuple(rows)


def _should_collect(observation: Observation, objective: Objective) -> bool:
    """Whether the objective wants an interaction rather than a movement.

    Read from the objective's own words so the tactical layer is not asked to
    know a game: an objective that names a collected fact wants the interaction.
    """
    wanted = " ".join(objective.success_conditions) + " " + objective.request
    return "collect" in wanted.lower() or "interact" in wanted.lower()


def _effect_seen(
    choice: TacticalChoice,
    tasks: Sequence[Any],
    observation: Observation | None,
    accepted: Mapping[str, Any] | None = None,
) -> tuple[bool | None, str]:
    """Whether a fresh observation shows the effect the task declared.

    The comparison itself lives in :func:`effect_observed` so the loop and the
    input door cannot disagree about what "the effect was seen" means. What this
    wrapper adds is the fallback for a task that declared its expectation as an
    ``expected_outputs`` tuple rather than a single ``expected`` string, and the
    landing cell when a task stated only that it moved.
    """
    declared = _expected_facts_of(tasks)
    if not declared:
        if choice.kind == "interact":
            declared = (f"{FACT_PREFIX}target.collected=true",)
        elif choice.kind == "move" and choice.direction:
            parameters = dict(getattr(tasks[0], "parameters", {}) or {}) if tasks else {}
            landing = parameters.get("landing")
            if landing:
                declared = (f"{FACT_PREFIX}agent.x={landing[0]},{FACT_PREFIX}agent.y={landing[1]}",)
    if not declared:
        return None, "the attempt declared no observable effect"
    matched, detail = effect_observed(declared, observation)
    if matched is True and accepted:
        # The environment's own report of what it accepted is corroboration, not
        # a substitute: both must agree before the effect is called seen.
        for fact in declared:
            key, _, value = fact.partition("=")
            reported = accepted.get(key.strip())
            if reported is not None and not same_value(reported, value):
                return False, (
                    f"the environment reported {key.strip()}={reported!r} while the "
                    f"observation shows {value.strip()!r}"
                )
    return matched, detail


def _expected_facts_of(tasks: Sequence[Any]) -> tuple[str, ...]:
    """Every ``key=value`` fact the tasks declared they would establish."""
    facts: list[str] = []
    for task in tasks or ():
        parameters = dict(getattr(task, "parameters", {}) or {})
        raw = str(parameters.get("expected") or "")
        for item in raw.split(","):
            text = item.strip()
            if text and "=" in text:
                facts.append(text)
        for item in getattr(task, "expected_outputs", ()) or ():
            text = str(item).strip()
            if text and "=" in text:
                facts.append(text)
    return tuple(dict.fromkeys(facts))


def _blocked_by_wall(
    choice: TacticalChoice,
    before: Observation,
    after: Observation | None,
) -> bool:
    """Whether a completed move left the agent where it was — a blocked cell."""
    if choice.kind != "move" or after is None:
        return False
    # One reader for "where is the agent", from the tactical layer: it accepts
    # the environment's own spelling and the prefixed one, so a blocked attempt
    # cannot look like an unreported position.
    before_position = read_position(before)
    after_position = read_position(after)
    if before_position is None or after_position is None:
        return False
    return before_position == after_position


def _where(observation: Observation) -> str:
    position = read_position(observation)
    if position is None:
        return "an unreported position"
    return f"{position[0]},{position[1]}"


def _expected_of(tasks: Sequence[Any]) -> str:
    if not tasks:
        return ""
    parameters = dict(getattr(tasks[0], "parameters", {}) or {})
    return str(parameters.get("expected") or getattr(tasks[0], "expected_outputs", ""))


def _summarise(observation: Observation) -> str:
    if not observation.structured:
        return f"{len(observation.entities)} region(s), no structured state"
    state = dict(observation.structured)
    position = f"{state.get('agent.x')},{state.get('agent.y')}"
    progress = dict(observation.objective_progress or {})
    distance = progress.get("distance_to_target")
    return (
        f"agent at {position}; distance {distance}; tick {state.get('tick')}"
        if distance is not None
        else f"agent at {position}; tick {state.get('tick')}"
    )


def _evaluation(
    state: SessionState,
    counts: Mapping[str, int],
    elapsed: float | None,
    *,
    verified: bool,
    terminal: str = "",
) -> Any:
    from novacontrol.embodied.models import Evaluation

    return Evaluation(
        session_id=state.session_id,
        objective_met=verified,
        verified_completion=verified,
        terminal=terminal or state.status.value,
        actions_attempted=int(counts.get("actions_attempted", 0)),
        actions_completed=int(counts.get("actions_completed", 0)),
        actions_rejected=int(counts.get("actions_rejected", 0)),
        actions_failed=int(counts.get("actions_failed", 0)),
        invalid_actions=int(counts.get("invalid_actions", 0)),
        input_failures=int(counts.get("input_failures", 0)),
        observations=int(state.observations_taken),
        stale_observations=int(counts.get("stale_observations", 0)),
        recoveries=int(counts.get("recoveries", 0)),
        recoveries_succeeded=int(counts.get("recoveries_succeeded", 0)),
        replans=int(state.replans_used),
        repeated_actions=int(counts.get("repeated_actions", 0)),
        verification_failures=int(counts.get("verification_failures", 0)),
        safety_violations=int(counts.get("safety_violations", 0)),
        seconds=round(elapsed, 3) if elapsed is not None else None,
        observation_freshness_ms=None
        if not state.last_observation
        else round(state.last_observation.age_ms, 3),
        progress=None
        if not state.last_observation
        else (state.last_observation.objective_progress.get("distance_to_target")),
    )


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


__all__ = [
    "AGENT_ACTION_EXECUTED",
    "AGENT_ACTION_FAILED",
    "AGENT_ACTION_SELECTED",
    "AGENT_BLOCKED",
    "AGENT_OBSERVATION",
    "AGENT_OBJECTIVE_FAILED",
    "AGENT_OBJECTIVE_VERIFIED",
    "AGENT_RECOVERY",
    "AGENT_SESSION_CANCELLED",
    "AGENT_SESSION_ENDED",
    "AGENT_SESSION_PAUSED",
    "AGENT_SESSION_RESUMED",
    "AGENT_SESSION_STARTED",
    "DEFAULT_MAX_ACTIONS",
    "DEFAULT_MAX_TRAJECTORY",
    "DEFAULT_REFRESH_ATTEMPTS",
    "EmbodiedAgent",
]
