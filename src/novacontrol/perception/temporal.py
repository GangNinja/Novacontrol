"""Temporal perception: what CHANGED between two observations, and nothing more.

The tracker answers "is this the same object?"; this layer answers a different
question — "what is different about the scene?" — and it answers it by comparing
two scene records field by field:

    text          lines appeared, changed, vanished (normalized, so a re-wrap is
                  not a change)
    composition   the multiset of object labels changed (a thing arrived or left)
    confidence    a measured scene confidence moved by more than a threshold
    movement      the tracker's own events for objects it followed

The separation matters because these are different kinds of evidence. Text is
exact: two line lists either match or they do not. Composition is a count, not an
identity. Movement needs the tracker, because movement is a property of an
identity and nothing else in the pipeline produces one.

What this module deliberately does NOT do is predict. There is no field for
"the user is about to click", no trend, no extrapolated position: an event here
describes two frames that were actually observed, or it does not exist. Whether
the change matters is a consumer's judgement, and building one in would be
Phase 22's world model.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from novacontrol.perception.models import (
    SceneRepresentation,
    TemporalEvent,
    TemporalEventKind,
)

__all__ = ["TemporalPerception"]


@dataclass(frozen=True, slots=True)
class _Baseline:
    """The comparable parts of the previous scene, kept small on purpose."""

    lines: tuple[str, ...] = ()
    labels: Counter[str] | None = None
    confidence: float | None = None
    scene_id: str = ""


def _normalized_lines(scene: SceneRepresentation) -> tuple[str, ...]:
    """The scene's text as trimmed, case-folded non-empty lines.

    Case folding is deliberate: an OCR engine that returns ``Save`` on one frame
    and ``save`` on the next has not seen a change, it has seen the same word.
    Whitespace is folded for the same reason — a re-wrap is a layout detail, not
    content.
    """
    return tuple(
        " ".join(item.text.split()).casefold()
        for item in scene.text
        if item.text.strip()
    )


def _label_counts(scene: SceneRepresentation) -> Counter[str]:
    """How many of each label the scene claims — a count, not an identity."""
    return Counter(item.label for item in scene.objects if item.label)


class TemporalPerception:
    """Compares consecutive scenes, keeping only what comparison needs."""

    def __init__(
        self,
        *,
        confidence_delta: float = 0.1,
        move_threshold_ratio: float = 0.01,
        max_events: int = 200,
    ) -> None:
        self.confidence_delta = max(0.0, confidence_delta)
        self.move_threshold_ratio = max(0.0, move_threshold_ratio)
        self.max_events = max(1, max_events)
        self._baseline: _Baseline | None = None
        self._observations = 0
        self._changes = 0
        self._truncated = 0

    # ── accounting ───────────────────────────────────────────────────────
    @property
    def observations(self) -> int:
        return self._observations

    @property
    def changes(self) -> int:
        return self._changes

    @property
    def has_baseline(self) -> bool:
        return self._baseline is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "observations": self._observations,
            "changes": self._changes,
            "truncated_events": self._truncated,
            "has_baseline": self._baseline is not None,
            "confidence_delta": self.confidence_delta,
            "move_threshold_ratio": self.move_threshold_ratio,
            "baseline_scene": self._baseline.scene_id if self._baseline is not None else "",
        }

    def reset(self) -> None:
        """Forget the baseline — a new stream has nothing to compare against."""
        self._baseline = None

    # ── observation ──────────────────────────────────────────────────────
    def observe(
        self,
        scene: SceneRepresentation,
        *,
        object_events: Sequence[TemporalEvent] = (),
    ) -> tuple[TemporalEvent, ...]:
        """The events this scene produced, given the one before it.

        ``object_events`` are the tracker's own findings, passed in rather than
        guessed here: this layer has no identities of its own, and inventing a
        second tracking mechanism to produce them would be exactly the
        duplication the phase rules out.
        """
        self._observations += 1
        lines = _normalized_lines(scene)
        labels = _label_counts(scene)
        events: list[TemporalEvent] = list(object_events)
        baseline = self._baseline
        if baseline is None:
            self._baseline = _Baseline(
                lines=lines, labels=labels, confidence=scene.confidence, scene_id=scene.scene_id
            )
            return tuple(events[: self.max_events])
        events.extend(self._text_events(baseline, scene, lines))
        events.extend(self._confidence_events(baseline, scene))
        events.extend(self._composition_events(baseline, labels, scene, bool(object_events)))
        self._baseline = _Baseline(
            lines=lines, labels=labels, confidence=scene.confidence, scene_id=scene.scene_id
        )
        if events:
            self._changes += 1
        if len(events) > self.max_events:
            self._truncated += len(events) - self.max_events
            events = events[: self.max_events]
        return tuple(events)

    # ── the three comparisons ────────────────────────────────────────────
    def _text_events(
        self, baseline: _Baseline, scene: SceneRepresentation, lines: tuple[str, ...]
    ) -> list[TemporalEvent]:
        """What the text did — appeared, changed, vanished, or nothing."""
        if baseline.lines == lines:
            return []
        if not baseline.lines and lines:
            return [
                _event(
                    TemporalEventKind.TEXT_APPEARED,
                    scene,
                    detail=f"{len(lines)} line(s) of text appeared",
                )
            ]
        if baseline.lines and not lines:
            return [
                _event(
                    TemporalEventKind.TEXT_REMOVED,
                    scene,
                    detail=f"{len(baseline.lines)} line(s) of text are no longer visible",
                )
            ]
        added = len(set(lines) - set(baseline.lines))
        removed = len(set(baseline.lines) - set(lines))
        return [
            _event(
                TemporalEventKind.TEXT_CHANGED,
                scene,
                detail=f"text changed: {added} line(s) added, {removed} removed",
            )
        ]

    def _confidence_events(
        self, baseline: _Baseline, scene: SceneRepresentation
    ) -> list[TemporalEvent]:
        """A measured confidence that moved — only when BOTH sides were measured."""
        before = baseline.confidence
        now = scene.confidence
        if before is None or now is None:
            return []
        delta = now - before
        if abs(delta) < self.confidence_delta:
            return []
        kind = (
            TemporalEventKind.CONFIDENCE_INCREASED
            if delta > 0
            else TemporalEventKind.CONFIDENCE_DECREASED
        )
        return [
            _event(
                kind,
                scene,
                detail=f"scene confidence {before:.2f} -> {now:.2f}",
                confidence=now,
            )
        ]

    def _composition_events(
        self,
        baseline: _Baseline,
        labels: Counter[str],
        scene: SceneRepresentation,
        had_object_events: bool,
    ) -> list[TemporalEvent]:
        """Whether the scene's make-up changed — or whether nothing changed at all.

        ``SCENE_STATIC`` is emitted only when there is genuinely nothing to
        report: no tracker events, the same text and the same label counts. That
        makes it a usable "no change" signal instead of noise on every frame.
        """
        previous = baseline.labels or Counter()
        if previous != labels:
            added = labels - previous
            gone = previous - labels
            parts: list[str] = []
            if added:
                parts.append(", ".join(f"+{count} {label}" for label,
                    count in sorted(added.items())))
            if gone:
                parts.append(", ".join(f"-{count} {label}" for label,
                    count in sorted(gone.items())))
            return [
                _event(
                    TemporalEventKind.SCENE_CHANGED,
                    scene,
                    detail="; ".join(parts) or "the objects in the scene changed",
                )
            ]
        if had_object_events:
            return []
        previous_lines = baseline.lines
        if previous_lines != _normalized_lines(scene):
            return []
        return [
            _event(
                TemporalEventKind.SCENE_STATIC,
                scene,
                detail="no measured change since the previous frame",
            )
        ]


def _event(
    kind: TemporalEventKind,
    scene: SceneRepresentation,
    *,
    detail: str,
    confidence: float | None = None,
) -> TemporalEvent:
    """One scene-level event, stamped with the frame it was observed on."""
    return TemporalEvent(
        kind=kind,
        object_id="",
        label="",
        detail=detail,
        timestamp=scene.timestamp,
        frame_id=scene.frame_id,
        confidence=confidence if confidence is not None else scene.confidence,
    )


def events_by_kind(
    events: Sequence[TemporalEvent],
) -> Mapping[str, int]:
    """Event counts by kind — the shape telemetry and tests want."""
    counts: Counter[str] = Counter(event.kind.value for event in events)
    return dict(sorted(counts.items()))
