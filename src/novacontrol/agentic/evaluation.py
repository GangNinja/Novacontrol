"""Policy evaluation: what a policy actually did, task by task.

An agentic policy is not better because its average reward went up. It is better
if it completes more tasks, its steps VERIFY, it does not take unsafe actions,
it does not waste steps, it selects the right tools, it recovers when something
fails and it does all of that without more latency or resources. So the evaluator
measures those separately and never merges them:

  * **short tasks vs long tasks** — a policy that only handles two-step tasks is
    reported as such;
  * **no-failure runs vs runs with a failure or a recovery** — the same policy
    can be strong at one and useless at the other;
  * **safety is its own block** — the safety numbers are never inside an average;
  * **unmeasurable is ``None``** — a metric with no sample (no recovery ever
    happened) is reported as ``None`` with a note, never as a zero that would
    read as a failure.

It also ships the two comparison modes the specification asks for, and both are
built on the SAME episode collection so nothing is compared across two different
procedures:

  * :class:`ShadowPolicyEvaluator` — the production policy ACTS; the shadow
    policy only PROPOSES for each state it observes, its proposals are recorded
    and compared against what actually happened, and it never executes anything.
  * :class:`ABPolicyEvaluator` — two policies run the SAME task list with the
    same seeds, and the outcomes are compared. It never switches anything: a
    promotion is a separate decision made by the promotion gates.

The baseline table compares only policies that CAN actually run here. The others
are listed as unavailable with the reason, because a comparison that quietly
invents a number for a model nobody has is worse than no comparison.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from novacontrol.agentic.environments import AgenticEnvironment, build_environment
from novacontrol.agentic.models import (
    REWARD_DIMENSIONS,
    AgentAction,
    AgenticEvaluationResult,
    AgentState,
    Episode,
    StepPenalty,
    as_mapping,
    as_real,
    as_text,
)
from novacontrol.agentic.policies import AgentPolicy
from novacontrol.agentic.rewards import episode_success
from novacontrol.agentic.rollout import AgenticRolloutManager, RolloutOutcome

#: The baselines the specification names, in the order it names them.
BASELINE_KINDS: tuple[str, ...] = (
    "deterministic_orchestration",
    "sft",
    "dpo_orpo",
    "rlhf_rlaif",
    "rlvr",
    "agentic_rl",
)

#: The baselines this installation can actually run. The rest are reported as
#: unavailable WITH a reason: NovaControl has no SFT/DPO/RLHF/RLVR *policy* wired
#: in as an agentic actor, and pretending it does would fabricate the comparison.
RUNNABLE_BASELINES: tuple[str, ...] = ("deterministic_orchestration", "agentic_rl")

#: Why a baseline cannot be measured here.
BASELINE_UNAVAILABLE_REASON: Mapping[str, str] = {
    "sft": (
        "the SFT model is a text model, not an agentic policy: no adapter exposes it "
        "as an action selector, so it cannot be measured on task outcomes here"
    ),
    "dpo_orpo": (
        "the DPO/ORPO candidate has no agentic action-selection interface wired in"
    ),
    "rlhf_rlaif": (
        "the RLHF/RLAIF candidate has no agentic action-selection interface wired in"
    ),
    "rlvr": (
        "the RLVR candidate has no agentic action-selection interface wired in"
    ),
}


@dataclass(frozen=True, slots=True)
class EvaluationTask:
    """One task in an evaluation set: an environment and the task it is given."""

    environment: str = ""
    task: Mapping[str, Any] = field(default_factory=dict)
    seed: int = 0
    label: str = ""
    #: Optional grouping for the report (``short``, ``long``, ``failure``…).
    kind: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "task": dict(self.task),
            "seed": self.seed,
            "label": self.label,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    """How the report is sliced."""

    minimum_sample_size: int = 8
    #: Episodes this long or shorter are reported as SHORT tasks.
    short_task_steps: int = 4
    latency_budget_ms: float = 2000.0
    resource_budget: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "minimum_sample_size": self.minimum_sample_size,
            "short_task_steps": self.short_task_steps,
            "latency_budget_ms": self.latency_budget_ms,
            "resource_budget": self.resource_budget,
        }


def default_tasks(*, short: bool = True, include_failure: bool = True) -> tuple[EvaluationTask, ...]:
    """A deterministic task set covering short, long, failure and context tasks.

    The full set — and the set with the failure tasks left out — carries at least
    :class:`EvaluationConfig`'s default ``minimum_sample_size`` tasks, on purpose:
    a task distribution that cannot produce a report meeting the default minimum
    would make the default configuration unusable with the default tasks.

    Every task here is solved by the reference rule policy, so the baseline is a
    real 1.0 rather than an excuse, and each slice has more than one row: two
    tool selections, two context clues, two planning budgets and two recovery
    depths. A caller that wants a task set which FAILS builds one.
    """
    tasks: list[EvaluationTask] = []
    if short:
        tasks.extend(
            [
                EvaluationTask("simple", {}, 0, "one step", "short"),
                EvaluationTask("multi_step", {}, 0, "three dependent steps", "short"),
                EvaluationTask(
                    "tool_selection",
                    {"hint": "search the web for the answer", "correct_tool": "search_web"},
                    0,
                    "web tool",
                    "short",
                ),
                EvaluationTask(
                    "tool_selection",
                    {"hint": "search the files for the answer", "correct_tool": "search_files"},
                    0,
                    "local tool",
                    "short",
                ),
                EvaluationTask(
                    "contextual",
                    {"expected_tool": "beta", "context": {"clue": "the beta path matches the goal"}},
                    0,
                    "context clue",
                    "short",
                ),
                EvaluationTask(
                    "contextual",
                    {"expected_tool": "gamma", "context": {"clue": "the gamma path matches the goal"}},
                    0,
                    "second context clue",
                    "short",
                ),
            ]
        )
    tasks.append(EvaluationTask("planning", {"budget": 5}, 0, "five-step plan", "long"))
    tasks.append(EvaluationTask("planning", {"budget": 3}, 0, "three-step plan", "long"))
    if include_failure:
        tasks.append(
            EvaluationTask("recovery", {"failures": 1}, 0, "transient failure", "failure")
        )
        tasks.append(
            EvaluationTask("recovery", {"failures": 2}, 0, "repeated failure", "failure")
        )
    return tuple(tasks)


class AgenticPolicyEvaluator:
    """Evaluates a policy on a task set through episodes, not opinions."""

    def __init__(
        self,
        *,
        rollout: AgenticRolloutManager | None = None,
        config: EvaluationConfig | None = None,
        environment_factory: Callable[[str], AgenticEnvironment] = build_environment,
    ) -> None:
        self.rollout = rollout
        self.config = config if config is not None else EvaluationConfig()
        self.environment_factory = environment_factory

    def collect(
        self,
        policy: AgentPolicy,
        tasks: Sequence[EvaluationTask],
        *,
        manager: AgenticRolloutManager | None = None,
    ) -> tuple[Episode, ...]:
        """Run every task once and return the episodes (the raw evidence)."""
        template = manager or self.rollout
        runner = (
            template.with_policy(policy) if template is not None else AgenticRolloutManager(policy)
        )
        episodes: list[Episode] = []
        for task in tasks:
            environment = self.environment_factory(task.environment)
            outcome: RolloutOutcome = runner.run_episode(
                environment, task=task.task, seed=task.seed
            )
            episodes.append(
                replace(
                    outcome.episode,
                    metadata={
                        **outcome.episode.metadata,
                        "evaluation_kind": task.kind,
                        "evaluation_label": task.label,
                    },
                )
            )
        return tuple(episodes)

    def evaluate(
        self,
        policy: AgentPolicy,
        tasks: Sequence[EvaluationTask] | None = None,
        *,
        episodes: Sequence[Episode] | None = None,
        manager: AgenticRolloutManager | None = None,
    ) -> AgenticEvaluationResult:
        rows = (
            tuple(episodes)
            if episodes is not None
            else self.collect(policy, tasks or default_tasks(), manager=manager)
        )
        return self.summarise(policy, rows)

    def summarise(
        self, policy: AgentPolicy, episodes: Sequence[Episode]
    ) -> AgenticEvaluationResult:
        """Everything measurable about these episodes, dimension by dimension."""
        config = self.config
        notes: list[str] = []
        if not episodes:
            return AgenticEvaluationResult(
                policy_id=policy.policy_id,
                policy_version=policy.version,
                notes=("no episode was collected, so nothing is measured",),
                minimum_sample_size=config.minimum_sample_size,
            )
        decided = [row for row in episodes if episode_success(row) is not None]
        successes = [row for row in episodes if episode_success(row) is True]
        failures = [row for row in episodes if episode_success(row) is False]
        undecided = [row for row in episodes if episode_success(row) is None]
        # Success rate is measured over DECIDED episodes only: a bounded run is
        # neither a success nor a failure, and counting it as a failure would
        # punish a policy for respecting its own limits.
        success_rate = (len(successes) / len(decided)) if decided else None
        if not decided:
            notes.append(
                "no episode ended with a decided outcome, so the success rate is not measurable"
            )
        steps = [row.length for row in episodes]
        step_checks = [
            (step, row)
            for row in episodes
            for step in row.steps
        ]
        checked = [
            step for step, _row in step_checks if as_text(step.verification.get("status")) != "skipped"
        ]
        passed = [step for step in checked if step.verified]
        verification_rate = (len(passed) / len(checked)) if checked else None
        if not checked:
            notes.append("no step carried a verification verdict, so the pass rate is not measurable")
        rewarded = [float(row.total_reward) for row in episodes]
        terminal = [
            float(step.step_reward)
            for row in episodes
            for step in row.steps
            if step.terminal
        ]
        intermediate = [
            float(step.step_reward)
            for row in episodes
            for step in row.steps
            if not step.terminal
        ]
        recovery_episodes = [row for row in episodes if row.recovery_events]
        recovered = [
            row
            for row in recovery_episodes
            if episode_success(row) is True
        ]
        recovery_rate = (len(recovered) / len(recovery_episodes)) if recovery_episodes else None
        if recovery_rate is None:
            notes.append("no episode contained a recovery, so recovery quality has no sample")
        unnecessary = _penalty_count(episodes, StepPenalty.UNNECESSARY_ACTION.value)
        unsafe_episodes = [row for row in episodes if row.safety_penalty < 0]
        latency = [
            as_real(as_mapping(row.resource_usage).get("elapsed_seconds"))
            for row in episodes
        ]
        latency_values = [value for value in latency if value is not None]
        steps_per_success = (
            (math.fsum(row.length for row in successes) / len(successes)) if successes else None
        )
        if steps_per_success is None:
            notes.append("no episode succeeded, so steps-per-success is not measurable")
        metrics: dict[str, float | None] = {
            "task_success_rate": _rounded(success_rate),
            "verification_pass_rate": _rounded(verification_rate),
            "mean_episode_reward": round(_mean(rewarded), 6),
            "mean_terminal_reward": round(_mean(terminal), 6),
            "mean_intermediate_reward": round(_mean(intermediate), 6),
            "mean_steps": round(_mean(steps), 6),
            "mean_steps_per_success": _rounded(steps_per_success),
            "recovery_success_rate": _rounded(recovery_rate),
            "failure_rate": _rounded((len(failures) / len(decided)) if decided else None),
            "undecided_rate": round(len(undecided) / len(episodes), 6),
            "safety_rate": round(1.0 - (len(unsafe_episodes) / len(episodes)), 6),
            "mean_unnecessary_actions": round(_mean(unnecessary), 6),
            "mean_latency_seconds": _rounded(_mean(latency_values) if latency_values else None),
            "mean_resource_used": _rounded(
                _mean([as_real(as_mapping(row.resource_usage).get("used"), 0.0) or 0.0 for row in episodes])
            ),
            "mean_refusals": round(
                _mean([as_real(as_mapping(row.resource_usage).get("refusals"), 0.0) or 0.0 for row in episodes]),
                6,
            ),
            "exploration_share": _rounded(
                (
                    sum(
                        as_real(as_mapping(row.resource_usage).get("explored_steps"), 0.0) or 0.0
                        for row in episodes
                    )
                    / sum(steps)
                )
                if sum(steps)
                else None
            ),
        }
        # The dimension means are the multi-objective reading: never collapsed
        # into the single number above.
        dimensions = {
            name: round(value, 6) for name, value in _mean_dimensions(episodes).items()
        }
        by_horizon = self._by_split(
            episodes,
            label_for=lambda row: (
                "short" if row.length <= config.short_task_steps else "long"
            ),
        )
        by_outcome = self._by_split(
            episodes,
            label_for=lambda row: (
                "failure_or_recovery" if row.failed_steps or row.recovery_events else "no_failure"
            ),
        )
        by_difficulty = self._by_difficulty(episodes)
        return AgenticEvaluationResult(
            policy_id=policy.policy_id,
            policy_version=policy.version,
            episodes=len(episodes),
            metrics=metrics,
            dimensions=dimensions,
            by_horizon=by_horizon,
            by_outcome=by_outcome,
            by_difficulty=by_difficulty,
            safety={
                "unsafe_episodes": len(unsafe_episodes),
                "safety_rate": metrics["safety_rate"],
                "safety_total": round(_mean([row.safety_penalty for row in episodes]), 6),
                "note": (
                    "safety is measured and reported on its own: it is never averaged "
                    "together with efficiency or reward"
                ),
            },
            efficiency={
                "mean_steps": metrics["mean_steps"],
                "mean_steps_per_success": metrics["mean_steps_per_success"],
                "mean_unnecessary_actions": metrics["mean_unnecessary_actions"],
                "mean_terminal_reward": metrics["mean_terminal_reward"],
                "note": (
                    "an increase in average reward alone is not evidence of "
                    "improvement: the task and verification numbers beside it are"
                ),
            },
            sample_size=len(episodes),
            minimum_sample_size=config.minimum_sample_size,
            notes=tuple(notes),
            episode_ids=tuple(row.episode_id for row in episodes),
        )

    # -- slices -----------------------------------------------------------------

    def _by_split(
        self,
        episodes: Sequence[Episode],
        *,
        label_for: Callable[[Episode], str],
    ) -> dict[str, dict[str, Any]]:
        buckets: dict[str, list[Episode]] = {}
        for row in episodes:
            buckets.setdefault(label_for(row), []).append(row)
        return {
            label: _bucket_summary(rows) for label, rows in sorted(buckets.items())
        }

    def _by_difficulty(self, episodes: Sequence[Episode]) -> dict[str, dict[str, Any]]:
        buckets: dict[str, list[Episode]] = {}
        for row in episodes:
            level = str(row.difficulty.level.value) if row.difficulty else "unknown"
            buckets.setdefault(level, []).append(row)
        return {
            level: _bucket_summary(rows) for level, rows in sorted(buckets.items())
        }


def _bucket_summary(episodes: Sequence[Episode]) -> dict[str, Any]:
    decided = [row for row in episodes if episode_success(row) is not None]
    successes = [row for row in episodes if episode_success(row) is True]
    return {
        "episodes": len(episodes),
        "successes": len(successes),
        "success_rate": _rounded((len(successes) / len(decided)) if decided else None),
        "mean_reward": round(_mean([row.total_reward for row in episodes]), 6),
        "mean_steps": round(_mean([row.length for row in episodes]), 6),
        "safety_total": round(_mean([row.safety_penalty for row in episodes]), 6),
    }


def _mean(values: Sequence[float]) -> float:
    return (math.fsum(float(value) for value in values) / len(values)) if values else 0.0


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _dimension(episode: Episode, name: str) -> float:
    return float(as_mapping(episode.dimension_totals).get(name, 0.0))


def _mean_dimensions(episodes: Sequence[Episode]) -> dict[str, float]:
    return {name: _mean([_dimension(row, name) for row in episodes]) for name in REWARD_DIMENSIONS}


def _penalty_count(episodes: Sequence[Episode], penalty: str) -> list[float]:
    counts: list[float] = []
    for row in episodes:
        count = 0
        for step in row.steps:
            if step.reward is not None and penalty in step.reward.penalties:
                count += 1
        counts.append(float(count))
    return counts


# ── comparison ───────────────────────────────────────────────────────────────


def compare_results(
    baseline: AgenticEvaluationResult, candidate: AgenticEvaluationResult
) -> dict[str, Any]:
    """A candidate against a baseline, metric by metric, with the deltas."""
    keys = sorted({*baseline.metrics, *candidate.metrics})
    rows: dict[str, Any] = {}
    for key in keys:
        base = baseline.metric(key)
        cand = candidate.metric(key)
        delta = (
            None
            if base is None or cand is None
            else round(float(cand) - float(base), 6)
        )
        rows[key] = {"baseline": base, "candidate": cand, "delta": delta}
    return {
        "baseline_policy": baseline.policy_id,
        "candidate_policy": candidate.policy_id,
        "metrics": rows,
        "baseline_sample": baseline.sample_size,
        "candidate_sample": candidate.sample_size,
        "comparable": (
            baseline.sample_size > 0
            and candidate.sample_size > 0
            and baseline.sample_size == candidate.sample_size
        ),
        "note": (
            "an improvement is only claimed where both sides measured the same "
            "metric on the same task set; a delta of None means one side did not "
            "measure it"
        ),
    }


def baseline_table(
    results: Mapping[str, AgenticEvaluationResult] | None = None,
) -> dict[str, Any]:
    """The six baselines, each marked available or unavailable WITH a reason."""
    provided = {
        as_text(name): value for name, value in as_mapping(results or {}).items() if value is not None
    }
    rows: dict[str, Any] = {}
    for kind in BASELINE_KINDS:
        result = provided.get(kind)
        rows[kind] = {
            "available": result is not None,
            "reason": "" if result is not None else BASELINE_UNAVAILABLE_REASON.get(
                kind, "no policy of this kind is wired in this installation"
            ),
            "result": result.to_dict() if result is not None else None,
        }
    return {
        "baselines": rows,
        "measured": [kind for kind, row in rows.items() if row["available"]],
        "note": (
            "only the baselines that can actually run here are measured; the others "
            "are reported as unavailable rather than estimated"
        ),
    }


# ── shadow ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    """What the shadow policy proposed, beside what the production policy did."""

    episodes: int = 0
    steps: int = 0
    agreements: int = 0
    disagreements: int = 0
    shadow_available: bool = True
    proposals: tuple[Mapping[str, Any], ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def comparable(self) -> int:
        """Steps where the shadow named an action, so a comparison was possible.

        A shadow that proposed nothing — a script with nothing left, a policy
        that answered with no action — is neither an agreement nor a
        disagreement, and it never quietly becomes one.
        """
        return self.agreements + self.disagreements

    @property
    def uncompared(self) -> int:
        return max(0, self.steps - self.comparable)

    @property
    def agreement_rate(self) -> float | None:
        return (self.agreements / self.comparable) if self.comparable else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "episodes": self.episodes,
            "steps": self.steps,
            "agreements": self.agreements,
            "disagreements": self.disagreements,
            "comparable": self.comparable,
            "uncompared": self.uncompared,
            "agreement_rate": _rounded(self.agreement_rate),
            "shadow_available": self.shadow_available,
            "proposals": [dict(item) for item in self.proposals],
            "executed_by": "production_policy",
            "shadow_executed_anything": False,
            "notes": list(self.notes),
        }


class ShadowPolicyEvaluator:
    """Runs a shadow policy beside the one that acts, and executes nothing itself."""

    def __init__(
        self,
        production: AgentPolicy,
        shadow: AgentPolicy,
        *,
        environment_factory: Callable[[str], AgenticEnvironment] = build_environment,
    ) -> None:
        self.production = production
        self.shadow = shadow
        self.environment_factory = environment_factory

    def run(
        self,
        tasks: Sequence[EvaluationTask] | None = None,
        *,
        manager: AgenticRolloutManager | None = None,
    ) -> ShadowComparison:
        """Collect episodes with the production policy, shadowing every state."""
        proposals: list[Mapping[str, Any]] = []
        available = True
        notes: list[str] = []
        seen: dict[int, Mapping[str, Any]] = {}

        def observer(index: int, state: AgentState, mask: Any, decision: Any) -> None:
            nonlocal available
            try:
                proposed = self.shadow.select_action(state, tuple(mask.allowed), mask=mask)
            except Exception as error:  # a shadow that cannot propose is not a failure
                available = False
                seen[index] = {
                    "index": index,
                    "shadow_available": False,
                    "reason": f"{type(error).__name__}: {error}",
                }
                return
            action = getattr(decision, "selected_action", None)
            chosen = action if isinstance(action, AgentAction) else None
            shadow_action = proposed.selected_action
            seen[index] = {
                "index": index,
                "state_fingerprint": state.fingerprint(),
                "state_step": state.step_index,
                "shadow_action": shadow_action.name if shadow_action else "",
                "shadow_action_id": shadow_action.action_id if shadow_action else "",
                "shadow_reason_code": proposed.reason_code,
                "production_action": chosen.name if chosen else "",
                "production_action_id": chosen.action_id if chosen else "",
                "agree": bool(
                    shadow_action is not None
                    and chosen is not None
                    and shadow_action.action_id == chosen.action_id
                ),
                "executed": False,
            }

        template = manager or AgenticRolloutManager(self.production)
        runner = template.with_policy(self.production)
        # The observer is how the shadow sees every state the production policy
        # is about to act in. It only reads; the runner executes what the
        # production policy chose, whatever the shadow proposed.
        runner.step_observer = observer
        episodes = 0
        steps = 0
        agreements = 0
        disagreements = 0
        for task in tasks or default_tasks():
            seen.clear()
            outcome = runner.run_episode(
                self.environment_factory(task.environment), task=task.task, seed=task.seed
            )
            episodes += 1
            for step in outcome.episode.steps:
                record = seen.get(step.index)
                if record is None:
                    continue
                steps += 1
                row = {
                    **record,
                    "verified": step.verification_result,
                    "reward": round(float(step.step_reward), 6),
                    "executed_action": step.action.name,
                }
                proposals.append(row)
                if row.get("agree"):
                    agreements += 1
                elif row.get("shadow_action"):
                    disagreements += 1
        if not available:
            notes.append(
                "the shadow policy could not propose for at least one state; those "
                "steps are reported as unavailable rather than as disagreement"
            )
        notes.append(
            "the shadow policy never executed an action: every proposal is recorded "
            "beside the action the production policy actually took"
        )
        return ShadowComparison(
            episodes=episodes,
            steps=steps,
            agreements=agreements,
            disagreements=disagreements,
            shadow_available=available,
            proposals=tuple(proposals),
            notes=tuple(notes),
        )


# ── A/B ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ABResult:
    """Two policies on the same task distribution, compared."""

    policy_a: str = ""
    policy_b: str = ""
    result_a: AgenticEvaluationResult | None = None
    result_b: AgenticEvaluationResult | None = None
    comparison: Mapping[str, Any] = field(default_factory=dict)
    same_task_distribution: bool = True
    tasks: tuple[Mapping[str, Any], ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_a": self.policy_a,
            "policy_b": self.policy_b,
            "same_task_distribution": self.same_task_distribution,
            "tasks": [dict(item) for item in self.tasks],
            "result_a": self.result_a.to_dict() if self.result_a else None,
            "result_b": self.result_b.to_dict() if self.result_b else None,
            "comparison": dict(self.comparison),
            "notes": list(self.notes),
        }


class ABPolicyEvaluator:
    """Runs two policies over one task list and compares the outcomes.

    The task list is part of the RESULT, so a reader can check that both sides
    ran the same tasks with the same seeds — the property that makes an A/B
    comparison a comparison at all.
    """

    def __init__(
        self,
        *,
        config: EvaluationConfig | None = None,
        environment_factory: Callable[[str], AgenticEnvironment] = build_environment,
    ) -> None:
        self.config = config if config is not None else EvaluationConfig()
        self.environment_factory = environment_factory

    def evaluate(
        self,
        policy_a: AgentPolicy,
        policy_b: AgentPolicy,
        tasks: Sequence[EvaluationTask] | None = None,
        *,
        manager_a: AgenticRolloutManager | None = None,
        manager_b: AgenticRolloutManager | None = None,
    ) -> ABResult:
        task_list = tuple(tasks or default_tasks())
        evaluator = AgenticPolicyEvaluator(
            config=self.config, environment_factory=self.environment_factory
        )
        result_a = evaluator.evaluate(policy_a, task_list, manager=manager_a)
        result_b = evaluator.evaluate(policy_b, task_list, manager=manager_b)
        return ABResult(
            policy_a=policy_a.policy_id,
            policy_b=policy_b.policy_id,
            result_a=result_a,
            result_b=result_b,
            comparison=compare_results(result_a, result_b),
            same_task_distribution=True,
            tasks=tuple(task.to_dict() for task in task_list),
            notes=(
                "both policies ran the same tasks with the same seeds",
                "this experiment switches nothing: promoting a policy is a separate "
                "decision that runs through the promotion gates and a person's approval",
            ),
        )


def evaluation_kinds() -> tuple[str, ...]:
    """The slices every report carries, for a caller that renders them."""
    return ("short", "long", "no_failure", "failure_or_recovery")


def summarise_episode(episode: Episode) -> dict[str, Any]:
    """One episode, in the shape the reports use."""
    return {
        "episode_id": episode.episode_id,
        "environment": episode.environment_id,
        "goal": episode.goal,
        "success": episode.success,
        "termination_reason": episode.termination_reason,
        "steps": episode.length,
        "total_reward": round(float(episode.total_reward), 6),
        "safety_total": round(episode.safety_penalty, 6),
        "verified_steps": len(episode.verified_steps),
        "failed_steps": len(episode.failed_steps),
        "recoveries": len(episode.recovery_events),
        "difficulty_level": episode.difficulty.level.value if episode.difficulty else 0,
    }


def reward_improvement_is_not_evidence(
    baseline: AgenticEvaluationResult, candidate: AgenticEvaluationResult
) -> dict[str, Any]:
    """Whether a reward increase came with the things that make it meaningful."""
    comparison = compare_results(baseline, candidate)
    metrics = as_mapping(comparison.get("metrics"))
    success = as_mapping(metrics.get("task_success_rate"))
    verification = as_mapping(metrics.get("verification_pass_rate"))
    safety = as_mapping(metrics.get("safety_rate"))
    reward = as_mapping(metrics.get("mean_episode_reward"))

    def improved(row: Mapping[str, Any]) -> bool | None:
        delta = row.get("delta")
        return None if delta is None else float(delta) > 0

    return {
        "reward_improved": improved(reward),
        "success_improved": improved(success),
        "verification_improved": improved(verification),
        "safety_improved": improved(safety),
        "reward_without_outcomes": (
            improved(reward) and not (improved(success) or improved(verification))
        ),
        "note": (
            "a higher mean reward is not treated as improvement unless the task or "
            "verification numbers moved with it"
        ),
    }


__all__ = [
    "BASELINE_KINDS",
    "BASELINE_UNAVAILABLE_REASON",
    "RUNNABLE_BASELINES",
    "ABPolicyEvaluator",
    "ABResult",
    "AgenticPolicyEvaluator",
    "EvaluationConfig",
    "EvaluationTask",
    "ShadowComparison",
    "ShadowPolicyEvaluator",
    "baseline_table",
    "compare_results",
    "default_tasks",
    "evaluation_kinds",
    "reward_improvement_is_not_evidence",
    "summarise_episode",
]
