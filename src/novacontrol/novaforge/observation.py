"""NF-08 A — structured game-state observation.

One observation is the only thing a decision may act on. It is deliberately a
*value*: timestamped, normalized, and explicit about what was measured versus
what was inferred.

The rules this module exists to enforce:

* **Raw and inferred are separate.** ``evidence`` holds facts that were read
  (a window title, OCR words, a frame path); ``interpretations`` holds anything
  a model or a rule concluded from them. A reader can therefore tell "the title
  said READY" from "the screen looks ready".
* **Missing evidence is a state, not a success.** No frame is ``unavailable``;
  an old frame is ``stale``; a frame with nothing readable is ``degraded``.
  None of them is silently promoted to ``observed``.
* **Every answer can be "cannot tell".** Confidence is ``None`` when nothing
  measured it (never 0.0, which would read as "measured and worthless"), and
  ``uncertainty`` names each source that did not contribute and why.
* **Processing is bounded.** Every probe is awaited under its own timeout, and
  a timeout becomes a stated uncertainty rather than a hang.

The builder composes seams it is given — a window probe, a capture, an OCR
reader and an optional describer — so the same code runs against the desktop
worker in the application and against fixtures in tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast, runtime_checkable

from .visual import VisualInterpretation

#: Statuses an observation may report. Only ``OBSERVED`` is actionable.
OBSERVATION_STATUSES = ("observed", "degraded", "stale", "unavailable")

DEFAULT_PROBE_TIMEOUT = 2.0
DEFAULT_MAX_FRAME_AGE = 2.0
#: A confidence under this is reported as uncertain even when a source supplied
#: one; above it the figure is kept, because it is still the source's number.
LOW_CONFIDENCE = 0.5


def utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


@runtime_checkable
class OcrReader(Protocol):
    """Anything that can read words out of an image source."""

    @property
    def available(self) -> bool:
        """Whether this reader can read anything on this machine."""

    async def read(self, source: str) -> Sequence[Any]:
        """The words this reader found (objects with ``text``/geometry)."""


class OcrUnavailable(RuntimeError):
    """The engine cannot read on this host; distinct from "the frame is blank"."""


@dataclass(slots=True)
class ScreenTextReader:
    """Adapts an existing text-reading callable to the ``OcrReader`` protocol.

    ``read_text`` is the application's OCR seam (``screen_text``). It returns
    ``None`` when the engine is unavailable, which becomes ``OcrUnavailable``
    rather than an empty reading — the two mean opposite things. Windows' OCR
    reports words without a confidence, so ``confidence`` stays ``None``.
    """

    read_text: Callable[[str], Awaitable[Any]]
    name: str = "windows.media.ocr"
    engine_available: bool = True
    confidence: float | None = None

    @property
    def available(self) -> bool:
        return bool(self.engine_available and self.read_text is not None)

    async def read(self, source: str) -> Sequence[Any]:
        words = await self.read_text(source)
        if words is None:
            raise OcrUnavailable("The OCR engine is unavailable on this host.")
        return cast(Sequence[Any], words)


@runtime_checkable
class FrameDescriber(Protocol):
    """An optional vision-language step. Absent means "no description"."""

    @property
    def available(self) -> bool:
        """Whether a model is installed and usable right now."""

    async def describe(self, source: str) -> Mapping[str, Any]:
        """A description plus its own confidence, or a mapping with a reason."""


@dataclass(frozen=True, slots=True)
class OcrReading:
    """What OCR read from one frame."""

    text: str
    words: tuple[dict[str, Any], ...] = ()
    confidence: float | None = None
    engine: str = ""
    note: str = ""

    @property
    def empty(self) -> bool:
        return not self.text.strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "words": [dict(word) for word in self.words],
            "confidence": self.confidence,
            "engine": self.engine,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class InterpretedSignal:
    """Something INFERRED from the observation, kept apart from the evidence."""

    kind: str
    value: str
    source: str
    confidence: float | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "value": self.value,
            "source": self.source,
            "confidence": self.confidence,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class GameObservation:
    """One timestamped, normalized read of the target environment.

    ``status`` is the whole contract: a decision loop may act only when it is
    ``observed``. ``degraded`` means something was measured but not enough to
    interpret (no readable text); ``stale`` means the frame is older than the
    allowed age; ``unavailable`` means the target could not be read at all.
    """

    observed_at: str
    status: str
    target: dict[str, Any]
    frame: dict[str, Any] | None = None
    ocr: OcrReading | None = None
    vision: dict[str, Any] | None = None
    interpretations: tuple[InterpretedSignal, ...] = ()
    changes: tuple[str, ...] = ()
    #: Where a vision model contradicted measured evidence. A conflict is
    #: recorded, never resolved in the model's favour: the measured facts stay
    #: the facts and the observation stops claiming a confidence it cannot have.
    conflicts: tuple[str, ...] = ()
    confidence: float | None = None
    uncertainty: tuple[str, ...] = ()
    evidence: tuple[dict[str, Any], ...] = ()
    reason: str = ""

    @property
    def actionable(self) -> bool:
        """Only a fully OBSERVED read may drive an action."""
        return self.status == "observed"

    @property
    def title(self) -> str:
        return str(self.target.get("title") or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at,
            "status": self.status,
            "actionable": self.actionable,
            "target": dict(self.target),
            "frame": dict(self.frame) if self.frame else None,
            "ocr": self.ocr.to_dict() if self.ocr else None,
            "vision": dict(self.vision) if self.vision else None,
            "interpretations": [item.to_dict() for item in self.interpretations],
            "changes": list(self.changes),
            "conflicts": list(self.conflicts),
            "confidence": self.confidence,
            "uncertainty": list(self.uncertainty),
            "evidence": [dict(item) for item in self.evidence],
            "reason": self.reason,
        }


#: ``probe(handle)`` -> a geometry-like object (or None). Same seam the
#: objective verifier uses, so observation and verification read ONE source.
WindowProbe = Callable[[int], Awaitable[Any | None]]
#: ``capture(title)`` -> the capture mapping (``fresh``/``path``/dimensions).
CaptureProbe = Callable[[str], Awaitable[Mapping[str, Any]]]


class ObservationBuilder:
    """Composes the existing seams into one honest observation.

    Nothing here opens a window, injects input or paints a picture: it reads,
    normalizes and reports. ``describe`` is optional and its absence is reported
    as "no vision model" rather than being papered over.
    """

    def __init__(
        self,
        *,
        probe: WindowProbe,
        capture: CaptureProbe | None = None,
        ocr: OcrReader | None = None,
        describe: FrameDescriber | None = None,
        max_frame_age: float = DEFAULT_MAX_FRAME_AGE,
        probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
        ocr_timeout: float = DEFAULT_PROBE_TIMEOUT,
        describe_timeout: float = DEFAULT_PROBE_TIMEOUT,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.probe = probe
        self.capture = capture
        self.ocr = ocr
        self.describe = describe
        self.max_frame_age = max(0.05, float(max_frame_age))
        self.probe_timeout = max(0.05, float(probe_timeout))
        self.ocr_timeout = max(0.05, float(ocr_timeout))
        self.describe_timeout = max(0.05, float(describe_timeout))
        self.now = now

    # -- capability ---------------------------------------------------------

    def capability(self) -> dict[str, Any]:
        """What this builder can actually measure on this host."""
        ocr_ready = bool(self.ocr is not None and getattr(self.ocr, "available", False))
        vision_detail: dict[str, Any] | None = None
        capability = getattr(self.describe, "capability", None)
        if self.describe is None:
            vision_ready = False
        elif callable(capability):
            # The describer's own report is the authority. Asking ``available``
            # first and then calling ``capability()`` probes the model TWICE —
            # with Ollama unreachable that is two bounded health checks (5 s
            # each) to answer one capability read.
            try:
                vision_detail = dict(capability())
            except Exception as exc:  # noqa: BLE001 - a capability report cannot fail a read
                vision_detail = {
                    "available": False,
                    "reason": (
                        "The vision describer could not report its capability: "
                        f"{type(exc).__name__}."
                    ),
                }
            vision_ready = bool(vision_detail.get("available"))
        else:
            vision_ready = bool(getattr(self.describe, "available", False))
        return {
            "observation": {
                "available": self.capture is not None,
                "capture": self.capture is not None,
                "ocr": ocr_ready,
                "vision_model": vision_ready,
                "vision": vision_detail,
                "source": "desktop window probe, capture and the configured OCR engine",
                "reason": (
                    "" if self.capture is not None
                    else "No capture seam is wired, so no frame can be described."
                ),
            },
            "interpretation": {
                "available": ocr_ready or vision_ready,
                "ocr": ocr_ready,
                "vision_model": vision_ready,
                "vision": vision_detail,
                "reason": (
                    "Text can be read by OCR"
                    + (" and frames can be described by a vision model." if vision_ready else ".")
                    if ocr_ready or vision_ready
                    else "No OCR engine is available and no vision model is wired, so "
                    "the observation carries geometry and frame evidence only."
                ),
            },
        }

    # -- observation --------------------------------------------------------

    async def observe(
        self,
        handle: int,
        *,
        pid: int | None = None,
        previous: GameObservation | None = None,
        describe_prompt: str = "Describe the visible game state in one sentence.",
    ) -> GameObservation:
        """Read the target once and return what can honestly be said about it."""
        observed_dt = self.now()
        observed_at = _stamp(observed_dt)
        uncertainty: list[str] = []
        evidence: list[dict[str, Any]] = []

        geometry = await self._call(self.probe(handle), self.probe_timeout)
        if geometry is None:
            return GameObservation(
                observed_at=observed_at,
                status="unavailable",
                target={"handle": handle, "pid": pid},
                uncertainty=("The target window could not be read.",),
                reason=(
                    "The window probe returned nothing, so no title, geometry or frame "
                    "exists for this observation."
                ),
            )

        title = str(getattr(geometry, "title", "") or "")
        observed_pid = getattr(geometry, "pid", None)
        target = {
            "title": title,
            "handle": int(getattr(geometry, "handle", handle) or handle),
            "pid": int(observed_pid if observed_pid is not None else (pid or 0) or 0) or None,
            "width": getattr(geometry, "width", None),
            "height": getattr(geometry, "height", None),
            "foreground": getattr(geometry, "foreground", None),
        }
        evidence.append(
            {
                "method": "window_probe",
                "observed": title,
                "reference": f"window:{target['handle']}",
                "at": observed_at,
            }
        )

        if previous is not None and previous.title and previous.title != title:
            target["previous_title"] = previous.title

        if self.capture is None:
            uncertainty.append("No capture seam is wired, so no frame was read.")
            return GameObservation(
                observed_at=observed_at,
                status="unavailable",
                target=target,
                uncertainty=tuple(uncertainty),
                evidence=tuple(evidence),
                reason="The target is open but this build cannot capture it.",
            )

        captured = await self._call(self.capture(title), self.probe_timeout)
        if not isinstance(captured, Mapping):
            uncertainty.append("The capture step returned no result.")
            return GameObservation(
                observed_at=observed_at,
                status="unavailable",
                target=target,
                uncertainty=tuple(uncertainty),
                evidence=tuple(evidence),
                reason="The capture step returned nothing; the target was not observed.",
            )

        frame = self._frame(captured, observed_dt)
        if frame is None:
            uncertainty.append("The capture reported no usable frame.")
            return GameObservation(
                observed_at=observed_at,
                status="unavailable",
                target=target,
                uncertainty=tuple(uncertainty),
                evidence=tuple(evidence),
                reason=(
                    "Capture returned no fresh frame: "
                    + str(captured.get("verification") or captured.get("reason") or "no reason")
                ),
            )
        # The probe identified the window by handle; the capture cropped by
        # title. If those disagree, the pixels are not the target's — and no
        # OCR or model may be asked about them (NF-09's frame/target binding).
        captured_handle = frame.get("handle")
        probed_handle = target.get("handle")
        considered = isinstance(captured_handle, int) and not isinstance(captured_handle, bool)
        if (
            considered
            and captured_handle
            and probed_handle is not None
            and int(captured_handle) != int(probed_handle)
        ):
            uncertainty.append(
                f"The capture came from window {captured_handle}, not the observed "
                f"window {target['handle']}."
            )
            return GameObservation(
                observed_at=observed_at,
                status="unavailable",
                target=target,
                frame=frame,
                uncertainty=tuple(uncertainty),
                evidence=tuple(evidence),
                reason=(
                    "The captured frame belongs to a different window than the one "
                    "probed; it is never read as this target's state."
                ),
            )
        captured_pid = frame.get("pid")
        probed_pid = target.get("pid")
        if (
            isinstance(captured_pid, int)
            and not isinstance(captured_pid, bool)
            and captured_pid
            and probed_pid is not None
            and int(captured_pid) != int(probed_pid)
        ):
            uncertainty.append(
                f"The capture came from pid {captured_pid}, not the observed pid {target['pid']}."
            )
            return GameObservation(
                observed_at=observed_at,
                status="unavailable",
                target=target,
                frame=frame,
                uncertainty=tuple(uncertainty),
                evidence=tuple(evidence),
                reason=(
                    "The captured frame belongs to a different process than the one "
                    "probed; it is never read as this target's state."
                ),
            )
        evidence.append(
            {
                "method": "frame",
                "observed": str(frame.get("path") or ""),
                "reference": f"frame:{frame.get('path') or ''}",
                "at": str(frame.get("captured_at") or observed_at),
                "note": f"age {frame.get('age_seconds')} s",
            }
        )
        if not frame["fresh"]:
            uncertainty.append(
                f"The frame is {frame['age_seconds']} s old (limit {self.max_frame_age} s)."
            )
            return GameObservation(
                observed_at=observed_at,
                status="stale",
                target=target,
                frame=frame,
                uncertainty=tuple(uncertainty),
                evidence=tuple(evidence),
                reason="A stale frame is never acted on: it does not describe the target now.",
            )

        ocr_reading = await self._read_text(str(frame.get("path") or ""), uncertainty)
        if ocr_reading is not None:
            evidence.append(
                {
                    "method": "ocr",
                    "observed": ocr_reading.text[:200],
                    "reference": f"ocr:{ocr_reading.engine}",
                    "at": observed_at,
                    "note": ocr_reading.note,
                }
            )

        vision, interpretations, conflicts = await self._describe(
            str(frame.get("path") or ""),
            describe_prompt,
            uncertainty,
            observed_at,
            frame=frame,
            target=target,
            ocr=ocr_reading,
        )

        changes = self._changes(previous, target, ocr_reading)
        # A window TITLE is text that was read, not decoration: a policy that
        # decides from it ("press while the marker is absent") has real evidence
        # even when OCR finds nothing. Degraded therefore means NOTHING could be
        # interpreted — no title and no recognised text — and every absent source
        # is still named in ``uncertainty`` so a reader can see what was missing.
        has_text = ocr_reading is not None and not ocr_reading.empty
        if has_text or title.strip():
            status = "observed"
            reason = (
                "The target was observed: title, a fresh frame and its text were read."
                if has_text
                else (
                    "The target was observed from its title and a fresh frame; no screen "
                    "text was recognised, so any decision rests on the title alone."
                )
            )
        else:
            status = "degraded"
            reason = (
                "The target was captured, but its title was blank and no readable text or "
                "vision description exists, so its state cannot be interpreted."
                if vision is None
                else (
                    "The frame was read by a vision model but OCR produced no text and the "
                    "title was blank; the observation is usable only through that description."
                )
            )

        confidences = [
            item
            for item in (
                ocr_reading.confidence if ocr_reading is not None else None,
                _seconds((vision or {}).get("confidence")),
            )
            if item is not None
        ]
        confidence = min(confidences) if confidences else None
        if conflicts:
            # Measured evidence still stands, but the read is contested: a number
            # that was computed before the disagreement cannot describe it.
            confidence = None
            uncertainty.append(
                "The vision model contradicts measured evidence; this observation is "
                "reported as uncertain rather than choosing one account."
            )
        if confidence is None:
            uncertainty.append("No source reported a confidence for this observation.")

        return GameObservation(
            observed_at=observed_at,
            status=status,
            target=target,
            frame=frame,
            ocr=ocr_reading,
            vision=vision,
            interpretations=tuple(interpretations),
            changes=changes,
            conflicts=conflicts,
            confidence=confidence,
            uncertainty=tuple(dict.fromkeys(uncertainty)),
            evidence=tuple(evidence),
            reason=reason,
        )

    # -- internals ----------------------------------------------------------

    async def _call(self, awaitable: Awaitable[Any], timeout: float) -> Any:
        """Await a probe under its own bound; a timeout is None, not a raise."""
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            return None
        except Exception:  # noqa: BLE001 - a broken probe is an unreadable source
            return None

    def _frame(self, captured: Mapping[str, Any], observed: datetime) -> dict[str, Any] | None:
        path = str(captured.get("path", "") or "")
        if not path:
            return None
        captured_at_raw = str(captured.get("captured_at") or captured.get("at") or "")
        age = _seconds(captured.get("age_seconds"))
        captured_at: datetime | None = None
        if captured_at_raw:
            try:
                captured_at = datetime.fromisoformat(captured_at_raw)
            except ValueError:
                captured_at = None
        if age is None and captured_at is not None:
            age = max(0.0, (observed - captured_at.astimezone(UTC)).total_seconds())
        # The capture's ``fresh`` means "this call produced this frame"; the
        # builder's ``max_frame_age`` is the tighter bound on acting on it. Both
        # must hold, so a frame the capture calls fresh while its own age is 30s
        # is STALE here — otherwise the age bound would be decorative.
        fresh = (
            bool(captured.get("fresh", True))
            and age is not None
            and age <= self.max_frame_age
        )
        note = str(captured.get("note") or "")
        clipped = bool(captured.get("clipped")) or "clipped" in note.lower()
        return {
            "path": path,
            "captured_at": captured_at_raw or None,
            "age_seconds": age,
            "fresh": fresh,
            "clipped": clipped,
            "width": captured.get("width"),
            "height": captured.get("height"),
            "size_bytes": captured.get("size_bytes"),
            "note": note,
            "source": str(captured.get("source", "window-cropped capture")),
            # Present when the capture seam reports which window it cropped.
            "handle": captured.get("handle"),
            "pid": captured.get("pid"),
        }

    async def _read_text(
        self, path: str, uncertainty: list[str]
    ) -> OcrReading | None:
        if self.ocr is None:
            uncertainty.append("No OCR engine is wired; text evidence is unavailable.")
            return None
        if not bool(getattr(self.ocr, "available", False)):
            uncertainty.append(
                "The OCR engine reports itself unavailable on this host, so no text was read."
            )
            return None
        try:
            words = await asyncio.wait_for(self.ocr.read(path), timeout=self.ocr_timeout)
        except (TimeoutError, asyncio.TimeoutError):
            uncertainty.append(f"OCR timed out after {self.ocr_timeout:g} s.")
            return None
        except OcrUnavailable as exc:
            uncertainty.append(str(exc))
            return None
        except Exception as exc:  # noqa: BLE001 - an OCR fault is an unreadable source
            uncertainty.append(f"OCR failed: {type(exc).__name__}.")
            return None
        lines: dict[int, list[str]] = {}
        serialised: list[dict[str, Any]] = []
        for index, word in enumerate(words or ()):
            text = str(_word_field(word, "text", "") or "")
            if not text:
                continue
            line = int(_word_field(word, "line", 0) or 0)
            lines.setdefault(line, []).append(text)
            serialised.append(
                {
                    "text": text,
                    "line": line,
                    "x": int(_word_field(word, "x", 0) or 0),
                    "y": int(_word_field(word, "y", 0) or 0),
                    "width": int(_word_field(word, "width", _word_field(word, "w", 0)) or 0),
                    "height": int(_word_field(word, "height", _word_field(word, "h", 0)) or 0),
                    "index": index,
                }
            )
        text = "\n".join(" ".join(parts) for _, parts in sorted(lines.items()))
        engine = str(getattr(self.ocr, "name", type(self.ocr).__name__))
        confidence = _seconds(getattr(self.ocr, "confidence", None))
        note = "" if text else "The engine is available but recognised no text in this frame."
        if not text:
            uncertainty.append(note)
        return OcrReading(
            text=text,
            words=tuple(serialised),
            confidence=confidence,
            engine=engine,
            note=note,
        )

    async def _describe(
        self,
        path: str,
        prompt: str,
        uncertainty: list[str],
        observed_at: str,
        *,
        frame: Mapping[str, Any] | None = None,
        target: Mapping[str, Any] | None = None,
        ocr: OcrReading | None = None,
    ) -> tuple[dict[str, Any] | None, list[InterpretedSignal], tuple[str, ...]]:
        """Ask the installed describer, and keep its answer a CLAIM.

        A describer that implements NF-09's typed ``interpret`` seam is used for
        its status, evidence references, latency and conflicts. The older
        ``describe(path)`` mapping remains accepted so an injected fixture (or a
        third-party describer) is not forced to grow the new method.
        """
        if self.describe is None:
            uncertainty.append(
                "No vision model is wired; the screen was not interpreted beyond its text."
            )
            return None, [], ()
        if not bool(getattr(self.describe, "available", False)):
            uncertainty.append("The configured vision model is not available right now.")
            return None, [], ()
        interpret = getattr(self.describe, "interpret", None)
        if callable(interpret):
            try:
                result = await asyncio.wait_for(
                    interpret(
                        frame=dict(frame or {"path": path}),
                        target=dict(target or {}),
                        observed_at=observed_at,
                        ocr_text=ocr.text if ocr is not None else "",
                        ocr_engine=ocr.engine if ocr is not None else "",
                    ),
                    timeout=self.describe_timeout,
                )
            except (TimeoutError, asyncio.TimeoutError):
                uncertainty.append(
                    f"The vision model did not answer within {self.describe_timeout:g} s."
                )
                return None, [], ()
            except Exception as exc:  # noqa: BLE001 - an inference failure is not evidence
                uncertainty.append(f"The vision model failed: {type(exc).__name__}.")
                return None, [], ()
            if not isinstance(result, VisualInterpretation):
                uncertainty.append("The vision describer returned an unexpected result.")
                return None, [], ()
            payload = result.to_dict()
            for note in result.uncertainty:
                uncertainty.append(note)
            if result.status not in ("interpreted", "degraded"):
                uncertainty.append(
                    result.reason
                    or f"The vision model produced no interpretation ({result.status})."
                )
                return payload, [], result.conflicts
            if not result.description:
                uncertainty.append(
                    result.reason or "The vision model described nothing in this frame."
                )
                return payload, [], result.conflicts
            interpretation = InterpretedSignal(
                kind="visual_interpretation",
                value=result.description,
                source=result.model or result.provider or "vision model",
                # A model's self-assessment is not a measurement; NF-09 keeps it
                # separate (`reported_confidence`) and leaves this None.
                confidence=result.confidence,
                reason=(
                    "Inferred from the frame by a vision model; a model claim, not a "
                    "measurement."
                ),
            )
            return payload, [interpretation], result.conflicts
        try:
            described = await asyncio.wait_for(
                self.describe.describe(path), timeout=self.describe_timeout
            )
        except (TimeoutError, asyncio.TimeoutError):
            uncertainty.append(
                f"The vision model did not answer within {self.describe_timeout:g} s."
            )
            return None, [], ()
        except Exception as exc:  # noqa: BLE001 - an inference failure is not evidence
            uncertainty.append(f"The vision model failed: {type(exc).__name__}.")
            return None, [], ()
        if not isinstance(described, Mapping):
            uncertainty.append("The vision model returned no description.")
            return None, [], ()
        text = str(described.get("description") or described.get("text") or "").strip()
        if not text:
            uncertainty.append(
                "The vision model answered without a description: "
                + str(described.get("reason") or "no reason given")
            )
            return None, [], ()
        confidence = _seconds(described.get("confidence"))
        if confidence is not None and confidence < LOW_CONFIDENCE:
            uncertainty.append(
                f"The vision description is low-confidence ({confidence:g})."
            )
        vision = {
            "description": text,
            "confidence": confidence,
            "model": str(described.get("model") or getattr(self.describe, "name", "vision")),
            "prompt": prompt,
            "at": observed_at,
        }
        interpretation = InterpretedSignal(
            kind="screen_description",
            value=text,
            source=str(vision["model"]),
            confidence=confidence,
            reason="Inferred from the frame by a vision model; not a measurement.",
        )
        return vision, [interpretation], ()

    @staticmethod
    def _changes(
        previous: GameObservation | None,
        target: Mapping[str, Any],
        ocr: OcrReading | None,
    ) -> tuple[str, ...]:
        if previous is None:
            return ()
        changes: list[str] = []
        if previous.title and previous.title != target.get("title"):
            changes.append(f"title changed from {previous.title!r} to {target.get('title')!r}")
        if previous.target.get("width") != target.get("width") or previous.target.get(
            "height"
        ) != target.get("height"):
            changes.append("window geometry changed")
        before = previous.ocr.text if previous.ocr is not None else ""
        after = ocr.text if ocr is not None else ""
        if before != after:
            changes.append("recognised text changed")
        return tuple(changes)


def summarise(observation: GameObservation, *, limit: int = 200) -> dict[str, Any]:
    """A compact, wire-safe view of one observation (for a loop's trajectory)."""
    return {
        "observed_at": observation.observed_at,
        "status": observation.status,
        "title": observation.title[:limit],
        "text": (observation.ocr.text if observation.ocr is not None else "")[:limit],
        "confidence": observation.confidence,
        "changes": list(observation.changes),
        "frame_age_seconds": (observation.frame or {}).get("age_seconds"),
        "uncertainty": list(observation.uncertainty),
        "conflicts": list(observation.conflicts),
        "vision": _vision_summary(observation.vision, limit),
    }


def _vision_summary(vision: Mapping[str, Any] | None, limit: int) -> dict[str, Any] | None:
    """The compact model-interpretation view carried in a loop step."""
    if not vision:
        return None
    return {
        "status": str(vision.get("status") or ""),
        "description": str(vision.get("description") or "")[:limit],
        "model": str(vision.get("model") or ""),
        "provider": str(vision.get("provider") or ""),
        "duration_ms": vision.get("duration_ms"),
        "provenance": str(vision.get("provenance") or "model_interpretation"),
        "reason": str(vision.get("reason") or "")[:limit],
    }


def _word_field(word: Any, name: str, default: Any = None) -> Any:
    """Read one OCR word field from a mapping or an object."""
    if isinstance(word, Mapping):
        return word.get(name, default)
    return getattr(word, name, default)


def merge_uncertainty(*groups: Iterable[str]) -> tuple[str, ...]:
    """De-duplicate uncertainty strings while keeping their order."""
    seen: dict[str, None] = {}
    for group in groups:
        for item in group:
            text = str(item or "").strip()
            if text:
                seen.setdefault(text, None)
    return tuple(seen)


@dataclass(slots=True)
class ObservationHistory:
    """Bound the observations a loop keeps, newest last."""

    limit: int = 12
    items: list[GameObservation] = field(default_factory=list)

    def add(self, observation: GameObservation) -> None:
        self.items.append(observation)
        if len(self.items) > self.limit:
            del self.items[: len(self.items) - self.limit]

    @property
    def latest(self) -> GameObservation | None:
        return self.items[-1] if self.items else None

    def summary(self) -> list[dict[str, Any]]:
        return [summarise(item) for item in self.items]
