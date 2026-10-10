"""Tracking objects across frames by SPATIAL continuity — and saying so.

The tracker matches this frame's detections against the previous frame's tracks
and keeps an identity alive while the match is good. That is what makes motion
reportable: without an identity, "a box was here and a box is there" is not a
movement, it is two unrelated observations.

The honest scope of the claim: matching is by intersection-over-union and centre
distance, so what survives is the SAME SPATIAL THING, not a recognized identity.
Nothing here knows that the box labelled ``region`` in frame 1 and the box
labelled ``person`` in frame 9 are the same person — a later provider may simply
label it better, and this tracker records that as a relabelling of one track
rather than as a new object. ``TrackedObject.spatial_only`` is ``True`` on every
track this build produces, and any consumer that wants semantic identity has to
build it on top.

The life cycle is deliberately forgiving for one frame and strict after that:
``OCCLUDED`` on the first miss (something stepped in front of it), ``LOST`` on
the second (report the disappearance), ``REMOVED`` after ``max_missed`` (the
identity is over and its state is reported one last time). A detection that
reappears under the same track before removal resumes its identity, frame count
and all — which is what occlusion handling is FOR.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from novacontrol.perception.models import (
    BBox,
    DetectedObject,
    TemporalEvent,
    TemporalEventKind,
    TrackedObject,
    TrackState,
)

__all__ = ["SpatialTracker", "TrackUpdate"]


@dataclass(frozen=True, slots=True)
class TrackUpdate:
    """The result of one tracker step: who is present, and what changed."""

    tracks: tuple[TrackedObject, ...] = ()
    events: tuple[TemporalEvent, ...] = ()
    matched: int = 0
    created: int = 0
    missed: int = 0
    removed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tracks": [track.to_dict() for track in self.tracks],
            "events": [event.to_dict() for event in self.events],
            "matched": self.matched,
            "created": self.created,
            "missed": self.missed,
            "removed": self.removed,
        }


@dataclass(slots=True)
class _Track:
    """The tracker's own record for one identity — mutable, and not exported."""

    track_id: str
    label: str
    bbox: BBox
    source: str = ""
    confidence: float | None = None
    first_seen: str = ""
    last_seen: str = ""
    frame_count: int = 1
    missed_frames: int = 0
    state: TrackState = TrackState.NEW
    previous_bbox: BBox | None = None
    previous_label: str = ""
    center: tuple[float, float] = (0.0, 0.0)
    previous_center: tuple[float, float] | None = None
    last_timestamp: str = ""
    velocity: tuple[float, float] | None = None


def _seconds(value: str) -> float | None:
    """An ISO-8601 timestamp as seconds, or ``None`` when unreadable.

    Unreadable is a real answer here: a velocity needs two timestamps, and one
    that could not be parsed must not be treated as zero seconds elapsed (which
    would produce a velocity of infinity).
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _distance(left: tuple[float, float], right: tuple[float, float]) -> float:
    return float(((left[0] - right[0]) ** 2 + (left[1] - right[1]) ** 2) ** 0.5)


class SpatialTracker:
    """Matches detections to tracks by position, and reports what that implies.

    Every tunable is named here and none is hidden: how much overlap counts as
the same thing (``iou_threshold``), how far a box may travel and still be
    considered it (``max_distance_ratio`` of the frame diagonal), how long an
    identity survives being unseen (``max_missed``), and how far it must move
    before the movement is worth reporting (``move_threshold_ratio``).
    """

    def __init__(
        self,
        *,
        iou_threshold: float = 0.2,
        max_distance_ratio: float = 0.12,
        max_missed: int = 3,
        move_threshold_ratio: float = 0.01,
    ) -> None:
        self.iou_threshold = max(0.0, min(1.0, iou_threshold))
        self.max_distance_ratio = max(0.0, max_distance_ratio)
        self.max_missed = max(1, max_missed)
        self.move_threshold_ratio = max(0.0, move_threshold_ratio)
        self._tracks: dict[str, _Track] = {}
        self._counter = 0
        self._updates = 0

    # ── accounting ───────────────────────────────────────────────────────
    @property
    def updates(self) -> int:
        return self._updates

    @property
    def live_tracks(self) -> int:
        return len(self._tracks)

    def snapshot(self) -> tuple[TrackedObject, ...]:
        """The live identities as immutable records, in a stable order."""
        return tuple(self._as_tracked(track) for track in self._ordered())

    def reset(self) -> None:
        """Forget every identity — a new stream starts with no history."""
        self._tracks.clear()
        self._counter = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "iou_threshold": self.iou_threshold,
            "max_distance_ratio": self.max_distance_ratio,
            "max_missed": self.max_missed,
            "move_threshold_ratio": self.move_threshold_ratio,
            "updates": self._updates,
            "live_tracks": len(self._tracks),
            "tracks": [track.to_dict() for track in self.snapshot()],
        }

    def _ordered(self) -> tuple[_Track, ...]:
        """Tracks in a deterministic order, so two runs serialize identically."""
        return tuple(self._tracks[key] for key in sorted(self._tracks))

    # ── one step ─────────────────────────────────────────────────────────
    def update(
        self,
        detections: Sequence[DetectedObject],
        *,
        frame_id: str = "",
        timestamp: str = "",
        width: int | None = None,
        height: int | None = None,
    ) -> TrackUpdate:
        """Match this frame's detections to the live tracks, and age the rest.

        The frame's diagonal is what makes the distance tolerance
        resolution-independent: "it moved a tenth of the frame" means the same
        thing on a 720p capture and a 4K one.
        """
        self._updates += 1
        diagonal = (
            float((width * width + height * height) ** 0.5)
            if width and height
            else 0.0
        )
        assignments = self._match(detections, diagonal)
        events: list[TemporalEvent] = []
        produced: list[TrackedObject] = []
        matched = created = missed = removed = 0
        seen: set[str] = set()
        for index, detection in enumerate(detections):
            matched_id = assignments.get(index)
            if matched_id is None:
                track = self._create(detection, timestamp)
                created += 1
                events.append(
                    TemporalEvent(
                        kind=TemporalEventKind.OBJECT_APPEARED,
                        object_id=track.track_id,
                        label=track.label,
                        current_bbox=track.bbox,
                        detail="appeared in this frame",
                        timestamp=timestamp,
                        frame_id=frame_id,
                        confidence=track.confidence,
                    )
                )
            else:
                track = self._tracks[matched_id]
                events.extend(
                    self._advance(track, detection, timestamp, frame_id, diagonal)
                )
                matched += 1
            seen.add(track.track_id)
            produced.append(self._as_tracked(track))
        for key in sorted(self._tracks):
            if key in seen:
                continue
            track = self._tracks[key]
            track.missed_frames += 1
            missed += 1
            if track.missed_frames >= self.max_missed:
                track.state = TrackState.REMOVED
                removed += 1
                events.append(
                    self._disappearance(
                        track,
                        frame_id,
                        timestamp,
                        f"removed after {track.missed_frames} unseen frames",
                    )
                )
                produced.append(self._as_tracked(track))
                del self._tracks[key]
            elif track.missed_frames >= 2:
                track.state = TrackState.LOST
                events.append(
                    self._disappearance(
                        track,
                        frame_id,
                        timestamp,
                        f"not seen for {track.missed_frames} frames",
                    )
                )
                produced.append(self._as_tracked(track))
            else:
                track.state = TrackState.OCCLUDED
                produced.append(self._as_tracked(track))
        return TrackUpdate(
            tracks=tuple(produced),
            events=tuple(events),
            matched=matched,
            created=created,
            missed=missed,
            removed=removed,
        )

    def _match(self, detections: Sequence[DetectedObject], diagonal: float) -> dict[int, str]:
        """Which detection continues which track — greedily, deterministically.

        Candidates are scored (overlap beats proximity, and the same label gets a
        bonus because a relabelling is rarer than a move), then assigned highest
        score first with ties broken by index and track id. Greedy matching can be
        wrong in principle and is right far more often than not here: the boxes
        it is sorting are a handful of OCR words and regions, not hundreds of
        crowds, and a deterministic answer is what a test can pin.
        """
        candidates: list[tuple[float, int, str]] = []
        allowed_distance = self.max_distance_ratio * diagonal if diagonal else 0.0
        for index, detection in enumerate(detections):
            if not detection.located:
                continue
            centre = detection.bbox.center
            for key, track in self._tracks.items():
                overlap = detection.bbox.iou(track.bbox)
                if overlap >= self.iou_threshold:
                    score = 0.5 + overlap
                else:
                    if not allowed_distance:
                        continue
                    distance = _distance(centre, track.center)
                    if distance > allowed_distance:
                        continue
                    score = 0.5 * (1.0 - distance / allowed_distance)
                if detection.label and detection.label == track.label:
                    score += 0.2
                candidates.append((score, index, key))
        candidates.sort(key=lambda row: (-row[0], row[1], row[2]))
        used_detections: set[int] = set()
        used_tracks: set[str] = set()
        assignments: dict[int, str] = {}
        for _score, index, key in candidates:
            if index in used_detections or key in used_tracks:
                continue
            used_detections.add(index)
            used_tracks.add(key)
            assignments[index] = key
        return assignments

    def _create(self, detection: DetectedObject, timestamp: str) -> _Track:
        """A new identity for a detection nothing matched."""
        self._counter += 1
        track = _Track(
            track_id=f"t{self._counter}",
            label=detection.label,
            bbox=detection.bbox,
            source=detection.source,
            confidence=detection.confidence,
            first_seen=timestamp,
            last_seen=timestamp,
            state=TrackState.NEW,
            center=detection.bbox.center,
            last_timestamp=timestamp,
        )
        self._tracks[track.track_id] = track
        return track

    def _advance(
        self,
        track: _Track,
        detection: DetectedObject,
        timestamp: str,
        frame_id: str,
        diagonal: float,
    ) -> list[TemporalEvent]:
        """Carry a track forward, returning the movement worth reporting."""
        events: list[TemporalEvent] = []
        previous_bbox = track.bbox
        previous_center = track.center
        centre = detection.bbox.center
        moved = _distance(previous_center, centre)
        threshold = self.move_threshold_ratio * diagonal if diagonal else 0.0
        if moved > 0 and (threshold <= 0 or moved > threshold):
            delta_x = centre[0] - previous_center[0]
            delta_y = centre[1] - previous_center[1]
            events.append(
                TemporalEvent(
                    kind=TemporalEventKind.OBJECT_MOVED,
                    object_id=track.track_id,
                    label=track.label,
                    previous_bbox=previous_bbox,
                    current_bbox=detection.bbox,
                    detail=(
                        f"moved {abs(delta_x):.0f}px "
                        f"{'right' if delta_x >= 0 else 'left'}, "
                        f"{abs(delta_y):.0f}px {'down' if delta_y >= 0 else 'up'}"
                    ),
                    timestamp=timestamp,
                    frame_id=frame_id,
                    confidence=detection.confidence,
                )
            )
        if detection.label and detection.label != track.label:
            track.previous_label = track.label
            track.label = detection.label
        elapsed = _elapsed_seconds(track.last_timestamp, timestamp)
        if elapsed and elapsed > 0:
            delta_x = centre[0] - previous_center[0]
            delta_y = centre[1] - previous_center[1]
            track.velocity = (delta_x / elapsed, delta_y / elapsed)
        elif elapsed is None:
            # One timestamp unreadable is not zero elapsed time: reporting a
            # velocity computed from a made-up interval would be a fabricated
            # measurement, so the field stays unset.
            track.velocity = None
        track.previous_bbox = previous_bbox
        track.previous_center = previous_center
        track.bbox = detection.bbox
        track.center = centre
        track.confidence = detection.confidence
        track.source = detection.source
        track.last_seen = timestamp
        track.last_timestamp = timestamp
        track.frame_count += 1
        track.missed_frames = 0
        track.state = TrackState.VISIBLE
        return events

    def _disappearance(
        self, track: _Track, frame_id: str, timestamp: str, detail: str
    ) -> TemporalEvent:
        """The one event a lost identity produces, with its last known box."""
        return TemporalEvent(
            kind=TemporalEventKind.OBJECT_DISAPPEARED,
            object_id=track.track_id,
            label=track.label,
            previous_bbox=track.bbox,
            current_bbox=None,
            detail=detail,
            timestamp=timestamp or track.last_seen,
            frame_id=frame_id,
            confidence=track.confidence,
        )

    @staticmethod
    def _as_tracked(track: _Track) -> TrackedObject:
        """The tracker's mutable record as the exported, immutable one."""
        return TrackedObject(
            track_id=track.track_id,
            label=track.label,
            bbox=track.bbox,
            previous_bbox=track.previous_bbox,
            velocity=track.velocity,
            confidence=track.confidence,
            first_seen=track.first_seen,
            last_seen=track.last_seen,
            frame_count=track.frame_count,
            missed_frames=track.missed_frames,
            state=track.state,
            source=track.source,
            previous_label=track.previous_label,
        )


def _elapsed_seconds(before: str, after: str) -> float | None:
    """Seconds between two ISO timestamps, or ``None`` when either is unreadable."""
    earlier = _seconds(before)
    later = _seconds(after)
    if earlier is None or later is None:
        return None
    return later - earlier

