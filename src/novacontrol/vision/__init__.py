"""Vision subsystem."""

from novacontrol.vision.models import (
    DetectedWindow,
    DocumentUnderstanding,
    ImageObservation,
    OcrResult,
    ScreenUnderstanding,
    VisionTaskType,
)
from novacontrol.vision.multimodal import MultimodalVisionProcessor
from novacontrol.vision.processors import (
    BasicDocumentUnderstandingProcessor,
    BasicImageUnderstandingProcessor,
    BasicScreenUnderstandingProcessor,
    VisionProcessor,
)
from novacontrol.vision.runtime import VisionModule

__all__ = [
    "BasicDocumentUnderstandingProcessor",
    "BasicImageUnderstandingProcessor",
    "BasicScreenUnderstandingProcessor",
    "DetectedWindow",
    "DocumentUnderstanding",
    "ImageObservation",
    "MultimodalVisionProcessor",
    "OcrResult",
    "ScreenUnderstanding",
    "VisionModule",
    "VisionProcessor",
    "VisionTaskType",
]
