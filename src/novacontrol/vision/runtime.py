"""Event-driven vision runtime module."""

from __future__ import annotations

from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import Capability
from novacontrol.vision.models import VisionTaskType
from novacontrol.vision.processors import BasicScreenUnderstandingProcessor, VisionProcessor


class VisionModule:
    """Runtime module for OCR, screen, image, and document understanding."""

    def __init__(self, processor: VisionProcessor | None = None, *, llm_provider: object | None = None) -> None:
        if processor is not None:
            self.processor = processor
        elif llm_provider is not None:
            from novacontrol.vision.multimodal import MultimodalVisionProcessor
            self.processor = MultimodalVisionProcessor(llm_provider=llm_provider)
        else:
            self.processor = BasicScreenUnderstandingProcessor()
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "vision"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability("vision.ocr", "Extract text from visual sources."),
            Capability("vision.screen", "Understand screen captures."),
            Capability("vision.windows", "Detect windows."),
            Capability("vision.image", "Understand images."),
            Capability("vision.document", "Understand visual documents."),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        await event_bus.subscribe("vision.task_requested", self._handle_task_requested)

    async def stop(self) -> None:
        self._event_bus = None

    async def _handle_task_requested(self, event: Event) -> None:
        payload = event.payload
        task_type = VisionTaskType(str(payload["task_type"]))
        source = str(payload["source"])
        if task_type is VisionTaskType.OCR:
            result = (await self.processor.ocr(source)).to_dict()
        elif task_type is VisionTaskType.SCREEN_UNDERSTANDING:
            result = (await self.processor.understand_screen(source)).to_dict()
        elif task_type is VisionTaskType.WINDOW_DETECTION:
            result = {"windows": [window.to_dict() for window in await self.processor.detect_windows(source)]}
        elif task_type is VisionTaskType.IMAGE_UNDERSTANDING:
            result = (await self.processor.understand_image(source)).to_dict()
        elif task_type is VisionTaskType.DOCUMENT_UNDERSTANDING:
            result = (await self.processor.understand_document(source)).to_dict()
        else:
            raise ValueError(f"Unsupported vision task type: {task_type}")

        if self._event_bus is not None:
            await self._event_bus.publish(
                Event(
                    type="vision.task_completed",
                    payload={
                        "task_type": task_type.value,
                        "source": source,
                        "result": result,
                    },
                    source="vision",
                    correlation_id=event.correlation_id,
                    causation_id=event.correlation_id,
                )
            )
