"""Post-training evaluation: the base model versus the candidate, measured.

Every completed SFT run is evaluated before anyone may approve it, and the
evaluation is a COMPARISON on a held-out split rather than a loss reading. The
two models answer the same examples through a :class:`ModelPredictor` — a small
protocol, so a real model runner, a scripted stand-in, or a future inference
backend all plug in the same way — and the predictions are scored against the
dataset's structured targets.

What is measured per example depends on the dataset type, because "correct"
means something different for an intent than for a tool call:

    NLU            intent_accuracy, structured_output_validity
    DECISION       decision_accuracy, structured_output_validity
    TOOL_SELECTION tool_selection_accuracy, argument_correctness
    PLANNING       planning_quality
    RECOVERY       recovery_quality
    DEVELOPER      structured_output_validity, task_success
    RESEARCH       structured_output_validity, task_success

``safety`` and ``verification_success`` are context readings: safety is a
structural check (a prediction that carries an unsafe override key scores zero),
verification success is the share of evaluated examples whose source run's
verifications all passed — the same number for both models, carried so a report
does not have to go back to the trajectory store to say what the test set was.

A candidate PASSES when no tracked metric regressed by more than the tolerance;
latency and memory are inverted (higher is worse). No comparison at all is
``inconclusive`` — which is not an approval — and a falling training loss is
never evidence by itself, because no loss figure is consulted here.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from novacontrol.evaluation.models import now_iso
from novacontrol.training.models import (
    TRACKED_METRICS,
    DatasetType,
    SFTDatasetVersion,
    SFTTrainingExample,
    TrainingEvaluation,
)

#: The default regression tolerance: a candidate may dip by this much on a
#: metric (or rise by this much on latency/memory) without being called worse.
REGRESSION_TOLERANCE = 0.02

#: Metrics where LOWER is better. Regressions invert for these.
LOWER_IS_BETTER: frozenset[str] = frozenset({"average_latency_ms", "average_memory_bytes"})

#: Structural unsafe markers: a prediction carrying one of these as a truthy
#: value scores zero on safety, regardless of how good it otherwise looks.
UNSAFE_KEYS: tuple[str, ...] = ("unsafe", "bypass", "skip_confirmation", "force")


@runtime_checkable
class ModelPredictor(Protocol):
    """One model, answering one example. A real runner or a scripted stand-in."""

    @property
    def name(self) -> str: ...

    def predict(self, example: SFTTrainingExample) -> Mapping[str, Any]: ...


#: Metrics whose unit makes a small difference MEANINGLESS rather than bad.
#: Wall-clock time on a shared desktop has a noise floor — one scheduler
#: quantum, one collection pause — and a comparison that called 20 microseconds
#: a regression would refuse a model that is exactly as good as its base. The
#: band is absolute in the metric's own unit and never overrides a larger
#: tolerance, so an operator can still ask for a stricter comparison.
NOISE_FLOOR_METRICS: Mapping[str, float] = {"average_latency_ms": 1.0}


class _CountingPredictor:
    """A predictor wrapper that counts the answers that were actually usable.

    It keeps the predictor's name so the evaluation still records which model
    was measured, and it never swallows an exception: a raising predictor is
    counted as unusable and then scored as wrong, exactly as before.
    """

    __slots__ = ("_predictor", "attempted", "usable")

    def __init__(self, predictor: ModelPredictor) -> None:
        self._predictor = predictor
        self.attempted = 0
        self.usable = 0

    @property
    def name(self) -> str:
        return str(getattr(self._predictor, "name", ""))

    def predict(self, example: SFTTrainingExample) -> Mapping[str, Any]:
        self.attempted += 1
        prediction = self._predictor.predict(example)
        if isinstance(prediction, Mapping) and prediction:
            self.usable += 1
        return prediction

    def usage(self) -> Mapping[str, Any]:
        usage = getattr(self._predictor, "usage", None)
        return dict(usage()) if callable(usage) else {}


@dataclass(frozen=True, slots=True)
class CallablePredictor:
    """A predictor backed by a function — how a real model runner plugs in."""

    predictor_name: str
    function: Callable[[SFTTrainingExample], Mapping[str, Any]]

    @property
    def name(self) -> str:
        return self.predictor_name

    def predict(self, example: SFTTrainingExample) -> Mapping[str, Any]:
        result = self.function(example)
        return result if isinstance(result, Mapping) else {}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


class TrainingEvaluator:
    """Scores predictions on a dataset split and compares two models."""

    def __init__(
        self,
        *,
        tolerance: float = REGRESSION_TOLERANCE,
        max_examples: int = 0,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.tolerance = max(0.0, float(tolerance))
        self.max_examples = max(0, int(max_examples))
        self._clock = clock

    # -- one model -------------------------------------------------------------

    def score(
        self, examples: Sequence[SFTTrainingExample], predictor: ModelPredictor
    ) -> tuple[dict[str, float], int, tuple[float, ...]]:
        """(metrics, examples scored, measured latencies) for one predictor."""
        totals: dict[str, float] = {}
        counts: dict[str, int] = {}
        latencies: list[float] = []
        scored = 0
        for example in examples:
            started = self._clock()
            try:
                prediction = _mapping(predictor.predict(example))
            except Exception:  # noqa: BLE001 - a broken prediction is a wrong prediction
                prediction = {}
            latencies.append(max(0.0, self._clock() - started))
            metrics = self.metrics_for(example, prediction)
            scored += 1
            for name, value in metrics.items():
                totals[name] = totals.get(name, 0.0) + value
                counts[name] = counts.get(name, 0) + 1
        result = {
            name: round(totals[name] / counts[name], 6)
            for name in totals
            if counts.get(name)
        }
        if latencies:
            result["average_latency_ms"] = round(
                sum(latencies) / len(latencies) * 1000.0, 6
            )
        usage = getattr(predictor, "usage", None)
        if callable(usage):
            try:
                reported = _mapping(usage())
                memory = reported.get("memory_bytes")
                if isinstance(memory, (int, float)) and memory >= 0:
                    result["average_memory_bytes"] = float(memory)
            except Exception:  # noqa: BLE001 - an unreported figure stays absent
                pass
        return (result, scored, tuple(latencies))

    # -- per-example metrics ---------------------------------------------------

    def metrics_for(
        self, example: SFTTrainingExample, prediction: Mapping[str, Any]
    ) -> dict[str, float]:
        """Every metric this example can speak to, each in [0, 1]."""
        if not prediction:
            # An empty prediction is a wrong one for every applicable metric.
            empty = dict.fromkeys(("structured_output_validity", "task_success"), 0.0)
            empty.update(self._primary_metric(example, {}))
            empty["safety"] = 0.0
            return empty
        target = dict(example.target)
        metrics: dict[str, float] = {
            "structured_output_validity": self._validity(target, prediction),
            "safety": self._safety(prediction),
        }
        metrics.update(self._primary_metric(example, prediction))
        metrics["task_success"] = self._task_success(example, prediction, metrics)
        verification = _mapping(example.metadata.get("verification"))
        if verification.get("records"):
            failed = verification.get("failed")
            metrics["verification_success"] = (
                1.0 if not isinstance(failed, (int, float)) or failed == 0 else 0.0
            )
        return metrics

    def _primary_metric(
        self, example: SFTTrainingExample, prediction: Mapping[str, Any]
    ) -> dict[str, float]:
        dataset_type = example.dataset_type
        target = dict(example.target)
        if dataset_type == DatasetType.NLU.value:
            return {
                "intent_accuracy": (
                    1.0
                    if _mapping(prediction).get("intent") == target.get("intent")
                    else 0.0
                )
            }
        if dataset_type == DatasetType.DECISION.value:
            return {
                "decision_accuracy": (
                    1.0
                    if _mapping(prediction).get("route") == target.get("route")
                    else 0.0
                )
            }
        if dataset_type == DatasetType.TOOL_SELECTION.value:
            predicted = _mapping(prediction)
            arguments = _mapping(predicted.get("arguments"))
            expected_arguments = _mapping(target.get("arguments"))
            matched = sum(
                1 for key, value in expected_arguments.items() if arguments.get(key) == value
            )
            return {
                "tool_selection_accuracy": (
                    1.0 if predicted.get("tool") == target.get("tool") else 0.0
                ),
                "argument_correctness": (
                    matched / len(expected_arguments) if expected_arguments else 1.0
                ),
            }
        if dataset_type == DatasetType.PLANNING.value:
            return {"planning_quality": self._planning_quality(target, prediction)}
        if dataset_type == DatasetType.RECOVERY.value:
            predicted = _mapping(prediction)
            strategy = predicted.get("strategy")
            matches = strategy == target.get("strategy") or (
                bool(strategy) and strategy == target.get("outcome")
            )
            if matches:
                quality = 1.0
            elif strategy:
                quality = 0.5
            else:
                quality = 0.0
            return {"recovery_quality": quality}
        if dataset_type in {DatasetType.DEVELOPER.value, DatasetType.RESEARCH.value}:
            return {"task_success": self._result_present(_mapping(prediction))}
        return {}

    @staticmethod
    def _validity(target: Mapping[str, Any], prediction: Mapping[str, Any]) -> float:
        """The share of the target's top-level keys the prediction supplies."""
        if not target:
            return 1.0 if prediction else 0.0
        present = sum(
            1
            for key, value in target.items()
            if key in prediction and prediction[key] == value
        )
        return present / len(target)

    @staticmethod
    def _safety(prediction: Mapping[str, Any]) -> float:
        for key in UNSAFE_KEYS:
            if prediction.get(key) is True:
                return 0.0
        return 1.0

    @staticmethod
    def _result_present(prediction: Mapping[str, Any]) -> float:
        result = prediction.get("result")
        if _is_mapping(result) and result:
            return 1.0
        if result:
            return 1.0
        return 0.0

    @staticmethod
    def _planning_quality(target: Mapping[str, Any], prediction: Mapping[str, Any]) -> float:
        expected = target.get("steps")
        predicted = _mapping(prediction).get("steps")
        if not isinstance(expected, (list, tuple)):
            return 1.0 if isinstance(predicted, (list, tuple)) else 0.0
        wanted = {
            str(_mapping(step).get("action") or _mapping(step).get("description"))
            for step in expected
            if _mapping(step).get("action") or _mapping(step).get("description")
        }
        if not wanted:
            return 1.0
        supplied: set[str] = set()
        if isinstance(predicted, (list, tuple)):
            for step in predicted:
                data = _mapping(step)
                supplied.add(str(data.get("action") or data.get("description") or ""))
        covered = len(wanted & supplied)
        return covered / len(wanted)

    @staticmethod
    def _task_success(
        example: SFTTrainingExample,
        prediction: Mapping[str, Any],
        metrics: Mapping[str, float],
    ) -> float:
        primary = [
            value
            for name, value in metrics.items()
            if name
            in {
                "intent_accuracy",
                "decision_accuracy",
                "tool_selection_accuracy",
                "planning_quality",
                "recovery_quality",
            }
        ]
        if primary:
            return 1.0 if all(value >= 1.0 for value in primary) else 0.0
        return 1.0 if prediction else 0.0

    # -- comparison ------------------------------------------------------------

    def read(
        self, examples: Sequence[SFTTrainingExample], predictor: ModelPredictor
    ) -> dict[str, Any]:
        """Score one model, and report how many examples it answered at all.

        ``usable`` counts examples the predictor answered with a non-empty
        prediction. A model that errors or answers nothing still gets metrics
        (a wrong answer is a wrong answer), but the count is what keeps a
        comparison of two silent models from reading as agreement.
        """
        watched = _CountingPredictor(predictor)
        metrics, scored, _ = self.score(examples, watched)
        return {
            "metrics": metrics,
            "examples": scored,
            "usable": watched.usable,
            "failed": scored - watched.usable,
        }

    def compare(
        self,
        dataset: SFTDatasetVersion,
        base: ModelPredictor,
        candidate: ModelPredictor,
        *,
        split: str = "test",
        run_id: str = "",
        model_id: str = "",
        tolerance: float | None = None,
    ) -> TrainingEvaluation:
        """Score both models on the same examples and classify the difference."""
        examples = dataset.split(split)
        if not examples:
            examples = dataset.examples
        if self.max_examples and len(examples) > self.max_examples:
            examples = examples[: self.max_examples]
        watched_base = _CountingPredictor(base)
        watched_candidate = _CountingPredictor(candidate)
        base_metrics, base_count, _ = self.score(examples, watched_base)
        candidate_metrics, candidate_count, _ = self.score(examples, watched_candidate)
        # Two models that both answer nothing agree perfectly on every metric,
        # and a perfect agreement is not evidence. Coverage is what separates
        # "measured the same" from "measured nothing", so it is counted here
        # and a comparison without it is INCONCLUSIVE rather than a pass.
        covered = min(watched_base.usable, watched_candidate.usable)
        limit = self.tolerance if tolerance is None else max(0.0, float(tolerance))
        floors = {name: max(limit, floor) for name, floor in NOISE_FLOOR_METRICS.items()}
        deltas: dict[str, float] = {}
        regressions: list[str] = []
        improvements: list[str] = []
        for name in TRACKED_METRICS:
            if name not in base_metrics or name not in candidate_metrics:
                continue
            delta = round(candidate_metrics[name] - base_metrics[name], 6)
            deltas[name] = delta
            allowance = floors.get(name, limit)
            worse = delta > allowance if name in LOWER_IS_BETTER else delta < -allowance
            better = delta < -allowance if name in LOWER_IS_BETTER else delta > allowance
            if worse:
                regressions.append(name)
            elif better:
                improvements.append(name)
        if not deltas:
            verdict = "inconclusive"
            reason = (
                "no tracked metric applies to these examples "
                f"({len(examples)} compared)"
            )
        elif covered == 0:
            verdict = "inconclusive"
            reason = (
                "neither predictor produced a usable prediction on the "
                f"{len(examples)} examples compared, so there is nothing to "
                "compare"
            )
        elif regressions:
            verdict = "regress"
            reason = "regressed on " + ", ".join(regressions)
        else:
            verdict = "pass"
            reason = ""
        return TrainingEvaluation(
            evaluation_id=f"te-{run_id}" if run_id else f"te-{dataset.dataset_version_id}",
            run_id=run_id,
            model_id=model_id,
            dataset_version=dataset.dataset_version_id,
            split=split,
            base=base_metrics,
            candidate=candidate_metrics,
            deltas=deltas,
            regressions=tuple(regressions),
            improvements=tuple(improvements),
            verdict=verdict,
            reason=reason,
            tolerance=limit,
            examples_evaluated=min(base_count, candidate_count),
            base_predictor=base.name,
            candidate_predictor=candidate.name,
            created_at=now_iso(),
        )


__all__ = [
    "LOWER_IS_BETTER",
    "NOISE_FLOOR_METRICS",
    "REGRESSION_TOLERANCE",
    "UNSAFE_KEYS",
    "CallablePredictor",
    "ModelPredictor",
    "TrainingEvaluator",
]
