"""Event-driven planning runtime module."""

from __future__ import annotations

from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import Capability
from novacontrol.planning.engine import PlanningEngine
from novacontrol.planning.executor import WorkflowExecutor


class PlanningModule:
    """Runtime module that creates and optionally executes plans."""

    def __init__(
        self,
        engine: PlanningEngine | None = None,
        executor: WorkflowExecutor | None = None,
    ) -> None:
        self.engine = engine or PlanningEngine()
        self.executor = executor or WorkflowExecutor()
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "planning"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability("planning.decompose", "Break goals into plans."),
            Capability("planning.execute", "Execute dependency-aware workflow plans."),
            Capability("planning.clarify", "Identify goals needing clarification."),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        await event_bus.subscribe("planning.plan_requested", self._handle_plan_requested)

    async def stop(self) -> None:
        self._event_bus = None

    async def _handle_plan_requested(self, event: Event) -> None:
        payload = event.payload
        plan = self.engine.create_plan(str(payload["goal"]))
        if self._event_bus is None:
            return
        if plan.needs_clarification:
            await self._event_bus.publish(
                Event(
                    type="planning.clarification_required",
                    payload=plan.to_dict(),
                    source="planning",
                    correlation_id=event.correlation_id,
                    causation_id=event.correlation_id,
                )
            )
            return

        await self._event_bus.publish(
            Event(
                type="planning.plan_created",
                payload=plan.to_dict(),
                source="planning",
                correlation_id=event.correlation_id,
                causation_id=event.correlation_id,
            )
        )
        if payload.get("execute"):
            result = await self.executor.execute(plan)
            await self._event_bus.publish(
                Event(
                    type=f"planning.workflow_{result.status.value}",
                    payload=result.to_dict(),
                    source="planning",
                    correlation_id=event.correlation_id,
                    causation_id=event.correlation_id,
                )
            )
