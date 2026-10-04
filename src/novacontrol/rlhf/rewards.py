"""Reward providers: one interface, four ways to earn a reward, one composition.

Every provider answers the same four questions — evaluate, validate, explain,
confidence — and every one of them returns a STRUCTURED
:class:`~novacontrol.evaluation.reward.RewardResult` stamped with where the
signal came from. That stamp is what keeps a person's "no" and an evaluator's
"0.2" from being treated as the same fact: they are both signal, they are not
the same signal, and the difference is stored.

The providers are deliberately unequal and deliberately composable:

  * :class:`HumanRewardProvider` — a person's short feedback, mapped through the
    versioned policy.
  * :class:`AIRatingRewardProvider` — an evaluator's criterion scores.
  * :class:`EvaluationRewardProvider` — Phase 15's weighted engine over the
    recorded trajectory, re-used, not reimplemented. Its readings are the
    verifier/rule voice.
  * :class:`CompositeRewardProvider` — the four combined under configured
    weights, with safety penalties kept separate so "mostly good but unsafe"
    cannot average into "good".

Normalisation is a policy decision, not a reflex: a raw total is always kept,
the normalised reading is derived, and a safety penalty is exempted from being
smoothed away (see :func:`normalize_reward`).
"""

from __future__ import annotations

import abc
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.evaluation.evaluator import EvaluationResult
from novacontrol.evaluation.models import AgentTrajectory
from novacontrol.evaluation.reward import (
    RewardComponent,
    RewardConfig,
    RewardEngine,
    RewardPenalty,
    RewardResult,
)
from novacontrol.rlhf.config import RewardPolicyConfig
from novacontrol.rlhf.evaluators import (
    DEFAULT_CRITERIA,
    EvaluationRequest,
    Evaluator,
    EvaluatorUnavailable,
    RuleBasedEvaluator,
    outcome_facts,
)
from novacontrol.rlhf.feedback import feedback_evidence, feedback_reward_value
from novacontrol.rlhf.models import (
    EVIDENCE_AI_RATING,
    EVIDENCE_HUMAN_ACCEPT,
    EVIDENCE_HUMAN_REJECT,
    RLHF_VERSION,
    AIRating,
    HumanFeedback,
    RewardSource,
    Rollout,
)

#: The name a safety-related penalty keeps wherever it travels, so it can never
#: be confused with an ordinary efficiency penalty.
SAFETY_PENALTY_PREFIX = "safety:"

#: How each provider's raw total is scaled before composition. Phase 15's engine
#: sums weighted components up to about 3; the human and AI readings are already
#: on -1..1. Composition needs one scale, so each provider declares its own.
EVALUATION_SCALE = 3.0
SIGNAL_SCALE = 1.0


class RewardProviderUnavailable(RuntimeError):
    """A provider that cannot measure says so instead of inventing a reward."""


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = _mean(values)
    variance = sum((value - average) ** 2 for value in values) / len(values)
    return float(variance**0.5)


@dataclass(frozen=True, slots=True)
class RewardRequest:
    """What a provider is allowed to look at: recorded behaviour, and nothing else.

    It carries the trajectory (or rollout), the Phase 15 evaluation, the human
    feedback and the AI ratings — all of them already structured. There is no
    field for a model's reasoning, and none will be added; a provider that needs
    more information than this is asking for something this phase does not
    collect.
    """

    trajectory: AgentTrajectory | None = None
    evaluation: EvaluationResult | None = None
    rollout: Rollout | None = None
    feedback: tuple[HumanFeedback, ...] = ()
    ratings: tuple[AIRating, ...] = ()
    task: Mapping[str, Any] = field(default_factory=dict)
    candidate: Mapping[str, Any] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)
    outcome: Mapping[str, Any] = field(default_factory=dict)
    constraints: tuple[str, ...] = ()
    mode: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def subject_id(self) -> str:
        if self.trajectory is not None:
            return self.trajectory.trajectory_id
        if self.rollout is not None:
            return self.rollout.trajectory_id or self.rollout.rollout_id
        if self.feedback:
            return self.feedback[0].target()
        return ""

    def evaluation_request(self, *, criteria: Sequence[str] = ()) -> EvaluationRequest:
        """The observable request an evaluator needs to rate this subject."""
        if self.trajectory is not None:
            request = EvaluationRequest.for_trajectory(
                self.trajectory,
                candidate=self.candidate or None,
                task=self.task or None,
                constraints=self.constraints,
                criteria=criteria,
            )
            return request.with_feedback(self.feedback)
        facts = dict(self.outcome)
        if self.rollout is not None:
            facts.setdefault("task_succeeded", self._rollout_succeeded())
            facts.setdefault("tools", [])
        return EvaluationRequest(
            task_id=_text(self.metadata.get("task_id")),
            trajectory_id=self.subject_id,
            task=dict(self.task),
            context=dict(self.context),
            candidate=dict(self.candidate),
            constraints=self.constraints,
            verification=dict(self.metadata.get("verification") or {}),
            outcome=facts,
            feedback=self.feedback,
            criteria=tuple(criteria) if criteria else DEFAULT_CRITERIA,
        )

    @classmethod
    def for_trajectory(
        cls,
        trajectory: AgentTrajectory,
        *,
        evaluation: EvaluationResult | None = None,
        feedback: Sequence[HumanFeedback] = (),
        ratings: Sequence[AIRating] = (),
        rollout: Rollout | None = None,
        candidate: Mapping[str, Any] | None = None,
        task: Mapping[str, Any] | None = None,
        constraints: Sequence[str] = (),
        mode: str = "",
    ) -> RewardRequest:
        """Read a finished run and everything recorded about it into a request."""
        return cls(
            trajectory=trajectory,
            evaluation=evaluation,
            rollout=rollout,
            feedback=tuple(feedback),
            ratings=tuple(ratings),
            task=dict(task) if task is not None else dict(trajectory.final_result),
            candidate=dict(candidate)
            if candidate is not None
            else dict(trajectory.final_result),
            context=dict(trajectory.context_summary),
            outcome=outcome_facts(trajectory),
            constraints=tuple(str(item) for item in constraints if str(item).strip()),
            mode=mode,
        )

    def _rollout_succeeded(self) -> bool | None:
        if self.rollout is None:
            return None
        status = _text(self.rollout.status)
        if status == "completed":
            return True
        if status == "failed":
            return False
        return None


class RewardProvider(abc.ABC):
    """The interface a reward source implements. Pluggable by construction."""

    provider_id: str = "abstract"
    source: str = RewardSource.RULE.value
    version: str = RLHF_VERSION
    #: The raw total's expected ceiling, used when composing providers.
    scale: float = SIGNAL_SCALE

    def __init__(self, policy: RewardPolicyConfig | None = None) -> None:
        self.policy = policy if policy is not None else RewardPolicyConfig()

    @property
    def name(self) -> str:
        return self.provider_id

    def is_available(self) -> bool:
        return True

    def missing_requirements(self) -> tuple[str, ...]:
        return ()

    def validate(self, request: RewardRequest) -> tuple[str, ...]:
        """Problems with the request itself, if any. Empty means usable."""
        del request
        return ()

    @abc.abstractmethod
    def evaluate(self, request: RewardRequest) -> RewardResult:
        """Produce the reward. Raise :class:`RewardProviderUnavailable` if it cannot."""

    def explain(self, reward: RewardResult) -> str:
        return reward.explanation_summary or f"{self.provider_id} produced no explanation"

    def confidence(self, reward: RewardResult) -> float | None:
        return reward.confidence

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "source": self.source,
            "version": self.version,
            "available": self.is_available(),
            "missing": list(self.missing_requirements()),
        }

    # -- shared plumbing ---------------------------------------------------------

    def _subject(self, request: RewardRequest) -> str:
        if request.trajectory is not None:
            return request.trajectory.trajectory_id
        return request.subject_id

    def _finish(
        self,
        request: RewardRequest,
        *,
        total: float,
        component_rewards: Mapping[str, float],
        penalties: Mapping[str, float],
        components: Sequence[RewardComponent] = (),
        penalty_breakdown: Sequence[RewardPenalty] = (),
        evidence: Sequence[str],
        confidence: float | None,
        evaluator_id: str = "",
        explanation: str = "",
    ) -> RewardResult:
        """Build the final reward with provenance, one construction site."""
        subject = self._subject(request)
        evaluation_id = request.evaluation.evaluation_id if request.evaluation is not None else ""
        normalized = self.policy.normalize_value(total, scale=self.scale)
        reward = RewardResult(
            reward_id=f"rwr-{subject}" if subject else "rwr-unknown",
            trajectory_id=subject,
            evaluation_id=evaluation_id,
            reward_source=self.source,
            confidence=confidence,
            evaluator_id=evaluator_id or self.provider_id,
            total_reward=total,
            normalized_reward=normalized,
            component_rewards=dict(component_rewards),
            penalties=dict(penalties),
            components=tuple(components),
            penalty_breakdown=tuple(penalty_breakdown),
            reward_version=self.version,
            weights=self.policy.to_mapping(),
            explanation_summary=explanation or self._summarise(component_rewards, penalties, total),
            evidence=tuple(dict.fromkeys(str(item) for item in evidence)),
        )
        return reward

    @staticmethod
    def _summarise(
        components: Mapping[str, float], penalties: Mapping[str, float], total: float
    ) -> str:
        parts = [
            f"{name} ({value:+.2f})"
            for name, value in sorted(components.items(), key=lambda item: item[1], reverse=True)
        ]
        text = f"total {total:+.2f}: "
        text += ", ".join(parts) if parts else "no component contributed"
        if penalties:
            text += "; penalties " + ", ".join(
                f"{name} ({value:+.2f})" for name, value in sorted(penalties.items())
            )
        return text


class HumanRewardProvider(RewardProvider):
    """A person's short feedback, mapped to a structured reward.

    The mapping is the versioned policy's, not a constant here: ``accept`` is
    worth what the policy says it is worth, a ``rating`` uses the rating itself,
    and ``report_unsafe`` is negative on purpose. Confidence comes from the
    feedback rows when they carry one and stays ``None`` when they do not —
    an invented confidence would make an unknowable number look measured.
    """

    provider_id = "human_feedback"
    source = RewardSource.HUMAN.value

    def validate(self, request: RewardRequest) -> tuple[str, ...]:
        problems: list[str] = []
        if self.policy.require_integrity and not request.feedback:
            problems.append("no human feedback was supplied")
        unknown = [
            row.feedback_type
            for row in request.feedback
            if not row.typed
        ]
        if unknown:
            problems.append(
                "unknown feedback type(s): " + ", ".join(sorted(set(unknown)))
            )
        return tuple(problems)

    def evaluate(self, request: RewardRequest) -> RewardResult:
        rows = tuple(row for row in request.feedback if row.typed)
        if not rows:
            raise RewardProviderUnavailable(
                "no human feedback describes this subject, so a human reward "
                "cannot be produced"
            )
        signals = [feedback_reward_value(row, self.policy) for row in rows]
        weights = [
            float(row.confidence) if row.confidence is not None else 0.5 for row in rows
        ]
        weighted = sum(signal * weight for signal, weight in zip(signals, weights, strict=True))
        total = weighted / max(1e-9, sum(weights))
        confidences = [row.confidence for row in rows if row.confidence is not None]
        components = [
            RewardComponent(
                name=f"human:{row.feedback_type}",
                value=signal,
                weight=weight,
                contribution=signal * weight,
                reason=(
                    f"feedback {row.feedback_id} said {row.feedback_type}"
                    + (f" ({row.reason_category})" if row.reason_category else "")
                ),
            )
            for row, signal, weight in zip(rows, signals, weights, strict=True)
        ]
        evidence: list[str] = [
            sign for row in rows for sign in feedback_evidence(row)
        ]
        if not evidence:
            evidence = [EVIDENCE_HUMAN_ACCEPT if total >= 0 else EVIDENCE_HUMAN_REJECT]
        return self._finish(
            request,
            total=max(-1.0, min(1.0, total)),
            component_rewards={item.name: item.contribution for item in components},
            penalties={},
            components=components,
            evidence=evidence,
            confidence=_mean(confidences) if confidences else None,
            evaluator_id="human",
            explanation=(
                f"total {total:+.2f}: {len(rows)} feedback row(s) "
                + ", ".join(sorted({row.feedback_type for row in rows}))
            ),
        )


class AIRatingRewardProvider(RewardProvider):
    """An evaluator's structured rating, mapped to a reward.

    When the request already carries ratings, they are used as given. When it
    does not, the provider ASKS its evaluator to rate the observable request —
    which is RLAIF's feedback generation step. An evaluator that cannot answer
    (no judge wired, nothing to judge) makes the provider unavailable; a
    criterion score is never invented.
    """

    provider_id = "ai_rating"
    source = RewardSource.AI.value

    def __init__(
        self,
        evaluator: Evaluator | None = None,
        *,
        policy: RewardPolicyConfig | None = None,
        criteria: Sequence[str] = (),
    ) -> None:
        super().__init__(policy)
        self.evaluator = evaluator if evaluator is not None else RuleBasedEvaluator()
        # An empty criteria list means "the shared vocabulary", not "score
        # nothing": a rating with no criterion scores would read as 0.0 and
        # produce a maximally negative reward out of a missing measurement.
        self.criteria = tuple(criteria) if criteria else DEFAULT_CRITERIA

    def is_available(self) -> bool:
        return self.evaluator.is_available()

    def missing_requirements(self) -> tuple[str, ...]:
        return self.evaluator.missing_requirements()

    def validate(self, request: RewardRequest) -> tuple[str, ...]:
        problems = list(self.evaluator.missing_requirements())
        if not request.ratings and request.trajectory is None and not request.outcome:
            problems.append("nothing observable to rate")
        return tuple(problems)

    def rate(self, request: RewardRequest) -> AIRating:
        """Ask the evaluator for a structured rating of this request."""
        evaluation_request = request.evaluation_request(criteria=self.criteria)
        problems = self.evaluator.validate(evaluation_request)
        if problems:
            raise EvaluatorUnavailable("; ".join(problems))
        return self.evaluator.evaluate(evaluation_request)

    def evaluate(self, request: RewardRequest) -> RewardResult:
        ratings = tuple(request.ratings)
        if not ratings:
            ratings = (self.rate(request),)
        usable = [rating for rating in ratings if rating.status != "rejected"]
        if not usable:
            raise RewardProviderUnavailable("every supplied rating was rejected")
        signals = [2.0 * max(0.0, min(1.0, rating.score)) - 1.0 for rating in usable]
        total = _mean(signals)
        weights = [
            float(rating.confidence) if rating.confidence is not None else 0.5
            for rating in usable
        ]
        components = [
            RewardComponent(
                name=f"ai:{rating.evaluator_id or 'evaluator'}",
                value=signal,
                weight=weight,
                contribution=signal * weight,
                reason=f"{rating.evaluator_id or 'evaluator'} scored {rating.score:.2f}",
            )
            for rating, signal, weight in zip(usable, signals, weights, strict=True)
        ]
        criteria = [
            CriterionComponent(item)
            for rating in usable
            for item in rating.criteria
        ]
        components.extend(item.as_component() for item in criteria)
        confidences = [rating.confidence for rating in usable if rating.confidence is not None]
        evidence = [sign for rating in usable for sign in rating.evidence] or [EVIDENCE_AI_RATING]
        return self._finish(
            request,
            total=max(-1.0, min(1.0, total)),
            component_rewards={
                **{item.name: item.contribution for item in components},
                "ai_rating": total,
            },
            penalties={},
            components=components,
            evidence=evidence,
            confidence=_mean(confidences) if confidences else None,
            evaluator_id=",".join(
                sorted({rating.evaluator_id for rating in usable if rating.evaluator_id})
            )
            or self.evaluator.evaluator_id,
            explanation=(
                f"total {total:+.2f}: "
                + ", ".join(
                    f"{rating.evaluator_id or 'evaluator'} {rating.score:.2f}"
                    for rating in usable
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class CriterionComponent:
    """A criterion score as a composable component (typing helper)."""

    score: Any

    def as_component(self) -> RewardComponent:
        item = self.score
        return RewardComponent(
            name=f"criterion:{item.name}",
            value=float(item.score),
            weight=float(item.weight),
            contribution=float(item.contribution),
            reason=item.reason or f"{item.name} scored {item.score:.2f}",
        )


class EvaluationRewardProvider(RewardProvider):
    """Phase 15's weighted engine, re-used as a provider.

    This is the rule/verifier voice: it reads the trajectory and the evaluation
    and produces the same total Phase 15 already defines, then stamps it with
    this phase's provenance. Nothing is reimplemented here — the engine's weights
    and its penalty vocabulary stay the single source of truth.
    """

    provider_id = "evaluation"
    source = RewardSource.RULE.value
    scale = EVALUATION_SCALE

    def __init__(
        self,
        engine: RewardEngine | None = None,
        *,
        policy: RewardPolicyConfig | None = None,
        source: str = "",
    ) -> None:
        super().__init__(policy)
        self.engine = engine if engine is not None else RewardEngine(RewardConfig())
        if source:
            self.source = source

    def validate(self, request: RewardRequest) -> tuple[str, ...]:
        if request.trajectory is None:
            return ("a trajectory is required for an evaluation-backed reward",)
        return ()

    def evaluate(self, request: RewardRequest) -> RewardResult:
        if request.trajectory is None:
            raise RewardProviderUnavailable(
                "no trajectory was supplied, so Phase 15's engine has nothing to score"
            )
        base = self.engine.score(request.trajectory, request.evaluation)
        confidence = self._confidence(request.evaluation)
        safety_penalties = [
            item
            for item in base.penalty_breakdown
            if item.name == "unsafe_action" and item.contribution > 0
        ]
        evidence = list(base.evidence)
        if safety_penalties:
            evidence.append("unsafe_action")
        return self._finish(
            request,
            total=base.total_reward,
            component_rewards={
                f"evaluation:{name}": value
                for name, value in base.component_rewards.items()
                if value
            },
            penalties={
                f"evaluation:{name}": value
                for name, value in base.penalties.items()
                if value
            },
            components=base.components,
            penalty_breakdown=base.penalty_breakdown,
            evidence=evidence,
            confidence=confidence,
            evaluator_id=request.evaluation.evaluator_version
            if request.evaluation is not None
            else "phase15_reward_engine",
            explanation=base.explanation_summary,
        )

    @staticmethod
    def _confidence(evaluation: EvaluationResult | None) -> float | None:
        if evaluation is None:
            return None
        scored = sum(1 for score in evaluation.scores().values() if score is not None)
        total = max(1, len(evaluation.scores()))
        return max(0.3, min(0.95, scored / total))


class CompositeRewardProvider(RewardProvider):
    """Several providers, combined under configured weights.

    Composition does three things and refuses to do a fourth. It WEIGHTS each
    source by the policy (a person's judgement counts for more than a rating,
    by default), it NORMALISES each provider's reading onto one scale before
    combining, and it KEEPS SAFETY SEPARATE: a sub-reward's unsafe-action
    penalty survives into the composite as its own term and caps the total, so a
    run that was mostly good and unsafe cannot average it away. What it refuses
    to do is decide which source is right — that is the disagreement detector's
    job, and it only flags.
    """

    provider_id = "composite"
    source = RewardSource.COMPOSITE.value

    def __init__(
        self,
        providers: Sequence[RewardProvider] = (),
        *,
        policy: RewardPolicyConfig | None = None,
    ) -> None:
        super().__init__(policy)
        self.providers: tuple[RewardProvider, ...] = tuple(providers)

    def is_available(self) -> bool:
        return any(provider.is_available() for provider in self.providers)

    def missing_requirements(self) -> tuple[str, ...]:
        if not self.providers:
            return ("at least one reward provider",)
        return tuple(
            requirement
            for provider in self.providers
            if not provider.is_available()
            for requirement in provider.missing_requirements()
        )

    def validate(self, request: RewardRequest) -> tuple[str, ...]:
        problems: list[str] = []
        if not self.providers:
            problems.append("no providers are composed")
        for provider in self.providers:
            problems.extend(
                f"{provider.provider_id}: {problem}" for problem in provider.validate(request)
            )
        return tuple(problems)

    def evaluate(self, request: RewardRequest) -> RewardResult:
        if not self.providers:
            raise RewardProviderUnavailable(
                "no providers are composed, so there is nothing to combine"
            )
        components: list[RewardComponent] = []
        penalties: list[RewardPenalty] = []
        evidence: list[str] = []
        confidences: list[float] = []
        weighted: list[tuple[float, float]] = []
        sources: list[str] = []
        unavailable: list[str] = []
        for provider in self.providers:
            if not self.policy.allows(provider.source):
                continue
            try:
                reward = provider.evaluate(request)
            except RewardProviderUnavailable:
                unavailable.append(provider.provider_id)
                continue
            weight = self.policy.weight(provider.source)
            normalized = self.policy.normalize_value(reward.total_reward, scale=provider.scale)
            if weight > 0:
                weighted.append((weight, normalized))
            components.append(
                RewardComponent(
                    name=f"source:{provider.source}",
                    value=reward.total_reward,
                    weight=weight,
                    contribution=weight * normalized,
                    reason=provider.explain(reward),
                )
            )
            for item in reward.penalty_breakdown:
                contribution = float(item.contribution)
                if item.name == "unsafe_action" and contribution > 0:
                    penalties.append(
                        RewardPenalty(
                            name=SAFETY_PENALTY_PREFIX + item.name,
                            count=item.count,
                            weight=item.weight,
                            contribution=contribution,
                            reason=item.reason or "an unsafe action bypassed its gate",
                        )
                    )
                elif item.name.startswith(SAFETY_PENALTY_PREFIX) and contribution > 0:
                    penalties.append(item)
            if reward.confidence is not None:
                confidences.append(float(reward.confidence))
            evidence.extend(reward.evidence)
            sources.append(f"{provider.source}:{provider.provider_id}")
        if not weighted:
            raise RewardProviderUnavailable(
                "no provider could measure this subject"
                + (f" (unavailable: {', '.join(unavailable)})" if unavailable else "")
            )
        weight_total = sum(weight for weight, _ in weighted)
        total = sum(weight * value for weight, value in weighted) / max(1e-9, weight_total)
        safety_total = sum(item.contribution for item in penalties)
        if safety_total > 0 and self.policy.keep_safety_separate:
            floor = min(self.policy.safety_floor, 0.0)
            capped = min(total, floor)
            components.append(
                RewardComponent(
                    name="safety_cap",
                    value=capped - total,
                    weight=1.0,
                    contribution=capped - total,
                    reason=(
                        "the composite was capped because a safety penalty applied: "
                        "an unsafe run cannot read as an acceptable one"
                    ),
                )
            )
            total = capped
        explanation = (
            f"total {total:+.2f} from {len(weighted)} source(s): "
            + ", ".join(
                f"{name} {value:+.2f}" for name, value in
                sorted(
                    ((item.name, item.contribution) for item in components),
                    key=lambda pair: pair[1],
                    reverse=True,
                )
            )
        )
        if penalties:
            explanation += "; safety penalties kept separate: " + ", ".join(
                f"{item.name} ({item.contribution:+.2f})" for item in penalties
            )
        if unavailable:
            explanation += "; unavailable providers: " + ", ".join(unavailable)
        return self._finish(
            request,
            total=max(-1.0, min(1.0, total)),
            component_rewards={item.name: item.contribution for item in components},
            penalties={item.name: item.contribution for item in penalties},
            components=components,
            penalty_breakdown=penalties,
            evidence=evidence or [EVIDENCE_AI_RATING],
            confidence=_mean(confidences) if confidences else None,
            evaluator_id="composite(" + ",".join(sources) + ")",
            explanation=explanation,
        )


def default_composite(
    *,
    evaluator: Evaluator | None = None,
    engine: RewardEngine | None = None,
    policy: RewardPolicyConfig | None = None,
    include_human: bool = True,
    include_ai: bool = True,
    include_evaluation: bool = True,
) -> CompositeRewardProvider:
    """The four providers, assembled in the order their sources matter."""
    resolved = policy if policy is not None else RewardPolicyConfig()
    providers: list[RewardProvider] = []
    if include_human:
        providers.append(HumanRewardProvider(resolved))
    if include_ai:
        providers.append(AIRatingRewardProvider(evaluator, policy=resolved))
    if include_evaluation:
        providers.append(EvaluationRewardProvider(engine, policy=resolved))
    return CompositeRewardProvider(providers, policy=resolved)


def normalize_reward(
    reward: RewardResult,
    policy: RewardPolicyConfig,
    *,
    scale: float = EVALUATION_SCALE,
) -> RewardResult:
    """The same reward with a normalized reading, safety kept distinguishable.

    The raw total is never replaced: ``total_reward`` stays what the source said
    and ``normalized_reward`` is the derived reading. When a safety penalty
    applied, the normalized value is floored at the policy's safety floor, so
    dividing by a scale cannot turn an unsafe run into a merely-average one.
    """
    normalized = policy.normalize_value(reward.total_reward, scale=scale)
    safety_applied = any(
        item.contribution > 0 and item.name in {"unsafe_action"}
        for item in reward.penalty_breakdown
    ) or any(
        str(name).startswith(SAFETY_PENALTY_PREFIX) and value > 0
        for name, value in reward.penalties.items()
    )
    if safety_applied and policy.keep_safety_separate:
        normalized = min(normalized, min(0.0, policy.safety_floor))
    return RewardResult.from_dict({**reward.to_dict(), "normalized_reward": normalized})


def clip_reward(reward: RewardResult, policy: RewardPolicyConfig) -> RewardResult:
    """The reward with its total clipped into the policy's range."""
    return RewardResult.from_dict(
        {**reward.to_dict(), "total_reward": policy.clip(reward.total_reward)}
    )


def component_rewards(reward: RewardResult, *, source: str = "") -> dict[str, float]:
    """The components of one reward, optionally only those of one source."""
    if not source:
        return dict(reward.component_rewards)
    prefix = f"{source}:"
    return {
        name: value
        for name, value in reward.component_rewards.items()
        if name.startswith(prefix)
    }


def reward_evidence_ok(reward: RewardResult) -> bool:
    """Whether a reward cites at least one observable fact."""
    return bool(reward.evidence)


def provider_for(
    name: str,
    *,
    evaluator: Evaluator | None = None,
    engine: RewardEngine | None = None,
    policy: RewardPolicyConfig | None = None,
    providers: Sequence[RewardProvider] = (),
) -> RewardProvider:
    """Build a provider by name. Unknown names are refused, never defaulted."""
    wanted = _text(name, "auto").lower()
    resolved = policy if policy is not None else RewardPolicyConfig()
    if providers:
        return CompositeRewardProvider(providers, policy=resolved)
    if wanted in {"auto", "composite"}:
        return default_composite(evaluator=evaluator, engine=engine, policy=resolved)
    if wanted in {"human", "human_feedback"}:
        return HumanRewardProvider(resolved)
    if wanted in {"ai", "ai_rating"}:
        return AIRatingRewardProvider(evaluator, policy=resolved)
    if wanted in {"rule", "evaluation", "verifier"}:
        return EvaluationRewardProvider(engine, policy=resolved)
    raise ValueError(
        f"unknown reward provider {name!r}: expected auto, composite, human, ai or evaluation"
    )


__all__ = [
    "AIRatingRewardProvider",
    "CriterionComponent",
    "CompositeRewardProvider",
    "EVALUATION_SCALE",
    "EvaluationRewardProvider",
    "HumanRewardProvider",
    "RewardProvider",
    "RewardProviderUnavailable",
    "RewardRequest",
    "SAFETY_PENALTY_PREFIX",
    "SIGNAL_SCALE",
    "clip_reward",
    "component_rewards",
    "default_composite",
    "normalize_reward",
    "provider_for",
    "reward_evidence_ok",
]
