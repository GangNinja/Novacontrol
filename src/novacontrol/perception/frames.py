"""Frame sources: where a picture comes from, as a replaceable seam.

Phase 21 processes FRAMES, and a frame is not the same thing as a file path: a
frame carries an identity, a sequence and a timestamp, and whoever produced it
may be a saved image, a screenshot, a camera, or a test fixture. This module is
that boundary — the same discipline ``vision/providers.py`` applies to models
applied to inputs.

Two rules shape it.

**A frame is a REFERENCE plus metadata, never pixels.** ``Frame.path`` names a
file that exists on disk; the raw bytes stay there. That is what keeps a frame
publishable on the event bus (``event_payload``) and storable in a scene record
without dragging an image into a log, a journal or another process.

**A source that cannot work says so instead of failing.** ``available`` is the
honest flag: a build with no camera backend reports ``available = False`` with a
reason, and the engine records the capability as unavailable rather than
pretending nothing was asked. A test fixture, a saved image and a screenshot are
all first-class; the camera is deliberately the one that has to earn its way in.

Nothing here captures anything by itself either: the screen and camera sources
take a CAPTURE CALLABLE, which is how the application wires the already
approval-gated desktop capture in without this module importing the desktop.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from novacontrol.perception.models import as_bool

__all__ = [
    "CameraFrameSource",
    "Frame",
    "FrameSource",
    "ImageFrameSource",
    "ScreenFrameSource",
    "SequenceFrameSource",
    "TestFrameSource",
    "frame_timestamp",
]


def frame_timestamp() -> str:
    """The moment a frame was acquired, ISO-8601 UTC — the one clock used."""
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class Frame:
    """One picture, identified and located but not held.

    ``width``/``height``/``size_bytes`` are ``None`` when the file could not be
    probed — a caller learns "this is not a decodable image" from the probe, and
    an unreadable frame still travels with its identity so the failure can be
    reported about a specific frame rather than about nothing.
    """

    frame_id: str = field(default_factory=lambda: uuid4().hex[:12])
    source_id: str = ""
    sequence: int = 0
    timestamp: str = field(default_factory=frame_timestamp)
    path: str = ""
    width: int | None = None
    height: int | None = None
    format: str = ""
    size_bytes: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def located(self) -> bool:
        return bool(self.path.strip())

    @property
    def measured_extent(self) -> bool:
        return bool(self.width and self.height)

    def with_metadata(self, extra: Mapping[str, Any]) -> Frame:
        """A copy carrying more metadata, for a source that learns it late."""
        return replace(self, metadata={**dict(self.metadata), **dict(extra)})

    def event_payload(self) -> dict[str, Any]:
        """What may travel on the event bus: identity and size, never the path.

        The path is a reference to the user's file, and a watcher that wants the
        picture can ask the engine for it; an event is a notification, not a
        delivery mechanism for someone's screen content.
        """
        payload: dict[str, Any] = {
            "frame_id": self.frame_id,
            "source_id": self.source_id,
            "sequence": self.sequence,
        }
        if self.width is not None:
            payload["width"] = self.width
        if self.height is not None:
            payload["height"] = self.height
        return payload

    def to_dict(self) -> dict[str, Any]:
        """The stored shape: a reference, its identity, and no pixel data."""
        return {
            "frame_id": self.frame_id,
            "source_id": self.source_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "path": self.path,
            "width": self.width,
            "height": self.height,
            "format": self.format,
            "size_bytes": self.size_bytes,
            "metadata": dict(self.metadata),
        }


@runtime_checkable
class FrameSource(Protocol):
    """Anything that can produce frames one at a time, in acquisition order."""

    @property
    def source_id(self) -> str:
        """A short, stable identifier for status output and provenance."""

    @property
    def kind(self) -> str:
        """What sort of source this is: image, sequence, screen, camera, test."""

    @property
    def available(self) -> bool:
        """Whether this source can produce a frame at all on this machine."""

    @property
    def unavailable_reason(self) -> str:
        """Why not, in the same words a result would use — empty when it can."""

    async def read(self) -> Frame | None:
        """The next frame, or ``None`` at the natural end of a finite source.

        Raises ``FrameSourceError`` when the source is broken rather than
        finished: "the camera is unplugged" and "that was the last frame" are
        different facts, and the engine reports them differently.
        """

    async def close(self) -> None:
        """Release whatever the source holds. Must be safe to call twice."""


class FrameSourceError(RuntimeError):
    """A source could not produce a frame — never means "there was no frame"."""

    def __init__(self, source_id: str, reason: str) -> None:
        self.source_id = source_id
        self.reason = reason
        super().__init__(f"{source_id}: {reason}")


@dataclass(frozen=True, slots=True)
class ImageInfo:
    """What a file says about itself: its pixel size, its format, its bytes."""

    width: int
    height: int
    format: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "format": self.format,
            "size_bytes": self.size_bytes,
        }


def probe_image_file(path: str) -> ImageInfo | None:
    """The file's own dimensions and format, or ``None`` when it is not an image.

    Pillow is imported here rather than at module import: the dependency is
    declared, but a build without it must still be able to report an honest
    "could not read an image" instead of failing to start. Only the header is
    read for the size (Pillow does not decode the pixels for ``size``/``format``),
    so probing a 4K screenshot is not a decode of it.
    """
    candidate = Path(str(path or ""))
    try:
        if not candidate.is_file():
            return None
        size_bytes = candidate.stat().st_size
    except OSError:
        return None
    if size_bytes <= 0:
        return None
    try:
        from PIL import Image

        with Image.open(candidate) as image:
            width, height = image.size
            image_format = str(image.format or "").lower()
    except Exception:  # noqa: BLE001 - any decode failure is "not a readable image"
        return None
    if width <= 0 or height <= 0:
        return None
    return ImageInfo(width=int(width), height=int(
        height), format=image_format, size_bytes=size_bytes)


def describe_frame(
    path: str,
    *,
    source_id: str,
    sequence: int,
    metadata: Mapping[str, Any] | None = None,
) -> Frame:
    """A frame for ``path``, probed when it can be and honest when it cannot.

    An unreadable path is NOT rejected here: the frame is built with unknown
    dimensions and the reader downstream reports why. Rejecting it here would
    lose the identity the failure has to be reported against.
    """
    info = probe_image_file(path)
    return Frame(
        source_id=source_id,
        sequence=sequence,
        path=str(path or ""),
        width=info.width if info else None,
        height=info.height if info else None,
        format=info.format if info else "",
        size_bytes=info.size_bytes if info else None,
        metadata=dict(metadata or {}),
    )


class ImageFrameSource:
    """One image file, read as a frame each time it is asked.

    Re-reading a still image produces a new frame with the same path: that is
    what a caller asking twice for the same file means, and the sequence number
    keeps the two distinguishable in a scene list.
    """

    kind = "image"

    def __init__(self, path: str, *, source_id: str = "image") -> None:
        self._path = str(path or "")
        self._source_id = source_id
        self._reads = 0

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def available(self) -> bool:
        return bool(self._path.strip())

    @property
    def unavailable_reason(self) -> str:
        return "" if self.available else "no image path was given"

    async def read(self) -> Frame | None:
        if not self.available:
            raise FrameSourceError(self._source_id, "no image path was given")
        sequence = self._reads
        self._reads += 1
        return describe_frame(self._path, source_id=self._source_id, sequence=sequence)

    async def close(self) -> None:
        return None


class SequenceFrameSource:
    """A finite list of image paths, handed out one frame per read.

    The end of the list returns ``None`` — the natural end of a finite source.
    Nothing wraps around: a stream that silently restarted would make a temporal
    diff compare the last frame of one pass with the first of the next.
    """

    kind = "sequence"

    def __init__(self, paths: Sequence[str], *, source_id: str = "sequence") -> None:
        self._paths = tuple(str(path or "") for path in paths)
        self._source_id = source_id
        self._index = 0

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def available(self) -> bool:
        return bool(self._paths)

    @property
    def unavailable_reason(self) -> str:
        return "" if self.available else "the sequence is empty"

    @property
    def remaining(self) -> int:
        return max(0, len(self._paths) - self._index)

    async def read(self) -> Frame | None:
        if self._index >= len(self._paths):
            return None
        index = self._index
        self._index += 1
        return describe_frame(self._paths[index], source_id=self._source_id, sequence=index)

    async def close(self) -> None:
        self._index = len(self._paths)


class ScreenFrameSource:
    """The desktop screen, through a capture callable the application wires in.

    The callable is the seam that keeps this module out of the desktop package
    and out of the approval question: the application passes the already
    approval-gated capture (the same one the Vision panel's Describe Screen
    uses), so a screen frame is obtained exactly the way a screenshot has always
    been obtained. A build with no capture callable is unavailable with a
    reason, never a crash.
    """

    kind = "screen"

    def __init__(
        self,
        capture: Any | None = None,
        *,
        source_id: str = "screen",
        unavailable_reason: str = "no screen capture is wired",
    ) -> None:
        self._capture = capture
        self._source_id = source_id
        self._reason = unavailable_reason
        self._reads = 0

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def available(self) -> bool:
        return callable(self._capture)

    @property
    def unavailable_reason(self) -> str:
        return "" if self.available else self._reason

    async def read(self) -> Frame | None:
        capture = self._capture
        if not callable(capture):
            raise FrameSourceError(self._source_id, self._reason)
        captured = capture()
        if hasattr(captured, "__await__"):
            captured = await captured
        path, metadata = _capture_result(captured)
        if not path:
            raise FrameSourceError(self._source_id, "the screen capture produced no image")
        sequence = self._reads
        self._reads += 1
        return describe_frame(path, source_id=self._source_id, sequence=sequence, metadata=metadata)

    async def close(self) -> None:
        return None


class CameraFrameSource:
    """A camera, through a capture callable — and honestly unavailable without one.

    This build ships no camera backend, and that is a fact this class states
    rather than hides: with no capture callable it reports ``available = False``
    and the reason, so a request for a camera frame ends as UNAVAILABLE naming
    the missing provider instead of failing somewhere deeper.
    """

    kind = "camera"

    def __init__(
        self,
        capture: Any | None = None,
        *,
        source_id: str = "camera",
        unavailable_reason: str = "no camera backend is wired",
    ) -> None:
        self._capture = capture
        self._source_id = source_id
        self._reason = unavailable_reason
        self._reads = 0

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def available(self) -> bool:
        return callable(self._capture)

    @property
    def unavailable_reason(self) -> str:
        return "" if self.available else self._reason

    async def read(self) -> Frame | None:
        capture = self._capture
        if not callable(capture):
            raise FrameSourceError(self._source_id, self._reason)
        captured = capture()
        if hasattr(captured, "__await__"):
            captured = await captured
        path, metadata = _capture_result(captured)
        if not path:
            raise FrameSourceError(self._source_id, "the camera produced no image")
        sequence = self._reads
        self._reads += 1
        return describe_frame(path, source_id=self._source_id, sequence=sequence, metadata=metadata)

    async def close(self) -> None:
        return None


class TestFrameSource:
    """Deterministic frames for tests and dry runs — the one non-live source.

    It reads the SAME files an image source would, in order, and it can be told
    exactly how many times to repeat them (the static-scene case). Being a test
    source is not a licence to fabricate: every frame it hands out points at a
    real file, so the deterministic path exercises the real decoder.
    """

    kind = "test"

    def __init__(
        self,
        paths: Sequence[str],
        *,
        repeats: int = 1,
        source_id: str = "test",
    ) -> None:
        self._paths = tuple(str(path or "") for path in paths)
        self._repeats = max(1, int(repeats))
        self._source_id = source_id
        self._index = 0
        self._total = len(self._paths) * self._repeats

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def available(self) -> bool:
        return bool(self._paths)

    @property
    def unavailable_reason(self) -> str:
        return "" if self.available else "the test source has no frames"

    @property
    def remaining(self) -> int:
        return max(0, self._total - self._index)

    async def read(self) -> Frame | None:
        if not self._paths or self._index >= self._total:
            return None
        index = self._index
        self._index += 1
        return describe_frame(
            self._paths[index % len(self._paths)],
            source_id=self._source_id,
            sequence=index,
            metadata={"repeat": index // len(self._paths)},
        )

    async def close(self) -> None:
        self._index = self._total


def _capture_result(captured: Any) -> tuple[str, dict[str, Any]]:
    """A capture's answer as (path, metadata) — tolerant, and never a guess.

    The application's capture returns a mapping with the saved file under one of
    a few conventional keys; a bare string is accepted as the path itself. A
    shape this function does not recognise yields an empty path, which the
    source turns into an honest error instead of reading a file named after
    a dict.
    """
    if isinstance(captured, str):
        return captured.strip(), {}
    if isinstance(captured, Mapping):
        rows = {str(key): value for key, value in captured.items()}
        path = ""
        for key in ("screenshot", "path", "frame_path", "image"):
            candidate = rows.get(key)
            if isinstance(candidate, str) and candidate.strip():
                path = candidate.strip()
                break
        metadata: dict[str, Any] = {
            key: rows[key]
            for key in ("captured", "width", "height", "monitor")
            if key in rows and not isinstance(rows[key], (bytes, bytearray))
        }
        if rows.get("captured") is False:
            return "", metadata
        return path, metadata
    return "", {}


def source_kind_of(source: FrameSource) -> str:
    """A source's kind, tolerating a duck-typed source that declares none."""
    kind = getattr(source, "kind", "")
    return str(kind or "unknown")


def source_is_available(source: FrameSource) -> bool:
    """Whether a source can produce a frame, defaulting to "no" when unsure.

    Conservative on purpose: a source whose availability cannot be read is not
    assumed usable, because the alternative is a request that claims SUCCESS by
    never running anything.
    """
    value = getattr(source, "available", None)
    if isinstance(value, bool):
        return value
    return as_bool(value, False)


def monotonic_ms() -> float:
    """Milliseconds from the monotonic clock — the only clock latency may use."""
    return time.monotonic() * 1000.0

