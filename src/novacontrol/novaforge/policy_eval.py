"""NF-10 — policy trial records and honest aggregates.

This is NOT a second runner and not a second evaluation system. Every number
here is read from a :class:`~novacontrol.novaforge.closed_loop.LoopResult` that
the existing guarded loop produced; a trial adds no execution path, no action
and no verdict of its own.

Three rules make the comparison believable:

* **A run ending is not a success.** The outcome vocabulary distinguishes a
  verifier-decided success from a verifier-decided failure, a refusal, an
  operator abort and an inconclusive stop. ``completed`` is only trusted when
  the verification block says ``verified_success``.
* **A figure that was not measured is ``None``, never zero.** A mean over no
  samples is ``None`` and its name is reported in ``unavailable_metrics``.
* **Every trial is kept**, including the failures, so a success rate cannot be
  computed from a filtered list by accident.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .closed_loop import LoopResult

#: The outcomes a trial can have. ``inconclusive`` is a stop the verifier did
#: not decide; it is not a failure and it is not a pass.
TRIAL_OUTCOMES: tuple[str, ...] = (
    "verified_success",
    "verified_failure",
    "inconclusive",
    "refused",
    "aborted",
    "verification_error",
    "unavailable",
    "error",
)


def _mean(values: Sequence[float]) -> float | None:
    """A mean over the samples that exist, or ``None`` when there are none."""
    usable = [float(value) for value in values if value is not None]
    return round(statistics.fmean(usable), 3) if usable else None


def _median(values: Sequence[float]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    return round(statistics.median(usable), 3) if usable else None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 3)


def stage_of_step(step: Mapping[str, Any]) -> str:
    """The stage a recorded step observed, or ``""`` when its title states none.

    ``summarise_trials(observation)`` puts the title at the top level; a step
    built by hand may carry the full observation under ``target``. Reading only
    one shape under-counted every real transition.
    """
    observation = step.get("observation") or {}
    if not isinstance(observation, Mapping):
        return ""
    target = observation.get("target")
    title = str(
        observation.get("title")
        or (target.get("title") if isinstance(target, Mapping) else None)
        or ""
    )
    marker = "stage "
    found = title.lower().find(marker)
    if found < 0:
        return ""
    return title.lower()[found + len(marker) :].split(" ", 1)[0].strip("—\u2014-:|,.;")


def _action_signature(step: Mapping[str, Any]) -> str | None:
    """A comparable identity for the action a step proposed, or None if it proposed none."""
    proposal = step.get("proposal")
    if not isinstance(proposal, Mapping):
        return None
    action = proposal.get("action")
    if not isinstance(action, Mapping) or not action:
        return None
    return json.dumps(dict(action), sort_keys=True, default=str)


def responded_to_transition(steps: Sequence[Mapping[str, Any]]) -> bool:
    """Did this run DECIDE DIFFERENTLY after the state changed?

    A state change alone is not a response: the fixed baseline moves the
    harness IDLE -> FAULT and then proposes nothing, which is exactly a policy
    that ignored what it saw. The metric therefore requires two decisions, an
    observed stage change between them, and a second action that is not the
    first one repeated.
    """
    previous_stage: str | None = None
    previous_action: str | None = None
    for step in steps:
        stage = stage_of_step(step)
        action = _action_signature(step)
        if action is None:
            continue
        if (
            previous_action is not None
            and previous_stage
            and stage
            and stage != previous_stage
            and action != previous_action
        ):
            return True
        previous_stage = stage or previous_stage
        previous_action = action
    return False


def outcome_of(result: LoopResult) -> str:
    """The trial outcome, decided by the VERIFIER where one spoke.

    ``completed`` without a ``verified_success`` is recorded as inconclusive:
    the loop does not complete on anything else today, and if it ever did, a
    trial must not inherit that as a win.
    """
    verification = result.verification or {}
    verdict = str(verification.get("outcome") or "")
    if verdict == "verified_success" and result.status == "completed":
        return "verified_success"
    if verdict == "verified_failure":
        return "verified_failure"
    if verdict == "verification_error":
        return "verification_error"
    return {
        "completed": "inconclusive",
        "failed": "verified_failure",
        "refused": "refused",
        "canceled": "aborted",
        "unavailable": "unavailable",
        "error": "error",
    }.get(result.status, "inconclusive")


@dataclass(frozen=True, slots=True)
class PolicyTrial:
    """One loop run, as a comparable measurement."""

    policy: str
    policy_version: int
    trial: int
    outcome: str
    reason: str
    actions_executed: int
    actions_refused: int
    decisions: int
    elapsed_seconds: float | None
    observation_age_seconds: float | None
    decision_latency_ms: float | None
    evidence_coverage: float | None
    model_backed: bool
    #: How many times the observed title/stage changed during the run. This is
    #: what separates "responded to a transition" from "replayed a script".
    observed_transitions: int
    #: Raw per-step measurements, kept so an aggregate can be recomputed.
    steps: tuple[dict[str, Any], ...] = ()

    @property
    def responded_to_transition(self) -> bool:
        """The run decided differently after observing a different state."""
        return responded_to_transition(self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "policy_version": self.policy_version,
            "trial": self.trial,
            "outcome": self.outcome,
            "reason": self.reason,
            "actions_executed": self.actions_executed,
            "actions_refused": self.actions_refused,
            "decisions": self.decisions,
            "elapsed_seconds": self.elapsed_seconds,
            "observation_age_seconds": self.observation_age_seconds,
            "decision_latency_ms": self.decision_latency_ms,
            "evidence_coverage": self.evidence_coverage,
            "model_backed": self.model_backed,
            "observed_transitions": self.observed_transitions,
            "responded_to_transition": self.responded_to_transition,
            "steps": [dict(step) for step in self.steps],
        }

    @classmethod
    def from_loop(cls, result: LoopResult, *, trial: int) -> "PolicyTrial":
        steps = [step.to_dict() for step in result.steps]
        ages = [
            step.observation_age_seconds
            for step in result.steps
            if step.observation_age_seconds is not None
        ]
        latencies = [
            step.decision_latency_ms
            for step in result.steps
            if step.decision_latency_ms is not None
        ]
        evidence = [
            step.evidence_count
            for step in result.steps
            if step.evidence_count is not None and step.proposal is not None
        ]
        stages: list[str] = []
        for step in steps:
            stage = stage_of_step(step)
            if stage and (not stages or stages[-1] != stage):
                stages.append(stage)
        return cls(
            policy=result.policy or "unknown",
            policy_version=result.policy_version,
            trial=trial,
            outcome=outcome_of(result),
            reason=result.reason,
            actions_executed=result.actions_executed,
            actions_refused=result.actions_refused,
            decisions=result.decisions,
            elapsed_seconds=_optional_float(result.elapsed_seconds),
            observation_age_seconds=_mean(ages),
            decision_latency_ms=_mean(latencies),
            evidence_coverage=(
                round(sum(evidence) / len(evidence), 3) if evidence else None
            ),
            model_backed=bool(result.model_backed),
            observed_transitions=max(0, len(stages) - 1),
            steps=tuple(steps),
        )


@dataclass(slots=True)
class TrialSummary:
    """Aggregates over every trial of one policy, with its raw rows."""

    policy: str
    policy_version: int
    trials: list[PolicyTrial] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.trials)

    def outcomes(self) -> dict[str, int]:
        counts = {name: 0 for name in TRIAL_OUTCOMES}
        for trial in self.trials:
            counts[trial.outcome] = counts.get(trial.outcome, 0) + 1
        return counts

    def to_dict(self, *, include_raw: bool = True) -> dict[str, Any]:
        """The aggregates, and by default every raw row they were computed from.

        ``include_raw=False`` is for a panel that already holds the rows: the
        steps inside a raw row are the loop's record, and repeating them in
        every summary only makes the response bigger, not more honest.
        """
        elapsed = [t.elapsed_seconds for t in self.trials if t.elapsed_seconds is not None]
        ages = [
            t.observation_age_seconds
            for t in self.trials
            if t.observation_age_seconds is not None
        ]
        latencies = [
            t.decision_latency_ms for t in self.trials if t.decision_latency_ms is not None
        ]
        evidence = [t.evidence_coverage for t in self.trials if t.evidence_coverage is not None]
        outcomes = self.outcomes()
        successes = outcomes["verified_success"]
        unavailable = [
            name
            for name, samples in (
                ("elapsed_seconds", elapsed),
                ("observation_age_seconds", ages),
                ("decision_latency_ms", latencies),
                ("evidence_coverage", evidence),
            )
            if not samples
        ]
        return {
            "policy": self.policy,
            "policy_version": self.policy_version,
            "trials": self.count,
            "outcomes": outcomes,
            "success_rate": (round(successes / self.count, 3) if self.count else None),
            "actions_executed_mean": _mean([t.actions_executed for t in self.trials]),
            "actions_refused_total": sum(t.actions_refused for t in self.trials),
            "decisions_mean": _mean([t.decisions for t in self.trials]),
            "elapsed_seconds_mean": _mean(elapsed),
            "elapsed_seconds_median": _median(elapsed),
            "observation_age_seconds_mean": _mean(ages),
            "decision_latency_ms_mean": _mean(latencies),
            "evidence_coverage_mean": _mean(evidence),
            "model_backed_trials": sum(1 for t in self.trials if t.model_backed),
            "responded_to_transition_trials": sum(
                1 for t in self.trials if t.responded_to_transition
            ),
            "unavailable_metrics": unavailable,
            "raw": [t.to_dict() for t in self.trials] if include_raw else [],
        }


def summarise_trials(trials: Iterable[PolicyTrial]) -> TrialSummary:
    """Group trials into one summary per policy, preserving every row."""
    grouped: dict[tuple[str, int], TrialSummary] = {}
    for trial in trials:
        key = (trial.policy, trial.policy_version)
        summary = grouped.setdefault(
            key, TrialSummary(policy=trial.policy, policy_version=trial.policy_version)
        )
        summary.trials.append(trial)
    if len(grouped) != 1:
        raise ValueError(
            "summarise_trials() takes the trials of ONE policy; use compare_policies() for two."
        )
    return next(iter(grouped.values()))


def summaries_by_policy(
    trials: Iterable[PolicyTrial], *, include_raw: bool = False
) -> list[dict[str, Any]]:
    """One aggregate per (policy, version), in first-run order.

    A process accumulates runs of MORE than one policy, so ``summarise_trials``
    (which deliberately takes one policy) cannot answer "how did each policy
    do?" on its own. This is that grouping — the aggregation itself is still
    the single ``TrialSummary`` implementation.
    """
    grouped: dict[tuple[str, int], list[PolicyTrial]] = {}
    for trial in trials:
        grouped.setdefault((trial.policy, trial.policy_version), []).append(trial)
    return [
        summarise_trials(rows).to_dict(include_raw=include_raw)
        for _, rows in grouped.items()
    ]


def compare_policies(
    baseline: Iterable[PolicyTrial], candidate: Iterable[PolicyTrial]
) -> dict[str, Any]:
    """Both summaries plus only the deltas that were actually measured.

    Deliberately no p-value: these are small samples of a deterministic task,
    and a significance claim would be a claim the data cannot support.
    """
    base = summarise_trials(baseline)
    cand = summarise_trials(candidate)
    base_data = base.to_dict()
    cand_data = cand.to_dict()
    deltas: dict[str, float | None] = {}
    for key in (
        "success_rate",
        "actions_executed_mean",
        "elapsed_seconds_mean",
        "decision_latency_ms_mean",
        "evidence_coverage_mean",
    ):
        before, after = base_data.get(key), cand_data.get(key)
        deltas[key] = (
            round(float(after) - float(before), 3)
            if isinstance(before, (int, float)) and isinstance(after, (int, float))
            else None
        )
    return {
        "baseline": base_data,
        "candidate": cand_data,
        "delta": deltas,
        "note": (
            "Deltas are raw differences on this task and sample. No significance is "
            "claimed; failed and inconclusive trials are included in every rate."
        ),
    }


def classify_runs(results: Sequence[LoopResult]) -> dict[str, int]:
    """Count outcomes over LoopResults without discarding the failures."""
    counts = {name: 0 for name in TRIAL_OUTCOMES}
    for result in results:
        counts[outcome_of(result)] = counts.get(outcome_of(result), 0) + 1
    return counts


def trial_rows(trials: Iterable[PolicyTrial]) -> list[Mapping[str, Any]]:
    """A compact table for a report: one row per trial, in run order."""
    return [
        {
            "policy": trial.policy,
            "trial": trial.trial,
            "outcome": trial.outcome,
            "actions": trial.actions_executed,
            "refused": trial.actions_refused,
            "transitions": trial.observed_transitions,
            # A transition alone is not adaptation (the baseline causes one by
            # being wrong); this flag says the policy chose DIFFERENTLY after it.
            "responded_to_transition": trial.responded_to_transition,
            "elapsed_s": trial.elapsed_seconds,
            "reason": trial.reason,
        }
        for trial in trials
    ]
