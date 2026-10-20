"""NovaForge NF-03: the evidence timeline, comparison and classification.

NF-03 answers one question honestly: **what actually happened during this
session, in what order, and what evidence supports it?**

The rules this module follows, each of which exists because the alternative
silently lies:

* **Sequence is the order; the clock is a label.** Every event carries a
  per-session ``seq`` assigned when it is appended, and an ``elapsed_ms``
  measured with ``time.monotonic()`` from the session's start. The ISO-8601
  ``at`` field is a wall-clock label only: a clock adjustment can make it jump
  backwards, and nothing here sorts by it.
* **Recorded is different from derived.** A session observed by NF-03 has a
  persisted event log. A session that predates NF-03 has no log, so its
  timeline is RECONSTRUCTED from the persisted record — and every such event
  says ``derived: true``. The payload says which kind of timeline it is, so a
  reconstruction is never presented as a recording.
* **A reference resolves or explains itself.** Evidence links are bounded
  references (a frame sequence number, an observation index), never file
  payloads and never a path from a request. Resolution happens at read time
  against the session's own record and frame directory; a frame that retention
  removed becomes ``available: false`` with the reason, and the event stays
  inspectable.
* **Unavailable is not zero, and unknown is not a guess.** A metric that was
  never recorded is ``None`` with a reason. Failure classification names only
  categories the evidence establishes, distinguishes a directly recorded cause
  from an inference, and reports ``unknown`` / ``no_failure_recorded`` rather
  than inventing a root cause or claiming a game was won.
* **Replay is read-only.** Nothing in this module can start, resume, stop or
  send input; it only reads records. The API exposes GET routes only.
* **The log is bounded.** Percentile-style unbounded growth is exactly how a
  "complete" timeline becomes a memory leak: the newest
  ``MAX_EVENTS_PER_SESSION`` events are kept and the number dropped is
  reported, never hidden.

The event bus remains a delivery channel, not a history. These events are
persisted independently of it so a slow or absent subscriber cannot lose
evidence, and the manager marks whether the persisted copy was verified.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .models import SessionRecord, SessionState, utc_now

TIMELINE_VERSION = 1
EVENT_VERSION = 1

#: Memory/disk bound per session. A 1 s loop over an hour produces 3600 frames;
#: keeping every one would make one session's evidence larger than the rest of
#: the application's state, so the newest events win and the loss is reported.
MAX_EVENTS_PER_SESSION = 240

#: A frame/observation reference list is bounded for the same reason.
MAX_EVIDENCE_REFS = 8

#: Payload strings are truncated; the timeline is an index, not a log dump.
MAX_PAYLOAD_STRING = 300
MAX_PAYLOAD_ITEMS = 20

_SAFE_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")

#: event type -> (category, default severity). This table is the ONE place the
#: vocabulary is declared: an unknown type is refused instead of being filed
#: under a made-up category.
EVENT_SPECS: dict[str, tuple[str, str]] = {
    "session.created": ("lifecycle", "info"),
    "session.started": ("lifecycle", "info"),
    "session.pausing": ("lifecycle", "info"),
    "session.paused": ("lifecycle", "info"),
    "session.resumed": ("lifecycle", "info"),
    "session.stopping": ("lifecycle", "info"),
    "session.stopped": ("lifecycle", "info"),
    "session.failed": ("lifecycle", "error"),
    "session.reconciled": ("lifecycle", "warning"),
    "target.validated": ("target", "info"),
    "target.lost": ("target", "warning"),
    "target.changed": ("target", "warning"),
    "target.unreadable": ("target", "warning"),
    "frame.captured": ("capture", "info"),
    "frame.rejected": ("capture", "warning"),
    "capture.failed": ("capture", "error"),
    "observation.recorded": ("evidence", "info"),
    "telemetry.snapshot": ("telemetry", "info"),
    "guardian.intervention": ("guardian", "info"),
    "error.recorded": ("error", "error"),
    "input.cleanup": ("session", "info"),
    "timeline.persistence_failed": ("persistence", "error"),
    # Remediation R1/R2: the action and verification stages. Adding a type here
    # is mandatory, not cosmetic: ``TimelineLog.append`` REFUSES an unknown type
    # and ``_event`` swallows that refusal, so a missing entry makes the event
    # disappear while the code looks like it recorded one.
    "action.proposed": ("action", "info"),
    "action.executed": ("action", "info"),
    "action.denied": ("action", "warning"),
    "action.failed": ("action", "error"),
    "action.aborted": ("action", "warning"),
    "objective.configured": ("objective", "info"),
    "objective.verified": ("objective", "info"),
}

CATEGORIES = tuple(sorted({category for category, _ in EVENT_SPECS.values()}))
SEVERITIES = ("info", "warning", "error")
SOURCES = (
    "session", "capture", "target", "telemetry", "guardian", "store", "record",
    "action", "objective",
)

#: Event types that are genuinely recorded by NF-03 itself.
RECORDED_TYPES = frozenset(EVENT_SPECS)

ORDERING_NOTE = (
    "Events are ordered by their per-session sequence number, assigned when each "
    "event was appended. elapsed_ms is measured with time.monotonic() from the "
    "session's start; the ISO-8601 at field is a wall-clock label and is never "
    "used for ordering."
)

SOURCE_NOTE = (
    "recorded: persisted by NF-03 as the event happened. derived: reconstructed "
    "from the persisted session record because the session predates NF-03's log; "
    "its order follows recorded timestamps, which can tie."
)


# ── small helpers ────────────────────────────────────────────────────


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: an integer too large for a float. A JSON number can be
        # arbitrarily long, and an unrepresentable one is not a crash in the
        # middle of loading a bucket of evidence: it is simply not a usable
        # measurement (see ``_bounded``), so it reads as None.
        return None
    return number if math.isfinite(number) else None


def _bounded(value: Any, *, depth: int = 0) -> Any:
    """A JSON-safe, size-bounded copy of a payload value.

    Bounded in every dimension: strings, items per container, and DEPTH. Both
    container branches stop at depth 2, so a deeply nested payload from a
    corrupted or hand-edited snapshot terminates with a placeholder instead of
    recursing until Python raises out of a load.
    """
    if isinstance(value, str):
        return value[:MAX_PAYLOAD_STRING]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if _finite(value) is not None else None
    if isinstance(value, dict):
        if depth >= 2:
            return "{…}"
        return {
            str(key)[:60]: _bounded(item, depth=depth + 1)
            for key, item in list(value.items())[:MAX_PAYLOAD_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        if depth >= 2:
            return ["…"]
        return [_bounded(item, depth=depth + 1) for item in list(value)[:MAX_PAYLOAD_ITEMS]]
    return str(value)[:MAX_PAYLOAD_STRING]


def frame_ref(session_id: str, seq: int) -> dict[str, Any]:
    """A bounded reference to one captured frame (never the image itself)."""
    return {"kind": "frame", "session_id": session_id, "seq": int(seq)}


def observation_ref(session_id: str, index: int, at: str = "") -> dict[str, Any]:
    """A bounded reference to one observation.

    The record keeps only its newest observations, but an index is NOT unique:
    the loop numbers new observations from the current list length, so trimmed
    entries leave several observations sharing an index. The timestamp is what
    identifies the one that was referenced; the index is kept as a readable
    label, not as the key.
    """
    ref: dict[str, Any] = {"kind": "observation", "session_id": session_id, "index": int(index)}
    if at:
        ref["at"] = str(at)
    return ref


def intervention_ref(session_id: str, index: int, at: str = "") -> dict[str, Any]:
    """A bounded reference to one Guardian intervention.

    The record's intervention list is bounded, so an index shifts as old
    entries are dropped. The timestamp identifies the referenced entry.
    """
    ref: dict[str, Any] = {"kind": "intervention", "session_id": session_id, "index": int(index)}
    if at:
        ref["at"] = str(at)
    return ref


def telemetry_ref(session_id: str) -> dict[str, Any]:
    return {"kind": "telemetry", "session_id": session_id}


def session_ref(session_id: str) -> dict[str, Any]:
    return {"kind": "session", "session_id": session_id}


def _refs(*items: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [dict(item) for item in items if item][:MAX_EVIDENCE_REFS]


# ── the event model ──────────────────────────────────────────────────


@dataclass(slots=True)
class TimelineEvent:
    """One recorded fact, with its sequence, clock labels and evidence."""

    type: str
    seq: int
    session_id: str
    event_id: str
    category: str
    severity: str
    source: str
    at: str
    elapsed_ms: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    correlation_id: str = ""
    derived: bool = False
    version: int = EVENT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "seq": self.seq,
            "type": self.type,
            "category": self.category,
            "severity": self.severity,
            "source": self.source,
            "at": self.at,
            "elapsed_ms": self.elapsed_ms,
            "payload": dict(self.payload),
            "evidence": [dict(ref) for ref in self.evidence],
            "correlation_id": self.correlation_id,
            "derived": self.derived,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "TimelineEvent | None":
        """Tolerant read of a persisted event; a malformed one is skipped.

        A bad row must not take the whole timeline down, and it must not be
        repaired into something that looks recorded: it is simply not returned.
        """
        data = dict(payload or {})
        event_type = str(data.get("type") or "")
        spec = EVENT_SPECS.get(event_type)
        if spec is None:
            return None
        try:
            seq = int(str(data.get("seq")))
        except (TypeError, ValueError):
            return None
        if seq < 1:
            return None
        session_id = str(data.get("session_id") or "")
        if not session_id:
            return None
        evidence = [
            dict(ref)
            for ref in (data.get("evidence") or [])
            if isinstance(ref, dict) and str(ref.get("kind") or "")
        ][:MAX_EVIDENCE_REFS]
        payload_data = data.get("payload")
        return cls(
            type=event_type,
            seq=seq,
            session_id=session_id,
            event_id=str(data.get("event_id") or f"{session_id}-{seq:06d}"),
            category=str(data.get("category") or spec[0]),
            severity=str(data.get("severity") or spec[1]),
            source=str(data.get("source") or "session"),
            at=str(data.get("at") or ""),
            elapsed_ms=_finite(data.get("elapsed_ms")),
            payload=_bounded(payload_data) if isinstance(payload_data, dict) else {},
            evidence=evidence,
            correlation_id=str(data.get("correlation_id") or ""),
            derived=bool(data.get("derived", False)),
            version=_int(data.get("version"), default=EVENT_VERSION),
        )

def summary_text(event: TimelineEvent) -> str:
    """A one-line, evidence-derived description — never a fabricated one."""
    payload = event.payload
    if event.type == "frame.captured":
        return (
            f"Frame {payload.get('frame_seq')} captured"
            + (f" ({payload.get('width')}×{payload.get('height')})" if payload.get("width") else "")
        )
    if event.type == "frame.rejected":
        return f"No fresh frame: {payload.get('reason') or 'the capture was not fresh'}"
    if event.type == "capture.failed":
        return f"Capture failed: {payload.get('error') or 'the capture seam raised'}"
    if event.type in {"target.lost", "target.changed", "target.unreadable"}:
        return f"Target {event.type.split('.')[1]}: {payload.get('detail') or 'recorded by the loop'}"
    if event.type == "target.validated":
        return f"Target validated: {payload.get('title') or 'the pinned window'}"
    if event.type == "guardian.intervention":
        return f"Guardian {payload.get('kind') or 'intervention'}: {payload.get('outcome') or 'recorded'}"
    if event.type == "error.recorded":
        return f"Error ({payload.get('where') or 'unspecified'}): {payload.get('message') or ''}"
    if event.type == "session.stopped":
        return f"Session stopped: {payload.get('end_reason') or 'no reason recorded'}"
    if event.type == "session.failed":
        return f"Session FAILED: {payload.get('reason') or 'an error ended it'}"
    if event.type == "session.reconciled":
        return "Session reconciled after an application restart"
    if event.type == "telemetry.snapshot":
        return (
            f"Telemetry snapshotted: {payload.get('frames')} frame(s), "
            f"{payload.get('capture_failures')} failure(s)"
        )
    if event.type == "input.cleanup":
        return (
            "Input cleanup " + ("ok" if payload.get("ok") is True else "needs review")
            + f" (held after: {payload.get('held_after')})"
        )
    if event.type == "timeline.persistence_failed":
        return f"Timeline persistence failed: {payload.get('reason') or 'unknown reason'}"
    if event.type == "session.paused":
        return f"Session paused: {payload.get('reason') or 'no reason recorded'}"
    return event.type.replace(".", " ").replace("_", " ")


# ── the per-session log ──────────────────────────────────────────────


@dataclass(slots=True)
class TimelineLog:
    """The bounded, ordered, deduplicating event log for ONE session.

    ``append`` is synchronous and assigns the sequence number before returning,
    so two coroutines on the one application event loop cannot observe the same
    number. (There is no await between the assignment and the append.) A
    duplicate ``event_id`` returns the event already stored instead of adding a
    second row, which is what makes retried ingestion safe.
    """

    session_id: str
    events: list[TimelineEvent] = field(default_factory=list)
    next_seq: int = 1
    dropped: int = 0
    persistence: dict[str, Any] = field(default_factory=dict)
    started_monotonic: float | None = field(default=None, repr=False)
    _index: dict[str, int] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.events.sort(key=lambda event: event.seq)
        self._index = {event.event_id: event.seq for event in self.events}
        self.next_seq = max(self.next_seq, max((event.seq for event in self.events), default=0) + 1)
        if not self.persistence:
            self.persistence = {
                "state": "unverified",
                "ok": None,
                "checked_at": "",
                "reason": "Persistence is verified when the session closes.",
                "failures": 0,
            }

    # -- writing ---------------------------------------------------------

    def append(
        self,
        event_type: str,
        *,
        source: str = "session",
        payload: dict[str, Any] | None = None,
        evidence: Iterable[dict[str, Any]] | None = None,
        severity: str | None = None,
        category: str | None = None,
        at: str | None = None,
        elapsed_ms: float | None | object = ...,
        event_id: str | None = None,
        derived: bool = False,
        correlation_id: str = "",
        now_monotonic: float | None = None,
    ) -> TimelineEvent:
        spec = EVENT_SPECS.get(event_type)
        if spec is None:
            raise ValueError(f"Unknown timeline event type: {event_type!r}")
        if event_id and event_id in self._index:
            return self._by_id(event_id)
        seq = self.next_seq
        self.next_seq += 1
        if elapsed_ms is ...:
            elapsed: float | None = None
            if self.started_monotonic is not None:
                clock = time_monotonic() if now_monotonic is None else now_monotonic
                elapsed = round(max(0.0, clock - self.started_monotonic) * 1000.0, 1)
        else:
            elapsed = _finite(elapsed_ms)
        chosen_severity = severity if severity in SEVERITIES else spec[1]
        event = TimelineEvent(
            type=event_type,
            seq=seq,
            session_id=self.session_id,
            event_id=event_id or f"{self.session_id}-{seq:06d}",
            category=category or spec[0],
            severity=chosen_severity,
            source=source if source in SOURCES else "session",
            at=at or utc_now(),
            elapsed_ms=elapsed,
            payload=_bounded(dict(payload or {})) if isinstance(payload, dict) else {},
            evidence=_refs(*(item for item in (evidence or []) if isinstance(item, dict))),
            correlation_id=str(correlation_id),
            derived=derived,
        )
        self.events.append(event)
        self._index[event.event_id] = seq
        if len(self.events) > MAX_EVENTS_PER_SESSION:
            dropped_event = self.events.pop(0)
            self._index.pop(dropped_event.event_id, None)
            self.dropped += 1
        return event

    def _by_id(self, event_id: str) -> TimelineEvent:
        seq = self._index[event_id]
        for event in reversed(self.events):
            if event.seq == seq:
                return event
        raise KeyError(event_id)  # pragma: no cover - index and list cannot disagree

    # -- reading ---------------------------------------------------------

    def filtered(
        self,
        *,
        types: Iterable[str] | None = None,
        categories: Iterable[str] | None = None,
        sources: Iterable[str] | None = None,
        severities: Iterable[str] | None = None,
    ) -> list[TimelineEvent]:
        """Filter WITHOUT reordering: identity and sequence are untouched."""
        return filter_events(
            self.events,
            types=types,
            categories=categories,
            sources=sources,
            severities=severities,
        )

    # -- persistence -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": TIMELINE_VERSION,
            "session_id": self.session_id,
            "next_seq": self.next_seq,
            "dropped": self.dropped,
            "persistence": dict(self.persistence),
            "events": [event.to_dict() for event in self.events],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None, *, session_id: str) -> "TimelineLog":
        data = dict(payload or {})
        events: list[TimelineEvent] = []
        for item in data.get("events") or []:
            if not isinstance(item, dict):
                continue
            event = TimelineEvent.from_dict(item)
            if event is None or event.session_id != session_id:
                continue
            events.append(event)
        # A log written under a different id must not be attached to this
        # session, even if it contains plausible rows.
        stored_id = str(data.get("session_id") or "")
        if stored_id and stored_id != session_id:
            events = []
        log = cls(
            session_id=session_id,
            events=events,
            next_seq=max(1, _int(data.get("next_seq"), default=1)),
            dropped=max(0, _int(data.get("dropped"), default=0)),
            persistence=dict(data.get("persistence") or {}),
        )
        if not log.persistence:
            log.persistence = {
                "state": "unverified",
                "ok": None,
                "checked_at": "",
                "reason": "No persistence result was stored with this timeline.",
                "failures": 0,
            }
        return log


def filter_events(
    events: Iterable[TimelineEvent],
    *,
    types: Iterable[str] | None = None,
    categories: Iterable[str] | None = None,
    sources: Iterable[str] | None = None,
    severities: Iterable[str] | None = None,
) -> list[TimelineEvent]:
    """A filtered view that keeps every event's identity and sequence."""
    wanted_types = {str(item) for item in types} if types is not None else None
    wanted_categories = {str(item) for item in categories} if categories is not None else None
    wanted_sources = {str(item) for item in sources} if sources is not None else None
    wanted_severities = {str(item) for item in severities} if severities is not None else None
    selected: list[TimelineEvent] = []
    for event in events:
        if wanted_types is not None and event.type not in wanted_types:
            continue
        if wanted_categories is not None and event.category not in wanted_categories:
            continue
        if wanted_sources is not None and event.source not in wanted_sources:
            continue
        if wanted_severities is not None and event.severity not in wanted_severities:
            continue
        selected.append(event)
    return selected


def _int(value: Any, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def time_monotonic() -> float:
    """Indirection so tests can freeze the clock without patching the module."""
    import time

    return time.monotonic()


def timeline_namespace(session_id: str) -> str | None:
    """The state-store namespace for one session's log, or None if unsafe.

    A session id becomes a file name, so it must match the generated shape
    exactly; a hand-edited record cannot steer a write outside the store.
    """
    return f"novaforge_timeline_{session_id}" if _SAFE_SESSION_ID.match(session_id) else None


# ── derived timelines (sessions that predate NF-03) ──────────────────


def _state_event_type(previous: str, target: str) -> str:
    mapping = {
        "STARTING": "session.created",
        "RUNNING": "session.resumed" if previous == "PAUSED" else "session.started",
        "PAUSING": "session.pausing",
        "PAUSED": "session.paused",
        "STOPPING": "session.stopping",
        "STOPPED": "session.stopped",
        "FAILED": "session.failed",
    }
    return mapping.get(target, "session.created")


def derived_events(record: SessionRecord) -> list[TimelineEvent]:
    """Reconstruct a timeline from the persisted record, marking it derived."""
    session_id = record.session_id
    rows: list[tuple[str, str, dict[str, Any], list[dict[str, Any]], str]] = []
    for entry in record.history:
        target = str(entry.get("to") or "")
        previous = str(entry.get("from") or "")
        payload = {"from": previous, "to": target}
        if entry.get("reason"):
            payload["reason"] = entry["reason"]
        if target == "STARTING":
            payload.update(
                {
                    "mission": record.mission.name,
                    "objective": record.mission.objective,
                    "target_title": record.mission.target_title,
                }
            )
        if target in {"STOPPED", "FAILED"}:
            payload["end_reason"] = record.end_reason
        rows.append(
            (
                str(entry.get("at") or record.created_at),
                _state_event_type(previous, target),
                payload,
                _refs(session_ref(session_id)),
                "session",
            )
        )
    for frame in record.frames:
        rows.append(
            (
                frame.captured_at,
                "frame.captured",
                {
                    "frame_seq": frame.seq,
                    "width": frame.width,
                    "height": frame.height,
                    "size_bytes": frame.size_bytes,
                    "capture_ms": frame.capture_ms,
                    "capture_mode": frame.capture_mode,
                    "fresh": frame.fresh,
                    "source": frame.source,
                },
                _refs(frame_ref(session_id, frame.seq)),
                "capture",
            )
        )
    for observation in record.observations:
        if observation.evidence:
            continue
        rows.append(
            (
                observation.at,
                "observation.recorded",
                {
                    "index": observation.index,
                    "summary": observation.summary,
                    "evidence": False,
                    "note": observation.note,
                },
                _refs(observation_ref(session_id, observation.index, observation.at)),
                "capture",
            )
        )
    for index, entry in enumerate(record.interventions):
        if not isinstance(entry, dict):
            continue
        rows.append(
            (
                str(entry.get("at") or record.created_at),
                "guardian.intervention",
                {
                    "kind": entry.get("kind"),
                    "reason": entry.get("reason"),
                    "outcome": entry.get("outcome"),
                    "detail": entry.get("detail"),
                },
                _refs(intervention_ref(session_id, index, str(entry.get("at") or ""))),
                "guardian",
            )
        )
    for entry in record.errors:
        rows.append(
            (
                str(entry.get("at") or record.created_at),
                "error.recorded",
                {"message": entry.get("message"), "where": entry.get("where")},
                _refs(session_ref(session_id)),
                "session",
            )
        )
    if record.input_cleanup:
        rows.append(
            (
                record.ended_at or record.started_at or record.created_at,
                "input.cleanup",
                dict(record.input_cleanup),
                _refs(session_ref(session_id)),
                "session",
            )
        )
    # Deterministic order: timestamp first (a tie keeps insertion order, which
    # is the record's own order), then a final seq is assigned.
    rows.sort(key=lambda row: row[0])
    events: list[TimelineEvent] = []
    for index, (at, event_type, payload, evidence, source) in enumerate(rows, start=1):
        events.append(
            TimelineEvent(
                type=event_type,
                seq=index,
                session_id=session_id,
                event_id=f"{session_id}-d{index:06d}",
                category=EVENT_SPECS[event_type][0],
                severity=EVENT_SPECS[event_type][1],
                source=source,
                at=at,
                elapsed_ms=None,
                payload=_bounded(payload),
                evidence=evidence,
                derived=True,
            )
        )
    return events


# ── filtering / pagination validation ───────────────────────────────


class TimelineQueryError(ValueError):
    """An unusable timeline query (unknown filter or bad paging value)."""


def _csv(values: Iterable[str] | None, *, label: str, allowed: Iterable[str]) -> set[str] | None:
    if values is None:
        return None
    cleaned = {str(item).strip() for item in values if str(item).strip()}
    if not cleaned:
        return None
    permitted = set(allowed)
    unknown = sorted(cleaned - permitted)
    if unknown:
        raise TimelineQueryError(
            f"Unknown {label} filter value(s): {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(permitted))}."
        )
    return cleaned


def validate_query(
    *,
    types: Iterable[str] | None = None,
    categories: Iterable[str] | None = None,
    sources: Iterable[str] | None = None,
    severities: Iterable[str] | None = None,
    limit: int = 100,
    after_seq: int = 0,
) -> dict[str, Any]:
    if not (1 <= int(limit) <= 500):
        raise TimelineQueryError("limit must be between 1 and 500.")
    if int(after_seq) < 0:
        raise TimelineQueryError("after_seq must be 0 or greater.")
    return {
        "types": _csv(types, label="type", allowed=RECORDED_TYPES),
        "categories": _csv(categories, label="category", allowed=CATEGORIES),
        "sources": _csv(sources, label="source", allowed=SOURCES),
        "severities": _csv(severities, label="severity", allowed=SEVERITIES),
        "limit": int(limit),
        "after_seq": int(after_seq),
    }


def paginate(
    events: list[TimelineEvent], *, after_seq: int, limit: int
) -> tuple[list[TimelineEvent], dict[str, Any]]:
    """One page in sequence order; paging never changes an event's identity."""
    ordered = sorted(events, key=lambda event: event.seq)
    remaining = [event for event in ordered if event.seq > after_seq]
    page = remaining[:limit]
    next_after = page[-1].seq if page else after_seq
    return page, {
        "limit": limit,
        "count": len(page),
        "total": len(ordered),
        "after_seq": after_seq,
        "next_after_seq": next_after,
        "has_more": len(remaining) > len(page),
        "first_seq": ordered[0].seq if ordered else None,
        "last_seq": ordered[-1].seq if ordered else None,
    }


# ── failure classification ──────────────────────────────────────────


def _evidence_for(
    events: list[TimelineEvent],
    *types: str,
    predicate: Callable[[TimelineEvent], bool] | None = None,
) -> list[dict[str, Any]]:
    """References that justify a classification, bounded and deduplicated."""
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        if event.type not in types:
            continue
        if predicate is not None and not predicate(event):
            continue
        key = event.event_id
        if key in seen:
            continue
        seen.add(key)
        refs.append({"kind": "event", "event_id": key, "seq": event.seq, "type": event.type})
        if len(refs) >= MAX_EVIDENCE_REFS:
            break
    return refs


def classify(record: SessionRecord, events: list[TimelineEvent], *, persistence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Evidence-based failure classification for one session.

    Every entry names the events that justify it. ``direct`` is True only where
    the record states the cause (the loop recorded a capture failure, the session
    ended on its time limit); a classification derived from correlated events is
    reported as an inference. Nothing here claims the objective was achieved:
    NF-03 has no verifier.
    """
    classifications: list[dict[str, Any]] = []
    stop_events = [event for event in events if event.type in {"session.stopped", "session.failed"}]
    end_reason = str(
        (stop_events[-1].payload.get("end_reason") if stop_events else record.end_reason) or ""
    )

    def add(category: str, *, direct: bool, detail: str, refs: list[dict[str, Any]]) -> None:
        classifications.append(
            {
                "category": category,
                "direct": direct,
                "detail": detail,
                "evidence": refs,
            }
        )

    capture_refs = _evidence_for(
        events,
        "capture.failed",
        "frame.rejected",
        "error.recorded",
        predicate=lambda event: event.type != "error.recorded"
        or str(event.payload.get("where") or "") == "capture",
    )
    if capture_refs:
        add(
            "capture_failure",
            direct=True,
            detail=(
                "The capture seam failed or returned a frame that was not fresh; the "
                "session recorded the failure instead of showing the frame as live."
            ),
            refs=capture_refs,
        )
    target_refs = _evidence_for(
        events,
        "target.lost",
        "target.changed",
        "target.unreadable",
        "guardian.intervention",
        predicate=lambda event: event.type != "guardian.intervention"
        or str(event.payload.get("reason") or "") in {"target_lost", "target_changed", "target_unreadable"},
    )
    if target_refs:
        add(
            "target_loss",
            direct=True,
            detail="The pinned target window was lost, replaced or could not be read.",
            refs=target_refs,
        )
    if end_reason == "time_limit":
        add(
            "timeout",
            direct=True,
            detail="The session ended because its configured time limit was reached.",
            refs=_evidence_for(events, "session.stopped") or _evidence_for(events, "guardian.intervention"),
        )
    elif end_reason in {"cancelled", "emergency_stop"}:
        add(
            "guardian_cancellation",
            direct=True,
            detail=f"The session was aborted by the Guardian control path ({end_reason}).",
            refs=_evidence_for(events, "guardian.intervention") or _evidence_for(events, "session.stopped"),
        )
    if end_reason == "error":
        add(
            "unexpected_exception",
            direct=True,
            detail="The observation loop raised; the session ended FAILED.",
            refs=_evidence_for(events, "session.failed", "error.recorded"),
        )
    persistence_state = str((persistence or {}).get("state") or "")
    if persistence_state == "failed":
        add(
            "persistence_failure",
            direct=True,
            detail=str(
                (persistence or {}).get("reason")
                or "The timeline snapshot could not be verified after the session closed."
            ),
            refs=_evidence_for(events, "timeline.persistence_failed"),
        )
    known_wheres = {
        "capture",
        "target",
        "limits",
        "capture_loop",
        "stop",
        "reconcile",
        "timeline_persist",
        "recovery",
    }
    unknown_errors = [
        event
        for event in events
        if event.type == "error.recorded" and str(event.payload.get("where") or "") not in known_wheres
    ]
    if unknown_errors:
        add(
            "unknown",
            direct=False,
            detail=(
                "An error was recorded at a location NF-03 does not classify; the cause "
                "is unknown rather than guessed."
            ),
            refs=_evidence_for(events, "error.recorded")[:MAX_EVIDENCE_REFS],
        )

    completion = {
        "verified": False,
        "state": record.state.value,
        "claimed": record.state is SessionState.COMPLETED,
        "reason": (
            "NF-03 has no objective verifier. A recorded COMPLETED state would have "
            "needed a verifier that does not exist; limits and stops are not success."
        ),
    }
    if not classifications:
        outcome = "no_failure_recorded"
        detail = (
            "No recorded event establishes a failure. That is not the same as success: "
            "NF-03 cannot verify an objective."
        )
    else:
        outcome = "classified"
        detail = f"{len(classifications)} classification(s) supported by recorded evidence."
    return {
        "version": 1,
        "outcome": outcome,
        "detail": detail,
        "end_reason": end_reason,
        "classifications": classifications,
        "completion": completion,
        "note": (
            "A classification marked direct is stated by the record; an inference is "
            "derived from correlated events. Unknown causes stay unknown."
        ),
    }


# ── session comparison ─────────────────────────────────────────────


def _metric(
    key: str,
    label: str,
    left: Any,
    right: Any,
    *,
    available: bool = True,
    reason: str = "",
) -> dict[str, Any]:
    delta: float | None = None
    if available and isinstance(left, (int, float)) and isinstance(right, (int, float)):
        delta = round(float(right) - float(left), 3)
    # A row can be available overall while one side was not measured (one
    # session recorded a p95 and the other could not). Keep the explanation in
    # that case instead of reporting a bare None with no stated cause.
    explained = bool(reason) and (not available or left is None or right is None)
    return {
        "key": key,
        "label": label,
        "left": left,
        "right": right,
        "delta": delta,
        "available": available,
        "reason": reason if explained else "",
    }


def compare(
    left: SessionRecord,
    left_telemetry: dict[str, Any],
    right: SessionRecord,
    right_telemetry: dict[str, Any],
) -> dict[str, Any]:
    """Two sessions side by side, with `None` wherever a figure was not measured."""

    def capture(payload: dict[str, Any]) -> dict[str, Any]:
        return dict(payload.get("capture") or {})

    def session(payload: dict[str, Any]) -> dict[str, Any]:
        return dict(payload.get("session") or {})

    left_capture, right_capture = capture(left_telemetry), capture(right_telemetry)
    left_session, right_session = session(left_telemetry), session(right_telemetry)
    left_latency = dict(left_capture.get("latency_ms") or {})
    right_latency = dict(right_capture.get("latency_ms") or {})
    rows = [
        _metric("state", "State", left.state.value, right.state.value, available=False,
                reason="State is a label, not a measured quantity."),
        _metric("end_reason", "End reason", left.end_reason or None, right.end_reason or None,
                available=False, reason="A recorded reason is text, not a quantity."),
        _metric("duration_seconds", "Duration (wall clock)", left_session.get("duration_seconds"),
                right_session.get("duration_seconds")),
        _metric("active_seconds", "Active time (monotonic)", left_session.get("active_seconds"),
                right_session.get("active_seconds")),
        _metric("paused_seconds", "Paused time (monotonic)", left_session.get("paused_seconds"),
                right_session.get("paused_seconds")),
        _metric("frames", "Frames captured", left_capture.get("frames"), right_capture.get("frames")),
        _metric("stale_frames", "Stale captures", left_capture.get("stale_frames"),
                right_capture.get("stale_frames")),
        _metric("capture_failures", "Capture failures", left_capture.get("capture_failures"),
                right_capture.get("capture_failures")),
        _metric("frame_gaps", "Frame gaps", left_capture.get("frame_gaps"),
                right_capture.get("frame_gaps")),
        _metric("latency_median_ms", "Capture latency median (ms)", left_latency.get("median_ms"),
                right_latency.get("median_ms")),
        _metric("latency_p95_ms", "Capture latency p95 (ms)", left_latency.get("p95_ms"),
                right_latency.get("p95_ms"),
                available=(
                    left_latency.get("p95_ms") is not None
                    or right_latency.get("p95_ms") is not None
                ),
                reason=str(left_latency.get("p95_reason") or right_latency.get("p95_reason") or "")),
        _metric("target_losses", "Target losses", (left_telemetry.get("target") or {}).get("losses"),
                (right_telemetry.get("target") or {}).get("losses")),
        _metric("target_changes", "Target identity changes",
                (left_telemetry.get("target") or {}).get("changes"),
                (right_telemetry.get("target") or {}).get("changes")),
        _metric("errors", "Errors recorded", len(left.errors), len(right.errors)),
        _metric("interventions", "Guardian interventions", len(left.interventions),
                len(right.interventions)),
        _metric("actions_executed", "Actions executed", None, None, available=False,
                reason="NF-03 records no action path, so no action count exists for either session."),
        _metric("verified", "Objective verified", False, False, available=False,
                reason="No objective verifier is attached to any recorded session."),
    ]
    missing_left = [row["label"] for row in rows if row["left"] is None and row["available"]]
    missing_right = [row["label"] for row in rows if row["right"] is None and row["available"]]
    return {
        "version": 1,
        "left": {"session_id": left.session_id, "state": left.state.value,
                 "mission": left.mission.name, "created_at": left.created_at},
        "right": {"session_id": right.session_id, "state": right.state.value,
                  "mission": right.mission.name, "created_at": right.created_at},
        "rows": rows,
        "missing": {
            "left": missing_left,
            "right": missing_right,
            "note": (
                "A missing figure means it was not measured for that session. It is "
                "reported as None, never as zero."
            ),
        },
        "read_only": True,
    }


__all__ = [
    "CATEGORIES",
    "EVENT_SPECS",
    "EVENT_VERSION",
    "MAX_EVENTS_PER_SESSION",
    "MAX_EVIDENCE_REFS",
    "RECORDED_TYPES",
    "SEVERITIES",
    "SOURCES",
    "TIMELINE_VERSION",
    "TimelineEvent",
    "TimelineLog",
    "TimelineQueryError",
    "classify",
    "compare",
    "derived_events",
    "filter_events",
    "frame_ref",
    "intervention_ref",
    "observation_ref",
    "paginate",
    "session_ref",
    "telemetry_ref",
    "ORDERING_NOTE",
    "SOURCE_NOTE",
    "summary_text",
    "timeline_namespace",
    "validate_query",
]
