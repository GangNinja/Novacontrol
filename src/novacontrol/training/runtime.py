"""Training on the bus (Phase 16's module seam).

The training manager is useful as a library, but the rest of this build talks in
events: a module subscribes to what it can answer and publishes what it did.
This module answers the four questions a UI, a CLI or another module asks —
status, datasets, runs, models — with a correlated reply each, and nothing else.

Deliberately NOT on the bus: starting or cancelling a run. A training run is a
long, resource-heavy operation with an explicit confirmation path, and a bus
request is too easy to fire by accident. Those operations go through the
application (API/CLI), where the confirmation is part of the call.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import Capability
from novacontrol.training.manager import TrainingManager
from novacontrol.training.storage import build_training_repositories

STATUS_REQUESTED = "training.status_requested"
STATUS_COMPLETED = "training.status_completed"
DATASETS_REQUESTED = "training.datasets_requested"
DATASETS_COMPLETED = "training.datasets_completed"
RUNS_REQUESTED = "training.runs_requested"
RUNS_COMPLETED = "training.runs_completed"
MODELS_REQUESTED = "training.models_requested"
MODELS_COMPLETED = "training.models_completed"
ESTIMATE_REQUESTED = "training.estimate_requested"
ESTIMATE_COMPLETED = "training.estimate_completed"


class TrainingModule:
    """Runtime module for the SFT subsystem."""

    def __init__(self, manager: TrainingManager | None = None) -> None:
        self.manager = manager if manager is not None else _unavailable_manager()
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "training"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability("training.status", "Report the SFT subsystem's state."),
            Capability("training.datasets", "List built training datasets."),
            Capability("training.runs", "List training runs and their progress."),
            Capability("training.models", "List registered trained models."),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        await event_bus.subscribe(STATUS_REQUESTED, self._handle_status)
        await event_bus.subscribe(DATASETS_REQUESTED, self._handle_datasets)
        await event_bus.subscribe(RUNS_REQUESTED, self._handle_runs)
        await event_bus.subscribe(MODELS_REQUESTED, self._handle_models)
        await event_bus.subscribe(ESTIMATE_REQUESTED, self._handle_estimate)

    async def stop(self) -> None:
        bus = self._event_bus
        if bus is not None:
            for type_, handler in (
                (STATUS_REQUESTED, self._handle_status),
                (DATASETS_REQUESTED, self._handle_datasets),
                (RUNS_REQUESTED, self._handle_runs),
                (MODELS_REQUESTED, self._handle_models),
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
        limit = event.payload.get("limit")
        count = int(limit) if isinstance(limit, int) and limit > 0 else 20
        datasets = [item.to_dict() for item in self.manager.datasets_list(limit=count)]
        await self._reply(DATASETS_COMPLETED, {"datasets": datasets}, event)

    async def _handle_runs(self, event: Event) -> None:
        status = str(event.payload.get("status", ""))
        limit = event.payload.get("limit")
        count = int(limit) if isinstance(limit, int) and limit > 0 else 20
        runs = [item.to_dict() for item in self.manager.runs_list(status=status, limit=count)]
        await self._reply(RUNS_COMPLETED, {"runs": runs}, event)

    async def _handle_models(self, event: Event) -> None:
        status = str(event.payload.get("status", ""))
        limit = event.payload.get("limit")
        count = int(limit) if isinstance(limit, int) and limit > 0 else 20
        models = [item.to_dict() for item in self.manager.models_list(status=status, limit=count)]
        await self._reply(MODELS_COMPLETED, {"models": models}, event)

    async def _handle_estimate(self, event: Event) -> None:
        config = event.payload.get("config")
        payload = self.manager.estimate_config(config if isinstance(config, Mapping) else {})
        await self._reply(ESTIMATE_COMPLETED, payload, event)

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
                    source="training",
                    correlation_id=source_event.correlation_id,
                    causation_id=source_event.correlation_id,
                )
            )
        except Exception:  # noqa: BLE001 - a reply is not the work
            return


def _unavailable_manager() -> TrainingManager:
    """A manager with empty in-memory stores, for a module built without one."""
    datasets, runs, checkpoints, models, evaluations = build_training_repositories(None)
    return TrainingManager(
        datasets=datasets,
        runs=runs,
        checkpoints=checkpoints,
        models=models,
        evaluations=evaluations,
    )


__all__ = [
    "DATASETS_COMPLETED",
    "DATASETS_REQUESTED",
    "ESTIMATE_COMPLETED",
    "ESTIMATE_REQUESTED",
    "MODELS_COMPLETED",
    "MODELS_REQUESTED",
    "RUNS_COMPLETED",
    "RUNS_REQUESTED",
    "STATUS_COMPLETED",
    "STATUS_REQUESTED",
    "TrainingModule",
]
