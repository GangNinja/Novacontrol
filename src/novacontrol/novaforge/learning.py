"""NF-26 — learning from verified outcomes, with the evidence attached.

NF-13A–NF-25 already observe, interpret, plan, act, verify, recover and judge.
What none of them does is remember, across tasks, *which way of doing things
worked* — and that is this module's whole job. It is deliberately the smallest
layer that can do it honestly:

* :class:`ExperienceRecord` — one task attempt, built from NF-25's judged
  report. Facts and interpretations live in separate fields; a dispatched input
  is a count, never a success label, and an uncertain ending stays uncertain.
* :class:`StrategyMetrics` / :func:`compare_strategies` — per-strategy metrics
  with sample size and uncertainty, never one unexplained score.
* :class:`StrategyProfile` / :class:`AdaptationEngine` — bounded, versioned,
  reversible changes to how the *existing* planner orders and bounds what it
  already does.
* :class:`ExperienceStore` — versioned persistence with provenance, duplicate
  handling, corrupt-row tolerance and bounded growth.
* :func:`analyse_failures` — recurring problems, with counterevidence, kept as
  hypotheses unless a record's own evidence names the cause.
* :class:`StrategyExperiment` — bounded comparison of a known-safe baseline
  against a candidate inside an approved NF-25 task.

The distinctions this module exists to keep:

    selected more often  !=  better
    more actions sent    !=  progress
    internal score       !=  measured outcome
    correlation          !=  causation
    simulation           !=  live evidence

Nothing here captures a frame, interprets a state, sends input or verifies an
outcome: it reads NF-25's report and returns a recommendation the planner may
ignore.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .control import CONTROL_KINDS
from .planner import StrategyHint
from .tasks import TaskVerdict

EXPERIENCE_SCHEMA_VERSION = "nf26.1"
LEARNING_NAMESPACE = "novaforge_learning"
STORE_VERSION = 1

#: Both arms of a comparison need at least this many DECIDED trials before any
#: superiority is claimed. Below it the answer is ``insufficient_evidence``.
MIN_TRIALS = 5
DEFAULT_MAX_RECORDS = 200
DEFAULT_MAX_CHANGES = 50
MAX_RECORDS_ALLOWED = 2000
MAX_CHANGES_ALLOWED = 500
DEFAULT_PREFERENCE_WEIGHT = 0.5

#: Keys an interpretation may never carry, because they are facts with a
#: different owner. Merging them would let a story overwrite a measurement.
FACT_KEYS = frozenset(
    {
        "experience_id",
        "task_id",
        "session_id",
        "objective",
        "game",
        "label",
        "verdict",
        "outcome",
        "actions_dispatched",
        "actions_responded",
        "verified_progress",
        "evidence_refs",
        "evidence_source",
        "simulated",
    }
)


class ExperienceLabel(StrEnum):
    """What a task attempt established — the only labels evidence may carry.

    ``verified_success`` needs NF-25's ``verified_completion`` (which itself
    needed the run's ending *and* the task's evidence *and* its action policy).
    ``verified_failure`` is NF-21's own ``objective_failed``. Everything else —
    cancelled, safety stop, resource refusal, dependency failure, interrupted,
    progress-only, outcome unknown — is ``uncertain`` and is excluded from the
    success and failure rates, because it is not evidence about the strategy.
    """

    VERIFIED_SUCCESS = "verified_success"
    VERIFIED_FAILURE = "verified_failure"
    UNCERTAIN = "uncertain"


_VERIFIED_FAILURE_VERDICTS = frozenset({TaskVerdict.OBJECTIVE_FAILED.value})


def label_for_verdict(verdict: str) -> ExperienceLabel:
    """The label NF-25's verdict earns, and nothing more."""
    text = str(verdict or "").strip().lower()
    if text == TaskVerdict.VERIFIED_COMPLETION.value:
        return ExperienceLabel.VERIFIED_SUCCESS
    if text in _VERIFIED_FAILURE_VERDICTS:
        return ExperienceLabel.VERIFIED_FAILURE
    return ExperienceLabel.UNCERTAIN


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime | None = None) -> str:
    return (moment or _now()).isoformat()


def _parse(stamp: str) -> datetime | None:
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def _duration_seconds(started: str, ended: str) -> float | None:
    """Measured seconds between two stamps, or ``None`` when unmeasurable."""
    left, right = _parse(started), _parse(ended)
    if left is None or right is None:
        return None
    return max(0.0, (right - left).total_seconds())


def _same_experience(first: "ExperienceRecord", second: "ExperienceRecord") -> bool:
    """Two records of the SAME run that say the same thing.

    ``recorded_at`` is when the learning layer *heard about* the run, not part of
    what it heard: replaying one report must deduplicate, while a re-judged run
    (a different verdict or a different count) is an update and replaces the row.
    """
    left = {key: value for key, value in first.to_dict().items() if key != "recorded_at"}
    right = {key: value for key, value in second.to_dict().items() if key != "recorded_at"}
    return left == right


def _failure_codes(report: Mapping[str, Any]) -> list[str]:
    """The failure-ish codes the report itself carries — no interpretation."""
    codes: list[str] = []
    verdict = str(report.get("verdict") or "")
    code = str(report.get("code") or "").strip()
    if code and verdict != TaskVerdict.VERIFIED_COMPLETION.value:
        codes.append(code)
    verification = _mapping(report.get("verification"))
    policy = _mapping(verification.get("action_policy"))
    for kind in policy.get("violations") or []:
        codes.append(f"policy:{kind}")
    if _int(policy.get("unlabelled")) > 0:
        codes.append("policy:unlabelled")
    if _int(verification.get("held_inputs_after")) > 0:
        codes.append("input_held")
    for tag in report.get("missing_evidence") or []:
        codes.append(f"missing_evidence:{tag}")
    seen: list[str] = []
    for item in codes:
        text = str(item).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


@dataclass(frozen=True, slots=True)
class ExperienceRecord:
    """One task attempt: what happened, kept apart from what it might mean."""

    experience_id: str
    task_id: str
    session_id: str = ""
    objective: str = ""
    game: str = ""
    profile: str = ""
    strategy_id: str = "baseline"
    strategy_version: int = 0
    label: str = ExperienceLabel.UNCERTAIN.value
    verdict: str = ""
    code: str = ""
    initial: dict[str, Any] = field(default_factory=dict)
    transitions: tuple[dict[str, Any], ...] = ()
    actions_requested: int | None = None
    actions_dispatched: int = 0
    actions_responded: int = 0
    verified_progress: int = 0
    recoveries: int = 0
    failure_codes: tuple[str, ...] = ()
    resource: dict[str, Any] = field(default_factory=dict)
    timing: dict[str, Any] = field(default_factory=dict)
    confidence: float | None = None
    evidence_refs: tuple[str, ...] = ()
    evidence_source: str = ""
    simulated: bool = True
    interpretation: dict[str, Any] = field(default_factory=dict)
    recorded_at: str = ""
    schema_version: str = EXPERIENCE_SCHEMA_VERSION

    # ── derived facts ──────────────────────────────────────────────────────
    @property
    def completed(self) -> bool:
        return self.label == ExperienceLabel.VERIFIED_SUCCESS.value

    @property
    def uncertain(self) -> bool:
        return self.label == ExperienceLabel.UNCERTAIN.value

    @property
    def unnecessary_actions(self) -> int:
        """Dispatched inputs the run never saw a response for.

        A dispatch whose effect could not be read is not proof of waste — it is
        proof of a missing reading. The count is therefore a *measurement gap*
        as much as an efficiency number, and is reported that way.
        """
        return max(0, int(self.actions_dispatched) - int(self.actions_responded))

    @property
    def seconds(self) -> float | None:
        return _float_or_none(self.timing.get("seconds"))

    def errors(self) -> list[str]:
        problems: list[str] = []
        if not str(self.experience_id or "").strip():
            problems.append("An experience record needs an id.")
        if not str(self.task_id or "").strip():
            problems.append("An experience record must name the task it came from.")
        if not str(self.objective or "").strip():
            problems.append("An experience record must name the objective it was for.")
        if not str(self.game or "").strip():
            problems.append("An experience record must name its game.")
        if self.label not in {item.value for item in ExperienceLabel}:
            problems.append(f"{self.label!r} is not an experience label.")
        for name in ("actions_dispatched", "actions_responded", "verified_progress", "recoveries"):
            if _int(getattr(self, name)) < 0:
                problems.append(f"{name} cannot be negative.")
        if int(self.strategy_version) < 0:
            problems.append("strategy_version cannot be negative.")
        if self.actions_requested is not None:
            if int(self.actions_requested) < 0:
                problems.append("actions_requested cannot be negative.")
            elif int(self.actions_dispatched) > int(self.actions_requested):
                problems.append(
                    "actions_dispatched cannot exceed actions_requested: a run cannot send "
                    "more inputs than it submitted."
                )
        if self.actions_responded > self.actions_dispatched:
            problems.append(
                "actions_responded cannot exceed actions_dispatched: the game cannot answer "
                "an input that was never sent."
            )
        if self.confidence is not None and not (0.0 <= float(self.confidence) <= 1.0):
            problems.append("confidence must be between 0 and 1 when measured.")
        if not isinstance(self.simulated, bool):
            problems.append("simulated must be a boolean, not a value that merely looks true.")
        source = str(self.evidence_source or "").strip().casefold()
        if not self.simulated and source and source != "live":
            # Promotion refused at the record: a row cannot say it is live while
            # its own source field says it came from a simulation.
            problems.append(
                "A record cannot claim live evidence while its own evidence_source says "
                f"{self.evidence_source!r}: simulation is never promoted into live evidence."
            )
        if str(self.recorded_at or "").strip() and _parse(self.recorded_at) is None:
            problems.append("recorded_at must be a readable timestamp.")
        for key in self.interpretation:
            if str(key) in FACT_KEYS:
                problems.append(
                    f"interpretation may not carry the fact {key!r}: an interpretation is not "
                    "a measurement."
                )
        return problems

    @property
    def valid(self) -> bool:
        return not self.errors()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "experience_id": self.experience_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "objective": self.objective,
            "game": self.game,
            "profile": self.profile,
            "strategy_id": self.strategy_id,
            "strategy_version": int(self.strategy_version),
            "label": self.label,
            "verdict": self.verdict,
            "code": self.code,
            "initial": dict(self.initial),
            "transitions": [dict(row) for row in self.transitions],
            "actions_requested": self.actions_requested,
            "actions_dispatched": int(self.actions_dispatched),
            "actions_responded": int(self.actions_responded),
            "verified_progress": int(self.verified_progress),
            "recoveries": int(self.recoveries),
            "failure_codes": list(self.failure_codes),
            "resource": dict(self.resource),
            "timing": dict(self.timing),
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
            "evidence_source": self.evidence_source,
            "simulated": bool(self.simulated),
            "interpretation": dict(self.interpretation),
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExperienceRecord | None":
        """Read a stored row back, or ``None`` when it cannot be trusted.

        A row from another schema version, or one whose required facts are
        missing, is dropped rather than guessed at: a record nobody can read is
        not evidence, and importing it under today's field meanings is how stale
        data becomes a modern label.
        """
        data = _mapping(payload)
        if str(data.get("schema_version") or "") != EXPERIENCE_SCHEMA_VERSION:
            return None
        if not str(data.get("experience_id") or "").strip():
            return None
        record = cls(
            experience_id=str(data.get("experience_id")).strip(),
            task_id=str(data.get("task_id") or "").strip(),
            session_id=str(data.get("session_id") or ""),
            objective=str(data.get("objective") or ""),
            game=str(data.get("game") or ""),
            profile=str(data.get("profile") or ""),
            strategy_id=str(data.get("strategy_id") or "baseline"),
            strategy_version=_int(data.get("strategy_version")),
            label=str(data.get("label") or ExperienceLabel.UNCERTAIN.value),
            verdict=str(data.get("verdict") or ""),
            code=str(data.get("code") or ""),
            initial=_mapping(data.get("initial")),
            transitions=tuple(_rows(data.get("transitions"))),
            actions_requested=(
                None
                if data.get("actions_requested") is None
                else _int(data.get("actions_requested"))
            ),
            actions_dispatched=_int(data.get("actions_dispatched")),
            actions_responded=_int(data.get("actions_responded")),
            verified_progress=_int(data.get("verified_progress")),
            recoveries=_int(data.get("recoveries")),
            failure_codes=tuple(str(item) for item in data.get("failure_codes") or []),
            resource=_mapping(data.get("resource")),
            timing=_mapping(data.get("timing")),
            confidence=_float_or_none(data.get("confidence")),
            evidence_refs=tuple(str(item) for item in data.get("evidence_refs") or []),
            evidence_source=str(data.get("evidence_source") or ""),
            simulated=bool(data.get("simulated", True)),
            interpretation=_mapping(data.get("interpretation")),
            recorded_at=str(data.get("recorded_at") or ""),
        )
        return record if record.valid else None

    @classmethod
    def from_task_report(
        cls,
        report: Mapping[str, Any],
        *,
        run: Mapping[str, Any] | None = None,
        strategy_id: str = "baseline",
        strategy_version: int = 0,
        profile: str = "",
        confidence: float | None = None,
        failure_codes: Iterable[str] = (),
        interpretation: Mapping[str, Any] | None = None,
        recorded_at: datetime | None = None,
    ) -> "ExperienceRecord | None":
        """Build the experience of ONE judged task, or ``None`` when none ran.

        ``None`` is the answer for a report that never reached a run — a refused
        spec, a missing approval, an unknown session. Those are preflight
        refusals, not trials of a strategy, and counting them would let a badly
        written task look like a failed strategy. An interrupted run IS recorded:
        its ending is evidence, and its label says ``uncertain``.

        ``run`` is NF-22's own run payload when the caller has it. It is what
        carries the submitted steps and the observed transitions; without it
        those two facts are reported as not measured rather than guessed.
        """
        data = _mapping(report)
        run_row = _mapping(data.get("run"))
        run_id = str(run_row.get("run_id") or "").strip()
        if not run_id:
            return None
        raw = _mapping(run)
        verdict = str(data.get("verdict") or "")
        progress = _mapping(data.get("progress"))
        verification = _mapping(data.get("verification"))
        timing = {
            "started_at": str(data.get("started_at") or ""),
            "ended_at": str(data.get("ended_at") or ""),
            "seconds": _duration_seconds(
                str(data.get("started_at") or ""), str(data.get("ended_at") or "")
            ),
        }
        # The run payload wins; a report whose own run row carries its cycles is
        # the same evidence, so both sides read one list and cannot disagree.
        cycle_rows = _rows(raw.get("cycles")) or _rows(run_row.get("cycles"))
        transitions: list[dict[str, Any]] = []
        requested: int | None = None
        if cycle_rows:
            # Zero submitted steps is a MEASUREMENT only when a cycle actually
            # carried its execution block; a payload that omitted the step lists
            # did not observe zero, it observed nothing, and claiming zero would
            # contradict a run that reports inputs it dispatched.
            total = 0
            measured = False
            for cycle in cycle_rows:
                execution = _mapping(cycle.get("execution"))
                if execution:
                    measured = True
                total += len(_rows(execution.get("steps")))
                state = _mapping(cycle.get("state"))
                scene = _mapping(state.get("scene"))
                if scene or state:
                    transitions.append(
                        {
                            "cycle": cycle.get("index"),
                            "state_id": str(state.get("state_id") or ""),
                            # A run payload that carries no scene did not observe an
                            # "unknown" one: say nothing rather than invent a reading.
                            "scene": str(scene.get("kind") or ""),
                            "scene_status": str(scene.get("status") or ""),
                        }
                    )
            requested = total if measured else None
        # The game PROFILE is the state layer's profile id, not the game's name:
        # a task can be read through more than one profile, and recording the game
        # here would claim an identity nobody observed. When the caller supplies
        # none, take the newest one the run's own cycles recorded.
        derived_profile = ""
        for cycle in reversed(cycle_rows):
            candidate = str(_mapping(cycle.get("state")).get("profile") or "").strip()
            if candidate:
                derived_profile = candidate
                break
        refs: list[str] = []
        for item in [*(data.get("evidence_found") or []), *(verification.get("verification_evidence") or [])]:
            text = str(item or "")
            if text and text not in refs:
                refs.append(text)
        codes = list(_failure_codes(data))
        for item in failure_codes:
            text = str(item or "").strip()
            if text and text not in codes:
                codes.append(text)
        evidence_source = str(
            data.get("evidence_source") or run_row.get("evidence_source") or ""
        )
        record = cls(
            experience_id=f"exp:{str(data.get('task_id') or '').strip()}:{run_id}",
            task_id=str(data.get("task_id") or "").strip(),
            session_id=str(data.get("session_id") or run_row.get("session_id") or ""),
            objective=str(data.get("objective") or run_row.get("objective") or ""),
            game=str(data.get("game") or run_row.get("game") or ""),
            profile=str(profile or derived_profile),
            strategy_id=str(strategy_id or "baseline"),
            strategy_version=_int(strategy_version),
            label=label_for_verdict(verdict).value,
            verdict=verdict,
            code=str(data.get("code") or ""),
            initial=_mapping(data.get("initial")),
            transitions=tuple(transitions),
            actions_requested=requested,
            actions_dispatched=_int(progress.get("actions_dispatched")),
            actions_responded=_int(progress.get("actions_responded")),
            verified_progress=_int(progress.get("verified_successes")),
            recoveries=_int(progress.get("recoveries")),
            failure_codes=tuple(codes),
            resource={
                "gate": _mapping(data.get("resource_gate")),
                "held_inputs_after": verification.get("held_inputs_after"),
                "limits": _mapping(data.get("limits")),
            },
            timing=timing,
            confidence=confidence,
            evidence_refs=tuple(refs),
            evidence_source=evidence_source,
            simulated=bool(data.get("simulated", evidence_source != "live")),
            interpretation=dict(interpretation or {}),
            recorded_at=_stamp(recorded_at),
        )
        return record if record.valid else None


def wilson_interval(successes: int, trials: int, *, z: float = 1.96) -> tuple[float, float] | None:
    """The 95 % Wilson interval for a rate, or ``None`` without trials.

    Wilson rather than the textbook normal interval because these samples are
    small and the rate is often near 0 or 1, where the normal interval produces
    bounds outside ``[0, 1]`` that make an overlap test meaningless.
    """
    if trials <= 0:
        return None
    phat = max(0.0, min(1.0, successes / trials))
    denominator = 1.0 + (z * z) / trials
    centre = (phat + (z * z) / (2 * trials)) / denominator
    margin = (
        z
        * math.sqrt((phat * (1 - phat) / trials) + (z * z) / (4 * trials * trials))
        / denominator
    )
    return (max(0.0, centre - margin), min(1.0, centre + margin))


@dataclass(frozen=True, slots=True)
class StrategyMetrics:
    """Per-strategy measurements, each with its own name and direction."""

    strategy_id: str = ""
    version: int = 0
    game: str = ""
    tasks: int = 0
    verified_successes: int = 0
    verified_failures: int = 0
    uncertain: int = 0
    decided: int = 0
    completion_rate: float | None = None
    uncertainty_rate: float | None = None
    verified_progress_per_minute: float | None = None
    actions_dispatched: int = 0
    actions_responded: int = 0
    unnecessary_actions: int = 0
    failed_action_rate: float | None = None
    recoveries: int = 0
    recovery_rate: float | None = None
    safety_stops: int = 0
    resource_refusals: int = 0
    policy_breaches: int = 0
    mean_task_seconds: float | None = None
    measured_seconds: int = 0
    live_records: int = 0
    simulated_records: int = 0
    completion_interval: tuple[float, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "version": int(self.version),
            "game": self.game,
            "tasks": int(self.tasks),
            "verified_successes": int(self.verified_successes),
            "verified_failures": int(self.verified_failures),
            "uncertain": int(self.uncertain),
            "decided": int(self.decided),
            "completion_rate": self.completion_rate,
            "uncertainty_rate": self.uncertainty_rate,
            "verified_progress_per_minute": self.verified_progress_per_minute,
            "actions_dispatched": int(self.actions_dispatched),
            "actions_responded": int(self.actions_responded),
            "unnecessary_actions": int(self.unnecessary_actions),
            "failed_action_rate": self.failed_action_rate,
            "recoveries": int(self.recoveries),
            "recovery_rate": self.recovery_rate,
            "safety_stops": int(self.safety_stops),
            "resource_refusals": int(self.resource_refusals),
            "policy_breaches": int(self.policy_breaches),
            "mean_task_seconds": self.mean_task_seconds,
            "measured_seconds": int(self.measured_seconds),
            "live_records": int(self.live_records),
            "simulated_records": int(self.simulated_records),
            "completion_interval": list(self.completion_interval)
            if self.completion_interval
            else None,
        }


def metrics_for(
    records: Sequence[ExperienceRecord],
    *,
    strategy_id: str = "",
    version: int | None = None,
    game: str = "",
) -> StrategyMetrics:
    """Measure one arm. A rate with no denominator is ``None``, never 0.0."""
    chosen = [
        record
        for record in records
        if (not strategy_id or record.strategy_id == strategy_id)
        and (version is None or int(record.strategy_version) == int(version))
        and (not game or record.game.casefold() == game.casefold())
    ]
    successes = sum(1 for item in chosen if item.completed)
    failures = sum(
        1 for item in chosen if item.label == ExperienceLabel.VERIFIED_FAILURE.value
    )
    uncertain = len(chosen) - successes - failures
    decided = successes + failures
    dispatched = sum(int(item.actions_dispatched) for item in chosen)
    responded = sum(int(item.actions_responded) for item in chosen)
    measured = [item for item in chosen if item.seconds is not None]
    progress_total = sum(int(item.verified_progress) for item in chosen)
    measured_minutes = sum(float(item.seconds or 0.0) for item in measured) / 60.0
    if strategy_id:
        label = strategy_id
    elif chosen:
        label = str(chosen[0].strategy_id)
    else:
        label = ""
    versions = {int(item.strategy_version) for item in chosen}
    return StrategyMetrics(
        strategy_id=label,
        version=int(version) if version is not None else (min(versions) if versions else 0),
        game=game,
        tasks=len(chosen),
        verified_successes=successes,
        verified_failures=failures,
        uncertain=uncertain,
        decided=decided,
        completion_rate=(successes / decided) if decided else None,
        uncertainty_rate=(uncertain / len(chosen)) if chosen else None,
        verified_progress_per_minute=(
            (progress_total / measured_minutes) if measured_minutes > 0 else None
        ),
        actions_dispatched=dispatched,
        actions_responded=responded,
        unnecessary_actions=max(0, dispatched - responded),
        failed_action_rate=(
            (max(0, dispatched - responded) / dispatched) if dispatched else None
        ),
        recoveries=sum(int(item.recoveries) for item in chosen),
        recovery_rate=(
            (sum(int(item.recoveries) for item in chosen) / len(chosen)) if chosen else None
        ),
        safety_stops=sum(
            1 for item in chosen if str(item.verdict) == TaskVerdict.SAFETY_STOP.value
        ),
        resource_refusals=sum(
            1 for item in chosen if str(item.verdict) == TaskVerdict.RESOURCE_REFUSED.value
        ),
        policy_breaches=sum(
            1 for item in chosen if any(code.startswith("policy:") for code in item.failure_codes)
        ),
        mean_task_seconds=(
            (sum(float(item.seconds or 0.0) for item in measured) / len(measured))
            if measured
            else None
        ),
        measured_seconds=len(measured),
        live_records=sum(1 for item in chosen if not item.simulated),
        simulated_records=sum(1 for item in chosen if item.simulated),
        completion_interval=wilson_interval(successes, decided),
    )


#: Metrics where a LARGER number is worse. Used only to state directions; the
#: comparator decides with intervals and the safety gate, never by summing these.
HIGHER_IS_WORSE = frozenset(
    {
        "uncertainty_rate",
        "unnecessary_actions",
        "failed_action_rate",
        "recovery_rate",
        "safety_stops",
        "resource_refusals",
        "policy_breaches",
        "mean_task_seconds",
    }
)

SAFETY_METRICS = ("safety_stops", "resource_refusals", "policy_breaches", "uncertainty_rate")


@dataclass(frozen=True, slots=True)
class StrategyComparison:
    """One baseline-versus-candidate comparison, with the doubt left in."""

    baseline: StrategyMetrics
    candidate: StrategyMetrics
    primary_metric: str = "completion_rate"
    verdict: str = "insufficient_evidence"
    reason: str = ""
    metric_deltas: dict[str, float | None] = field(default_factory=dict)
    safety_ok: bool = True
    safety_notes: tuple[str, ...] = ()
    comparable: bool = False
    live_evidence: bool = False
    evidence_ids: tuple[str, ...] = ()
    sample: dict[str, int] = field(default_factory=dict)
    schema_version: str = EXPERIENCE_SCHEMA_VERSION

    @property
    def supported(self) -> bool:
        return self.verdict == "improvement" and self.safety_ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "baseline": self.baseline.to_dict(),
            "candidate": self.candidate.to_dict(),
            "primary_metric": self.primary_metric,
            "verdict": self.verdict,
            "reason": self.reason,
            "supported": self.supported,
            "metric_deltas": dict(self.metric_deltas),
            "safety_ok": self.safety_ok,
            "safety_notes": list(self.safety_notes),
            "comparable": self.comparable,
            "live_evidence": self.live_evidence,
            "sample": dict(self.sample),
            "evidence_ids": list(self.evidence_ids),
            "note": (
                "A verdict about the configured arms only. Sample size and uncertainty "
                "travel with it, simulated evidence is never reported as a live result, "
                "and a safety metric that got worse is never part of an improvement."
            ),
        }


def _metric_value(metrics: StrategyMetrics, name: str) -> float | None:
    value = getattr(metrics, name, None)
    if value is None:
        return None
    return float(value)


def compare_strategies(
    baseline_records: Sequence[ExperienceRecord],
    candidate_records: Sequence[ExperienceRecord],
    *,
    primary_metric: str = "completion_rate",
    min_trials: int = MIN_TRIALS,
    allowance: float = 0.0,
    strategy_id: str = "",
    version: int | None = None,
) -> StrategyComparison:
    """Compare two arms honestly: same conditions, enough trials, no spin.

    * Both arms must cover the same games, the same objectives and the same class
      of evidence (live with live, simulated with simulated). Comparing a live arm
      with a simulated one is not a comparison, and is refused by name.
    * Each arm needs ``min_trials`` DECIDED trials (successes + verified
      failures). Under that, the answer is ``insufficient_evidence`` — a lucky run
      or two is not an improvement.
    * ``improvement`` needs the primary metric to be better by more than
      ``allowance`` **and** the completion-rate intervals not to overlap (when the
      primary metric is the completion rate) **and** no safety metric to get worse.
    * A primary metric that improves while a safety metric worsens is
      ``regression``: the change is not acceptable whatever it bought.
    """
    baseline = metrics_for(baseline_records, strategy_id=strategy_id, version=version)
    candidate = metrics_for(candidate_records, version=version)
    deltas: dict[str, float | None] = {}
    for name in (
        "completion_rate",
        "uncertainty_rate",
        "verified_progress_per_minute",
        "unnecessary_actions",
        "failed_action_rate",
        "recovery_rate",
        "safety_stops",
        "resource_refusals",
        "mean_task_seconds",
    ):
        left, right = _metric_value(baseline, name), _metric_value(candidate, name)
        deltas[name] = None if left is None or right is None else round(right - left, 4)
    sample = {
        "baseline_decided": baseline.decided,
        "candidate_decided": candidate.decided,
        "baseline_tasks": baseline.tasks,
        "candidate_tasks": candidate.tasks,
    }
    evidence_ids = tuple(
        [item.experience_id for item in baseline_records]
        + [item.experience_id for item in candidate_records]
    )
    games_left = {item.game.casefold() for item in baseline_records}
    games_right = {item.game.casefold() for item in candidate_records}
    objectives_left = {item.objective.strip().casefold() for item in baseline_records}
    objectives_right = {item.objective.strip().casefold() for item in candidate_records}
    live_left = any(not item.simulated for item in baseline_records)
    live_right = any(not item.simulated for item in candidate_records)
    # An arm that carries BOTH classes is not "live with live": pooling its rows
    # into one completion rate is exactly the promotion of a simulation result
    # into live evidence this phase forbids, so such an arm is not comparable
    # even when the other arm happens to be mixed the same way.
    mixed_arms = [
        name
        for name, rows, live in (
            ("baseline", baseline_records, live_left),
            ("candidate", candidate_records, live_right),
        )
        if live and any(item.simulated for item in rows)
    ]
    spanning: list[str] = []
    if version is None:
        for name, rows in (
            ("baseline", baseline_records),
            ("candidate", candidate_records),
        ):
            if len({int(item.strategy_version) for item in rows}) > 1:
                spanning.append(name)
    # The SAME SET of games is not the same game: two arms both covering A and B
    # pass the set check while their rates pool across two titles, so a gain on
    # one can hide a regression on the other. One game per comparison.
    multi_game = [
        name
        for name, rows in (
            ("baseline", baseline_records),
            ("candidate", candidate_records),
        )
        if len({item.game.casefold() for item in rows}) > 1
    ]
    comparable = (
        bool(baseline_records)
        and bool(candidate_records)
        and (games_left == games_right)
        and (objectives_left == objectives_right)
        and live_left == live_right
        and not mixed_arms
        and not spanning
        and not multi_game
    )

    def result(
        verdict: str,
        reason: str,
        *,
        safety_ok: bool = True,
        safety_notes: Sequence[str] = (),
    ) -> StrategyComparison:
        return StrategyComparison(
            baseline=baseline,
            candidate=candidate,
            primary_metric=primary_metric,
            verdict=verdict,
            reason=reason,
            metric_deltas=deltas,
            safety_ok=safety_ok,
            safety_notes=tuple(safety_notes),
            comparable=comparable,
            live_evidence=bool(live_left and live_right),
            evidence_ids=evidence_ids,
            sample=sample,
        )

    if not comparable:
        if live_left != live_right:
            return result(
                "insufficient_evidence",
                "The two arms are not comparable: they must cover the same games, the same "
                "objectives, and the same class of evidence (live with live, simulated with "
                "simulated).",
            )
        if mixed_arms:
            return result(
                "insufficient_evidence",
                "The "
                + " and ".join(mixed_arms)
                + " arm mixes live and simulated records, so its rate would pool the two "
                "classes of evidence. Compare simulated with simulated, or live with live.",
            )
        if multi_game:
            return result(
                "insufficient_evidence",
                "The "
                + " and ".join(multi_game)
                + " arm spans more than one game, so its rate would mix two titles and could "
                "hide a regression in one of them. Compare one game at a time.",
            )
        if spanning:
            return result(
                "insufficient_evidence",
                "The "
                + " and ".join(spanning)
                + " arm spans more than one strategy version, so its rate would describe "
                "neither profile. Pin the comparison to one version.",
            )
        return result(
            "insufficient_evidence",
            "The two arms are not comparable: they must cover the same games, the same "
            "objectives, and the same class of evidence (live with live, simulated with "
            "simulated).",
        )
    if baseline.decided < int(min_trials) or candidate.decided < int(min_trials):
        return result(
            "insufficient_evidence",
            f"At least {int(min_trials)} decided trials per arm are needed to compare; "
            f"the baseline has {baseline.decided} and the candidate {candidate.decided}.",
        )
    left_value = _metric_value(baseline, primary_metric)
    right_value = _metric_value(candidate, primary_metric)
    if left_value is None or right_value is None:
        return result(
            "insufficient_evidence",
            f"The primary metric {primary_metric!r} was not measured for both arms.",
        )
    direction = "lower" if primary_metric in HIGHER_IS_WORSE else "higher"
    delta = round(right_value - left_value, 4)
    better = (delta < -float(allowance)) if direction == "lower" else (delta > float(allowance))
    worse = (delta > float(allowance)) if direction == "lower" else (delta < -float(allowance))
    safety_notes: list[str] = []
    safety_ok = True
    for name in SAFETY_METRICS:
        left, right = _metric_value(baseline, name), _metric_value(candidate, name)
        if left is None or right is None:
            continue
        if right > left + float(allowance):
            safety_ok = False
            safety_notes.append(
                f"{name} got worse: {left} -> {right} (a safety metric may not be traded "
                "for a better primary metric)."
            )
    if worse:
        return result("regression", f"{primary_metric} is worse: {left_value} -> {right_value}.")
    if better and not safety_ok:
        return result(
            "regression",
            f"{primary_metric} improved ({left_value} -> {right_value}) but a safety metric "
            "got worse, so this is not an acceptable change.",
            safety_ok=False,
            safety_notes=safety_notes,
        )
    if better:
        if primary_metric == "completion_rate":
            left_interval, right_interval = baseline.completion_interval, candidate.completion_interval
            if left_interval and right_interval and right_interval[0] <= left_interval[1]:
                return result(
                    "no_difference",
                    "The completion rate improved, but the intervals still overlap: the "
                    "difference is not established at this sample size.",
                    safety_notes=safety_notes,
                )
        return result(
            "improvement",
            f"{primary_metric} improved by {delta} over {sample['baseline_decided']} + "
            f"{sample['candidate_decided']} decided trials with no safety metric worse.",
            safety_notes=safety_notes,
        )
    return result(
        "no_difference",
        f"{primary_metric} differs by {delta}, which is inside the allowance "
        f"({allowance:g}); no improvement is claimed.",
        safety_notes=safety_notes,
    )


@dataclass(frozen=True, slots=True)
class StrategyProfile:
    """The WHOLE surface a learned strategy may change, and nothing else.

    A profile may ask the existing planner to order already-feasible candidates
    differently, to use a *shorter* plan and to demand *fresher* evidence. It
    cannot widen a budget, forbid an action outright, invent one, change a plan
    rule, or reach the input path: the planner clamps every field against its own
    policy, and the reference profile (``baseline``, version 0) is always
    available whatever a later profile says.
    """

    strategy_id: str = "baseline"
    version: int = 0
    max_actions_per_plan: int | None = None
    max_state_age_seconds: float | None = None
    preferred_kinds: tuple[str, ...] = ()
    avoided_kinds: tuple[str, ...] = ()
    preference_weight: float = DEFAULT_PREFERENCE_WEIGHT
    evidence: tuple[str, ...] = ()
    rationale: str = ""
    evaluated_by: str = ""
    created_at: str = ""
    supersedes: str = ""
    active: bool = True

    @property
    def key(self) -> str:
        return f"{self.strategy_id}@{int(self.version)}"

    @property
    def reference(self) -> bool:
        """True for the known-safe reference profile (nothing changed)."""
        return not self.any_change

    @property
    def any_change(self) -> bool:
        return bool(
            self.max_actions_per_plan is not None
            or self.max_state_age_seconds is not None
            or self.preferred_kinds
            or self.avoided_kinds
        )

    def errors(self) -> list[str]:
        problems: list[str] = []
        if not str(self.strategy_id or "").strip():
            problems.append("A profile must name its strategy.")
        if int(self.version) < 0:
            problems.append("A profile version cannot be negative.")
        if self.max_actions_per_plan is not None and not (1 <= int(self.max_actions_per_plan) <= 20):
            problems.append("max_actions_per_plan must be between 1 and 20.")
        if self.max_state_age_seconds is not None and not (
            0.1 <= float(self.max_state_age_seconds) <= 600.0
        ):
            problems.append("max_state_age_seconds must be between 0.1 and 600.")
        seen: set[str] = set()
        for field_name, kinds in (
            ("preferred_kinds", self.preferred_kinds),
            ("avoided_kinds", self.avoided_kinds),
        ):
            for kind in kinds:
                text = str(kind or "").strip().lower()
                if text not in CONTROL_KINDS:
                    problems.append(
                        f"{field_name} contains {kind!r}, which is not an action this build "
                        "can perform."
                    )
                if text in seen:
                    problems.append(f"{field_name} names {text!r} twice.")
                seen.add(text)
        overlap = {str(item).strip().lower() for item in self.preferred_kinds} & {
            str(item).strip().lower() for item in self.avoided_kinds
        }
        if overlap:
            problems.append(
                "A kind cannot be both preferred and avoided: " + ", ".join(sorted(overlap))
            )
        if not (0.0 <= float(self.preference_weight) <= 1.0):
            problems.append("preference_weight must be between 0 and 1.")
        if self.created_at.strip() and _parse(self.created_at) is None:
            problems.append("created_at must be a readable timestamp.")
        return problems

    @property
    def valid(self) -> bool:
        return not self.errors()

    def hint(self) -> StrategyHint:
        """This profile as the planner reads it (the planner clamps it again)."""
        return StrategyHint(
            strategy_id=str(self.strategy_id).strip() or "baseline",
            version=int(self.version),
            preferred_kinds=tuple(str(item).strip().lower() for item in self.preferred_kinds),
            avoided_kinds=tuple(str(item).strip().lower() for item in self.avoided_kinds),
            preference_weight=float(self.preference_weight),
            max_actions_per_plan=(
                None if self.max_actions_per_plan is None else int(self.max_actions_per_plan)
            ),
            max_state_age_seconds=(
                None
                if self.max_state_age_seconds is None
                else float(self.max_state_age_seconds)
            ),
            evidence=tuple(self.evidence),
            rationale=str(self.rationale or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "version": int(self.version),
            "key": self.key,
            "max_actions_per_plan": self.max_actions_per_plan,
            "max_state_age_seconds": self.max_state_age_seconds,
            "preferred_kinds": list(self.preferred_kinds),
            "avoided_kinds": list(self.avoided_kinds),
            "preference_weight": float(self.preference_weight),
            "evidence": list(self.evidence),
            "rationale": self.rationale,
            "evaluated_by": self.evaluated_by,
            "created_at": self.created_at,
            "supersedes": self.supersedes,
            "active": bool(self.active),
            "reference": self.reference,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StrategyProfile | None":
        data = _mapping(payload)
        if not str(data.get("strategy_id") or "").strip():
            return None
        profile = cls(
            strategy_id=str(data.get("strategy_id")).strip(),
            version=_int(data.get("version")),
            max_actions_per_plan=(
                None
                if data.get("max_actions_per_plan") is None
                else _int(data.get("max_actions_per_plan"))
            ),
            max_state_age_seconds=_float_or_none(data.get("max_state_age_seconds")),
            preferred_kinds=tuple(str(item) for item in data.get("preferred_kinds") or []),
            avoided_kinds=tuple(str(item) for item in data.get("avoided_kinds") or []),
            preference_weight=float(_float_or_none(data.get("preference_weight")) or 0.0),
            evidence=tuple(str(item) for item in data.get("evidence") or []),
            rationale=str(data.get("rationale") or ""),
            evaluated_by=str(data.get("evaluated_by") or ""),
            created_at=str(data.get("created_at") or ""),
            supersedes=str(data.get("supersedes") or ""),
            active=bool(data.get("active", True)),
        )
        return profile if profile.valid else None


@dataclass(frozen=True, slots=True)
class StrategyChange:
    """Every adaptation is a record: what changed, why, and how to undo it.

    ``rejected_reason`` is set when the evidence did not support the change — a
    comparison that was not an improvement, an invalid profile, or a missing
    approval. A rejected change is still stored: the request and the reason are
    part of the record of what this system refused to learn.
    """

    change_id: str
    strategy_id: str
    previous: StrategyProfile
    proposed: StrategyProfile
    comparison: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()
    expected_benefit: str = ""
    risks: tuple[str, ...] = ()
    conditions: dict[str, Any] = field(default_factory=dict)
    method: str = ""
    created_at: str = ""
    applied_at: str = ""
    approved: bool = False
    rejected_reason: str = ""
    rolled_back_at: str = ""
    rollback_reason: str = ""
    schema_version: str = EXPERIENCE_SCHEMA_VERSION

    @property
    def applied(self) -> bool:
        return bool(self.applied_at)

    @property
    def rolled_back(self) -> bool:
        return bool(self.rolled_back_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "change_id": self.change_id,
            "strategy_id": self.strategy_id,
            "previous": self.previous.to_dict(),
            "proposed": self.proposed.to_dict(),
            "comparison": dict(self.comparison),
            "evidence": list(self.evidence),
            "expected_benefit": self.expected_benefit,
            "risks": list(self.risks),
            "conditions": dict(self.conditions),
            "method": self.method,
            "created_at": self.created_at,
            "applied_at": self.applied_at,
            "approved": bool(self.approved),
            "applied": self.applied,
            "rejected_reason": self.rejected_reason,
            "rolled_back_at": self.rolled_back_at,
            "rolled_back": self.rolled_back,
            "rollback_reason": self.rollback_reason,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StrategyChange | None":
        data = _mapping(payload)
        change_id = str(data.get("change_id") or "").strip()
        previous = StrategyProfile.from_dict(_mapping(data.get("previous")))
        proposed = StrategyProfile.from_dict(_mapping(data.get("proposed")))
        if not change_id or previous is None or proposed is None:
            return None
        return cls(
            change_id=change_id,
            strategy_id=str(data.get("strategy_id") or proposed.strategy_id),
            previous=previous,
            proposed=proposed,
            comparison=_mapping(data.get("comparison")),
            evidence=tuple(str(item) for item in data.get("evidence") or []),
            expected_benefit=str(data.get("expected_benefit") or ""),
            risks=tuple(str(item) for item in data.get("risks") or []),
            conditions=_mapping(data.get("conditions")),
            method=str(data.get("method") or ""),
            created_at=str(data.get("created_at") or ""),
            applied_at=str(data.get("applied_at") or ""),
            approved=bool(data.get("approved", False)),
            rejected_reason=str(data.get("rejected_reason") or ""),
            rolled_back_at=str(data.get("rolled_back_at") or ""),
            rollback_reason=str(data.get("rollback_reason") or ""),
        )


class FailureBlame(StrEnum):
    """Where a recurring problem most plausibly sits — a bucket, not a verdict."""

    INVALID_OBSERVATION = "invalid_observation"
    STATE_INTERPRETATION = "state_interpretation"
    OBJECTIVE_DEFINITION = "objective_definition"
    PLAN_SELECTION = "plan_selection"
    INPUT_DISPATCH = "input_dispatch"
    NO_PROGRESS = "no_observable_progress"
    UNEXPECTED_TRANSITION = "unexpected_transition"
    INEFFECTIVE_RECOVERY = "ineffective_recovery"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    UNVERIFIABLE_OUTCOME = "unverifiable_outcome"
    UNKNOWN = "unknown"


#: Cause codes whose text names the cause outright. Only these make a pattern
#: ``verified``, and only with an occurrence of their own: a pattern is never
#: verified because it is frequent.
_DIRECT_CAUSES: dict[str, FailureBlame] = {
    "invalid_task": FailureBlame.OBJECTIVE_DEFINITION,
    "action_policy_exceeded": FailureBlame.OBJECTIVE_DEFINITION,
    "initial_state_unavailable": FailureBlame.STATE_INTERPRETATION,
    "initial_state_unknown": FailureBlame.STATE_INTERPRETATION,
    "initial_state_mismatch": FailureBlame.STATE_INTERPRETATION,
    "initial_state_stale": FailureBlame.STATE_INTERPRETATION,
    "state_stale": FailureBlame.STATE_INTERPRETATION,
    "unknown_state_age": FailureBlame.STATE_INTERPRETATION,
    "observation_unavailable": FailureBlame.INVALID_OBSERVATION,
    "timed_out": FailureBlame.INPUT_DISPATCH,
    "resource_limit": FailureBlame.RESOURCE_EXHAUSTION,
    "resource_limit_reached": FailureBlame.RESOURCE_EXHAUSTION,
    "resource_gate_refused": FailureBlame.RESOURCE_EXHAUSTION,
    "dependency_unavailable": FailureBlame.PLAN_SELECTION,
    "unsupported_action": FailureBlame.PLAN_SELECTION,
    "start_failed": FailureBlame.PLAN_SELECTION,
    "unknown_plan": FailureBlame.PLAN_SELECTION,
    "no_steps": FailureBlame.PLAN_SELECTION,
    "evidence_missing": FailureBlame.NO_PROGRESS,
    "no_evidence": FailureBlame.UNVERIFIABLE_OUTCOME,
    "not_verifiable": FailureBlame.UNVERIFIABLE_OUTCOME,
}


def _blame_for(codes: Sequence[str], verdict: str) -> FailureBlame:
    for code in codes:
        if code in _DIRECT_CAUSES:
            return _DIRECT_CAUSES[code]
    if str(verdict) == TaskVerdict.SAFETY_STOP.value:
        return FailureBlame.UNEXPECTED_TRANSITION
    if str(verdict) == TaskVerdict.PROGRESS_ONLY.value:
        return FailureBlame.NO_PROGRESS
    if str(verdict) == TaskVerdict.INTERRUPTED.value:
        return FailureBlame.INVALID_OBSERVATION
    return FailureBlame.UNKNOWN


#: Remediations are ADVICE about the recorded cause, never an automatic edit.
_REMEDIATION: dict[FailureBlame, str] = {
    FailureBlame.INVALID_OBSERVATION: (
        "Re-check the capture backend and the observation interval before re-planning; "
        "the evidence, not the plan, is what failed."
    ),
    FailureBlame.STATE_INTERPRETATION: (
        "Re-read the state (or add a profile for this screen) before acting; a plan built "
        "on a misread screen is not evidence about the actions in it."
    ),
    FailureBlame.OBJECTIVE_DEFINITION: (
        "Restate the objective with an observable success condition and the evidence that "
        "would prove it (NF-25 refuses one without both)."
    ),
    FailureBlame.PLAN_SELECTION: (
        "Inspect the candidates and the knowledge the plan used; a refusal from a stage is "
        "a dependency answer, not a failed game action."
    ),
    FailureBlame.INPUT_DISPATCH: (
        "Confirm the target window and the control gate, then re-read the state before "
        "sending anything else."
    ),
    FailureBlame.NO_PROGRESS: (
        "Re-read the objective's progress indicator; if it did not move, prefer a different "
        "candidate rather than repeating the same action."
    ),
    FailureBlame.UNEXPECTED_TRANSITION: (
        "Re-acquire the target and the state, and record what changed before resuming."
    ),
    FailureBlame.INEFFECTIVE_RECOVERY: (
        "Review the recovery decision for this outcome; a recovery that leaves the run in "
        "the same state is not a recovery."
    ),
    FailureBlame.RESOURCE_EXHAUSTION: (
        "Lower the task's budget or free the resource; a resource refusal is remembered but "
        "must not be attributed to the actions."
    ),
    FailureBlame.UNVERIFIABLE_OUTCOME: (
        "Keep the outcome unknown; add a per-action contract or a measurable progress "
        "indicator so the next attempt can be verified."
    ),
    FailureBlame.UNKNOWN: (
        "Gather another observation of the same signature before drawing any conclusion."
    ),
}


@dataclass(frozen=True, slots=True)
class FailurePattern:
    """Recurring problem signed by its own evidence, with its counterevidence.

    ``confidence`` is occurrences ÷ (occurrences + counterevidence) and is ``None``
    when there is no counterevidence at all — a ratio with no denominator is not a
    probability. ``verified`` is true only when a direct cause code appears in the
    pattern's own records; a frequent signature without one stays a hypothesis.
    """

    signature: str
    blame: str
    occurrences: int
    counterevidence: int
    confidence: float | None
    verified: bool
    games: tuple[str, ...] = ()
    strategies: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    failure_codes: tuple[str, ...] = ()
    affected_tasks: tuple[str, ...] = ()
    remediation: str = ""
    hypothesis: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "blame": self.blame,
            "occurrences": int(self.occurrences),
            "counterevidence": int(self.counterevidence),
            "confidence": self.confidence,
            "verified": bool(self.verified),
            "status": "verified_cause" if self.verified else "hypothesis",
            "games": list(self.games),
            "strategies": list(self.strategies),
            "evidence_refs": list(self.evidence_refs),
            "failure_codes": list(self.failure_codes),
            "affected_tasks": list(self.affected_tasks),
            "remediation": self.remediation,
            "hypothesis": self.hypothesis,
        }


def _signature(record: ExperienceRecord) -> tuple[str, list[str]]:
    codes = sorted(record.failure_codes) or [str(record.verdict or "unknown")]
    scene = ""
    if record.transitions:
        scene = str(record.transitions[-1].get("scene") or "")
    signature = "|".join(["failure", *codes, scene or "scene:unknown", record.game])
    return signature, codes


def analyse_failures(
    records: Sequence[ExperienceRecord], *, limit: int = 20
) -> list[FailurePattern]:
    """Group non-successful records by signature, with their counterevidence.

    Counterevidence is every record for the SAME game and strategy that shares
    neither the signature's codes nor its outcome: without those, a pattern is
    just a list of things that went wrong once. Correlation is never reported as
    causation — only a direct cause code can make a row ``verified``.
    """
    groups: dict[str, list[ExperienceRecord]] = {}
    for record in records:
        if record.label == ExperienceLabel.VERIFIED_SUCCESS.value:
            continue
        signature, _ = _signature(record)
        groups.setdefault(signature, []).append(record)
    patterns: list[FailurePattern] = []
    for signature, members in groups.items():
        _, codes = _signature(members[0])
        game = members[0].game
        strategy = members[0].strategy_id
        counter = [
            item
            for item in records
            if item.game.casefold() == game.casefold()
            and item.strategy_id == strategy
            and item not in members
            and item.label == ExperienceLabel.VERIFIED_SUCCESS.value
        ]
        blame = _blame_for(codes, members[0].verdict)
        direct = any(code in _DIRECT_CAUSES for code in codes)
        confidence = (
            len(members) / (len(members) + len(counter))
            if counter
            else None
        )
        patterns.append(
            FailurePattern(
                signature=signature,
                blame=blame.value,
                occurrences=len(members),
                counterevidence=len(counter),
                confidence=round(confidence, 4) if confidence is not None else None,
                verified=bool(direct),
                games=tuple(sorted({item.game for item in members})),
                strategies=tuple(sorted({item.strategy_id for item in members})),
                evidence_refs=tuple(
                    ref for item in members for ref in item.evidence_refs
                )[:20],
                failure_codes=tuple(codes),
                affected_tasks=tuple(sorted({item.task_id for item in members})),
                remediation=_REMEDIATION[blame],
                hypothesis=(
                    ""
                    if direct
                    else (
                        "This signature recurs, but nothing in the records names its cause: "
                        "it is a hypothesis (correlation is not causation), and no strategy "
                        "change may be based on it alone."
                    )
                ),
            )
        )
    patterns.sort(key=lambda item: (-item.occurrences, item.signature))
    return patterns[: max(1, int(limit))]


@dataclass(frozen=True, slots=True)
class LearningSnapshot:
    """Everything the learning store holds, after a forgiving read."""

    records: tuple[ExperienceRecord, ...] = ()
    changes: tuple[StrategyChange, ...] = ()
    profiles: tuple[StrategyProfile, ...] = ()
    dropped: int = 0
    duplicates: int = 0
    version: int = STORE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": int(self.version),
            "schema": EXPERIENCE_SCHEMA_VERSION,
            "records": [item.to_dict() for item in self.records],
            "changes": [item.to_dict() for item in self.changes],
            "profiles": [item.to_dict() for item in self.profiles],
        }


class ExperienceStore:
    """Versioned, bounded persistence for experience and strategy changes.

    The store it wraps is the application's own forgiving JSON snapshot store:
    a missing file reads as "nothing yet", an unreadable one does not raise, and
    a write that cannot land degrades instead of failing the work that produced
    the record. This class adds the learning layer's own rules on top: rows from
    another schema version or with missing facts are DROPPED and counted, growth
    is bounded oldest-first, and nothing here ever raises into a caller.
    """

    def __init__(
        self,
        store: Any | None = None,
        *,
        namespace: str = LEARNING_NAMESPACE,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_changes: int = DEFAULT_MAX_CHANGES,
    ) -> None:
        self.store = store
        self.namespace = str(namespace or LEARNING_NAMESPACE)
        self.max_records = max(1, min(int(max_records), MAX_RECORDS_ALLOWED))
        self.max_changes = max(1, min(int(max_changes), MAX_CHANGES_ALLOWED))
        self.last_error: dict[str, Any] | None = None

    @property
    def available(self) -> bool:
        return self.store is not None

    def read(self) -> LearningSnapshot:
        if self.store is None:
            return LearningSnapshot()
        try:
            payload = _mapping(self.store.read(self.namespace))
        except Exception as exc:  # noqa: BLE001 - an unreadable store is "nothing yet"
            self.last_error = {"code": "store_unreadable", "reason": f"{type(exc).__name__}: {exc}"}
            return LearningSnapshot()
        dropped = 0
        records: list[ExperienceRecord] = []
        seen: set[str] = set()
        duplicates = 0
        for row in _rows(payload.get("records")):
            record = ExperienceRecord.from_dict(row)
            if record is None:
                dropped += 1
                continue
            if record.experience_id in seen:
                duplicates += 1
                continue
            seen.add(record.experience_id)
            records.append(record)
        changes: list[StrategyChange] = []
        for row in _rows(payload.get("changes")):
            change = StrategyChange.from_dict(row)
            if change is None:
                dropped += 1
                continue
            changes.append(change)
        profiles: list[StrategyProfile] = []
        for row in _rows(payload.get("profiles")):
            profile = StrategyProfile.from_dict(row)
            if profile is None:
                dropped += 1
                continue
            profiles.append(profile)
        return LearningSnapshot(
            records=tuple(records[-self.max_records :]),
            changes=tuple(changes[-self.max_changes :]),
            profiles=tuple(profiles),
            dropped=dropped,
            duplicates=duplicates,
        )

    def write(self, snapshot: LearningSnapshot) -> bool:
        """Persist the ledger, bounded. A refused write is reported, not raised."""
        if self.store is None:
            return False
        bounded = LearningSnapshot(
            records=snapshot.records[-self.max_records :],
            changes=snapshot.changes[-self.max_changes :],
            profiles=snapshot.profiles,
        )
        try:
            self.store.write(self.namespace, bounded.to_dict())
        except Exception as exc:  # noqa: BLE001 - recording must not fail the work
            self.last_error = {"code": "store_unwritable", "reason": f"{type(exc).__name__}: {exc}"}
            return False
        return True

    def status(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "namespace": self.namespace,
            "max_records": self.max_records,
            "max_changes": self.max_changes,
            "last_error": dict(self.last_error) if self.last_error else None,
            "version": STORE_VERSION,
            "schema": EXPERIENCE_SCHEMA_VERSION,
        }


@dataclass
class AdaptationEngine:
    """Bounded, versioned, reversible strategy changes over an evidence gate.

    The engine never invents evidence and never applies a change on its own: a
    proposal must cite a comparison whose verdict is ``improvement``, and an
    application must carry the operator's approval when the policy requires it.
    Every profile it has ever activated is kept, so a rollback needs no rebuild.
    """

    require_approval: bool = True
    clock: Callable[[], datetime] = field(default=_now)
    _profiles: dict[str, StrategyProfile] = field(default_factory=dict, init=False, repr=False)
    _changes: list[StrategyChange] = field(default_factory=list, init=False, repr=False)
    _counter: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._profiles.setdefault("baseline", StrategyProfile(created_at=_stamp(self.clock())))

    # ── read surface ───────────────────────────────────────────────────────
    def active(self, strategy_id: str = "baseline") -> StrategyProfile:
        return self._profiles.get(str(strategy_id or "baseline"), self._profiles["baseline"])

    @property
    def profiles(self) -> list[StrategyProfile]:
        return list(self._profiles.values())

    @property
    def changes(self) -> list[StrategyChange]:
        return list(self._changes)

    def install(self, slot: str, profile: StrategyProfile) -> StrategyProfile:
        """Put a profile in a strategy slot outright, returning the old one.

        Used by a bounded experiment to set its independent variable: this is
        explicitly NOT an adaptation, writes no change record and claims no
        improvement. An invalid profile is refused (the slot keeps its value,
        and the caller learns that from the returned profile).
        """
        target = str(slot or "baseline")
        if not profile.valid:
            return self.active(target)
        previous = self._profiles.get(target)
        self._profiles[target] = profile
        return previous if previous is not None else self.active(target)

    def restore(self, profiles: Sequence[StrategyProfile], changes: Sequence[StrategyChange]) -> None:
        """Adopt persisted profiles and changes (the store's own rows)."""
        for profile in profiles:
            if profile.valid:
                self._profiles[profile.strategy_id] = profile
        self._changes = [item for item in changes]
        self._counter = max(self._counter, len(self._changes))

    def history(self, *, strategy_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
        rows = [
            item.to_dict()
            for item in reversed(self._changes)
            if not strategy_id or item.strategy_id == strategy_id
        ]
        return rows[: max(1, int(limit))]

    # ── write surface ──────────────────────────────────────────────────────
    def propose(
        self,
        profile: StrategyProfile,
        *,
        comparison: Mapping[str, Any] | StrategyComparison | None = None,
        evidence: Iterable[str] = (),
        expected_benefit: str = "",
        risks: Iterable[str] = (),
        conditions: Mapping[str, Any] | None = None,
        method: str = "",
        strategy_id: str = "",
    ) -> StrategyChange:
        """Record a proposed change — applied only later, and only if it earns it."""
        target = str(strategy_id or profile.strategy_id or "baseline")
        previous = self.active(target)
        row = (
            comparison.to_dict()
            if isinstance(comparison, StrategyComparison)
            else dict(comparison or {})
        )
        self._counter += 1
        change = StrategyChange(
            change_id=f"chg:{target}:{self._counter}",
            strategy_id=target,
            previous=previous,
            proposed=profile,
            comparison=row,
            evidence=tuple(str(item) for item in evidence) or tuple(
                str(item) for item in row.get("evidence_ids") or []
            ),
            expected_benefit=str(expected_benefit or ""),
            risks=tuple(str(item) for item in risks),
            conditions=dict(conditions or {}),
            method=str(method or ""),
            created_at=_stamp(self.clock()),
        )
        problems = profile.errors()
        if problems:
            change = self._reject(change, "invalid_profile: " + "; ".join(problems))
        elif row.get("verdict") != "improvement":
            change = self._reject(
                change,
                "evidence_insufficient: the comparison's verdict is "
                f"{row.get('verdict') or 'missing'!r}, so this change is not supported "
                "by verified outcomes.",
            )
        elif not change.evidence:
            change = self._reject(
                change,
                "no_evidence: a change must cite the experience ids that motivated it.",
            )
        self._changes.append(change)
        return change

    def _reject(self, change: StrategyChange, reason: str) -> StrategyChange:
        return StrategyChange(
            change_id=change.change_id,
            strategy_id=change.strategy_id,
            previous=change.previous,
            proposed=change.proposed,
            comparison=change.comparison,
            evidence=change.evidence,
            expected_benefit=change.expected_benefit,
            risks=change.risks,
            conditions=change.conditions,
            method=change.method,
            created_at=change.created_at,
            rejected_reason=reason,
        )

    def apply(self, change_id: str, *, approved: bool = False) -> StrategyChange:
        """Activate a proposed change, with the operator's approval and no rewrite.

        A change that was rolled back can be applied again: the operator undoing
        an adaptation is not a decision that the evidence got worse. Applying it
        again is a new activation and clears the rollback fields, so no record
        ever says both "applied" and "rolled back".
        """
        change = self._find(change_id)
        if change is None:
            return StrategyChange(
                change_id=str(change_id),
                strategy_id="baseline",
                previous=self.active(),
                proposed=self.active(),
                created_at=_stamp(self.clock()),
                rejected_reason="unknown_change",
            )
        if change.rejected_reason:
            return change
        if change.applied and not change.rolled_back:
            return change
        if self.require_approval and not approved:
            # A missing approval is a REFUSAL, not a rejection: the proposal is
            # still supported by evidence and stays appliable once the operator
            # approves it. Writing the refusal into the record would make an
            # unapproved call permanently poison a valid change.
            return self._reject(
                change,
                "approval_required: a strategy change alters how future plans choose "
                "among permitted actions, so it needs the operator's approval.",
            )
        applied = StrategyChange(
            change_id=change.change_id,
            strategy_id=change.strategy_id,
            previous=change.previous,
            proposed=change.proposed,
            comparison=change.comparison,
            evidence=change.evidence,
            expected_benefit=change.expected_benefit,
            risks=change.risks,
            conditions=change.conditions,
            method=change.method,
            created_at=change.created_at,
            applied_at=_stamp(self.clock()),
            approved=True,
        )
        installed = StrategyProfile.from_dict(
            {**change.proposed.to_dict(), "version": change.proposed.version}
        ) or change.proposed
        self._profiles[change.strategy_id] = installed
        # The ACTIVE slot is the one planning reads (``LearningService.hint``). An
        # applied change that lived only under its own strategy id would never
        # reach a plan, which is the opposite of an adaptation.
        self._profiles["baseline"] = installed
        return self._replace(change, applied)

    def rollback(self, strategy_id: str = "baseline", *, reason: str = "") -> StrategyProfile:
        """Return a strategy to the profile the last applied change replaced.

        The reference profile is always reachable: with nothing applied, a
        rollback is a no-op that says so rather than inventing a new profile.
        """
        target = str(strategy_id or "baseline")
        applied = [item for item in self._changes if item.strategy_id == target and item.applied]
        if not applied:
            # Nothing was applied FOR THIS SLOT. A change applied under another
            # strategy id still installed itself as the ACTIVE profile (that is
            # how an adaptation reaches planning), so the default rollback must
            # be able to take it back out: undoing the active adaptation is the
            # operator's whole purpose here.
            active = self._profiles.get("baseline")
            if active is not None:
                applied = [
                    item
                    for item in self._changes
                    if item.applied and item.proposed.key == active.key
                ]
        if not applied:
            return self.active(target)
        last = applied[-1]
        # Restore BOTH the slot the change was proposed for and the slot it was
        # installed into (the active one planning reads), otherwise a rollback
        # would leave the adapted profile in force on one of the two paths.
        self._profiles[last.strategy_id] = last.previous
        if target != last.strategy_id:
            self._profiles[target] = last.previous
        if self._profiles.get("baseline") is not None and (
            self._profiles["baseline"].key == last.proposed.key
        ):
            self._profiles["baseline"] = last.previous
        self._replace(
            last,
            StrategyChange(
                change_id=last.change_id,
                strategy_id=last.strategy_id,
                previous=last.previous,
                proposed=last.proposed,
                comparison=last.comparison,
                evidence=last.evidence,
                expected_benefit=last.expected_benefit,
                risks=last.risks,
                conditions=last.conditions,
                method=last.method,
                created_at=last.created_at,
                applied_at=last.applied_at,
                approved=last.approved,
                rolled_back_at=_stamp(self.clock()),
                rollback_reason=str(reason or "operator rollback"),
            ),
        )
        return self._profiles[target]

    # ── internals ─────────────────────────────────────────────────────────
    def _find(self, change_id: str) -> StrategyChange | None:
        for item in self._changes:
            if item.change_id == str(change_id):
                return item
        return None

    def _replace(self, old: StrategyChange, new: StrategyChange) -> StrategyChange:
        for index, item in enumerate(self._changes):
            if item.change_id == old.change_id:
                self._changes[index] = new
                break
        return new


@dataclass(frozen=True, slots=True)
class StrategyExperiment:
    """A bounded comparison of a known-safe baseline against one candidate.

    It runs *inside* an approved NF-25 task definition: the task's budgets and
    permitted actions are the experiment's budgets, the arms differ only in the
    profile the planner receives, and the arm for each trial is decided by
    alternation so the run is reproducible without a random seed.
    """

    experiment_id: str
    task: Any
    baseline: StrategyProfile = field(default_factory=StrategyProfile)
    candidate: StrategyProfile = field(default_factory=StrategyProfile)
    max_trials_per_arm: int = 6
    min_state_confidence: float = 0.5
    primary_metric: str = "completion_rate"
    min_trials: int = MIN_TRIALS
    requires_approval: bool = True
    conditions: dict[str, Any] = field(default_factory=dict)
    schema_version: str = EXPERIENCE_SCHEMA_VERSION

    def errors(self) -> list[str]:
        problems: list[str] = []
        if not str(self.experiment_id or "").strip():
            problems.append("An experiment needs an id.")
        if self.task is None:
            problems.append("An experiment needs an approved task definition to run inside.")
        else:
            errors = getattr(self.task, "errors", None)
            if callable(errors):
                problems.extend(f"task: {item}" for item in errors())
        if self.baseline.strategy_id == self.candidate.strategy_id:
            problems.append(
                "An experiment compares two strategies; the arms name the same strategy "
                f"{self.baseline.strategy_id!r}."
            )
        if not self.candidate.any_change:
            problems.append(
                "The candidate arm changes nothing, so no difference could be measured."
            )
        problems.extend(f"baseline: {item}" for item in self.baseline.errors())
        problems.extend(f"candidate: {item}" for item in self.candidate.errors())
        if not 1 <= int(self.max_trials_per_arm) <= 20:
            problems.append("max_trials_per_arm must be between 1 and 20.")
        if not 0.0 <= float(self.min_state_confidence) <= 1.0:
            problems.append("min_state_confidence must be between 0 and 1.")
        return problems

    @property
    def runnable(self) -> bool:
        return not self.errors()

    def arm_for(self, trial: int) -> StrategyProfile:
        """Baseline and candidate alternate, so the schedule is reproducible."""
        return self.baseline if int(trial) % 2 == 0 else self.candidate

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "experiment_id": self.experiment_id,
            "baseline": self.baseline.to_dict(),
            "candidate": self.candidate.to_dict(),
            "max_trials_per_arm": int(self.max_trials_per_arm),
            "min_state_confidence": float(self.min_state_confidence),
            "primary_metric": self.primary_metric,
            "min_trials": int(self.min_trials),
            "requires_approval": bool(self.requires_approval),
            "conditions": dict(self.conditions),
            "errors": self.errors(),
        }


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    """What one bounded experiment established, or why it stopped."""

    experiment_id: str
    status: str = "completed"
    stopped_reason: str = ""
    trials: dict[str, int] = field(default_factory=dict)
    baseline: StrategyMetrics | None = None
    candidate: StrategyMetrics | None = None
    comparison: StrategyComparison | None = None
    conditions: dict[str, Any] = field(default_factory=dict)
    records: tuple[str, ...] = ()
    live_evidence: bool = False
    schema_version: str = EXPERIENCE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "experiment_id": self.experiment_id,
            "status": self.status,
            "stopped_reason": self.stopped_reason,
            "trials": dict(self.trials),
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "candidate": self.candidate.to_dict() if self.candidate else None,
            "comparison": self.comparison.to_dict() if self.comparison else None,
            "verdict": self.comparison.verdict if self.comparison else "",
            "conditions": dict(self.conditions),
            "records": list(self.records),
            "live_evidence": bool(self.live_evidence),
            "note": (
                "An experiment compares two bounded ways of doing the same approved task. "
                "Its evidence is simulation unless every record says live, and it never "
                "sends input outside the existing control path."
            ),
        }


async def run_experiment(
    experiment: StrategyExperiment,
    run_arm: Callable[[StrategyProfile, Any], Awaitable[Mapping[str, Any]]],
    *,
    approved: bool = False,
    confidence_probe: Callable[[], float | None] | None = None,
) -> ExperimentResult:
    """Run both arms by alternation, stopping on the first safety signal.

    Stop conditions, all reported rather than silent: an unapproved experiment,
    an unrunnable one, a low state confidence, a safety stop, a resource refusal,
    and the per-arm trial cap. The comparison is only made from what ran.
    """
    problems = experiment.errors()
    if problems:
        return ExperimentResult(
            experiment_id=experiment.experiment_id,
            status="refused",
            stopped_reason="invalid_experiment: " + "; ".join(problems),
            conditions=dict(experiment.conditions),
        )
    if experiment.requires_approval and not approved:
        return ExperimentResult(
            experiment_id=experiment.experiment_id,
            status="refused",
            stopped_reason=(
                "approval_required: an experiment sends bounded input to a game, so it "
                "needs the operator's explicit approval."
            ),
            conditions=dict(experiment.conditions),
        )
    baseline_records: list[ExperienceRecord] = []
    candidate_records: list[ExperienceRecord] = []
    stopped = ""
    for trial in range(int(experiment.max_trials_per_arm) * 2):
        arm = experiment.arm_for(trial)
        if confidence_probe is not None:
            measured = confidence_probe()
            if measured is not None and float(measured) < float(experiment.min_state_confidence):
                stopped = (
                    f"state confidence {float(measured):.2f} fell below the configured "
                    f"{float(experiment.min_state_confidence):.2f}; exploration stopped rather "
                    "than acting on an unconfident reading."
                )
                break
        report = _mapping(await run_arm(arm, experiment.task))
        record = ExperienceRecord.from_task_report(
            report,
            strategy_id=arm.strategy_id,
            strategy_version=int(arm.version),
            profile=str(experiment.conditions.get("profile") or ""),
        )
        if record is None:
            stopped = (
                f"trial {trial + 1} produced no run to learn from; the experiment stopped "
                "instead of counting a refusal as a trial."
            )
            break
        (baseline_records if arm.strategy_id == experiment.baseline.strategy_id else candidate_records).append(
            record
        )
        if str(record.verdict) in {
            TaskVerdict.SAFETY_STOP.value,
            TaskVerdict.RESOURCE_REFUSED.value,
        }:
            stopped = (
                f"the {arm.strategy_id} arm ended {record.verdict}; exploration stopped at "
                "the first safety signal rather than running on."
            )
            break
    comparison = compare_strategies(
        baseline_records,
        candidate_records,
        primary_metric=experiment.primary_metric,
        min_trials=int(experiment.min_trials),
    )
    return ExperimentResult(
        experiment_id=experiment.experiment_id,
        status="completed" if not stopped else "stopped",
        stopped_reason=stopped,
        trials={"baseline": len(baseline_records), "candidate": len(candidate_records)},
        baseline=comparison.baseline,
        candidate=comparison.candidate,
        comparison=comparison,
        conditions=dict(experiment.conditions),
        records=tuple(
            [item.experience_id for item in baseline_records]
            + [item.experience_id for item in candidate_records]
        ),
        live_evidence=bool(comparison.live_evidence),
    )


@dataclass
class LearningService:
    """The product's learning surface: record, measure, compare, adapt, roll back.

    Everything it knows comes from NF-25's judged reports. It owns no window, no
    input path and no verifier; its whole output is a recommendation
    (:class:`~novacontrol.novaforge.planner.StrategyHint`) the planner may use and
    the operator may roll back. The store it writes to is the application's own
    snapshot store, so nothing here needs a database or a model.
    """

    store: Any | None = None
    clock: Callable[[], datetime] = _now
    require_approval: bool = True
    min_trials: int = MIN_TRIALS
    max_records: int = DEFAULT_MAX_RECORDS
    max_changes: int = DEFAULT_MAX_CHANGES
    _snapshot: ExperienceStore = field(init=False, repr=False)
    _records: list[ExperienceRecord] = field(default_factory=list, init=False, repr=False)
    _engine: AdaptationEngine = field(init=False, repr=False)
    _loaded: bool = field(default=False, init=False, repr=False)
    _counts: dict[str, int] = field(
        default_factory=lambda: {
            "received": 0,
            "recorded": 0,
            "duplicates": 0,
            "refused": 0,
            "uncertain": 0,
            "analysed": 0,
        },
        init=False,
        repr=False,
    )
    _last_error: dict[str, Any] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._snapshot = ExperienceStore(
            self.store, max_records=self.max_records, max_changes=self.max_changes
        )
        self._engine = AdaptationEngine(
            require_approval=bool(self.require_approval), clock=self.clock
        )

    # ── loading ───────────────────────────────────────────────────────────
    def load(self, *, force: bool = False) -> LearningSnapshot:
        """Read the ledger once. A read fault leaves an empty, usable service."""
        if self._loaded and not force:
            return LearningSnapshot(
                records=tuple(self._records),
                changes=tuple(self._engine.changes),
                profiles=tuple(self._engine.profiles),
            )
        snapshot = self._snapshot.read()
        if snapshot.dropped:
            self._counts["refused"] += snapshot.dropped
        if snapshot.duplicates:
            self._counts["duplicates"] += snapshot.duplicates
        for record in snapshot.records:
            if not any(item.experience_id == record.experience_id for item in self._records):
                self._records.append(record)
        self._engine.restore(snapshot.profiles, snapshot.changes)
        self._loaded = True
        return snapshot

    def _persist(self) -> bool:
        return self._snapshot.write(
            LearningSnapshot(
                records=tuple(self._records),
                changes=tuple(self._engine.changes),
                profiles=tuple(self._engine.profiles),
            )
        )

    # ── recording ─────────────────────────────────────────────────────────
    def record_task_report(
        self,
        report: Mapping[str, Any],
        *,
        run: Mapping[str, Any] | None = None,
        strategy_id: str = "baseline",
        strategy_version: int = 0,
        profile: str = "",
        confidence: float | None = None,
        interpretation: Mapping[str, Any] | None = None,
        failure_codes: Iterable[str] = (),
    ) -> ExperienceRecord | None:
        """Record ONE judged task. Returns the record, or ``None`` with a reason.

        ``None`` covers two different cases on purpose: a report that never ran
        (no experience exists) and a record that failed validation (an experience
        exists but is not trustworthy). ``status()['last_error']`` says which.
        """
        self.load()
        record = ExperienceRecord.from_task_report(
            report,
            run=run,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            profile=profile,
            confidence=confidence,
            failure_codes=failure_codes,
            interpretation=interpretation,
            recorded_at=self.clock(),
        )
        self._counts["received"] += 1
        if record is None:
            self._counts["refused"] += 1
            self._last_error = {
                "code": "no_run_or_invalid",
                "reason": (
                    "The report did not describe a run, or the record it would produce "
                    "failed validation; nothing was recorded."
                ),
            }
            return None
        existing = next(
            (item for item in self._records if item.experience_id == record.experience_id),
            None,
        )
        if existing is not None:
            if _same_experience(existing, record):
                self._counts["duplicates"] += 1
                return existing
            # The same run judged again: the newest verdict wins, and the old row
            # does not linger beside it as a contradictory one.
            self._records = [
                item for item in self._records if item.experience_id != record.experience_id
            ]
        self._records.append(record)
        if record.uncertain:
            self._counts["uncertain"] += 1
        self._counts["recorded"] += 1
        if len(self._records) > self.max_records:
            del self._records[: len(self._records) - self.max_records]
        self._persist()
        return record

    def record(self, record: ExperienceRecord) -> bool:
        """Record an already-built experience (a fixture or a restored row)."""
        self.load()
        problems = record.errors()
        if problems:
            self._counts["refused"] += 1
            self._last_error = {"code": "invalid_record", "reason": "; ".join(problems)}
            return False
        self._records = [
            item for item in self._records if item.experience_id != record.experience_id
        ]
        self._records.append(record)
        if record.uncertain:
            self._counts["uncertain"] += 1
        self._counts["recorded"] += 1
        if len(self._records) > self.max_records:
            del self._records[: len(self._records) - self.max_records]
        self._persist()
        return True

    # ── read surface ──────────────────────────────────────────────────────
    def records(
        self,
        *,
        game: str = "",
        strategy_id: str = "",
        label: str = "",
        limit: int = 200,
    ) -> list[ExperienceRecord]:
        self.load()
        rows = [
            item
            for item in self._records
            if (not game or item.game.casefold() == str(game).casefold())
            and (not strategy_id or item.strategy_id == strategy_id)
            and (not label or item.label == label)
        ]
        return rows[-max(1, int(limit)) :]

    def metrics(self, *, strategy_id: str = "baseline", game: str = "") -> StrategyMetrics:
        return metrics_for(self.records(game=game), strategy_id=strategy_id, game=game)

    def compare_arms(
        self,
        *,
        baseline_id: str = "baseline",
        candidate_id: str,
        game: str = "",
        primary_metric: str = "completion_rate",
        allowance: float = 0.0,
        version: int | None = None,
    ) -> StrategyComparison:
        """Compare two stored arms, scoped by game and (optionally) version.

        Scope matters here: an unscoped arm that spans two games, two strategy
        versions or both evidence classes is refused rather than averaged, so
        ``game`` and ``version`` are how a caller says which trials it means.
        """
        self._counts["analysed"] += 1
        return compare_strategies(
            self.records(strategy_id=baseline_id, game=game),
            self.records(strategy_id=candidate_id, game=game),
            primary_metric=primary_metric,
            min_trials=self.min_trials,
            allowance=allowance,
            version=version,
        )

    def failures(self, *, limit: int = 20, game: str = "") -> list[FailurePattern]:
        self.load()
        return analyse_failures(self.records(game=game, limit=MAX_RECORDS_ALLOWED), limit=limit)

    def provenance(self, experience_id: str) -> dict[str, Any]:
        """Which evidence a record carries, and which changes cite it."""
        self.load()
        record = next(
            (item for item in self._records if item.experience_id == str(experience_id)), None
        )
        if record is None:
            return {"found": False, "experience_id": str(experience_id)}
        return {
            "found": True,
            "experience_id": record.experience_id,
            "evidence_refs": list(record.evidence_refs),
            "evidence_source": record.evidence_source,
            "simulated": record.simulated,
            "strategy": f"{record.strategy_id}@{record.strategy_version}",
            "cited_by": [
                item.change_id
                for item in self._engine.changes
                if record.experience_id in item.evidence
            ],
        }

    # ── adaptation ────────────────────────────────────────────────────────
    def active_profile(self, strategy_id: str = "baseline") -> StrategyProfile:
        self.load()
        return self._engine.active(strategy_id)

    def use_profile(self, profile: StrategyProfile, *, slot: str = "baseline") -> StrategyProfile:
        """Install a profile as the planner's active strategy for an experiment arm.

        Returns the profile it replaced, which the caller restores. No change is
        recorded and no improvement is claimed: the arm IS the independent
        variable, and only a measured comparison can later justify a change.
        """
        self.load()
        return self._engine.install(slot, profile)

    def restore_profile(self, profile: StrategyProfile, *, slot: str = "baseline") -> None:
        """Put back what :meth:`use_profile` returned."""
        self.load()
        self._engine.install(slot, profile)

    def hint(self, strategy_id: str = "baseline") -> StrategyHint | None:
        """The active strategy as the planner reads it, or ``None``.

        ``None`` is the right answer for the reference profile: a hint that asks
        for nothing would still put a strategy row on every plan, and an empty
        recommendation is not a recommendation.
        """
        profile = self.active_profile(strategy_id)
        if not profile.active or not profile.any_change:
            return None
        hint = profile.hint()
        return hint if hint.usable else None

    def propose_change(
        self,
        profile: StrategyProfile,
        *,
        comparison: StrategyComparison | Mapping[str, Any] | None = None,
        expected_benefit: str = "",
        risks: Iterable[str] = (),
        conditions: Mapping[str, Any] | None = None,
        method: str = "",
        evidence: Iterable[str] = (),
    ) -> StrategyChange:
        self.load()
        change = self._engine.propose(
            profile,
            comparison=comparison,
            evidence=evidence,
            expected_benefit=expected_benefit,
            risks=risks,
            conditions=conditions,
            method=method,
        )
        self._persist()
        return change

    def apply_change(self, change_id: str, *, approved: bool = False) -> StrategyChange:
        self.load()
        change = self._engine.apply(change_id, approved=approved)
        self._persist()
        return change

    def rollback(self, strategy_id: str = "baseline", *, reason: str = "") -> StrategyProfile:
        self.load()
        profile = self._engine.rollback(strategy_id, reason=reason)
        self._persist()
        return profile

    def history(self, *, strategy_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
        self.load()
        return self._engine.history(strategy_id=strategy_id, limit=limit)

    # ── status ────────────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        self.load()
        return {
            "schema": EXPERIENCE_SCHEMA_VERSION,
            "records": len(self._records),
            "counts": dict(self._counts),
            "min_trials": int(self.min_trials),
            "require_approval": bool(self.require_approval),
            "profiles": [item.to_dict() for item in self._engine.profiles],
            "changes": len(self._engine.changes),
            "store": self._snapshot.status(),
            "last_error": dict(self._last_error) if self._last_error else None,
            "note": (
                "Learning reads judged task reports. It never captures, interprets, sends "
                "input or verifies an outcome, and its only output is a bounded hint the "
                "planner may ignore."
            ),
        }


__all__ = [
    "DEFAULT_MAX_CHANGES",
    "DEFAULT_MAX_RECORDS",
    "DEFAULT_PREFERENCE_WEIGHT",
    "EXPERIENCE_SCHEMA_VERSION",
    "HIGHER_IS_WORSE",
    "LEARNING_NAMESPACE",
    "MIN_TRIALS",
    "SAFETY_METRICS",
    "STORE_VERSION",
    "AdaptationEngine",
    "ExperienceLabel",
    "ExperienceRecord",
    "ExperienceStore",
    "ExperimentResult",
    "FailureBlame",
    "FailurePattern",
    "LearningService",
    "LearningSnapshot",
    "StrategyChange",
    "StrategyComparison",
    "StrategyExperiment",
    "StrategyMetrics",
    "StrategyProfile",
    "analyse_failures",
    "compare_strategies",
    "label_for_verdict",
    "metrics_for",
    "run_experiment",
    "wilson_interval",
]
