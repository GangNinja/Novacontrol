"""RLVR and critique learning on the bus (Phase 19's module seam).

Same shape as Phase 16's training module and Phase 18's RLHF module, for the same
reasons: the manager is useful as a library, and the rest of the build talks in
events. This module answers the questions a UI, a CLI or another module asks —
status, verifiers, critiques, corrections, critique datasets, a pipeline plan, a
dry run — with a correlated reply each.

Two things are deliberately NOT on the bus:

  * RECORDING a critique or a correction. Both change what a future dataset may
    train on and both must carry a name; a bus message is too easy to send
    anonymously, so they go through the application where the caller is part of
    the call.
  * STARTING a run and APPROVING a model. Phase 16's reason holds: both are long,
    expensive and explicitly confirmed.

The module never starts training, never loads a model and never promotes
anything. A dry run it answers simulates the ten stages on deterministic inputs
and changes nothing on disk.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import Capability
from novacontrol.rlhf.storage import build_rlhf_repositories
from novacontrol.rlvr.manager import RLVRManager
from novacontrol.rlvr.storage import build_rlvr_repositories
from novacontrol.training.storage import build_training_repositories

STATUS_REQUESTED = "rlvr.status_requested"
STATUS_COMPLETED = "rlvr.status_completed"
VERIFIERS_REQUESTED = "rlvr.verifiers_requested"
VERIFIERS_COMPLETED = "rlvr.verifiers_completed"
CRITIQUES_REQUESTED = "rlvr.critiques_requested"
CRITIQUES_COMPLETED = "rlvr.critiques_completed"
CORRECTIONS_REQUESTED = "rlvr.corrections_requested"
CORRECTIONS_COMPLETED = "rlvr.corrections_completed"
DATASETS_REQUESTED = "rlvr.datasets_requested"
DATASETS_COMPLETED = "rlvr.datasets_completed"
PIPELINE_REQUESTED = "rlvr.pipeline_requested"
PIPELINE_COMPLETED = "rlvr.pipeline_completed"
DRY_RUN_REQUESTED = "rlvr.dry_run_requested"
DRY_RUN_COMPLETED = "rlvr.dry_run_completed"


class RLVRModule:
    """Runtime module for the RLVR / critique-based learning subsystem."""

    def __init__(self, manager: RLVRManager | None = None) -> None:
        self.manager = manager if manager is not None else _unavailable_manager()
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "rlvr"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability("rlvr.status", "Report the RLVR subsystem's state."),
            Capability("rlvr.verifiers", "List registered verifiers and their integrity state."),
            Capability("rlvr.critiques", "List recorded structured critiques."),
            Capability("rlvr.corrections", "List corrected examples and their quality verdicts."),
            Capability("rlvr.datasets", "List built critique datasets."),
            Capability("rlvr.pipeline", "Plan the RLVR pipeline."),
            Capability("rlvr.dry_run", "Simulate the RLVR pipeline without training."),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        await event_bus.subscribe(STATUS_REQUESTED, self._handle_status)
        await event_bus.subscribe(VERIFIERS_REQUESTED, self._handle_verifiers)
        await event_bus.subscribe(CRITIQUES_REQUESTED, self._handle_critiques)
        await event_bus.subscribe(CORRECTIONS_REQUESTED, self._handle_corrections)
        await event_bus.subscribe(DATASETS_REQUESTED, self._handle_datasets)
        await event_bus.subscribe(PIPELINE_REQUESTED, self._handle_pipeline)
        await event_bus.subscribe(DRY_RUN_REQUESTED, self._handle_dry_run)

    async def stop(self) -> None:
        bus = self._event_bus
        if bus is not None:
            for type_, handler in (
                (STATUS_REQUESTED, self._handle_status),
                (VERIFIERS_REQUESTED, self._handle_verifiers),
                (CRITIQUES_REQUESTED, self._handle_critiques),
                (CORRECTIONS_REQUESTED, self._handle_corrections),
                (DATASETS_REQUESTED, self._handle_datasets),
                (PIPELINE_REQUESTED, self._handle_pipeline),
                (DRY_RUN_REQUESTED, self._handle_dry_run),
            ):
                try:
                    await bus.unsubscribe(type_, handler)
                except Exception:  # noqa: BLE001 - a stop must always complete
                    continue
        self._event_bus = None

    # -- handlers --------------------------------------------------------------

    async def _handle_status(self, event: Event) -> None:
        await self._reply(STATUS_COMPLETED, self.manager.rlvr_status(), event)

    async def _handle_verifiers(self, event: Event) -> None:
        await self._reply(VERIFIERS_COMPLETED, self.manager.verifiers(), event)

    async def _handle_critiques(self, event: Event) -> None:
        count = self._limit(event, 20)
        rows = [
            item.to_dict()
            for item in self.manager.critiques_list(limit=count, newest_first=True)
        ]
        await self._reply(
            CRITIQUES_COMPLETED,
            {"critiques": rows, "stats": self.manager.critique_stats()},
            event,
        )

    async def _handle_corrections(self, event: Event) -> None:
        count = self._limit(event, 20)
        pending = event.payload.get("pending_only")
        rows = (
            self.manager.corrections_pending(limit=count)
            if isinstance(pending, bool) and pending
            else self.manager.corrections_list(limit=count, newest_first=True)
        )
        await self._reply(
            CORRECTIONS_COMPLETED,
            {
                "corrections": [item.to_dict() for item in rows],
                "stats": self.manager.correction_stats(),
            },
            event,
        )

    async def _handle_datasets(self, event: Event) -> None:
        count = self._limit(event, 20)
        datasets = [
            item.to_dict() for item in self.manager.critique_datasets_list(limit=count)
        ]
        await self._reply(DATASETS_COMPLETED, {"datasets": datasets}, event)

    async def _handle_pipeline(self, event: Event) -> None:
        config = event.payload.get("config")
        tasks = event.payload.get("tasks")
        dataset_version = event.payload.get("dataset_version")
        payload = self.manager.pipeline_plan(
            config if isinstance(config, Mapping) else {},
            tasks=tasks if isinstance(tasks, (list, tuple)) else (),
            dataset_version=dataset_version if isinstance(dataset_version, str) else "",
        )
        await self._reply(PIPELINE_COMPLETED, payload, event)

    async def _handle_dry_run(self, event: Event) -> None:
        config = event.payload.get("config")
        tasks = event.payload.get("tasks")
        labels = event.payload.get("labels")
        dataset_version = event.payload.get("dataset_version")
        payload = self.manager.dry_run(
            config=config if isinstance(config, Mapping) else None,
            tasks=tasks if isinstance(tasks, (list, tuple)) else (),
            dataset_version=dataset_version if isinstance(dataset_version, str) else "",
            labels=labels if isinstance(labels, Mapping) else None,
        )
        await self._reply(DRY_RUN_COMPLETED, payload, event)

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
                    source="rlvr",
                    correlation_id=source_event.correlation_id,
                    causation_id=source_event.correlation_id,
                )
            )
        except Exception:  # noqa: BLE001 - a reply is not the work
            return


def _unavailable_manager() -> RLVRManager:
    """A manager with empty in-memory stores, for a module built without one."""
    repos = build_rlvr_repositories(None)
    rlhf = build_rlhf_repositories(None)
    supervised, runs, checkpoints, models, evaluations = build_training_repositories(None)
    return RLVRManager(
        critiques=repos.critiques,
        corrections=repos.corrections,
        critique_datasets=repos.datasets,
        datasets=rlhf.datasets,
        feedback=rlhf.feedback,
        ratings=rlhf.ratings,
        disagreements=rlhf.disagreements,
        runs=runs,
        checkpoints=checkpoints,
        models=models,
        evaluations=evaluations,
        supervised_datasets=supervised,
    )


__all__ = [
    "CORRECTIONS_COMPLETED",
    "CORRECTIONS_REQUESTED",
    "CRITIQUES_COMPLETED",
    "CRITIQUES_REQUESTED",
    "DATASETS_COMPLETED",
    "DATASETS_REQUESTED",
    "DRY_RUN_COMPLETED",
    "DRY_RUN_REQUESTED",
    "PIPELINE_COMPLETED",
    "PIPELINE_REQUESTED",
    "RLVRModule",
    "STATUS_COMPLETED",
    "STATUS_REQUESTED",
    "VERIFIERS_COMPLETED",
    "VERIFIERS_REQUESTED",
]
