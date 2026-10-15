"""Comparing what was expected with what was seen — including "cannot tell".

This module is where a learning system most easily lies to itself, so each of the
cases §6 names has an explicit branch and an explicit verdict:

* **no prediction**          agreement is ``unable_to_evaluate`` — not a failure of
                             the experiment, and not silent agreement either;
* **no observation**         ``unable_to_evaluate``. An absent reading is never
                             evidence that an event did not happen;
* **incomplete observation** ``inconclusive`` when the keys the prediction was about
                             were not covered, with the keys NAMED;
* **stale observation**      ``inconclusive``: a reading that predates the action
                             describes the state the action started from;
* **conflicting observation**``inconclusive``: two values for one key is a property
                             of the source, not a fact about the world;
* **environment error**      ``unable_to_evaluate`` with the error kept;
* **different initial state**``inconclusive``: the prediction was about the state the
                             experiment did not have, so matching it would be luck;
* **all predicted keys absent from a complete reading** ``inconclusive``, because a
                             delayed or narrower effect cannot be told from an
                             absent one. A PARTIAL match is ``partially_confirmed``.

``match_hypotheses`` scores each hypothesis against the same reading, and
``evidence_for`` turns those verdicts into evidence references whose ``source_key`` is
the experiment's signature — so re-running one experiment is one source, while a
different experiment that confirms the same thing is genuinely independent (§7).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from novacontrol.interactive.models import (
    ComparisonResult,
    EnvironmentObservation,
    EvidenceKind,
    EvidenceReference,
    EvidenceRole,
    ExperimentSpec,
    Hypothesis,
    MatchingPolicy,
    OutcomeKind,
    Prediction,
    PredictionBasis,
    as_text,
    new_id,
)

__all__ = [
    "evidence_for",
    "evidence_role",
    "compare",
    "match_hypotheses",
    "match_one",
]

#: Values are compared equal within this when nothing else is declared.
_DEFAULT_SIMULATOR_NOTE = (
    "the reading came from a simulator, so this is evidence about the simulator"
)


def _numeric_equal(left: Any, right: Any, tolerance: float) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return bool(left == right)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= tolerance
    if isinstance(left, str) and isinstance(right, str):
        return left.strip() == right.strip()
    return bool(left == right)


def _classify(
    expected: Mapping[str, Any],
    observation: EnvironmentObservation,
    matching: MatchingPolicy,
) -> tuple[OutcomeKind, tuple[str, ...], tuple[str, ...], tuple[str, ...], str]:
    """Match one expectation against one reading.

    Returns the verdict, the matched keys, the mismatched keys, the keys the reading
    did not cover, and the sentence that explains the verdict.
    """
    values = dict(observation.values)
    matched: list[str] = []
    mismatched: list[str] = []
    missing: list[str] = []
    for key, wanted in expected.items():
        if key not in values:
            missing.append(key)
            continue
        if _numeric_equal(values[key], wanted, matching.tolerance_for(key)):
            matched.append(key)
        else:
            mismatched.append(key)
    if mismatched:
        return (
            OutcomeKind.CONTRADICTED if not matched else OutcomeKind.PARTIALLY_CONFIRMED,
            tuple(matched),
            tuple(mismatched),
            tuple(missing),
            (
                f"{len(mismatched)} predicted value(s) did not match the reading"
                if not matched
                else "some predicted values matched and some did not"
            ),
        )
    if matched and not missing:
        return (
            OutcomeKind.CONFIRMED,
            tuple(matched),
            (),
            (),
            f"every predicted value matched the reading ({len(matched)} key(s))",
        )
    if matched and missing:
        return (
            OutcomeKind.PARTIALLY_CONFIRMED,
            tuple(matched),
            (),
            tuple(missing),
            f"{len(matched)} predicted value(s) matched; the reading did not cover "
            f"{sorted(missing)}",
        )
    return (
        OutcomeKind.INCONCLUSIVE,
        (),
        (),
        tuple(missing),
        (
            "the reading did not contain any key the prediction was about; a delayed or "
            "narrower effect cannot be told apart from an absent one"
        ),
    )


def compare(
    prediction: Prediction,
    observation: EnvironmentObservation | None,
    *,
    matching: MatchingPolicy | None = None,
    before: EnvironmentObservation | None = None,
    expected_before: Mapping[str, Any] | None = None,
    started_at: str = "",
) -> ComparisonResult:
    """The verdict on the prediction that was made, with its reasons attached."""
    policy = matching or MatchingPolicy()
    if observation is None:
        return ComparisonResult(
            kind=OutcomeKind.UNABLE_TO_EVALUATE,
            prediction_basis=prediction.basis,
            detail="the environment produced no observation, so nothing could be compared",
            limitations=(
                "an absent reading is not evidence that the event did not occur",
                *prediction.limitations,
            ),
        )
    if not prediction.available:
        return ComparisonResult(
            kind=OutcomeKind.UNABLE_TO_EVALUATE,
            prediction_basis=PredictionBasis.UNAVAILABLE,
            detail=(
                "no prediction was available, so agreement could not be evaluated; the "
                "observation is still recorded as evidence"
            ),
            limitations=("an experiment without a prediction cannot be scored for agreement",),
        )
    notes: list[str] = []
    if observation.conflicting:
        return ComparisonResult(
            kind=OutcomeKind.INCONCLUSIVE,
            prediction_basis=prediction.basis,
            detail=(
                "the source returned conflicting values for the same key, so no verdict "
                "about the prediction can be drawn from it"
            ),
            limitations=(
                "a conflict is a property of the reading, not a fact about the world",
                *prediction.limitations,
            ),
        )
    if started_at and observation.timestamp and observation.timestamp < started_at:
        return ComparisonResult(
            kind=OutcomeKind.INCONCLUSIVE,
            prediction_basis=prediction.basis,
            detail="the observation predates the action, so it describes the state it started from",
            limitations=("a stale reading cannot confirm or contradict a prediction",),
        )
    if expected_before and before is not None:
        drift = [
            key
            for key, wanted in expected_before.items()
            if key in before.values
            and not _numeric_equal(before.values[key], wanted, policy.tolerance_for(key))
        ]
        if drift:
            return ComparisonResult(
                kind=OutcomeKind.INCONCLUSIVE,
                prediction_basis=prediction.basis,
                mismatched=tuple(drift),
                detail=(
                    f"the state before the action differed from the one the prediction "
                    f"assumed (keys: {sorted(drift)}), so matching it would not test the "
                    f"prediction"
                ),
                limitations=("a prediction assumes an initial state; this one did not hold",),
            )
    if not observation.complete and policy.require_complete:
        return ComparisonResult(
            kind=OutcomeKind.INCONCLUSIVE,
            prediction_basis=prediction.basis,
            detail=(
                "the observation did not cover the whole scope, so a value that is absent "
                "from it proves nothing about what happened"
            ),
            limitations=(
                "an incomplete reading cannot confirm or contradict a prediction",
                *prediction.limitations,
            ),
        )
    kind, matched, mismatched, missing, detail = _classify(prediction.expected, observation, policy)
    if observation.quality < 1.0:
        notes.append(f"the reading was partial or noisy (quality {observation.quality:.2f})")
    if observation.kind.value == "simulator":
        notes.append(_DEFAULT_SIMULATOR_NOTE)
    if not observation.complete:
        notes.append("the observation did not cover the whole scope")
    if prediction.basis is PredictionBasis.RULE_BASED:
        notes.append("the prediction came from a rule, not from a model")
    return ComparisonResult(
        kind=kind,
        matched=matched,
        mismatched=mismatched,
        missing=missing,
        detail=detail,
        prediction_basis=prediction.basis,
        limitations=(*notes, *prediction.limitations),
    )


def match_one(
    expected: Mapping[str, Any],
    observation: EnvironmentObservation | None,
    *,
    matching: MatchingPolicy | None = None,
) -> OutcomeKind:
    """One hypothesis' verdict against one reading."""
    if observation is None:
        return OutcomeKind.UNABLE_TO_EVALUATE
    policy = matching or MatchingPolicy()
    if observation.conflicting:
        return OutcomeKind.INCONCLUSIVE
    if not expected:
        return OutcomeKind.INCONCLUSIVE
    if not observation.complete and policy.require_complete:
        covered = set(expected) & set(observation.values)
        if not covered:
            return OutcomeKind.INCONCLUSIVE
    kind, _, _, _, _ = _classify(expected, observation, policy)
    return kind


def match_hypotheses(
    spec: ExperimentSpec,
    observation: EnvironmentObservation | None,
    *,
    matching: MatchingPolicy | None = None,
) -> dict[str, OutcomeKind]:
    """Every tested hypothesis' verdict against the same reading."""
    policy = matching or MatchingPolicy()
    return {
        hypothesis_id: match_one(predicted, observation, matching=policy)
        for hypothesis_id, predicted in spec.predicted_by_hypothesis.items()
    }


def evidence_role(kind: OutcomeKind) -> EvidenceRole:
    """Which way a verdict points.

    ``PARTIALLY_CONFIRMED`` is deliberately INCONCLUSIVE: a prediction that half held
    is not support for the explanation, and treating it as such is how partial noise
    becomes belief.
    """
    if kind is OutcomeKind.CONFIRMED:
        return EvidenceRole.SUPPORTS
    if kind is OutcomeKind.CONTRADICTED:
        return EvidenceRole.CONTRADICTS
    return EvidenceRole.INCONCLUSIVE


def evidence_for(
    spec: ExperimentSpec,
    *,
    observation: EnvironmentObservation | None,
    verdicts: Mapping[str, OutcomeKind],
    comparison: ComparisonResult | None = None,
    reason: str = "",
) -> tuple[EvidenceReference, ...]:
    """Evidence references for one experiment run — one per hypothesis, deduplicated.

    The ``source_key`` is the experiment's signature, so re-running one experiment is
    ONE source however many times it runs (§7). A prediction that was made is recorded
    too, with ``kind=PREDICTION``: it is kept for the report and is never counted as
    support for the hypothesis that made it.
    """
    timestamp = observation.timestamp if observation is not None else ""
    ref = observation.observation_ref if observation is not None else ""
    values = dict(observation.values) if observation is not None else {}
    rows: list[EvidenceReference] = []
    for hypothesis_id in spec.hypothesis_ids:
        kind = verdicts.get(hypothesis_id, OutcomeKind.UNABLE_TO_EVALUATE)
        predicted = dict(spec.predicted_by_hypothesis.get(hypothesis_id, {}))
        observed = {key: values[key] for key in predicted if key in values}
        rows.append(
            EvidenceReference(
                evidence_id=new_id("ev"),
                role=evidence_role(kind),
                kind=EvidenceKind.OBSERVATION,
                detail=(f"{kind.value}: {comparison.detail if comparison is not None else reason}"),
                source_key=spec.experiment_id,
                observation_ref=ref,
                experiment_id=spec.experiment_id,
                hypothesis_id=hypothesis_id,
                timestamp=timestamp,
                values=observed,
            )
        )
        if predicted:
            rows.append(
                EvidenceReference(
                    evidence_id=new_id("evp"),
                    role=EvidenceRole.SUPPORTS,
                    kind=EvidenceKind.PREDICTION,
                    detail=(
                        f"the expectation recorded before the action for {hypothesis_id}: "
                        f"{predicted}"
                    ),
                    source_key=spec.experiment_id,
                    observation_ref="",
                    experiment_id=spec.experiment_id,
                    hypothesis_id=hypothesis_id,
                    timestamp="",
                    values=predicted,
                )
            )
    return tuple(rows)


def drifted_keys(
    before: EnvironmentObservation | None, expected_before: Mapping[str, Any] | None
) -> tuple[str, ...]:
    """Keys whose initial value differed from what a prediction assumed."""
    if before is None or not expected_before:
        return ()
    return tuple(
        key
        for key, wanted in expected_before.items()
        if key in before.values and before.values[key] != wanted
    )


def describe(verdicts: Mapping[str, OutcomeKind]) -> str:
    """One line describing a set of per-hypothesis verdicts — for a report or a log."""
    if not verdicts:
        return "no hypothesis was scored"
    grouped: dict[str, list[str]] = {}
    for hypothesis_id, kind in verdicts.items():
        grouped.setdefault(kind.value, []).append(hypothesis_id)
    return "; ".join(f"{kind}: {len(ids)}" for kind, ids in sorted(grouped.items()))


def hypotheses_of(spec: ExperimentSpec, hypotheses: Sequence[Hypothesis]) -> tuple[Hypothesis, ...]:
    """The hypotheses a spec tests, in the spec's own order."""
    by_id = {item.hypothesis_id: item for item in hypotheses}
    return tuple(by_id[key] for key in spec.hypothesis_ids if key in by_id)


def annotated_result(result: ComparisonResult, note: str) -> ComparisonResult:
    """A comparison with one extra limitation — used when a caller adds context."""
    text = as_text(note)
    if not text or text in result.limitations:
        return result
    return ComparisonResult(
        kind=result.kind,
        matched=result.matched,
        mismatched=result.mismatched,
        missing=result.missing,
        detail=result.detail,
        prediction_basis=result.prediction_basis,
        limitations=(*result.limitations, text),
    )
