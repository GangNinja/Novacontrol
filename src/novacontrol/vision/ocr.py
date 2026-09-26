"""OCR as its own, replaceable step — the cheap half of the vision pipeline.

The pipeline's first question is not "what does this picture mean?" but "can
this be answered from the text in it?". Answering that needs an OCR engine, and
an OCR engine is a different kind of thing from a vision model: it is
deterministic, it is far cheaper, and on a CPU-only machine it is the difference
between a second and a minute.

So OCR is a named, swappable engine rather than a step buried inside a prompt.
The shipped engine REUSES the OCR NovaControl already had — Windows' own engine,
driven from ``desktop.vision_guide``, which returns real recognized words with
real screen coordinates (the desktop's click guidance depends on those
coordinates). Nothing is re-implemented here; this module only gives that
engine a boundary, a normalized record, and an honest ``available`` flag so the
manager can tell "no text in this image" from "no OCR on this machine".
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class OcrWord:
    """One recognized word, with where it was found.

    ``line`` groups words into the line they were read on, which is what turns
    a bag of words back into readable text. Coordinates are in the image's own
    pixels; an engine with no geometry (a plain text file) reports zeros and
    says so through ``kind``.
    """

    text: str
    line: int = 0
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    #: Which engine produced this word — provenance, not decoration.
    kind: str = "ocr"

    @property
    def has_geometry(self) -> bool:
        return bool(self.width or self.height)

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "line": self.line,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "kind": self.kind,
        }


@runtime_checkable
class OcrEngine(Protocol):
    """Anything that can read text out of an image source."""

    @property
    def name(self) -> str:
        """A short, stable identifier for status output."""

    @property
    def available(self) -> bool:
        """Whether this engine can read anything at all on this machine."""

    async def read(self, source: str) -> tuple[OcrWord, ...]:
        """The words this engine can find, empty when it finds none."""


class NullOcrEngine:
    """The engine a machine without OCR gets: it reads nothing, and says so."""

    name = "none"
    available = False

    async def read(self, source: str) -> tuple[OcrWord, ...]:
        del source
        return ()


class TextFileOcrEngine:
    """A text file is already text — the one source needing no engine at all."""

    name = "text-file"
    available = True
    _SUFFIXES = frozenset({".txt", ".md", ".log", ".json", ".yaml", ".yml", ".csv"})

    async def read(self, source: str) -> tuple[OcrWord, ...]:
        path = Path(source)
        try:
            if not path.is_file() or path.suffix.lower() not in self._SUFFIXES:
                return ()
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ()
        lines = raw.splitlines()
        return tuple(
            OcrWord(text=line.strip(), line=index, kind=self.name)
            for index, line in enumerate(lines)
            if line.strip()
        )


class WindowsOcrEngine:
    """Windows' built-in OCR, through the helper the desktop already uses.

    The import is deferred and the call is total: on a non-Windows machine, or
    when the engine, the language pack or the subprocess is unavailable, this
    returns nothing rather than raising. A caller learns the difference between
    "unsupported" (``available``) and "found no text" (an empty read) from those
    two answers, which is what keeps a refusal honest.
    """

    name = "windows-ocr"

    def __init__(self) -> None:
        self._available = sys.platform.startswith("win")

    @property
    def available(self) -> bool:
        return self._available

    async def read(self, source: str) -> tuple[OcrWord, ...]:
        if not self._available:
            return ()
        # Deferred: desktop.vision_guide pulls in the LLM integration and the
        # normalize layer, and this module must stay importable from the vision
        # package without dragging the desktop in behind it.
        from novacontrol.desktop.vision_guide import _ocr_words

        words = await _ocr_words(source)
        if not words:
            return ()
        return tuple(_to_word(word) for word in words if str(word.get("text", "")).strip())


class ChainOcrEngine:
    """Try each engine in order; the first that reads anything wins.

    Ordering is the whole configuration: text files are free and exact, the
    platform engine is the real work, and a null engine at the end makes
    "nothing could read this" an explicit result instead of an exception.
    """

    def __init__(self, engines: Sequence[OcrEngine]) -> None:
        self._engines = tuple(engines)

    @property
    def name(self) -> str:
        return "+".join(engine.name for engine in self._engines) or "none"

    @property
    def available(self) -> bool:
        return any(engine.available for engine in self._engines)

    def engines(self) -> tuple[OcrEngine, ...]:
        return self._engines

    async def read(self, source: str) -> tuple[OcrWord, ...]:
        for engine in self._engines:
            words = await engine.read(source)
            if words:
                return words
        return ()


def default_ocr_engine() -> OcrEngine:
    """The OCR this build ships: text files, then the platform engine."""
    return ChainOcrEngine((TextFileOcrEngine(), WindowsOcrEngine()))


def group_lines(words: Iterable[OcrWord]) -> tuple[str, ...]:
    """Word records back into the lines a person would read.

    Grouping by the engine's own line index (not by y-coordinates) keeps this
    exact for engines that know their lines and harmless for engines that only
    report one line per word: either way the order is the engine's reading
    order, which is the only order that is reproducible.
    """
    lines: dict[int, list[str]] = {}
    for word in words:
        text = word.text.strip()
        if text:
            lines.setdefault(word.line, []).append(text)
    return tuple(" ".join(lines[key]) for key in sorted(lines))


def _to_word(record: Any) -> OcrWord:
    """A word record from the desktop helper into this module's shape."""
    return OcrWord(
        text=str(record.get("text", "")).strip(),
        line=_int(record.get("line"), 0),
        x=_int(record.get("x"), 0),
        y=_int(record.get("y"), 0),
        width=_int(record.get("w"), 0),
        height=_int(record.get("h"), 0),
        kind=WindowsOcrEngine.name,
    )


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
