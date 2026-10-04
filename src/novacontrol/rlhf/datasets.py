"""Building a reward dataset out of recorded behaviour — carefully.

This is the step where a pile of trajectories, feedback, ratings, rollouts and
rewards becomes something a policy optimizer may learn from. It is also the step
where reward hacking would enter the pipeline if nothing stopped it, so the
builder's order is: assemble, AUDIT, classify, deduplicate, split.

What it refuses to do:

  * It does not accept a row whose reward failed its integrity check. An
    ``invalid`` reward is stored annotated and left out of the splits; a
    ``suspicious`` one is HELD (needs_review) unless the rules say otherwise.
  * It does not learn from a reward that cites no observable fact.
  * It does not silently drop anything. Every input row is present in the built
    version with its status and reasons, so a person can audit the exclusions.
  * It does not store hidden reasoning: candidate structures are scanned for
    chain-of-thought-shaped keys during validation, and the version refuses to
    validate if one is found.

Splits reuse Phase 16's splitter by projecting rows onto
:class:`~novacontrol.training.models.SFTTrainingExample` and mapping the
assignment back — one walk implementation, shared, keyed by this dataset's ids.
Whole tasks travel together, so a model is never tested on something it trained
on.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from novacontrol.evaluation.evaluator import EvaluationResult
from novacontrol.evaluation.models import AgentTrajectory
from novacontrol.evaluation.reward import RewardResult
from novacontrol.rlhf.config import RewardPolicyConfig
from novacontrol.rlhf.evaluators import outcome_facts
from novacontrol.rlhf.feedback import FeedbackQualityFilter
from novacontrol.rlhf.integrity import IntegrityContext, RewardIntegrityChecker
from novacontrol.rlhf.models import (
    FEEDBACK_ACCEPTED,
    FEEDBACK_NEEDS_REVIEW,
    FEEDBACK_REJECTED,
    MODES,
    REWARD_EXAMPLE_KINDS,
    REWARD_SOURCES,
    RLHF_VERSION,
    AIRating,
    HumanFeedback,
    RewardDatasetStatistics,
    RewardDatasetVersion,
    RewardExample,
    RewardIntegrityStatus,
    RewardSource,
    Rollout,
)
from novacontrol.rlhf.rewards import RewardProvider, RewardProviderUnavailable, RewardRequest
from novacontrol.training.datasets import SFTDatasetBuilder
from novacontrol.training.models import SplitConfig

#: The split names this phase uses, shared with Phase 16 on purpose.
SPLIT_NAMES: tuple[str, ...] = ("train", "validation", "test")

#: A row a person AND an evaluator both judged. It is not a third signal: an
#: RLHF dataset asks for the person, an RLAIF one for the evaluator, and this row
#: carries both, so it belongs in either.
MODE_MIXED = "mixed"

#: Keys that must never appear in a stored candidate: nothing in this pipeline
#: learns from a model's private reasoning, so a dataset containing it is invalid
#: rather than merely suspicious.
REASONING_KEYS: frozenset[str] = frozenset(
    {"chain_of_thought", "cot", "reasoning", "thinking", "scratchpad", "internal_monologue"}
)

#: Reasons a row was skipped or excluded, kept as stable codes.
REASON_NO_REWARD = "no_reward"
REASON_SOURCE_NOT_ALLOWED = "reward_source_not_allowed"
REASON_REWARD_OUT_OF_RANGE = "reward_out_of_range"
REASON_INTEGRITY_INVALID = "reward_integrity_invalid"
REASON_INTEGRITY_FLAGGED = "reward_integrity_flagged"
REASON_NO_EVIDENCE = "reward_without_evidence"
REASON_NOT_VERIFIED = "verification_required"
REASON_HUMAN_REQUIRED = "human_feedback_required"
REASON_AI_REQUIRED = "ai_feedback_required"
REASON_MODE_MISMATCH = "mode_mismatch"
REASON_DUPLICATE = "duplicate_reward_example"
REASON_CONTRADICTORY = "contradictory_rewards"
REASON_MALFORMED = "malformed_example"
REASON_MISSING_TARGET = "missing_trajectory"

#: The estimated tokens one reward row is assumed to cost when no measurement
#: exists. Small, because a reward row is a task, a candidate and a number.
DEFAULT_TOKENS_PER_EXAMPLE = 128


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _flag(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _texts(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = _mean(values)
    variance = sum((value - average) ** 2 for value in values) / len(values)
    return float(variance**0.5)


@dataclass(frozen=True, slots=True)
class RewardDatasetRules:
    """What this dataset will and will not learn from.

    The defaults are deliberately strict: only rows whose rewards survived their
    integrity check enter a split, a suspicious reward is held rather than
    trusted, and a row must cite an observable fact. An installation can relax
    these, but it has to say so in the rules that travel with the dataset.
    """

    mode: str = "mixed"
    sources: tuple[str, ...] = ()
    require_integrity: bool = True
    hold_suspicious: bool = True
    require_evidence: bool = True
    require_verified: bool = False
    require_human: bool = False
    require_ai: bool = False
    require_reward: bool = True
    min_reward: float | None = None
    max_reward: float | None = None
    deduplicate: bool = True
    include_rejected: bool = True
    max_examples: int = 0
    max_tokens_per_example: int = DEFAULT_TOKENS_PER_EXAMPLE
    group_by: str = "task"
    tag: str = ""

    def validate(self) -> tuple[str, ...]:
        problems: list[str] = []
        if self.mode not in {*MODES, "mixed"}:
            problems.append(f"mode must be rlhf, rlaif or mixed (got {self.mode!r})")
        unknown = [name for name in self.sources if name not in REWARD_SOURCES]
        if unknown:
            problems.append(f"unknown source(s): {', '.join(unknown)}")
        if (
            self.min_reward is not None
            and self.max_reward is not None
            and self.min_reward > self.max_reward
        ):
            problems.append("min_reward is above max_reward")
        if self.group_by not in {"task", "example"}:
            problems.append("group_by must be 'task' or 'example'")
        if not 1 <= self.max_tokens_per_example <= 1_000_000:
            problems.append("max_tokens_per_example must be between 1 and 1000000")
        return tuple(problems)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "sources": list(self.sources),
            "require_integrity": self.require_integrity,
            "hold_suspicious": self.hold_suspicious,
            "require_evidence": self.require_evidence,
            "require_verified": self.require_verified,
            "require_human": self.require_human,
            "require_ai": self.require_ai,
            "require_reward": self.require_reward,
            "min_reward": self.min_reward,
            "max_reward": self.max_reward,
            "deduplicate": self.deduplicate,
            "include_rejected": self.include_rejected,
            "max_examples": self.max_examples,
            "max_tokens_per_example": self.max_tokens_per_example,
            "group_by": self.group_by,
            "tag": self.tag,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> RewardDatasetRules:
        defaults = cls()
        if not isinstance(data, Mapping):
            return defaults

        def real(key: str, current: float | None) -> float | None:
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            return current

        return cls(
            mode=_text(data.get("mode"), defaults.mode),
            sources=_texts(data.get("sources")),
            require_integrity=_flag(data.get("require_integrity"), defaults.require_integrity),
            hold_suspicious=_flag(data.get("hold_suspicious"), defaults.hold_suspicious),
            require_evidence=_flag(data.get("require_evidence"), defaults.require_evidence),
            require_verified=_flag(data.get("require_verified"), defaults.require_verified),
            require_human=_flag(data.get("require_human"), defaults.require_human),
            require_ai=_flag(data.get("require_ai"), defaults.require_ai),
            require_reward=_flag(data.get("require_reward"), defaults.require_reward),
            min_reward=real("min_reward", defaults.min_reward),
            max_reward=real("max_reward", defaults.max_reward),
            deduplicate=_flag(data.get("deduplicate"), defaults.deduplicate),
            include_rejected=_flag(data.get("include_rejected"), defaults.include_rejected),
            max_examples=max(0, int(data.get("max_examples") or defaults.max_examples)),
            max_tokens_per_example=int(
                data.get("max_tokens_per_example") or defaults.max_tokens_per_example
            ),
            group_by=_text(data.get("group_by"), defaults.group_by),
            tag=_text(data.get("tag")),
        )


@dataclass(frozen=True, slots=True)
class RewardDatasetRequest:
    """The raw material one build reads, all of it optional.

    A caller may hand the builder examples that are already assembled, or raw
    trajectories with their evaluations, feedback, ratings, rewards and rollouts
    — and the builder will assemble the rest. When a reward provider is
    configured, subjects without a reward get one generated from their recorded
    behaviour; when it is not, a subject without a reward is skipped rather than
    given a zero nobody measured.
    """

    trajectories: tuple[AgentTrajectory, ...] = ()
    evaluations: tuple[EvaluationResult, ...] = ()
    feedback: tuple[HumanFeedback, ...] = ()
    ratings: tuple[AIRating, ...] = ()
    rewards: tuple[RewardResult, ...] = ()
    rollouts: tuple[Rollout, ...] = ()
    examples: tuple[RewardExample, ...] = ()
    source_datasets: tuple[str, ...] = ()
    source_data_version: str = ""

    @classmethod
    def of(
        cls,
        *,
        trajectories: Sequence[AgentTrajectory] = (),
        evaluations: Sequence[EvaluationResult] = (),
        feedback: Sequence[HumanFeedback] = (),
        ratings: Sequence[AIRating] = (),
        rewards: Sequence[RewardResult] = (),
        rollouts: Sequence[Rollout] = (),
        examples: Sequence[RewardExample] = (),
        source_datasets: Sequence[str] = (),
        source_data_version: str = "",
    ) -> RewardDatasetRequest:
        return cls(
            trajectories=tuple(trajectories),
            evaluations=tuple(evaluations),
            feedback=tuple(feedback),
            ratings=tuple(ratings),
            rewards=tuple(rewards),
            rollouts=tuple(rollouts),
            examples=tuple(examples),
            source_datasets=tuple(source_datasets),
            source_data_version=source_data_version,
        )


#: Reasons that make a row unusable rather than merely unproven. A person can
#: overturn a hold; these are rule violations, and the row says so.
REJECTING_REASONS: frozenset[str] = frozenset(
    {
        REASON_SOURCE_NOT_ALLOWED,
        REASON_REWARD_OUT_OF_RANGE,
        REASON_INTEGRITY_INVALID,
        REASON_NOT_VERIFIED,
        REASON_HUMAN_REQUIRED,
        REASON_AI_REQUIRED,
        REASON_MODE_MISMATCH,
    }
)


def _audit_row(
    example: RewardExample, rules: RewardDatasetRules, policy: RewardPolicyConfig
) -> RewardExample:
    """Classify one row: accepted, rejected for a stated rule, or held for review."""
    reasons: list[str] = []
    source = example.reward_source or RewardSource.RULE.value
    if rules.sources and source not in rules.sources:
        reasons.append(REASON_SOURCE_NOT_ALLOWED)
    if not policy.allows(source):
        reasons.append(REASON_SOURCE_NOT_ALLOWED)
    total = example.reward.total_reward
    if rules.min_reward is not None and total < rules.min_reward:
        reasons.append(REASON_REWARD_OUT_OF_RANGE)
    if rules.max_reward is not None and total > rules.max_reward:
        reasons.append(REASON_REWARD_OUT_OF_RANGE)
    integrity = example.integrity
    if integrity.status == RewardIntegrityStatus.INVALID.value and rules.require_integrity:
        reasons.append(REASON_INTEGRITY_INVALID)
    elif integrity.flagged and rules.hold_suspicious:
        reasons.append(REASON_INTEGRITY_FLAGGED)
    if rules.require_evidence and not example.reward.evidence:
        reasons.append(REASON_NO_EVIDENCE)
    if rules.require_verified and not _has_verification(example):
        reasons.append(REASON_NOT_VERIFIED)
    # A row satisfies "this dataset contains human feedback" when the signal
    # COMES from a person — directly, or through a composite that includes their
    # feedback. Requiring the composite to be the human source would reject the
    # very rows a combined reward is made of.
    if rules.require_human and not _has_human(example):
        reasons.append(REASON_HUMAN_REQUIRED)
    if rules.require_ai and not _has_ai(example):
        reasons.append(REASON_AI_REQUIRED)
    if not _mode_satisfies(rules.mode, example.mode):
        reasons.append(REASON_MODE_MISMATCH)
    if rules.require_integrity and not integrity.trusted and not reasons:
        reasons.append(REASON_INTEGRITY_FLAGGED)
    if any(code in REJECTING_REASONS for code in reasons):
        status = FEEDBACK_REJECTED
    elif reasons:
        status = FEEDBACK_NEEDS_REVIEW
    else:
        status = FEEDBACK_ACCEPTED
    return replace(example, status=status, reasons=tuple(dict.fromkeys(reasons)))


class RewardDatasetBuilder:
    """Assembles, audits and splits reward-labelled examples."""

    def __init__(
        self,
        *,
        policy: RewardPolicyConfig | None = None,
        checker: RewardIntegrityChecker | None = None,
        provider: RewardProvider | None = None,
        feedback_filter: FeedbackQualityFilter | None = None,
    ) -> None:
        self.policy = policy if policy is not None else RewardPolicyConfig()
        self.checker = (
            checker if checker is not None else RewardIntegrityChecker(self.policy)
        )
        self.provider = provider
        self.feedback_filter = (
            feedback_filter
            if feedback_filter is not None
            else FeedbackQualityFilter(policy=self.policy)
        )
        self._splitter = SFTDatasetBuilder()

    # -- building ---------------------------------------------------------------

    def build(
        self,
        name: str,
        request: RewardDatasetRequest | None = None,
        *,
        version: str = "",
        description: str = "",
        rules: RewardDatasetRules | None = None,
        split: SplitConfig | None = None,
        tags: Sequence[str] = (),
        existing_versions: Sequence[str] = (),
    ) -> RewardDatasetVersion:
        """Build and return an immutable reward dataset version (does not store)."""
        chosen = rules if rules is not None else RewardDatasetRules()
        problems = chosen.validate()
        if problems:
            raise ValueError("invalid reward dataset rules: " + "; ".join(problems))
        material = request if request is not None else RewardDatasetRequest()
        dataset_name = _text(name)
        if not dataset_name:
            raise ValueError("a reward dataset needs a name")
        resolved_split = split if split is not None else SplitConfig()
        resolved_version = _text(version) or _next_version(existing_versions)

        skipped: dict[str, int] = {}

        def skip(code: str) -> None:
            skipped[code] = skipped.get(code, 0) + 1

        examples = list(material.examples)
        for trajectory in material.trajectories:
            example = self._from_trajectory(trajectory, material, chosen, skip)
            if example is not None:
                examples.append(example)
        rows: list[RewardExample] = []
        duplicates = 0
        seen: dict[str, str] = {}
        for example in examples:
            if chosen.deduplicate:
                fingerprint = example.fingerprint()
                if fingerprint in seen:
                    duplicates += 1
                    skip(REASON_DUPLICATE)
                    continue
                seen[fingerprint] = example.example_id
            rows.append(_audit_row(example, chosen, self.policy))
        rows = self._resolve_contradictions(rows, chosen, skip)
        if chosen.max_examples and len(rows) > chosen.max_examples:
            rows = sorted(rows, key=lambda item: (not item.accepted, item.example_id))[
                : chosen.max_examples
            ]
        accepted = [row for row in rows if row.accepted]
        splits = self._split(accepted, resolved_split, chosen)
        statistics = self._statistics(rows, skipped, splits, chosen)
        sources: dict[str, int] = {}
        for row in rows:
            source_name = row.reward_source or "unknown"
            sources[source_name] = sources.get(source_name, 0) + 1
        dataset = RewardDatasetVersion(
            dataset_version_id=f"{dataset_name}@{resolved_version}",
            name=dataset_name,
            version=resolved_version,
            description=description,
            mode=chosen.mode,
            examples=tuple(rows),
            splits=splits,
            selection=chosen.to_mapping(),
            split_config={
                "train_ratio": resolved_split.train_ratio,
                "validation_ratio": resolved_split.validation_ratio,
                "test_ratio": resolved_split.test_ratio,
                "seed": resolved_split.seed,
                "group_by": chosen.group_by or resolved_split.group_by,
            },
            statistics=statistics,
            sources=sources,
            source_datasets=tuple(material.source_datasets),
            reward_version=self.policy.version,
            reward_policy=self.policy.to_mapping(),
            source_data_version=material.source_data_version,
            preprocessing_version=RLHF_VERSION,
            tags=tuple(tags),
        )
        return dataset

    # -- assembling one example --------------------------------------------------

    def _from_trajectory(
        self,
        trajectory: AgentTrajectory,
        material: RewardDatasetRequest,
        rules: RewardDatasetRules,
        skip: Any,
    ) -> RewardExample | None:
        if not trajectory.trajectory_id:
            skip(REASON_MISSING_TARGET)
            return None
        evaluations = {item.trajectory_id: item for item in material.evaluations}
        rewards = {item.trajectory_id: item for item in material.rewards}
        rollouts = {item.trajectory_id: item for item in material.rollouts}
        feedback = tuple(
            row for row in material.feedback if row.describes(trajectory.trajectory_id)
        )
        ratings = tuple(
            row for row in material.ratings if row.trajectory_id == trajectory.trajectory_id
        )
        rollout = rollouts.get(trajectory.trajectory_id)
        reward = rewards.get(trajectory.trajectory_id)
        if reward is None and self.provider is not None:
            request = RewardRequest.for_trajectory(
                trajectory,
                evaluation=evaluations.get(trajectory.trajectory_id),
                feedback=feedback,
                ratings=ratings,
                rollout=rollout,
            )
            try:
                reward = self.provider.evaluate(request)
            except RewardProviderUnavailable:
                reward = None
        if reward is None and rules.require_reward:
            skip(REASON_NO_REWARD)
            return None
        facts = outcome_facts(trajectory)
        context = IntegrityContext.from_trajectory(
            trajectory, sources=_source_readings(reward, feedback, ratings)
        )
        if reward is None:
            reward = RewardResult(trajectory_id=trajectory.trajectory_id)
        integrity = self.checker.check(reward, context)
        kinds = {row.feedback_type for row in feedback}
        return RewardExample(
            trajectory_id=trajectory.trajectory_id,
            task_id=trajectory.task_id,
            mode=_mode_for(feedback, ratings),
            task=dict(trajectory.final_result) or {"request": trajectory.user_request},
            candidate=dict(trajectory.final_result),
            reward=reward,
            reward_source=reward.reward_source or RewardSource.RULE.value,
            integrity=integrity,
            feedback_ids=tuple(row.feedback_id for row in feedback),
            rating_ids=tuple(row.rating_id for row in ratings),
            rollout_id=rollout.rollout_id if rollout is not None else "",
            difficulty=_difficulty(facts),
            tags=(),
            metadata={
                "kinds": sorted(kinds),
                "verification_total": int(facts.get("verification_total") or 0),
                "task_succeeded": facts.get("task_succeeded"),
            },
        )

    # -- auditing ----------------------------------------------------------------

    def _audit(self, example: RewardExample, rules: RewardDatasetRules) -> RewardExample:
        """Classify one row: accepted, rejected for a stated rule, or held."""
        return _audit_row(example, rules, self.policy)

    def _resolve_contradictions(
        self,
        rows: Sequence[RewardExample],
        rules: RewardDatasetRules,
        skip: Any,
    ) -> list[RewardExample]:
        """Two rewards for the same subject that point opposite ways are held."""
        by_subject: dict[str, list[RewardExample]] = {}
        for row in rows:
            key = row.trajectory_id or row.task_id or row.example_id
            by_subject.setdefault(key, []).append(row)
        produced: list[RewardExample] = []
        for key, group in by_subject.items():
            del key
            signals = [row.reward.total_reward for row in group]
            contradictory = any(value > 0 for value in signals) and any(
                value < 0 for value in signals
            )
            for row in group:
                if contradictory and row.accepted:
                    produced.append(
                        replace(
                            row,
                            status=FEEDBACK_NEEDS_REVIEW,
                            reasons=tuple(dict.fromkeys((*row.reasons, REASON_CONTRADICTORY))),
                        )
                    )
                    skip(REASON_CONTRADICTORY)
                else:
                    produced.append(row)
        return produced

    # -- splitting ---------------------------------------------------------------

    def _split(
        self,
        accepted: Sequence[RewardExample],
        config: SplitConfig,
        rules: RewardDatasetRules,
    ) -> dict[str, tuple[str, ...]]:
        """Assign whole tasks to splits by reusing Phase 16's splitter."""
        if not accepted:
            return dict.fromkeys(SPLIT_NAMES, ())
        effective = replace(config, group_by=rules.group_by)
        projected = [row.as_sft_example() for row in accepted]
        assignment = self._splitter.split_examples(projected, effective)
        produced: dict[str, tuple[str, ...]] = dict.fromkeys(SPLIT_NAMES, ())
        for name, ids in assignment.items():
            if name in produced:
                produced[name] = tuple(ids)
        return produced

    # -- statistics and validation ----------------------------------------------

    def _statistics(
        self,
        rows: Sequence[RewardExample],
        skipped: Mapping[str, int],
        splits: Mapping[str, tuple[str, ...]],
        rules: RewardDatasetRules,
    ) -> RewardDatasetStatistics:
        totals = [row.reward.total_reward for row in rows]
        by_source: dict[str, int] = {}
        by_mode: dict[str, int] = {}
        by_integrity: dict[str, int] = {}
        for row in rows:
            by_source[row.reward_source or "unknown"] = (
                by_source.get(row.reward_source or "unknown", 0) + 1
            )
            if row.mode:
                by_mode[row.mode] = by_mode.get(row.mode, 0) + 1
            by_integrity[row.integrity.status] = by_integrity.get(row.integrity.status, 0) + 1
        return RewardDatasetStatistics(
            total=len(rows),
            accepted=sum(1 for row in rows if row.status == FEEDBACK_ACCEPTED),
            rejected=sum(1 for row in rows if row.status == FEEDBACK_REJECTED),
            needs_review=sum(1 for row in rows if row.status == FEEDBACK_NEEDS_REVIEW),
            trusted=sum(1 for row in rows if row.trusted),
            flagged=sum(1 for row in rows if row.integrity.flagged),
            by_source=by_source,
            by_mode=by_mode,
            by_integrity=by_integrity,
            average_reward=_mean(totals),
            reward_std=_std(totals),
            duplicates_removed=skipped.get(REASON_DUPLICATE, 0),
            sensitive_removed=0,
            malformed_removed=skipped.get(REASON_MALFORMED, 0),
            groups=len({row.group_key() for row in rows}),
            source_trajectories=len({row.trajectory_id for row in rows if row.trajectory_id}),
            estimated_tokens=sum(rules.max_tokens_per_example for _ in rows),
            by_split={name: len(ids) for name, ids in splits.items()},
            skipped=dict(skipped),
        )

    def validate(self, dataset: RewardDatasetVersion | None) -> tuple[str, ...]:
        """Structural problems with a built version, as a tuple of messages."""
        if dataset is None:
            return ("no dataset was supplied",)
        issues: list[str] = []
        if not dataset.name or not dataset.version:
            issues.append("name and version are required")
        if dataset.dataset_version_id != f"{dataset.name}@{dataset.version}":
            issues.append("dataset_version_id does not match name@version")
        if not dataset.examples:
            issues.append("the dataset has no examples")
        if dataset.mode not in {*MODES, "mixed"}:
            issues.append(f"unknown mode {dataset.mode!r}")
        ids = [row.example_id for row in dataset.examples]
        if len(set(ids)) != len(ids):
            issues.append("example ids are not unique")
        for name in dataset.splits:
            if name not in SPLIT_NAMES:
                issues.append(f"unknown split {name!r}")
        placed: dict[str, str] = {}
        for name, split_ids in dataset.splits.items():
            if not split_ids:
                continue
            for example_id in split_ids:
                if example_id in placed:
                    issues.append(
                        f"example {example_id} appears in both {placed[example_id]} and {name}"
                    )
                placed[example_id] = name
        by_id = {row.example_id: row for row in dataset.examples}
        for example_id in placed:
            if example_id not in by_id:
                issues.append(f"split {placed[example_id]} names an unknown example {example_id}")
        for row in dataset.examples:
            if not row.accepted:
                continue
            if not row.task and not row.candidate:
                issues.append(f"accepted example {row.example_id} has no task or candidate")
            if not row.integrity.status:
                issues.append(f"accepted example {row.example_id} carries no integrity status")
            if (
                row.reward.trajectory_id
                and row.trajectory_id
                and row.reward.trajectory_id != row.trajectory_id
            ):
                issues.append(
                    f"example {row.example_id} references trajectory {row.trajectory_id} "
                    f"but its reward references {row.reward.trajectory_id}"
                )
            if row.kind not in REWARD_EXAMPLE_KINDS:
                issues.append(f"example {row.example_id} has unknown kind {row.kind!r}")
            violations = _reasoning_violations(row.candidate) + _reasoning_violations(row.task)
            if violations:
                issues.append(
                    f"example {row.example_id} contains hidden reasoning key(s): "
                    + ", ".join(sorted(set(violations)))
                )
            if (
                row.status == FEEDBACK_ACCEPTED
                and self.policy.require_integrity
                and not row.integrity.trusted
            ):
                issues.append(
                    f"accepted example {row.example_id} carries an untrusted reward "
                    f"({row.integrity.status})"
                )
        for name, split_ids in dataset.splits.items():
            for example_id in split_ids:
                found_row = by_id.get(example_id)
                if found_row is not None and not found_row.accepted:
                    issues.append(f"a non-accepted example {example_id} is in the {name} split")
        # Leakage: a task must not appear in two different splits.
        known_split: dict[str, str] = {}
        for name, split_ids in dataset.splits.items():
            for example_id in split_ids:
                found_row = by_id.get(example_id)
                if found_row is None:
                    continue
                key = found_row.group_key()
                seen = known_split.get(key)
                if seen is not None and seen != name:
                    issues.append(
                        f"task {key} leaks across {seen} and {name}"
                    )
                known_split[key] = name
        return tuple(issues)

    # -- convenience -------------------------------------------------------------

    def validate_reasoning_free(self, dataset: RewardDatasetVersion) -> tuple[str, ...]:
        """Only the hidden-reasoning check, for a caller that wants just that."""
        issues: list[str] = []
        for row in dataset.examples:
            for key in _reasoning_violations(row.candidate) + _reasoning_violations(row.task):
                issues.append(f"{row.example_id}: {key}")
        return tuple(issues)

    @staticmethod
    def describe(dataset: RewardDatasetVersion) -> dict[str, Any]:
        """The summary an API or a report shows for a built version."""
        return {
            "dataset_version": dataset.dataset_version_id,
            "name": dataset.name,
            "version": dataset.version,
            "mode": dataset.mode,
            "examples": len(dataset),
            "accepted": len(dataset.accepted_examples()),
            "trusted": len(dataset.trusted_examples()),
            "splits": {name: len(ids) for name, ids in dataset.splits.items() if ids},
            "sources": dict(dataset.sources),
            "statistics": dataset.statistics.to_dict(),
            "reward_version": dataset.reward_version,
            "source_datasets": list(dataset.source_datasets),
            "fingerprint": dataset.fingerprint(),
        }


def _next_version(existing: Sequence[str]) -> str:
    """The next version name, counting only well-formed ``vN`` predecessors."""
    highest = 0
    for item in existing:
        text = _text(item)
        if text.startswith("v") and text[1:].isdigit():
            highest = max(highest, int(text[1:]))
    return f"v{highest + 1}"


def _mode_for(
    feedback: Sequence[HumanFeedback], ratings: Sequence[AIRating]
) -> str:
    has_human = any(row.typed for row in feedback)
    has_ai = any(row.evaluator_id for row in ratings)
    if has_human and has_ai:
        return MODE_MIXED
    if has_human:
        return "rlhf"
    if has_ai:
        return "rlaif"
    return ""


def _source_readings(
    reward: RewardResult | None,
    feedback: Sequence[HumanFeedback],
    ratings: Sequence[AIRating],
) -> dict[str, float | None]:
    """What each OTHER source said about the same subject, on -1..1."""
    from novacontrol.rlhf.integrity import _reading

    readings: dict[str, float | None] = {}
    if feedback:
        readings[RewardSource.HUMAN.value] = _reading(feedback)
    if ratings:
        readings[RewardSource.AI.value] = _reading(ratings)
    if reward is not None and reward.reward_source:
        readings[reward.reward_source] = _reading(reward)
    return readings


def _mode_satisfies(dataset_mode: str, row_mode: str) -> bool:
    """Whether a row carrying ``row_mode``'s signals belongs in ``dataset_mode``.

    A dataset's mode declares which signal it must CONTAIN, not which label every
    row wears: ``mixed`` at the dataset level asks for no particular source, and a
    ``mixed`` row (a person and an evaluator both spoke) carries the signal either
    mode is looking for. A row with no signal of its own (``""``) is not a
    mismatch here — the per-source requirements refuse it when the dataset
    demands a source it does not have.
    """
    if not dataset_mode or not row_mode:
        return True
    if dataset_mode == MODE_MIXED or row_mode == MODE_MIXED:
        return True
    return dataset_mode == row_mode


def _has_verification(example: RewardExample) -> bool:
    value = example.metadata.get("verification_total")
    return bool(value)


def _has_human(row: RewardExample) -> bool:
    """Whether a person's feedback is part of this row's signal."""
    return row.reward_source == RewardSource.HUMAN.value or bool(row.feedback_ids)


def _has_ai(row: RewardExample) -> bool:
    """Whether an evaluator's rating is part of this row's signal."""
    return row.reward_source == RewardSource.AI.value or bool(row.rating_ids)


def _difficulty(facts: Mapping[str, Any]) -> str:
    steps = int(facts.get("plan_steps") or 0) + int(facts.get("tool_calls") or 0)
    if steps >= 6:
        return "complex"
    if steps >= 3:
        return "moderate"
    return "simple"


def _reasoning_violations(value: Mapping[str, Any], *, depth: int = 0) -> list[str]:
    """Keys anywhere in a structure that name hidden reasoning."""
    if depth > 8:
        return []
    found: list[str] = []
    for key, item in value.items():
        name = str(key).lower()
        if name in REASONING_KEYS:
            found.append(name)
        if isinstance(item, Mapping):
            found.extend(_reasoning_violations(item, depth=depth + 1))
        elif isinstance(item, (list, tuple)):
            for entry in item:
                if isinstance(entry, Mapping):
                    found.extend(_reasoning_violations(entry, depth=depth + 1))
    return found


def reward_dataset_fingerprint(dataset: RewardDatasetVersion) -> str:
    """A deterministic content hash for a dataset (fewer moving parts than the method)."""
    payload = json.dumps(
        {
            "name": dataset.name,
            "version": dataset.version,
            "mode": dataset.mode,
            "examples": sorted(row.fingerprint() for row in dataset.examples),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


__all__ = [
    "DEFAULT_TOKENS_PER_EXAMPLE",
    "REASON_AI_REQUIRED",
    "REASON_CONTRADICTORY",
    "REASON_DUPLICATE",
    "REASON_HUMAN_REQUIRED",
    "REASON_INTEGRITY_FLAGGED",
    "REASON_INTEGRITY_INVALID",
    "REASON_MALFORMED",
    "REASON_MISSING_TARGET",
    "REASON_MODE_MISMATCH",
    "REASON_NO_EVIDENCE",
    "REASON_NO_REWARD",
    "REASON_NOT_VERIFIED",
    "REASON_REWARD_OUT_OF_RANGE",
    "REASON_SOURCE_NOT_ALLOWED",
    "REASONING_KEYS",
    "RewardDatasetBuilder",
    "RewardDatasetRequest",
    "RewardDatasetRules",
    "SPLIT_NAMES",
    "reward_dataset_fingerprint",
]
