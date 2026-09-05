"""Event-driven phone control runtime module."""

from __future__ import annotations

from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import Capability
from novacontrol.phone.controller import PhoneControlController


class PhoneControlModule:
    """Runtime module for approved phone workflows."""

    def __init__(self, controller: PhoneControlController) -> None:
        self.controller = controller
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "phone"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability("phone.status", "Inspect phone bridge and device status."),
            Capability("phone.plan", "Plan approval-gated phone actions."),
            Capability("phone.execute", "Execute approved phone actions."),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        await event_bus.subscribe("phone.workflow_requested", self._handle_workflow_requested)

    async def stop(self) -> None:
        self._event_bus = None

    async def _handle_workflow_requested(self, event: Event) -> None:
        payload = event.payload
        workflow_type = payload.get("workflow_type")
        if workflow_type != "open_application":
            raise ValueError(f"Unsupported phone workflow type: {workflow_type}")
        workflow = self.controller.plan_open_application(str(payload["application"]))
        await self._publish(
            Event(
                type="phone.workflow_planned",
                payload=workflow.to_dict(),
                source="phone",
                correlation_id=event.correlation_id,
                causation_id=event.correlation_id,
            )
        )
        if payload.get("execute"):
            results = await self.controller.execute_workflow(workflow)
            status = results[-1].status.value if results else "completed"
            await self._publish(
                Event(
                    type=f"phone.workflow_{status}",
                    payload={
                        "workflow": workflow.to_dict(),
                        "results": [result.to_dict() for result in results],
                    },
                    source="phone",
                    correlation_id=event.correlation_id,
                    causation_id=event.correlation_id,
                )
            )

    async def _publish(self, event: Event) -> None:
        if self._event_bus is not None:
            await self._event_bus.publish(event)
