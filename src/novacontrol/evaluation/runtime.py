"""Evaluation on the bus (Phase 15's module seam).

The service is useful as a library, but the rest of this build talks in events:
a module subscribes to what it can answer and publishes what it did. This module
is that seam for evaluation, and it follows the house rules — one ``name``, a
declared ``capabilities`` tuple, ``start``/``stop`` — with one responsibility
that is not a request/response pair: it ATTACHES the trajectory recorder to the
bus it was given, so the lifecycle events the application already publishes
become a trajectory without anything having to be rewired.

Requests are answered on the same terms as every other module: a correlated
reply for each one. The vocabulary is deliberately narrow (summary, metrics,
one trajectory, recent rewards) because those are the four questions an API, a
CLI or another module asks; anything deeper reads the repositories directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import Capability
from novacontrol.evaluation.service import EvaluationService


class EvaluationModule:
    """Runtime module for trajectories, evaluation and rewards."""

    def __init__(self, service: EvaluationService | None = None) -> None:
        self.service = service if service is not None else EvaluationService()
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "evaluation"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability("trajectory.record", "Record a task's trajectory from the lifecycle."),
            Capability("evaluation.summary", "Summarise what has been recorded and scored."),
            Capability("evaluation.metrics", "Aggregate metrics over stored trajectories."),
            Capability("evaluation.reward", "Read the reward breakdown of recorded work."),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self.service.set_publisher(self._publish_now)
        await event_bus.subscribe("evaluation.summary_requested", self._handle_summary)
        await event_bus.subscribe("evaluation.metrics_requested", self._handle_metrics)
        await event_bus.subscribe("evaluation.trajectory_requested", self._handle_trajectory)
        await event_bus.subscribe("evaluation.reward_requested", self._handle_reward)
        # Last: attaching the recorder is what starts capture, and it should only
        # happen once this module can answer questions about what it captured.
        await self.service.start(event_bus)

    async def stop(self) -> None:
        bus = self._event_bus
        self.service.set_publisher(None)
        if bus is not None:
            for type_ in (
                "evaluation.summary_requested",
                "evaluation.metrics_requested",
                "evaluation.trajectory_requested",
                "evaluation.reward_requested",
            ):
                try:
                    await bus.unsubscribe(type_, self._handler_for(type_))
                except Exception:  # noqa: BLE001 - a stop must always complete
                    continue
        await self.service.stop(bus)
        self._event_bus = None

    def _handler_for(self, type_: str) -> Any:
        return {
            "evaluation.summary_requested": self._handle_summary,
            "evaluation.metrics_requested": self._handle_metrics,
            "evaluation.trajectory_requested": self._handle_trajectory,
            "evaluation.reward_requested": self._handle_reward,
        }[type_]

    # -- requests --------------------------------------------------------------

    async def _handle_summary(self, event: Event) -> None:
        await self._reply("evaluation.summary_completed", self.service.summary(), event)

    async def _handle_metrics(self, event: Event) -> None:
        await self._reply("evaluation.metrics_completed", self.service.metrics_snapshot(), event)

    async def _handle_trajectory(self, event: Event) -> None:
        trajectory_id = str(event.payload.get("trajectory_id", ""))
        found = self.service.trajectory(trajectory_id)
        await self._reply(
            "evaluation.trajectory_completed",
            found if found is not None else {"trajectory": None, "reason": "not found"},
            event,
        )

    async def _handle_reward(self, event: Event) -> None:
        trajectory_id = str(event.payload.get("trajectory_id", ""))
        limit = event.payload.get("limit")
        if trajectory_id:
            found = self.service.trajectory(trajectory_id)
            reward = found.get("reward") if found else None
            await self._reply(
                "evaluation.reward_completed",
                reward if isinstance(reward, Mapping) else {},
                event,
            )
            return
        count = int(limit) if isinstance(limit, int) and limit > 0 else 20
        rewards = [result.to_dict() for result in self.service.list_rewards(limit=count)]
        await self._reply("evaluation.reward_completed", {"rewards": rewards}, event)

    # -- replies and announcements --------------------------------------------

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
                    source="evaluation",
                    correlation_id=source_event.correlation_id,
                    causation_id=source_event.correlation_id,
                )
            )
        except Exception:  # noqa: BLE001 - a reply is not the work
            return

    def _publish_now(self, type_: str, payload: Mapping[str, Any]) -> None:
        """The service's publisher: schedule an event if a loop is running.

        The service calls this from the recorder's sink, which runs inside an
        event handler in the loop — but the same service is usable from a test
        with no loop at all, and there the announcement is simply skipped: the
        stored row is the record, the event is only how a UI hears about it.
        """
        bus = self._event_bus
        if bus is None:
            return
        event = Event.of(type_, source="evaluation", **dict(payload))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(bus.emit(event.type, source="evaluation", **dict(payload)))


__all__ = ["EvaluationModule"]
