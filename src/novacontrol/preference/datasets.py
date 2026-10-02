"""Building preference pairs out of what NovaControl observed.

A preference pair says "of these two behaviours for this prompt, prefer the
first". The hard part is not the schema — it is refusing to invent one. This
builder therefore pairs only what the record already answers:

  * **explicit user feedback** — one candidate a person accepted and another they
    rejected. A pair needs both halves: a user who accepted A and never saw B has
    expressed no preference between them, and calling that a pair would put words
    in their mouth.
  * **a user's correction** — the run produced something and the person recorded a
    STRUCTURED correction for the same prompt (``metadata["corrected_output"]``).
    Prose is never parsed here: interpreting free text would make the builder the
    thing that decides what somebody meant.
  * **verified outcomes** — two candidates for one prompt where one succeeded with
    its verification passing and the other did not. Where both succeeded there is
    no outcome preference, because which is better becomes a question of cost.
  * **evaluation results** — the Phase 15 ``EvaluationEngine``'s dimension scores,
    with the winner ahead by a configured margin. A tie produces nothing.
  * **benchmark expectations** — the Phase 15 golden dataset's expected intent,
    decision or tool, but ONLY where the run disagreed with it. A run that matched
    the expectation has no preference in it: there is nothing to prefer.
  * **human review** — a pair a person submitted, or one they settled in the
    review queue.

Anything a teacher model or a synthetic rule suggests arrives through
``extra_pairs`` and is marked ``TEACHER_MODEL``/``SYNTHETIC``, so it is never
counted as though a person had said it.

Pairs are then sanitised, classified by :class:`PreferenceQualityFilter`, split
with Phase 16's deterministic group-safe splitter (the same seed, the same
relative-deficit walk, the same guarantee that one group never crosses a split),
and published as an immutable ``name@version``. Refusals are counted by name, so a
small dataset explains itself instead of just being small.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from novacontrol.evaluation.datasets import GoldenDataset, GoldenExample
from novacontrol.evaluation.evaluator import EvaluationResult
from novacontrol.evaluation.models import AgentTrajectory, now_iso
from novacontrol.preference.models import (
    EVIDENCE_BENCHMARK_EXPECTATION,
    EVIDENCE_EVALUATION_DIMENSIONS,
    EVIDENCE_EXPLICIT_USER_PREFERENCE,
    EVIDENCE_HUMAN_REVIEW,
    EVIDENCE_VERIFIED_SUCCESS,
    PAIR_SPLIT_NAMES,
    PREFERENCE_PREPROCESSING_VERSION,
    SOURCE_STRENGTH,
    PreferenceDatasetType,
    PreferenceDatasetVersion,
    PreferenceEvidence,
    PreferenceExample,
    PreferenceQualityStatus,
    PreferenceRules,
    PreferenceSource,
    PreferenceStatistics,
    PreferenceStrength,
    next_preference_version,
    preference_dataset_version_id,
    preference_source_data_version,
)
from novacontrol.preference.quality import (
    PreferenceQualityConfig,
    PreferenceQualityFilter,
)
from novacontrol.training.datasets import SFTDatasetBuilder
from novacontrol.training.models import SplitConfig, difficulty_of, reasoning_violations

# ── skip reasons (one vocabulary, counted in the statistics) ────────────────

REASON_MISSING_DATA = "missing_structured_data"
REASON_IDENTICAL = "identical_candidates"
REASON_LOSER_NOT_WORSE = "no_evidence_the_rejected_side_is_worse"
REASON_BELOW_CONFIDENCE = "below_min_confidence"
REASON_MODEL = "model_filter"
REASON_CATEGORY = "task_category_filter"
REASON_SOURCE = "source_filter"
REASON_TAGS = "tag_filter"
REASON_DATE = "date_filter"
REASON_SOURCE_NOT_ALLOWED = "source_not_allowed"
REASON_UNVERIFIED_SOURCE = "unverified_source"
REASON_MISSING_PROVENANCE = "missing_provenance"
REASON_DUPLICATE = "duplicate"
REASON_CONTRADICTORY = "contradictory_preference"
REASON_HIDDEN_REASONING = "hidden_reasoning"
REASON_SENSITIVE = "residual_sensitive_data"
REASON_MALFORMED = "malformed_pair"
REASON_REVIEW_REQUIRED = "needs_review"
REASON_REJECTED = "rejected_by_quality_filter"
REASON_NO_PAIRS = "no_pairs_for_this_type"
REASON_NO_SECOND_CANDIDATE = "no_second_candidate"
REASON_NO_FEEDBACK = "no_explicit_feedback"

#: Feedback labels that mean "this was good" / "this was not", as stored by
#: whoever collected the feedback. A label in neither set is not a preference:
#: guessing at prose is how a builder starts inventing data.
_POSITIVE_LABELS = frozenset(
    {"accepted", "accept", "approved", "good", "helpful", "correct", "positive", "right"}
)
_NEGATIVE_LABELS = frozenset(
    {"rejected", "reject", "bad", "wrong", "unhelpful", "incorrect", "negative", "unusable"}
)
#: A label that says the person WROTE the right answer down. The correction itself
#: must be structured; the label only says that one exists.
_CORRECTION_LABELS = frozenset({"corrected", "correction", "fixed", "edited"})

#: How many trajectory rows one build will look at before it stops. A store with
#: hundreds of thousands of rows is a data-pipeline job, not something to attempt
#: synchronously while somebody waits for an HTTP response.
MAX_ROWS_EXAMINED = 20_000

#: How many candidates one prompt may contribute to a pairing decision.
MAX_CANDIDATES_PER_PROMPT = 8


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _unit(value: float) -> float:
    return round(min(1.0, max(0.0, float(value))), 6)


def _filtered(source: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    """The named keys of a mapping, in the order they were asked for."""
    data = _mapping(source)
    return {key: data[key] for key in keys if key in data}


def _meets_expectation(produced: Mapping[str, Any], expectation: Mapping[str, Any]) -> bool:
    """Whether a run's candidate matches what a golden fixture asserts.

    A golden example is a PARTIAL statement — it declares a tool and its
    arguments and says nothing about the call's status or capability — while the
    run's candidate is the full observed behaviour. Comparing the two mappings
    for equality would report a run that did exactly what was expected as a
    disagreement and manufacture a pair from it, so only the fields the fixture
    actually asserts are compared.
    """
    return all(produced.get(key) == value for key, value in expectation.items())


def _verified_outcome(trajectory: AgentTrajectory | None) -> bool:
    """Whether a candidate's success is BACKED by verification, not just claimed."""
    if trajectory is None or trajectory.success is not True:
        return False
    records = trajectory.verification_results
    if not records:
        return False
    return not trajectory.failed_verifications and any(record.passed for record in records)


def _outcome(trajectory: AgentTrajectory | None) -> dict[str, Any]:
    """The observable outcome of one candidate, as structured readings."""
    if trajectory is None:
        return {}
    records = trajectory.verification_results
    feedback = trajectory.user_feedback
    data: dict[str, Any] = {
        "success": trajectory.success,
        "status": trajectory.status,
        "verification_records": len(records),
        "verification_failed": len(trajectory.failed_verifications),
        "verification_passed": sum(1 for record in records if record.passed),
        "recovery_events": len(trajectory.recovery_events),
        "recovered": any(
            _text(record.outcome) == "recovered" for record in trajectory.recovery_events
        ),
        "retries": trajectory.retries,
        "duration_ms": trajectory.latency_metrics.total_ms,
    }
    if feedback is not None:
        data["feedback"] = {
            "label": _text(feedback.label),
            "rating": feedback.rating,
            "source": _text(feedback.source),
        }
    return data


def _evaluation_reading(result: EvaluationResult | None) -> dict[str, Any]:
    """The Phase 15 dimension scores, kept separate rather than averaged away."""
    if result is None:
        return {}
    scores = {
        name: round(float(value), 6)
        for name, value in result.scores().items()
        if value is not None
    }
    return {
        "evaluation_id": result.evaluation_id,
        "task_success": result.task_success,
        "overall_status": result.overall_status,
        "dimensions": scores,
        "overall": round(sum(scores.values()) / len(scores), 6) if scores else 0.0,
    }


def _overall(result: EvaluationResult | None) -> float | None:
    reading = _evaluation_reading(result)
    if not reading.get("dimensions"):
        return None
    return float(reading["overall"])


def _feedback_polarity(trajectory: AgentTrajectory) -> tuple[int, str]:
    """+1 accepted, -1 rejected, 0 no opinion. The label says which, not the prose."""
    feedback = trajectory.user_feedback
    if feedback is None:
        return (0, "")
    label = _text(feedback.label).lower()
    if label in _POSITIVE_LABELS:
        return (1, label)
    if label in _NEGATIVE_LABELS:
        return (-1, label)
    rating = feedback.rating
    if isinstance(rating, int) and not isinstance(rating, bool):
        if rating >= 4:
            return (1, f"rating={rating}")
        if rating <= 2:
            return (-1, f"rating={rating}")
    return (0, label)


def _is_correction(trajectory: AgentTrajectory) -> bool:
    feedback = trajectory.user_feedback
    if feedback is None:
        return False
    return _text(feedback.label).lower() in _CORRECTION_LABELS


def _correction_of(trajectory: AgentTrajectory) -> dict[str, Any]:
    """The structured correction a person recorded, if there is one."""
    return _mapping(_mapping(trajectory.metadata).get("corrected_output"))


def _prompt_of(trajectory: AgentTrajectory) -> dict[str, Any]:
    """The prompt two candidates share. The task id groups, the request names it."""
    request = _text(trajectory.user_request)
    return {
        "request": request,
        "task_id": _text(trajectory.task_id),
        "group_key": _text(trajectory.task_id)
        or _text(trajectory.parent_task_id)
        or request,
    }


def _prompt_key(trajectory: AgentTrajectory) -> str:
    """What makes two trajectories candidates for the same decision.

    The task id when there is one (the application threads it through a whole
    request), otherwise the request text, case-folded — two spellings of the same
    request are the same prompt, and treating them as different would silently
    drop every pair.
    """
    task_id = _text(trajectory.task_id) or _text(trajectory.parent_task_id)
    if task_id:
        return f"task:{task_id}"
    return "request:" + _text(trajectory.user_request).casefold()


def _context_of(trajectory: AgentTrajectory) -> dict[str, Any]:
    return {
        "context_summary": _mapping(trajectory.context_summary),
        "source": _text(trajectory.source),
        "model_information": _mapping(trajectory.model_information),
    }


def _tags(trajectory: AgentTrajectory) -> set[str]:
    tags = {str(item).lower() for item in _rows(_mapping(trajectory.metadata).get("tags"))}
    intent = _text(trajectory.structured_intent.get("intent")).lower()
    if intent:
        tags.add(intent)
    source = _text(trajectory.source).lower()
    if source:
        tags.add(source)
    return tags


def _category(trajectory: AgentTrajectory) -> str:
    metadata = _mapping(trajectory.metadata)
    for key in ("task_category", "category"):
        named = _text(metadata.get(key))
        if named:
            return named
    return _text(trajectory.source)


def _mentions_model(trajectory: AgentTrajectory, wanted: str) -> bool:
    needle = wanted.lower()
    for key, value in _mapping(trajectory.model_information).items():
        if needle in str(value).lower() or needle in str(key).lower():
            return True
    return False


def _evaluation_ids(*results: EvaluationResult | None) -> tuple[str, ...]:
    found = [
        item.evaluation_id for item in results if item is not None and item.evaluation_id
    ]
    return tuple(dict.fromkeys(found))


def _has_provenance(pair: PreferenceExample) -> bool:
    return bool(pair.source_trajectory_ids or pair.source_evaluation_ids or pair.reviewed)


def _benchmark_list(
    benchmarks: Sequence[GoldenExample] | GoldenDataset | None,
) -> list[GoldenExample]:
    if benchmarks is None:
        return []
    if isinstance(benchmarks, GoldenDataset):
        return list(benchmarks.examples)
    return [item for item in benchmarks if isinstance(item, GoldenExample)]


def _verified_winner(
    first: AgentTrajectory, second: AgentTrajectory
) -> tuple[tuple[AgentTrajectory, AgentTrajectory] | None, float, str] | None:
    """One candidate verified and the other not — the clearest preference there is.

    Both verified is NOT a preference (both worked; which is cheaper is the
    evaluation source's question), and neither verified is nothing to go on.
    """
    first_ok = _verified_outcome(first)
    second_ok = _verified_outcome(second)
    if first_ok == second_ok:
        return None
    winner, loser = (first, second) if first_ok else (second, first)
    return (
        (winner, loser),
        0.9,
        "the preferred candidate's verification passed; the other's did not",
    )


def _evaluation_winner(
    first: AgentTrajectory,
    second: AgentTrajectory,
    evaluations: Mapping[str, EvaluationResult],
    rules: PreferenceRules,
) -> tuple[tuple[AgentTrajectory, AgentTrajectory] | None, float, str] | None:
    """Order two candidates by their Phase 15 dimension scores, by a real margin."""
    first_score = _overall(evaluations.get(first.trajectory_id))
    second_score = _overall(evaluations.get(second.trajectory_id))
    if first_score is None or second_score is None:
        return None
    margin = abs(first_score - second_score)
    if margin < max(0.0, float(rules.min_score_margin)):
        return None
    winner, loser = (first, second) if first_score > second_score else (second, first)
    return (
        (winner, loser),
        min(0.95, 0.6 + margin),
        f"the evaluation scored the preferred candidate {max(first_score, second_score):.3f} "
        f"against {min(first_score, second_score):.3f}",
    )


class PreferenceDatasetBuilder:
    """Turns observed NovaControl behaviour into versioned preference datasets."""

    def __init__(
        self,
        *,
        quality: PreferenceQualityFilter | None = None,
        splitter: SFTDatasetBuilder | None = None,
    ) -> None:
        self.quality = quality if quality is not None else PreferenceQualityFilter()
        #: The splitter is Phase 16's, deliberately: deterministic, group-safe and
        #: shared, so re-tuning a split in one phase changes it in both rather than
        #: letting two definitions of "leak-free" drift apart.
        self._splitter = splitter if splitter is not None else SFTDatasetBuilder()

    # -- candidates ------------------------------------------------------------

    def candidate_for(self, dataset_type: str, trajectory: AgentTrajectory) -> dict[str, Any]:
        """The observable output of one run for one preference family.

        Returns an empty mapping when this run said nothing of that shape, which is
        what stops a trajectory being squeezed into a family it never produced.
        """
        if dataset_type == PreferenceDatasetType.NLU.value:
            return _filtered(
                trajectory.structured_intent,
                ("intent", "confidence", "entities", "requires_vision", "strategy"),
            )
        if dataset_type == PreferenceDatasetType.DECISION.value:
            return _filtered(
                trajectory.decision,
                ("route", "decision_type", "capability", "model", "requires_confirmation"),
            )
        if dataset_type == PreferenceDatasetType.TOOL_SELECTION.value:
            calls = list(trajectory.tool_calls)
            if not calls:
                return {}
            picked = next((call for call in calls if call.succeeded), calls[0])
            return {
                "tool": _text(picked.tool),
                "arguments": _mapping(picked.arguments),
                "capability": _text(picked.capability),
                "status": _text(picked.status),
            }
        if dataset_type == PreferenceDatasetType.PLANNING.value:
            plan = _mapping(trajectory.plan)
            steps = _rows(plan.get("steps")) or [
                step.to_dict() for step in trajectory.execution_steps
            ]
            cleaned: list[dict[str, Any]] = []
            for step in steps:
                data = _mapping(step)
                action = _text(data.get("action")) or _text(data.get("description"))
                if not action:
                    continue
                cleaned.append(
                    {
                        key: data[key]
                        for key in ("step_id", "description", "action", "tool", "depends_on")
                        if key in data
                    }
                )
            if not cleaned:
                return {}
            return {
                "goal": _text(plan.get("goal")) or _text(trajectory.user_request),
                "steps": cleaned,
            }
        if dataset_type == PreferenceDatasetType.RECOVERY.value:
            recovery = list(trajectory.recovery_events)
            if recovery:
                first = recovery[0]
                return {
                    "strategy": _text(first.strategy),
                    "outcome": _text(first.outcome),
                    "attempts": first.attempts,
                }
            # A failed run that never recovered IS a rejected behaviour: the absence
            # of a recovery is the decision being judged.
            if trajectory.success is not True:
                return {"strategy": "", "outcome": "unrecovered", "attempts": 0}
            return {}
        if dataset_type == PreferenceDatasetType.RESPONSE.value:
            result = _mapping(trajectory.final_result)
            text = (
                _text(result.get("response"))
                or _text(result.get("summary"))
                or _text(result.get("text"))
            )
            if not text:
                return {}
            return {
                "response": text,
                "result": result,
                "status": trajectory.status,
                "complete": bool(result) and trajectory.success is True,
            }
        return {}

    def benchmark_candidate(self, dataset_type: str, expectation: GoldenExample) -> dict[str, Any]:
        """What Phase 15's golden dataset expects for one family, if it speaks to it.

        The fixture carries intent, decision, tool and plan expectations only, so
        the families it cannot answer return nothing rather than a guess.
        """
        if dataset_type == PreferenceDatasetType.NLU.value:
            intent = _text(expectation.expected_intent)
            return {"intent": intent} if intent else {}
        if dataset_type == PreferenceDatasetType.DECISION.value:
            return _filtered(
                expectation.expected_decision, ("route", "decision_type", "capability")
            )
        if dataset_type == PreferenceDatasetType.TOOL_SELECTION.value:
            tool = _text(expectation.expected_tool)
            if not tool:
                return {}
            candidate: dict[str, Any] = {"tool": tool}
            arguments = _mapping(expectation.expected_arguments)
            if arguments:
                # The fixture says nothing about arguments when it carries none:
                # declaring ``{}`` would turn "no opinion" into an assertion that
                # the run must have passed no arguments.
                candidate["arguments"] = arguments
            return candidate
        return {}

    # -- strength --------------------------------------------------------------

    def _strength(
        self,
        source: str,
        *,
        confidence: float,
        chosen: AgentTrajectory | None,
        evidence_kind: str,
        evidence_detail: str = "",
        evidence_verified: bool = False,
    ) -> PreferenceStrength:
        """The three axes and the evidence list behind one pair.

        ``verification_strength`` is read from the preferred candidate's recorded
        outcome: a success verification confirmed scores 1, a success nobody checked
        scores a half (it happened; nothing confirmed it), anything else zero. The
        evidence list keeps the individual reason — who decided, and whether it was
        observed.
        """
        if _verified_outcome(chosen):
            verification = 1.0
        elif chosen is not None and chosen.success is True:
            verification = 0.5
        else:
            verification = 0.0
        return PreferenceStrength(
            confidence=_unit(confidence),
            evidence_quality=_unit(SOURCE_STRENGTH.get(source, 0.3)),
            verification_strength=verification,
            evidence=(
                PreferenceEvidence(
                    kind=evidence_kind,
                    strength=_unit(SOURCE_STRENGTH.get(source, 0.3)),
                    detail=evidence_detail,
                    source=source,
                    verified=evidence_verified,
                ),
            ),
            source=source,
        )

    def _pair(
        self,
        dataset_type: str,
        *,
        winner: AgentTrajectory,
        loser: AgentTrajectory | None,
        chosen: Mapping[str, Any],
        rejected: Mapping[str, Any],
        source: str,
        confidence: float,
        evidence_kind: str,
        detail: str,
        verified: bool,
        evaluations: Mapping[str, EvaluationResult],
        tags: Sequence[str] = (),
    ) -> PreferenceExample:
        return PreferenceExample(
            dataset_type=dataset_type,
            prompt=_prompt_of(winner),
            context=_context_of(winner),
            chosen=dict(chosen),
            rejected=dict(rejected),
            chosen_outcome=_outcome(winner),
            rejected_outcome=_outcome(loser),
            chosen_evaluation=_evaluation_reading(evaluations.get(winner.trajectory_id)),
            rejected_evaluation=_evaluation_reading(
                evaluations.get(loser.trajectory_id) if loser is not None else None
            ),
            preference_source=source,
            confidence=confidence,
            strength=self._strength(
                source,
                confidence=confidence,
                chosen=winner,
                evidence_kind=evidence_kind,
                evidence_detail=detail,
                evidence_verified=verified,
            ),
            source_trajectory_ids=tuple(
                item
                for item in (
                    winner.trajectory_id,
                    loser.trajectory_id if loser is not None else "",
                )
                if item
            ),
            source_evaluation_ids=_evaluation_ids(
                evaluations.get(winner.trajectory_id),
                evaluations.get(loser.trajectory_id) if loser is not None else None,
            ),
            difficulty=difficulty_of(winner),
            tags=tuple(sorted({dataset_type, *tags, *_tags(winner)})),
        )

    # -- explicit feedback -----------------------------------------------------

    def pairs_from_feedback(
        self,
        dataset_type: str,
        trajectories: Sequence[AgentTrajectory],
        evaluations: Mapping[str, EvaluationResult],
        rules: PreferenceRules,
    ) -> tuple[list[PreferenceExample], dict[str, int]]:
        """Explicit user feedback, and structured user corrections."""
        pairs: list[PreferenceExample] = []
        skipped: dict[str, int] = {}

        def skip(reason: str) -> None:
            skipped[reason] = skipped.get(reason, 0) + 1

        groups: dict[str, list[AgentTrajectory]] = {}
        for trajectory in trajectories:
            groups.setdefault(_prompt_key(trajectory), []).append(trajectory)

        for key in sorted(groups):
            members = groups[key][:MAX_CANDIDATES_PER_PROMPT]
            for trajectory in members:
                polarity, label = _feedback_polarity(trajectory)
                if polarity > 0:
                    rejected_peers = [
                        peer
                        for peer in members
                        if peer.trajectory_id != trajectory.trajectory_id
                        and _feedback_polarity(peer)[0] < 0
                    ]
                    if not rejected_peers:
                        skip(REASON_NO_FEEDBACK if len(members) > 1 else REASON_NO_SECOND_CANDIDATE)
                        continue
                    peer = rejected_peers[0]
                    peer_label = _feedback_polarity(peer)[1]
                    chosen = self.candidate_for(dataset_type, trajectory)
                    rejected = self.candidate_for(dataset_type, peer)
                    if not chosen or not rejected or chosen == rejected:
                        skip(
                            REASON_MISSING_DATA
                            if not chosen or not rejected
                            else REASON_IDENTICAL
                        )
                        continue
                    pairs.append(
                        self._pair(
                            dataset_type,
                            winner=trajectory,
                            loser=peer,
                            chosen=chosen,
                            rejected=rejected,
                            source=PreferenceSource.EXPLICIT_USER_FEEDBACK.value,
                            confidence=0.95,
                            evidence_kind=EVIDENCE_EXPLICIT_USER_PREFERENCE,
                            detail=(
                                f"a person labelled one candidate {label or 'accepted'} "
                                f"and the other {peer_label or 'rejected'}"
                            ),
                            verified=True,
                            evaluations=evaluations,
                            tags=("feedback",),
                        )
                    )
                    continue
                if polarity < 0 or not _is_correction(trajectory):
                    continue
                corrected = _correction_of(trajectory)
                produced = self.candidate_for(dataset_type, trajectory)
                if not corrected:
                    skip(REASON_MISSING_DATA)
                    continue
                if not produced or produced == corrected:
                    skip(REASON_MISSING_DATA if not produced else REASON_IDENTICAL)
                    continue
                pairs.append(
                    self._pair(
                        dataset_type,
                        winner=trajectory,
                        loser=None,
                        chosen=corrected,
                        rejected=produced,
                        source=PreferenceSource.EXPLICIT_USER_FEEDBACK.value,
                        confidence=0.9,
                        evidence_kind=EVIDENCE_EXPLICIT_USER_PREFERENCE,
                        detail="a person recorded a structured correction for this run",
                        verified=True,
                        evaluations=evaluations,
                        tags=("correction",),
                    )
                )
        return (pairs, skipped)

    # -- outcomes and evaluations ----------------------------------------------

    def pairs_from_outcomes(
        self,
        dataset_type: str,
        trajectories: Sequence[AgentTrajectory],
        evaluations: Mapping[str, EvaluationResult],
        rules: PreferenceRules,
    ) -> tuple[list[PreferenceExample], dict[str, int]]:
        """Verified outcomes: one candidate worked, the other did not."""
        return self._peer_pairs(
            dataset_type,
            trajectories,
            evaluations,
            rules,
            decide=_verified_winner,
            source=PreferenceSource.VERIFIED_OUTCOME.value,
            evidence_kind=EVIDENCE_VERIFIED_SUCCESS,
            verified=True,
        )

    def pairs_from_evaluations(
        self,
        dataset_type: str,
        trajectories: Sequence[AgentTrajectory],
        evaluations: Mapping[str, EvaluationResult],
        rules: PreferenceRules,
    ) -> tuple[list[PreferenceExample], dict[str, int]]:
        """Evaluation results: the Phase 15 engine's dimension scores decide."""
        return self._peer_pairs(
            dataset_type,
            trajectories,
            evaluations,
            rules,
            decide=lambda a, b: _evaluation_winner(a, b, evaluations, rules),
            source=PreferenceSource.EVALUATION.value,
            evidence_kind=EVIDENCE_EVALUATION_DIMENSIONS,
            verified=False,
        )

    def _peer_pairs(
        self,
        dataset_type: str,
        trajectories: Sequence[AgentTrajectory],
        evaluations: Mapping[str, EvaluationResult],
        rules: PreferenceRules,
        *,
        decide: Any,
        source: str,
        evidence_kind: str,
        verified: bool,
    ) -> tuple[list[PreferenceExample], dict[str, int]]:
        """Pair up candidates that share a prompt, decided by ``decide``.

        ``decide(a, b)`` returns ``((winner, loser), confidence, detail)`` when it
        can order the two and ``None`` when it cannot — in which case NO pair is
        produced. That is the phase's rule: a successful run is not automatically
        a preference.
        """
        pairs: list[PreferenceExample] = []
        skipped: dict[str, int] = {}
        groups: dict[str, list[AgentTrajectory]] = {}
        for trajectory in trajectories:
            groups.setdefault(_prompt_key(trajectory), []).append(trajectory)

        for key in sorted(groups):
            members = groups[key][:MAX_CANDIDATES_PER_PROMPT]
            if len(members) < 2:
                skipped[REASON_NO_SECOND_CANDIDATE] = (
                    skipped.get(REASON_NO_SECOND_CANDIDATE, 0) + 1
                )
                continue
            ordered: tuple[AgentTrajectory, AgentTrajectory] | None = None
            confidence = 0.0
            detail = ""
            for index, candidate in enumerate(members):
                for other in members[index + 1 :]:
                    verdict = decide(candidate, other)
                    if verdict is None:
                        continue
                    ordered, confidence, detail = verdict
                    break
                if ordered is not None:
                    break
            if ordered is None:
                skipped[REASON_LOSER_NOT_WORSE] = (
                    skipped.get(REASON_LOSER_NOT_WORSE, 0) + 1
                )
                continue
            winner, loser = ordered
            chosen = self.candidate_for(dataset_type, winner)
            rejected = self.candidate_for(dataset_type, loser)
            if not chosen or not rejected:
                skipped[REASON_MISSING_DATA] = skipped.get(REASON_MISSING_DATA, 0) + 1
                continue
            if chosen == rejected:
                skipped[REASON_IDENTICAL] = skipped.get(REASON_IDENTICAL, 0) + 1
                continue
            pairs.append(
                self._pair(
                    dataset_type,
                    winner=winner,
                    loser=loser,
                    chosen=chosen,
                    rejected=rejected,
                    source=source,
                    confidence=confidence,
                    evidence_kind=evidence_kind,
                    detail=detail,
                    verified=verified,
                    evaluations=evaluations,
                )
            )
        return (pairs, skipped)

    # -- benchmark expectations ------------------------------------------------

    def pairs_from_benchmarks(
        self,
        dataset_type: str,
        trajectories: Sequence[AgentTrajectory],
        benchmarks: Sequence[GoldenExample],
        rules: PreferenceRules,
    ) -> tuple[list[PreferenceExample], dict[str, int]]:
        """The golden dataset's expectation, where the run disagreed with it."""
        pairs: list[PreferenceExample] = []
        skipped: dict[str, int] = {}
        for expectation in benchmarks:
            wanted = _text(expectation.request).casefold()
            if not wanted:
                continue
            shipped = self.benchmark_candidate(dataset_type, expectation)
            if not shipped:
                skipped[REASON_MISSING_DATA] = skipped.get(REASON_MISSING_DATA, 0) + 1
                continue
            for trajectory in trajectories:
                if _text(trajectory.user_request).casefold() != wanted:
                    continue
                produced = self.candidate_for(dataset_type, trajectory)
                if not produced or _meets_expectation(produced, shipped):
                    # The run agreed with the expectation: there is no pair here.
                    skipped[REASON_IDENTICAL] = skipped.get(REASON_IDENTICAL, 0) + 1
                    continue
                pairs.append(
                    self._pair(
                        dataset_type,
                        winner=trajectory,
                        loser=None,
                        chosen=shipped,
                        rejected=produced,
                        source=PreferenceSource.BENCHMARK.value,
                        confidence=0.8,
                        evidence_kind=EVIDENCE_BENCHMARK_EXPECTATION,
                        detail=(
                            f"the golden example {expectation.example_id!r} expects "
                            "something this run did not produce"
                        ),
                        verified=False,
                        evaluations={},
                        tags=("benchmark", expectation.example_id),
                    )
                )
        return (pairs, skipped)

    # -- pairs a person put together -------------------------------------------

    def human_pair(
        self,
        dataset_type: str,
        *,
        prompt: Mapping[str, Any],
        chosen: Mapping[str, Any],
        rejected: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
        chosen_outcome: Mapping[str, Any] | None = None,
        rejected_outcome: Mapping[str, Any] | None = None,
        reviewer: str = "",
        reason: str = "",
        confidence: float = 1.0,
        group_key: str = "",
        tags: Sequence[str] = (),
    ) -> PreferenceExample:
        """A pair a human put together: the strongest preference there is.

        Built here rather than in the review layer so a submitted pair and a settled
        review carry the same evidence shape — a reviewer's decision IS evidence, and
        it is recorded as evidence.
        """
        prompt_data = dict(prompt)
        if group_key:
            prompt_data["group_key"] = group_key
        return PreferenceExample(
            dataset_type=dataset_type,
            prompt=prompt_data,
            context=dict(context or {}),
            chosen=dict(chosen),
            rejected=dict(rejected),
            chosen_outcome=dict(chosen_outcome or {"success": True, "status": "reviewed"}),
            rejected_outcome=dict(rejected_outcome or {}),
            preference_source=PreferenceSource.HUMAN_REVIEW.value,
            confidence=confidence,
            strength=self._strength(
                PreferenceSource.HUMAN_REVIEW.value,
                confidence=confidence,
                chosen=None,
                evidence_kind=EVIDENCE_HUMAN_REVIEW,
                evidence_detail=(
                    f"{reviewer or 'a reviewer'} chose this candidate"
                    + (f": {reason}" if reason else "")
                ),
                evidence_verified=True,
            ),
            tags=tuple(sorted({dataset_type, "human_review", *tags})),
            review={
                "decision": "choose_a",
                "reviewer": reviewer,
                "reason": reason,
                "at": now_iso(),
            },
        )

    # -- building --------------------------------------------------------------

    def build(
        self,
        *,
        name: str,
        dataset_type: PreferenceDatasetType | str,
        trajectories: Sequence[AgentTrajectory] = (),
        evaluations: Sequence[EvaluationResult] = (),
        benchmarks: Sequence[GoldenExample] | GoldenDataset | None = None,
        extra_pairs: Sequence[PreferenceExample] = (),
        reviewed: Sequence[PreferenceExample] = (),
        rules: PreferenceRules | None = None,
        quality: PreferenceQualityConfig | None = None,
        split: SplitConfig | None = None,
        version: str = "",
        description: str = "",
        tags: Sequence[str] = (),
        existing_versions: Sequence[str] = (),
        source_datasets: Sequence[str] = (),
    ) -> PreferenceDatasetVersion:
        """Build and return one immutable preference dataset version."""
        wanted = (
            dataset_type.value
            if isinstance(dataset_type, PreferenceDatasetType)
            else str(dataset_type)
        )
        selection = rules if rules is not None else PreferenceRules()
        split_config = split if split is not None else SplitConfig()
        problems = split_config.issues()
        if problems:
            raise ValueError("; ".join(problems))
        clean_name = str(name).strip()
        if not clean_name or "@" in clean_name:
            raise ValueError("a dataset name must be non-empty and must not contain '@'")
        known = {member.value for member in PreferenceDatasetType}
        if wanted not in known:
            raise ValueError(
                f"unknown preference dataset_type {wanted!r}: expected one of "
                + ", ".join(sorted(known))
            )
        # A per-build quality configuration makes a LOCAL filter: the builder is
        # shared by the manager, and one caller's thresholds must not silently
        # become every later caller's.
        filter_ = (
            PreferenceQualityFilter(quality, redactor=self.quality.redactor)
            if quality is not None
            else self.quality
        )

        by_trajectory = {
            result.trajectory_id: result for result in evaluations if result.trajectory_id
        }
        rows, skipped = self._eligible(wanted, list(trajectories), selection)
        allowed = (
            set(selection.preference_sources)
            if selection.preference_sources
            else {member.value for member in PreferenceSource}
        )

        candidates: list[PreferenceExample] = []
        if PreferenceSource.EXPLICIT_USER_FEEDBACK.value in allowed:
            found, reasons = self.pairs_from_feedback(wanted, rows, by_trajectory, selection)
            candidates.extend(found)
            _merge(skipped, reasons)
        if PreferenceSource.VERIFIED_OUTCOME.value in allowed:
            found, reasons = self.pairs_from_outcomes(wanted, rows, by_trajectory, selection)
            candidates.extend(found)
            _merge(skipped, reasons)
        if PreferenceSource.EVALUATION.value in allowed:
            found, reasons = self.pairs_from_evaluations(
                wanted, rows, by_trajectory, selection
            )
            candidates.extend(found)
            _merge(skipped, reasons)
        fixtures = _benchmark_list(benchmarks)
        if selection.use_benchmark_expectations and PreferenceSource.BENCHMARK.value in allowed:
            found, reasons = self.pairs_from_benchmarks(wanted, rows, fixtures, selection)
            candidates.extend(found)
            _merge(skipped, reasons)
        for pair in (*extra_pairs, *reviewed):
            if pair.preference_source not in allowed:
                skipped[REASON_SOURCE_NOT_ALLOWED] = (
                    skipped.get(REASON_SOURCE_NOT_ALLOWED, 0) + 1
                )
                continue
            candidates.append(replace(pair, dataset_type=wanted))

        pairs, counts = self.screen(candidates, selection, quality=filter_)
        _merge(skipped, counts)
        if selection.max_pairs and len(pairs) > selection.max_pairs:
            pairs = pairs[: selection.max_pairs]

        resolved = str(version).strip() or next_preference_version(existing_versions)
        splits = self.split_pairs(pairs, split_config)
        identifier = preference_dataset_version_id(clean_name, resolved)
        stamped = tuple(
            pair
            if pair.dataset_version == identifier
            else replace(pair, dataset_version=identifier)
            for pair in pairs
        )
        statistics = _statistics(
            stamped,
            splits=splits,
            skipped=skipped,
            source_trajectories=len({item.trajectory_id for item in rows}),
            source_evaluations=len(by_trajectory),
        )
        rules_mapping = dict(selection.to_mapping())
        rules_mapping["quality"] = filter_.config.to_mapping()
        rules_mapping["benchmarks_considered"] = len(fixtures)
        sources = ",".join(sorted(statistics.by_source))
        return PreferenceDatasetVersion(
            dataset_version_id=identifier,
            name=clean_name,
            version=resolved,
            dataset_type=wanted,
            description=str(description or ""),
            examples=stamped,
            splits=splits,
            rules=rules_mapping,
            split_config=split_config.to_mapping(),
            statistics=statistics,
            source_datasets=tuple(str(item) for item in source_datasets),
            preference_sources=dict(statistics.by_source),
            source_data_version=preference_source_data_version(
                trajectories=len(rows),
                evaluations=len(by_trajectory),
                feedback=sum(1 for item in rows if item.user_feedback is not None),
                reviews=sum(1 for item in stamped if item.reviewed),
                extra=(
                    f"dataset_type={wanted};sources={sources}"
                    if sources
                    else f"dataset_type={wanted}"
                ),
            ),
            preprocessing_version=PREFERENCE_PREPROCESSING_VERSION,
            tags=tuple(str(item) for item in tags),
        )

    # -- eligibility -----------------------------------------------------------

    def _eligible(
        self,
        dataset_type: str,
        trajectories: list[AgentTrajectory],
        rules: PreferenceRules,
    ) -> tuple[list[AgentTrajectory], dict[str, int]]:
        """The rows a pair may be built from, with the reasons the rest were not."""
        rows: list[AgentTrajectory] = []
        skipped: dict[str, int] = {}
        for index, trajectory in enumerate(trajectories):
            if index >= MAX_ROWS_EXAMINED:
                break
            reason = self._reason_not_eligible(trajectory, rules)
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            produced = self.candidate_for(dataset_type, trajectory)
            corrected = _correction_of(trajectory) if _is_correction(trajectory) else {}
            if not produced and not corrected:
                skipped[REASON_MISSING_DATA] = skipped.get(REASON_MISSING_DATA, 0) + 1
                continue
            rows.append(trajectory)
        return (rows, skipped)

    def _reason_not_eligible(self, trajectory: AgentTrajectory, rules: PreferenceRules) -> str:
        """Why this row may not contribute a pair, or ``""`` when it may.

        The hidden-reasoning guard runs FIRST and over the whole row: a pair is
        built out of structured behaviour, and a row carrying a reasoning trace has
        already failed the phase's one absolute rule — trimming it and using the
        rest is how a leaky producer stays invisible.
        """
        if reasoning_violations(trajectory.to_dict(), path="trajectory"):
            return REASON_HIDDEN_REASONING
        if rules.model and not _mentions_model(trajectory, rules.model):
            return REASON_MODEL
        if rules.task_category and rules.task_category.lower() not in _category(
            trajectory
        ).lower():
            return REASON_CATEGORY
        if rules.source and rules.source.lower() not in _text(trajectory.source).lower():
            return REASON_SOURCE
        if rules.tags and not (set(rules.tags) & _tags(trajectory)):
            return REASON_TAGS
        if rules.since and trajectory.timestamp < rules.since:
            return REASON_DATE
        if rules.until and trajectory.timestamp > rules.until:
            return REASON_DATE
        return ""

    # -- quality ---------------------------------------------------------------

    def screen(
        self,
        pairs: Sequence[PreferenceExample],
        rules: PreferenceRules,
        *,
        quality: PreferenceQualityFilter | None = None,
    ) -> tuple[list[PreferenceExample], dict[str, int]]:
        """Sanitise and classify every candidate pair, counting each refusal.

        A pair can be refused by the row rules (confidence, source, provenance) or
        by the quality filter (identical candidates, contradictory orientation,
        residual secrets). Both answer with a named reason, so the statistics can
        say why the dataset is smaller than the record.

        A pair the filter held is KEPT with ``needs_review`` on it, because storing
        the doubt is what lets a person settle it; ``PreferenceDatasetVersion.split``
        only ever trains on accepted pairs.
        """
        filter_ = quality if quality is not None else self.quality
        kept: list[PreferenceExample] = []
        counts: dict[str, int] = {}
        seen: dict[str, dict[str, Any]] = {}

        def skip(reason: str) -> None:
            counts[reason] = counts.get(reason, 0) + 1

        allowed = set(rules.preference_sources) if rules.preference_sources else set()
        for pair in pairs:
            if allowed and pair.preference_source not in allowed:
                skip(REASON_SOURCE_NOT_ALLOWED)
                continue
            if rules.require_verified_source and not PreferenceSource(
                pair.preference_source
            ).verified:
                skip(REASON_UNVERIFIED_SOURCE)
                continue
            if pair.confidence < rules.min_confidence:
                skip(REASON_BELOW_CONFIDENCE)
                continue
            if not _has_provenance(pair):
                skip(REASON_MISSING_PROVENANCE)
                continue
            cleaned, verdict = filter_.apply(pair)
            if cleaned is None:
                if any("sensitive" in reason for reason in verdict.reasons):
                    skip(REASON_SENSITIVE)
                else:
                    skip(REASON_REJECTED)
                continue
            fingerprint = cleaned.fingerprint()
            previous = seen.get(fingerprint)
            if previous is not None:
                # The same two candidates the other way round is a CONTRADICTION,
                # not a duplicate: one of the two orientations is wrong and nothing
                # in the record says which, so neither may train.
                if previous != dict(cleaned.chosen):
                    skip(REASON_CONTRADICTORY)
                else:
                    skip(REASON_DUPLICATE)
                continue
            try:
                json.dumps(cleaned.to_dict(), ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError):
                skip(REASON_MALFORMED)
                continue
            if verdict.needs_review:
                skip(REASON_REVIEW_REQUIRED)
            seen[fingerprint] = dict(cleaned.chosen)
            kept.append(cleaned)
        if not kept and not counts:
            counts[REASON_NO_PAIRS] = 1
        return (kept, counts)

    # -- splitting -------------------------------------------------------------

    def split_pairs(
        self, pairs: Sequence[PreferenceExample], config: SplitConfig
    ) -> dict[str, tuple[str, ...]]:
        """Phase 16's deterministic, group-safe split, applied to preference pairs.

        Each pair is projected to the supervised shape for the walk (the splitter
        speaks that type) and the ids are translated back, so the walk itself — group
        ordering, relative-deficit assignment, tie-breaking — has exactly one
        implementation in this codebase.
        """
        projections = [pair.as_sft_example() for pair in pairs]
        by_example_id = {
            projection.example_id: pair.preference_id
            for projection, pair in zip(projections, pairs, strict=True)
        }
        splits = self._splitter.split_examples(projections, config)
        return {
            name: tuple(
                by_example_id[example_id]
                for example_id in ids
                if example_id in by_example_id
            )
            for name, ids in splits.items()
        }

    # -- validation ------------------------------------------------------------

    def validate(self, dataset: PreferenceDatasetVersion) -> tuple[str, ...]:
        """Every reason this dataset must not be trained on (empty is good)."""
        issues: list[str] = []
        if not dataset.name or not dataset.version:
            issues.append("the dataset has no name or no version")
        if dataset.dataset_version_id != preference_dataset_version_id(
            dataset.name, dataset.version
        ):
            issues.append("dataset_version_id does not match name@version")
        if not dataset.examples:
            issues.append("the dataset has no preference pairs")
        known = {member.value for member in PreferenceDatasetType}
        if dataset.dataset_type not in known:
            issues.append(f"unknown dataset_type {dataset.dataset_type!r}")

        split_of: dict[str, str] = {}
        for name, ids in dataset.splits.items():
            if name not in PAIR_SPLIT_NAMES:
                issues.append(f"unknown split {name!r}")
            for preference_id in ids:
                if preference_id in split_of:
                    issues.append(
                        f"pair {preference_id} appears in both {split_of[preference_id]} "
                        f"and {name}"
                    )
                split_of[preference_id] = name

        seen: set[str] = set()
        fingerprints: set[str] = set()
        groups: dict[str, str] = {}
        accepted = 0
        for pair in dataset.examples:
            if pair.preference_id in seen:
                issues.append(f"duplicate preference id {pair.preference_id}")
            seen.add(pair.preference_id)
            if pair.dataset_type != dataset.dataset_type:
                issues.append(
                    f"pair {pair.preference_id} has type {pair.dataset_type}, "
                    f"the dataset has {dataset.dataset_type}"
                )
            if not pair.prompt:
                issues.append(f"pair {pair.preference_id} has no prompt")
            if not pair.chosen or not pair.rejected:
                issues.append(f"pair {pair.preference_id} is missing a candidate")
            elif dict(pair.chosen) == dict(pair.rejected):
                issues.append(
                    f"pair {pair.preference_id} has identical candidates, which teaches "
                    "nothing"
                )
            violations = reasoning_violations(pair.to_dict(), path="pair")
            if violations:
                issues.append(
                    f"pair {pair.preference_id} carries hidden reasoning at "
                    + ", ".join(violations)
                )
            if pair.quality_status == PreferenceQualityStatus.ACCEPTED.value:
                accepted += 1
            if pair.preference_id not in split_of:
                issues.append(f"pair {pair.preference_id} is in no split")
            if not pair.strength.evidence:
                issues.append(f"pair {pair.preference_id} cites no evidence")
            if not _has_provenance(pair):
                issues.append(f"pair {pair.preference_id} has no provenance")
            fingerprint = pair.fingerprint()
            if fingerprint in fingerprints:
                issues.append(
                    f"pair {pair.preference_id} repeats another pair's candidates"
                )
            fingerprints.add(fingerprint)
            where = split_of.get(pair.preference_id, "")
            known_split = groups.setdefault(pair.group_key, where)
            if known_split and where and known_split != where:
                issues.append(
                    f"group {pair.group_key!r} appears in {known_split} and {where}"
                )

        statistics = dataset.statistics
        if statistics.total != len(dataset.examples):
            issues.append(
                f"statistics say {statistics.total} pairs, the dataset has "
                f"{len(dataset.examples)}"
            )
        counted = sum(len(ids) for ids in dataset.splits.values())
        if counted != len(dataset.examples):
            issues.append(
                f"the splits hold {counted} pairs, the dataset has {len(dataset.examples)}"
            )
        if not dataset.source_data_version:
            issues.append("the dataset does not say what it was built from")
        if not dataset.preprocessing_version:
            issues.append("the dataset does not record a preprocessing version")
        if accepted == 0:
            issues.append("no pair in this dataset has been accepted as clean")
        return tuple(issues)


# ── helpers ─────────────────────────────────────────────────────────────────


def _merge(counts: dict[str, int], extra: Mapping[str, int]) -> None:
    for reason, count in extra.items():
        counts[reason] = counts.get(reason, 0) + int(count)


def _statistics(
    pairs: Sequence[PreferenceExample],
    *,
    splits: Mapping[str, tuple[str, ...]],
    skipped: Mapping[str, int],
    source_trajectories: int,
    source_evaluations: int,
) -> PreferenceStatistics:
    by_source: dict[str, int] = {}
    by_difficulty: dict[str, int] = {}
    verified = 0
    total_strength = 0.0
    for pair in pairs:
        by_source[pair.preference_source] = by_source.get(pair.preference_source, 0) + 1
        by_difficulty[pair.difficulty] = by_difficulty.get(pair.difficulty, 0) + 1
        if pair.strength.verified:
            verified += 1
        total_strength += pair.strength.overall
    return PreferenceStatistics(
        total=len(pairs),
        by_difficulty=by_difficulty,
        by_source=by_source,
        by_split={name: len(ids) for name, ids in splits.items() if ids},
        groups=len({pair.group_key for pair in pairs}),
        source_trajectories=source_trajectories,
        source_evaluations=source_evaluations,
        verified_pairs=verified,
        review_required=sum(
            1
            for pair in pairs
            if pair.quality_status == PreferenceQualityStatus.NEEDS_REVIEW.value
        ),
        rejected=sum(
            1
            for pair in pairs
            if pair.quality_status == PreferenceQualityStatus.REJECTED.value
        ),
        duplicates_removed=skipped.get(REASON_DUPLICATE, 0),
        sensitive_removed=skipped.get(REASON_SENSITIVE, 0),
        malformed_removed=skipped.get(REASON_MALFORMED, 0),
        contradictory_removed=skipped.get(REASON_CONTRADICTORY, 0),
        average_strength=round(total_strength / len(pairs), 6) if pairs else 0.0,
        skipped=dict(skipped),
        estimated_tokens=sum(pair.estimated_tokens for pair in pairs),
    )


__all__ = [
    "MAX_CANDIDATES_PER_PROMPT",
    "MAX_ROWS_EXAMINED",
    "REASON_BELOW_CONFIDENCE",
    "REASON_CATEGORY",
    "REASON_CONTRADICTORY",
    "REASON_DATE",
    "REASON_DUPLICATE",
    "REASON_HIDDEN_REASONING",
    "REASON_IDENTICAL",
    "REASON_LOSER_NOT_WORSE",
    "REASON_MALFORMED",
    "REASON_MISSING_DATA",
    "REASON_MISSING_PROVENANCE",
    "REASON_MODEL",
    "REASON_NO_FEEDBACK",
    "REASON_NO_PAIRS",
    "REASON_NO_SECOND_CANDIDATE",
    "REASON_REJECTED",
    "REASON_REVIEW_REQUIRED",
    "REASON_SENSITIVE",
    "REASON_SOURCE",
    "REASON_SOURCE_NOT_ALLOWED",
    "REASON_TAGS",
    "REASON_UNVERIFIED_SOURCE",
    "PreferenceDatasetBuilder",
]
