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
