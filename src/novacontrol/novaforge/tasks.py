"""NF-25 — one bounded real-game task, judged by evidence rather than by dispatch.

NF-22 already owns the loop (observe → understand → objective → plan → approve →
act → verify → recover) and NF-19/20/21 own input, planning and verification. This
module adds the layer a *task* needs and the loop cannot give itself:

* :class:`TaskSpec` — what the operator asked for, in a form that can be refused
  before anything runs: objective, target identity, success/failure conditions,
  permitted action kinds, action/cycle/time budgets, an observation freshness
  bound, a recovery limit and the evidence that would prove success.
* :class:`TaskRunner` — the preflight gates (approval, target identity, control
  policy, initial state, resource gate, duplicate run) and the delegation to the
  NF-22 controller as it is. It holds no loop, no planner and no input path.
* :func:`judge_task` — the ONE judge. A run's own ending is not enough: the
  task's declared evidence must actually be present, or the verdict stays
  unverified. Dispatch is counted separately from a game response and from
  verified progress, so "we sent keys" can never read as "the objective moved".

The distinction this module exists to keep:

    dispatched input  !=  observed game response  !=  verified progress
                      !=  objective completion  !=  live evidence
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from .autonomy import (
    MAX_ACTIONS,
    MAX_ACTIONS_PER_PLAN,
    MAX_CYCLES,
    MAX_RECOVERY_ATTEMPTS,
    MAX_REPLANS,
    MAX_SESSION_SECONDS,
    AutonomyLimits,
    AutonomyOutcome,
)
from .control import CONTROL_KINDS
from .models import MissionConfig

TASK_SCHEMA_VERSION = "nf25.1"

#: A task's bounds may never exceed the supervisor's own ceilings. A wider ask is
#: refused rather than clamped: a silently narrowed budget is as dishonest as a
#: silently widened one (NF-22's rule, kept here).
MAX_TASK_ACTIONS = MAX_ACTIONS
MAX_TASK_CYCLES = MAX_CYCLES
MAX_TASK_SECONDS = MAX_SESSION_SECONDS

#: Goals that state no observable success condition. A task built on one of these
#: cannot be verified by any evidence, so it is refused before it can run.
VAGUE_GOALS = frozenset(
    {
        "play well",
        "play the game",
        "win",
        "win the game",
        "beat the game",
        "finish the game",
        "complete the game",
        "get good",
        "make progress",
        "do your best",
        "complete the mission",
        "progress",
    }
)

MIN_OBSERVATION_AGE = 0.1
MAX_OBSERVATION_AGE = 30.0
MAX_CONDITION_LENGTH = 300
MIN_EVIDENCE_TAG = 3
MAX_EVIDENCE_TAG = 120


class TaskVerdict(StrEnum):
    """What a finished task established — and nothing it did not.

    ``VERIFIED_COMPLETION`` needs the run's terminal ending AND the task's own
    evidence; every other member is a distinct answer, never a synonym for
    success or for failure.
    """

    VERIFIED_COMPLETION = "verified_completion"
    OBJECTIVE_FAILED = "objective_failed"
    PROGRESS_ONLY = "progress_only"
    OUTCOME_UNKNOWN = "outcome_unknown"
    CANCELLED = "cancelled"
    SAFETY_STOP = "safety_stop"
    RESOURCE_REFUSED = "resource_refused"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    INTERRUPTED = "interrupted"


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime | None = None) -> str:
    return (moment or _now()).isoformat()


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _age_seconds(stamp: str, now: datetime) -> float | None:
    """How old a reading is, or ``None`` when its time cannot be read."""
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (now - moment).total_seconds())


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """One bounded real-game task, validated before it can run.

    ``mission`` is the EXISTING :class:`~novacontrol.novaforge.models.MissionConfig`
    — the objective, the target window title and the session's action budget — so
    a task never invents a second objective model. The fields beside it are what
    makes it bounded: what may be sent, for how long, from which screen, with how
    much recovery, and which evidence would prove it.
    """

    task_id: str
    mission: MissionConfig
    success_conditions: tuple[str, ...] = ()
    failure_conditions: tuple[str, ...] = ()
    permitted_actions: tuple[str, ...] = ()
    #: Scenes the task may start from (``menu``, ``gameplay``, …). Empty means the
    #: task accepts any scene the state can actually NAME — never an unknown one.
    initial_scenes: tuple[str, ...] = ()
    #: The evidence that would prove the task. Matched case-insensitively against
    #: the run's own evidence references; a task that names none is refused,
    #: because nothing could then distinguish success from a plausible story.
    required_evidence: tuple[str, ...] = ()
    game: str = ""
    max_seconds: float = 120.0
    max_cycles: int = 6
    max_actions_per_plan: int = 1
    observation_max_age_seconds: float = 5.0
    recovery_limit: int = 1
    requires_approval: bool = True
    notes: str = ""

    def errors(self) -> list[str]:
        """Every reason this task must not run; empty means it may.

        Each rule exists because breaking it would allow a claim the pipeline
        cannot support: no objective (nothing to pursue), a vague goal (nothing
        to verify), no evidence requirement (nothing to check), an unknown action
        kind (input this build cannot bound), or a budget beyond the supervisor's
        ceilings (a bound that would be quietly narrowed).
        """
        problems: list[str] = []
        if not str(self.task_id or "").strip():
            problems.append("A task needs an id.")
        elif len(str(self.task_id).strip()) > 64:
            problems.append("A task id must be at most 64 characters.")
        if not str(self.game or "").strip():
            problems.append("A task must name the game it is for.")
        problems.extend(self.mission.errors())
        if self.mission.max_actions is None:
            problems.append(
                "A task must declare an action budget (MissionConfig.max_actions); "
                "an unbounded task is not a bounded task."
            )
        elif int(self.mission.max_actions) > MAX_TASK_ACTIONS:
            problems.append(
                f"A task may send at most {MAX_TASK_ACTIONS} actions, not "
                f"{self.mission.max_actions}; a wider bound is refused, never clamped."
            )
        if not self.success_conditions:
            problems.append("A task needs at least one success condition.")
        for condition in self.success_conditions:
            text = str(condition or "").strip()
            if not text:
                problems.append("A success condition cannot be blank.")
                continue
            if len(text) > MAX_CONDITION_LENGTH:
                problems.append(
                    f"A success condition must be at most {MAX_CONDITION_LENGTH} characters."
                )
            if text.casefold().strip(" .!") in VAGUE_GOALS:
                problems.append(
                    f"The success condition {text!r} states no observable outcome, so no "
                    "evidence could ever establish it. Name what would be visible."
                )
        for condition in self.failure_conditions:
            if not str(condition or "").strip():
                problems.append("A failure condition cannot be blank.")
        if not self.required_evidence:
            problems.append(
                "A task must name the evidence that would prove it; without one, a run "
                "cannot be judged."
            )
        for tag in self.required_evidence:
            text = str(tag or "").strip()
            if not text:
                problems.append("A required evidence tag cannot be blank.")
            elif len(text) < MIN_EVIDENCE_TAG:
                problems.append(
                    f"A required evidence tag must be at least {MIN_EVIDENCE_TAG} "
                    f"characters; {text!r} would match almost any evidence."
                )
            elif len(text) > MAX_EVIDENCE_TAG:
                problems.append(
                    f"A required evidence tag must be at most {MAX_EVIDENCE_TAG} characters."
                )
        if not self.permitted_actions:
            problems.append("A task must permit at least one action kind.")
        seen: set[str] = set()
        for kind in self.permitted_actions:
            text = str(kind or "").strip().lower()
            if text not in CONTROL_KINDS:
                problems.append(
                    f"{kind!r} is not an action this build can perform; expected one of "
                    f"{', '.join(sorted(CONTROL_KINDS))}."
                )
            if text in seen:
                problems.append(f"Action kind {text!r} is declared twice.")
            seen.add(text)
        for scene in self.initial_scenes:
            name = str(scene or "").strip().casefold()
            if name == "unknown":
                problems.append(
                    "'unknown' cannot be an allowed initial scene: the preflight refuses "
                    "an unnamed screen, so declaring it would state a permission nothing "
                    "can satisfy."
                )
            elif name not in {
                "menu",
                "loading",
                "gameplay",
                "pause",
                "death",
                "victory",
                "defeat",
            }:
                problems.append(f"{scene!r} is not a scene this build can name.")
        if not 1 <= int(self.max_cycles) <= MAX_TASK_CYCLES:
            problems.append(f"max_cycles must be between 1 and {MAX_TASK_CYCLES}.")
        if not 1 <= int(self.max_actions_per_plan) <= MAX_ACTIONS_PER_PLAN:
            problems.append(
                f"max_actions_per_plan must be between 1 and {MAX_ACTIONS_PER_PLAN}."
            )
        if self.mission.max_actions is not None and int(self.max_actions_per_plan) > int(
            self.mission.max_actions
        ):
            problems.append("max_actions_per_plan must not exceed the task's action budget.")
        if not 0 < float(self.max_seconds) <= MAX_TASK_SECONDS:
            problems.append(f"max_seconds must be between 0 and {MAX_TASK_SECONDS:g}.")
        session_limit = self.mission.time_limit_seconds
        if session_limit is not None and float(self.max_seconds) > float(session_limit):
            problems.append(
                f"max_seconds ({self.max_seconds:g}) must not exceed the mission's own "
                f"time limit ({session_limit} s): a task cannot outlive its session's budget."
            )
        if not 0 <= int(self.recovery_limit) <= MAX_RECOVERY_ATTEMPTS:
            problems.append(
                f"recovery_limit must be between 0 and {MAX_RECOVERY_ATTEMPTS}."
            )
        if int(self.recovery_limit) > MAX_REPLANS:
            problems.append(f"recovery_limit must be at most {MAX_REPLANS}.")
        if not (
            MIN_OBSERVATION_AGE
            <= float(self.observation_max_age_seconds)
            <= MAX_OBSERVATION_AGE
        ):
            problems.append(
                "observation_max_age_seconds must be between "
                f"{MIN_OBSERVATION_AGE:g} and {MAX_OBSERVATION_AGE:g}."
            )
        return problems

    @property
    def runnable(self) -> bool:
        return not self.errors()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": str(self.task_id).strip(),
            "mission": self.mission.to_dict(),
            "game": str(self.game or "").strip(),
            "objective": self.mission.objective.strip(),
            "target_title": self.mission.target_title.strip(),
            "success_conditions": list(self.success_conditions),
            "failure_conditions": list(self.failure_conditions),
            "permitted_actions": [str(item).strip().lower() for item in self.permitted_actions],
            "initial_scenes": [str(item).strip().casefold() for item in self.initial_scenes],
            "required_evidence": list(self.required_evidence),
            "limits": {
                "max_actions": self.mission.max_actions,
                "max_cycles": self.max_cycles,
                "max_actions_per_plan": self.max_actions_per_plan,
                "max_seconds": self.max_seconds,
                "observation_max_age_seconds": self.observation_max_age_seconds,
                "recovery_limit": self.recovery_limit,
            },
            "requires_approval": self.requires_approval,
            "notes": str(self.notes or ""),
        }

    def autonomy_limits(self) -> AutonomyLimits:
        """The supervisor's bounds, translated from this task's own limits."""
        return AutonomyLimits(
            max_cycles=int(self.max_cycles),
            max_actions=int(self.mission.max_actions or 0),
            max_actions_per_plan=int(self.max_actions_per_plan),
            session_seconds=float(self.max_seconds),
            recovery_attempts=int(self.recovery_limit),
            replans=int(self.recovery_limit),
        )


@dataclass(slots=True)
class TaskReport:
    """What one task attempt established, with the evidence it used."""

    task_id: str
    session_id: str = ""
    status: str = "refused"
    verdict: str = ""
    code: str = ""
    reason: str = ""
    game: str = ""
    target: str = ""
    objective: str = ""
    success_conditions: list[str] = field(default_factory=list)
    failure_conditions: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)
    evidence_found: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    initial: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    resource_gate: dict[str, Any] = field(default_factory=dict)
    run: dict[str, Any] = field(default_factory=dict)
    progress: dict[str, Any] = field(default_factory=dict)
    #: The objective's own ladder, each stage answered separately (Objective E):
    #: identified → initial state → plan → approval → request → dispatch →
    #: response → progress evidence → verified completion. A later stage is never
    #: implied by an earlier one.
    stages: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    evidence_source: str = ""
    simulated: bool = False
    started_at: str = ""
    ended_at: str = ""
    limitations: list[str] = field(default_factory=list)
    schema_version: str = TASK_SCHEMA_VERSION

    @property
    def completed(self) -> bool:
        return self.verdict == TaskVerdict.VERIFIED_COMPLETION.value

    @property
    def terminal(self) -> bool:
        return self.status in {"finished", "refused"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "status": self.status,
            "verdict": self.verdict,
            "completed": self.completed,
            "code": self.code,
            "reason": self.reason,
            "game": self.game,
            "target": self.target,
            "objective": self.objective,
            "success_conditions": list(self.success_conditions),
            "failure_conditions": list(self.failure_conditions),
            "required_evidence": list(self.required_evidence),
            "evidence_found": list(self.evidence_found),
            "missing_evidence": list(self.missing_evidence),
            "initial": dict(self.initial),
            "limits": dict(self.limits),
            "resource_gate": dict(self.resource_gate),
            "run": dict(self.run),
            "progress": dict(self.progress),
            "stages": dict(self.stages),
            "verification": dict(self.verification),
            "evidence_source": self.evidence_source,
            "simulated": self.simulated,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "limitations": list(self.limitations),
            "note": (
                "A verified completion needs the run's terminal ending AND the task's "
                "declared evidence. A dispatched input is never a game response, a game "
                "response is never completion, and simulation is never live evidence."
            ),
        }


def _flatten_outcomes(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every NF-21 outcome row a run recorded, newest last."""
    rows: list[dict[str, Any]] = []
    for cycle in _rows(run.get("cycles")):
        for outcome in _rows(_mapping(cycle.get("verification")).get("outcomes")):
            rows.append(outcome)
    return rows


def _accepted_refs(outcomes: Sequence[Mapping[str, Any]]) -> list[str]:
    """The evidence references NF-21 ACCEPTED for a verified effect.

    Only an explicit ``accepted: true`` counts. A row that does not say whether
    the reference was accepted is not evidence that it was — absence is not
    consent, and this is the function that decides whether a claim is grounded.
    """
    refs: list[str] = []
    for outcome in outcomes:
        if str(outcome.get("status") or "") != "verified_success":
            continue
        for ref in _rows(outcome.get("evidence")):
            if ref.get("accepted") is not True:
                continue
            for key in ("ref", "observation_id", "state_id"):
                text = str(ref.get(key) or "")
                if text and text not in refs:
                    refs.append(text)
    return refs


def _last_cycle(run: Mapping[str, Any]) -> dict[str, Any]:
    """The cycle whose ending the run's verdict was taken from."""
    cycles = _rows(run.get("cycles"))
    return cycles[-1] if cycles else {}


def _objective_refs(run: Mapping[str, Any]) -> list[str]:
    """The reference the NF-17 objective row carries for its FINAL verdict.

    Only the last cycle's objective counts: an objective row from an earlier
    cycle describes a screen the completion was not read from, and accepting it
    as proof would let old evidence satisfy a new claim.
    """
    refs: list[str] = []
    objective = _mapping(_last_cycle(run).get("objective"))
    for ref in _rows(objective.get("evidence")):
        for key in ("reference", "observation_id", "state_id", "ref"):
            text = str(ref.get(key) or "")
            if text and text not in refs:
                refs.append(text)
    objective_id = str(objective.get("objective_id") or "")
    # The id alone is a name, not a reading: only an objective row that actually
    # carries evidence contributes its token.
    if objective_id and refs and f"objective:{objective_id}" not in refs:
        refs.append(f"objective:{objective_id}")
    return refs


def task_progress(run: Mapping[str, Any]) -> dict[str, Any]:
    """What the run dispatched, what the game showed, and what was verified.

    These are three different counts on purpose. A plan that sent four keys has
    ``actions_dispatched = 4`` and, if nothing was readable afterwards,
    ``actions_responded = 0`` — the number that matters for the phase's honesty.
    """
    outcomes = _flatten_outcomes(run)
    statuses = [str(item.get("status") or "") for item in outcomes]
    responded = [
        item
        for item in outcomes
        if str(item.get("status") or "")
        in {"verified_success", "verified_failure", "partial_success"}
        or bool(str(item.get("observed_effect") or "").strip())
    ]
    return {
        "cycles": len(_rows(run.get("cycles"))),
        "actions_dispatched": int(run.get("actions_dispatched") or 0),
        "actions_responded": len(responded),
        "verified_successes": statuses.count("verified_success"),
        "verified_failures": statuses.count("verified_failure"),
        "partial_successes": statuses.count("partial_success"),
        "unverified_outcomes": sum(
            1
            for status in statuses
            if status
            in {
                "outcome_unknown",
                "verification_timed_out",
                "verification_aborted",
                "not_verifiable",
            }
        ),
        "recoveries": int(run.get("recoveries") or 0),
        "replans": int(run.get("replans") or 0),
        "evidence_refs": len(run.get("verification_evidence") or []),
    }


def dispatched_kinds(run: Mapping[str, Any]) -> tuple[list[str], int]:
    """The action kinds a run actually DISPATCHED, and how many it could not name.

    Only ``ok: true`` step records count: a refused step sent nothing, so it is
    not an action the task performed. The second value is the number of dispatched
    steps whose record carries no kind — an action nobody can attribute is
    reported as unlabelled rather than assumed permitted.
    """
    kinds: list[str] = []
    unlabelled = 0
    for cycle in _rows(run.get("cycles")):
        for row in _rows(_mapping(cycle.get("execution")).get("steps")):
            if row.get("ok") is not True:
                continue
            kind = str(row.get("kind") or "").strip().lower()
            if not kind:
                unlabelled += 1
                continue
            if kind not in kinds:
                kinds.append(kind)
    return sorted(kinds), unlabelled


def _stage(reached: bool, detail: str) -> dict[str, Any]:
    return {"reached": bool(reached), "detail": detail}


def task_stages(
    run: Mapping[str, Any], *, initial: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The objective's stages, each answered from what the run itself recorded.

    Kept separate because they are separate facts: a plan existing does not mean
    it was approved, an approval does not mean a step was requested, a request
    does not mean an input was dispatched, and a dispatch does not mean the game
    answered. ``completion_verified`` is left FALSE here on purpose — only
    :func:`judge_task`, after its evidence gate, may set it.
    """
    cycles = _rows(run.get("cycles"))
    outcomes = _flatten_outcomes(run)
    plans = [cycle for cycle in cycles if _mapping(cycle.get("plan"))]
    approvals = [
        cycle for cycle in cycles if _mapping(cycle.get("approval")).get("approved") is True
    ]
    requests = [
        row
        for cycle in cycles
        for row in _rows(_mapping(cycle.get("execution")).get("steps"))
    ]
    dispatched = [row for row in requests if row.get("ok") is True]
    responded = [
        outcome
        for outcome in outcomes
        if str(outcome.get("status") or "")
        in {"verified_success", "verified_failure", "partial_success"}
        or bool(str(outcome.get("observed_effect") or "").strip())
    ]
    progressed = [
        outcome
        for outcome in outcomes
        if str(outcome.get("status") or "") == "verified_success"
    ]
    objective_row = _mapping(cycles[-1].get("objective")) if cycles else {}
    objective_id = str(run.get("objective_id") or objective_row.get("objective_id") or "")
    objective_text = str(run.get("objective") or objective_row.get("description") or "")
    initial_row = dict(initial or {})
    phase = str(run.get("phase") or "")
    outcome = str(run.get("outcome") or "")
    return {
        "objective_identified": _stage(
            bool(objective_id or objective_text),
            objective_text or objective_id or "no objective was named",
        ),
        "initial_state_verified": _stage(
            bool(initial_row.get("state_id")),
            str(initial_row.get("scene") or "")
            or "no preflight reading was recorded for this run",
        ),
        "plan_generated": _stage(
            bool(plans), f"{len(plans)} cycle(s) produced a plan"
        ),
        "plan_approved": _stage(
            bool(approvals), f"{len(approvals)} cycle(s) recorded an approval"
        ),
        "action_requested": _stage(
            bool(requests), f"{len(requests)} step(s) were submitted"
        ),
        "input_dispatched": _stage(
            bool(dispatched) or int(run.get("actions_dispatched") or 0) > 0,
            f"{len(dispatched)} input operation(s) completed",
        ),
        "game_response_observed": _stage(
            bool(responded), f"{len(responded)} outcome(s) carried an observed effect"
        ),
        "progress_evidence_recorded": _stage(
            bool(progressed), f"{len(progressed)} verified success(es) recorded"
        ),
        "completion_verified": _stage(
            False,
            f"the run ended {outcome or '(unstated)'} in {phase or '(unstated)'}; "
            "verification is this judge's decision, not the run's",
        ),
    }


def _evidence_found(run: Mapping[str, Any]) -> list[str]:
    """Every reference the run produced, including the ones it did not verify."""
    found: list[str] = []
    for item in run.get("verification_evidence") or []:
        text = str(item or "")
        if text and text not in found:
            found.append(text)
    for ref in _accepted_refs(_flatten_outcomes(run)) + _objective_refs(run):
        if ref not in found:
            found.append(ref)
    # The scene/state tokens come from the LAST cycle: that is the reading the
    # completion was taken from. An earlier `scene:menu` cannot prove the screen
    # was a menu at the end, and treating it as if it could is how a stale
    # reading would satisfy a fresh claim.
    state = _mapping(_last_cycle(run).get("state"))
    state_id = str(state.get("state_id") or "")
    if state_id and f"state:{state_id}" not in found:
        found.append(f"state:{state_id}")
    scene = _mapping(state.get("scene"))
    kind = str(scene.get("kind") or "")
    if kind and kind != "unknown":
        token = f"scene:{kind}"
        if token not in found:
            found.append(token)
    return found


def judge_task(
    spec: TaskSpec,
    run: Mapping[str, Any],
    *,
    now: datetime | None = None,
    initial: Mapping[str, Any] | None = None,
) -> TaskReport:
    """Turn one finished run into the task's verdict — the phase's ONE judge.

    Completion is the only verdict that can be wrong in the operator's favour, so
    it is the only one with gates: the run must have ended ``objective_completed``
    from a ``COMPLETED`` phase, every tag in ``spec.required_evidence`` must be
    present in the evidence the run actually produced, AND every action the run
    dispatched must be one this task permitted. Everything else maps to a distinct
    verdict, never to "probably fine".

    ``initial`` is the runner's preflight reading (its ``state_id``/scene). It is
    recorded on the objective's stage ladder, never used as completion evidence:
    the screen the task started from is not the screen it finished on.
    """
    moment = now or _now()
    phase = str(run.get("phase") or "").upper()
    outcome = str(run.get("outcome") or "")
    found = _evidence_found(run)
    missing = [
        str(tag)
        for tag in spec.required_evidence
        if not any(str(tag).casefold() in item.casefold() for item in found)
    ]
    progress = task_progress(run)
    stages = task_stages(run, initial=initial)
    dispatched, unlabelled = dispatched_kinds(run)
    permitted = sorted({str(item).strip().lower() for item in spec.permitted_actions})
    # An action the task did not declare is outside the boundary the operator
    # approved. The build's control policy may still have allowed it; the TASK's
    # policy is narrower, and a claim of completion under a wider policy than the
    # task declared would be a claim about a different task.
    violations = [kind for kind in dispatched if kind not in permitted]
    report = TaskReport(
        task_id=spec.task_id,
        session_id=str(run.get("session_id") or ""),
        status="finished",
        game=str(run.get("game") or spec.game or ""),
        target=str(
            _mapping(run.get("target")).get("title") or spec.mission.target_title
        ),
        objective=str(run.get("objective") or spec.mission.objective),
        success_conditions=list(spec.success_conditions),
        failure_conditions=list(spec.failure_conditions),
        required_evidence=list(spec.required_evidence),
        evidence_found=found,
        missing_evidence=missing,
        limits=dict(run.get("limits") or spec.to_dict()["limits"]),
        progress=progress,
        stages=stages,
        evidence_source=str(run.get("evidence_source") or ""),
        simulated=str(run.get("evidence_source") or "") != "live",
        started_at=str(run.get("started_at") or ""),
        ended_at=str(run.get("ended_at") or "") or _stamp(moment),
        verification={
            "run_outcome": outcome or None,
            "run_phase": phase or None,
            "verification_evidence": list(run.get("verification_evidence") or []),
            "action_policy": {
                "permitted": permitted,
                "dispatched": dispatched,
                "unlabelled": unlabelled,
                "violations": violations,
            },
        },
        run={
            "run_id": str(run.get("run_id") or ""),
            "phase": phase,
            "outcome": outcome,
            "cycles": len(_rows(run.get("cycles"))),
            "actions_dispatched": progress["actions_dispatched"],
            "actions_responded": progress["actions_responded"],
            "evidence_source": str(run.get("evidence_source") or ""),
        },
    )

    if report.simulated:
        report.limitations.append(
            "This run's evidence source is "
            f"{report.evidence_source or 'unstated'!r}, not 'live': nothing here is a "
            "live-game result."
        )
    if unlabelled:
        report.limitations.append(
            f"{unlabelled} dispatched step(s) carried no action kind, so they could not be "
            "checked against the task's permitted actions."
        )
    if violations:
        report.limitations.append(
            "The run dispatched "
            + ", ".join(violations)
            + ", which this task did not permit ("
            + (", ".join(permitted) or "none declared")
            + "). The task's action policy is a boundary, not a label: this layer can "
            "report the breach but cannot pre-filter a plan the existing supervisor "
            "already built."
        )

    if phase == "COMPLETED" and outcome == AutonomyOutcome.OBJECTIVE_COMPLETED.value:
        if violations or unlabelled:
            breach = ", ".join(violations)
            if unlabelled:
                breach = (breach + "; " if breach else "") + (
                    f"{unlabelled} dispatched step(s) carrying no action kind"
                )
            report.verdict = TaskVerdict.SAFETY_STOP.value
            report.code = "action_policy_exceeded"
            report.reason = (
                "The run reported the objective completed, but it dispatched "
                f"{breach}, which cannot be shown to be one this task permits; "
                "completion under an unproven action policy is not this task's "
                "completion."
            )
            report.verification["basis"] = "none"
            return report
        if missing:
            report.verdict = TaskVerdict.OUTCOME_UNKNOWN.value
            report.code = "evidence_missing"
            report.reason = (
                "The run reported the objective completed, but the task's required "
                f"evidence was not present ({', '.join(missing)}). Completion is not "
                "claimed from an ending alone."
            )
            report.verification["basis"] = "none"
            return report
        if not found:
            report.verdict = TaskVerdict.OUTCOME_UNKNOWN.value
            report.code = "no_evidence"
            report.reason = (
                "The run reported the objective completed without recording a single "
                "evidence reference; nothing can be re-checked, so completion is not "
                "claimed."
            )
            report.verification["basis"] = "none"
            return report
        report.verdict = TaskVerdict.VERIFIED_COMPLETION.value
        report.code = "objective_completed"
        report.reason = (
            "The run ended COMPLETED with objective_completed and every required "
            "evidence reference was present."
        )
        report.stages["completion_verified"] = _stage(
            True,
            "the run's ending and the task's required evidence agree, and every "
            "dispatched action was permitted",
        )
        # Only a verified effect in the cycle the completion was read from makes
        # NF-21 the basis; an unrelated earlier success does not.
        last_outcomes = _rows(
            _mapping(_last_cycle(run).get("verification")).get("outcomes")
        )
        report.verification["basis"] = (
            "nf21_verified_effect"
            if _accepted_refs(last_outcomes)
            else "nf17_objective_evidence"
        )
        report.limitations.append(
            "Completion is a claim about the objective's own evidence, not proof that "
            "every action behaved as intended."
        )
        if spec.failure_conditions:
            report.limitations.append(
                "The declared failure conditions are documentation for the operator; this "
                "layer does not evaluate them. A run can only fail through NF-17/NF-21's "
                "objective verdict."
            )
        return report

    mapping = {
        AutonomyOutcome.OBJECTIVE_FAILED.value: (
            TaskVerdict.OBJECTIVE_FAILED,
            "objective_failed",
        ),
        AutonomyOutcome.SESSION_CANCELLED.value: (
            TaskVerdict.CANCELLED,
            "cancelled",
        ),
        AutonomyOutcome.SAFETY_STOP.value: (TaskVerdict.SAFETY_STOP, "safety_stop"),
        AutonomyOutcome.DEPENDENCY_FAILURE.value: (
            TaskVerdict.DEPENDENCY_UNAVAILABLE,
            "dependency_unavailable",
        ),
        AutonomyOutcome.OBSERVATION_UNAVAILABLE.value: (
            TaskVerdict.INTERRUPTED,
            "observation_unavailable",
        ),
    }
    if outcome in mapping:
        verdict, code = mapping[outcome]
        report.verdict = verdict.value
        report.code = code
        report.reason = str(run.get("reason") or f"The run ended {outcome}.")
        return report

    if outcome == AutonomyOutcome.SESSION_TIMED_OUT.value:
        if progress["verified_successes"] > 0:
            report.verdict = TaskVerdict.PROGRESS_ONLY.value
            report.code = "progress_without_completion"
            report.reason = (
                "The session's time budget was spent with verified progress but no "
                "verified completion."
            )
        else:
            report.verdict = TaskVerdict.INTERRUPTED.value
            report.code = "timed_out"
            report.reason = (
                "The session's time budget was spent without verified progress; the "
                "objective is neither complete nor failed."
            )
        return report

    if outcome == AutonomyOutcome.RESOURCE_LIMIT_REACHED.value:
        if progress["verified_successes"] > 0:
            report.verdict = TaskVerdict.PROGRESS_ONLY.value
            report.code = "resource_limit_with_progress"
            report.reason = (
                "A resource limit ended the run after verified progress; completion was "
                "never established."
            )
        else:
            report.verdict = TaskVerdict.RESOURCE_REFUSED.value
            report.code = "resource_limit"
            report.reason = str(
                run.get("reason") or "A resource limit ended the run before progress."
            )
        return report

    report.verdict = TaskVerdict.OUTCOME_UNKNOWN.value
    report.code = "outcome_unknown"
    report.reason = str(
        run.get("reason")
        or (
            f"The run ended in phase {phase or '(unstated)'} with outcome "
            f"{outcome or '(unstated)'}; nothing about the objective was established."
        )
    )
    return report


# ── the gateway the runner needs (NF-22 + this manager's readers) ──────────


@runtime_checkable
class TaskGateway(Protocol):
    """The slice of the product a task runner is allowed to use.

    Every member maps to something that already exists: the NF-22 controller's
    start/wait/cancel/runs, and the manager's own session, state, objective,
    control-policy and resource readers.
    """

    def session(self, session_id: str) -> Any | None: ...

    def target_title(self, session_id: str) -> str: ...

    async def read_state(self) -> Mapping[str, Any]: ...

    def objective_picture(self, session_id: str) -> Mapping[str, Any]: ...

    def allowed_actions(self) -> Sequence[str]: ...

    def resource_gate(self) -> Mapping[str, Any]: ...

    async def start_run(
        self,
        session_id: str,
        spec: TaskSpec,
        *,
        approved: bool,
        note: str,
    ) -> Mapping[str, Any]: ...

    async def wait_run(
        self, session_id: str, *, timeout: float | None
    ) -> Mapping[str, Any]: ...

    def run(self, session_id: str) -> Mapping[str, Any] | None: ...

    async def cancel(self, session_id: str, *, reason: str) -> Mapping[str, Any]: ...

    def held_inputs(self) -> Sequence[Mapping[str, Any]] | None:
        """Held input as the control layer sees it, or None when unreadable."""
        ...


class AutonomyTaskGateway:
    """The adapter from the product to :class:`TaskGateway`.

    ``autonomy`` is the ONE :class:`~novacontrol.novaforge.autonomy.AutonomousSessionController`;
    the callables are the manager's own readers, passed in rather than reached
    for, so a test can drive this without a manager and the product keeps ONE
    wiring in :meth:`NovaForgeManager.tasks`.
    """

    def __init__(
        self,
        *,
        autonomy: Any,
        sessions: Callable[[str], Any | None],
        target_title: Callable[[str], str],
        read_state: Callable[[], Awaitable[Mapping[str, Any]]],
        objective_picture: Callable[[str], Mapping[str, Any]],
        allowed_actions: Callable[[], Sequence[str]],
        resource_gate: Callable[[], Mapping[str, Any]],
        held_inputs: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    ) -> None:
        self.autonomy = autonomy
        self.sessions = sessions
        self.target_title = target_title
        self._read_state = read_state
        self.objective_picture = objective_picture
        self.allowed_actions = allowed_actions
        self.resource_gate = resource_gate
        self._held_inputs = held_inputs

    def session(self, session_id: str) -> Any | None:
        return self.sessions(session_id)

    async def read_state(self) -> Mapping[str, Any]:
        payload = await self._read_state()
        return payload if isinstance(payload, Mapping) else {}

    async def start_run(
        self,
        session_id: str,
        spec: TaskSpec,
        *,
        approved: bool,
        note: str,
    ) -> Mapping[str, Any]:
        answer = await self.autonomy.start(
            session_id,
            approved=approved,
            note=note,
            task=spec.mission.objective,
            limits=spec.autonomy_limits(),
        )
        return answer if isinstance(answer, Mapping) else {}

    async def wait_run(
        self, session_id: str, *, timeout: float | None
    ) -> Mapping[str, Any]:
        answer = await self.autonomy.wait(session_id, timeout=timeout)
        return answer if isinstance(answer, Mapping) else {}

    def run(self, session_id: str) -> Mapping[str, Any] | None:
        record = getattr(self.autonomy, "runs", {}).get(session_id)
        if record is None:
            return None
        payload = record.to_dict()
        return payload if isinstance(payload, Mapping) else None

    async def cancel(self, session_id: str, *, reason: str) -> Mapping[str, Any]:
        answer = await self.autonomy.cancel(session_id, reason=reason)
        return answer if isinstance(answer, Mapping) else {}

    def held_inputs(self) -> Sequence[Mapping[str, Any]] | None:
        if self._held_inputs is None:
            return None
        try:
            return tuple(self._held_inputs())
        except Exception:  # noqa: BLE001 - an unreadable control layer is not "zero held"
            return None


# ── the runner ─────────────────────────────────────────────────────────────


class TaskRunner:
    """Preflight, start, wait and judge — never plan, never inject, never verify.

    The runner's own state is only a ledger: which task is active on which
    session, and what each attempt established. It creates no loop, so there is
    exactly one observe→act→verify implementation in the build (NF-22's).
    """

    def __init__(
        self,
        gateway: TaskGateway,
        *,
        clock: Callable[[], datetime] | None = None,
        max_reports: int = 20,
    ) -> None:
        self.gateway = gateway
        self.clock = clock or _now
        self.max_reports = max(1, int(max_reports))
        self._active: dict[str, TaskReport] = {}
        self._reports: list[TaskReport] = []
        self._counts = {"started": 0, "refused": 0, "finished": 0}

    # -- state -------------------------------------------------------------

    @property
    def active(self) -> dict[str, str]:
        return {session: report.task_id for session, report in self._active.items()}

    def status(self) -> dict[str, Any]:
        return {
            "schema_version": TASK_SCHEMA_VERSION,
            "active": dict(self.active),
            "counts": dict(self._counts),
            "reports": [report.to_dict() for report in self._reports[-self.max_reports :]],
        }

    def last_report(self, session_id: str = "") -> TaskReport | None:
        for report in reversed(self._reports):
            if not session_id or report.session_id == session_id:
                return report
        return None

    # -- start -------------------------------------------------------------

    async def start(
        self,
        spec: TaskSpec,
        session_id: str,
        *,
        approved: bool = False,
        note: str = "",
    ) -> TaskReport:
        """Run every preflight gate, then hand the task to the NF-22 controller.

        A refusal is a finished report with a code, never an exception: the panel
        has to be able to explain why nothing ran.
        """
        report = TaskReport(
            task_id=spec.task_id,
            session_id=session_id,
            objective=spec.mission.objective.strip(),
            target=spec.mission.target_title.strip(),
            game=str(spec.game or "").strip(),
            success_conditions=list(spec.success_conditions),
            failure_conditions=list(spec.failure_conditions),
            required_evidence=list(spec.required_evidence),
            limits=spec.to_dict()["limits"],
        )
        problems = spec.errors()
        if problems:
            return self._refuse(report, "invalid_task", "; ".join(problems))
        if spec.requires_approval and not approved:
            return self._refuse(
                report,
                "approval_required",
                "A task may send bounded input to the game, so it needs the operator's "
                "explicit approval. Nothing was started.",
            )
        try:
            known_session = self.gateway.session(session_id)
        except Exception as exc:  # noqa: BLE001 - a broken reader is a refusal
            return self._refuse(
                report,
                "dependency_unavailable",
                f"The session list could not be read: {type(exc).__name__}: {exc}",
            )
        if known_session is None:
            return self._refuse(
                report,
                "unknown_session",
                f"No NovaForge session is named {session_id!r}.",
            )
        if session_id in self._active:
            return self._refuse(
                report,
                "already_running",
                f"Task {self._active[session_id].task_id!r} is already running on this "
                "session; two tasks on one window is how input races.",
            )
        try:
            title = str(self.gateway.target_title(session_id) or "")
        except Exception as exc:  # noqa: BLE001 - a broken reader is a refusal
            return self._refuse(
                report,
                "target_unverified",
                f"The pinned target could not be read: {type(exc).__name__}: {exc}",
            )
        if not title:
            return self._refuse(
                report,
                "target_unverified",
                "The session has not pinned a target window; nothing may be sent.",
            )
        report.target = title
        wanted = spec.mission.target_title.strip().casefold()
        if wanted not in title.casefold():
            return self._refuse(
                report,
                "target_mismatch",
                f"The pinned window {title!r} is not the task's target "
                f"{spec.mission.target_title!r}.",
            )
        try:
            allowed = {
                str(item).strip().lower() for item in self.gateway.allowed_actions()
            }
        except Exception as exc:  # noqa: BLE001 - a broken reader is a refusal
            return self._refuse(
                report,
                "unsupported_action",
                f"The control policy could not be read: {type(exc).__name__}: {exc}",
            )
        if not allowed:
            return self._refuse(
                report,
                "unsupported_action",
                "No input path is wired in this build, so no task action could be performed; "
                "the task was refused rather than started without a way to act.",
            )
        unsupported = [
            kind for kind in spec.permitted_actions if kind.strip().lower() not in allowed
        ]
        if unsupported:
            return self._refuse(
                report,
                "unsupported_action",
                "This build cannot perform "
                + ", ".join(sorted(unsupported))
                + "; the task was refused rather than narrowed.",
            )
        # The initial state is the one reading the task's first plan will rest on.
        try:
            state_payload = dict(await self.gateway.read_state())
        except Exception as exc:  # noqa: BLE001 - an unreadable state is a refusal
            return self._refuse(
                report,
                "initial_state_unavailable",
                f"The state could not be read: {type(exc).__name__}: {exc}",
            )
        state = _mapping(state_payload.get("state"))
        if not state_payload.get("analyzed") or not state:
            return self._refuse(
                report,
                "initial_state_unavailable",
                str(
                    state_payload.get("reason")
                    or "No validated state exists yet; a task never starts on a guess."
                ),
            )
        state_game = str(state.get("game") or "").strip()
        wanted_game = str(spec.game or "").strip()
        if state_game and wanted_game:
            left, right = state_game.casefold(), wanted_game.casefold()
            if left not in right and right not in left:
                return self._refuse(
                    report,
                    "game_mismatch",
                    f"The state layer identifies the game as {state_game!r}, not the "
                    f"task's {wanted_game!r}.",
                )
        scene = _mapping(state.get("scene"))
        scene_status = str(scene.get("status") or "unknown")
        scene_kind = str(scene.get("kind") or "unknown")
        if scene_status not in {"observed", "inferred"} or scene_kind == "unknown":
            return self._refuse(
                report,
                "initial_state_unknown",
                "The screen could not be named ("
                + str(scene.get("evidence") or scene_status)
                + "); a task never starts on an unclassified screen.",
            )
        # Freshness is a property of the FRAME the state was read from, not of when
        # the analysis ran: a state built seconds ago from an old observation is an
        # old reading. ``observation.at`` is the frame's own time; the state
        # timestamp is only the fallback when the reference does not carry one.
        observation_row = _mapping(state.get("observation"))
        age = _age_seconds(
            str(observation_row.get("at") or state.get("timestamp") or ""), self.clock()
        )
        if age is None:
            return self._refuse(
                report,
                "initial_state_stale",
                "The initial observation carries no readable timestamp, so its freshness "
                "cannot be established.",
            )
        if age > float(spec.observation_max_age_seconds):
            return self._refuse(
                report,
                "initial_state_stale",
                f"The initial state is {age:.1f} s old; this task requires at most "
                f"{spec.observation_max_age_seconds:g} s.",
            )
        if spec.initial_scenes and scene_kind not in {
            item.strip().casefold() for item in spec.initial_scenes
        }:
            return self._refuse(
                report,
                "initial_state_mismatch",
                f"The task starts from {', '.join(spec.initial_scenes)} but the screen is "
                f"{scene_kind!r}.",
            )
        try:
            resources = _mapping(self.gateway.resource_gate())
        except Exception as exc:  # noqa: BLE001 - an unreadable gate is "not measured"
            resources = {
                "allow": None,
                "reason": f"The resource gate could not be read: {type(exc).__name__}: {exc}",
            }
        report.resource_gate = resources
        if resources.get("allow") is False:
            return self._refuse(
                report,
                "resource_gate_refused",
                str(resources.get("reason") or "The resource gate refused the task."),
            )
        report.initial = {
            "observation_id": str(observation_row.get("observation_id") or ""),
            "state_id": str(state.get("state_id") or ""),
            "scene": scene_kind,
            "scene_status": scene_status,
            "scene_evidence": list(scene.get("evidence") or []),
            "age_seconds": round(age, 3),
            "sequence": state.get("sequence"),
            "read_at": _stamp(self.clock()),
        }
        try:
            answer = dict(
                await self.gateway.start_run(
                    session_id, spec, approved=approved, note=str(note or "")
                )
            )
        except Exception as exc:  # noqa: BLE001 - a start fault is a refusal, not a crash
            return self._refuse(
                report,
                "start_failed",
                f"The supervisor raised {type(exc).__name__}: {exc}. Nothing is claimed.",
            )
        if not answer.get("started"):
            return self._refuse(
                report,
                str(answer.get("code") or "not_started"),
                str(answer.get("reason") or "The supervisor declined to start the task."),
            )
        report.status = "running"
        report.code = "started"
        report.reason = "The bounded task started through the existing supervisor."
        report.started_at = _stamp(self.clock())
        run = _mapping(answer.get("status"))
        report.run = {
            "run_id": str(answer.get("run_id") or run.get("run_id") or ""),
            "objective_id": str(answer.get("objective_id") or ""),
            "limits": dict(answer.get("limits") or {}),
        }
        report.limitations.append(
            "An action's completion means the input operation finished; only a fresh "
            "observation and the verifier can establish an effect."
        )
        self._active[session_id] = report
        self._counts["started"] += 1
        return report

    async def finish(
        self,
        spec: TaskSpec,
        session_id: str,
        *,
        timeout: float | None = None,
    ) -> TaskReport:
        """Wait for the run's own ending, then judge it from its evidence."""
        active = self._active.get(session_id)
        if active is not None and active.task_id != spec.task_id:
            report = TaskReport(task_id=spec.task_id, session_id=session_id, status="refused")
            return self._refuse(
                report,
                "wrong_task",
                f"Task {active.task_id!r} is the one running on this session.",
            )
        if active is not None and active.objective and active.objective != spec.mission.objective.strip():
            report = TaskReport(task_id=spec.task_id, session_id=session_id, status="refused")
            return self._refuse(
                report,
                "wrong_task",
                "The running task's objective is not this spec's objective; a shared task id "
                "does not make two different tasks the same task.",
            )
        bound = timeout if timeout is not None else float(spec.max_seconds) + 10.0
        try:
            waited = dict(await self.gateway.wait_run(session_id, timeout=bound))
        except Exception as exc:  # noqa: BLE001 - an unreadable ending is not success
            report = active or TaskReport(task_id=spec.task_id, session_id=session_id)
            report.status = "finished"
            report.verdict = TaskVerdict.OUTCOME_UNKNOWN.value
            report.code = "wait_failed"
            report.reason = (
                f"The supervisor's run could not be followed: {type(exc).__name__}: {exc}. "
                "No completion is claimed."
            )
            report.ended_at = _stamp(self.clock())
            self._archive(session_id, report)
            return report
        try:
            fallback_run = self.gateway.run(session_id)
        except Exception:  # noqa: BLE001 - an unreadable record is not a completion
            fallback_run = None
        payload = _mapping(waited.get("status")) or _mapping(fallback_run)
        if not payload:
            report = active or TaskReport(task_id=spec.task_id, session_id=session_id)
            report.status = "finished"
            report.verdict = TaskVerdict.OUTCOME_UNKNOWN.value
            report.code = "no_run_record"
            report.reason = "The supervisor holds no run record for this session."
            report.ended_at = _stamp(self.clock())
            self._archive(session_id, report)
            return report
        if active is not None:
            started_run = str((active.run or {}).get("run_id") or "")
            finished_run = str(payload.get("run_id") or "")
            if started_run and finished_run and started_run != finished_run:
                report = TaskReport(task_id=spec.task_id, session_id=session_id)
                return self._refuse(
                    report,
                    "wrong_run",
                    f"The task started run {started_run!r} but the supervisor now holds "
                    f"{finished_run!r}; another run replaced it, so it is not this task's ending.",
                )
        if active is None:
            # Finishing without having started through this runner is only
            # allowed for the SAME task: a run that names a different objective
            # is someone else's work, and judging it as this task would credit
            # the wrong attempt.
            run_task = str(payload.get("task") or "").strip()
            if run_task and run_task != spec.mission.objective.strip():
                report = TaskReport(task_id=spec.task_id, session_id=session_id)
                return self._refuse(
                    report,
                    "wrong_task",
                    "The session's run belongs to another objective "
                    f"({run_task!r}); it is not this task's run to judge.",
                )
        report = judge_task(
            spec,
            payload,
            now=self.clock(),
            initial=(active.initial if active is not None else None),
        )
        if active is not None:
            report.initial = active.initial or report.initial
            report.resource_gate = active.resource_gate or report.resource_gate
            report.started_at = active.started_at or report.started_at
        if not waited.get("finished"):
            report.status = "running"
            report.verdict = ""
            report.code = "still_running"
            report.reason = str(
                waited.get("reason")
                or "The run had not reached a terminal phase when it was read."
            )
            self._active[session_id] = report
            return report
        report.status = "finished"
        # Input release is a MEASUREMENT, not an implication of the run ending.
        # When the control layer can be read, its held list is recorded; when it
        # cannot, the report says nothing rather than assuming zero.
        held = self.gateway.held_inputs()
        if held is not None:
            report.verification["held_inputs_after"] = len(held)
            if held:
                report.limitations.append(
                    f"{len(held)} input(s) were still held after the run ended; the game "
                    "may not be in a neutral state."
                )
        self._archive(session_id, report)
        return report

    async def run(
        self,
        spec: TaskSpec,
        session_id: str,
        *,
        approved: bool = False,
        note: str = "",
        timeout: float | None = None,
    ) -> TaskReport:
        """Start and judge one task. The single entry point the product uses."""
        started = await self.start(spec, session_id, approved=approved, note=note)
        if started.status != "running":
            return started
        return await self.finish(spec, session_id, timeout=timeout)

    async def cancel(
        self, session_id: str, *, reason: str = "user"
    ) -> dict[str, Any]:
        """Stop a running task through the supervisor's own cancel path.

        The answer carries the supervisor's cleanup, which is where held input is
        released; the runner never claims a release it did not see. A CONFIRMED
        cancellation also ends the task here: the ledger must stop claiming an
        activity that no longer exists, or the session would be refused
        ``already_running`` forever. A cancel the supervisor did not confirm — and
        a cancel that could not be delivered — leaves the ledger as it was, because
        an unconfirmed stop is not a stop.
        """
        if session_id not in self._active:
            return {
                "cancelled": False,
                "code": "no_active_task",
                "reason": "No task is running on this session.",
            }
        report = self._active[session_id]
        try:
            answer = dict(await self.gateway.cancel(session_id, reason=reason))
        except Exception as exc:  # noqa: BLE001 - an unanswered cancel is not a stop
            message = (
                f"The cancellation could not be delivered ({type(exc).__name__}: {exc}); "
                "the task stays active because nothing confirmed that it stopped."
            )
            report.limitations.append(message)
            return {
                "cancelled": False,
                "code": "cancel_failed",
                "reason": message,
                "held_inputs": None,
                "input_release_measured": False,
                "task": report.to_dict(),
            }
        # The supervisor's answer is not a release record: it may carry no
        # cleanup at all. Held input is therefore MEASURED through the control
        # layer when a reader is wired, and reported as unmeasured otherwise.
        # Turning "nobody said" into "held=0" would be the phase's own honesty
        # failure.
        cleanup = _mapping(answer.get("cleanup"))
        held = self.gateway.held_inputs()
        if held is None:
            report.limitations.append(
                "Cancellation was requested; input release was not measured (no held-input "
                "reader is wired), so this answer does not claim the game is free of held keys."
            )
        else:
            report.limitations.append(
                "Cancellation was requested; the control layer reported "
                f"{len(held)} held input(s) afterwards."
            )
        confirmed = answer.get("cancelled") is True
        run_row = _mapping(answer.get("status"))
        run_outcome = str(run_row.get("outcome") or "")
        report.reason = str(answer.get("reason") or f"Task cancelled: {reason}.")
        if not confirmed:
            # The runner believes a task is active and the supervisor does not
            # confirm stopping anything: the two views disagree. The ledger
            # follows the supervisor (nothing is running to supervise) without
            # claiming a cancellation nobody performed.
            report.status = "finished"
            report.verdict = TaskVerdict.OUTCOME_UNKNOWN.value
            report.code = str(answer.get("code") or "cancel_unconfirmed")
            report.reason = (
                str(answer.get("reason") or "The supervisor did not confirm a cancellation.")
                + " The task is no longer supervised, and nothing about the objective was "
                "established by this call."
            )
            report.ended_at = _stamp(self.clock())
            self._archive(session_id, report)
            return {
                "cancelled": False,
                "code": report.code,
                "reason": report.reason,
                "cleanup": cleanup,
                "held_inputs": len(held) if held is not None else None,
                "input_release_measured": held is not None,
                "task": report.to_dict(),
            }
        run_phase = str(run_row.get("phase") or "")
        if run_phase and run_outcome not in {"", AutonomyOutcome.SESSION_CANCELLED.value}:
            # The run had already ended some other way (completed, failed, stopped)
            # when the cancel arrived. Reporting "cancelled" would overwrite the
            # run's own ending, and judging that ending is the judge's job, not a
            # cancel path's — so the task is left without a verdict here.
            completed = run_outcome == AutonomyOutcome.OBJECTIVE_COMPLETED.value
            report.verdict = TaskVerdict.OUTCOME_UNKNOWN.value
            report.code = (
                "cancelled_after_completion" if completed else "cancelled_after_ending"
            )
            report.reason = (
                f"The run had already ended as {run_outcome or run_phase} when the "
                "cancellation arrived; the cancel path does not apply the task's "
                "evidence gate, so no completion is claimed here. Judge the run to "
                "establish what it did."
            )
        else:
            report.verdict = TaskVerdict.CANCELLED.value
            report.code = "cancelled"
        report.status = "finished"
        report.ended_at = _stamp(self.clock())
        self._archive(session_id, report)
        return {
            "cancelled": True,
            "code": "cancelled",
            "reason": report.reason,
            "cleanup": cleanup,
            "held_inputs": len(held) if held is not None else None,
            "input_release_measured": held is not None,
            "task": report.to_dict(),
        }

    # -- internals ---------------------------------------------------------

    def _refuse(self, report: TaskReport, code: str, reason: str) -> TaskReport:
        report.status = "refused"
        report.verdict = ""
        report.code = code
        report.reason = reason
        report.ended_at = _stamp(self.clock())
        self._counts["refused"] += 1
        self._remember(report)
        return report

    def _archive(self, session_id: str, report: TaskReport) -> None:
        self._active.pop(session_id, None)
        self._counts["finished"] += 1
        self._remember(report)

    def _remember(self, report: TaskReport) -> None:
        self._reports.append(report)
        if len(self._reports) > self.max_reports:
            del self._reports[: len(self._reports) - self.max_reports]


def monotonic_seconds() -> float:
    """A monotonic reading, for callers that want to time a task's own work."""
    return time.monotonic()
