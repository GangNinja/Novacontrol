"""The evaluation service: one place where a finished run becomes evidence.

The pieces each do one job — the recorder builds a trajectory from the
lifecycle, the quality filter classifies it, the evaluator scores nine
dimensions, the reward engine turns those into a weighted total, the
repositories store the three rows. This service is the ORDER they run in, and
the only component that knows all of them:

    trajectory → quality verdict → evaluation → reward → stored rows

Two properties matter more than the wiring:

  * **a failure here cannot break the run.** The recorder hands trajectories in
    from the event bus; every step below is wrapped, a failure is counted and
    logged, and the request that produced the trajectory has already finished
    anyway. The layer that learns from work must never be able to break the work.
  * **rows are written once, and updated in place.** The evaluation id and the
    reward id are derived from the trajectory id, so a late annotation (the
    measured latency arriving after the task ended) REPLACES its earlier rows
    rather than leaving two results that disagree.

Retention is applied at start rather than on every write: pruning belongs to a
boot, not to a request path.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from novacontrol.evaluation.datasets import (
    BUILTIN_DATASET_ID,
    GoldenDataset,
    builtin_golden_dataset,
)
from novacontrol.evaluation.evaluator import EVALUATOR_VERSION, EvaluationEngine, EvaluationResult
from novacontrol.evaluation.metrics import MetricsCalculator
from novacontrol.evaluation.models import (
    GOLDEN_DATASET_SCHEMA_VERSION,
    TRAJECTORY_SCHEMA_VERSION,
    AgentTrajectory,
    UserFeedback,
    now_iso,
)
from novacontrol.evaluation.quality import DataQualityFilter, QualityVerdict
from novacontrol.evaluation.recorder import TrajectoryRecorder
from novacontrol.evaluation.reward import REWARD_VERSION, RewardEngine, RewardResult
from novacontrol.evaluation.storage import (
    DEFAULT_REPOSITORY_CAP,
    DatasetRepository,
    EvaluationRepository,
    RewardRepository,
    TrajectoryRepository,
)

_logger = logging.getLogger(__name__)

#: The custom event types this service publishes. They are NOT part of the
#: lifecycle vocabulary: they describe the evaluation layer's own progress, and
#: a subscriber that does not care about them simply never subscribes.
TRAJECTORY_RECORDED = "trajectory.recorded"
EVALUATION_COMPLETED = "evaluation.completed"
REWARD_COMPUTED = "reward.computed"
RETENTION_APPLIED = "evaluation.retention_applied"

Publisher = Callable[[str, Mapping[str, Any]], None]


@dataclass(slots=True)
class _Outcome:
    """What processing one trajectory produced."""

    trajectory: AgentTrajectory
    verdict: QualityVerdict
    evaluation: EvaluationResult | None = None
    reward: RewardResult | None = None


class EvaluationService:
    """Records, filters, evaluates and stores — in that order, failure-isolated."""

    def __init__(
        self,
        *,
        recorder: TrajectoryRecorder | None = None,
        trajectories: TrajectoryRepository | None = None,
        evaluations: EvaluationRepository | None = None,
        rewards: RewardRepository | None = None,
        datasets: DatasetRepository | None = None,
        evaluator: EvaluationEngine | None = None,
        reward_engine: RewardEngine | None = None,
        quality: DataQualityFilter | None = None,
        metrics: MetricsCalculator | None = None,
        dataset: GoldenDataset | None = None,
        retention_days: int = 30,
        max_records: int = DEFAULT_REPOSITORY_CAP,
        evaluate: bool = True,
        compute_rewards: bool = True,
        publish: Publisher | None = None,
    ) -> None:
        self.recorder = recorder if recorder is not None else TrajectoryRecorder()
        self.trajectories = trajectories if trajectories is not None else TrajectoryRepository()
        self.evaluations = evaluations if evaluations is not None else EvaluationRepository()
        self.rewards = rewards if rewards is not None else RewardRepository()
        self.datasets = datasets if datasets is not None else DatasetRepository()
        self.evaluator = evaluator if evaluator is not None else EvaluationEngine()
        self.reward_engine = reward_engine if reward_engine is not None else RewardEngine()
        self.quality = quality if quality is not None else DataQualityFilter()
        self.metrics = metrics if metrics is not None else MetricsCalculator()
        self.dataset = dataset if dataset is not None else builtin_golden_dataset()
        self.retention_days = max(0, int(retention_days))
        self.max_records = max(0, int(max_records))
        self.evaluate_enabled = bool(evaluate)
        self.reward_enabled = bool(compute_rewards)
        self._publish = publish
        #: Counters a status surface reads. ``failures`` is the honest one: work
        #: that could not be learned from, counted rather than hidden.
        self.failures = 0
        self.processed = 0
        # The recorder hands every finished trajectory to this service. A
        # recorder built without a sink still works; one built with this sink
        # cannot be the reason a request failed (see ``_on_trajectory``).
        self.recorder.set_sink(self._on_trajectory)

    # -- configuration ---------------------------------------------------------

    @property
    def recording_enabled(self) -> bool:
        return self.recorder.enabled

    def set_recording(self, enabled: bool) -> bool:
        return self.recorder.set_enabled(enabled)

    def set_publisher(self, publish: Publisher | None) -> None:
        self._publish = publish

    def set_retention(
        self, *, days: int | None = None, max_records: int | None = None
    ) -> dict[str, Any]:
        """Change retention. Applied by :meth:`apply_retention`, not here."""
        if days is not None:
            self.retention_days = max(0, int(days))
        if max_records is not None:
            self.max_records = max(0, int(max_records))
        return {"retention_days": self.retention_days, "max_records": self.max_records}

    # -- bus -------------------------------------------------------------------

    async def start(self, event_bus: Any) -> None:
        """Attach the recorder to the bus, then apply retention once."""
        await self.recorder.attach(event_bus)
        self.apply_retention()

    async def stop(self, event_bus: Any | None = None) -> None:
        await self.recorder.detach(event_bus)

    # -- recording -------------------------------------------------------------

    def _on_trajectory(self, trajectory: AgentTrajectory) -> None:
        """The recorder's sink. Never raises: it runs inside an observer."""
        try:
            self.record(trajectory)
        except Exception as exc:  # noqa: BLE001 - learning must not break the work
            self.failures += 1
            _logger.warning(
                "Evaluation service could not process trajectory %s: %s: %s",
                trajectory.trajectory_id,
                type(exc).__name__,
                exc,
            )

    def record(self, trajectory: AgentTrajectory) -> dict[str, Any]:
        """Classify, evaluate, reward and store one trajectory."""
        outcome = self._process(trajectory)
        self.processed += 1
        return {
            "trajectory_id": outcome.trajectory.trajectory_id,
            "task_id": outcome.trajectory.task_id,
            "quality": outcome.verdict.to_dict(),
            "evaluation_id": outcome.evaluation.evaluation_id if outcome.evaluation else "",
            "reward_id": outcome.reward.reward_id if outcome.reward else "",
            "total_reward": outcome.reward.total_reward if outcome.reward else None,
        }

    def _process(self, trajectory: AgentTrajectory) -> _Outcome:
        verdict = self.quality.assess(trajectory)
        row = trajectory.with_quality(verdict.to_dict())
        evaluation: EvaluationResult | None = None
        reward: RewardResult | None = None
        if self.evaluate_enabled:
            expected = self._expected_for(trajectory)
            evaluation = self.evaluator.evaluate(row, expected=expected)
            row = _with_field(row, "evaluation_id", evaluation.evaluation_id)
            self.evaluations.save(evaluation)
        if self.reward_enabled:
            reward = self.reward_engine.score(row, evaluation)
            row = row.with_reward(reward.to_dict())
            self.rewards.save(reward)
        self.trajectories.save(row)
        self._announce(row, verdict, evaluation, reward)
        return _Outcome(row, verdict, evaluation, reward)

    def _expected_for(self, trajectory: AgentTrajectory) -> Any:
        """The golden example this run was made for, when it names one."""
        named = str(trajectory.metadata.get("golden_example_id", "")).strip()
        if not named:
            return None
        return self.dataset.example(named)

    def _announce(
        self,
        trajectory: AgentTrajectory,
        verdict: QualityVerdict,
        evaluation: EvaluationResult | None,
        reward: RewardResult | None,
    ) -> None:
        if self._publish is None:
            return
        events: list[tuple[str, dict[str, Any]]] = [
            (
                TRAJECTORY_RECORDED,
                {
                    "trajectory_id": trajectory.trajectory_id,
                    "task_id": trajectory.task_id,
                    "status": trajectory.status,
                    "success": trajectory.success,
                    "quality": verdict.verdict,
                },
            )
        ]
        if evaluation is not None:
            events.append(
                (
                    EVALUATION_COMPLETED,
                    {
                        "evaluation_id": evaluation.evaluation_id,
                        "trajectory_id": evaluation.trajectory_id,
                        "overall_status": evaluation.overall_status,
                        "scores": evaluation.scores(),
                    },
                )
            )
        if reward is not None:
            events.append(
                (
                    REWARD_COMPUTED,
                    {
                        "reward_id": reward.reward_id,
                        "trajectory_id": reward.trajectory_id,
                        "total_reward": reward.total_reward,
                        "normalized_reward": reward.normalized_reward,
                    },
                )
            )
        for type_, payload in events:
            try:
                self._publish(type_, payload)
            except Exception as exc:  # noqa: BLE001 - announcing is not the work
                self.failures += 1
                _logger.warning(
                    "Could not publish %s: %s: %s", type_, type(exc).__name__, exc
                )

    def feedback(
        self,
        trajectory_id: str,
        *,
        rating: int | None = None,
        label: str = "",
        comment: str = "",
        source: str = "operator",
    ) -> AgentTrajectory | None:
        """Attach a person's verdict to a stored run and re-score it.

        Feedback is NOT part of the reward formula — it is the signal a future
        phase trains against, and folding it into the reward now would make the
        reward report a person's opinion as if it were a measurement.
        """
        row = self.trajectories.get(trajectory_id)
        if row is None:
            return None
        updated = row.with_feedback(
            UserFeedback(
                rating=rating,
                label=str(label or ""),
                comment=str(comment or ""),
                source=str(source or "operator"),
                timestamp=now_iso(),
            )
        )
        evaluation = self.evaluations.for_trajectory(trajectory_id)
        reward = self.reward_engine.score(updated, evaluation) if self.reward_enabled else None
        if reward is not None:
            updated = updated.with_reward(reward.to_dict())
            self.rewards.save(reward)
        self.trajectories.save(updated)
        return updated

    # -- reading ---------------------------------------------------------------

    def trajectory(self, trajectory_id: str) -> dict[str, Any] | None:
        """One stored run: the trajectory, its evaluation and its reward."""
        row = self.trajectories.get(trajectory_id)
        if row is None:
            return None
        evaluation = self.evaluations.for_trajectory(row.trajectory_id)
        reward = self.rewards.for_trajectory(row.trajectory_id)
        return {
            "trajectory": row.to_dict(),
            "evaluation": evaluation.to_dict() if evaluation is not None else None,
            "reward": reward.to_dict() if reward is not None else None,
        }

    def list_trajectories(
        self,
        *,
        limit: int = 20,
        task_id: str = "",
        model: str = "",
        status: str = "",
        since: str = "",
        until: str = "",
    ) -> tuple[AgentTrajectory, ...]:
        return self.trajectories.list(
            limit=limit,
            task_id=task_id,
            model=model,
            status=status,
            since=since,
            until=until,
        )

    def list_rewards(
        self,
        *,
        limit: int = 20,
        min_total: float | None = None,
        max_total: float | None = None,
        since: str = "",
        until: str = "",
    ) -> tuple[RewardResult, ...]:
        return self.rewards.list(
            limit=limit, min_total=min_total, max_total=max_total, since=since, until=until
        )

    def list_evaluations(
        self, *, limit: int = 20, status: str = ""
    ) -> tuple[EvaluationResult, ...]:
        return self.evaluations.list(limit=limit, status=status)

    def metrics_snapshot(self) -> dict[str, Any]:
        """Every aggregate figure over what is stored right now."""
        trajectories = self.trajectories.list()
        evaluations = tuple(self.evaluations.list())
        rewards = tuple(self.rewards.list())
        verdicts = tuple(
            QualityVerdict.from_dict(dict(row.quality))
            for row in trajectories
            if isinstance(row.quality, Mapping)
        )
        snapshot = self.metrics.compute(trajectories, evaluations, rewards, verdicts)
        snapshot["stored"] = {
            "trajectories": self.trajectories.count(),
            "evaluations": self.evaluations.count(),
            "rewards": self.rewards.count(),
        }
        return snapshot

    def summary(self) -> dict[str, Any]:
        """The status surface: what is recorded, what it scored, what is held."""
        snapshot = self.metrics_snapshot()
        return {
            "recording": self.status(),
            "trajectories": {
                "total": snapshot["trajectories"],
                "completed": snapshot["completed"],
                "failed": snapshot["failed"],
                "undecided": snapshot["undecided"],
                "stored": snapshot["stored"]["trajectories"],
                "task_success_rate": snapshot["task_success_rate"],
                "failure_rate": snapshot["failure_rate"],
                "retry_rate": snapshot["retry_rate"],
                "fast_path_percentage": snapshot["fast_path_percentage"],
                "llm_escalation_percentage": snapshot["llm_escalation_percentage"],
                "average_latency_ms": snapshot["average_latency_ms"],
                "p50_latency_ms": snapshot["p50_latency_ms"],
                "p95_latency_ms": snapshot["p95_latency_ms"],
            },
            "evaluations": {
                "stored": snapshot["stored"]["evaluations"],
                "overall_status": snapshot["overall_status"],
                "dimension_scores": snapshot["dimension_scores"],
            },
            "rewards": {
                "stored": snapshot["stored"]["rewards"],
                "average": snapshot["average_reward"],
            },
            "quality": snapshot["quality"],
            "datasets": {
                "active": self.dataset.dataset_id,
                "version": self.dataset.version,
                "examples": len(self.dataset),
                "stored_versions": len(self.datasets.versions(self.dataset.dataset_id)),
                "builtin": BUILTIN_DATASET_ID,
            },
            "retention": {
                "days": self.retention_days,
                "max_records": self.max_records,
            },
            "versions": {
                # The trajectory schema this build WRITES — not the dataset's.
                # Both are 1 today; reading the wrong one would only be noticed
                # on the day they diverge.
                "trajectory_schema": TRAJECTORY_SCHEMA_VERSION,
                "golden_dataset_schema": GOLDEN_DATASET_SCHEMA_VERSION,
                "evaluator": EVALUATOR_VERSION,
                "reward": REWARD_VERSION,
            },
            "privacy": {
                "redaction_enabled": self.recorder.status()["redaction_enabled"],
                "stores_chain_of_thought": False,
                "recording_can_be_disabled": True,
            },
        }

    def status(self) -> dict[str, Any]:
        """The recorder's own state, plus this service's counters."""
        state = self.recorder.status()
        state.update(
            {
                "processed": self.processed,
                "failures": self.failures,
                "evaluating": self.evaluate_enabled,
                "rewarding": self.reward_enabled,
                "stored": {
                    "trajectories": self.trajectories.count(),
                    "evaluations": self.evaluations.count(),
                    "rewards": self.rewards.count(),
                },
            }
        )
        return state

    # -- datasets --------------------------------------------------------------

    def golden_dataset(self) -> GoldenDataset:
        """The dataset measurements are taken against.

        A stored version of the built-in dataset WINS over the built-in copy:
        an operator (or a later phase) that publishes a revised set of
        expectations gets measured against it without editing code.
        """
        stored = self.datasets.latest(self.dataset.dataset_id)
        if stored is not None and len(stored) > 0:
            return stored
        return self.dataset

    def publish_dataset(self, dataset: GoldenDataset) -> GoldenDataset:
        """Store a dataset version. Versions accumulate; nothing is overwritten."""
        return self.datasets.save(dataset)

    def datasets_list(self) -> tuple[GoldenDataset, ...]:
        return self.datasets.list()

    # -- retention -------------------------------------------------------------

    def apply_retention(self) -> dict[str, Any]:
        """Apply the configured retention to the three stores. Called at start."""
        removed = {
            "trajectories": self.trajectories.prune(
                max_records=self.max_records, older_than_days=self.retention_days
            ),
            "evaluations": self.evaluations.prune(
                max_records=self.max_records, older_than_days=self.retention_days
            ),
            "rewards": self.rewards.prune(
                max_records=self.max_records, older_than_days=self.retention_days
            ),
        }
        if any(removed.values()):
            self._announce_removal(removed)
        return removed

    def _announce_removal(self, removed: Mapping[str, int]) -> None:
        if self._publish is None:
            return
        try:
            self._publish(
                RETENTION_APPLIED,
                {"removed": dict(removed), "retention_days": self.retention_days},
            )
        except Exception as exc:  # noqa: BLE001 - announcing is not the work
            self.failures += 1
            _logger.warning(
                "Could not publish %s: %s: %s", RETENTION_APPLIED, type(exc).__name__, exc
            )


def _with_field(trajectory: AgentTrajectory, name: str, value: Any) -> AgentTrajectory:
    return replace(trajectory, **{name: value})


__all__ = [
    "EVALUATION_COMPLETED",
    "RETENTION_APPLIED",
    "REWARD_COMPUTED",
    "TRAJECTORY_RECORDED",
    "EvaluationService",
]
