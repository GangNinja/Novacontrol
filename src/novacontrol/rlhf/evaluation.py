"""Evaluating an RL candidate against every model it could replace.

The phase's rule is simple and strict: A HIGHER REWARD IS NOT IMPROVEMENT. The
reward is what the run optimised, and if it were also the verdict, reward hacking
would be indistinguishable from progress. So the verdict comes from measured
BEHAVIOUR on held-out examples, exactly as Phase 16 and 17 do it:

    base model   vs  RL candidate
    SFT model    vs  RL candidate
    DPO/ORPO     vs  RL candidate

Each comparison is Phase 16's :class:`~novacontrol.training.evaluation.
TrainingEvaluator` — the same tracked metrics, the same tolerance, the same
refusal to call two silent models equal. The aggregator here adds what four
models need: per-model readings, a verdict that is the WORST comparison
(any regression is a regression), and the objective-specific evidence the
registry stores — reward integrity, the human and AI preference readings, and
whether a reward figure was consulted (it is not).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from novacontrol.preference.evaluation import as_predictor
from novacontrol.rlhf.models import RLHF_VERSION
from novacontrol.training.evaluation import (
    ModelPredictor,
    TrainingEvaluator,
)
from novacontrol.training.models import SFTDatasetVersion, TrainingEvaluation

#: The areas a regression must never be hidden in. Kept as a report so a reader
#: sees which of them moved, not as an extra gate — the evaluator's tracked
#: metrics already gate the verdict.
CRITICAL_AREAS: tuple[str, ...] = (
    "task_success",
    "verification",
    "tool_selection",
    "planning",
    "recovery",
    "safety",
    "structured_output",
    "nlu",
)

#: The model roles this phase compares, in the order they appear in a report.
BASELINE_ROLES: tuple[str, ...] = ("base", "sft", "preference")


class RLModelEvaluator:
    """Scores up to four models on one held-out split and classifies the change."""

    def __init__(self, evaluator: TrainingEvaluator | None = None) -> None:
        self.evaluator = evaluator if evaluator is not None else TrainingEvaluator()

    def compare(
        self,
        dataset: SFTDatasetVersion,
        *,
        base: ModelPredictor,
        candidate: ModelPredictor,
        sft: ModelPredictor | None = None,
        preference: ModelPredictor | None = None,
        split: str = "test",
        tolerance: float | None = None,
        run_id: str = "",
        model_id: str = "",
        reward_metrics: Mapping[str, Any] | None = None,
    ) -> TrainingEvaluation:
        """Compare the candidate against every baseline that was supplied.

        A comparison with no baselines at all is ``inconclusive`` rather than a
        pass: one model measured alone has not been shown to be better than
        anything.
        """
        examples = dataset.split(split) or dataset.examples
        # A caller may hand a real predictor or a mapping that describes one; a
        # mapping has no ``predict``, so it is normalised to a silent stand-in
        # and the comparison says "nothing was measured" instead of raising.
        candidate_predictor = as_predictor(candidate)
        resolved = {
            "base": as_predictor(base),
            "candidate": candidate_predictor,
            "sft": as_predictor(sft) if sft is not None else None,
            "preference": as_predictor(preference) if preference is not None else None,
        }
        readings: dict[str, dict[str, Any]] = {}
        for role, predictor in resolved.items():
            if predictor is None:
                continue
            readings[role] = self.evaluator.read(examples, predictor)
        comparisons: dict[str, Any] = {}
        baselines: list[str] = []
        for role in ("base", "sft", "preference"):
            predictor = resolved[role]
            if predictor is None:
                continue
            baselines.append(role)
            comparisons[role] = self.evaluator.compare(
                dataset,
                predictor,
                candidate_predictor,
                split=split,
                tolerance=tolerance,
            ).to_dict()
        candidate_metrics = dict(readings.get("candidate", {}).get("metrics", {}))
        base_metrics = dict(readings.get("base", {}).get("metrics", {}))
        deltas = {
            name: round(candidate_metrics[name] - base_metrics[name], 6)
            for name in candidate_metrics
            if name in base_metrics
        }
        regressions: list[str] = []
        improvements: list[str] = []
        for row in comparisons.values():
            regressions.extend(row.get("regressions", ()))
            improvements.extend(row.get("improvements", ()))
        unique_regressions = tuple(dict.fromkeys(regressions))
        unique_improvements = tuple(
            name for name in dict.fromkeys(improvements) if name not in unique_regressions
        )
        verdict, reason = self._verdict(comparisons, baselines, readings)
        details: dict[str, Any] = {
            "phase": "18",
            "phase_version": RLHF_VERSION,
            "comparisons": comparisons,
            "readings": readings,
            "baselines": baselines,
            "critical_areas": list(CRITICAL_AREAS),
            "critical_regressions": [
                name for name in unique_regressions if name in CRITICAL_AREAS
            ],
            "reward_metrics_consulted": False,
            "reward_metrics": dict(reward_metrics or {}),
            "note": (
                "the verdict comes from measured behaviour on held-out examples; "
                "the run's reward readings are recorded beside it and never used "
                "as the verdict"
            ),
        }
        return TrainingEvaluation(
            # A deterministic id, so re-measuring a run replaces its row instead of
            # piling up copies: Phase 16's ``te-`` and Phase 17's ``pe-`` convention.
            evaluation_id=(
                f"re-{run_id}" if run_id else f"re-{dataset.dataset_version_id}"
            ),
            run_id=run_id,
            model_id=model_id,
            dataset_version=dataset.dataset_version_id,
            split=split,
            base=base_metrics,
            candidate=candidate_metrics,
            deltas=deltas,
            regressions=unique_regressions,
            improvements=unique_improvements,
            verdict=verdict,
            reason=reason,
            tolerance=float(comparisons[baselines[0]]["tolerance"]) if baselines else 0.02,
            examples_evaluated=len(examples),
            base_predictor=getattr(base, "name", "base"),
            candidate_predictor=getattr(candidate, "name", "candidate"),
            details=details,
        )

    @staticmethod
    def _verdict(
        comparisons: Mapping[str, Any],
        baselines: Sequence[str],
        readings: Mapping[str, Mapping[str, Any]],
    ) -> tuple[str, str]:
        if not comparisons:
            return (
                "inconclusive",
                "no baseline model was supplied, so there is nothing to compare "
                "the candidate against",
            )
        verdicts = {str(row.get("verdict")) for row in comparisons.values()}
        if "regress" in verdicts:
            return (
                "regress",
                "the candidate regressed against "
                + ", ".join(
                    role
                    for role in baselines
                    if str(comparisons[role].get("verdict")) == "regress"
                ),
            )
        candidate_usable = int(readings.get("candidate", {}).get("usable", 0) or 0)
        if candidate_usable == 0:
            return (
                "inconclusive",
                "the candidate produced no usable prediction on the examples "
                "compared, so a comparison would read as agreement between two "
                "silent models",
            )
        if verdicts == {"pass"}:
            return (
                "pass",
                "no tracked metric regressed against "
                + ", ".join(baselines)
                + " and the candidate answered on "
                + f"{candidate_usable} example(s)",
            )
        return (
            "inconclusive",
            "at least one comparison could not be decided (no tracked metric "
            "applied, or a side answered nothing)",
        )


def regression_report(evaluation: TrainingEvaluation) -> dict[str, Any]:
    """The evaluation as a report: what moved, and where."""
    details = evaluation.details if isinstance(evaluation.details, Mapping) else {}
    return {
        "verdict": evaluation.verdict,
        "reason": evaluation.reason,
        "dataset_version": evaluation.dataset_version,
        "split": evaluation.split,
        "examples": evaluation.examples_evaluated,
        "regressions": list(evaluation.regressions),
        "improvements": list(evaluation.improvements),
        "critical_regressions": list(details.get("critical_regressions", ())),
        "reward_metrics_consulted": bool(details.get("reward_metrics_consulted", False)),
        "readings": dict(details.get("readings", {})) if isinstance(details, Mapping) else {},
    }


__all__ = [
    "BASELINE_ROLES",
    "CRITICAL_AREAS",
    "RLModelEvaluator",
    "regression_report",
]
