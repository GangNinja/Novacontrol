"""Vision processor interfaces and deterministic baseline implementations."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from novacontrol.vision.models import (
    DetectedWindow,
    DocumentUnderstanding,
    ImageObservation,
    OcrResult,
    ScreenUnderstanding,
)


@runtime_checkable
class VisionProcessor(Protocol):
    async def ocr(self, source: str) -> OcrResult:
        """Extract text from an image, screen, or document source."""

    async def understand_screen(self, source: str) -> ScreenUnderstanding:
        """Summarize a screen capture."""

    async def detect_windows(self, source: str) -> tuple[DetectedWindow, ...]:
        """Detect windows from a screen capture."""

    async def understand_image(self, source: str) -> ImageObservation:
        """Summarize an image."""

    async def understand_document(self, source: str) -> DocumentUnderstanding:
        """Summarize a document-like visual source."""


class BasicImageUnderstandingProcessor:
    """Dependency-free image understanding baseline."""

    async def understand_image(self, source: str) -> ImageObservation:
        path = Path(source)
        labels = tuple(part for part in path.stem.replace("-", "_").split("_") if part)
        summary = f"Image source {path.name or source}"
        return ImageObservation(summary=summary, labels=labels, metadata={"source": source})


class BasicDocumentUnderstandingProcessor:
    """Dependency-free document understanding baseline."""

    async def understand_document(self, source: str) -> DocumentUnderstanding:
        text = _read_text_if_possible(source)
        title = Path(source).stem or "document"
        sections = tuple(line.strip() for line in text.splitlines() if line.strip()[:1] == "#")
        summary = _summarize_text(text) if text else f"Document source {source}"
        return DocumentUnderstanding(
            title=title,
            summary=summary,
            sections=sections,
            metadata={"source": source},
        )


class BasicScreenUnderstandingProcessor:
    """Dependency-free screen understanding baseline."""

    def __init__(self) -> None:
        self.image_processor = BasicImageUnderstandingProcessor()
        self.document_processor = BasicDocumentUnderstandingProcessor()

    async def ocr(self, source: str) -> OcrResult:
        text = _read_text_if_possible(source)
        if not text:
            text = Path(source).stem.replace("_", " ").replace("-", " ")
        return OcrResult(text=text, confidence=0.5 if text else 0.0)

    async def understand_screen(self, source: str) -> ScreenUnderstanding:
        ocr_result = await self.ocr(source)
        windows = await self.detect_windows(source)
        summary = _summarize_text(ocr_result.text) if ocr_result.text else f"Screen source {source}"
        return ScreenUnderstanding(summary=summary, windows=windows, text=ocr_result.text)

    async def detect_windows(self, source: str) -> tuple[DetectedWindow, ...]:
        title = Path(source).stem.replace("_", " ").title() or "Unknown Window"
        return (
            DetectedWindow(
                title=title,
                bounds={"x": 0, "y": 0, "width": 0, "height": 0},
                confidence=0.25,
            ),
        )

    async def understand_image(self, source: str) -> ImageObservation:
        return await self.image_processor.understand_image(source)

    async def understand_document(self, source: str) -> DocumentUnderstanding:
        return await self.document_processor.understand_document(source)


def _read_text_if_possible(source: str) -> str:
    path = Path(source)
    if path.exists() and path.is_file() and path.suffix.lower() in {".txt", ".md"}:
        return path.read_text(encoding="utf-8")
    return ""


def _summarize_text(text: str, *, max_chars: int = 180) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."
