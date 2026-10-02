"""Phase 17 schema: preference examples, their strength, and preference datasets.

A preference example is a PAIR of observable behaviours for one prompt — what
NovaControl chose and what it could have chosen — together with the evidence that
says which one was better. Nothing here is a thought: ``chosen`` and ``rejected``
hold structured outputs (an intent, a decision, a tool call with its arguments, a
plan, a recovery decision, a final response), and the outcomes and evaluations
that decided the pair are structured readings from Phase 15, never narration.

The provenance rules are the point of this module rather than a detail:

  * every pair names WHERE the preference came from (:class:`PreferenceSource`),
  * a pair carries its strength on THREE axes — ``confidence``,
    ``evidence_quality`` and ``verification_strength`` — instead of one
    unexplained number, and the list of evidence it was derived from,
  * a pair says whether its orientation was observed or inferred, and a
    trainer can therefore weight a human's explicit choice above a synthetic
    one without either being silently promoted.

The dataset part mirrors Phase 16 deliberately: an immutable ``name@version``,
deterministic group-safe splits, statistics that explain what was kept and what
was refused, and a ``validate()`` that returns every reason a version must not be
trained on. Reusing that shape is what lets the preference trainer, the
checkpoint manager, the registry and the evaluation gate be shared rather than
rebuilt.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any
from uuid import uuid4

from novacontrol.evaluation.models import now_iso
from novacontrol.training.models import (
    PREPROCESSING_VERSION as SFT_PREPROCESSING_VERSION,
)
from novacontrol.training.models import (
    SPLIT_NAMES,
    DatasetStatistics,
    SFTDatasetVersion,
    SFTTrainingExample,
)

PREFERENCE_SCHEMA_VERSION = 1
PREFERENCE_DATASET_SCHEMA_VERSION = 1

#: The builder's own version. A pair built by a later preprocessing rule is a
#: different pair, and a dataset must be able to say which rules made it.
PREFERENCE_PREPROCESSING_VERSION = "phase17.1"

#: The splits a preference dataset is divided into. Reused from Phase 16 rather
#: than re-declared: a leak-free split means the same thing in both phases.
PAIR_SPLIT_NAMES = SPLIT_NAMES

#: The evidence kinds a pair may cite. Plain strings rather than an enum because
#: a deployment may add its own reviewer channel without a schema change, and the
#: readings that matter (verified or not) are carried by the evidence itself.
EVIDENCE_EXPLICIT_USER_PREFERENCE = "explicit_user_preference"
EVIDENCE_HUMAN_REVIEW = "human_review"
EVIDENCE_VERIFIED_SUCCESS = "verified_success"
EVIDENCE_EVALUATION_DIMENSIONS = "evaluation_dimensions"
EVIDENCE_BENCHMARK_EXPECTATION = "benchmark_expectation"
EVIDENCE_TEACHER_AGREEMENT = "teacher_model_agreement"
EVIDENCE_SYNTHETIC_RULE = "synthetic_rule"


class PreferenceSource(StrEnum):
    """Where a preference pair came from.

    The order is the order of trust, and it is not decorative: ``verified`` is
    what a curriculum or a weight may rely on, and a teacher or a synthetic rule
    is marked unverified so a downstream reader can hold it to a lower standard
    without having to re-derive where it came from.
    """

    EXPLICIT_USER_FEEDBACK = "explicit_user_feedback"
    HUMAN_REVIEW = "human_review"
    VERIFIED_OUTCOME = "verified_outcome"
    EVALUATION = "evaluation"
    BENCHMARK = "benchmark"
    TEACHER_MODEL = "teacher_model"
    SYNTHETIC = "synthetic"

    @property
    def verified(self) -> bool:
        """Whether the preference rests on something observed to have happened."""
        return self not in {
            PreferenceSource.TEACHER_MODEL,
            PreferenceSource.SYNTHETIC,
        }


#: The trust ordering as numbers, so an operator can compare or filter without
#: reading the enum. Higher is stronger; a trainer may weight by it.
SOURCE_STRENGTH: Mapping[str, float] = {
    PreferenceSource.HUMAN_REVIEW.value: 1.0,
    PreferenceSource.EXPLICIT_USER_FEEDBACK.value: 0.95,
    PreferenceSource.VERIFIED_OUTCOME.value: 0.9,
    PreferenceSource.EVALUATION.value: 0.75,
    PreferenceSource.BENCHMARK.value: 0.7,
    PreferenceSource.TEACHER_MODEL.value: 0.5,
    PreferenceSource.SYNTHETIC.value: 0.3,
}


class PreferenceDatasetType(StrEnum):
    """A preference family: one prompt shape and one pair of outputs to compare.

    ``RESPONSE`` is the family a final answer belongs to. A developer or research
    trajectory's observable result is a response, so it lands here rather than in
    a seventh vocabulary.
    """

    NLU = "nlu"
    DECISION = "decision"
    TOOL_SELECTION = "tool_selection"
    PLANNING = "planning"
    RECOVERY = "recovery"
    RESPONSE = "response"


class PreferenceAlgorithm(StrEnum):
    """The preference-optimization objective. The trainer is chosen from this."""

    DPO = "dpo"
    ORPO = "orpo"


class PreferenceQualityStatus(StrEnum):
    """The preference filter's answer about one pair (Phase 15's vocabulary)."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


class PreferenceReviewDecision(StrEnum):
    """What a reviewer may decide about a pair awaiting review."""

    CHOOSE_A = "choose_a"
    CHOOSE_B = "choose_b"
    TIE = "tie"
    REJECT = "reject"


# ── small parsers (each field is read defensively; a store must load) ────────


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mappings(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def _texts(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _whole(value: Any) -> int | None:
    parsed = _number(value)
    return None if parsed is None else int(parsed)


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _unit(value: Any, default: float = 0.0) -> float:
    """A reading in [0, 1]; anything else keeps the default rather than clipping."""
    parsed = _number(value)
    if parsed is None or not 0.0 <= parsed <= 1.0:
        return default
    return round(parsed, 6)


def preference_dataset_version_id(name: str, version: str) -> str:
    """The ``name@version`` string a run stores and a reader resolves."""
    return f"{str(name).strip()}@{str(version).strip()}"


def next_preference_version(existing: Sequence[str], requested: str = "") -> str:
    """The version a new build takes: the requested one, or the next patch.

    Same rule as Phase 16's ``next_version`` — a version is a claim about
    content, so a build that would collide with an existing one moves forward
    instead of overwriting it.
    """
    wanted = _text(requested)
    if wanted:
        return wanted
    highest: tuple[int, int, int] | None = None
    for item in existing:
        parts = _text(item).split(".")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            continue
        numbers = (int(parts[0]), int(parts[1]), int(parts[2]))
        if highest is None or numbers > highest:
            highest = numbers
    if highest is None:
        return "1.0.0"
    return f"{highest[0]}.{highest[1]}.{highest[2] + 1}"


# ── preference strength ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PreferenceEvidence:
    """One thing that says which candidate was better.

    ``verified`` distinguishes "we watched it work" from "a model suggested it".
    It is carried per item rather than inferred from the source, because a single
    pair can rest on a user's choice AND on a benchmark expectation, and only one
    of those is a measurement.
    """

    kind: str = EVIDENCE_SYNTHETIC_RULE
    strength: float = 0.0
    detail: str = ""
    source: str = ""
    verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "strength": self.strength,
            "detail": self.detail,
            "source": self.source,
            "verified": self.verified,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PreferenceEvidence:
        return cls(
            kind=_text(data.get("kind"), EVIDENCE_SYNTHETIC_RULE),
            strength=_unit(data.get("strength")),
            detail=_text(data.get("detail")),
            source=_text(data.get("source")),
            verified=bool(_flag(data.get("verified")) or False),
        )


@dataclass(frozen=True, slots=True)
class PreferenceStrength:
    """How much this pair is worth, on three axes that are not interchangeable.

    * ``confidence`` — how sure the extractor is that the orientation is right.
    * ``evidence_quality`` — how strong the evidence itself is (an explicit human
      choice outranks an evaluation margin).
    * ``verification_strength`` — whether the preferred candidate has an
      OBSERVED, verified outcome behind it.

    ``overall`` is a documented weighted mean of the three, kept for ranking and
    reporting; the axes stay separate so a curriculum can say "I will accept low
    evidence only where verification is strong" without re-deriving anything.
    """

    confidence: float = 0.0
    evidence_quality: float = 0.0
    verification_strength: float = 0.0
    evidence: tuple[PreferenceEvidence, ...] = ()
    source: str = PreferenceSource.SYNTHETIC.value

    @property
    def overall(self) -> float:
        """The single number a report shows, with its inputs still available."""
        weighted = (
            0.4 * self.confidence
            + 0.35 * self.evidence_quality
            + 0.25 * self.verification_strength
        )
        return round(weighted, 6)

    @property
    def verified(self) -> bool:
        return any(item.verified for item in self.evidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "confidence": self.confidence,
            "evidence_quality": self.evidence_quality,
            "verification_strength": self.verification_strength,
            "overall": self.overall,
            "verified": self.verified,
            "source": self.source,
            "evidence": [item.to_dict() for item in self.evidence],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PreferenceStrength:
        return cls(
            confidence=_unit(data.get("confidence")),
            evidence_quality=_unit(data.get("evidence_quality")),
            verification_strength=_unit(data.get("verification_strength")),
            evidence=tuple(
                PreferenceEvidence.from_dict(item) for item in _mappings(data.get("evidence"))
            ),
            source=_text(data.get("source"), PreferenceSource.SYNTHETIC.value),
        )


# ── the preference example ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PreferenceExample:
    """One preference pair: a prompt and two observed candidate behaviours.

    ``chosen`` is the behaviour the evidence preferred and ``rejected`` the one it
    did not. Both are structured mappings — an intent, a decision, a tool call, a
    plan, a recovery decision, or a response — and both are things that actually
    happened (or, for a benchmark expectation, a fixture that outranks the run).

    ``review`` records the answer a human gave when the pair was presented
    (``decision``, ``reviewer``, ``reason``, ``at``); ``quality`` records the
    filter's verdict and its structured reasons. Neither is training data: they
    are the paper trail that says why this pair is trusted.
    """

    preference_id: str = field(default_factory=lambda: uuid4().hex)
    dataset_type: str = PreferenceDatasetType.NLU.value
    prompt: Mapping[str, Any] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)
    chosen: Mapping[str, Any] = field(default_factory=dict)
    rejected: Mapping[str, Any] = field(default_factory=dict)
    chosen_outcome: Mapping[str, Any] = field(default_factory=dict)
    rejected_outcome: Mapping[str, Any] = field(default_factory=dict)
    chosen_evaluation: Mapping[str, Any] = field(default_factory=dict)
    rejected_evaluation: Mapping[str, Any] = field(default_factory=dict)
    preference_source: str = PreferenceSource.SYNTHETIC.value
    confidence: float = 0.0
    strength: PreferenceStrength = field(default_factory=PreferenceStrength)
    source_trajectory_ids: tuple[str, ...] = ()
    source_evaluation_ids: tuple[str, ...] = ()
    difficulty: str = "simple"
    tags: tuple[str, ...] = ()
    quality: Mapping[str, Any] = field(default_factory=dict)
    review: Mapping[str, Any] = field(default_factory=dict)
    dataset_version: str = ""
    created_at: str = field(default_factory=now_iso)
    schema_version: int = PREFERENCE_SCHEMA_VERSION

    # -- reading ---------------------------------------------------------------

    @property
    def group_key(self) -> str:
        """The unit that must not be split across train and test.

        Two candidates for the same prompt are one group: testing a model on the
        rejected half of a pair it trained on the chosen half of measures nothing.
        """
        named = _text(self.prompt.get("group_key"))
        if named:
            return named
        if self.source_trajectory_ids:
            return "|".join(sorted(self.source_trajectory_ids))
        return self.preference_id

    @property
    def quality_status(self) -> str:
        return _text(self.quality.get("status"))

    @property
    def accepted(self) -> bool:
        return self.quality_status == PreferenceQualityStatus.ACCEPTED.value

    @property
    def needs_review(self) -> bool:
        return self.quality_status == PreferenceQualityStatus.NEEDS_REVIEW.value

    @property
    def reviewed(self) -> bool:
        return bool(_text(self.review.get("decision")))

    def fingerprint(self) -> str:
        """A stable identity, ORDER-INSENSITIVE in the two candidates.

        (A, B) and (B, A) are the same pair seen from two sides, and a dataset
        that held both would contain its own contradiction. They share a
        fingerprint so the builder can catch the reversal and say so.
        """
        sides = sorted(
            json.dumps(side, sort_keys=True, ensure_ascii=False, default=str)
            for side in (dict(self.chosen), dict(self.rejected))
        )
        payload = json.dumps(
            {
                "type": self.dataset_type,
                "prompt": dict(self.prompt),
                "context": dict(self.context),
                "sides": sides,
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def as_sft_example(self, *, split: str = "") -> SFTTrainingExample:
        """This pair as one supervised example: the prompt, and the CHOSEN output.

        The three-model comparison (base vs SFT vs preference-optimized) is
        measured by the Phase 16 evaluator, and that evaluator scores structured
        outputs against a structured target. This projection is how a preference
        pair enters it without the evaluator learning a second schema: the
        rejected side is not discarded, it is simply not a target.
        """
        return SFTTrainingExample(
            example_id=self.sft_example_id,
            dataset_type=self.dataset_type,
            input=dict(self.prompt),
            context=dict(self.context),
            target=dict(self.chosen),
            metadata={
                "group_key": self.group_key,
                "preference_id": self.preference_id,
                "preference_source": self.preference_source,
                "split": split,
                "chosen_outcome": dict(self.chosen_outcome),
                "rejected_outcome": dict(self.rejected_outcome),
                "verification": _verification_reading(self.chosen_outcome),
            },
            tags=tuple(self.tags),
            difficulty=self.difficulty,
            quality_status=self.quality_status,
        )

    @property
    def sft_example_id(self) -> str:
        """The id this pair takes in the supervised projection."""
        return f"{self.preference_id}:chosen"

    @property
    def estimated_tokens(self) -> int:
        """A cheap size reading (serialised length / 4). Not a tokenizer.

        Both candidates count: a preference pair feeds a model the prompt AND
        the two outputs it must separate, so pricing only the prompt would
        understate the run by half.
        """
        try:
            payload = json.dumps(
                {
                    "prompt": dict(self.prompt),
                    "context": dict(self.context),
                    "chosen": dict(self.chosen),
                    "rejected": dict(self.rejected),
                },
                ensure_ascii=False,
                default=str,
            )
        except (TypeError, ValueError):
            return 0
        return max(1, len(payload) // 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "preference_id": self.preference_id,
            "dataset_type": self.dataset_type,
            "prompt": dict(self.prompt),
            "context": dict(self.context),
            "chosen": dict(self.chosen),
            "rejected": dict(self.rejected),
            "chosen_outcome": dict(self.chosen_outcome),
            "rejected_outcome": dict(self.rejected_outcome),
            "chosen_evaluation": dict(self.chosen_evaluation),
            "rejected_evaluation": dict(self.rejected_evaluation),
            "preference_source": self.preference_source,
            "confidence": self.confidence,
            "strength": self.strength.to_dict(),
            "source_trajectory_ids": list(self.source_trajectory_ids),
            "source_evaluation_ids": list(self.source_evaluation_ids),
            "difficulty": self.difficulty,
            "tags": list(self.tags),
            "quality": dict(self.quality),
            "review": dict(self.review),
            "dataset_version": self.dataset_version,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PreferenceExample:
        strength = data.get("strength")
        return cls(
            preference_id=_text(data.get("preference_id")) or uuid4().hex,
            dataset_type=_text(data.get("dataset_type"), PreferenceDatasetType.NLU.value),
            prompt=_mapping(data.get("prompt")),
            context=_mapping(data.get("context")),
            chosen=_mapping(data.get("chosen")),
            rejected=_mapping(data.get("rejected")),
            chosen_outcome=_mapping(data.get("chosen_outcome")),
            rejected_outcome=_mapping(data.get("rejected_outcome")),
            chosen_evaluation=_mapping(data.get("chosen_evaluation")),
            rejected_evaluation=_mapping(data.get("rejected_evaluation")),
            preference_source=_text(
                data.get("preference_source"), PreferenceSource.SYNTHETIC.value
            ),
            confidence=_unit(data.get("confidence")),
            strength=(
                PreferenceStrength.from_dict(strength)
                if isinstance(strength, Mapping)
                else PreferenceStrength()
            ),
            source_trajectory_ids=_texts(data.get("source_trajectory_ids")),
            source_evaluation_ids=_texts(data.get("source_evaluation_ids")),
            difficulty=_text(data.get("difficulty"), "simple"),
            tags=_texts(data.get("tags")),
            quality=_mapping(data.get("quality")),
            review=_mapping(data.get("review")),
            dataset_version=_text(data.get("dataset_version")),
            created_at=_text(data.get("created_at")) or now_iso(),
            schema_version=_whole(data.get("schema_version")) or PREFERENCE_SCHEMA_VERSION,
        )

    def with_quality(self, status: str, *, reasons: Sequence[str] = ()) -> PreferenceExample:
        return replace(
            self,
            quality={"status": status, "reasons": list(reasons), "checked_at": now_iso()},
        )

    def with_review(
        self,
        decision: PreferenceReviewDecision | str,
        *,
        reviewer: str = "",
        reason: str = "",
    ) -> PreferenceExample:
        """Apply a reviewer's decision: confirm, swap the sides, or drop the pair.

        ``CHOOSE_A`` keeps the pair as presented and ``CHOOSE_B`` swaps the two
        sides, because "A" is always the side currently held as ``chosen``. A tie
        or a rejection is recorded as REJECTED with the reason, so the pair stays
        explainable instead of quietly disappearing.
        """
        chosen = decision.value if isinstance(decision, PreferenceReviewDecision) else str(decision)
        entry = {
            "decision": chosen,
            "reviewer": _text(reviewer),
            "reason": _text(reason),
            "at": now_iso(),
        }
        settled = {
            "status": PreferenceQualityStatus.ACCEPTED.value,
            "reasons": [f"a reviewer decided {chosen}"],
            "checked_at": now_iso(),
        }
        if chosen == PreferenceReviewDecision.CHOOSE_A.value:
            return replace(self, review=entry, quality=settled)
        if chosen == PreferenceReviewDecision.CHOOSE_B.value:
            return replace(
                self,
                chosen=dict(self.rejected),
                rejected=dict(self.chosen),
                chosen_outcome=dict(self.rejected_outcome),
                rejected_outcome=dict(self.chosen_outcome),
                chosen_evaluation=dict(self.rejected_evaluation),
                rejected_evaluation=dict(self.chosen_evaluation),
                review=entry,
                quality=settled,
            )
        return replace(
            self,
            review=entry,
            quality={
                "status": PreferenceQualityStatus.REJECTED.value,
                "reasons": [f"a reviewer decided {chosen}"],
                "checked_at": now_iso(),
            },
        )


def _verification_reading(outcome: Mapping[str, Any]) -> dict[str, Any]:
    """The ``{records, failed}`` reading the Phase 16 evaluator understands."""
    data = _mapping(outcome)
    records = _whole(data.get("verification_records"))
    failed = _whole(data.get("verification_failed"))
    if records is None:
        return {}
    return {"records": max(0, records), "failed": max(0, failed or 0)}


# ── dataset statistics and the built version ─────────────────────────────────


@dataclass(frozen=True, slots=True)
class PreferenceRules:
    """Which observed rows may become preference pairs, and how strong they must be.

    These are the operator's lines rather than constants: a dataset built for NLU
    work may accept a thin benchmark expectation, and one built to nudge a
    production model may demand a verified outcome and an explicit human decision.
    The defaults are the cautious ones — a pair needs evidence, benchmark
    expectations are used (they are the project's own fixtures), and nothing is
    taken from a model's suggestion unless it is asked for.
    """

    #: Preference sources in scope. Empty means every source.
    preference_sources: tuple[str, ...] = ()
    #: Below this, the pair is not accepted into the dataset.
    min_confidence: float = 0.0
    #: The winner must beat the loser by this much on the evaluation score.
    min_score_margin: float = 0.05
    #: Accept only sources that rest on something observed (not teacher/synthetic).
    require_verified_source: bool = False
    #: Pair a run against Phase 15's golden expectations when it disagreed with one.
    use_benchmark_expectations: bool = True
    max_pairs: int = 0
    model: str = ""
    task_category: str = ""
    source: str = ""
    tags: tuple[str, ...] = ()
    since: str = ""
    until: str = ""

    def to_mapping(self) -> dict[str, Any]:
        return {
            "preference_sources": list(self.preference_sources),
            "min_confidence": self.min_confidence,
            "min_score_margin": self.min_score_margin,
            "require_verified_source": self.require_verified_source,
            "use_benchmark_expectations": self.use_benchmark_expectations,
            "max_pairs": self.max_pairs,
            "model": self.model,
            "task_category": self.task_category,
            "source": self.source,
            "tags": list(self.tags),
            "since": self.since,
            "until": self.until,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> PreferenceRules:
        defaults = cls()
        if not isinstance(data, Mapping):
            return defaults

        def flag(key: str, fallback: bool) -> bool:
            value = data.get(key)
            return value if isinstance(value, bool) else fallback

        margin = _number(data.get("min_score_margin"))
        confidence = _number(data.get("min_confidence"))
        return cls(
            preference_sources=_texts(data.get("preference_sources")),
            min_confidence=_unit(confidence if confidence is not None else defaults.min_confidence),
            min_score_margin=(
                margin if margin is not None else defaults.min_score_margin
            ),
            require_verified_source=flag(
                "require_verified_source", defaults.require_verified_source
            ),
            use_benchmark_expectations=flag(
                "use_benchmark_expectations", defaults.use_benchmark_expectations
            ),
            max_pairs=max(0, min(1_000_000, _whole(data.get("max_pairs")) or 0)),
            model=_text(data.get("model")),
            task_category=_text(data.get("task_category")),
            source=_text(data.get("source")),
            tags=_texts(data.get("tags")),
            since=_text(data.get("since")),
            until=_text(data.get("until")),
        )


@dataclass(frozen=True, slots=True)
class PreferenceStatistics:
    """What the build saw and what it kept, so a version explains itself."""

    total: int = 0
    by_difficulty: Mapping[str, int] = field(default_factory=dict)
    by_source: Mapping[str, int] = field(default_factory=dict)
    by_split: Mapping[str, int] = field(default_factory=dict)
    groups: int = 0
    source_trajectories: int = 0
    source_evaluations: int = 0
    verified_pairs: int = 0
    review_required: int = 0
    rejected: int = 0
    duplicates_removed: int = 0
    sensitive_removed: int = 0
    malformed_removed: int = 0
    contradictory_removed: int = 0
    average_strength: float = 0.0
    skipped: Mapping[str, int] = field(default_factory=dict)
    estimated_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "by_difficulty": dict(self.by_difficulty),
            "by_source": dict(self.by_source),
            "by_split": dict(self.by_split),
            "groups": self.groups,
            "source_trajectories": self.source_trajectories,
            "source_evaluations": self.source_evaluations,
            "verified_pairs": self.verified_pairs,
            "review_required": self.review_required,
            "rejected": self.rejected,
            "duplicates_removed": self.duplicates_removed,
            "sensitive_removed": self.sensitive_removed,
            "malformed_removed": self.malformed_removed,
            "contradictory_removed": self.contradictory_removed,
            "average_strength": self.average_strength,
            "skipped": dict(self.skipped),
            "estimated_tokens": self.estimated_tokens,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PreferenceStatistics:
        def counts(key: str) -> dict[str, int]:
            value = data.get(key)
            if not isinstance(value, Mapping):
                return {}
            return {
                str(name): int(item)
                for name, item in value.items()
                if isinstance(item, (int, float)) and not isinstance(item, bool)
            }

        return cls(
            total=max(0, _whole(data.get("total")) or 0),
            by_difficulty=counts("by_difficulty"),
            by_source=counts("by_source"),
            by_split=counts("by_split"),
            groups=max(0, _whole(data.get("groups")) or 0),
            source_trajectories=max(0, _whole(data.get("source_trajectories")) or 0),
            source_evaluations=max(0, _whole(data.get("source_evaluations")) or 0),
            verified_pairs=max(0, _whole(data.get("verified_pairs")) or 0),
            review_required=max(0, _whole(data.get("review_required")) or 0),
            rejected=max(0, _whole(data.get("rejected")) or 0),
            duplicates_removed=max(0, _whole(data.get("duplicates_removed")) or 0),
            sensitive_removed=max(0, _whole(data.get("sensitive_removed")) or 0),
            malformed_removed=max(0, _whole(data.get("malformed_removed")) or 0),
            contradictory_removed=max(0, _whole(data.get("contradictory_removed")) or 0),
            average_strength=_unit(data.get("average_strength")),
            skipped=counts("skipped"),
            estimated_tokens=max(0, _whole(data.get("estimated_tokens")) or 0),
        )


@dataclass(frozen=True, slots=True)
class PreferenceDatasetVersion:
    """One immutable version of a preference dataset.

    ``source_datasets`` names the Phase 15/16 datasets and stores the pairs came
    from, and ``preference_sources`` records how many pairs each source
    contributed. Both are required: a preference version that cannot say where it
    came from cannot be audited, and a trainer that cannot say whether it learned
    from a human or a rule cannot be trusted with either.
    """

    dataset_version_id: str
    name: str = ""
    version: str = ""
    dataset_type: str = PreferenceDatasetType.NLU.value
    description: str = ""
    examples: tuple[PreferenceExample, ...] = ()
    splits: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    rules: Mapping[str, Any] = field(default_factory=dict)
    split_config: Mapping[str, Any] = field(default_factory=dict)
    statistics: PreferenceStatistics = field(default_factory=PreferenceStatistics)
    source_datasets: tuple[str, ...] = ()
    preference_sources: Mapping[str, int] = field(default_factory=dict)
    source_data_version: str = ""
    preprocessing_version: str = PREFERENCE_PREPROCESSING_VERSION
    tags: tuple[str, ...] = ()
    created_at: str = field(default_factory=now_iso)
    schema_version: int = PREFERENCE_DATASET_SCHEMA_VERSION

    def __len__(self) -> int:
        return len(self.examples)

    def example(self, preference_id: str) -> PreferenceExample | None:
        wanted = _text(preference_id)
        for item in self.examples:
            if item.preference_id == wanted:
                return item
        return None

    def pair_ids(self, name: str) -> tuple[str, ...]:
        return tuple(self.splits.get(name, ()))

    def split(self, name: str, *, accepted_only: bool = True) -> tuple[PreferenceExample, ...]:
        """One split's pairs.

        ``accepted_only`` defaults to True because this is what CHANGES A MODEL:
        a pair the quality filter held for review is stored and counted so it can
        be settled, and it must not quietly become training data in the meantime.
        A report that wants to show everything asks for ``accepted_only=False``.
        """
        wanted = _text(name)
        by_id = {item.preference_id: item for item in self.examples}
        return tuple(
            pair
            for pair_id in self.splits.get(wanted, ())
            if (pair := by_id.get(pair_id)) is not None
            and (not accepted_only or pair.accepted)
        )

    def split_names(self) -> tuple[str, ...]:
        return tuple(name for name in PAIR_SPLIT_NAMES if self.splits.get(name))

    def accepted_pairs(self) -> tuple[PreferenceExample, ...]:
        """Every pair in this version that may train."""
        return tuple(item for item in self.examples if item.accepted)

    def fingerprint(self) -> str:
        """A CONTENT hash, so a re-build of the same version is recognisable.

        The splits are hashed by pair content rather than by ``preference_id``:
        ids are minted per build, so an id-based hash would change when nothing
        about the data changed — and the immutability rule (a version may not be
        rewritten with different pairs) could then never tell an identical rebuild
        from a contradictory one. The pair's quality verdict and review entry are
        hashed with it, because "which pairs may train" is part of what a version
        asserts.
        """

        def identity(item: PreferenceExample) -> str:
            # The verdict and the decision, but not the timestamps they were taken
            # at: re-checking a pair a second later must not look like new data.
            payload = json.dumps(
                {
                    "pair": item.fingerprint(),
                    "status": _text(item.quality.get("status")),
                    "reasons": sorted(_texts(item.quality.get("reasons"))),
                    "decision": _text(item.review.get("decision")),
                    "reviewer": _text(item.review.get("reviewer")),
                    "review_reason": _text(item.review.get("reason")),
                },
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

        by_id = {item.preference_id: identity(item) for item in self.examples}
        payload = json.dumps(
            {
                "name": self.name,
                "version": self.version,
                "type": self.dataset_type,
                "rules": dict(self.rules),
                "pairs": [identity(item) for item in self.examples],
                "splits": {
                    name: [by_id[pair_id] for pair_id in ids if pair_id in by_id]
                    for name, ids in self.splits.items()
                },
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    # -- the supervised projection ---------------------------------------------

    def as_sft_dataset(self) -> SFTDatasetVersion:
        """The preference dataset as a Phase 16 SFT dataset, for the comparison.

        The chosen side becomes the target and the pair keeps its split, so the
        existing evaluator, regression metrics and noise floors apply unchanged.
        The projection is derived, never stored: one authority for the pairs.
        """
        examples = tuple(
            item.as_sft_example(split=split)
            for split in PAIR_SPLIT_NAMES
            for item in self.split(split)
        )
        placed = {item.example_id for item in examples}
        for item in self.examples:
            if item.sft_example_id not in placed:
                examples = (*examples, item.as_sft_example())
        splits = {
            name: tuple(
                item.sft_example_id for item in self.split(name)
            )
            for name in PAIR_SPLIT_NAMES
        }
        return SFTDatasetVersion(
            dataset_version_id=self.dataset_version_id,
            name=self.name,
            version=self.version,
            dataset_type=self.dataset_type,
            description=self.description,
            examples=examples,
            splits=splits,
            selection=dict(self.rules),
            split_config=dict(self.split_config),
            statistics=DatasetStatistics(
                total=len(examples),
                by_split={name: len(ids) for name, ids in splits.items() if ids},
                groups=len({item.group_key for item in self.examples}),
                source_trajectories=self.statistics.source_trajectories,
                duplicates_removed=self.statistics.duplicates_removed,
                sensitive_removed=self.statistics.sensitive_removed,
                malformed_removed=self.statistics.malformed_removed,
                skipped=dict(self.statistics.skipped),
                estimated_tokens=self.statistics.estimated_tokens,
            ),
            source_data_version=self.source_data_version,
            preprocessing_version=SFT_PREPROCESSING_VERSION,
            tags=self.tags,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_version_id": self.dataset_version_id,
            "name": self.name,
            "version": self.version,
            "dataset_type": self.dataset_type,
            "description": self.description,
            "examples": [item.to_dict() for item in self.examples],
            "splits": {name: list(ids) for name, ids in self.splits.items()},
            "rules": dict(self.rules),
            "split_config": dict(self.split_config),
            "statistics": self.statistics.to_dict(),
            "source_datasets": list(self.source_datasets),
            "preference_sources": dict(self.preference_sources),
            "source_data_version": self.source_data_version,
            "preprocessing_version": self.preprocessing_version,
            "tags": list(self.tags),
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PreferenceDatasetVersion:
        splits: dict[str, tuple[str, ...]] = {}
        raw_splits = data.get("splits")
        if isinstance(raw_splits, Mapping):
            splits = {str(name): _texts(ids) for name, ids in raw_splits.items()}
        sources = data.get("preference_sources")
        return cls(
            dataset_version_id=_text(data.get("dataset_version_id")),
            name=_text(data.get("name")),
            version=_text(data.get("version")),
            dataset_type=_text(data.get("dataset_type"), PreferenceDatasetType.NLU.value),
            description=_text(data.get("description")),
            examples=tuple(
                PreferenceExample.from_dict(item)
                for item in _mappings(data.get("examples"))
            ),
            splits=splits,
            rules=_mapping(data.get("rules")),
            split_config=_mapping(data.get("split_config")),
            statistics=PreferenceStatistics.from_dict(
                _mapping(data.get("statistics")) or {}
            ),
            source_datasets=_texts(data.get("source_datasets")),
            preference_sources={
                str(name): int(count)
                for name, count in (sources.items() if isinstance(sources, Mapping) else ())
                if isinstance(count, (int, float)) and not isinstance(count, bool)
            },
            source_data_version=_text(data.get("source_data_version")),
            preprocessing_version=_text(
                data.get("preprocessing_version"), PREFERENCE_PREPROCESSING_VERSION
            ),
            tags=_texts(data.get("tags")),
            created_at=_text(data.get("created_at")) or now_iso(),
            schema_version=(
                _whole(data.get("schema_version")) or PREFERENCE_DATASET_SCHEMA_VERSION
            ),
        )


def preference_source_data_version(
    *,
    trajectories: int,
    evaluations: int,
    feedback: int,
    reviews: int,
    extra: str = "",
) -> str:
    """A readable stamp of the Phase 15/16 rows a preference version came from."""
    stamp = (
        f"preference-preprocessing={PREFERENCE_PREPROCESSING_VERSION};"
        f"trajectories={max(0, int(trajectories))};"
        f"evaluations={max(0, int(evaluations))};"
        f"feedback={max(0, int(feedback))};"
        f"reviews={max(0, int(reviews))}"
    )
    return f"{stamp};{extra}" if extra else stamp


__all__ = [
    "EVIDENCE_BENCHMARK_EXPECTATION",
    "EVIDENCE_EVALUATION_DIMENSIONS",
    "EVIDENCE_EXPLICIT_USER_PREFERENCE",
    "EVIDENCE_HUMAN_REVIEW",
    "EVIDENCE_SYNTHETIC_RULE",
    "EVIDENCE_TEACHER_AGREEMENT",
    "EVIDENCE_VERIFIED_SUCCESS",
    "PAIR_SPLIT_NAMES",
    "PREFERENCE_DATASET_SCHEMA_VERSION",
    "PREFERENCE_PREPROCESSING_VERSION",
    "PREFERENCE_SCHEMA_VERSION",
    "SOURCE_STRENGTH",
    "PreferenceDatasetType",
    "PreferenceDatasetVersion",
    "PreferenceEvidence",
    "PreferenceExample",
    "PreferenceQualityStatus",
    "PreferenceReviewDecision",
    "PreferenceRules",
    "PreferenceSource",
    "PreferenceStatistics",
    "PreferenceStrength",
    "PreferenceAlgorithm",
    "next_preference_version",
    "preference_dataset_version_id",
    "preference_source_data_version",
]
