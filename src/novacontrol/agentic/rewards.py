"""Rewards for a whole episode, and the credit that shows which steps earned them.

This extends Phase 15's reward engine rather than replacing it. Phase 15 scores
one finished run; Phase 20 scores each STEP of a multi-step run and then an
episode, and it keeps the dimensions APART the whole way:

    task_success · verification · safety · efficiency · latency · resource_usage
    tool_correctness · planning_efficiency · recovery_quality

Safety is its own dimension with its own weight and is reported separately
everywhere. A step that is fast and unsafe must never average out to a good step,
which is exactly what a single opaque number would do.

Where a finished trajectory exists, the episode reading is handed back as a
Phase 15 :class:`~novacontrol.evaluation.reward.RewardResult` — the same record,
the same component/penalty breakdown, the same JSON — so nothing downstream has to
learn a second reward shape.

Then :class:`CreditAssigner` answers the NEXT question: which steps caused the
outcome? Four simple, checkable methods plus a verifier-based one, all of them
exact arithmetic over the recorded rewards — no estimator is invented, and a
value the run never measured stays ``None``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.agentic.config import AgenticRewardWeights
from novacontrol.agentic.models import (
    REWARD_DIMENSIONS,
    SAFETY_DIMENSION,
    AgentAction,
    CreditAssignmentResult,
    CreditMethod,
    Episode,
    RewardDimension,
    StateTransition,
    StepPenalty,
    StepReward,
    StepSignal,
    TerminationReason,
    as_int,
    as_mapping,
    as_text,
)
from novacontrol.evaluation.models import AgentTrajectory
from novacontrol.evaluation.reward import (
    REWARD_VERSION,
    RewardComponent,
    RewardEngine,
    RewardPenalty,
    RewardResult,
)
from novacontrol.rlhf.models import RewardSource

#: The source stamped on every reading this module produces: a deterministic rule,
#: never a model's opinion.
REWARD_SOURCE = RewardSource.RULE.value

#: A ratio above which a latency or resource overrun starts costing reward.
OVERRUN_TOLERANCE = 1.0


@dataclass(frozen=True, slots=True)
class StepContext:
    """Everything the step reward is computed from — explicitly, not by closure."""

    index: int = 0
    action: AgentAction = field(default_factory=AgentAction)
    observation: Mapping[str, Any] = field(default_factory=dict)
    result: Mapping[str, Any] = field(default_factory=dict)
    verification: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""
    progressed: bool = False
    repeated: bool = False
    repeat_count: int = 0
    latency_ms: float | None = None
    resource_used: float | None = None
    expected_tool: str = ""
    recovery: Mapping[str, Any] = field(default_factory=dict)
    done: bool = False
    success: bool | None = None
    termination_reason: str = ""
    unsafe: bool = False
    unnecessary: bool = False
    arguments_invalid: bool = False


class AgenticRewardEngine:
    """Step and episode rewards over the multi-objective vocabulary."""

    def __init__(
        self,
        weights: AgenticRewardWeights | None = None,
        *,
        engine: RewardEngine | None = None,
        source: str = REWARD_SOURCE,
    ) -> None:
        self.weights = weights if weights is not None else AgenticRewardWeights()
        self.engine = engine
        self.source = source

    # -- one step ---------------------------------------------------------------

    def step_reward(self, context: StepContext) -> StepReward:
        """The step's reward, with every dimension it touched recorded."""
        w = self.weights
        contributions: dict[str, float] = {}
        signals: list[str] = []
        penalties: list[str] = []
        reasons: list[str] = []

        def add(dimension: str, value: float, reason: str) -> None:
            if not value:
                return
            contributions[dimension] = round(contributions.get(dimension, 0.0) + value, 6)
            reasons.append(reason)

        verification_status = as_text(context.verification.get("status"))
        terminal = bool(context.done and context.termination_reason)

        # Safety first, and separately: an unsafe step is never rescued by any
        # other dimension.
        if context.unsafe:
            penalties.append(StepPenalty.UNSAFE_ACTION.value)
            add(
                SAFETY_DIMENSION,
                -w.safety * w.unsafe_penalty,
                "the action was unsafe or unapproved",
            )

        # Tool correctness: was the tool the one the task wanted?
        if context.expected_tool:
            if context.action.name == context.expected_tool:
                signals.append(StepSignal.CORRECT_TOOL.value)
                add(
                    RewardDimension.TOOL_CORRECTNESS.value,
                    w.tool_correctness * w.correct_tool_reward,
                    f"the correct tool ({context.expected_tool}) was used",
                )
            else:
                penalties.append(StepPenalty.INCORRECT_TOOL.value)
                add(
                    RewardDimension.TOOL_CORRECTNESS.value,
                    -w.tool_correctness * w.incorrect_tool_penalty,
                    f"the wrong tool ({context.action.name}) was used where "
                    f"{context.expected_tool!r} was required",
                )

        # A valid action: it ran and the environment did not refuse it.
        refused = bool(as_mapping(context.result).get("refused"))
        if not refused:
            signals.append(StepSignal.VALID_ACTION.value)
            add(
                RewardDimension.EFFICIENCY.value,
                w.efficiency * w.valid_action_reward,
                "the action was valid here",
            )
        if context.arguments_invalid:
            penalties.append(StepPenalty.INVALID_ARGUMENTS.value)
            add(
                RewardDimension.EFFICIENCY.value,
                -w.efficiency * w.invalid_arguments_penalty,
                "the action's arguments were invalid",
            )

        # Progress: an action that advanced the task.
        if context.progressed:
            signals.append(StepSignal.SUBTASK_SUCCEEDED.value)
            add(
                RewardDimension.PLANNING_EFFICIENCY.value,
                w.planning_efficiency * w.subtask_reward,
                "the step advanced the task",
            )
        # Information gathering that actually produced something.
        if as_text(context.action.action_type) in {"information_gathering"} and not context.error:
            signals.append(StepSignal.INFORMATION_GAINED.value)
            add(
                RewardDimension.PLANNING_EFFICIENCY.value,
                w.planning_efficiency * w.information_reward,
                "the step gathered useful information",
            )

        # Efficiency: an unnecessary action costs, an efficient one earns a token.
        if context.unnecessary or (context.action.is_noop and not terminal):
            penalties.append(StepPenalty.UNNECESSARY_ACTION.value)
            add(
                RewardDimension.EFFICIENCY.value,
                -w.efficiency * w.unnecessary_action_penalty,
                "the action was unnecessary",
            )
        elif not (context.error and context.repeated):
            # Repeating a FAILED action is a loop; repeating a successful one is
            # how a long task is finished, so only the first is charged.
            signals.append(StepSignal.EFFICIENT_ACTION.value)
            add(
                RewardDimension.EFFICIENCY.value,
                w.efficiency * w.efficient_action_reward,
                "the action was efficient",
            )
        if context.error and context.repeat_count >= 1:
            penalties.append(StepPenalty.REPEATED_FAILURE.value)
            add(
                RewardDimension.EFFICIENCY.value,
                -w.efficiency * w.repeated_failure_penalty,
                f"the same action failed again ({context.repeat_count + 1} attempts)",
            )

        # Failure and its verification.
        if context.error:
            penalties.append(StepPenalty.FAILED_ACTION.value)
            add(
                RewardDimension.VERIFICATION.value,
                -w.verification * w.failed_action_penalty,
                f"the step failed: {context.error}",
            )
        if verification_status == "pass":
            signals.append(StepSignal.VERIFICATION_PASSED.value)
            add(
                RewardDimension.VERIFICATION.value,
                w.verification * w.verification_passed_reward,
                "verification passed",
            )
        elif verification_status == "fail" and not context.error:
            penalties.append(StepPenalty.FAILED_ACTION.value)
            add(
                RewardDimension.VERIFICATION.value,
                -w.verification * w.failed_action_penalty,
                "verification failed",
            )

        # Latency, only when it was actually measured.
        if context.latency_ms is not None and w.latency_budget_ms > 0:
            over = max(0.0, float(context.latency_ms) - w.latency_budget_ms)
            if over > 0:
                # Charged once, and the first 100% of the budget is free.
                penalties.append(StepPenalty.EXCESSIVE_LATENCY.value)
                add(
                    RewardDimension.LATENCY.value,
                    -w.latency * w.latency_penalty,
                    f"the step took {context.latency_ms:.1f} ms against a "
                    f"{w.latency_budget_ms:.0f} ms budget",
                )

        # Resource usage, only when it was measured and the budget was exceeded.
        if (
            context.resource_used is not None
            and w.resource_budget > 0
            and float(context.resource_used) > w.resource_budget
        ):
            penalties.append(StepPenalty.EXCESSIVE_RESOURCE_USAGE.value)
            add(
                RewardDimension.RESOURCE_USAGE.value,
                -w.resource_usage * w.resource_penalty,
                f"the step used {context.resource_used:.3f} against a "
                f"{w.resource_budget:.3f} budget",
            )

        # Recovery quality: recovering well is worth something, recovering when
        # nothing was wrong is worth less than nothing.
        if context.recovery:
            outcome = context.recovery.get("succeeded")
            unnecessary = bool(context.recovery.get("unnecessary"))
            strategy = as_text(context.recovery.get("strategy"), "unknown")
            if unnecessary:
                penalties.append(StepPenalty.UNNECESSARY_RECOVERY.value)
                add(
                    RewardDimension.RECOVERY_QUALITY.value,
                    -w.recovery_quality * w.unnecessary_recovery_penalty,
                    "a recovery ran where nothing had failed",
                )
            elif outcome is True:
                add(
                    RewardDimension.RECOVERY_QUALITY.value,
                    w.recovery_quality * w.subtask_reward,
                    f"recovery ({strategy}) succeeded",
                )
            elif outcome is False:
                penalties.append(StepPenalty.REPEATED_FAILURE.value)
                add(
                    RewardDimension.RECOVERY_QUALITY.value,
                    -w.recovery_quality * w.repeated_failure_penalty,
                    f"recovery ({strategy}) was attempted and did not succeed",
                )
            else:
                # ``None`` means the recovery has not concluded yet. Charging it
                # here would bill the same failure twice: once as the failure and
                # once as a recovery that has not finished.
                reasons.append(
                    f"recovery ({strategy}) was chosen and has not concluded yet"
                )

        # Terminal reward, when this step ended the episode.
        if terminal:
            self._terminal_contributions(
                contributions, signals, penalties, reasons, context
            )

        total = math.fsum(contributions.values())
        return StepReward(
            index=max(0, as_int(context.index)),
            total=round(total, 6),
            dimensions=contributions,
            signals=tuple(signals),
            penalties=tuple(penalties),
            reasons=tuple(reasons),
            safety=round(contributions.get(SAFETY_DIMENSION, 0.0), 6),
            source=self.source,
            terminal=terminal,
        )

    def _terminal_contributions(
        self,
        contributions: dict[str, float],
        signals: list[str],
        penalties: list[str],
        reasons: list[str],
        context: StepContext,
    ) -> None:
        w = self.weights
        success = context.success
        if success is True:
            signals.append(StepSignal.SUBTASK_SUCCEEDED.value)
            value = w.task_success * w.terminal_success
            contributions[RewardDimension.TASK_SUCCESS.value] = round(
                contributions.get(RewardDimension.TASK_SUCCESS.value, 0.0) + value, 6
            )
            reasons.append("the task completed and verification agreed")
        elif success is False:
            penalties.append(StepPenalty.FAILED_ACTION.value)
            value = w.task_success * w.terminal_failure
            contributions[RewardDimension.TASK_SUCCESS.value] = round(
                contributions.get(RewardDimension.TASK_SUCCESS.value, 0.0) + value, 6
            )
            reasons.append(
                f"the task ended without success ({context.termination_reason or 'failure'})"
            )
        else:
            # ``None`` is a real outcome: nobody decided. It is neither rewarded
            # nor punished, and the episode says so.
            reasons.append(
                "the episode ended without anything deciding whether it worked"
            )
        if as_text(context.verification.get("status")) == "pass":
            contributions[RewardDimension.VERIFICATION.value] = round(
                contributions.get(RewardDimension.VERIFICATION.value, 0.0)
                + w.verification * w.terminal_verification,
                6,
            )
            reasons.append("the final verification passed")

    # -- a whole episode --------------------------------------------------------

    def episode_reward(self, episode: Episode) -> dict[str, Any]:
        """The episode's totals per dimension, plus the step/total relationship."""
        w = self.weights
        totals: dict[str, float] = dict.fromkeys(REWARD_DIMENSIONS, 0.0)
        for step in episode.steps:
            if step.reward is None:
                continue
            for name, value in step.reward.dimensions.items():
                if name in totals:
                    totals[name] += float(value)
        step_total = math.fsum(float(step.step_reward) for step in episode.steps)
        terminal_total = math.fsum(
            float(step.step_reward) for step in episode.steps if step.terminal
        )
        intermediate = step_total - terminal_total
        # The guard the specification asks for: a policy must not be able to farm
        # step rewards into a success. The STORED steps keep what they measured;
        # what is capped is the reading that says how much of the total came from
        # shaping, and the cap is reported.
        shaped_share: float | None = None
        capped_intermediate = intermediate
        if terminal_total:
            share = abs(intermediate) / abs(terminal_total)
            shaped_share = round(share, 6)
            limit = abs(terminal_total) * w.max_step_reward_share
            if abs(intermediate) > limit:
                capped_intermediate = math.copysign(limit, intermediate)
        return {
            "total_reward": round(math.fsum([capped_intermediate, terminal_total]), 6),
            "measured_total_reward": round(step_total, 6),
            "intermediate_reward": round(capped_intermediate, 6),
            "terminal_reward": round(terminal_total, 6),
            "dimension_totals": {
                name: round(value, 6) for name, value in totals.items()
            },
            "safety_total": round(totals.get(SAFETY_DIMENSION, 0.0), 6),
            "shaped_share": shaped_share,
            "step_reward_cap": round(
                abs(terminal_total) * w.max_step_reward_share, 6
            ),
            "reasons": (
                "the shaped (intermediate) reward was capped at "
                f"{w.max_step_reward_share:.0%} of the terminal reward so step "
                "rewards cannot outweigh the outcome"
                if capped_intermediate != intermediate
                else "the intermediate reward stayed inside the shaping cap"
            ),
            "gamma": 0.0,
        }

    def to_reward_result(
        self,
        episode: Episode,
        *,
        trajectory: AgentTrajectory | None = None,
        gamma: float = 1.0,
    ) -> RewardResult:
        """The episode as a Phase 15 reward record, dimensions included.

        This is the bridge the specification asks for: the existing
        :class:`RewardResult` carries the same component/penalty breakdown, so a
        reader that already knows Phase 15 needs no new vocabulary.
        """
        summary = self.episode_reward(episode)
        components: list[RewardComponent] = []
        for name in REWARD_DIMENSIONS:
            value = float(summary["dimension_totals"].get(name, 0.0))
            if not value:
                continue
            weight = self.weights.weight_for(name)
            components.append(
                RewardComponent(
                    name=name,
                    value=round(value / weight, 6) if weight else value,
                    weight=weight,
                    contribution=round(value, 6),
                    reason=_dimension_reason(name, value),
                )
            )
        penalties: list[RewardPenalty] = []
        if float(summary["safety_total"]) < 0:
            penalties.append(
                RewardPenalty(
                    name=StepPenalty.UNSAFE_ACTION.value,
                    count=1.0,
                    weight=self.weights.safety,
                    contribution=round(float(summary["safety_total"]), 6),
                    reason="safety penalties are reported separately from efficiency",
                )
            )
        breakdown = episode.to_rollout().breakdown(gamma=max(0.0, min(1.0, gamma)))
        return RewardResult(
            trajectory_id=trajectory.trajectory_id if trajectory is not None else "",
            evaluation_id="",
            reward_source=self.source,
            total_reward=float(summary["total_reward"]),
            component_rewards=dict(summary["dimension_totals"]),
            penalties={
                item.name: item.contribution for item in penalties
            },
            components=tuple(components),
            penalty_breakdown=tuple(penalties),
            reward_version=REWARD_VERSION,
            weights=self.weights.to_mapping(),
            explanation_summary=(
                f"episode {episode.episode_id}: "
                f"{summary['total_reward']} total, "
                f"{summary['terminal_reward']} terminal, "
                f"{summary['safety_total']} safety"
            ),
            evidence=(
                f"episode:{episode.episode_id}",
                f"steps:{episode.length}",
                f"termination:{episode.termination_reason or 'unfinished'}",
                f"returns:discounted={breakdown.discounted_reward}",
            ),
        )


def _dimension_reason(name: str, value: float) -> str:
    direction = "earned" if value > 0 else "cost"
    return f"the {name} dimension {direction} {abs(value):.3f} over the episode"


# ── credit assignment ────────────────────────────────────────────────────────


class CreditAssigner:
    """Estimates which steps contributed to the outcome.

    Every method is exact arithmetic over the recorded rewards. Nothing here
    invents a value the run did not measure: a step whose reward was never
    observed contributes zero and says so in the reasons.
    """

    def __init__(
        self,
        method: str = CreditMethod.DISCOUNTED_RETURN.value,
        *,
        gamma: float = 0.99,
        baseline: float | None = None,
    ) -> None:
        self.method = method if method in _METHODS else CreditMethod.DISCOUNTED_RETURN.value
        self.gamma = max(0.0, min(1.0, float(gamma)))
        self.baseline = baseline

    def discounted_returns(self, rewards: Sequence[float]) -> tuple[float, ...]:
        """``G_t = r_t + γ r_{t+1} + γ² r_{t+2} + …``, computed backwards.

        Backwards accumulation is what keeps the arithmetic stable: the running
        total is multiplied by gamma and a single reward is added each step, so
        nothing is raised to a large power and no error compounds.
        """
        returns = [0.0] * len(rewards)
        running = 0.0
        for index in range(len(rewards) - 1, -1, -1):
            running = float(rewards[index]) + self.gamma * running
            returns[index] = running
        return tuple(round(value, 6) for value in returns)

    def cumulative(self, rewards: Sequence[float]) -> tuple[float, ...]:
        running = 0.0
        out: list[float] = []
        for value in rewards:
            running = math.fsum([running, float(value)])
            out.append(round(running, 6))
        return tuple(out)

    def assign(self, episode: Episode) -> CreditAssignmentResult:
        rewards = list(episode.step_rewards())
        indices = [step.index for step in episode.steps]
        returns = self.discounted_returns(rewards)
        terminal = [
            float(step.step_reward) for step in episode.steps if step.terminal
        ]
        terminal_total = math.fsum(terminal)
        reasons: list[str] = []
        credits: dict[str, float] = {}
        advantages: dict[str, float] = {}

        method = self.method
        if method == CreditMethod.STEP_ACCUMULATION.value:
            credits = {str(i): round(r, 6) for i, r in zip(indices, rewards, strict=False)}
            reasons.append("each step is credited with the reward it earned")
        elif method == CreditMethod.DISCOUNTED_RETURN.value:
            credits = {str(i): round(r, 6) for i, r in zip(indices, returns, strict=False)}
            reasons.append(
                f"each step is credited with its discounted return at gamma={self.gamma}"
            )
        elif method == CreditMethod.TERMINAL_PROPAGATION.value:
            share = terminal_total / len(indices) if indices else 0.0
            credits = {
                str(i): round(math.fsum([r, share]), 6)
                for i, r in zip(indices, rewards, strict=False)
            }
            reasons.append(
                "the terminal reward is propagated equally across the steps, and each "
                "step keeps the reward it earned"
            )
        elif method == CreditMethod.ADVANTAGE.value:
            baseline = (
                self.baseline
                if self.baseline is not None
                else (math.fsum(returns) / len(returns) if returns else 0.0)
            )
            advantages = {
                str(i): round(r - baseline, 6)
                for i, r in zip(indices, returns, strict=False)
            }
            credits = dict(advantages)
            reasons.append(
                f"each step is credited with its return above the episode baseline "
                f"({baseline:.6f})"
            )
        elif method == CreditMethod.VERIFIER_ATTRIBUTION.value:
            verified = [
                step.index
                for step in episode.steps
                if as_text(step.verification.get("status")) == "pass"
            ]
            share = terminal_total / len(verified) if verified else 0.0
            credits = {
                str(i): round(math.fsum([r, share if i in verified else 0.0]), 6)
                for i, r in zip(indices, rewards, strict=False)
            }
            reasons.append(
                "the terminal reward is shared only among steps whose verification "
                f"passed ({len(verified)} step(s))"
            )
        if not episode.steps:
            reasons.append("the episode has no steps, so nothing is credited")
        if not terminal_total:
            reasons.append(
                "the episode has no terminal reward, so the credit is shaped reward only"
            )
        return CreditAssignmentResult(
            method=method,
            credits=credits,
            returns={str(i): round(r, 6) for i, r in zip(indices, returns, strict=False)},
            advantages=advantages,
            discounted_return=round(returns[0] if returns else 0.0, 6),
            gamma=self.gamma,
            reasons=tuple(reasons),
        )

    def cumulative_and_returns(self, episode: Episode) -> dict[str, Any]:
        """The episode's cumulative and discounted readings side by side."""
        rewards = list(episode.step_rewards())
        returns = self.discounted_returns(rewards)
        return {
            "step_rewards": [round(value, 6) for value in rewards],
            "cumulative": list(self.cumulative(rewards)),
            "discounted": list(returns),
            "episode_return": round(returns[0] if returns else 0.0, 6),
            "undiscounted_return": round(math.fsum(rewards), 6),
            "gamma": self.gamma,
        }


_METHODS: tuple[str, ...] = tuple(member.value for member in CreditMethod)


def dimension_totals(episode: Episode) -> dict[str, float]:
    """The episode's reward per dimension, read from the stored steps."""
    totals = dict.fromkeys(REWARD_DIMENSIONS, 0.0)
    for step in episode.steps:
        if step.reward is None:
            continue
        for name, value in step.reward.dimensions.items():
            if name in totals:
                totals[name] += float(value)
    return {name: round(value, 6) for name, value in totals.items()}


def terminal_step(episode: Episode) -> StateTransition | None:
    for step in reversed(episode.steps):
        if step.terminal:
            return step
    return None


def episode_success(episode: Episode) -> bool | None:
    """The episode's success, taken from the record and never inferred."""
    if episode.success is not None:
        return episode.success
    if episode.termination_reason == TerminationReason.SUCCESS.value:
        return True
    if episode.termination_reason in {
        TerminationReason.FAILURE.value,
        TerminationReason.ENVIRONMENT_ERROR.value,
    }:
        return False
    return None


def step_failed(step: StateTransition) -> bool:
    return bool(step.error) or as_text(step.verification.get("status")) == "fail"


__all__ = [
    "OVERRUN_TOLERANCE",
    "REWARD_SOURCE",
    "AgenticRewardEngine",
    "CreditAssigner",
    "StepContext",
    "dimension_totals",
    "episode_success",
    "step_failed",
    "terminal_step",
]
