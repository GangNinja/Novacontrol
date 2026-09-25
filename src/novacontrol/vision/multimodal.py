"""Multimodal vision processor that leverages LLM providers for real image understanding.

When an LLM provider with multimodal support is available, this processor
sends images for real analysis. Otherwise, it falls back to the basic
deterministic processor.
"""

from __future__ import annotations

import base64
import io
import os
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
            image_data = _load_image_base64_for_vision(source)
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

    async def understand_screen(
        self, source: str, *, question: str = ""
    ) -> ScreenUnderstanding:
        """Understand a screen capture using LLM if available.

        ``question`` is what the user actually asked about the screen ("why
        isn't the button working?"). A generic screen description answers a
        question nobody asked: the model has the pixels, and the one thing it
        most needs to know is what to look FOR. When no question is given the
        structured checklist is used, so an explicit Describe Screen click
        behaves exactly as before.
        """
        if not self.has_llm:
            return await self._fallback.understand_screen(source, question=question)

        try:
            image_data = _load_image_base64_for_vision(source)
            if not image_data:
                return await self._fallback.understand_screen(source, question=question)

            prompt = _screen_prompt(question)
            result = await self._call_llm(prompt, image_data=image_data)
            return ScreenUnderstanding(summary=result, windows=(), text=result)
        except Exception:
            return await self._fallback.understand_screen(source, question=question)

    async def detect_windows(self, source: str) -> tuple[DetectedWindow, ...]:
        """Detect windows using LLM if available."""
        if not self.has_llm:
            return await self._fallback.detect_windows(source)

        try:
            image_data = _load_image_base64_for_vision(source)
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
            image_data = _load_image_base64_for_vision(source)
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
                image_data = _load_image_base64_for_vision(source)
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


def _screen_prompt(question: str) -> str:
    """The vision prompt: the user's own question first, then the checklist.

    The checklist stays even when a question is present, because the model is
    being asked to be *useful*, not terse: answering "why isn't the button
    working" needs the visible error text and the interactive elements, and a
    model that only answers the literal question tends to omit them.
    """
    checklist = (
        "Describe:\n"
        "1. What application(s) are visible\n"
        "2. The current state/interface\n"
        "3. Any errors, warnings, or important text\n"
        "4. Any interactive elements (buttons, forms, menus)\n"
        "Be concise but thorough."
    )
    asked = " ".join(str(question or "").split())
    if not asked:
        return f"Analyze this screenshot. {checklist}"
    return (
        f"Analyze this screenshot and answer this question about it:\n{asked}\n\n"
        f"Answer from what is actually visible, and say so when the screen does not "
        f"show it. {checklist}"
    )


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


def vision_max_image_side() -> int:
    """Longest edge (px) a capture may keep before it is sent to the model.

    Every screenshot pixel becomes image tokens the model must process, and on
    a CPU-only Ollama box that token count IS most of the wall-clock time: a
    full 1920x1080 PNG costs several times the inference of the same screen at
    1280 px. The default keeps typical UI text legible while roughly halving the
    pixel count of a 1080p capture. Set
    ``NOVACONTROL_VISION_MAX_IMAGE_SIDE`` to 0 (or negative) to send captures
    at full resolution.
    """
    raw = os.environ.get("NOVACONTROL_VISION_MAX_IMAGE_SIDE", "1280").strip()
    try:
        return int(float(raw))
    except ValueError:
        return 1280


def _load_image_base64_for_vision(path: str) -> str | None:
    """Load an image for the vision model, downscaled to the token budget.

    Coordinates are unaffected by the downscale: multimodal models answer on a
    normalized grid (0-1000 in this codebase) and every caller maps that onto
    the ORIGINAL image dimensions, so returned points stay valid full-resolution
    screen coordinates. Falls back to the untouched file bytes when Pillow is
    unavailable, the file cannot be decoded, or it already fits the budget —
    the model never sees a "prepared" image that silently lost its content.
    """
    limit = vision_max_image_side()
    if limit <= 0:
        return _load_image_base64(path)
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is optional at import time
        return _load_image_base64(path)
    try:
        with Image.open(path) as img:
            width, height = img.size
            longest = max(width, height)
            if longest <= limit:
                return _load_image_base64(path)
            scale = limit / longest
            resized = img.convert("RGB").resize(
                (max(1, round(width * scale)), max(1, round(height * scale))),
                # Resampling.LANCZOS (not the module-level alias) is what the
                # typed Pillow stub exposes, so this survives mypy.
                Image.Resampling.LANCZOS,
            )
        buffer = io.BytesIO()
        # PNG (lossless) on purpose: the resolution cut alone is the latency
        # saving, and JPEG artifacts would cost the text legibility that
        # locating a labeled button depends on.
        resized.save(buffer, format="PNG", optimize=True)
    except Exception:
        return _load_image_base64(path)
    return base64.b64encode(buffer.getvalue()).decode("ascii")
