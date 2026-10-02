"""Evaluating a preference-optimized model: three models, measured behaviour.

A preference run produces a model that is better at *preferring* — and that is not
the same thing as a model that is better at the job. So the evaluation that can
approve a candidate is a comparison of measured behaviour, and the preference
figure is one metric among them rather than the verdict:

    BASE MODEL  vs  SFT MODEL  vs  PREFERENCE-OPTIMIZED MODEL

each answering the same held-out pairs, scored by the Phase 16 evaluator on the
supervised projection of the dataset (the chosen side is the target), plus two
figures this phase adds:

  * ``preference_accuracy`` — the share of pairs where the model's answer sits
    closer to the CHOSEN output than to the rejected one. A model that answers
    nothing, or answers identically for both sides, scores zero, not a half.
  * ``chosen_match_rate`` / ``rejected_match_rate`` — the two sides of that
    comparison, so a reader can see whether a candidate prefers the right thing or
    merely distances itself from the wrong one.

A model whose LOSS improved proves nothing: no loss figure is consulted anywhere
in this module, and ``loss_consulted`` is recorded as False on every comparison so
a reader does not have to take that on trust.

:meth:`RegressionDetector.check` then answers the question the phase actually
cares about — did anything the installation already did well get worse? — over the
nine capability areas it names, with the blocking ones (task success, verification,
safety, structured output) able to stop an approval on their own.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, cast, runtime_checkable

from novacontrol.preference.models import PreferenceDatasetVersion, PreferenceExample
from novacontrol.training.evaluation import (
    LOWER_IS_BETTER,
    NOISE_FLOOR_METRICS,
    REGRESSION_TOLERANCE,
    CallablePredictor,
    ModelPredictor,
    TrainingEvaluator,
)
from novacontrol.training.models import TRACKED_METRICS, TrainingEvaluation

#: The metrics a preference comparison reports on top of Phase 16's twelve.
#: ``preference_accuracy`` is a tracked metric like the others: a candidate that
#: got WORSE at preferring is a worse preference model, however good its loss
#: curve looked.
PREFERENCE_METRICS: tuple[str, ...] = (
    *TRACKED_METRICS,
    "preference_accuracy",
    "chosen_match_rate",
)

#: Which metrics each capability area answers to. One table, so "did tool
#: selection regress?" has one answer rather than one per caller.
REGRESSION_AREAS: Mapping[str, tuple[str, ...]] = {
    "deterministic_commands": ("task_success",),
    "nlu": ("intent_accuracy", "structured_output_validity"),
    "structured_output": ("structured_output_validity",),
    "decision_routing": ("decision_accuracy",),
    "tool_selection": ("tool_selection_accuracy", "argument_correctness"),
    "planning": ("planning_quality",),
    "verification": ("verification_success",),
    "safety": ("safety",),
    "recovery": ("recovery_quality",),
    "task_success": ("task_success",),
    "preference": ("preference_accuracy",),
}

#: The areas whose regression BLOCKS approval, whatever the preference metrics say.
#: A model that improved its preference accuracy while verified task success fell
#: is exactly the failure this list exists to catch.
REQUIRED_AREAS: frozenset[str] = frozenset(
    {"task_success", "verification", "safety", "structured_output"}
)


@runtime_checkable
class PreferenceScorer(Protocol):
    """A predictor that can score a candidate directly (a real model's log-prob).

    Optional: the evaluator falls back to comparing the model's structured answer
    against each candidate, which is what a generator can actually be measured on.
    A backend that can report a likelihood should implement this, because a
    likelihood is what a preference objective really optimises.
    """

    def score(self, pair: PreferenceExample, candidate: Mapping[str, Any]) -> float: ...


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _match_score(metrics: Mapping[str, float]) -> float:
    """One candidate's match, averaged over the metrics that apply.

    Every metric in the Phase 16 vocabulary is in [0, 1] and higher-is-better
    except latency and memory, which are not in this reading, so a plain mean is
    the honest summary — and the individual figures stay in the delta mapping.
    """
    values = [float(value) for value in metrics.values()]
    return round(sum(values) / len(values), 6) if values else 0.0


@dataclass(frozen=True, slots=True)
class PreferenceReading:
    """One model's measured behaviour on the held-out pairs."""

    model: str = ""
    metrics: Mapping[str, float] = field(default_factory=dict)
    pairs: int = 0
    usable: int = 0
    failed: int = 0
    preference_accuracy: float = 0.0
    chosen_match_rate: float = 0.0
    rejected_match_rate: float = 0.0
    preference_gap: float = 0.0
    ties: int = 0
    scorer: str = "output_similarity"

    @property
    def coverage(self) -> float:
        return round(self.usable / self.pairs, 6) if self.pairs else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "metrics": dict(self.metrics),
            "pairs": self.pairs,
            "usable": self.usable,
            "failed": self.failed,
            "coverage": self.coverage,
            "preference_accuracy": self.preference_accuracy,
            "chosen_match_rate": self.chosen_match_rate,
            "rejected_match_rate": self.rejected_match_rate,
            "preference_gap": self.preference_gap,
            "ties": self.ties,
            "scorer": self.scorer,
        }


@dataclass(frozen=True, slots=True)
class RegressionReport:
    """Whether anything the installation already did well got worse."""

    regressed: tuple[str, ...] = ()
    improved: tuple[str, ...] = ()
    areas_regressed: tuple[str, ...] = ()
    blocking: tuple[str, ...] = ()
    verdict: str = "inconclusive"
    reason: str = ""
    preference_gain: float = 0.0
    notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    @property
    def blocks_approval(self) -> bool:
        return bool(self.blocking) or self.verdict == "regress"

    def to_dict(self) -> dict[str, Any]:
        return {
            "regressed": list(self.regressed),
            "improved": list(self.improved),
            "areas_regressed": list(self.areas_regressed),
            "blocking": list(self.blocking),
            "verdict": self.verdict,
            "reason": self.reason,
            "preference_gain": self.preference_gain,
            "notes": list(self.notes),
            "blocks_approval": self.blocks_approval,
        }


@dataclass(frozen=True, slots=True)
class PreferenceComparison:
    """The three-model comparison, its regression report and its verdict."""

    evaluation: TrainingEvaluation
    base: PreferenceReading
    candidate: PreferenceReading
    sft: PreferenceReading | None = None
    regressions: RegressionReport = field(default_factory=RegressionReport)
    base_to_sft: TrainingEvaluation | None = None
    sft_to_candidate: TrainingEvaluation | None = None
    algorithm: str = ""
    loss_consulted: bool = False
    notes: tuple[str, ...] = ()

    @property
    def verdict(self) -> str:
        return self.evaluation.verdict

    def readings(self) -> dict[str, PreferenceReading]:
        found = {"base": self.base, "candidate": self.candidate}
        if self.sft is not None:
            found["sft"] = self.sft
        return found

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation": self.evaluation.to_dict(),
            "readings": {
                name: reading.to_dict() for name, reading in self.readings().items()
            },
            "regressions": self.regressions.to_dict(),
            "base_to_sft": self.base_to_sft.to_dict() if self.base_to_sft else {},
            "sft_to_candidate": (
                self.sft_to_candidate.to_dict() if self.sft_to_candidate else {}
            ),
            "algorithm": self.algorithm,
            "verdict": self.verdict,
            "loss_consulted": self.loss_consulted,
            "notes": list(self.notes),
        }


def check_regressions(
    baseline: Mapping[str, float],
    candidate: Mapping[str, float],
    *,
    tolerance: float = REGRESSION_TOLERANCE,
    areas: Mapping[str, tuple[str, ...]] | None = None,
    required: Sequence[str] = (),
    preference_metric: str = "preference_accuracy",
) -> RegressionReport:
    """Compare two readings metric by metric, then area by area.

    A metric regresses when it moved past the tolerance in the worse direction —
    down for an accuracy, up for latency or memory. The noise floors Phase 16
    documents (a millisecond of scheduler jitter on wall-clock time) are honoured
    here too, so this cannot call a model worse for being equally good.

    An area regresses when ANY of its metrics did. The blocking areas are checked
    separately, because "improved its preference metrics but broke verified task
    success" is the specific failure this phase has to catch.
    """
    limit = max(0.0, float(tolerance))
    floors = {name: max(limit, floor) for name, floor in NOISE_FLOOR_METRICS.items()}
    table = dict(areas or REGRESSION_AREAS)
    regressed: list[str] = []
    improved: list[str] = []
    for name in {*baseline, *candidate}:
        if name not in baseline or name not in candidate:
            continue
        delta = round(float(candidate[name]) - float(baseline[name]), 6)
        allowance = floors.get(name, limit)
        worse = delta > allowance if name in LOWER_IS_BETTER else delta < -allowance
        better = delta < -allowance if name in LOWER_IS_BETTER else delta > allowance
        if worse:
            regressed.append(name)
        elif better:
            improved.append(name)

    regressed_areas = [
        area
        for area, metrics in table.items()
        if any(metric in set(regressed) for metric in metrics)
    ]
    blocking = [area for area in regressed_areas if area in set(required)]
    gain = round(
        float(candidate.get(preference_metric, 0.0))
        - float(baseline.get(preference_metric, 0.0)),
        6,
    )
    notes: list[str] = []
    if gain > limit and not regressed_areas:
        notes.append(
            "preference accuracy improved without a measured regression elsewhere"
        )
    if gain > limit and any(area in set(blocking) for area in blocking):
        notes.append(
            "preference accuracy improved while a required capability regressed: the "
            "preference gain does not excuse it"
        )
    if blocking:
        verdict = "regress"
        reason = "required capabilities regressed: " + ", ".join(sorted(blocking))
    elif regressed_areas:
        verdict = "regress"
        reason = "regressed on " + ", ".join(sorted(regressed_areas))
    elif not regressed and not improved:
        verdict = "inconclusive"
        reason = (
            "no metric could be compared, or every metric moved within the tolerance"
            if baseline and candidate
            else "one side of the comparison was not measured"
        )
    else:
        verdict = "pass"
        reason = ""
    return RegressionReport(
        regressed=tuple(regressed),
        improved=tuple(improved),
        areas_regressed=tuple(sorted(regressed_areas)),
        blocking=tuple(sorted(blocking)),
        verdict=verdict,
        reason=reason,
        preference_gain=gain,
        notes=tuple(notes),
    )


def as_predictor(predictor: Any) -> ModelPredictor:
    """The predictor itself, or a silent stand-in when the caller sent something else.

    A comparison over HTTP cannot carry a model object — a JSON payload is a
    mapping, and a mapping has no ``predict`` — so the request that arrives
    through :mod:`novacontrol.api.app` is answered with a model that says
    nothing: the comparison is then reported as inconclusive ("nothing was
    measured") rather than letting an ``AttributeError`` escape as a 500. An
    in-process caller that passes a real predictor is untouched.
    """
    if callable(getattr(predictor, "predict", None)):
        return cast("ModelPredictor", predictor)
    return CallablePredictor(
        str(getattr(predictor, "name", "") or "unavailable"), lambda _example: {}
    )


class PreferenceEvaluator:
    """Scores pairs, compares three models, and reports regressions."""

    def __init__(
        self,
        *,
        tolerance: float = REGRESSION_TOLERANCE,
        max_pairs: int = 0,
        clock: Callable[[], float] = time.perf_counter,
        evaluator: TrainingEvaluator | None = None,
    ) -> None:
        self.tolerance = max(0.0, float(tolerance))
        self.max_pairs = max(0, int(max_pairs))
        self._clock = clock
        #: The Phase 16 evaluator does the per-metric work — one definition of
        #: "intent accuracy" and of the noise floors, whichever phase asks.
        self._evaluator = evaluator if evaluator is not None else TrainingEvaluator()

    # -- one model -------------------------------------------------------------

    def pairs_for(
        self, dataset: PreferenceDatasetVersion, split: str
    ) -> tuple[PreferenceExample, ...]:
        """The held-out accepted pairs, bounded by ``max_pairs`` if it is set."""
        found = dataset.split(split)
        if not found:
            found = tuple(dataset.accepted_pairs())
        if self.max_pairs and len(found) > self.max_pairs:
            found = found[: self.max_pairs]
        return found

    def read(
        self,
        dataset: PreferenceDatasetVersion,
        predictor: ModelPredictor,
        *,
        split: str = "test",
    ) -> PreferenceReading:
        """One model's behaviour: the Phase 16 metrics plus preference accuracy.

        One predictor call per pair: the same answer is scored against the chosen
        side and the rejected side, because asking a model twice for the same input
        would measure the difference between two calls rather than two behaviours.
        """
        pairs = self.pairs_for(dataset, split)
        scorer = _scorer_of(predictor)
        totals: dict[str, float] = {}
        counts: dict[str, int] = {}
        latencies: list[float] = []
        chosen_scores: list[float] = []
        rejected_scores: list[float] = []
        preferred = 0
        ties = 0
        usable = 0
        for pair in pairs:
            example = pair.as_sft_example(split=split)
            started = self._clock()
            try:
                prediction = _mapping(predictor.predict(example))
            except Exception:  # noqa: BLE001 - a broken prediction is a wrong one
                prediction = {}
            latencies.append(max(0.0, self._clock() - started))
            if prediction:
                usable += 1
            chosen_metrics = _mapping(self._evaluator.metrics_for(example, prediction))
            rejected_metrics = _mapping(
                self._evaluator.metrics_for(
                    replace(example, target=dict(pair.rejected)), prediction
                )
            )
            for name, value in chosen_metrics.items():
                totals[name] = totals.get(name, 0.0) + float(value)
                counts[name] = counts.get(name, 0) + 1
            if scorer is not None:
                chosen_score = _safe_score(scorer, pair, pair.chosen)
                rejected_score = _safe_score(scorer, pair, pair.rejected)
            else:
                chosen_score = _match_score(chosen_metrics)
                rejected_score = _match_score(rejected_metrics)
            chosen_scores.append(chosen_score)
            rejected_scores.append(rejected_score)
            if chosen_score > rejected_score:
                preferred += 1
            elif chosen_score == rejected_score:
                ties += 1
        metrics = {
            name: round(totals[name] / counts[name], 6)
            for name in totals
            if counts.get(name)
        }
        if latencies:
            metrics["average_latency_ms"] = round(
                sum(latencies) / len(latencies) * 1000.0, 6
            )
        usage = getattr(predictor, "usage", None)
        if callable(usage):
            try:
                reported = _mapping(usage())
                memory = reported.get("memory_bytes")
                if isinstance(memory, (int, float)) and memory >= 0:
                    metrics["average_memory_bytes"] = float(memory)
            except Exception:  # noqa: BLE001 - an unreported figure stays absent
                pass
        total = len(pairs)
        chosen_mean = _mean(chosen_scores)
        rejected_mean = _mean(rejected_scores)
        metrics["preference_accuracy"] = round(preferred / total, 6) if total else 0.0
        metrics["chosen_match_rate"] = chosen_mean
        return PreferenceReading(
            model=str(getattr(predictor, "name", "") or ""),
            metrics=metrics,
            pairs=total,
            usable=usable,
            failed=total - usable,
            preference_accuracy=metrics["preference_accuracy"],
            chosen_match_rate=chosen_mean,
            rejected_match_rate=rejected_mean,
            preference_gap=round(chosen_mean - rejected_mean, 6),
            ties=ties,
            scorer="log_probability" if scorer is not None else "output_similarity",
        )

    # -- comparison ------------------------------------------------------------

    def compare(
        self,
        dataset: PreferenceDatasetVersion,
        base: ModelPredictor,
        candidate: ModelPredictor,
        *,
        sft: ModelPredictor | None = None,
        split: str = "test",
        run_id: str = "",
        model_id: str = "",
        tolerance: float | None = None,
        algorithm: str = "",
    ) -> PreferenceComparison:
        """Base, SFT and candidate on the same held-out pairs.

        The behavioural verdict comes from Phase 16's comparison over the
        supervised projection; the preference metrics are then folded in and the
        verdict is re-derived from both, so a candidate cannot be approved by
        improving one figure while another falls.
        """
        base = as_predictor(base)
        candidate = as_predictor(candidate)
        sft = as_predictor(sft) if sft is not None else None
        limit = self.tolerance if tolerance is None else max(0.0, float(tolerance))
        view = dataset.as_sft_dataset()
        evaluation = self._evaluator.compare(
            view,
            base,
            candidate,
            split=split,
            run_id=run_id,
            model_id=model_id,
            tolerance=limit,
        )
        base_reading = self.read(dataset, base, split=split)
        candidate_reading = self.read(dataset, candidate, split=split)
        sft_reading = self.read(dataset, sft, split=split) if sft is not None else None
        base_to_sft = (
            self._evaluator.compare(
                view, base, sft, split=split, run_id=run_id, model_id=model_id, tolerance=limit
            )
            if sft is not None
            else None
        )
        sft_to_candidate = (
            self._evaluator.compare(
                view, sft, candidate, split=split, run_id=run_id, model_id=model_id, tolerance=limit
            )
            if sft is not None
            else None
        )

        merged_base = {**dict(evaluation.base), **{
            name: base_reading.metrics[name]
            for name in ("preference_accuracy", "chosen_match_rate")
            if name in base_reading.metrics
        }}
        merged_candidate = {**dict(evaluation.candidate), **{
            name: candidate_reading.metrics[name]
            for name in ("preference_accuracy", "chosen_match_rate")
            if name in candidate_reading.metrics
        }}
        deltas = dict(evaluation.deltas)
        regressed = list(evaluation.regressions)
        improved = list(evaluation.improvements)
        # The SAME rule Phase 16 applies, over the two metrics this phase adds:
        # a preference figure that moved past the tolerance counts, and a drop in
        # preference accuracy is a worse preference model, not a neutral fact.
        floors = {name: max(limit, floor) for name, floor in NOISE_FLOOR_METRICS.items()}
        for name in ("preference_accuracy", "chosen_match_rate"):
            if name not in merged_base or name not in merged_candidate:
                continue
            delta = round(merged_candidate[name] - merged_base[name], 6)
            deltas[name] = delta
            allowance = floors.get(name, limit)
            worse = delta > allowance if name in LOWER_IS_BETTER else delta < -allowance
            better = delta < -allowance if name in LOWER_IS_BETTER else delta > allowance
            if worse and name not in regressed:
                regressed.append(name)
            elif better and name not in improved:
                improved.append(name)

        report = check_regressions(
            merged_base, merged_candidate, tolerance=limit, required=sorted(REQUIRED_AREAS)
        )
        notes = list(report.notes)
        if sft is None:
            notes.append(
                "no SFT model was supplied, so the comparison is base versus "
                "candidate: the SFT stage is reported as not measured"
            )
        if base_reading.usable == 0 and candidate_reading.usable == 0:
            verdict = "inconclusive"
            reason = (
                "neither model produced a usable answer on the "
                f"{base_reading.pairs} pairs compared, so there is nothing to compare"
            )
        elif regressed:
            verdict = "regress"
            reason = "regressed on " + ", ".join(sorted(set(regressed)))
        elif not deltas:
            verdict = "inconclusive"
            reason = f"no metric applies to these pairs ({base_reading.pairs} compared)"
        else:
            verdict = "pass"
            reason = ""
        evaluation = replace(
            evaluation,
            base=merged_base,
            candidate=merged_candidate,
            deltas=deltas,
            regressions=tuple(sorted(set(regressed))),
            improvements=tuple(sorted(set(improved))),
            verdict=verdict,
            reason=reason,
            examples_evaluated=min(base_reading.pairs, candidate_reading.pairs),
            base_predictor=base_reading.model or evaluation.base_predictor,
            candidate_predictor=candidate_reading.model or evaluation.candidate_predictor,
            details={
                "algorithm": algorithm,
                "loss_consulted": False,
                "preference_improved": report.preference_gain > limit,
                "preference_gain": report.preference_gain,
                "regressions": report.to_dict(),
                "readings": _readings_payload(base_reading, sft_reading, candidate_reading),
                "notes": notes,
                "verdict_source": "measured behaviour on the held-out pairs",
            },
        )
        return PreferenceComparison(
            evaluation=evaluation,
            base=base_reading,
            candidate=candidate_reading,
            sft=sft_reading,
            regressions=report,
            base_to_sft=base_to_sft,
            sft_to_candidate=sft_to_candidate,
            algorithm=algorithm,
            loss_consulted=False,
            notes=tuple(notes),
        )


def _readings_payload(
    base: PreferenceReading,
    sft: PreferenceReading | None,
    candidate: PreferenceReading,
) -> dict[str, Any]:
    """The three readings as a mapping, omitting a model that was not measured."""
    payload: dict[str, Any] = {"base": base.to_dict(), "candidate": candidate.to_dict()}
    if sft is not None:
        payload["sft"] = sft.to_dict()
    return payload


def _scorer_of(predictor: ModelPredictor) -> Any:
    """A predictor's optional direct scorer, if it has one."""
    scorer = getattr(predictor, "score", None)
    return scorer if callable(scorer) else None


def _safe_score(scorer: Any, pair: PreferenceExample, candidate: Mapping[str, Any]) -> float:
    """A direct score, or ``0.0`` when the scorer fails — never an exception."""
    try:
        value = scorer(pair, dict(candidate))
    except Exception:  # noqa: BLE001 - a broken scorer is not a broken evaluation
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 6) if values else 0.0


__all__ = [
    "PREFERENCE_METRICS",
    "REGRESSION_AREAS",
    "REQUIRED_AREAS",
    "PreferenceComparison",
    "PreferenceEvaluator",
    "PreferenceReading",
    "PreferenceScorer",
    "RegressionReport",
    "check_regressions",
]
