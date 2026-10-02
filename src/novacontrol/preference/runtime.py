"""Preference optimization on the bus (Phase 17's module seam).

Same shape as Phase 16's training module, for the same reason: the manager is
useful as a library, and the rest of this build talks in events. This module
answers the questions a UI, a CLI or another module asks — status, datasets, the
review queue, the objective table, an estimate — with a correlated reply each.

The one thing that is deliberately NOT on the bus is the review DECISION. A
reviewer's choice changes what a future dataset may train on, and it must carry a
name; a bus message is too easy to send anonymously. Decisions go through the
application (API/CLI), where the reviewer is part of the call. Starting and
cancelling runs stay off the bus for Phase 16's reason: a run is long, expensive
and explicitly confirmed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import Capability
from novacontrol.preference.manager import PreferenceManager
from novacontrol.preference.storage import build_preference_repositories
from novacontrol.training.storage import build_training_repositories

STATUS_REQUESTED = "preference.status_requested"
STATUS_COMPLETED = "preference.status_completed"
DATASETS_REQUESTED = "preference.datasets_requested"
DATASETS_COMPLETED = "preference.datasets_completed"
REVIEWS_REQUESTED = "preference.reviews_requested"
REVIEWS_COMPLETED = "preference.reviews_completed"
ALGORITHMS_REQUESTED = "preference.algorithms_requested"
ALGORITHMS_COMPLETED = "preference.algorithms_completed"
ESTIMATE_REQUESTED = "preference.estimate_requested"
ESTIMATE_COMPLETED = "preference.estimate_completed"


class PreferenceModule:
    """Runtime module for the DPO/ORPO subsystem."""

    def __init__(self, manager: PreferenceManager | None = None) -> None:
        self.manager = manager if manager is not None else _unavailable_manager()
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "preference"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability("preference.status", "Report the preference subsystem's state."),
            Capability("preference.datasets", "List built preference datasets."),
            Capability("preference.reviews", "List pairs waiting on a human review."),
            Capability("preference.algorithms", "Describe the DPO and ORPO objectives."),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        await event_bus.subscribe(STATUS_REQUESTED, self._handle_status)
        await event_bus.subscribe(DATASETS_REQUESTED, self._handle_datasets)
        await event_bus.subscribe(REVIEWS_REQUESTED, self._handle_reviews)
        await event_bus.subscribe(ALGORITHMS_REQUESTED, self._handle_algorithms)
        await event_bus.subscribe(ESTIMATE_REQUESTED, self._handle_estimate)

    async def stop(self) -> None:
        bus = self._event_bus
        if bus is not None:
            for type_, handler in (
                (STATUS_REQUESTED, self._handle_status),
                (DATASETS_REQUESTED, self._handle_datasets),
                (REVIEWS_REQUESTED, self._handle_reviews),
                (ALGORITHMS_REQUESTED, self._handle_algorithms),
                (ESTIMATE_REQUESTED, self._handle_estimate),
            ):
                try:
                    await bus.unsubscribe(type_, handler)
                except Exception:  # noqa: BLE001 - a stop must always complete
                    continue
        self._event_bus = None

    # -- handlers --------------------------------------------------------------

    async def _handle_status(self, event: Event) -> None:
        await self._reply(STATUS_COMPLETED, self.manager.status(), event)

    async def _handle_datasets(self, event: Event) -> None:
        count = self._limit(event, 20)
        datasets = [
            item.to_dict() for item in self.manager.preference_datasets(limit=count)
        ]
        await self._reply(DATASETS_COMPLETED, {"datasets": datasets}, event)

    async def _handle_reviews(self, event: Event) -> None:
        count = self._limit(event, 20)
        pending = event.payload.get("pending_only")
        reviews = list(
            self.manager.reviews_list(
                pending_only=bool(pending) if isinstance(pending, bool) else True,
                limit=count,
            )
        )
        await self._reply(
            REVIEWS_COMPLETED,
            {"reviews": reviews, "stats": self.manager.review_stats()},
            event,
        )

    async def _handle_algorithms(self, event: Event) -> None:
        await self._reply(ALGORITHMS_COMPLETED, self.manager.algorithms(), event)

    async def _handle_estimate(self, event: Event) -> None:
        config = event.payload.get("config")
        payload = self.manager.estimate_config(config if isinstance(config, Mapping) else {})
        await self._reply(ESTIMATE_COMPLETED, payload, event)

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
                    source="preference",
                    correlation_id=source_event.correlation_id,
                    causation_id=source_event.correlation_id,
                )
            )
        except Exception:  # noqa: BLE001 - a reply is not the work
            return


def _unavailable_manager() -> PreferenceManager:
    """A manager with empty in-memory stores, for a module built without one."""
    datasets, reviews = build_preference_repositories(None)
    supervised, runs, checkpoints, models, evaluations = build_training_repositories(None)
    return PreferenceManager(
        datasets=datasets,
        runs=runs,
        checkpoints=checkpoints,
        models=models,
        evaluations=evaluations,
        reviews=reviews,
        supervised_datasets=supervised,
    )


__all__ = [
    "ALGORITHMS_COMPLETED",
    "ALGORITHMS_REQUESTED",
    "DATASETS_COMPLETED",
    "DATASETS_REQUESTED",
    "ESTIMATE_COMPLETED",
    "ESTIMATE_REQUESTED",
    "REVIEWS_COMPLETED",
    "REVIEWS_REQUESTED",
    "STATUS_COMPLETED",
    "STATUS_REQUESTED",
    "PreferenceModule",
]
