"""Vision domain models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class VisionTaskType(StrEnum):
    OCR = "ocr"
    SCREEN_UNDERSTANDING = "screen_understanding"
    WINDOW_DETECTION = "window_detection"
    IMAGE_UNDERSTANDING = "image_understanding"
    DOCUMENT_UNDERSTANDING = "document_understanding"


@dataclass(frozen=True, slots=True)
class OcrResult:
    text: str
    confidence: float = 0.0
    regions: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "regions": [dict(region) for region in self.regions],
        }


@dataclass(frozen=True, slots=True)
class DetectedWindow:
    title: str
    bounds: Mapping[str, int]
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "bounds": dict(self.bounds),
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class ScreenUnderstanding:
    summary: str
    windows: tuple[DetectedWindow, ...] = ()
    text: str = ""
    #: What the pipeline could say ABOUT its own answer — chiefly whether a
    #: question about the screen was actually answered, or declined because no
    #: vision model is wired. Absent for a plain description, so nothing that
    #: reads a summary before this field existed changes shape.
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "windows": [window.to_dict() for window in self.windows],
            "text": self.text,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ImageObservation:
    summary: str
    labels: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "summary": self.summary,
            "labels": self.labels,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class DocumentUnderstanding:
    title: str
    summary: str
    sections: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "sections": self.sections,
            "metadata": dict(self.metadata),
        }


class VisionImageType(StrEnum):
    """What KIND of picture this is — decided before any model sees it."""

    APPLICATION_SCREENSHOT = "application_screenshot"
    ERROR_SCREENSHOT = "error_screenshot"
    DOCUMENT = "document"
    PHOTO = "photo"
    UNKNOWN = "unknown"


class VisionTaskKind(StrEnum):
    """What is being ASKED of the image, which is what picks the cheap path.

    ``OCR`` is a text task and never needs a vision model; ``LOCATE`` wants a
    point; ``UNDERSTAND`` is everything else ("what is this error?"), where the
    only way to know whether OCR suffices is to read the image and compare.
    """

    OCR = "ocr"
    LOCATE = "locate"
    UNDERSTAND = "understand"


@dataclass(frozen=True, slots=True)
class VisionRequest:
    """One question about one image, before anything has been read.

    Deliberately not "a screenshot": the manager accepts a saved file, a fresh
    capture, or a document the same way, and it is the request that says which
    it believes it is looking at. ``prefer_ocr`` and ``allow_vlm`` let a caller
    opt out of the cheap path or forbid the expensive one — a machine with no
    vision model still answers text questions.
    """

    source: str
    question: str = ""
    task: VisionTaskKind = VisionTaskKind.UNDERSTAND
    image_type: VisionImageType = VisionImageType.UNKNOWN
    #: Read the image with OCR first and answer from the text when the text is
    #: enough — the whole point of not paying for a VLM on every picture.
    prefer_ocr: bool = True
    #: Whether a vision model MAY be used. False means "answer from OCR or say
    #: honestly that this needs eyes"; it never means "invent an answer".
    allow_vlm: bool = True
    #: For ``LOCATE``: the label to find on the image ("login button").
    target: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "question": self.question,
            "task": self.task.value,
            "image_type": self.image_type.value,
            "prefer_ocr": self.prefer_ocr,
            "allow_vlm": self.allow_vlm,
            "target": self.target,
        }


@dataclass(frozen=True, slots=True)
class VisionElement:
    """One thing on the screen a person could interact with.

    ``bounds`` is in the image's own pixels, so a caller maps it onto whatever
    the screen really is; a point is a bounds with no extent.
    """

    label: str
    kind: str = "element"
    bounds: Mapping[str, int] = field(default_factory=dict)
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "kind": self.kind,
            "bounds": dict(self.bounds),
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class VisionResult:
    """What the vision pipeline concluded, in the shape the planner consumes.

    Structured on purpose: a caller deciding what to do next needs the error
    lines and the elements as data, not as a paragraph it must re-read. The
    summary is prose for a person, and ``metadata`` carries the pipeline's own
    provenance — which reader answered, whether the question was actually
    answered, whether the model was consulted — never the model's reasoning.

    ``answered`` is the honest bit. A model that could not see the image, or a
    machine with no vision model at all, produces a result that says so rather
    than a summary that reads like an answer.
    """

    image_type: VisionImageType = VisionImageType.UNKNOWN
    summary: str = ""
    detected_text: tuple[str, ...] = ()
    ui_elements: tuple[VisionElement, ...] = ()
    errors: tuple[str, ...] = ()
    relevant_regions: tuple[Mapping[str, Any], ...] = ()
    confidence: float = 0.0
    answered: bool = False
    escalated: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """The detected text as one block — for a caller that only wants it."""
        return "\n".join(self.detected_text)

    def to_dict(self) -> dict[str, Any]:
        """The documented result shape, plus provenance under ``metadata``."""
        return {
            "image_type": self.image_type.value,
            "detected_text": list(self.detected_text),
            "ui_elements": [element.to_dict() for element in self.ui_elements],
            "errors": list(self.errors),
            "relevant_regions": [dict(region) for region in self.relevant_regions],
            "summary": self.summary,
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
        }

    def to_screen_understanding(self) -> ScreenUnderstanding:
        """The older screen shape, so existing consumers keep working.

        The question-and-answer provenance rides along in ``metadata``, which is
        exactly the field ``ScreenUnderstanding`` already uses to say whether a
        question about the screen was answered or declined.
        """
        return ScreenUnderstanding(
            summary=self.summary,
            windows=(),
            text=self.text,
            metadata={
                **dict(self.metadata),
                "question_answered": self.answered,
                "image_type": self.image_type.value,
            },
        )
