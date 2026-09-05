"""Event-driven browser automation module."""

from __future__ import annotations

from novacontrol.browser.controller import BrowserAutomationController
from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import Capability


class BrowserAutomationModule:
    """Runtime module for browser workflows."""

    def __init__(self, controller: BrowserAutomationController) -> None:
        self.controller = controller
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "browser"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability("browser.navigate", "Navigate browser sessions."),
            Capability("browser.extract", "Extract data from pages."),
            Capability("browser.forms", "Fill forms with approval."),
            Capability("browser.test", "Run browser-based web app checks."),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        await event_bus.subscribe("browser.workflow_requested", self._handle_workflow_requested)

    async def stop(self) -> None:
        self._event_bus = None

    async def _handle_workflow_requested(self, event: Event) -> None:
        payload = event.payload
        workflow_type = payload.get("workflow_type")
        if workflow_type == "navigate":
            workflow = self.controller.plan_navigation(str(payload["url"]))
        elif workflow_type == "extract":
            workflow = self.controller.plan_extraction(
                str(payload["selector"]),
                source=str(payload.get("source", "current_page")),
            )
        elif workflow_type == "fill_form":
            workflow = self.controller.plan_form_fill(str(payload["url"]), dict(payload["fields"]))
        elif workflow_type == "test_web_app":
            workflow = self.controller.plan_web_test(
                str(payload["url"]),
                tuple(payload.get("assertions", ())),
            )
        else:
            raise ValueError(f"Unsupported browser workflow type: {workflow_type}")

        await self._publish(
            Event(
                type="browser.workflow_planned",
                payload=workflow.to_dict(),
                source="browser",
                correlation_id=event.correlation_id,
                causation_id=event.correlation_id,
            )
        )
        if payload.get("execute"):
            results = await self.controller.execute_workflow(workflow)
            status = results[-1].status.value if results else "completed"
            await self._publish(
                Event(
                    type=f"browser.workflow_{status}",
                    payload={
                        "workflow": workflow.to_dict(),
                        "results": [result.to_dict() for result in results],
                    },
                    source="browser",
                    correlation_id=event.correlation_id,
                    causation_id=event.correlation_id,
                )
            )

    async def _publish(self, event: Event) -> None:
        if self._event_bus is not None:
            await self._event_bus.publish(event)
