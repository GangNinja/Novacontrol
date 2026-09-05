"""Multimodal vision processor that leverages LLM providers for real image understanding.

When an LLM provider with multimodal support is available, this processor
sends images for real analysis. Otherwise, it falls back to the basic
deterministic processor.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from novacontrol.vision.models import (
    DetectedWindow,
    DocumentUnderstanding,
    ImageObservation,
    OcrResult,
    ScreenUnderstanding,
)
from novacontrol.vision.processors import BasicScreenUnderstandingProcessor


class MultimodalVisionProcessor:
    """Vision processor that uses LLM for image understanding when available.

    Usage:
        processor = MultimodalVisionProcessor(llm_provider=my_llm)
        result = await processor.understand_image("/path/to/screenshot.png")
    """

    def __init__(self, *, llm_provider: object | None = None) -> None:
        self._llm_provider = llm_provider
        self._fallback = BasicScreenUnderstandingProcessor()

    @property
    def has_llm(self) -> bool:
        return self._llm_provider is not None

    async def ocr(self, source: str) -> OcrResult:
        """Extract text from an image using LLM if available."""
        if not self.has_llm:
            return await self._fallback.ocr(source)

        try:
            image_data = _load_image_base64(source)
            if not image_data:
                return await self._fallback.ocr(source)

            result = await self._call_llm(
                "Extract ALL visible text from this image exactly as it appears. "
                "Return ONLY the extracted text, no commentary.",
                image_data=image_data,
            )
            return OcrResult(text=result.strip(), confidence=0.9)
        except Exception:
            return await self._fallback.ocr(source)

    async def understand_screen(self, source: str) -> ScreenUnderstanding:
        """Understand a screen capture using LLM if available."""
        if not self.has_llm:
            return await self._fallback.understand_screen(source)

        try:
            image_data = _load_image_base64(source)
            if not image_data:
                return await self._fallback.understand_screen(source)

            prompt = (
                "Analyze this screenshot. Describe:\n"
                "1. What application(s) are visible\n"
                "2. The current state/interface\n"
                "3. Any errors, warnings, or important text\n"
                "4. Any interactive elements (buttons, forms, menus)\n"
                "Be concise but thorough."
            )
            result = await self._call_llm(prompt, image_data=image_data)
            return ScreenUnderstanding(summary=result, windows=(), text=result)
        except Exception:
            return await self._fallback.understand_screen(source)

    async def detect_windows(self, source: str) -> tuple[DetectedWindow, ...]:
        """Detect windows using LLM if available."""
        if not self.has_llm:
            return await self._fallback.detect_windows(source)

        try:
            image_data = _load_image_base64(source)
            if not image_data:
                return await self._fallback.detect_windows(source)

            result = await self._call_llm(
                "List all visible windows in this screenshot. "
                "For each window, provide the title and approximate position (top/middle/bottom, left/center/right). "
                "Format as: Window: [title] at [position]",
                image_data=image_data,
            )
            windows = []
            for line in result.splitlines():
                line = line.strip()
                if line.lower().startswith("window:"):
                    title = line.split(":", 1)[1].strip()
                    windows.append(DetectedWindow(
                        title=title,
                        bounds={"x": 0, "y": 0, "width": 0, "height": 0},
                        confidence=0.7,
                    ))
            return tuple(windows) if windows else await self._fallback.detect_windows(source)
        except Exception:
            return await self._fallback.detect_windows(source)

    async def understand_image(self, source: str) -> ImageObservation:
        """Understand an image using LLM if available."""
        if not self.has_llm:
            return await self._fallback.understand_image(source)

        try:
            image_data = _load_image_base64(source)
            if not image_data:
                return await self._fallback.understand_image(source)

            result = await self._call_llm(
                "Describe this image in detail. What does it show? "
                "Identify key objects, text, people, scenes, or UI elements.",
                image_data=image_data,
            )
            return ImageObservation(
                summary=result,
                metadata={"source": source, "llm_analyzed": True},
            )
        except Exception:
            return await self._fallback.understand_image(source)

    async def understand_document(self, source: str) -> DocumentUnderstanding:
        """Understand a document using LLM if available."""
        path = Path(source)
        text = ""
        if path.exists() and path.is_file() and path.suffix.lower() in {".txt", ".md", ".json", ".yaml", ".py", ".js", ".html", ".css"}:
            text = path.read_text(encoding="utf-8", errors="replace")

        if not self.has_llm:
            return await self._fallback.understand_document(source)

        try:
            if text:
                result = await self._call_llm(
                    "Analyze this document. Provide:\n"
                    "1. Title/purpose\n"
                    "2. Key sections\n"
                    "3. Summary\n"
                    "Be concise.",
                    text_content=text[:5000],
                )
            else:
                image_data = _load_image_base64(source)
                if image_data:
                    result = await self._call_llm(
                        "Analyze this document image. Identify the title, "
                        "key sections, and provide a summary.",
                        image_data=image_data,
                    )
                else:
                    return await self._fallback.understand_document(source)

            title = path.stem.replace("-", " ").replace("_", " ").title() if path.exists() else "Document"
            sections = tuple(line.strip() for line in result.splitlines() if line.strip().startswith(("#", "-", "*")))
            return DocumentUnderstanding(
                title=title,
                summary=result,
                sections=sections,
                metadata={"source": source, "llm_analyzed": True},
            )
        except Exception:
            return await self._fallback.understand_document(source)

    async def _call_llm(
        self,
        prompt: str,
        *,
        image_data: str | None = None,
        text_content: str | None = None,
    ) -> str:
        """Call the LLM provider with text and/or image content."""
        messages = []
        if image_data:
            # Build multimodal message (OpenAI format)
            content: list[dict[str, Any]] = [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{image_data}",
                        "detail": "high",
                    },
                },
            ]
            messages.append({"role": "user", "content": content})
        elif text_content:
            messages.append({"role": "user", "content": f"{prompt}\n\n---\n{text_content}"})
        else:
            messages.append({"role": "user", "content": prompt})

        complete = getattr(self._llm_provider, "complete", None)
        if complete is None:
            raise RuntimeError("LLM provider has no complete method")
        return str(await complete(messages))


def _load_image_base64(path: str) -> str | None:
    """Load an image file as base64 string."""
    try:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return None
        if p.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}:
            return None
        data = p.read_bytes()
        return base64.b64encode(data).decode("ascii")
    except Exception:
        return None
