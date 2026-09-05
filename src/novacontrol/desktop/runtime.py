"""Event-driven desktop automation module."""

from __future__ import annotations

from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import Capability
from novacontrol.desktop.controller import DesktopAutomationController


class DesktopAutomationModule:
    """Runtime module for approved desktop automation workflows."""

    def __init__(self, controller: DesktopAutomationController) -> None:
        self.controller = controller
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "desktop"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability("desktop.plan", "Plan desktop workflows."),
            Capability("desktop.execute", "Execute approved desktop workflows."),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        await event_bus.subscribe("desktop.workflow_requested", self._handle_workflow_requested)

    async def stop(self) -> None:
        self._event_bus = None

    async def _handle_workflow_requested(self, event: Event) -> None:
        payload = event.payload
        workflow_type = payload.get("workflow_type")
        if workflow_type == "open_application":
            workflow = self.controller.plan_open_application(str(payload["application"]))
        elif workflow_type == "execute_script":
            workflow = self.controller.plan_execute_script(
                str(payload["command"]),
                working_directory=payload.get("working_directory"),
            )
        elif workflow_type == "organize_files":
            workflow = self.controller.plan_file_organization(
                str(payload["source_directory"]),
                dict(payload.get("extension_to_directory", {})),
            )
        else:
            raise ValueError(f"Unsupported desktop workflow type: {workflow_type}")

        await self._publish(
            Event(
                type="desktop.workflow_planned",
                payload=workflow.to_dict(),
                source="desktop",
                correlation_id=event.correlation_id,
                causation_id=event.correlation_id,
            )
        )
        if payload.get("execute"):
            results = await self.controller.execute_workflow(workflow)
            status = results[-1].status.value if results else "completed"
            await self._publish(
                Event(
                    type=f"desktop.workflow_{status}",
                    payload={
                        "workflow": workflow.to_dict(),
                        "results": [result.to_dict() for result in results],
                    },
                    source="desktop",
                    correlation_id=event.correlation_id,
                    causation_id=event.correlation_id,
                )
            )

    async def _publish(self, event: Event) -> None:
        if self._event_bus is not None:
            await self._event_bus.publish(event)
