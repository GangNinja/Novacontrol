"""RLHF/RLAIF on the bus (Phase 18's module seam).

Same shape as Phase 16's training module and Phase 17's preference module, for
the same reasons: the manager is useful as a library, and the rest of the build
talks in events. This module answers the questions a UI, a CLI or another module
asks — status, feedback, ratings, disagreements, datasets, algorithms, an
estimate, a pipeline plan — with a correlated reply each.

Three things are deliberately NOT on the bus:

  * a FEEDBACK DECISION and a RATING. Both change what a future dataset may
    train on and both must carry a name; a bus message is too easy to send
    anonymously, so they go through the application where the caller is part of
    the call.
  * STARTING a run and APPROVING a model. Phase 16's reason holds: both are
    long, expensive and explicitly confirmed.

The module never starts training, never loads a model and never promotes
anything — the queries it answers are read-only by construction.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import Capability
from novacontrol.rlhf.manager import RLHFManager
from novacontrol.rlhf.storage import build_rlhf_repositories
from novacontrol.training.storage import build_training_repositories

STATUS_REQUESTED = "rlhf.status_requested"
STATUS_COMPLETED = "rlhf.status_completed"
FEEDBACK_REQUESTED = "rlhf.feedback_requested"
FEEDBACK_COMPLETED = "rlhf.feedback_completed"
RATINGS_REQUESTED = "rlhf.ratings_requested"
RATINGS_COMPLETED = "rlhf.ratings_completed"
DISAGREEMENTS_REQUESTED = "rlhf.disagreements_requested"
DISAGREEMENTS_COMPLETED = "rlhf.disagreements_completed"
DATASETS_REQUESTED = "rlhf.datasets_requested"
DATASETS_COMPLETED = "rlhf.datasets_completed"
ALGORITHMS_REQUESTED = "rlhf.algorithms_requested"
ALGORITHMS_COMPLETED = "rlhf.algorithms_completed"
ESTIMATE_REQUESTED = "rlhf.estimate_requested"
ESTIMATE_COMPLETED = "rlhf.estimate_completed"
PIPELINE_REQUESTED = "rlhf.pipeline_requested"
PIPELINE_COMPLETED = "rlhf.pipeline_completed"


class RLHFModule:
    """Runtime module for the RLHF/RLAIF subsystem."""

    def __init__(self, manager: RLHFManager | None = None) -> None:
        self.manager = manager if manager is not None else _unavailable_manager()
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "rlhf"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability("rlhf.status", "Report the RLHF/RLAIF subsystem's state."),
            Capability("rlhf.feedback", "List recorded human feedback."),
            Capability("rlhf.ratings", "List recorded AI ratings."),
            Capability("rlhf.disagreements", "List detected source disagreements."),
            Capability("rlhf.datasets", "List built reward datasets."),
            Capability("rlhf.algorithms", "Describe the modes and policy optimizers."),
            Capability("rlhf.estimate", "Price an RL configuration."),
            Capability("rlhf.pipeline", "Plan the RLHF/RLAIF pipeline."),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        await event_bus.subscribe(STATUS_REQUESTED, self._handle_status)
        await event_bus.subscribe(FEEDBACK_REQUESTED, self._handle_feedback)
        await event_bus.subscribe(RATINGS_REQUESTED, self._handle_ratings)
        await event_bus.subscribe(DISAGREEMENTS_REQUESTED, self._handle_disagreements)
        await event_bus.subscribe(DATASETS_REQUESTED, self._handle_datasets)
        await event_bus.subscribe(ALGORITHMS_REQUESTED, self._handle_algorithms)
        await event_bus.subscribe(ESTIMATE_REQUESTED, self._handle_estimate)
        await event_bus.subscribe(PIPELINE_REQUESTED, self._handle_pipeline)

    async def stop(self) -> None:
        bus = self._event_bus
        if bus is not None:
            for type_, handler in (
                (STATUS_REQUESTED, self._handle_status),
                (FEEDBACK_REQUESTED, self._handle_feedback),
                (RATINGS_REQUESTED, self._handle_ratings),
                (DISAGREEMENTS_REQUESTED, self._handle_disagreements),
                (DATASETS_REQUESTED, self._handle_datasets),
                (ALGORITHMS_REQUESTED, self._handle_algorithms),
                (ESTIMATE_REQUESTED, self._handle_estimate),
                (PIPELINE_REQUESTED, self._handle_pipeline),
            ):
                try:
                    await bus.unsubscribe(type_, handler)
                except Exception:  # noqa: BLE001 - a stop must always complete
                    continue
        self._event_bus = None

    # -- handlers --------------------------------------------------------------

    async def _handle_status(self, event: Event) -> None:
        await self._reply(STATUS_COMPLETED, self.manager.status(), event)

    async def _handle_feedback(self, event: Event) -> None:
        count = self._limit(event, 20)
        pending = event.payload.get("pending_only")
        rows = self.manager.feedback_list(
            pending_only=bool(pending) if isinstance(pending, bool) else False,
            limit=count,
        )
        await self._reply(
            FEEDBACK_COMPLETED,
            {"feedback": rows, "stats": self.manager.feedback_stats()},
            event,
        )

    async def _handle_ratings(self, event: Event) -> None:
        count = self._limit(event, 20)
        await self._reply(
            RATINGS_COMPLETED,
            {
                "ratings": self.manager.ratings_list(limit=count),
                "stats": self.manager.rating_stats(),
            },
            event,
        )

    async def _handle_disagreements(self, event: Event) -> None:
        count = self._limit(event, 20)
        await self._reply(
            DISAGREEMENTS_COMPLETED,
            {
                "disagreements": self.manager.disagreements_list(limit=count),
                "stats": self.manager.disagreement_stats(),
            },
            event,
        )

    async def _handle_datasets(self, event: Event) -> None:
        count = self._limit(event, 20)
        datasets = [
            item.to_dict() for item in self.manager.reward_datasets_list(limit=count)
        ]
        await self._reply(DATASETS_COMPLETED, {"datasets": datasets}, event)

    async def _handle_algorithms(self, event: Event) -> None:
        await self._reply(ALGORITHMS_COMPLETED, self.manager.algorithms(), event)

    async def _handle_estimate(self, event: Event) -> None:
        config = event.payload.get("config")
        payload = self.manager.estimate_config(
            config if isinstance(config, Mapping) else {}
        )
        await self._reply(ESTIMATE_COMPLETED, payload, event)

    async def _handle_pipeline(self, event: Event) -> None:
        config = event.payload.get("config")
        dataset_version = event.payload.get("dataset_version")
        payload = self.manager.pipeline_plan(
            config if isinstance(config, Mapping) else {},
            dataset_version if isinstance(dataset_version, str) else "",
        )
        await self._reply(PIPELINE_COMPLETED, payload, event)

    # -- plumbing --------------------------------------------------------------

    @staticmethod
    def _limit(event: Event, default: int) -> int:
        limit = event.payload.get("limit")
        return int(limit) if isinstance(limit, int) and limit > 0 else default

    async def _reply(
        self, type_: str, payload: Mapping[str, Any], source_event: Event
    ) -> None:
        if self._event_bus is None:
            return
        try:
            await self._event_bus.publish(
                Event(
                    type=type_,
                    payload=dict(payload),
                    source="rlhf",
                    correlation_id=source_event.correlation_id,
                    causation_id=source_event.correlation_id,
                )
            )
        except Exception:  # noqa: BLE001 - a reply is not the work
            return


def _unavailable_manager() -> RLHFManager:
    """A manager with empty in-memory stores, for a module built without one."""
    repos = build_rlhf_repositories(None)
    supervised, runs, checkpoints, models, evaluations = build_training_repositories(None)
    return RLHFManager(
        datasets=repos.datasets,
        runs=runs,
        checkpoints=checkpoints,
        models=models,
        evaluations=evaluations,
        feedback=repos.feedback,
        ratings=repos.ratings,
        disagreements=repos.disagreements,
        supervised_datasets=supervised,
    )


__all__ = [
    "ALGORITHMS_COMPLETED",
    "ALGORITHMS_REQUESTED",
    "DATASETS_COMPLETED",
    "DATASETS_REQUESTED",
    "DISAGREEMENTS_COMPLETED",
    "DISAGREEMENTS_REQUESTED",
    "ESTIMATE_COMPLETED",
    "ESTIMATE_REQUESTED",
    "FEEDBACK_COMPLETED",
    "FEEDBACK_REQUESTED",
    "PIPELINE_COMPLETED",
    "PIPELINE_REQUESTED",
    "RATINGS_COMPLETED",
    "RATINGS_REQUESTED",
    "RLHFModule",
    "STATUS_COMPLETED",
    "STATUS_REQUESTED",
]
