"""Interchangeable capture backends, selected by declared capability.

NovaForge used to have ONE capture source: GDI ``CopyFromScreen`` of the display,
cropped to the window. That source is honest while the target is foreground and
useless when it is covered — the crop then contains the covering window, and the
full-desktop fallback is a frame of the desktop, not the target.

Two backends ship here and they are not interchangeable promises:

* :class:`DisplayCropBackend` copies the composited display and crops to the
  window rectangle. It requires the target to be FOREGROUND (a covered window's
  rectangle contains whatever is drawn on top of it).
* :class:`WindowSurfaceBackend` asks the window itself to render, via
  ``PrintWindow(PW_RENDERFULLCONTENT)``. It can see a covered window, but some
  renderers answer black, so every frame is content-validated before use.

The :class:`CaptureRouter` chooses by DECLARED capability plus live state, then
validates the pixels. When no wired backend can honestly see the target it
returns a refusal that names the condition — it never falls back to a source it
knows is not the target.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from novacontrol.desktop.capture_validation import (
    FrameInspection,
    FrameVerdict,
    inspect_frame,
    reason_code,
    validate_frame,
)

__all__ = [
    "BackendCapabilities",
    "CaptureBackend",
    "CaptureOutcome",
    "CaptureRouter",
    "CaptureTarget",
    "DisplayCropBackend",
    "FrameCapture",
    "WindowSurfaceBackend",
    "crop_to_bounds",
]


def _field(result: Any, name: str, default: Any = None) -> Any:
    """Read one field from a capture seam's mapping OR dataclass result."""
    if isinstance(result, Mapping):
        return result.get(name, default)
    return getattr(result, name, default)


@dataclass(frozen=True, slots=True)
class CaptureTarget:
    """The facts a backend needs about the window it is asked to capture."""

    handle: int
    title: str
    bounds: tuple[int, int, int, int]
    minimized: bool = False
    visible: bool = True
    foreground: bool = False


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """What one backend CLAIMS, as data a caller can print and test."""

    backend: str
    captures: str  # "window_surface" | "display"
    unfocused: bool
    occluded: bool
    minimized: bool
    requires_foreground: bool
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "captures": self.captures,
            "unfocused": self.unfocused,
            "occluded": self.occluded,
            "minimized": self.minimized,
            "requires_foreground": self.requires_foreground,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class FrameCapture:
    """One backend's raw answer, before content validation."""

    ok: bool
    backend: str
    source: str
    path: str = ""
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    elapsed_ms: float = 0.0
    error: str = ""
    #: The 48x27 paint read the source wrote beside this frame, when it wrote
    #: one. Validation uses it instead of decoding the frame; "" means the
    #: caller must read the frame itself. It must describe the FRAME's region —
    #: a screen-wide read is not a substitute for a window crop.
    thumbnail: str = ""


class CaptureBackend(Protocol):
    """The contract every capture source implements."""

    #: A read-only property, not a mutable attribute: both shipped backends are
    #: frozen dataclasses, and a mutable protocol member would make them
    #: invariant (an unassignable tuple of concrete backends).
    @property
    def name(self) -> str: ...

    def capabilities(self) -> BackendCapabilities: ...

    def available(self) -> tuple[bool, str]: ...

    async def capture_frame(self, path: str, target: CaptureTarget) -> FrameCapture: ...


def crop_to_bounds(
    path: Path, bounds: tuple[int, int, int, int]
) -> tuple[int, int, int, str, str]:
    """Crop a full-screen frame to ``bounds`` in place.

    The capture worker writes PHYSICAL pixels in one DPI-aware space and the
    window bounds came from that same worker, so no scale conversion is needed.
    Pillow is already a dependency of the vision layer; when it is unavailable
    the full frame is kept and the source says ``screen`` — a truthful fallback
    rather than a fabricated window-sized frame.
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - optional here, required for cropping
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return 0, 0, size, "screen", "Pillow is unavailable; serving the full screen frame."
    try:
        with Image.open(path) as image:
            width, height = image.size
            left = max(0, min(width, bounds[0]))
            top = max(0, min(height, bounds[1]))
            right = max(0, min(width, bounds[2]))
            bottom = max(0, min(height, bounds[3]))
            if right - left < 8 or bottom - top < 8:
                size = path.stat().st_size
                return width, height, size, "screen", (
                    "The target window is outside the captured display; showing the full screen."
                )
            cropped = image.crop((left, top, right, bottom))
            cropped_size = cropped.size
            clipped = (
                bounds[0] < 0
                or bounds[1] < 0
                or bounds[2] > width
                or bounds[3] > height
            )
        # Saved AFTER the source handle closes: Windows refuses to replace a
        # file that is still open for reading.
        cropped.save(path, format="PNG")
        return (
            cropped_size[0],
            cropped_size[1],
            path.stat().st_size,
            "window_crop",
            "The target window extends beyond the captured display; this frame is clipped."
            if clipped
            else "",
        )
    except Exception as exc:  # noqa: BLE001 - keep serving a real frame
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return (
            0, 0, size, "screen",
            f"The window crop failed ({type(exc).__name__}); full frame kept.",
        )


@dataclass(frozen=True, slots=True)
class DisplayCropBackend:
    """The historical source: full-display copy, cropped to the window.

    Declares ``unfocused=False`` because it is a DESKTOP capture: while the
    target is covered, its rectangle on screen belongs to the covering window.
    """

    capture: Callable[[str], Awaitable[Any]]
    name: str = "gdi_display"

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            backend=self.name,
            captures="display",
            unfocused=False,
            occluded=False,
            minimized=False,
            requires_foreground=True,
            notes=(
                "GDI CopyFromScreen of the primary display, cropped to the window. "
                "Cannot see a covered or minimized window; only valid while the "
                "target is foreground."
            ),
        )

    def available(self) -> tuple[bool, str]:
        if self.capture is None:
            return False, "No display-capture seam is wired."
        return True, ""

    async def capture_frame(self, path: str, target: CaptureTarget) -> FrameCapture:
        if target.minimized or not target.visible:
            # The rectangle of a minimized/hidden window is not on the display;
            # copying the display and calling the result a window frame is the
            # H02 bug. Refuse here too, not only in the router, so a caller that
            # drives the backend directly gets the same answer.
            return FrameCapture(
                ok=False, backend=self.name, source="display", path=path,
                error="target_minimized" if target.minimized else "target_hidden",
            )
        try:
            result = await self.capture(path)
        except Exception as exc:  # noqa: BLE001 - a failed capture is a reported result
            return FrameCapture(
                ok=False, backend=self.name, source="display", path=path,
                error=f"{type(exc).__name__}: {exc}",
            )
        # Two seam shapes reach here: the desktop runner's mapping carries
        # ``fresh`` (its verified screenshot answer), the capture worker's own
        # result carries ``ok``. Read both rather than assuming one, or a wired
        # worker would look like a permanently stale capture.
        fresh = bool(_field(result, "fresh", _field(result, "ok", False)))
        elapsed = float(_field(result, "capture_ms", 0.0) or 0.0)
        if not fresh:
            error = str(
                _field(result, "verification", "") or _field(result, "error", "") or "not fresh"
            )
            return FrameCapture(
                ok=False, backend=self.name, source="display", path=path,
                error=error, elapsed_ms=elapsed,
            )
        width, height, size, source, note = crop_to_bounds(Path(path), target.bounds)
        if source != "window_crop":
            return FrameCapture(
                ok=False, backend=self.name, source=source, path=path,
                width=width, height=height, size_bytes=size, elapsed_ms=elapsed,
                error=note or f"The capture did not contain the target (source: {source}).",
            )
        return FrameCapture(
            ok=True, backend=self.name, source=source, path=path,
            width=width, height=height, size_bytes=size, elapsed_ms=elapsed,
            error=note,
        )


@dataclass(frozen=True, slots=True)
class WindowSurfaceBackend:
    """The target's own surface, via ``PrintWindow(PW_RENDERFULLCONTENT)``.

    It declares ``unfocused``/``occluded`` support because the API asks the
    window to render itself rather than copying the screen. ``minimized`` is
    FALSE: a minimized window has no surface to render, and every observation
    here returned a blank bitmap for one. Content validation is still required
    because a renderer may answer black even when visible.
    """

    capture_window: Callable[[int, str], Awaitable[Any]]
    name: str = "printwindow"

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            backend=self.name,
            captures="window_surface",
            unfocused=True,
            occluded=True,
            minimized=False,
            requires_foreground=False,
            notes=(
                "PrintWindow with PW_RENDERFULLCONTENT. Captures the window's own surface "
                "even while it is covered, but some DirectX renderers answer black; every "
                "frame is blank-checked."
            ),
        )

    def available(self) -> tuple[bool, str]:
        if self.capture_window is None:
            return False, "No window-surface seam is wired."
        return True, ""

    async def capture_frame(self, path: str, target: CaptureTarget) -> FrameCapture:
        if target.minimized:
            return FrameCapture(
                ok=False, backend=self.name, source="window_surface", path=path,
                error="target_minimized",
            )
        try:
            result = await self.capture_window(target.handle, path)
        except Exception as exc:  # noqa: BLE001 - a failed capture is a reported result
            return FrameCapture(
                ok=False, backend=self.name, source="window_surface", path=path,
                error=f"{type(exc).__name__}: {exc}",
            )
        ok = bool(_field(result, "ok", _field(result, "fresh", False)))
        error = str(_field(result, "error", "") or "")
        if not ok:
            return FrameCapture(
                ok=False, backend=self.name, source="window_surface", path=path,
                error=error or "the window surface could not be captured",
            )
        return FrameCapture(
            ok=True, backend=self.name, source="window_surface", path=path,
            width=int(_field(result, "width", 0) or 0),
            height=int(_field(result, "height", 0) or 0),
            size_bytes=int(_field(result, "size_bytes", 0) or 0),
            elapsed_ms=float(_field(result, "elapsed_ms", 0) or 0),
            error=error,
            # PrintWindow's side car covers exactly the window, so it is the
            # frame's own region and safe to judge the frame with.
            thumbnail=str(_field(result, "thumbnail", "") or ""),
        )


@dataclass(frozen=True, slots=True)
class CaptureOutcome:
    """The router's answer: the frame plus every fact used to judge it."""

    ok: bool
    backend: str = ""
    source: str = ""
    path: str = ""
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    elapsed_ms: float = 0.0
    reason: str = ""
    #: The backend's own note about an ACCEPTED frame (``FrameCapture.error``).
    #: It is how a clipped frame says so: the crop to the display can succeed
    #: while the window extends past it, and dropping that note made a partial
    #: frame indistinguishable from the whole target (NF-09 refuses to describe
    #: a clipped frame, so the fact has to survive the router).
    note: str = ""
    verdict: dict[str, Any] = field(default_factory=dict)
    inspection: dict[str, Any] | None = None
    attempts: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "backend": self.backend,
            "source": self.source,
            "path": self.path,
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
            "elapsed_ms": self.elapsed_ms,
            "reason": self.reason,
            "note": self.note,
            "verdict": self.verdict,
            "inspection": self.inspection,
            "attempts": list(self.attempts),
        }


class CaptureRouter:
    """Choose a backend by capability, then validate its pixels.

    Selection is deliberately boring:

    1. A minimized/hidden/off-screen target captures nothing (no backend here
       can render a minimized window; claiming otherwise would be the H02 bug).
    2. Backends whose declared capability allows the target's current state are
       tried in order (window surface first).
    3. The first frame whose CONTENT validates as the target wins. A backend
       that produces a fresh but blank/desktop frame is recorded as a failed
       attempt and the next eligible backend is tried.
    4. If none succeeds, the outcome names the reasons and is NOT live.
    """

    def __init__(
        self,
        backends: list[CaptureBackend] | tuple[CaptureBackend, ...],
        *,
        max_age_seconds: float = 3.0,
        content_inspection: bool = True,
    ) -> None:
        self._backends = tuple(backends)
        self.max_age_seconds = max_age_seconds
        self.content_inspection = content_inspection
        self.last_content_hash = ""

    def capabilities(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for backend in self._backends:
            available, reason = backend.available()
            rows.append(
                {
                    **backend.capabilities().to_dict(),
                    "available": available,
                    "unavailable_reason": reason,
                }
            )
        return rows

    def _eligible(self, backend: CaptureBackend, target: CaptureTarget) -> tuple[bool, str]:
        available, reason = backend.available()
        if not available:
            return False, reason or "the backend is unavailable"
        caps = backend.capabilities()
        if target.minimized and not caps.minimized:
            return False, "target_minimized"
        if not target.visible:
            return False, "target_hidden"
        if (caps.requires_foreground or not caps.unfocused) and not target.foreground:
            return False, "target_not_foreground"
        return True, ""

    @staticmethod
    def _discard_side_car(thumbnail: str) -> None:
        """Remove a frame's paint side car once it has been read.

        A file that cannot be removed is not worth failing a frame over: the
        frame prunes sweep this suffix too, so a locked read is a delay, not a
        leak.
        """
        if not thumbnail:
            return
        try:
            Path(thumbnail).unlink()
        except OSError:
            pass

    def _corroborated_dark(
        self, reads: list[dict[str, Any]], eligible: list[CaptureBackend]
    ) -> dict[str, Any] | None:
        """The last read, when EVERY eligible backend independently read black.

        A single black image cannot tell a legitimate dark scene from a renderer
        that refused to paint, so one signal is never enough: this returns None
        unless every backend that can see the target produced a frame AND every
        one of those frames was blank. A backend that produced no frame, or one
        frame with any paint in it, breaks the agreement and the blank refusal
        stands. ``None`` also when fewer than two backends were eligible: with
        only one source there is nothing to corroborate with.
        """
        if len(reads) < 2 or len(reads) != len(eligible):
            return None
        if not all(entry.get("blank") for entry in reads):
            return None
        return reads[-1]

    async def capture(
        self,
        path: str,
        target: CaptureTarget,
        *,
        previous_hash: str = "",
    ) -> CaptureOutcome:
        attempts: list[dict[str, Any]] = []
        #: One entry per backend that produced a frame, with everything needed to
        #: judge it. Kept so a blank read can be corroborated by the OTHER
        #: backends' own reads instead of by a second capture.
        reads: list[dict[str, Any]] = []
        #: How many backends produced a frame, INDEPENDENT of whether it was
        #: inspected: with content inspection off a frame exists but has no
        #: verdict, and "the backend produced no frame" would be false.
        produced = 0
        if target.minimized:
            return self._refuse(
                "target_minimized",
                "The target window is minimized; no capture backend can render it.",
                attempts,
            )
        if not target.visible:
            return self._refuse("target_hidden", "The target window is not visible.", attempts)
        eligible = [backend for backend in self._backends if self._eligible(backend, target)[0]]
        if not eligible:
            reasons = {self._eligible(backend, target)[1] for backend in self._backends}
            reason = (
                "target_not_foreground"
                if "target_not_foreground" in reasons
                else "capture_unavailable"
            )
            return self._refuse(
                reason,
                "No wired capture backend can see the target in its current state "
                f"({', '.join(sorted(r for r in reasons if r)) or 'none wired'}).",
                attempts,
            )
        for backend in eligible:
            result = await backend.capture_frame(path, target)
            if not result.ok:
                error = str(result.error or "capture failed")
                attempts.append(
                    {"backend": backend.name, "source": result.source, "ok": False,
                     "reason": error,
                     # The backend's OWN code, kept as a CODE: the aggregate
                     # refusal below must not re-read it as prose.
                     "code": reason_code(error)}
                )
                continue
            produced += 1
            inspection: FrameInspection | None = None
            if self.content_inspection:
                # The source's own 48x27 paint read, when it wrote one: the same
                # statistics from 3888 bytes instead of a full PNG decode (~35 ms
                # warm, 300+ ms cold at 5.2 MP). It is consumed here and removed:
                # it is evidence for this verdict, not a second artefact to keep.
                inspection = inspect_frame(
                    path, previous_hash=previous_hash, thumbnail=result.thumbnail
                )
                self._discard_side_car(result.thumbnail)
            # Age is measured from the file the backend wrote, not from its
            # reported latency: a slow capture is not a stale frame, and a
            # backend that returns an old file must not be called current.
            try:
                age_seconds: float | None = max(
                    0.0, time.time() - Path(result.path or path).stat().st_mtime
                )
            except OSError:
                age_seconds = None
            if inspection is not None:
                reads.append(
                    {
                        "backend": backend,
                        "result": result,
                        "inspection": inspection,
                        "age_seconds": age_seconds,
                        "blank": bool(inspection.blank),
                    }
                )
            verdict: FrameVerdict = validate_frame(
                capture_ok=True,
                backend=backend.name,
                source=result.source,
                target_visible=not target.minimized and target.visible,
                foreground=target.foreground,
                inspection=inspection,
                age_seconds=age_seconds,
                max_age_seconds=self.max_age_seconds,
            )
            if not verdict.livable:
                attempts.append(
                    {"backend": backend.name, "source": result.source, "ok": False,
                     "reason": verdict.reason, "content": verdict.content}
                )
                continue
            attempts.append(
                {"backend": backend.name, "source": result.source, "ok": True,
                 "reason": verdict.content}
            )
            if inspection is not None and inspection.content_hash:
                self.last_content_hash = inspection.content_hash
            return CaptureOutcome(
                ok=True,
                backend=backend.name,
                source=result.source,
                path=result.path,
                width=result.width,
                height=result.height,
                size_bytes=result.size_bytes,
                elapsed_ms=round(result.elapsed_ms, 1),
                reason=verdict.reason,
                note=result.error,
                verdict=verdict.to_dict(),
                inspection=inspection.to_dict() if inspection is not None else None,
                attempts=tuple(attempts),
            )
        corroborated = self._corroborated_dark(reads, eligible)
        if corroborated is not None:
            # Every backend that can see the target independently read black.
            # Two independent captures agreeing is the second signal a single
            # blank frame cannot provide, so the read is served as DARK — with
            # the limitation attached, never as painted content.
            dark_verdict: FrameVerdict = validate_frame(
                capture_ok=True,
                backend=corroborated["backend"].name,
                source=corroborated["result"].source,
                target_visible=not target.minimized and target.visible,
                foreground=target.foreground,
                inspection=corroborated["inspection"],
                age_seconds=corroborated["age_seconds"],
                max_age_seconds=self.max_age_seconds,
                dark_confirmed=True,
            )
            if dark_verdict.livable:
                witnesses = [entry["backend"].name for entry in reads]
                attempts[-1] = {
                    "backend": corroborated["backend"].name,
                    "source": corroborated["result"].source,
                    "ok": True,
                    "reason": dark_verdict.content,
                    "content": dark_verdict.content,
                    "agreement": witnesses,
                }
                self.last_content_hash = corroborated["inspection"].content_hash or ""
                return CaptureOutcome(
                    ok=True,
                    backend=corroborated["backend"].name,
                    source=corroborated["result"].source,
                    path=corroborated["result"].path,
                    width=corroborated["result"].width,
                    height=corroborated["result"].height,
                    size_bytes=corroborated["result"].size_bytes,
                    elapsed_ms=round(corroborated["result"].elapsed_ms, 1),
                    reason=dark_verdict.reason,
                    note=corroborated["result"].error,
                    verdict=dark_verdict.to_dict(),
                    inspection=corroborated["inspection"].to_dict(),
                    attempts=tuple(attempts),
                )
        last = attempts[-1] if attempts else {}
        text = str(last.get("reason") or "Every eligible capture backend was rejected.")
        # The refusal carries a STABLE code (prose is the reason, never the code):
        # the session pauses on the code and the panel shows the reason. When NO
        # backend produced a frame, the backend's own structured code wins —
        # re-deriving it from its prose is how a "target_unreadable" refusal was
        # once reported as a generic rejection while claiming `capture_ok`, which
        # the panel then displayed as an identity mismatch for a frame that never
        # existed.
        code = reason_code(text) if produced else str(last.get("code") or reason_code(text))
        # The refusal's content must describe what was actually read. A frame that
        # was read and judged blank is a blank frame, not an identity mismatch;
        # a refusal with no frame at all is unverified, because "not_target" is a
        # claim about a frame that never existed.
        content = "unverified"
        if produced:
            read_content = str(last.get("content") or "")
            content = (
                read_content
                if read_content in {"blank", "repeated", "dark", "unverified", "not_target"}
                else "not_target"
            )
        return self._refuse(
            code, text, attempts, frame_present=bool(produced), content=content
        )

    def _refuse(
        self,
        code: str,
        reason: str,
        attempts: list[dict[str, Any]],
        *,
        frame_present: bool = False,
        content: str = "unverified",
    ) -> CaptureOutcome:
        """A refusal, and whether a frame existed to refuse.

        ``capture_ok`` answers "did a backend produce pixels?" — the same fact
        ``validate_frame`` is given. A refusal that produced no frame must not
        claim it did, and a validated rejection (blank / not the target / stale)
        must not lose the fact that a frame existed.
        """
        return CaptureOutcome(
            ok=False,
            reason=reason,
            verdict={
                "capture_ok": bool(frame_present),
                "target_visible": code not in {"target_minimized", "target_hidden"},
                "fresh": False,
                "content_ok": False,
                "livable": False,
                "reason": reason,
                "content": content,
                "code": code,
                "limitations": [],
            },
            attempts=tuple(attempts),
        )
